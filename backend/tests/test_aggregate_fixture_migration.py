"""Dry-run-first aggregate-fixture scene migration."""

import pytest

from scene_studio.domain.scenes import FixtureState
from scene_studio.migration.aggregate_fixture import (
    apply_aggregate_fixture_plan,
    plan_aggregate_fixture_removal,
)
from scene_studio.stores import SceneStudioStore


def _store(tmp_path, *, scenes=None, extra_fixtures=None):
    store = SceneStudioStore(tmp_path)
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    store.fixtures.add_target({"id": "office", "name": "Office"})
    store.fixtures.add_target({"id": "whole_house", "name": "Whole House"})
    store.fixtures.add_fixture(
        {
            "id": "lamp",
            "name": "Lamp",
            "groups": ["studio", "whole_house"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    store.fixtures.add_fixture(
        {
            "id": "g_strip",
            "name": "G Strip",
            "groups": ["studio", "whole_house"],
            "binding": {"provider": "hue_v2", "bridge_id": "b", "resource_id": "r1", "ha_entity_id": "light.hue_g_strip"},
            "capabilities": {"on_off": True, "brightness": True, "color_xy": True},
        }
    )
    store.fixtures.add_fixture(
        {
            "id": "office_lights",
            "name": "Office Lights",
            "groups": ["office", "whole_house"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.office_lights"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    for item in extra_fixtures or []:
        store.fixtures.add_fixture(item)
    for scene in scenes or []:
        store.scenes.add_scene(scene)
    return store


def test_redundant_override_is_removed_not_expanded(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "plain",
                "name": "Plain",
                "target_ids": ["studio"],
                "default_state": {"on": True, "brightness": 40.0},
                "fixture_states": {"office_lights": {"on": True, "brightness": 40.0}, "lamp": {"on": True, "color": "#ff0000"}},
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.lamp", "light.hue_g_strip"]
    )
    scene_plan = plan["scenes"][0]
    assert scene_plan["removed_override"] == {"on": True, "brightness": 40.0}
    assert "office_lights" not in scene_plan["after_fixture_states"]
    assert scene_plan["after_fixture_states"]["lamp"]["color"] == "#ff0000"
    assert "g_strip" not in scene_plan["after_fixture_states"]  # redundant, not expanded
    assert plan["safe_to_apply"] is True


def test_unique_intent_expands_only_where_child_has_no_override(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "meeting_blue",
                "name": "Meeting Blue",
                "target_ids": ["studio"],
                "fixture_states": {
                    "lamp": {"on": True, "color": "#2962ff"},
                    "office_lights": {"on": True, "brightness": 80.0, "color_temp_mirek": 220},
                },
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.lamp", "light.hue_g_strip"]
    )
    scene_plan = plan["scenes"][0]
    assert scene_plan["expanded_onto"] == ["g_strip"]
    assert plan["safe_to_apply"] is True
    assert scene_plan["after_fixture_states"]["lamp"] == {"on": True, "color": "#2962ff"}
    assert scene_plan["after_fixture_states"]["g_strip"] == {"on": True, "brightness": 80.0, "color_temp_mirek": 220}
    assert "office_lights" not in scene_plan["after_fixture_states"]


def test_apply_removes_aggregate_and_does_not_duplicate_whole_house_members(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "wind_down",
                "name": "Wind down",
                "target_ids": ["office", "studio"],
                "fixture_states": {"lamp": {"on": False}},
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.lamp", "light.hue_g_strip"]
    )
    assert plan["safe_to_apply"] is True
    assert "office" in plan["retire_targets"]
    result = apply_aggregate_fixture_plan(store, plan)
    assert result["removed_fixture"] == "office_lights"
    assert "office" in result["retired_targets"]
    ids = {fixture.id for fixture in store.fixtures.list_fixtures()}
    assert "office_lights" not in ids
    studio = [fixture.id for fixture in store.resolve_target("studio")]
    whole = [fixture.id for fixture in store.resolve_target("whole_house")]
    assert studio == ["g_strip", "lamp"]
    assert whole == ["g_strip", "lamp"]
    assert store.scenes.get_scene("wind_down").target_ids == ["studio"]


def test_unique_intent_unassigned_when_every_child_already_has_override(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "meeting_blue",
                "name": "Meeting Blue",
                "target_ids": ["office"],
                "fixture_states": {
                    "lamp": {"on": True, "color": "#111111"},
                    "g_strip": {"on": True, "color": "#222222"},
                    "office_lights": {"on": True, "brightness": 80.0},
                },
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.lamp", "light.hue_g_strip"]
    )
    assert plan["scenes"][0]["unique_intent_unassigned"] is True
    assert plan["safe_to_apply"] is False
    assert any("unique aggregate intent could not be assigned" in item for item in plan["warnings"])
    assert "office_lights" not in plan["scenes"][0]["after_fixture_states"]
    assert FixtureState.from_dict(plan["scenes"][0]["after_fixture_states"]["lamp"]).color == "#111111"
    with pytest.raises(ValueError, match="not safe to apply"):
        apply_aggregate_fixture_plan(store, plan)
    assert "office_lights" in {fixture.id for fixture in store.fixtures.list_fixtures()}
    assert "office_lights" in store.scenes.get_scene("meeting_blue").fixture_states


def test_unique_override_with_no_children_is_unsafe(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "meeting_blue",
                "name": "Meeting Blue",
                "target_ids": ["studio"],
                "fixture_states": {"office_lights": {"on": True, "brightness": 80.0}},
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.unknown_member"]
    )
    assert plan["child_ids"] == []
    assert plan["scenes"][0]["unique_intent_unassigned"] is True
    assert plan["safe_to_apply"] is False
    assert any("no child fixtures inferred" in item for item in plan["warnings"])


def test_last_empty_active_target_is_unsafe(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "office_only",
                "name": "Office Only",
                "target_ids": ["office"],
                "fixture_states": {"lamp": {"on": True}},
            }
        ],
    )
    plan = plan_aggregate_fixture_removal(
        store, "office_lights", ha_member_entity_ids=["light.lamp", "light.hue_g_strip"]
    )
    assert plan["scenes"][0].get("kept_last_empty_target") is True
    assert plan["safe_to_apply"] is False
    assert any("empty" in item for item in plan["warnings"])
    with pytest.raises(ValueError, match="not safe to apply"):
        apply_aggregate_fixture_plan(store, plan)
    assert store.scenes.get_scene("office_only").target_ids == ["office"]


def test_cli_apply_refuses_unsafe_plan_before_writes(tmp_path):
    store = _store(
        tmp_path,
        scenes=[
            {
                "schema_version": 2,
                "id": "meeting_blue",
                "name": "Meeting Blue",
                "target_ids": ["office"],
                "fixture_states": {
                    "lamp": {"on": True, "color": "#111111"},
                    "g_strip": {"on": True, "color": "#222222"},
                    "office_lights": {"on": True, "brightness": 80.0},
                },
            }
        ],
    )
    from scene_studio.migration.aggregate_cli import main

    rc = main(
        [
            str(tmp_path),
            "--fixture-id",
            "office_lights",
            "--ha-member",
            "light.lamp",
            "--ha-member",
            "light.hue_g_strip",
            "--apply",
        ]
    )
    assert rc == 1
    reread = SceneStudioStore(tmp_path)
    assert "office_lights" in {fixture.id for fixture in reread.fixtures.list_fixtures()}
    assert "office_lights" in reread.scenes.get_scene("meeting_blue").fixture_states
