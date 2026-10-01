#!/usr/bin/env python3
"""Scene Studio portable deployment profile — validate / resolve / render apps.yaml.

The profile is a single JSON file that ties together every knob a fresh
Scene Studio install needs, so a new deployment never edits Scene Studio
source and never inherits this project's network topology. Python is used
for the wrapper because the backend itself is Python; the profile format is
plain JSON so PowerShell tooling can consume it too (no YAML dependency).

Rules (enforced by ``validate``):

- unknown keys fail loudly instead of being ignored;
- no credential value ever belongs in the profile — secret fields hold
  AppDaemon ``secrets.yaml`` NAMES only (shape-checked);
- validation happens BEFORE any remote write;
- unknown/unsupported values produce actionable messages.

Usage (Python 3.9+, stdlib only)::

    python scene-studio-profile.py validate        --profile my.profile.json
    python scene-studio-profile.py resolve         --profile my.profile.json   # JSON summary
    python scene-studio-profile.py render-apps-yaml --profile my.profile.json  # apps.yaml block + notes
"""

from __future__ import annotations

import argparse
import json
import re
import sys

SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9._-]+(:\d+)?$")
TARGET_PATH_RE = re.compile(r"^/addon_configs/[^/]+$")
RUNTIME_MODES = ("registry_admin", "read_only", "normal")
CONTENTION_POLICIES = ("yield", "takeover", "ignore")
ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

TOP_KEYS = {"ha_url", "ha_ssh_host", "appdaemon_config_root", "store_root", "runtime_mode", "providers", "integrations"}
PROVIDER_KEYS = {"hue", "wled", "ha_light"}
HUE_KEYS = {"enabled", "host", "application_key_secret", "bridge_id"}
WLED_KEYS = {"enabled", "hosts"}
HA_LIGHT_KEYS = {"enabled", "ignored_entity_ids"}
INTEGRATION_KEYS = {"hyperhdr"}
HYPERHDR_KEYS = {"enabled", "host", "default_policy", "probe_ttl_seconds", "wled_instance_ids", "hue_instance_ids"}


class ProfileError(Exception):
    """One or more profile validation failures (each message is actionable)."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _require_type(errors, path, value, expected, expected_desc):
    if not isinstance(value, expected):
        errors.append(f"{path}: expected {expected_desc}, got {type(value).__name__}")
        return False
    return True


def _validate_hue(errors, path, data):
    if not _require_type(errors, path, data, dict, "an object"):
        return
    unknown = set(data) - HUE_KEYS
    if unknown:
        errors.append(f"{path}: unknown key(s) {sorted(unknown)}; allowed: {sorted(HUE_KEYS)}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        errors.append(f"{path}.enabled: expected a boolean")
    enabled = data.get("enabled", False)
    host = data.get("host")
    if enabled:
        if not isinstance(host, str) or not HOST_RE.match(host):
            errors.append(f"{path}.host: expected a hue bridge host (hostname or IP, optional :port) when enabled")
        secret = data.get("application_key_secret")
        if not isinstance(secret, str) or not SECRET_NAME_RE.match(secret):
            errors.append(
                f"{path}.application_key_secret: expected an AppDaemon secrets.yaml NAME "
                "(lowercase letters/digits/underscores) — never the key value itself"
            )
    elif host not in (None, "") and not isinstance(host, str):
        errors.append(f"{path}.host: expected a string or null")
    bridge_id = data.get("bridge_id")
    if bridge_id is not None and not isinstance(bridge_id, str):
        errors.append(f"{path}.bridge_id: expected a string or null (the bridge hardware id, used for first-run fixture adoption)")


def _validate_wled(errors, path, data):
    if not _require_type(errors, path, data, dict, "an object"):
        return
    unknown = set(data) - WLED_KEYS
    if unknown:
        errors.append(f"{path}: unknown key(s) {sorted(unknown)}; allowed: {sorted(WLED_KEYS)}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        errors.append(f"{path}.enabled: expected a boolean")
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(item, str) for item in hosts):
        errors.append(f"{path}.hosts: expected a list of host strings")
        return
    if data.get("enabled", False) and len(hosts) == 0:
        errors.append(f"{path}.hosts: list at least one controller host when enabled")
    if len(hosts) > 1:
        errors.append(
            f"{path}.hosts: {len(hosts)} controllers given, but this runtime resolves ONE global "
            "wled_host; list the primary controller only (per-fixture endpoint hints are learned "
            "from discovery, and multi-controller discovery is a known limitation, not silent)"
        )
    for index, host in enumerate(hosts):
        if not HOST_RE.match(host):
            errors.append(f"{path}.hosts.{index}: expected a host (hostname or IP, optional :port)")


def _validate_ha_light(errors, path, data):
    if not _require_type(errors, path, data, dict, "an object"):
        return
    unknown = set(data) - HA_LIGHT_KEYS
    if unknown:
        errors.append(f"{path}: unknown key(s) {sorted(unknown)}; allowed: {sorted(HA_LIGHT_KEYS)}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        errors.append(f"{path}.enabled: expected a boolean")
    ignored = data.get("ignored_entity_ids", [])
    if not isinstance(ignored, list):
        errors.append(f"{path}.ignored_entity_ids: expected a list of HA entity ids")
        return
    for index, entity_id in enumerate(ignored):
        if not isinstance(entity_id, str) or not ENTITY_ID_RE.match(entity_id):
            errors.append(f"{path}.ignored_entity_ids.{index}: expected an entity id like 'light.kitchen_group'")


def _validate_hyperhdr(errors, path, data):
    if not _require_type(errors, path, data, dict, "an object"):
        return
    unknown = set(data) - HYPERHDR_KEYS
    if unknown:
        errors.append(f"{path}: unknown key(s) {sorted(unknown)}; allowed: {sorted(HYPERHDR_KEYS)}")
    if "enabled" in data and not isinstance(data["enabled"], bool):
        errors.append(f"{path}.enabled: expected a boolean")
    enabled = data.get("enabled", False)
    host = data.get("host")
    if enabled and (not isinstance(host, str) or not HOST_RE.match(host)):
        errors.append(f"{path}.host: expected a hyperHDR host:port (e.g. 'tv.local:8090') when enabled")
    if not enabled and host is not None and not isinstance(host, str):
        errors.append(f"{path}.host: expected a string or null")
    policy = data.get("default_policy", "yield")
    if policy not in CONTENTION_POLICIES:
        errors.append(f"{path}.default_policy: must be one of {list(CONTENTION_POLICIES)}")
    ttl = data.get("probe_ttl_seconds", 5)
    if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
        errors.append(f"{path}.probe_ttl_seconds: expected a positive number")
    for key in ("wled_instance_ids", "hue_instance_ids"):
        ids = data.get(key)
        if ids is None:
            continue
        if (
            not isinstance(ids, list)
            or not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in ids)
        ):
            errors.append(f"{path}.{key}: expected a list of non-negative instance indexes")


def validate_profile(profile):
    """Validate a parsed profile dict; raises ProfileError with every failure."""
    errors: list[str] = []
    if not isinstance(profile, dict):
        raise ProfileError(["profile: expected a JSON object"])
    unknown = set(profile) - TOP_KEYS
    if unknown:
        errors.append(f"profile: unknown key(s) {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}")

    ha_url = profile.get("ha_url")
    if not isinstance(ha_url, str) or not ha_url.startswith(("http://", "https://")):
        errors.append("ha_url: expected the Home Assistant URL, e.g. 'http://homeassistant.local:8123'")
    ssh_host = profile.get("ha_ssh_host")
    if ssh_host is not None and (not isinstance(ssh_host, str) or not ssh_host):
        errors.append("ha_ssh_host: expected an ssh config alias/host string or null")
    root = profile.get("appdaemon_config_root")
    if not isinstance(root, str) or not TARGET_PATH_RE.match(root):
        errors.append(
            "appdaemon_config_root: expected the AppDaemon add-on config directory on the HA host, "
            "e.g. '/addon_configs/a0d7b954_appdaemon' (find the exact slug under /addon_configs)"
        )
    store_root = profile.get("store_root")
    if not isinstance(store_root, str) or not store_root.startswith("/"):
        errors.append("store_root: expected an absolute POSIX path on the HA host, e.g. '/config/scene_studio_store'")
    mode = profile.get("runtime_mode", "registry_admin")
    if mode not in RUNTIME_MODES:
        errors.append(
            f"runtime_mode: must be one of {list(RUNTIME_MODES)}; first installs must start in "
            "'registry_admin' (provider writes blocked until the registry is deliberately built)"
        )

    providers = profile.get("providers", {})
    if not isinstance(providers, dict):
        errors.append("providers: expected an object")
    else:
        unknown = set(providers) - PROVIDER_KEYS
        if unknown:
            errors.append(f"providers: unknown provider(s) {sorted(unknown)}; allowed: {sorted(PROVIDER_KEYS)}")
        if "hue" in providers:
            _validate_hue(errors, "providers.hue", providers["hue"])
        if "wled" in providers:
            _validate_wled(errors, "providers.wled", providers["wled"])
        if "ha_light" in providers:
            _validate_ha_light(errors, "providers.ha_light", providers["ha_light"])
        if not any(
            isinstance(providers.get(key), dict) and providers[key].get("enabled", False)
            for key in PROVIDER_KEYS
        ):
            errors.append(
                "providers: at least one provider must be enabled — a bootstrap install still "
                "needs one reachable provider for discovery (it reads; it never writes during setup)"
            )

    integrations = profile.get("integrations", {})
    if not isinstance(integrations, dict):
        errors.append("integrations: expected an object")
    else:
        unknown = set(integrations) - INTEGRATION_KEYS
        if unknown:
            errors.append(f"integrations: unknown integration(s) {sorted(unknown)}; allowed: {sorted(INTEGRATION_KEYS)}")
        if "hyperhdr" in integrations:
            _validate_hyperhdr(errors, "integrations.hyperhdr", integrations["hyperhdr"])

    if errors:
        raise ProfileError(errors)
    return profile


def load_profile(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            profile = json.load(handle)
    except FileNotFoundError:
        raise ProfileError([f"profile file not found: {path}"]) from None
    except json.JSONDecodeError as exc:
        raise ProfileError([f"profile is not valid JSON: {path}: {exc}"]) from None
    return validate_profile(profile)


def normalized(profile):
    """Profile with defaults filled in (call after validate_profile).

    This is the ONE canonical resolved shape (the ``resolve`` subcommand's
    output and the installer's contract): top-level deployment targets plus
    the profile's own ``providers``/``integrations`` nesting. Consumers must
    read ``resolved["providers"]["hue"]`` etc., never flattened keys.
    """
    providers = profile.get("providers", {})
    integrations = profile.get("integrations", {})
    hue = providers.get("hue", {})
    wled = providers.get("wled", {})
    ha_light = providers.get("ha_light", {})
    hyperhdr = integrations.get("hyperhdr", {})
    return {
        "ha_url": profile["ha_url"],
        "ha_ssh_host": profile.get("ha_ssh_host"),
        "appdaemon_config_root": profile["appdaemon_config_root"],
        "store_root": profile["store_root"],
        "runtime_mode": profile.get("runtime_mode", "registry_admin"),
        "providers": {
            "hue": {
                "enabled": bool(hue.get("enabled", False)),
                "host": hue.get("host"),
                "application_key_secret": hue.get("application_key_secret"),
                "bridge_id": hue.get("bridge_id"),
            },
            "wled": {"enabled": bool(wled.get("enabled", False)), "hosts": list(wled.get("hosts", []))},
            "ha_light": {
                "enabled": bool(ha_light.get("enabled", True)),
                "ignored_entity_ids": list(ha_light.get("ignored_entity_ids", [])),
            },
        },
        "integrations": {
            "hyperhdr": {
                "enabled": bool(hyperhdr.get("enabled", False)),
                "host": hyperhdr.get("host"),
                "default_policy": hyperhdr.get("default_policy", "yield"),
                "probe_ttl_seconds": hyperhdr.get("probe_ttl_seconds", 5),
                "wled_instance_ids": hyperhdr.get("wled_instance_ids"),
                "hue_instance_ids": hyperhdr.get("hue_instance_ids"),
            },
        },
    }


def render_apps_yaml(profile):
    """Render the Scene Studio ``apps.yaml`` block + setup notes for a validated profile."""
    data = normalized(profile)
    lines = [
        "# Scene Studio — generated from a validated portable deployment profile.",
        "# Merge this block into AppDaemon's apps.yaml. Provider keys appear only",
        "# when the profile enables them. Secrets stay NAMES resolved from the",
        "# add-on's secrets.yaml — never values.",
        "scene_studio:",
        "  module: scene_studio.appdaemon_adapter.adapter",
        "  class: SceneStudioApp",
    ]
    mode = data["runtime_mode"]
    lines.append(f"  registry_admin: {'true' if mode == 'registry_admin' else 'false'}")
    lines.append(f"  read_only: {'true' if mode == 'read_only' else 'false'}")
    lines.append("  legacy_events_enabled: false")
    lines.append(f"  store_root: {data['store_root']}")
    notes: list[str] = []
    hue = data["providers"]["hue"]
    if hue["enabled"]:
        lines.append(f"  hue_ip: {hue['host']}")
        lines.append(f"  hue_username: !secret {hue['application_key_secret']}")
        if hue["bridge_id"]:
            lines.append(f"  hue_bridge_id: {hue['bridge_id']}")
        else:
            notes.append(
                "providers.hue.bridge_id is unset: hue discovery works, but first-run "
                "fixture.adopt on Hue lights needs the bridge hardware id (Hue app > "
                "Settings > My Hue bridge). Add 'hue_bridge_id: <id>' before adoption."
            )
    wled = data["providers"]["wled"]
    if wled["enabled"]:
        lines.append(f"  wled_host: {wled['hosts'][0]}")
    ha_light = data["providers"]["ha_light"]
    if not ha_light["enabled"]:
        # Only emitted when DISABLED: the adapter's default is discovery on,
        # and the generated block must state an opt-out explicitly rather
        # than carry an option the runtime would otherwise ignore.
        lines.append("  ha_light_enabled: false")
    if ha_light["ignored_entity_ids"]:
        lines.append(f"  ignored_ha_entity_ids: {json.dumps(ha_light['ignored_entity_ids'])}")
    hyperhdr = data["integrations"]["hyperhdr"]
    if hyperhdr["enabled"]:
        lines.append(f"  hyperhdr_host: {hyperhdr['host']}")
        lines.append(f"  contention_default_policy: {hyperhdr['default_policy']}")
        lines.append(f"  hyperhdr_probe_ttl_seconds: {int(hyperhdr['probe_ttl_seconds'])}")
        if hyperhdr["wled_instance_ids"] is not None:
            lines.append(f"  hyperhdr_wled_instance_ids: {json.dumps(hyperhdr['wled_instance_ids'])}")
        if hyperhdr["hue_instance_ids"] is not None:
            lines.append(f"  hyperhdr_hue_instance_ids: {json.dumps(hyperhdr['hue_instance_ids'])}")
    else:
        notes.append(
            "hyperHDR contention is not configured (optional integration): the contention "
            "layer reports configured:false and every gate is a pass-through."
        )
    if mode == "registry_admin":
        notes.append(
            "runtime_mode is 'registry_admin' (the safe first-install default): discovery and "
            "registry/bootstrap commands work, but scene.apply, playback, and every provider "
            "write are blocked. When the registry is deliberately ready, set both "
            "'registry_admin: false' and 'read_only: false' (mode 'normal') and restart AppDaemon."
        )
    lines.extend([
        "", "scene_studio_update:",
        "  module: scene_studio_update_supervisor",
        "  class: SceneStudioUpdateSupervisor",
        f"  addon_slug: {data['appdaemon_config_root'].rsplit('/', 1)[-1]}",
        "  config_root: /config",
        f"  store_root: {data['store_root']}",
        "  api_url: http://127.0.0.1:5050",
    ])
    return "\n".join(lines), notes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "resolve", "render-apps-yaml"):
        child = sub.add_parser(name)
        child.add_argument("--profile", required=True, help="path to the deployment profile JSON")
    args = parser.parse_args(argv)
    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print("Profile INVALID:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    if args.cmd == "validate":
        print("Profile OK.")
        return 0
    if args.cmd == "resolve":
        print(json.dumps(normalized(profile), indent=2, sort_keys=True))
        return 0
    block, notes = render_apps_yaml(profile)
    print(block)
    if notes:
        print()
        print("Notes:")
        for note in notes:
            print(f"- {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
