"""Routine projection domain tests (routines pass, Task 1).

Covers the pure parser/classifier: structural recognition of the canonical
``scene_studio_ui_command`` bridge, the supported grammar (exact time +
optional weekdays + one scene action), ``recognized_advanced`` detection,
provenance independence, alias/scene rename resilience, and the normalized
concurrency digest.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scene_studio.domain.routines import (
    CLASSIFICATION_ADVANCED,
    CLASSIFICATION_NATIVE,
    ROUTINE_SCHEMA_VERSION,
    RoutineSchedule,
    build_alias,
    build_provenance_description,
    canonical_config_digest,
    classify_automation,
    describe_schedule,
    find_bridge_reference,
    format_time_12h,
    generate_routine_config,
    new_automation_id,
    parse_provenance,
    validate_automation_id,
    validate_routine_time,
    validate_routine_weekdays,
)
from scene_studio.domain.serde import ValidationError

# ---------------------------------------------------------------------------
# canonical config builders
# ---------------------------------------------------------------------------


def bridge_action(command: str = "scene.apply", scene_id: str = "evening_glow") -> dict:
    return {
        "event": "scene_studio_ui_command",
        "event_data": {"command": command, "scene_id": scene_id},
    }


def native_config(
    *,
    at: str = "19:30:00",
    weekdays=None,
    command: str = "scene.apply",
    scene_id: str = "evening_glow",
    alias: str = "Scene Studio · Evening Glow · Weekdays 7:00 PM",
    automation_id: str = "ssr_0f1e2d3c4b5a",
    extra_top: dict | None = None,
) -> dict:
    config = {
        "id": automation_id,
        "alias": alias,
        "description": build_provenance_description(scene_id=scene_id, behavior="apply" if command == "scene.apply" else "play"),
        "mode": "single",
        "trigger": [{"platform": "time", "at": at}],
        "action": [bridge_action(command, scene_id)],
    }
    if weekdays is not None:
        config["condition"] = [{"condition": "time", "weekday": weekdays}]
    if extra_top:
        config.update(extra_top)
    return config


def classify(config: dict, *, entity_state: str = "on", entity_id: str = "automation.evening_glow"):
    automation_id = config.get("id", "unknown")
    return classify_automation(
        config,
        automation_id=automation_id,
        entity_id=entity_id,
        entity_state=entity_state,
        alias=config.get("alias"),
    )


# ---------------------------------------------------------------------------
# supported grammar parses
# ---------------------------------------------------------------------------


def test_supported_exact_time_routine_parses():
    projection = classify(native_config())
    assert projection is not None
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.schedule == RoutineSchedule(time="19:30", weekdays=None)
    assert projection.scene_id == "evening_glow"
    assert projection.behavior == "apply"
    assert projection.enabled is True
    assert projection.entity_id == "automation.evening_glow"
    assert projection.automation_id == "ssr_0f1e2d3c4b5a"
    assert projection.unsupported_reasons == []


def test_weekday_schedule_parses():
    projection = classify(native_config(weekdays=["mon", "tue", "wed", "thu", "fri"]))
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.schedule == RoutineSchedule(time="19:30", weekdays=("mon", "tue", "wed", "thu", "fri"))


def test_time_trigger_without_seconds_and_with_trigger_id_parses():
    config = native_config()
    config["trigger"] = [{"platform": "time", "at": "07:05", "id": "morning"}]
    projection = classify(config)
    assert projection.schedule.time == "07:05"


def test_modernized_trigger_type_key_parses():
    """HA >= 2024.8 normalizes the trigger TYPE key to `trigger` on save —
    the read-back of our own write must classify native, not advanced."""
    config = native_config()
    config["trigger"] = [{"trigger": "time", "at": "19:30:00"}]
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.schedule == RoutineSchedule(time="19:30", weekdays=None)


def test_modernized_plural_list_keys_parse():
    """Newer HA storage can return the plural list aliases."""
    config = {
        "id": "ssr_0f1e2d3c4b5a",
        "alias": "Scene Studio · Evening Glow · Weekdays 7:00 PM",
        "description": build_provenance_description(scene_id="evening_glow", behavior="apply"),
        "mode": "single",
        "triggers": [{"trigger": "time", "at": "19:30:00"}],
        "conditions": [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}],
        "actions": [bridge_action()],
    }
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.schedule == RoutineSchedule(time="19:30", weekdays=("mon", "tue", "wed", "thu", "fri"))
    assert projection.behavior == "apply"


def test_both_singular_and_plural_lists_are_advanced():
    config = native_config()
    config["triggers"] = config["trigger"]
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_ADVANCED
    assert any("both 'trigger' and 'triggers'" in reason for reason in projection.unsupported_reasons)


def test_apply_vs_play_classification():
    apply_projection = classify(native_config(command="scene.apply"))
    play_projection = classify(native_config(command="playback.start", scene_id="aurora_flow"))
    assert apply_projection.behavior == "apply"
    assert apply_projection.scene_id == "evening_glow"
    assert play_projection.behavior == "play"
    assert play_projection.scene_id == "aurora_flow"


def test_disabled_entity_state_marks_routine_disabled():
    projection = classify(native_config(), entity_state="off")
    assert projection.enabled is False


def test_unknown_entity_state_defaults_enabled():
    projection = classify(native_config(), entity_state="unavailable", entity_id=None)
    assert projection.enabled is True


# ---------------------------------------------------------------------------
# recognition is structural, not provenance-based
# ---------------------------------------------------------------------------


def test_ha_created_compatible_routine_recognized_without_provenance():
    config = {
        "id": "1729b0e4fa874123",
        "alias": "My own evening automation",
        "trigger": [{"platform": "time", "at": "20:15:00"}],
        "action": [bridge_action("playback.start", "aurora_flow")],
    }
    projection = classify(config, entity_state="on")
    assert projection is not None
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.behavior == "play"
    assert projection.scene_id == "aurora_flow"
    assert projection.provenance is None


def test_provenance_marker_round_trips():
    description = build_provenance_description(scene_id="evening_glow", behavior="apply")
    provenance = parse_provenance(description)
    assert provenance == {
        "schema": ROUTINE_SCHEMA_VERSION,
        "scene_id": "evening_glow",
        "behavior": "apply",
    }
    assert parse_provenance("An ordinary human description") is None


def test_generated_routine_round_trips_through_parser():
    config = generate_routine_config(
        automation_id="ssr_112233445566",
        scene_id="twilight",
        scene_name="Twilight",
        behavior="apply",
        schedule=RoutineSchedule(time="18:45", weekdays=("sat", "sun")),
    )
    projection = classify(config)
    assert projection is not None
    assert projection.classification == CLASSIFICATION_NATIVE
    assert projection.schedule == RoutineSchedule(time="18:45", weekdays=("sat", "sun"))
    assert projection.scene_id == "twilight"
    assert projection.behavior == "apply"
    assert projection.alias == "Scene Studio · Twilight · Weekends 6:45 PM"
    assert projection.provenance is not None and projection.provenance["scene_id"] == "twilight"


# ---------------------------------------------------------------------------
# advanced / unrelated automations
# ---------------------------------------------------------------------------


def test_advanced_automation_with_scene_action_detected_but_not_native():
    config = {
        "id": "mixed_automation",
        "alias": "Occupancy + evening scene",
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion"}],
        "condition": [],
        "action": [
            {"service": "light.turn_on", "entity_id": "light.kitchen"},
            bridge_action(),
        ],
    }
    projection = classify(config)
    assert projection is not None
    assert projection.classification == CLASSIFICATION_ADVANCED
    assert projection.scene_id == "evening_glow"
    assert projection.behavior == "apply"
    assert projection.schedule is None
    assert projection.unsupported_reasons


def test_nested_scene_action_is_advanced_but_reference_still_found():
    config = {
        "id": "choose_automation",
        "alias": "Evening with a choice",
        "trigger": [{"platform": "time", "at": "19:00:00"}],
        "action": [
            {
                "choose": [
                    {"conditions": [{"condition": "state", "entity_id": "person.x", "state": "home"}],
                     "sequence": [bridge_action()]},
                ]
            }
        ],
    }
    reference = find_bridge_reference(config)
    assert reference == ("apply", "evening_glow")
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_ADVANCED


def test_event_data_extra_keys_move_routine_to_advanced():
    config = native_config()
    config["action"] = [{
        "event": "scene_studio_ui_command",
        "event_data": {"command": "scene.apply", "scene_id": "evening_glow", "target_id": "office"},
    }]
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_ADVANCED


def test_sun_trigger_is_advanced():
    config = native_config()
    config["trigger"] = [{"platform": "sun", "event": "sunset", "offset": "00:30:00"}]
    projection = classify(config)
    assert projection.classification == CLASSIFICATION_ADVANCED
    assert any("sun" in reason for reason in projection.unsupported_reasons)


def test_nonzero_seconds_is_advanced():
    config = native_config(at="19:30:30")
    assert classify(config).classification == CLASSIFICATION_ADVANCED


def test_blueprint_or_variables_key_is_advanced():
    config = native_config(extra_top={"variables": {"who": "me"}})
    assert classify(config).classification == CLASSIFICATION_ADVANCED


def test_unrelated_ha_automation_is_ignored():
    config = {
        "id": "hall_light",
        "alias": "Hall light on motion",
        "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall"}],
        "action": [{"service": "light.turn_on", "entity_id": "light.hall"}],
    }
    assert classify(config) is None
    assert find_bridge_reference(config) is None


# ---------------------------------------------------------------------------
# rename resilience (scene-id linkage)
# ---------------------------------------------------------------------------


def test_scene_rename_does_not_break_scene_id_linkage():
    """Scene ids are immutable; renames change display names only, and HA
    automations keep referencing the scene id — so a scene rename leaves the
    stored HA config untouched and the linkage intact (the alias may keep
    the previous display name until the routine is next edited)."""
    before = classify(native_config(alias="Scene Studio · Evening Glow · Weekdays 7:00 PM"))
    # After a Scene Studio scene.rename, HA still stores the ORIGINAL config:
    after = classify(native_config(alias="Scene Studio · Evening Glow · Weekdays 7:00 PM"))
    assert before.scene_id == after.scene_id == "evening_glow"
    assert before.source_digest == after.source_digest
    assert after.classification == CLASSIFICATION_NATIVE


def test_automation_alias_rename_does_not_break_recognition():
    config = native_config()
    first = classify(config)
    config["alias"] = "Completely renamed by the user in HA"
    second = classify(config)
    assert first.classification == second.classification == CLASSIFICATION_NATIVE
    assert second.scene_id == "evening_glow"
    assert second.alias == "Completely renamed by the user in HA"
    assert first.source_digest != second.source_digest  # the digest HONESTLY moves with the edit


# ---------------------------------------------------------------------------
# digest / concurrency tokens
# ---------------------------------------------------------------------------


def test_digest_is_stable_and_normalizes_volatile_keys():
    config = native_config()
    with_trace = dict(config, trace={"configured_by": "storage"})
    with_source = dict(config, source={"user": "storage"})
    assert canonical_config_digest(config) == canonical_config_digest(config)
    assert canonical_config_digest(config) == canonical_config_digest(with_trace)
    assert canonical_config_digest(config) == canonical_config_digest(with_source)
    reordered = {key: config[key] for key in reversed(list(config))}
    assert canonical_config_digest(config) == canonical_config_digest(reordered)


def test_digest_changes_when_definition_changes():
    config = native_config()
    edited = native_config(at="20:00:00")
    assert canonical_config_digest(config) != canonical_config_digest(edited)


# ---------------------------------------------------------------------------
# labels / validation helpers
# ---------------------------------------------------------------------------


def test_schedule_labels():
    assert describe_schedule(RoutineSchedule(time="19:00")) == "Daily 7:00 PM"
    assert describe_schedule(RoutineSchedule(time="07:00", weekdays=("mon", "tue", "wed", "thu", "fri"))) == "Weekdays 7:00 AM"
    assert describe_schedule(RoutineSchedule(time="09:00", weekdays=("sat", "sun"))) == "Weekends 9:00 AM"
    assert describe_schedule(RoutineSchedule(time="22:00", weekdays=("thu",))) == "Thursdays 10:00 PM"
    assert describe_schedule(RoutineSchedule(time="12:30", weekdays=("mon", "thu"))) == "Mon, Thu 12:30 PM"


def test_build_alias_matches_the_contract_shape():
    alias = build_alias(
        scene_name="Evening Glow",
        schedule=RoutineSchedule(time="19:00", weekdays=("mon", "tue", "wed", "thu", "fri")),
    )
    assert alias == "Scene Studio · Evening Glow · Weekdays 7:00 PM"


def test_format_time_12h_boundaries():
    assert format_time_12h("00:15") == "12:15 AM"
    assert format_time_12h("12:00") == "12:00 PM"


def test_validate_routine_time():
    assert validate_routine_time("19:30", "t") == "19:30"
    with pytest.raises(ValidationError):
        validate_routine_time("19:30:00", "t")  # editor surface is HH:MM only
    with pytest.raises(ValidationError):
        validate_routine_time("25:00", "t")
    with pytest.raises(ValidationError):
        validate_routine_time("sunset", "t")


def test_validate_routine_weekdays():
    assert validate_routine_weekdays(None, "w") is None
    assert validate_routine_weekdays(["sun", "mon"], "w") == ("mon", "sun")
    with pytest.raises(ValidationError):
        validate_routine_weekdays([], "w")
    with pytest.raises(ValidationError):
        validate_routine_weekdays(["holiday"], "w")
    with pytest.raises(ValidationError):
        validate_routine_weekdays(["mon", "mon"], "w")


def test_new_automation_id_shape():
    automation_id = new_automation_id("ABCDEF012345")
    assert automation_id == "ssr_abcdef012345"
    assert automation_id == validate_automation_id(automation_id)
