"""Scene Studio Home Assistant add-on delivery gates (offline).

This repository doubles as its own Supervisor add-on repository: the manifest
at the root (config.yaml), the on-device image build (Dockerfile + build.yaml,
built by Supervisor from the repository root as the build context), and the
runtime config renderer (render_config.py). This install path needs no
PowerShell, SSH, or workstation; updates are owned by Supervisor, so the
independent update companion is intentionally absent on this track.
"""
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def renderer():
    spec = importlib.util.spec_from_file_location("scene_studio_render_config", ROOT / "render_config.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifest():
    return (ROOT / "config.yaml").read_text(encoding="utf-8")


def _section_keys(manifest, section):
    keys = set()
    inside = False
    for line in manifest.splitlines():
        if line.strip() == f"{section}:" and not line.startswith((" ", "\t")):
            inside = True
            continue
        if inside:
            if line.startswith("  ") and not line.startswith("   "):
                keys.add(line.strip().split(":")[0])
            elif line.strip() and not line.startswith(" "):
                inside = False
    return keys


def test_addon_version_matches_root_version():
    match = re.search(r'(?m)^version:\s*"([^"\s]+)"\s*$', _manifest())
    assert match, "config.yaml must declare a quoted version"
    assert match.group(1) == (ROOT / "VERSION").read_text().strip()


def test_validate_versions_gates_addon_manifest(tmp_path):
    spec = importlib.util.spec_from_file_location("version_script_addon_gate", ROOT / "scripts/version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate_versions() == (ROOT / "VERSION").read_text().strip()
    (tmp_path / "VERSION").write_text("0.1.0")
    (tmp_path / "backend").mkdir()
    (tmp_path / "workbench").mkdir()
    (tmp_path / "backend/pyproject.toml").write_text('[project]\nversion="0.1.0"\n')
    for name in ("package.json", "package-lock.json"):
        (tmp_path / "workbench" / name).write_text(json.dumps({"version": "0.1.0", "packages": {"": {"version": "0.1.0"}}}))
    (tmp_path / "config.yaml").write_text('name: Scene Studio\nversion: "0.2.0"\n')
    with pytest.raises(ValueError, match="config.yaml version"):
        module.validate_versions(tmp_path)
    (tmp_path / "config.yaml").write_text('name: Scene Studio\nversion: "0.1.0"\n')
    assert module.validate_versions(tmp_path) == "0.1.0"


def test_addon_manifest_shape():
    manifest = _manifest()
    assert re.search(r"(?m)^slug: scene_studio$", manifest)
    assert re.search(r"(?m)^homeassistant_api: true$", manifest)
    assert re.search(r"(?m)^init: false$", manifest)
    arch = re.search(r"(?m)^arch:\n((?:  - .+\n)+)", manifest)
    assert arch and "amd64" in arch.group(1) and "aarch64" in arch.group(1)
    options = _section_keys(manifest, "options")
    schema = _section_keys(manifest, "schema")
    assert options and schema
    assert options <= schema, "every add-on option must have a schema entry"
    assert {"mode", "hue_host", "hue_key", "wled_host", "hyperhdr_host"} <= options


def test_repository_manifest_present():
    # Supervisor identifies a custom add-on repository by repository.yaml at
    # the repository root.
    repo = (ROOT / "repository.yaml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^name: Scene Studio$", repo)
    assert "https://github.com/pHarmG/Scene-Studio" in repo


def test_dockerfile_copies_payload_and_sets_addon_identity():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY backend/src/scene_studio /opt/scene-studio/apps/scene_studio" in dockerfile
    assert "/opt/scene-studio/www/scene_studio" in dockerfile
    assert "SCENE_STUDIO_ADDON=1" in dockerfile
    assert "appdaemon~=" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "COPY run.sh render_config.py" in dockerfile
    assert "'channel': 'addon'" in dockerfile
    # build.yaml is obsolete (Home Assistant BuildKit migration): the per-arch
    # HA base images are staged directly in the Dockerfile and TARGETARCH
    # selects between them.
    assert not (ROOT / "build.yaml").exists()
    assert "FROM ghcr.io/home-assistant/amd64-base-python:" in dockerfile
    assert "FROM ghcr.io/home-assistant/aarch64-base-python:" in dockerfile
    assert "FROM base-${TARGETARCH}" in dockerfile


def test_run_sh_renders_configuration_before_start():
    run_sh = (ROOT / "run.sh").read_text(encoding="utf-8")
    assert "render_config.py" in run_sh
    assert "exec appdaemon -c /data" in run_sh


def test_fresh_setup_renders_registry_admin_managed_block(renderer):
    apps = renderer.render_apps_yaml(renderer.normalize_options({"mode": "setup"}))
    assert renderer.MANAGED_BEGIN in apps and renderer.MANAGED_END in apps
    assert "registry_admin: true" in apps and "read_only: false" in apps
    assert 'store_root: "/data/scene_studio_store"' in apps
    assert "module: scene_studio.appdaemon_adapter.adapter" in apps
    for absent in ("hue_ip", "hue_username", "hue_bridge_id", "wled_host", "hyperhdr_host", "ignored_ha_entity_ids"):
        assert absent not in apps


@pytest.mark.parametrize("mode,admin,read_only", [
    ("setup", "true", "false"),
    ("normal", "false", "false"),
    ("read_only", "false", "true"),
])
def test_mode_maps_to_runtime_policy(renderer, mode, admin, read_only):
    apps = renderer.render_apps_yaml(renderer.normalize_options({"mode": mode}))
    assert f"registry_admin: {admin}" in apps
    assert f"read_only: {read_only}" in apps


def test_hue_key_stored_by_name_only(renderer):
    options = renderer.normalize_options({
        "hue_host": "hue-bridge.local", "hue_key": "sk-test-hue-key-123", "hue_bridge_id": "bridge-0017",
    })
    apps = renderer.render_apps_yaml(options)
    assert 'hue_ip: "hue-bridge.local"' in apps
    assert "hue_username: !secret scene_studio_hue_key" in apps
    assert 'hue_bridge_id: "bridge-0017"' in apps
    assert "sk-test-hue-key-123" not in apps
    assert renderer.render_secrets_yaml(options) == 'scene_studio_hue_key: "sk-test-hue-key-123"\n'


def test_without_hue_no_secrets_file(renderer):
    options = renderer.normalize_options({})
    assert renderer.render_secrets_yaml(options) is None
    apps = renderer.render_apps_yaml(options)
    assert "hue" not in apps


def test_entity_ignore_list_rendered_quoted(renderer):
    options = renderer.normalize_options({"ignored_ha_entity_ids": ["light.kitchen", "light.tv_backlight"]})
    apps = renderer.render_apps_yaml(options)
    assert 'ignored_ha_entity_ids: ["light.kitchen", "light.tv_backlight"]' in apps


@pytest.mark.parametrize("bad", [
    {"mode": "production"},
    {"hue_host": "hue.local"},
    {"hue_key": "only-a-key"},
    {"hue_bridge_id": "bridge-0017"},
    {"hue_host": "http://hue.local"},
    {"wled_host": "two hosts"},
    {"hyperhdr_host": "tv.local:99999"},
    {"ignored_ha_entity_ids": "light.kitchen"},
    {"ignored_ha_entity_ids": ["Light.Kitchen"]},
    {"ignored_ha_entity_ids": ["lightkitchen"]},
])
def test_invalid_options_rejected(renderer, bad):
    with pytest.raises(ValueError):
        renderer.normalize_options(bad)


def test_appdaemon_token_via_env_var_never_on_disk(renderer):
    apps = renderer.render_apps_yaml(renderer.normalize_options({}))
    daemon = renderer.render_appdaemon_yaml()
    assert "http://supervisor/core" in daemon
    # The token is resolved at config-load time from the process environment;
    # no token value may ever be rendered into a file.
    assert "token: !env_var SUPERVISOR_TOKEN" in daemon
    assert 'app_dir: "/opt/scene-studio/apps"' in daemon
    assert "supervisor-token-value" not in daemon
    assert "supervisor-token-value" not in apps
    secrets = renderer.render_secrets_yaml(renderer.normalize_options(
        {"hue_host": "hue.local", "hue_key": "other-secret"}))
    assert "supervisor-token-value" not in secrets


def test_timezone_detection_from_localtime_link(renderer, tmp_path):
    zone = tmp_path / "zoneinfo" / "Europe" / "Berlin"
    zone.parent.mkdir(parents=True)
    zone.write_text("")
    assert renderer.detect_time_zone(str(zone.parent)) is None
    link = tmp_path / "localtime"
    if os.name == "nt":
        pytest.skip("symlink creation requires privileges on Windows")
    link.symlink_to(zone, target_is_directory=True)
    assert renderer.detect_time_zone(str(link)) == "Europe/Berlin"
    assert renderer.detect_time_zone(str(tmp_path / "missing")) is None


def test_summary_masks_all_secret_values(renderer):
    options = renderer.normalize_options({"hue_host": "hue.local", "hue_key": "hue-key-secret-value"})
    summary = renderer.summarize(options, "supervisor-token-value")
    assert "hue-key-secret-value" not in summary
    assert "supervisor-token-value" not in summary
    assert "hue.local" in summary and "present" in summary


def test_main_writes_runtime_files(tmp_path, monkeypatch, renderer, capsys):
    data = tmp_path / "data"
    data.mkdir()
    payload = tmp_path / "payload"
    (payload / "apps").mkdir(parents=True)
    (payload / "www").mkdir(parents=True)
    (payload / "www" / "index.html").write_text("<html>workbench</html>")
    # A stale pre-4.5 render location must be cleaned up, not left to mislead.
    (data / "apps.yaml").write_text("stale")
    (data / "options.json").write_text(json.dumps({"mode": "setup", "wled_host": "wled-1.local"}))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok-for-main-test")
    assert renderer.main(["--data-dir", str(data), "--payload-root", str(payload)]) == 0
    apps = (payload / "apps" / "apps.yaml").read_text(encoding="utf-8")
    assert "registry_admin: true" in apps and 'wled_host: "wled-1.local"' in apps
    daemon = (data / "appdaemon.yaml").read_text(encoding="utf-8")
    assert "token: !env_var SUPERVISOR_TOKEN" in daemon
    assert "tok-for-main-test" not in daemon  # the token value is never persisted
    assert not (payload / "apps" / "secrets.yaml").exists()
    assert not (data / "apps.yaml").exists()
    assert (data / "scene_studio_store").is_dir()
    assert (data / "www" / "index.html").is_file()
    printed = capsys.readouterr().out
    assert "tok-for-main-test" not in printed
    if os.name == "posix":
        assert stat.S_IMODE((data / "appdaemon.yaml").stat().st_mode) == 0o600
        assert stat.S_IMODE((payload / "apps" / "apps.yaml").stat().st_mode) == 0o600
    assert renderer.main(["--data-dir", str(tmp_path / "absent")]) == 1
