#!/usr/bin/env python3
"""Render AppDaemon runtime configuration for the Scene Studio add-on.

Runs inside the add-on container at every start (invoked by run.sh) and turns
the Supervisor-provided options (/data/options.json) into:

- ``<data>/appdaemon.yaml`` — AppDaemon itself: ``app_dir`` pointing into the
  immutable image payload, HTTP on the container port, and the hass plugin
  pointed at the Supervisor HA proxy. The SUPERVISOR_TOKEN is resolved at
  config-load time via AppDaemon's ``!env_var`` tag, so the token value is
  never written to disk anywhere; it also never appears in apps.yaml.
- ``<payload>/apps/apps.yaml`` — the scene_studio app block between managed
  markers. AppDaemon 4.5 discovers app config files inside app_dir (not the
  config dir) and resolves ``!secret`` against a secrets.yaml NEXT TO each
  app config file. Secret values never appear in apps.yaml itself: the Hue
  key lives in that secrets.yaml under a NAME.
- ``<payload>/apps/secrets.yaml`` — secret values, mode 0600, only when
  configured.

The immutable product payload lives in the image (``/opt/scene-studio``);
only rendered configuration, the store, and a synced copy of the Workbench
live in ``/data``, so Supervisor updates replace the payload while user data
persists (each start re-syncs the Workbench from the image).

Stdlib only. Every interpolated value is written through ``json.dumps`` so
the output is always valid YAML, and the stdout summary never contains a
secret value.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

PAYLOAD_DEFAULT = "/opt/scene-studio"
DATA_DEFAULT = "/data"
SECRETS_NAME_HUE_KEY = "scene_studio_hue_key"
# mode -> (registry_admin, read_only): fresh installs must start in
# registry_admin; the option replaces the guided installer's manual apps.yaml
# flip after the Workbench setup view is complete.
MODES = {"setup": (True, False), "normal": (False, False), "read_only": (False, True)}
MANAGED_BEGIN = "# >>> scene_studio managed block (rendered by the Scene Studio add-on) >>>"
MANAGED_END = "# <<< scene_studio managed block <<<"
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$")
BRIDGE_ID_RE = re.compile(r"^[0-9A-Za-z\-]{4,32}$")
ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_\-]+$")


def _text(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    return value or None


def _host(value, field):
    value = _text(value, field)
    if value is None:
        return None
    if not HOST_RE.fullmatch(value):
        raise ValueError(f"{field} must be a bare host or host:port (no scheme, path, or whitespace)")
    port = value.rsplit(":", 1)[-1]
    if ":" in value and int(port) > 65535:
        raise ValueError(f"{field} port is out of range")
    return value


def normalize_options(options):
    """Validate Supervisor options; returns the canonical rendering input."""
    if not isinstance(options, dict):
        raise ValueError("options.json must contain a JSON object")
    mode = _text(options.get("mode"), "mode") or "setup"
    if mode not in MODES:
        raise ValueError("mode must be one of: setup, normal, read_only")
    hue_host = _host(options.get("hue_host"), "hue_host")
    hue_key = _text(options.get("hue_key"), "hue_key")
    hue_bridge_id = _text(options.get("hue_bridge_id"), "hue_bridge_id")
    if hue_bridge_id and not BRIDGE_ID_RE.fullmatch(hue_bridge_id):
        raise ValueError("hue_bridge_id must be 4-32 letters, digits, or dashes")
    if bool(hue_host) != bool(hue_key):
        raise ValueError("Hue needs both hue_host and hue_key (or neither)")
    if hue_bridge_id and not hue_host:
        raise ValueError("hue_bridge_id requires hue_host and hue_key")
    ignored = options.get("ignored_ha_entity_ids") or []
    if not isinstance(ignored, list):
        raise ValueError("ignored_ha_entity_ids must be a list")
    entities = []
    for entity in ignored:
        if not isinstance(entity, str) or not ENTITY_ID_RE.fullmatch(entity.strip()):
            raise ValueError(f"ignored_ha_entity_ids entries must be entity ids like light.kitchen: {entity!r}")
        entities.append(entity.strip())
    return {
        "mode": mode,
        "hue_host": hue_host,
        "hue_key": hue_key,
        "hue_bridge_id": hue_bridge_id,
        "wled_host": _host(options.get("wled_host"), "wled_host"),
        "hyperhdr_host": _host(options.get("hyperhdr_host"), "hyperhdr_host"),
        "ignored_ha_entity_ids": entities,
    }


def render_apps_yaml(options, data_dir=DATA_DEFAULT):
    registry_admin, read_only = MODES[options["mode"]]
    # Container paths are POSIX regardless of the OS rendering this file, so
    # they are built as strings (never os.path/Path).
    store_root = (data_dir.rstrip("/") or "") + "/scene_studio_store"
    lines = [
        MANAGED_BEGIN,
        "scene_studio:",
        "  module: scene_studio.appdaemon_adapter.adapter",
        "  class: SceneStudioApp",
        f"  registry_admin: {json.dumps(registry_admin)}",
        f"  read_only: {json.dumps(read_only)}",
        "  legacy_events_enabled: false",
        f"  store_root: {json.dumps(store_root)}",
    ]
    if options["hue_host"]:
        lines += [
            f"  hue_ip: {json.dumps(options['hue_host'])}",
            f"  hue_username: !secret {SECRETS_NAME_HUE_KEY}",
        ]
        if options["hue_bridge_id"]:
            lines.append(f"  hue_bridge_id: {json.dumps(options['hue_bridge_id'])}")
    if options["wled_host"]:
        lines.append(f"  wled_host: {json.dumps(options['wled_host'])}")
    if options["hyperhdr_host"]:
        lines.append(f"  hyperhdr_host: {json.dumps(options['hyperhdr_host'])}")
    if options["ignored_ha_entity_ids"]:
        lines.append(f"  ignored_ha_entity_ids: {json.dumps(options['ignored_ha_entity_ids'])}")
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def render_secrets_yaml(options):
    if not options["hue_key"]:
        return None
    return f"{SECRETS_NAME_HUE_KEY}: {json.dumps(options['hue_key'])}\n"


def render_appdaemon_yaml(payload_root=PAYLOAD_DEFAULT, time_zone=None):
    # The SUPERVISOR_TOKEN is referenced with AppDaemon's !env_var tag and
    # resolved from the process environment when the config is loaded — the
    # token value is never rendered to disk. A missing token fails loudly at
    # config load (AppDaemon raises), which is correct for an add-on where
    # Supervisor always provides it.
    lines = [
        "appdaemon:",
        f"  app_dir: {json.dumps(payload_root + '/apps')}",
        # AppDaemon 4 treats these four as mandatory and refuses to boot
        # without them. Scene Studio does not use AppDaemon's astro features
        # (scene scheduling lives in Home Assistant), so a detected zone with
        # neutral coordinates is the honest default. total_threads keeps a
        # worker pool for the sync-style adapter app.
        f"  time_zone: {json.dumps(time_zone or 'UTC')}",
        "  latitude: 0.0",
        "  longitude: 0.0",
        "  elevation: 0",
        "  total_threads: 10",
        "  plugins:",
        "    hass:",
        "      type: hass",
        "      ha_url: http://supervisor/core",
        "      token: !env_var SUPERVISOR_TOKEN",
    ]
    lines += [
        "http:",
        # AppDaemon urlparses this (scheme required for a netloc); 0.0.0.0
        # binds all container interfaces for the host port mapping.
        "  url: http://0.0.0.0:5050",
        # Presence alone enables the AppDaemon API component that routes
        # /api/appdaemon/scene_studio_api to the backend adapter.
        "api:",
    ]
    return "\n".join(lines) + "\n"


def detect_time_zone(localtime="/etc/localtime"):
    """Best-effort zoneinfo name from the container's localtime symlink."""
    try:
        target = os.path.realpath(localtime)
    except OSError:
        return None
    if "/zoneinfo/" not in target:
        return None
    return target.split("/zoneinfo/", 1)[1] or None


def write_atomic(path, content, mode=0o600):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    os.replace(temp, path)


def summarize(options, supervisor_token):
    parts = [f"mode: {options['mode']}"]
    for name in ("hue_host", "wled_host", "hyperhdr_host"):
        if options[name]:
            parts.append(f"{name}: {options[name]}")
    if options["hue_host"] and options["hue_bridge_id"]:
        parts.append("hue_bridge_id: set")
    if options["ignored_ha_entity_ids"]:
        parts.append(f"ignored_ha_entity_ids: {len(options['ignored_ha_entity_ids'])} configured")
    parts.append("SUPERVISOR_TOKEN: " + ("present" if supervisor_token else "ABSENT (HA connection will fail)"))
    return ", ".join(parts)


def sync_workbench(data_dir, payload_root):
    """Mirror the image's Workbench payload into the persistent /data/www.

    AppDaemon serves /local from <config_dir>/www and aiohttp refuses to
    follow symlinks, so the payload is copied rather than linked. Re-syncing
    at every start is what makes Supervisor image updates propagate.
    """
    source = Path(payload_root) / "www"
    target = Path(data_dir) / "www"
    if not source.is_dir():
        print(f"warning: {source} missing — Workbench will not be served", file=sys.stderr)
        return
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)


def ensure_store(data_dir):
    Path(data_dir).joinpath("scene_studio_store").mkdir(mode=0o755, parents=True, exist_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=DATA_DEFAULT)
    parser.add_argument("--payload-root", default=PAYLOAD_DEFAULT)
    args = parser.parse_args(argv)

    options_path = Path(args.data_dir) / "options.json"
    if not options_path.is_file():
        print(f"error: {options_path} not found — this script runs inside the Scene Studio add-on", file=sys.stderr)
        return 1
    options = normalize_options(json.loads(options_path.read_text(encoding="utf-8")))
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    time_zone = detect_time_zone()

    data_dir = Path(args.data_dir)
    write_atomic(data_dir / "appdaemon.yaml",
                 render_appdaemon_yaml(args.payload_root, time_zone))
    # AppDaemon 4.5 discovers app config files INSIDE app_dir (not the config
    # dir), and resolves !secret against a secrets.yaml next to each app
    # config file. Both are container-layer writes re-rendered at every start
    # from the persistent options, so image updates re-render them.
    apps_dir = Path(args.payload_root) / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(apps_dir / "apps.yaml", render_apps_yaml(options, args.data_dir))
    secrets = render_secrets_yaml(options)
    if secrets is None:
        (apps_dir / "secrets.yaml").unlink(missing_ok=True)
    else:
        write_atomic(apps_dir / "secrets.yaml", secrets)
    # Pre-4.5 render locations are inert but misleading; clean them up.
    (data_dir / "apps.yaml").unlink(missing_ok=True)
    (data_dir / "secrets.yaml").unlink(missing_ok=True)
    ensure_store(args.data_dir)
    sync_workbench(args.data_dir, args.payload_root)

    print("Scene Studio add-on configuration rendered: " + summarize(options, supervisor_token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
