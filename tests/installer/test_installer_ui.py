"""Installer UI (browser wizard) acceptance tests.

Exercises ``installer/ui/server.py`` — the local web UI that fronts the
guided installer — against the same fake ssh host + fake HA REST API used
by the CLI wizard tests, covering:

* HTTP boundary: session token + Host-header guards, static assets;
* the answers API (schema enforcement, secret fields never accepted);
* read-only probes (HA API/auth, ssh, AppDaemon HTTP, config-root
  detection, remote state classification) proving no remote writes;
* the review plan: profile validation through ``scene-studio-profile.py``,
  the rendered apps.yaml block, and the merge preview semantics (a faithful
  port of the wizard's deterministic merge — unit-tested separately);
* the apply gate (confirmation required, 409 while running) and one FULL
  fresh install driven end-to-end through the real unattended wizard on the
  fake host, asserting the log stream never carries the token value;
* the release tree ships the UI and its server boots there in release mode
  (manifest present, root wizard located).
"""

import http.client
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_installer_acceptance import (  # noqa: F401
    ADDON_ROOT,
    HAS_PWSH,
    FakeHaApi,
    _make_env,
    _read_calls,
    _write_ssh_shim,
    release_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_PY = REPO_ROOT / "installer" / "ui" / "server.py"

FAKE_HA_TOKEN = "ui-token-do-not-persist-0123456789"

WRITE_VERBS = ("mv ", "install -d", "rm -rf", "tee ", "chown", "tar ", "cp -a", "chmod ")


def _load_server_module():
    spec = importlib.util.spec_from_file_location("installer_ui_server", SERVER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_fresh_target(state_dir: Path) -> None:
    addon = state_dir / "fs" / ADDON_ROOT.strip("/")
    addon.mkdir(parents=True, exist_ok=True)
    (addon / "appdaemon.yaml").write_text("appdaemon:\n  latitude: 0\n", encoding="utf-8")
    (state_dir / "apps_mode").write_text("registry_admin", encoding="utf-8")


class UiClient:
    """Direct HTTP client for the local UI server (controls token + Host)."""

    def __init__(self, port: int, token: str):
        self.port = port
        self.token = token

    def request(self, method: str, path: str, body=None, *, token: str | None = None, host: str | None = None, timeout: int = 60):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        headers = {"X-Scene-Studio-Token": self.token if token is None else token}
        if host is not None:
            headers["Host"] = host
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"raw": raw.decode("utf-8", errors="replace")}
        return response.status, parsed

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body, **kwargs)


class UiEnv:
    def __init__(self, client: UiClient, state_dir: Path, module, ha_port: int, session):
        self.client = client
        self.state_dir = state_dir
        self.module = module
        self.ha_port = ha_port
        self.session = session

    def set_answers(self, **overrides) -> None:
        answers = {
            "ha_url": f"http://127.0.0.1:{self.ha_port}",
            "ssh_host": "fake-ha",
            "ssh_user": "friend",
            "ssh_port": 2222,
            "appdaemon_config_root": ADDON_ROOT,
            "store_root": "/config/scene_studio_store",
            "appdaemon_http_url": f"http://appdaemon.example.test:{self.ha_port}",
            "install_ha_card": True,
            "providers": {
                "ha_light": True,
                "hue": {"enabled": False, "host": None, "bridge_id": None},
                "wled": {"enabled": False, "host": None},
                "hyperhdr": {"enabled": False, "host": None},
            },
        }
        answers.update(overrides)
        status, payload = self.client.post("/api/answers", answers)
        assert status == 200, payload

    def assert_no_remote_writes(self) -> None:
        for call in _read_calls(self.state_dir):
            joined = " ".join(call["argv"])
            for verb in WRITE_VERBS:
                assert verb not in joined, f"unexpected remote write: {joined[:200]}"


@pytest.fixture()
def ui_env(tmp_path, monkeypatch):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    shim_dir = _write_ssh_shim(tmp_path / "shim")
    monkeypatch.setenv("FAKE_HA_STATE", str(state_dir))
    monkeypatch.setenv("SCENE_STUDIO_HA_TOKEN", FAKE_HA_TOKEN)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    module = _load_server_module()
    with FakeHaApi() as api:
        session = module.InstallerSession()
        server = module.InstallerUiServer(("127.0.0.1", 0), module.InstallerUiHandler, session)
        module.InstallerUiHandler.session = session
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield UiEnv(UiClient(server.server_address[1], session.token), state_dir, module, api.port, session)
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# the merge preview port (pure; runs everywhere)
# ---------------------------------------------------------------------------


def _server_module_for_unit_tests():
    return _load_server_module()


def test_merge_preview_empty_file_gets_block_only():
    module = _server_module_for_unit_tests()
    merged = module.merge_block_preview("", "scene_studio:\n  class: SceneStudioApp")
    assert merged.startswith(module.BLOCK_START_MARKER + "\n")
    assert merged.endswith(module.BLOCK_END_MARKER + "\n")


def test_merge_preview_replaces_managed_block_exactly():
    module = _server_module_for_unit_tests()
    original = (
        "legacy_app:\n"
        "  module: legacy.mod\n"
        + module.BLOCK_START_MARKER
        + "\nscene_studio:\n  class: OLD\n"
        + module.BLOCK_END_MARKER
        + "\ntrailing_app:\n  module: t.m\n"
    )
    merged = module.merge_block_preview(original, "scene_studio:\n  class: NEW")
    assert "  class: NEW" in merged
    assert "OLD" not in merged
    assert "legacy_app:" in merged and "trailing_app:" in merged
    assert merged.count(module.BLOCK_START_MARKER) == 1
    assert merged.endswith("\n")


def test_merge_preview_bounds_bare_scene_studio_key():
    module = _server_module_for_unit_tests()
    original = (
        "scene_studio:\n"
        "  class: OLD\n"
        "  store_root: /old\n"
        "next_app:\n"
        "  module: n.m\n"
    )
    merged = module.merge_block_preview(original, "scene_studio:\n  class: NEW")
    assert "class: NEW" in merged
    assert "OLD" not in merged and "/old" not in merged
    assert merged.index("next_app:") > merged.index(module.BLOCK_END_MARKER)


def test_merge_preview_rejects_unterminated_block():
    module = _server_module_for_unit_tests()
    with pytest.raises(module.UiError):
        module.merge_block_preview(module.BLOCK_START_MARKER + "\nno end marker\n", "scene_studio:\n  class: NEW")


# ---------------------------------------------------------------------------
# HTTP boundary
# ---------------------------------------------------------------------------


def test_ui_static_served_without_token_and_api_rejects_wrong_token(ui_env):
    status, body = ui_env.client.get("/api/session", token="wrong-token")
    assert status == 401

    status, body = ui_env.client.get("/api/session", host="evil.example:1")
    assert status == 403

    # static assets are served (no token) — they carry no secrets
    conn = http.client.HTTPConnection("127.0.0.1", ui_env.client.port, timeout=30)
    conn.request("GET", "/")
    response = conn.getresponse()
    index = response.read()
    conn.close()
    assert response.status == 200
    assert b"Scene Studio Installer" in index
    assert FAKE_HA_TOKEN.encode() not in index


def test_ui_session_reports_bundle_and_workstation(ui_env):
    status, session = ui_env.client.get("/api/session")
    assert status == 200
    assert session["bundle_mode"] == "development"
    assert session["wizard"].endswith("Install-SceneStudio.ps1")
    assert session["version"]
    assert session["workstation"]["python"]
    assert session["answers"]["store_root"] == "/config/scene_studio_store"
    assert session["secrets_entered"]["ha_token"] is True  # from the environment
    assert FAKE_HA_TOKEN not in json.dumps(session)


def test_ui_answers_schema_rejects_unknown_keys_and_probes(ui_env):
    status, payload = ui_env.client.post("/api/answers", {"nonsense": True})
    assert status == 400

    status, payload = ui_env.client.post("/api/answers", {"providers": {"hue": {"unknown": 1}}})
    assert status == 400

    status, payload = ui_env.client.post("/api/probe", {"names": ["not_a_probe"]})
    assert status == 400

    status, payload = ui_env.client.post("/api/secrets", {"ha_token": "value"})
    assert status == 200  # accepted, stored in memory only


# ---------------------------------------------------------------------------
# read-only probes against the fake target
# ---------------------------------------------------------------------------


def test_ui_probes_are_read_only_and_structured(ui_env):
    ui_env.set_answers()
    status, payload = ui_env.client.post(
        "/api/probe",
        {"names": ["ha_api", "ha_auth", "ssh", "appdaemon_http", "appdaemon_root", "remote_state"]},
    )
    assert status == 200
    results = payload["results"]

    assert results["ha_api"]["status"] == "ok"
    assert results["ha_auth"]["status"] == "ok"
    assert results["ha_auth"]["data"]["version"] == "2026.9.10"
    assert results["ssh"]["status"] == "ok"
    # the explicit endpoint name does not resolve from the test box: an
    # explicit-but-unreachable Scene Studio HTTP endpoint is a warning the
    # installer tolerates (same semantics as the CLI wizard)
    assert results["appdaemon_http"]["status"] == "warn"
    assert results["appdaemon_root"]["status"] == "ok"
    assert results["appdaemon_root"]["data"]["selected"] == ADDON_ROOT

    remote = results["remote_state"]
    assert remote["status"] == "ok"
    assert remote["data"]["install_kind"] == "fresh"
    assert remote["data"]["apps_yaml_present"] is False
    ui_env.assert_no_remote_writes()
    assert FAKE_HA_TOKEN not in json.dumps(results)


def test_ui_probe_reports_unreachable_ha_as_failed_probe_not_error(ui_env):
    ui_env.set_answers(ha_url="http://127.0.0.1:1")
    status, payload = ui_env.client.post("/api/probe", {"names": ["ha_api"]})
    assert status == 200
    result = payload["results"]["ha_api"]
    assert result["status"] == "fail"
    assert "not reachable" in result["detail"]


def test_ui_remote_state_detects_upgrade_and_preserved_mode(ui_env):
    addon_fs = ui_env.state_dir / "fs" / ADDON_ROOT.strip("/")
    (addon_fs / "apps" / "scene_studio" / "appdaemon_adapter").mkdir(parents=True, exist_ok=True)
    (addon_fs / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py").write_text("# old\n", encoding="utf-8")
    (ui_env.state_dir / "apps_mode").write_text("normal", encoding="utf-8")
    ui_env.set_answers()
    status, payload = ui_env.client.post("/api/probe", {"names": ["remote_state"]})
    assert status == 200
    data = payload["results"]["remote_state"]["data"]
    assert data["install_kind"] == "upgrade"
    assert data["runtime_mode"] == "normal"
    assert data["backend_present"] is True


# ---------------------------------------------------------------------------
# the review plan
# ---------------------------------------------------------------------------


def test_ui_plan_fresh_install(ui_env):
    ui_env.set_answers()
    ui_env.client.post("/api/probe", {"names": ["remote_state"]})
    status, plan = ui_env.client.post("/api/plan", {})
    assert status == 200
    assert plan["ok"] is True, plan.get("errors")
    assert plan["install_kind"] == "fresh"
    assert plan["runtime_mode"] == "registry_admin"
    block = plan["block"]
    assert "scene_studio:" in block
    assert "registry_admin: true" in block
    assert "store_root: /config/scene_studio_store" in block
    assert plan["apps_yaml_present"] is False
    labels = {row["label"] for row in plan["summary"]}
    assert {"Home Assistant API", "AppDaemon (ssh)", "Scene Studio HTTP", "Runtime mode"} <= labels
    assert plan["block"].startswith("# Scene Studio")


def test_ui_plan_fails_readonly_on_bad_store_root(ui_env):
    ui_env.set_answers(store_root="not-a-posix-path")
    status, plan = ui_env.client.post("/api/plan", {})
    assert status == 200
    assert plan["ok"] is False
    assert any("store_root" in error for error in plan["errors"])
    ui_env.assert_no_remote_writes()


# ---------------------------------------------------------------------------
# apply gate + one full fresh install through the real unattended wizard
# ---------------------------------------------------------------------------


def test_ui_apply_gate_requires_confirmation_and_running_lock(ui_env):
    ui_env.set_answers()
    status, payload = ui_env.client.post("/api/apply", {"confirm": "yes"})
    assert status == 400

    ui_env.session.run_status = "running"  # simulate an active install
    try:
        status, payload = ui_env.client.post("/api/apply", {"confirm": "INSTALL"})
        assert status == 409
    finally:
        ui_env.session.run_status = "idle"


def test_ui_apply_missing_answers_rejected_before_any_process(ui_env):
    status, payload = ui_env.client.post("/api/apply", {"confirm": "INSTALL"})
    assert status == 400
    ui_env.assert_no_remote_writes()


def test_ui_full_fresh_install_through_unattended_wizard(ui_env):
    ui_env.set_answers()
    status, _ = ui_env.client.post("/api/probe", {"names": ["remote_state"]})
    assert status == 200
    status, payload = ui_env.client.post("/api/apply", {"confirm": "INSTALL"})
    assert status == 200, payload

    log_text_parts: list[str] = []
    seen = 0
    deadline = time.time() + 900
    while time.time() < deadline:
        status, snap = ui_env.client.get(f"/api/run?since={seen}")
        assert status == 200
        for line in snap["lines"]:
            log_text_parts.append(line["text"])
            seen = max(seen, line["n"])
        if snap["status"] != "running":
            assert snap["status"] == "success", "install failed; log tail:\n" + "\n".join(log_text_parts[-40:])
            assert snap["returncode"] == 0
            break
        time.sleep(1)

    log_text = "\n".join(log_text_parts)
    assert "Installation complete" in log_text
    assert "registry_admin" in log_text
    assert "Open the Workbench:  http://appdaemon.example.test" in log_text
    # the token value must never surface in the streamed log
    assert FAKE_HA_TOKEN not in log_text

    fs = ui_env.state_dir / "fs"
    assert (fs / ADDON_ROOT.strip("/") / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py").is_file()
    assert (fs / ADDON_ROOT.strip("/") / "www" / "scene_studio" / "index.html").is_file()
    apps_yaml = (fs / ADDON_ROOT.strip("/") / "apps.yaml").read_text(encoding="utf-8")
    assert "# >>> scene_studio managed block" in apps_yaml
    assert "registry_admin: true" in apps_yaml

    # the generated answers file carries NO secret values (policy: env only)
    status, snap = ui_env.client.get("/api/run?since=0")
    answers_path = Path(snap["answers_path"])
    assert answers_path.is_file()
    assert FAKE_HA_TOKEN not in answers_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the release tree ships the UI, and its server boots there in release mode
# ---------------------------------------------------------------------------


def test_release_tree_ships_installer_ui(release_dir):
    assert (release_dir / "installer" / "ui" / "server.py").is_file()
    assert (release_dir / "installer" / "ui" / "static" / "index.html").is_file()
    assert (release_dir / "installer" / "ui" / "static" / "app.js").is_file()
    assert (release_dir / "installer" / "ui" / "static" / "style.css").is_file()


def test_release_ui_server_boots_in_release_mode(release_dir):
    process = subprocess.Popen(
        [sys.executable, str(release_dir / "installer" / "ui" / "server.py"), "--port", "0", "--no-browser"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(release_dir),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        banner = ""
        deadline = time.time() + 30
        match = None
        while time.time() < deadline and match is None:
            line = process.stdout.readline()
            if not line:
                break
            banner += line
            match = re.search(r"open   : (http://127\.0\.0\.1:(\d+)/\?token=\S+)", banner)
        assert match, f"server did not print a launch URL:\n{banner}"
        url = match.group(1)
        port = int(match.group(2))
        token = url.split("token=")[1]

        client = UiClient(port, token)
        status, session = client.get("/api/session")
        assert status == 200
        assert session["bundle_mode"] == "release"
        assert session["manifest_present"] is True
        assert session["wizard"] == str(release_dir / "Install-SceneStudio.ps1")

        status, payload = client.get("/api/session", token="nope")
        assert status == 401
    finally:
        process.terminate()
        process.wait(timeout=30)
