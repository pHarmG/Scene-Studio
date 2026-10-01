"""HA-native compact control-surface bridge — pure tests (HA card rework plan §4).

No AppDaemon: every function under test takes/returns plain dicts.
"""

from scene_studio.appdaemon_adapter.ui_bridge import (
    UI_BRIDGE_ALLOWLIST,
    build_projection_state,
    build_ui_command_envelope,
)
from scene_studio.domain.bindings import HaLightBinding, HueBinding
from scene_studio.domain.fixtures import Fixture, FixtureRegistry, Target


# ---------------------------------------------------------------------------
# build_ui_command_envelope: scene type/action mapping (plan §13)
# ---------------------------------------------------------------------------


def test_scene_apply_envelope():
    envelope, rejection = build_ui_command_envelope(
        {"command": "scene.apply", "scene_id": "twilight", "request_id": "r1"}
    )
    assert rejection is None
    assert envelope == {"command": "scene.apply", "request_id": "r1", "scene_id": "twilight"}


def test_playback_start_envelope_with_optional_target():
    envelope, rejection = build_ui_command_envelope(
        {"command": "playback.start", "scene_id": "aurora_flow", "target_id": "studio"}
    )
    assert rejection is None
    assert envelope == {"command": "playback.start", "scene_id": "aurora_flow", "target_id": "studio"}


def test_playback_pause_envelope_uses_exact_session_id():
    envelope, rejection = build_ui_command_envelope(
        {"command": "playback.pause", "session_id": "sess-123", "request_id": "r2"}
    )
    assert rejection is None
    assert envelope == {"command": "playback.pause", "request_id": "r2", "session_id": "sess-123"}


def test_playback_resume_envelope_uses_exact_session_id():
    envelope, rejection = build_ui_command_envelope(
        {"command": "playback.resume", "session_id": "sess-456"}
    )
    assert rejection is None
    assert envelope == {"command": "playback.resume", "session_id": "sess-456"}


def test_playback_stop_envelope_uses_exact_session_id():
    envelope, rejection = build_ui_command_envelope(
        {"command": "playback.stop", "session_id": "sess-789"}
    )
    assert rejection is None
    assert envelope == {"command": "playback.stop", "session_id": "sess-789"}


def test_scene_archive_envelope():
    envelope, rejection = build_ui_command_envelope({"command": "scene.archive", "scene_id": "meeting_blue"})
    assert rejection is None
    assert envelope == {"command": "scene.archive", "scene_id": "meeting_blue"}


def test_playback_pause_ignores_a_scene_id_it_was_not_given():
    """The card must address lifecycle commands by exact session_id — a
    scene_id supplied alongside must never be forwarded or substituted."""
    envelope, rejection = build_ui_command_envelope(
        {"command": "playback.pause", "session_id": "sess-1", "scene_id": "twilight"}
    )
    assert rejection is None
    assert envelope == {"command": "playback.pause", "session_id": "sess-1"}
    assert "scene_id" not in envelope


# ---------------------------------------------------------------------------
# build_ui_command_envelope: bridge allowlist + validation (plan §4/§13)
# ---------------------------------------------------------------------------


def test_allowlist_is_exactly_the_plan_suggested_set():
    assert UI_BRIDGE_ALLOWLIST == {
        "scene.apply",
        "playback.start",
        "playback.pause",
        "playback.resume",
        "playback.stop",
        "scene.archive",
    }


def test_registry_administration_and_builder_commands_are_rejected():
    for command in (
        "fixture.enable",
        "fixture.rebind",
        "registry.migrate",
        "discovery.run",
        "scene.create",
        "scene.update",
        "scene.rename",
    ):
        envelope, rejection = build_ui_command_envelope({"command": command, "scene_id": "x"})
        assert envelope is None, command
        assert rejection["ok"] is False
        assert "not permitted through the UI bridge" in rejection["error"]


def test_missing_command_is_rejected():
    envelope, rejection = build_ui_command_envelope({})
    assert envelope is None
    assert rejection["command"] is None
    assert rejection["ok"] is False


def test_non_dict_payload_is_rejected_not_raised():
    envelope, rejection = build_ui_command_envelope(None)
    assert envelope is None
    assert rejection["ok"] is False
    envelope, rejection = build_ui_command_envelope("not a dict")
    assert envelope is None
    assert rejection["ok"] is False


def test_scene_apply_without_scene_id_is_rejected():
    envelope, rejection = build_ui_command_envelope({"command": "scene.apply"})
    assert envelope is None
    assert rejection["command"] == "scene.apply"
    assert "scene_id is required" in rejection["error"]


def test_playback_pause_without_session_id_is_rejected():
    envelope, rejection = build_ui_command_envelope({"command": "playback.pause"})
    assert envelope is None
    assert rejection["command"] == "playback.pause"
    assert "session_id is required" in rejection["error"]


def test_blank_ids_are_treated_as_missing():
    envelope, rejection = build_ui_command_envelope({"command": "scene.apply", "scene_id": "   "})
    assert envelope is None
    assert "scene_id is required" in rejection["error"]


def test_request_id_passes_through_even_on_rejection():
    envelope, rejection = build_ui_command_envelope(
        {"command": "fixture.enable", "request_id": "r-keep-me"}
    )
    assert envelope is None
    assert rejection["request_id"] == "r-keep-me"


# ---------------------------------------------------------------------------
# build_projection_state: the compact HA-visible projection (plan §4)
# ---------------------------------------------------------------------------


def _status(*, mode="normal", provider_writes_blocked=False, revision=7, sessions=None, allowed_commands=None):
    return {
        "engine": {"ok": True, "revision": revision, "event_capacity": 500, "events": 3},
        "runtime": {
            "mode": mode,
            "read_only": mode == "read_only",
            "provider_writes_blocked": provider_writes_blocked,
            "allowed_commands": allowed_commands if allowed_commands is not None else ["scene.apply"],
        },
        "playback": {"sessions": sessions or [], "counts": {}, "owned_fixture_count": 0},
    }


def test_projection_state_is_the_runtime_mode():
    state, _attrs = build_projection_state(_status(mode="normal"), {"scenes": []}, last_command=None)
    assert state == "normal"
    state, _attrs = build_projection_state(_status(mode="read_only"), {"scenes": []}, last_command=None)
    assert state == "read_only"


def test_projection_attributes_carry_engine_revision_and_policy():
    _state, attrs = build_projection_state(
        _status(revision=42, provider_writes_blocked=True), {"scenes": []}, last_command=None
    )
    assert attrs["engine_revision"] == 42
    assert attrs["runtime_mode"] == "normal"
    assert attrs["provider_writes_blocked"] is True


def test_projection_scenes_are_compact_and_ordered_by_catalog():
    scenes = {
        "scenes": [
            {
                "id": "twilight",
                "name": "Twilight",
                "palette": ["#111", "#222"],
                "motion": {"mode": "static"},
                "target_ids": ["studio"],
            },
            {
                "id": "aurora_flow",
                "name": "Aurora Flow",
                "palette": ["#0ff", "#00f"],
                "motion": {"mode": "palette_cycle"},
                "target_ids": ["studio", "office"],
            },
        ]
    }
    _state, attrs = build_projection_state(_status(), scenes, last_command=None)
    assert attrs["scenes"] == [
        {
            "id": "twilight",
            "name": "Twilight",
            "palette": ["#111", "#222"],
            "motion_mode": "static",
            "target_ids": ["studio"],
        },
        {
            "id": "aurora_flow",
            "name": "Aurora Flow",
            "palette": ["#0ff", "#00f"],
            "motion_mode": "palette_cycle",
            "target_ids": ["studio", "office"],
        },
    ]


def test_projection_scene_with_no_palette_or_motion_still_projects_cleanly():
    scenes = {"scenes": [{"id": "plain", "name": "Plain"}]}
    _state, attrs = build_projection_state(_status(), scenes, last_command=None)
    assert attrs["scenes"] == [
        {"id": "plain", "name": "Plain", "palette": [], "motion_mode": "static", "target_ids": []}
    ]


def test_projection_sessions_exclude_stopped_and_never_pick_a_singleton():
    """Plural-session correctness (plan §6/§13): the projection is a plain
    list of every LIVE session for a scene — never a first-session shortcut."""
    sessions = [
        {"session_id": "s1", "scene_id": "aurora_flow", "state": "active", "target_ids": ["studio"]},
        {"session_id": "s2", "scene_id": "aurora_flow", "state": "paused", "target_ids": ["office"]},
        {"session_id": "s3", "scene_id": "old_scene", "state": "stopped", "target_ids": ["bedroom"]},
    ]
    _state, attrs = build_projection_state(_status(sessions=sessions), {"scenes": []}, last_command=None)
    assert attrs["sessions"] == [
        {"session_id": "s1", "scene_id": "aurora_flow", "state": "active", "target_ids": ["studio"]},
        {"session_id": "s2", "scene_id": "aurora_flow", "state": "paused", "target_ids": ["office"]},
    ]


def test_projection_allowed_commands_is_the_bridge_allowlist_intersected_with_engine_policy():
    """Contracts §4: frontends read `runtime` to disable actions, never
    duplicate command rules — the card must get a pre-narrowed truth."""
    status = _status(
        allowed_commands=[
            "scene.apply", "scene.archive", "fixture.enable", "discovery.run", "playback.start",
        ]
    )
    _state, attrs = build_projection_state(status, {"scenes": []}, last_command=None)
    assert attrs["allowed_commands"] == sorted(["playback.start", "scene.apply", "scene.archive"])
    # never leaks a command outside the bridge's own allowlist, however
    # permissive the engine policy is
    assert set(attrs["allowed_commands"]) <= UI_BRIDGE_ALLOWLIST
    assert "fixture.enable" not in attrs["allowed_commands"]
    assert "discovery.run" not in attrs["allowed_commands"]


def test_projection_allowed_commands_empty_in_a_fully_restricted_mode():
    status = _status(allowed_commands=["scene.preview", "discovery.run", "diagnostics.export"])
    _state, attrs = build_projection_state(status, {"scenes": []}, last_command=None)
    assert attrs["allowed_commands"] == []


def test_projection_carries_schema_version_current_and_empty_targets_by_default():
    status = _status()
    status["current"] = {"scene_id": "meeting_blue", "target_id": None}
    _state, attrs = build_projection_state(status, {"scenes": []}, last_command=None)
    assert attrs["bridge_schema_version"] == 3
    assert attrs["current"] == {"scene_id": "meeting_blue", "target_id": None}
    assert attrs["targets"] == []


def test_projection_targets_come_from_canonical_registry_membership():
    registry = FixtureRegistry(
        fixtures=[
            Fixture(
                id="lamp",
                name="Lamp",
                groups=["studio"],
                binding=HaLightBinding(ha_entity_id="light.lamp"),
            ),
            Fixture(
                id="g_strip",
                name="G Strip",
                groups=["studio"],
                enabled=False,
                binding=HueBinding(bridge_id="b", resource_id="r", ha_entity_id="light.hue_g_strip"),
            ),
        ],
        targets=[Target(id="studio", name="Studio")],
    )
    _state, attrs = build_projection_state(_status(), {"scenes": []}, last_command=None, registry=registry)
    assert attrs["targets"] == [
        {
            "id": "studio",
            "name": "Studio",
            "fixture_ids": ["lamp"],
            "ha_entity_ids": ["light.lamp"],
            "enabled_fixture_count": 1,
            "ha_covered_fixture_count": 1,
            "complete_ha_coverage": True,
        }
    ]


def test_projection_carries_last_command_verbatim():
    last_command = {"request_id": "r1", "ok": True, "command": "scene.apply"}
    _state, attrs = build_projection_state(_status(), {"scenes": []}, last_command=last_command)
    assert attrs["last_command"] == last_command
    _state, attrs = build_projection_state(_status(), {"scenes": []}, last_command=None)
    assert attrs["last_command"] is None
