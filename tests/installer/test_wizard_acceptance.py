"""Guided-installer (wizard) acceptance tests — the actual friend journey.

Operates on the GENERATED RELEASE TREE's root entry point
(``Install-SceneStudio.ps1``) through the fake ssh host + fake HA REST API,
covering the wizard boundary rather than only the internal installer:

* fresh install with INDEPENDENT HA API, SSH target, and AppDaemon HTTP
  endpoint values (three-endpoint model; the Workbench URL comes from the
  AppDaemon HTTP endpoint, never the HA API host);
* AppDaemon HTTP inference (ssh host + default port) and the unattended
  actionable failure when inference is unreachable and no explicit URL is
  given;
* AppDaemon root auto-detection (single candidate, multiple selected by
  number, none -> actionable failure);
* invalid/unreachable SSH target and HA API failing before any mutation;
* fresh install AND existing-install upgrade (bounded apps.yaml replace,
  unrelated content preserved, runtime mode preserved);
* provider choices driving the generated profile + secrets.yaml handling;
* zero remote mutation before explicit install confirmation;
* automatic configuration rollback when the deployment fails, plus the
  SANITIZED SUPPORT REPORT: the archive exists, contains the four expected
  members, and the fake token/Hue key values appear NOWHERE inside it;
* the optional HA dashboard card step (default on: deploys the prebuilt card
  to the Scene Studio-owned /config/www location and prints the resource
  registration guidance + minimal config; opt-out installs nothing).

The wizard's unattended mode (-Unattended -Answers) is the testable
expression of the interactive prompt flow; secrets always come from the
environment, never from the answers file.
"""

import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

from test_installer_acceptance import (  # noqa: F401
    ADDON_ROOT,
    HAS_PWSH,
    FakeHaApi,
    _make_env,
    _read_calls,
    _run_ps,
    _write_ssh_shim,
    release_dir,
)

pytestmark = pytest.mark.skipif(not HAS_PWSH, reason="wizard acceptance needs pwsh on Windows")

FAKE_HA_TOKEN = "wizard-token-do-not-persist-0123456789"
FAKE_HUE_KEY = "wizard-hue-app-key-ABCDEF0123456789"

WRITE_VERBS = ("mv ", "install -d", "rm -rf", "tee ", "chown", "tar ", "cp -a", "chmod ")

CARD_WWW = "/config/www/scene-studio-card/scene-studio-card.js"

WIZARD_ANSWERS_DEFAULT = {
    "ha_url": None,  # filled per-test (fake HA API port)
    "ssh_host": "fake-ha",
    "ssh_user": "friend",
    "ssh_port": 22,
    "appdaemon_config_root": ADDON_ROOT,
    "store_root": "/config/scene_studio_store",
    "install_ha_card": True,
    "ha_config_filesystem_confirmed": True,
    "providers": {
        "ha_light": True,
        "hue": {"enabled": False, "host": None, "bridge_id": None},
        "wled": {"enabled": False, "host": None},
        "hyperhdr": {"enabled": False, "host": None},
    },
    "confirm_install": True,
}


def _write_answers(path: Path, **overrides) -> Path:
    answers = json.loads(json.dumps(WIZARD_ANSWERS_DEFAULT))
    for key, value in overrides.items():
        if value is ...:  # sentinel: remove the key entirely
            answers.pop(key, None)
        else:
            answers[key] = value
    path.write_text(json.dumps(answers, indent=2), encoding="utf-8")
    return path


def _run_wizard(release_dir: Path, answers: Path, env: dict, *, yes: bool = True, timeout: int = 1200):
    arguments = ["-Unattended", "-Answers", str(answers)]
    if yes:
        arguments.append("-Yes")
    return _run_ps(release_dir / "Install-SceneStudio.ps1", arguments, env, timeout=timeout)


def _wizard_env(tmp_path: Path, state_dir: Path, *, hue_key: str | None = None, token: str | None = None) -> dict:
    # _write_ssh_shim must run BEFORE _make_env: the shim dir is prepended to
    # PATH, and without it the real ssh.exe resolves (and cannot reach the
    # fake host).
    _write_ssh_shim(tmp_path / "shim")
    env = _make_env(state_dir, tmp_path / "shim")
    env["SCENE_STUDIO_HA_TOKEN"] = token or FAKE_HA_TOKEN
    if hue_key is not None:
        env["SCENE_STUDIO_HUE_APP_KEY"] = hue_key
    return env


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _seed_fresh_target(state_dir: Path) -> None:
    addon = state_dir / "fs" / ADDON_ROOT.strip("/")
    addon.mkdir(parents=True, exist_ok=True)
    config = state_dir / "fs/config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "configuration.yaml").write_text("default_config:\n", encoding="utf-8")
    (config / ".HA_VERSION").write_text("2026.9.10", encoding="utf-8")
    # AppDaemon's own config file is the auto-detection marker
    (addon / "appdaemon.yaml").write_text("appdaemon:\n  latitude: 0\n", encoding="utf-8")
    (state_dir / "apps_mode").write_text("registry_admin", encoding="utf-8")


def _assert_no_remote_writes(state_dir: Path) -> None:
    for call in _read_calls(state_dir):
        joined = " ".join(call["argv"])
        for verb in WRITE_VERBS:
            assert verb not in joined, f"unexpected remote write: {joined[:200]}"


# ---------------------------------------------------------------------------
# the full friend journey
# ---------------------------------------------------------------------------


def test_wizard_fresh_install_three_independent_endpoints(release_dir, tmp_path):
    """unzip -> run root wizard -> answer -> review -> install -> registry_admin.

    All THREE endpoints are deliberately different values: the HA API is a
    localhost port, ssh is a named host on a non-default port, and the
    AppDaemon HTTP endpoint is an explicit third URL. The final Workbench URL
    MUST come from the AppDaemon HTTP endpoint, never the HA API host.
    """
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            ssh_port=2222,
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
    assert "Installation complete" in output
    assert "registry_admin" in output
    assert "using the provided Scene Studio HTTP endpoint: http://appdaemon.example.test" in output
    assert "healthy" in output or "reachable" in output
    # the Workbench URL is built from the AppDaemon HTTP endpoint
    assert "Open the Workbench:  http://appdaemon.example.test" in output
    assert f"http://127.0.0.1:{api.port}/local/" not in output
    # docs pointers print even in unattended mode (banner)
    assert "home-assistant.io/docs/authentication" in output
    assert "github.com/hassio-addons/app-ssh" in output
    assert "github.com/hassio-addons/addon-appdaemon" in output

    fs = state_dir / "fs"
    backend_adapter = fs / ADDON_ROOT.strip("/") / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py"
    workbench_index = fs / ADDON_ROOT.strip("/") / "www" / "scene_studio" / "index.html"
    assert backend_adapter.is_file()
    assert workbench_index.is_file()
    apps_yaml = (fs / ADDON_ROOT.strip("/") / "apps.yaml").read_text(encoding="utf-8")
    assert "scene_studio:" in apps_yaml
    assert "# >>> scene_studio managed block" in apps_yaml
    assert "registry_admin: true" in apps_yaml
    assert "store_root: /config/scene_studio_store" in apps_yaml


def test_wizard_card_installed_by_default(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-4000:]
    fs = state_dir / "fs"
    deployed = fs / "config" / "www" / "scene-studio-card" / "scene-studio-card.js"
    assert deployed.is_file(), "the optional card was not deployed to /config/www"
    assert deployed.read_bytes() == (
        release_dir / "home-assistant" / "scene-studio-card" / "dist" / "scene-studio-card.js"
    ).read_bytes()
    # guidance + minimal config printed; no dashboard was touched
    assert "custom:scene-studio-card" in output
    assert "/local/scene-studio-card/scene-studio-card.js" in output


def test_wizard_card_opt_out_installs_nothing(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            install_ha_card=False,
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-4000:]
    assert not (state_dir / "fs" / "config" / "www" / "scene-studio-card").exists()
    assert "HA dashboard card step skipped" in output


def test_wizard_inferred_appdaemon_http_unreachable_fails_actionable(release_dir, tmp_path):
    """Unattended + no explicit AppDaemon HTTP + unreachable inference =>
    actionable failure telling the user about appdaemon_http_url, BEFORE any
    remote mutation."""
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=...,  # removed: force inference from 'fake-ha' (unresolvable)
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "appdaemon_http_url" in output
    assert "Failed stage: appdaemon-endpoint" in output
    assert "Support report:" in output
    _assert_no_remote_writes(state_dir)


def test_wizard_auto_detects_single_addon_root(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            appdaemon_config_root=...,
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-3000:]
    assert "found exactly one candidate" in output
    assert f"using {ADDON_ROOT}" in output


def test_wizard_multiple_addon_root_selection(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    addon_configs = state_dir / "fs" / "addon_configs"
    (addon_configs / "other_appdaemon" / "apps").mkdir(parents=True, exist_ok=True)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            appdaemon_config_root=None,
            addon_root_choice=2,
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-3000:]
    assert "Several AppDaemon installations were found" in output
    assert "using /addon_configs/other_appdaemon" in output
    assert (state_dir / "fs" / "addon_configs" / "other_appdaemon" / "apps" / "scene_studio").is_dir()


def test_wizard_invalid_addon_root_fails_readonly(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            appdaemon_config_root="/addon_configs/does_not_exist",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "does not exist on the target" in output
    assert "fresh installation detected" not in output
    _assert_no_remote_writes(state_dir)


def test_wizard_unreachable_ssh_fails_before_mutation(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    (state_dir / "ssh_unreachable").write_text("")
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "ssh" in output.lower()
    _assert_no_remote_writes(state_dir)


def test_wizard_unreachable_ha_api_fails_before_remote_contact(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    env = _wizard_env(tmp_path, state_dir)
    answers = _write_answers(tmp_path / "answers.json", ha_url="http://127.0.0.1:1")
    result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "not reachable" in output
    # failed at the HA API step: the wizard never probed the remote TARGET
    # (the local `ssh -V` client-version probe is not remote contact)
    assert [c for c in _read_calls(state_dir) if c["argv"] != ["-V"]] == []


def test_wizard_zero_mutation_before_confirmation(release_dir, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            confirm_install=False,
        )
        # Independent workstation sessions must not share timestamp-named temp dirs.
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: _run_wizard(release_dir, answers, env, yes=False), range(2)))
    for result in results:
        output = _strip_ansi(result.stdout + result.stderr)
        assert result.returncode == 0, output[-3000:]
        assert "REVIEW ONLY" in output
        assert "Nothing was changed" in output
    _assert_no_remote_writes(state_dir)
    assert not (state_dir / "fs" / ADDON_ROOT.strip("/") / "apps" / "scene_studio").exists()


def test_wizard_upgrade_preserves_unrelated_apps_yaml(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    addon_fs = state_dir / "fs" / ADDON_ROOT.strip("/")
    backend = addon_fs / "apps" / "scene_studio" / "appdaemon_adapter"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "adapter.py").write_text("# previous backend\n", encoding="utf-8")
    www = addon_fs / "www" / "scene_studio"
    www.mkdir(parents=True, exist_ok=True)
    (www / "index.html").write_text("<!doctype html><html><head><title>Scene Studio Workbench</title></head></html>", encoding="utf-8")
    original_apps = (
        "legacy_app:\n"
        "  module: legacy.some_module\n"
        "  class: LegacyApp\n"
        "scene_studio:\n"
        "  module: scene_studio.appdaemon_adapter.adapter\n"
        "  class: SceneStudioApp\n"
        "  registry_admin: true\n"
        "  read_only: false\n"
        "  store_root: /config/scene_studio_store\n"
    )
    (addon_fs / "apps.yaml").write_text(original_apps, encoding="utf-8")
    (state_dir / "apps_mode").write_text("normal", encoding="utf-8")

    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir)
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-4000:]
    assert "upgrade of the existing install" in output
    assert "runtime mode is 'normal' (preserved)" in output

    apps_after = (addon_fs / "apps.yaml").read_text(encoding="utf-8")
    assert "legacy_app:\n  module: legacy.some_module\n  class: LegacyApp\n" in apps_after
    assert "# >>> scene_studio managed block" in apps_after
    assert "registry_admin: false" in apps_after
    assert "read_only: false" in apps_after
    assert apps_after.count("scene_studio:") == 1
    backups = addon_fs / "backups"
    config_backups = [p for p in backups.glob("scene-studio-config-*")] if backups.exists() else []
    assert config_backups, "wizard did not back up apps.yaml"
    saved = list(config_backups[0].glob("apps.yaml.before"))
    assert saved and saved[0].read_text(encoding="utf-8") == original_apps


def test_wizard_provider_profile_and_secret_handling(release_dir, tmp_path):
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir, hue_key=FAKE_HUE_KEY)
        env['SCENE_STUDIO_GITHUB_TOKEN'] = 'synthetic-private-access-marker'
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
            providers={
                "ha_light": True,
                "hue": {"enabled": True, "host": f"127.0.0.1:{api.port}", "bridge_id": None},
                "wled": {"enabled": False, "host": None},
                "hyperhdr": {"enabled": False, "host": None},
            },
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode == 0, output[-4000:]
    fs = state_dir / "fs" / ADDON_ROOT.strip("/")
    apps_yaml = (fs / "apps.yaml").read_text(encoding="utf-8")
    assert "hue_username: !secret scene_studio_hue_app_key" in apps_yaml
    secrets_yaml = (fs / "secrets.yaml").read_text(encoding="utf-8")
    assert 'scene_studio_hue_app_key: "' + FAKE_HUE_KEY + '"' in secrets_yaml
    assert FAKE_HUE_KEY not in output
    assert FAKE_HUE_KEY not in apps_yaml
    # Hue help pointer printed when the provider is enabled
    assert "developers.meethue.com" in output
    for path in fs.rglob("*"):
        if path.is_file():
            assert FAKE_HA_TOKEN not in path.read_text(encoding="utf-8", errors="replace"), path
    assert FAKE_HA_TOKEN not in output


def test_wizard_config_rollback_and_sanitized_support_report(release_dir, tmp_path):
    """Failed install => config restored byte-for-byte AND a support report
    whose archive never contains the token or Hue key values."""
    state_dir = tmp_path / "fake-ha-state"
    _seed_fresh_target(state_dir)
    addon_fs = state_dir / "fs" / ADDON_ROOT.strip("/")
    original_apps = "legacy_app:\n  module: legacy.some_module\n  class: LegacyApp\n"
    (addon_fs / "apps.yaml").write_text(original_apps, encoding="utf-8")
    # the backend deployer's staging writes fail AFTER the wizard's config
    # write -> install fails -> auto-rollback + support report
    (state_dir / "fail_backend_staging").write_text("")
    with FakeHaApi() as api:
        env = _wizard_env(tmp_path, state_dir, hue_key=FAKE_HUE_KEY)
        env['SCENE_STUDIO_GITHUB_TOKEN'] = 'synthetic-private-access-marker'
        answers = _write_answers(
            tmp_path / "answers.json",
            ha_url=f"http://127.0.0.1:{api.port}",
            appdaemon_http_url=f"http://appdaemon.example.test:{api.port}",
        )
        result = _run_wizard(release_dir, answers, env)
    output = _strip_ansi(result.stdout + result.stderr)
    assert result.returncode != 0
    assert "previous configuration restored successfully" in output
    assert "Failed stage:" in output
    assert (addon_fs / "apps.yaml").read_text(encoding="utf-8") == original_apps
    assert "# >>> scene_studio managed block" not in (addon_fs / "apps.yaml").read_text(encoding="utf-8")
    backups = addon_fs / "backups"
    config_backups = [p for p in backups.glob("scene-studio-config-*")] if backups.exists() else []
    assert config_backups

    # --- the support report is sanitized --------------------------------
    match = re.search(r"Support report:\s*\n\s*(\S+\.zip)", output)
    assert match, "wizard did not print the support report path"
    zip_path = Path(match.group(1))
    assert zip_path.is_file(), f"support report missing: {zip_path}"
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.namelist()
        for expected in ("support-report.json", "installer.log", "environment.txt", "probes.txt"):
            assert any(name.endswith(expected) for name in members), f"missing member: {expected}"
        for name in members:
            payload = archive.read(name).decode("utf-8", errors="replace")
            assert FAKE_HA_TOKEN not in payload, f"token leaked into {name}"
            assert FAKE_HUE_KEY not in payload, f"hue key leaked into {name}"
            assert 'synthetic-private-access-marker' not in payload
    # stage + remote-change status are recorded
    with zipfile.ZipFile(zip_path) as archive:
        report_name = next(name for name in members if name.endswith("support-report.json"))
        report = json.loads(archive.read(report_name).decode("utf-8"))
        assert report["remote_changes"] == "previous configuration restored successfully"
        assert report["error"]
        assert report["topology"]["filesystem_transport"] == "ssh"
        assert report["topology"]["appdaemon_restart_strategy"] == "supervisor"
        assert report["endpoints"]["ha_api"].startswith("http://127.0.0.1:")
        assert report["endpoints"]["appdaemon_http"].startswith("http://appdaemon.example.test")
