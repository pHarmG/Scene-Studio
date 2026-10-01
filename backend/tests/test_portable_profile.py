"""Portable deployment profile: validation, resolution, and apps.yaml rendering.

The profile module lives in ``installer/`` (it ships in the
distribution bundle); these tests load it by path and pin the contract that
portability work depends on: unknown keys fail, secret fields hold secret
NAMES only, validation fails before any remote write, and the generated
apps.yaml block starts every fresh install in ``registry_admin``.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(SRC_ROOT / "src"))

PROFILE_PY = REPO_ROOT / "installer" / "scene_studio_profile.py"
EXAMPLE = REPO_ROOT / "installer" / "scene-studio.profile.example.json"


def _module():
    spec = importlib.util.spec_from_file_location("scene_studio_profile", PROFILE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example_profile():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _valid_profile(**overrides):
    profile = _example_profile()
    profile["providers"]["hue"] = {
        "enabled": True,
        "host": "bridge.local",
        "application_key_secret": "hue_app_key",
        "bridge_id": "001788demo01",
    }
    profile["appdaemon_config_root"] = "/addon_configs/a0d7b954_appdaemon"
    profile.update(overrides)
    return profile


def test_example_profile_validates():
    module = _module()
    assert module.validate_profile(_example_profile()) == _example_profile()


def test_unknown_top_level_key_fails_loudly():
    module = _module()
    profile = _valid_profile(totally_unknown={"x": 1})
    try:
        module.validate_profile(profile)
    except module.ProfileError as exc:
        assert any("totally_unknown" in message for message in exc.errors)
    else:
        raise AssertionError("unknown key was silently accepted")


def test_unknown_provider_and_integration_fail():
    module = _module()
    profile = _valid_profile()
    profile["providers"]["knx"] = {"enabled": True}
    profile["integrations"]["printer"] = {"enabled": True}
    try:
        module.validate_profile(profile)
    except module.ProfileError as exc:
        joined = "; ".join(exc.errors)
        assert "knx" in joined and "printer" in joined
    else:
        raise AssertionError("unknown provider/integration was silently accepted")


def test_secret_field_must_hold_a_secret_name_not_a_value():
    module = _module()
    profile = _valid_profile()
    profile["providers"]["hue"]["application_key_secret"] = "AAAAAAAAREALKEYVALUE00"
    try:
        module.validate_profile(profile)
    except module.ProfileError as exc:
        assert any("secrets.yaml NAME" in message for message in exc.errors)
    else:
        raise AssertionError("a credential-shaped value was accepted as a secret name")


def test_wled_multi_host_is_rejected_not_truncated():
    module = _module()
    profile = _valid_profile()
    profile["providers"]["wled"] = {"enabled": True, "hosts": ["wled-a.local", "wled-b.local"]}
    try:
        module.validate_profile(profile)
    except module.ProfileError as exc:
        assert any("ONE global" in message for message in exc.errors)
    else:
        raise AssertionError("multiple wled hosts were silently truncated")


def test_runtime_mode_restricted_and_at_least_one_provider():
    module = _module()
    profile = _valid_profile()
    profile["runtime_mode"] = "r5_validation"
    profile["providers"] = {
        "hue": {"enabled": False, "host": None, "application_key_secret": None, "bridge_id": None},
        "wled": {"enabled": False, "hosts": []},
        "ha_light": {"enabled": False, "ignored_entity_ids": []},
    }
    try:
        module.validate_profile(profile)
    except module.ProfileError as exc:
        joined = "; ".join(exc.errors)
        assert "runtime_mode" in joined and "at least one provider" in joined
    else:
        raise AssertionError("invalid mode / providerless profile accepted")


def test_render_apps_yaml_first_install_shape():
    module = _module()
    block, notes = module.render_apps_yaml(_valid_profile())
    assert "scene_studio:" in block
    assert "module: scene_studio.appdaemon_adapter.adapter" in block
    assert "registry_admin: true" in block
    assert "read_only: false" in block
    assert "legacy_events_enabled: false" in block
    assert "store_root: /config/scene_studio_store" in block
    # provider keys only when configured
    assert "hue_ip: bridge.local" in block
    assert "hue_username: !secret hue_app_key" in block
    assert "hue_bridge_id: 001788demo01" in block
    assert "wled_host" not in block
    assert "hyperhdr_host" not in block
    # leaving registry_admin is spelled out for the operator
    assert any("registry_admin" in note and "restart AppDaemon" in note for note in notes)


def test_render_apps_yaml_optional_integrations_appear_only_when_enabled():
    module = _module()
    profile = _valid_profile()
    profile["providers"]["wled"] = {"enabled": True, "hosts": ["wled.local"]}
    profile["integrations"]["hyperhdr"] = {
        "enabled": True, "host": "tv.local:8090", "default_policy": "yield", "probe_ttl_seconds": 5,
    }
    block, _notes = module.render_apps_yaml(profile)
    assert "wled_host: wled.local" in block
    assert "hyperhdr_host: tv.local:8090" in block
    assert "contention_default_policy: yield" in block
    # the adapter's real option name is hyperhdr_probe_ttl_seconds
    assert "hyperhdr_probe_ttl_seconds: 5" in block
    assert "hyperhdr_wled_instance_ids" not in block


def test_resolve_reports_normalized_deployment_values():
    module = _module()
    resolved = module.normalized(module.validate_profile(_valid_profile()))
    assert resolved["ha_url"] == "http://homeassistant.local:8123"
    assert resolved["runtime_mode"] == "registry_admin"
    assert resolved["providers"]["hue"]["enabled"] is True
    assert resolved["providers"]["ha_light"]["enabled"] is True
    assert resolved["integrations"]["hyperhdr"]["enabled"] is False


def test_resolved_shape_is_nested_canonical_consumed_by_the_installer():
    """The installer reads $Settings.providers.<name>.enabled — pin the shape.

    This is the contract test for the resolve -> PowerShell consumer seam:
    a flattened top-level provider block here silently breaks the portable
    installer's preflight (review finding, 849c643 corrective pass).
    """
    module = _module()
    resolved = module.normalized(module.validate_profile(_valid_profile()))
    assert set(resolved) == {
        "ha_url", "ha_ssh_host", "appdaemon_config_root", "store_root", "runtime_mode",
        "providers", "integrations",
    }
    assert set(resolved["providers"]) == {"hue", "wled", "ha_light"}
    assert set(resolved["integrations"]) == {"hyperhdr"}
    for name in ("hue", "wled", "ha_light"):
        assert isinstance(resolved["providers"][name].get("enabled"), bool)


def test_render_apps_yaml_opts_out_of_ha_light_discovery_explicitly():
    module = _module()
    profile = _valid_profile()
    profile["providers"]["ha_light"] = {"enabled": False, "ignored_entity_ids": ["light.legacy_group"]}
    block, _notes = module.render_apps_yaml(profile)
    assert "ha_light_enabled: false" in block
    assert "ignored_ha_entity_ids" in block
    # enabled (default) must NOT emit the key — the runtime default is already on
    block_on, _ = module.render_apps_yaml(_valid_profile())
    assert "ha_light_enabled" not in block_on
