"""Installer acceptance harness for the standalone Scene Studio repository.

Operates on the GENERATED RELEASE TREE (`scripts/build_release.py` output —
the artifact friends actually download) through a fake ssh host + fake HA
REST API, and exercises the internal installer boundary:

* release build + gates (allowlist, portability scan, secret scan, imports,
  MANIFEST.sha256);
* ``installer/verify_bundle_manifest.ps1`` accept/tamper/missing/extra paths;
* installer preflight (read-only) proving no remote writes before ``-Apply``;
* a full FRESH INSTALL end to end through the stubs (absent backend +
  Workbench trees -> staged/hash-checked/activated -> AppDaemon restarted ->
  first health check lands in ``registry_admin``; prebuilt Workbench path
  with no npm);
* the UPGRADE path on a pre-seeded install (backup archive + retained
  previous tree + mode preservation);
* the fresh-install profile rule (``runtime_mode: registry_admin`` required)
  and the provider-reachability preflight gate on ``-Apply``.

The fake remote is ``fake_remote_ha.py``: a stateful fake filesystem behind
an ``ssh`` PATH shim plus a localhost HA REST stub (config, addon restart,
and the AppDaemon scene_studio_api envelope). Everything remote is recorded
in ``calls.log`` so tests can prove read-only paths never wrote.

Wizard-level coverage lives in ``test_wizard_acceptance.py``.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUPPORT_DIR = Path(__file__).resolve().parent
FAKE_SSH = SUPPORT_DIR / "fake_remote_ha.py"
BUILDER = REPO_ROOT / "scripts" / "build_release.py"
INSTALLER = REPO_ROOT / "installer" / "install_scene_studio.ps1"
MANIFEST_TOOL = REPO_ROOT / "installer" / "verify_bundle_manifest.ps1"
PROFILE_EXAMPLE = REPO_ROOT / "installer" / "scene-studio.profile.example.json"

ADDON_ROOT = "/addon_configs/fake_addon"

HAS_PWSH = sys.platform == "win32" and shutil.which("pwsh") is not None

pytestmark = pytest.mark.skipif(not HAS_PWSH, reason="acceptance harness needs pwsh on Windows")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeHaApi:
    """Localhost HA REST stub: /api/config, addon_restart, scene_studio_api."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.restart_calls = 0

        class Handler(BaseHTTPRequestHandler):
            def _send(handler, code, payload):
                body = json.dumps(payload).encode("utf-8")
                handler.send_response(code)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def do_GET(handler):  # noqa: N802
                if handler.path == "/api/config":
                    handler._send(200, {"version": "2026.9.10"})
                else:
                    handler._send(404, {"message": "not found"})

            def do_POST(handler):  # noqa: N802
                if handler.path == "/api/services/hassio/addon_restart":
                    self.restart_calls += 1
                    handler._send(200, {"result": "ok", "data": {}})
                elif handler.path == "/api/appdaemon/scene_studio_api":
                    handler._send(200, {
                        "status": 200,
                        "body": {"runtime": {"mode": "registry_admin", "allowed_commands": []}},
                    })
                else:
                    handler._send(404, {"message": "not found"})

            def log_message(handler, *args):  # silence
                return

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._server.shutdown()
        self._server.server_close()


def _write_ssh_shim(shim_dir: Path) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "ssh.cmd"
    shim.write_text(
        '@echo off\r\n'
        f'python "{FAKE_SSH}" %*\r\n',
        encoding="utf-8",
    )
    return shim_dir


def _make_env(state_dir: Path, shim_dir: Path) -> dict:
    env = dict(os.environ)
    env["FAKE_HA_STATE"] = str(state_dir)
    env["SCENE_STUDIO_HA_TOKEN"] = "fake-token-for-acceptance"
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_ps(script: Path, arguments: list[str], env: dict, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _read_calls(state_dir: Path) -> list[dict]:
    log = state_dir / "calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_profile(path: Path, *, ha_url: str, runtime_mode: str = "registry_admin", wled_host: str | None = None) -> None:
    profile = json.loads(PROFILE_EXAMPLE.read_text(encoding="utf-8"))
    profile["ha_url"] = ha_url
    profile["ha_ssh_host"] = "fake-ha"
    profile["appdaemon_config_root"] = ADDON_ROOT
    profile["store_root"] = "/config/scene_studio_store"
    profile["runtime_mode"] = runtime_mode
    profile["providers"]["hue"] = {"enabled": False, "host": None, "application_key_secret": None, "bridge_id": None}
    if wled_host is None:
        profile["providers"]["wled"] = {"enabled": False, "hosts": []}
    else:
        profile["providers"]["wled"] = {"enabled": True, "hosts": [wled_host]}
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def _seed_upgrade_state(state_dir: Path) -> None:
    """A pre-existing install: backend + workbench trees, apps in normal mode."""
    fs = state_dir / "fs"
    backend = fs / ADDON_ROOT.strip("/") / "apps" / "scene_studio" / "appdaemon_adapter"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "adapter.py").write_text("# previous backend install\n", encoding="utf-8")
    (backend.parent / "__init__.py").write_text("", encoding="utf-8")
    www = fs / ADDON_ROOT.strip("/") / "www" / "scene_studio"
    www.mkdir(parents=True, exist_ok=True)
    (www / "index.html").write_text(
        "<!doctype html><html><head><title>Scene Studio Workbench</title></head><body>old</body></html>",
        encoding="utf-8",
    )
    (state_dir / "apps_mode").write_text("normal", encoding="utf-8")


# ---------------------------------------------------------------------------
# session-scoped release build (the artifact under test)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def release_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("scene-studio-release")
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--out", str(out), "--refresh", "--zip"],
        capture_output=True,
        text=True,
        timeout=1200,
    )
    assert result.returncode == 0, f"release build failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    assert (out / "MANIFEST.sha256").is_file()
    # distribution layout: the release root IS the product tree (no nesting)
    assert (out / "Install-SceneStudio.ps1").is_file()
    assert (out / "workbench" / "dist" / "index.html").is_file()
    assert (out / "home-assistant" / "scene-studio-card" / "dist" / "scene-studio-card.js").is_file()
    assert not (out / "support").exists(), "the monorepo support/ nesting must be gone"
    return out


@pytest.fixture()
def fresh_env(tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    state_dir.mkdir()
    (state_dir / "apps_mode").write_text("registry_admin", encoding="utf-8")
    # the add-on config root itself always exists on a real host (the add-on
    # created it); apps/ and www/ may not - that is the fresh-install case
    (state_dir / "fs" / ADDON_ROOT.strip("/")).mkdir(parents=True)
    shim_dir = _write_ssh_shim(tmp_path / "shim")
    api = FakeHaApi()
    with api:
        yield {
            "state_dir": state_dir,
            "env": _make_env(state_dir, shim_dir),
            "ha_url": f"http://127.0.0.1:{api.port}",
            "api": api,
            "tmp": tmp_path,
        }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_release_manifest_verification_accept_and_reject(release_dir):
    ok = _run_ps(MANIFEST_TOOL, ["-BundleRoot", str(release_dir)], dict(os.environ))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "byte-exact" in ok.stdout

    victim = release_dir / "installer" / "scene_studio_profile.py"
    original = victim.read_bytes()
    try:
        victim.write_bytes(original + b"\n# tampered\n")
        tampered = _run_ps(MANIFEST_TOOL, ["-BundleRoot", str(release_dir)], dict(os.environ))
        assert tampered.returncode != 0
        assert "mismatch for" in (tampered.stdout + tampered.stderr).lower()

        victim.unlink()
        missing = _run_ps(MANIFEST_TOOL, ["-BundleRoot", str(release_dir)], dict(os.environ))
        assert missing.returncode != 0
        assert "missing from bundle" in (missing.stdout + missing.stderr)
    finally:
        victim.write_bytes(original)

    extra = release_dir / "smuggled.txt"
    extra.write_text("unlisted payload", encoding="utf-8")
    try:
        extra_added = _run_ps(MANIFEST_TOOL, ["-BundleRoot", str(release_dir)], dict(os.environ))
        assert extra_added.returncode != 0
        assert "not listed" in (extra_added.stdout + extra_added.stderr)
    finally:
        extra.unlink()


def test_installer_preflight_is_read_only(release_dir, fresh_env, tmp_path):
    profile = tmp_path / "fresh.profile.json"
    _write_profile(profile, ha_url=fresh_env["ha_url"])
    result = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(profile)], fresh_env["env"])
    assert result.returncode == 0, f"stdout:\n{result.stdout[-3000:]}\nstderr:\n{result.stderr[-3000:]}"
    assert "Preflight OK" in result.stdout
    assert "ABSENT (fresh install)" in result.stdout
    # no remote writes happened: the recorded calls are classification/reads only
    forbidden = ("mv ", "install -d", "rm -rf", "tee ", "chown", "tar ")
    for call in _read_calls(fresh_env["state_dir"]):
        joined = " ".join(call["argv"])
        for verb in forbidden:
            assert verb not in joined, f"preflight issued a write: {joined[:200]}"


def test_installer_rejects_nonexistent_addon_root(release_dir, fresh_env, tmp_path):
    """A mistyped add-on root fails READ-ONLY preflight as itself — never as
    a valid fresh install."""
    profile = tmp_path / "bad-root.profile.json"
    _write_profile(profile, ha_url=fresh_env["ha_url"])
    profile_text = json.loads(profile.read_text(encoding="utf-8"))
    profile_text["appdaemon_config_root"] = "/addon_configs/does_not_exist"
    profile.write_text(json.dumps(profile_text, indent=2), encoding="utf-8")
    result = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(profile)], fresh_env["env"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "does not exist" in combined, combined[-800:]
    # it must NOT be misreported as a fresh install
    assert "fresh install" not in combined
    for call in _read_calls(fresh_env["state_dir"]):
        joined = " ".join(call["argv"])
        for verb in ("mv ", "install -d", "rm -rf", "tee ", "chown", "tar "):
            assert verb not in joined, f"invalid-root preflight issued a write: {joined[:200]}"


def test_fresh_install_end_to_end_through_stubs(release_dir, fresh_env, tmp_path):
    profile = tmp_path / "fresh.profile.json"
    _write_profile(profile, ha_url=fresh_env["ha_url"])
    result = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(profile), "-Apply"], fresh_env["env"])
    assert result.returncode == 0, f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"

    fs = fresh_env["state_dir"] / "fs"
    backend_adapter = fs / ADDON_ROOT.strip("/") / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py"
    workbench_index = fs / ADDON_ROOT.strip("/") / "www" / "scene_studio" / "index.html"
    assert backend_adapter.is_file(), "backend was not activated on the fake host"
    assert workbench_index.is_file(), "workbench was not activated on the fake host"

    # fresh install takes NO pre-deploy backup and retains NO previous tree
    backups = fs / ADDON_ROOT.strip("/") / "backups"
    assert not backups.exists() or not any(backups.rglob("*.tar.gz")), \
        "a fresh install must not fabricate a backup of a nonexistent previous package"

    # AppDaemon was restarted and the first health check saw registry_admin
    assert fresh_env["api"].restart_calls >= 1
    assert "registry_admin" in result.stdout
    # the prebuilt Workbench path never invoked npm
    for call in _read_calls(fresh_env["state_dir"]):
        assert "npm" not in " ".join(call["argv"])

    # the deployed Workbench index is byte-identical to the release's
    assert workbench_index.read_bytes() == (release_dir / "workbench" / "dist" / "index.html").read_bytes()


def test_fresh_install_requires_registry_admin_profile(release_dir, fresh_env, tmp_path):
    profile = tmp_path / "normal.profile.json"
    _write_profile(profile, ha_url=fresh_env["ha_url"], runtime_mode="normal")
    result = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(profile), "-Apply"], fresh_env["env"])
    assert result.returncode != 0
    assert "registry_admin" in (result.stdout + result.stderr)
    for call in _read_calls(fresh_env["state_dir"]):
        joined = " ".join(call["argv"])
        assert "install -d" not in joined and "mv " not in joined


def test_provider_preflight_blocks_apply_on_unreachable_host(release_dir, fresh_env, tmp_path):
    profile = tmp_path / "wled.profile.json"
    # port 1 on 127.0.0.1 is closed in the test environment
    _write_profile(profile, ha_url=fresh_env["ha_url"], wled_host="127.0.0.1:1")
    result = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(profile), "-Apply"], fresh_env["env"])
    assert result.returncode != 0
    assert "not reachable" in (result.stdout + result.stderr)

    # the same profile passes preflight once the host IS reachable
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        reachable_profile = tmp_path / "wled-ok.profile.json"
        _write_profile(reachable_profile, ha_url=fresh_env["ha_url"], wled_host=f"127.0.0.1:{port}")
        ok = _run_ps(release_dir / "installer" / "install_scene_studio.ps1", ["-Profile", str(reachable_profile), "-Apply"], fresh_env["env"])
        assert ok.returncode == 0, f"stdout:\n{ok.stdout[-3000:]}\nstderr:\n{ok.stderr[-3000:]}"
    finally:
        listener.close()


def test_upgrade_preserves_backup_and_mode_semantics(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    state_dir.mkdir()
    _seed_upgrade_state(state_dir)
    shim_dir = _write_ssh_shim(tmp_path / "shim")
    profile = tmp_path / "upgrade.profile.json"
    with FakeHaApi() as api:
        _write_profile(profile, ha_url=f"http://127.0.0.1:{api.port}", runtime_mode="normal")
        result = _run_ps(
            release_dir / "installer" / "install_scene_studio.ps1",
            ["-Profile", str(profile), "-Apply"],
            _make_env(state_dir, shim_dir),
        )
    assert result.returncode == 0, f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"

    fs = state_dir / "fs"
    addon = fs / ADDON_ROOT.strip("/")
    backup_dirs = [path for path in (addon / "backups").glob("*") if path.is_dir()] if (addon / "backups").exists() else []
    assert backup_dirs, "upgrade did not create a backup directory"
    archives = list((addon / "backups").rglob("*.tar.gz"))
    assert archives, "upgrade did not create a backup archive"
    previous_dirs = list((addon / "backups").rglob("live-before"))
    assert previous_dirs, "upgrade did not retain the previous live tree(s)"
    assert "previous backend install" not in (addon / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py").read_text(encoding="utf-8")
    assert (state_dir / "apps_mode").read_text(encoding="utf-8").strip() == "normal"


def test_deployer_verify_only_reports_absent_target_clearly(release_dir, fresh_env):
    backend_deployer = release_dir / "installer" / "deploy_scene_studio_backend.ps1"
    result = _run_ps(
        backend_deployer,
        ["-VerifyOnly", "-HaHost", "fake-ha", "-AddonConfigRoot", ADDON_ROOT, "-HaUrl", fresh_env["ha_url"]],
        fresh_env["env"],
    )
    assert result.returncode != 0
    assert "not installed" in (result.stdout + result.stderr)
