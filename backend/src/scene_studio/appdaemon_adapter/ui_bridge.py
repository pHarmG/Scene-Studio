"""HA-native compact control-surface bridge — the canonical HA integration seam.

The file name remains ``ui_bridge.py`` from the HA scene-control card rework;
the contract it publishes is now the general Home Assistant ↔ Scene Studio
boundary (unification pass):

- **Command ingress** (:data:`UI_COMMAND_EVENT` = ``scene_studio_ui_command``):
  the compact card AND HA scripts/automations fire this event. The allowlist
  stays intentionally small — Builder, discovery mutation, registry-admin,
  and arbitrary engine RPC stay unreachable through HA.
- **State projection** (:data:`UI_PROJECTION_ENTITY` = ``sensor.scene_studio_ui``):
  a compact revision-driven attribute set (scenes, sessions, canonical target
  membership, last command, schema version). Never a second scene store.

Pure glue, no AppDaemon import — every function here takes/returns plain
dicts so it is unit-testable without a running daemon. ``adapter.py`` is
the only module that touches ``self.listen_event`` / ``self.set_state``.
"""

from __future__ import annotations

from ..domain.fixtures import FixtureRegistry, project_target_membership

__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "UI_BRIDGE_ALLOWLIST",
    "UI_COMMAND_EVENT",
    "UI_PROJECTION_ENTITY",
    "build_projection_state",
    "build_ui_command_envelope",
    "project_canonical_targets",
]

# Canonical event the card AND HA scripts fire (via hass.callApi or event).
UI_COMMAND_EVENT = "scene_studio_ui_command"

# Compact HA-visible projection entity the card and scripts read.
UI_PROJECTION_ENTITY = "sensor.scene_studio_ui"

# Lets HA cards/scripts detect the expected projection contract without
# inferring it from individual attributes. v2 adds canonical target
# membership and an explicit schema version. v3 adds per-scene ``target_ids``
# so controllers resolve a scene's lights from authoring data alone — live
# sessions and the applied-``current`` pointer are runtime state that
# automations and restarts legitimately clear, and tying slider visibility
# to them made the trim disappear under exactly those conditions.
BRIDGE_SCHEMA_VERSION = 3

# Plan §4 "Suggested UI bridge allowlist" — deliberately smaller than the
# full COMMAND_CATALOG. Registry administration, discovery mutation,
# Builder create/update, and everything else stay unreachable through this
# seam regardless of runtime mode.
UI_BRIDGE_ALLOWLIST = frozenset(
    {
        "scene.apply",
        "playback.start",
        "playback.pause",
        "playback.resume",
        "playback.stop",
        "scene.archive",
    }
)

# Commands addressed by scene_id (+ optional target_id for the two that take one).
_SCENE_ID_COMMANDS = frozenset({"scene.apply", "playback.start", "scene.archive"})
_TARGET_ID_COMMANDS = frozenset({"scene.apply", "playback.start"})
# Commands addressed by an exact backend-issued session_id (never a scene_id).
_SESSION_ID_COMMANDS = frozenset({"playback.pause", "playback.resume", "playback.stop"})


def _clean_str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def build_ui_command_envelope(data: dict) -> tuple[dict | None, dict | None]:
    """Validate one ``scene_studio_ui_command`` event payload.

    Returns ``(envelope, None)`` on success — ``envelope`` is a canonical
    command dict ready for ``engine.handle()`` — or ``(None, rejection)``
    where ``rejection`` is a compact ``last_command``-shaped dict
    (``request_id``/``ok``/``command``/``error``) so the caller can publish
    an honest failure without ever calling the engine. Never raises.
    """
    if not isinstance(data, dict):
        return None, {"request_id": None, "ok": False, "command": None, "error": "malformed event payload"}

    request_id = _clean_str(data.get("request_id"))
    command = data.get("command")
    if not isinstance(command, str) or command not in UI_BRIDGE_ALLOWLIST:
        return None, {
            "request_id": request_id,
            "ok": False,
            "command": command if isinstance(command, str) else None,
            "error": f"command {command!r} is not permitted through the UI bridge",
        }

    envelope: dict = {"command": command}
    if request_id:
        envelope["request_id"] = request_id

    if command in _SCENE_ID_COMMANDS:
        scene_id = _clean_str(data.get("scene_id"))
        if scene_id is None:
            return None, {
                "request_id": request_id, "ok": False, "command": command,
                "error": "scene_id is required",
            }
        envelope["scene_id"] = scene_id
        if command in _TARGET_ID_COMMANDS:
            target_id = _clean_str(data.get("target_id"))
            if target_id is not None:
                envelope["target_id"] = target_id
    elif command in _SESSION_ID_COMMANDS:
        session_id = _clean_str(data.get("session_id"))
        if session_id is None:
            return None, {
                "request_id": request_id, "ok": False, "command": command,
                "error": "session_id is required",
            }
        envelope["session_id"] = session_id

    return envelope, None


def build_projection_state(
    status: dict,
    scenes: dict,
    *,
    last_command: dict | None,
    registry: FixtureRegistry | None = None,
    targets: list[dict] | None = None,
) -> tuple[str, dict]:
    """Compress ``engine.status()`` + the active scene catalog into the
    compact projection ``(state, attributes)`` pair for
    :data:`UI_PROJECTION_ENTITY`.

    ``state`` is the runtime mode (a short, meaningful single value); the
    rich data lives entirely in attributes. Only LIVE playback sessions
    (not ``stopped``) are included — the card needs "is something running",
    not stopped-session history. Never mirrors full scene documents or
    backend diagnostics.

    ``allowed_commands`` is ``UI_BRIDGE_ALLOWLIST`` intersected with the
    engine's own ``runtime.allowed_commands`` — contracts §4: "Frontends
    only read `runtime` to disable unavailable actions — they never
    duplicate command rules." The card must never infer availability from
    ``runtime_mode`` by name; this is the backend-owned truth, pre-narrowed
    to exactly the commands this bridge can ever send.
    """
    runtime = status.get("runtime") or {}
    mode = runtime.get("mode") or "unknown"
    engine_allowed = set(runtime.get("allowed_commands") or [])
    allowed_commands = sorted(UI_BRIDGE_ALLOWLIST & engine_allowed)

    scene_list = [
        {
            "id": scene.get("id"),
            "name": scene.get("name"),
            "palette": list(scene.get("palette") or []),
            "motion_mode": (scene.get("motion") or {}).get("mode", "static"),
            "target_ids": list(scene.get("target_ids") or []),
        }
        for scene in scenes.get("scenes", [])
    ]

    playback = status.get("playback") or {}
    session_list = [
        {
            "session_id": session.get("session_id"),
            "scene_id": session.get("scene_id"),
            "state": session.get("state"),
            "target_ids": list(session.get("target_ids") or []),
        }
        for session in playback.get("sessions", [])
        if session.get("state") != "stopped"
    ]

    if targets is None and registry is not None:
        targets = project_canonical_targets(registry)
    elif targets is None:
        targets = []

    attributes = {
        "bridge_schema_version": BRIDGE_SCHEMA_VERSION,
        "engine_revision": (status.get("engine") or {}).get("revision"),
        "runtime_mode": mode,
        "provider_writes_blocked": bool(runtime.get("provider_writes_blocked")),
        "allowed_commands": allowed_commands,
        "current": status.get("current"),
        "scenes": scene_list,
        "sessions": session_list,
        "targets": list(targets),
        "last_command": last_command,
    }
    return mode, attributes


def project_canonical_targets(registry: FixtureRegistry) -> list[dict]:
    """Delegate to the domain projector so HA never copies membership logic."""
    return project_target_membership(registry)
