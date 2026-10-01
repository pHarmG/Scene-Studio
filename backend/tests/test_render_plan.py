"""Tests for the shared scene -> RenderPlan builder (contracts §3).

Uses the sample registry/scenes as the seed dataset. Dry-run only: the
builder must never contact devices (it only emits operation plans).
"""

import copy

import pytest

from scene_studio import FixtureRegistry, Scene
from scene_studio.domain.fidelity import FidelityLevel, RenderPlan
from scene_studio.renderers import build_render_plan, hex_to_xy

PALETTE_0_XY = {"x": 0.1419, "y": 0.3085}  # hex_to_xy("#00e5ff"), aurora palette head


@pytest.fixture()
def registry(sample_registry_data) -> FixtureRegistry:
    return FixtureRegistry.from_dict(sample_registry_data)


@pytest.fixture()
def scenes(sample_scenes_data) -> dict[str, Scene]:
    return {scene.id: scene for scene in (Scene.from_dict(data) for data in sample_scenes_data["scenes"])}


def _plan_ids(plan):
    return [fixture_plan.fixture_id for fixture_plan in plan.fixture_plans]


def _notes_contain(plan, fragment):
    return any(fragment in note for note in plan.notes)


# ---------------------------------------------------------------------------
# twilight: static scene on the studio target
# ---------------------------------------------------------------------------


def test_twilight_plans_every_stateful_studio_fixture(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry)

    assert plan.scene_id == "twilight"
    assert plan.target_ids == ["studio"]
    # 14 studio fixtures: office_strip now belongs to studio; custom_gradient
    # has no state and twilight has no default_state; double_strip is disabled
    # -> 12 planned fixtures.
    assert _plan_ids(plan) == sorted(
        [
            "g_strip",
            "middle_bar",
            "lamp",
            "lower_bar",
            "upper_bar",
            "office_strip",
            "wled_seg_0",
            "wled_seg_1",
            "wled_seg_2",
            "wled_seg_3",
            "wled_seg_4",
            "wled_seg_5",
        ]
    )
    assert plan.skipped_fixture_ids == ["double_strip"]
    joined_notes = "\n".join(plan.notes)
    assert _notes_contain(plan, "custom_gradient")  # no fixture state, no default_state
    assert _notes_contain(plan, "double_strip") and _notes_contain(plan, "disabled")
    assert not _notes_contain(plan, "office_strip") or "not in resolved targets" not in joined_notes


def test_twilight_hue_gradient_plan_is_native_with_ordered_points(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry)
    g_strip = next(fixture_plan for fixture_plan in plan.fixture_plans if fixture_plan.fixture_id == "g_strip")

    assert g_strip.provider == "hue_v2"
    assert g_strip.fidelity is FidelityLevel.NATIVE
    assert len(g_strip.operations) == 1
    operation = g_strip.operations[0]
    assert operation.op == "hue.put_light"
    assert operation.resource_ref == "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
    gradient = operation.payload["gradient"]
    assert gradient["mode"] == "interpolated_palette"
    scene_gradient = scenes["twilight"].fixture_states["g_strip"].gradient
    assert [point["color"]["xy"] for point in gradient["points"]] == [
        dict(zip(("x", "y"), hex_to_xy(color))) for color in scene_gradient
    ]


def test_twilight_static_color_payload_is_exact(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry)
    middle_bar = next(fixture_plan for fixture_plan in plan.fixture_plans if fixture_plan.fixture_id == "middle_bar")

    assert middle_bar.provider == "hue_v2"
    assert middle_bar.fidelity is FidelityLevel.EQUIVALENT
    assert middle_bar.operations[0].payload == {
        "on": {"on": True},
        "dimming": {"brightness": 50.0},
        "color": {"xy": {"x": 0.2149, "y": 0.0722}},
    }


def test_twilight_wled_fixtures_are_planned_by_the_wled_renderer(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry)
    wled_plans = [fixture_plan for fixture_plan in plan.fixture_plans if fixture_plan.fixture_id.startswith("wled_")]

    assert len(wled_plans) == 6
    for fixture_plan in wled_plans:
        assert fixture_plan.provider == "wled"
        assert fixture_plan.operations, "wled fixtures must carry at least one operation"


# ---------------------------------------------------------------------------
# aurora_flow: dynamic scene with default_state
# ---------------------------------------------------------------------------


def test_aurora_dynamic_palette_on_g_strip_routes_through_scene_mechanism(scenes, registry):
    # R5D corrective pass: Hue dynamic state is a scene-resource feature.
    # Even a dynamic-native gradient fixture carries its static state as the
    # scene action (light PUT, no dynamics) plus the managed-scene marker;
    # the bridge rejects per-light dynamics.status writes outright.
    scene = scenes["aurora_flow"]
    plan = build_render_plan(scene, registry)
    g_strip = next(fixture_plan for fixture_plan in plan.fixture_plans if fixture_plan.fixture_id == "g_strip")

    assert g_strip.provider == "hue_v2"
    assert g_strip.fidelity is FidelityLevel.NATIVE
    assert len(g_strip.operations) == 2
    static_op, scene_op = g_strip.operations
    assert static_op.op == "hue.put_light"
    assert static_op.resource_ref == "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
    assert "dynamics" not in static_op.payload
    # the state's own gradient (3 colors) is rendered into the action, not the scene palette
    scene_gradient = scenes["aurora_flow"].fixture_states["g_strip"].gradient
    assert [point["color"]["xy"] for point in static_op.payload["gradient"]["points"]] == [
        dict(zip(("x", "y"), hex_to_xy(color))) for color in scene_gradient
    ]
    assert scene_op.op == "hue.put_scene_dynamic"
    assert scene_op.payload == {"speed": scene.motion.speed, "palette": list(scene.palette)}
    assert scene_op.payload["speed"] == 0.4


def test_aurora_default_state_applied_to_non_overridden_fixtures(scenes, registry):
    plan = build_render_plan(scenes["aurora_flow"], registry)
    # custom_gradient is not dynamic_native (live-verified): default_state
    # {on: true, brightness: 60} renders as an approximate static snapshot of
    # the first palette color.
    custom_gradient = next(fp for fp in plan.fixture_plans if fp.fixture_id == "custom_gradient")
    assert custom_gradient.fidelity is FidelityLevel.APPROXIMATE
    assert custom_gradient.operations[0].payload == {
        "on": {"on": True},
        "dimming": {"brightness": 60.0},
        "color": {"xy": PALETTE_0_XY},
    }
    # middle_bar IS dynamic_native (reconciled from live 2026-09-11): native
    # bridge-scene dynamic execution instead of the static snapshot.
    middle_bar = next(fp for fp in plan.fixture_plans if fp.fixture_id == "middle_bar")
    assert middle_bar.fidelity is FidelityLevel.NATIVE


def test_aurora_degraded_fixture_is_planned_and_disabled_fixture_skipped(scenes, registry):
    plan = build_render_plan(scenes["aurora_flow"], registry)

    # degraded is not disabled/missing: custom_gradient still gets a plan.
    assert "custom_gradient" in _plan_ids(plan)
    assert plan.skipped_fixture_ids == ["double_strip"]
    assert plan.notes == ["skipped fixture 'double_strip': disabled"]


def test_unknown_fixture_state_is_skipped_with_note(scenes, registry, sample_scenes_data):
    data = next(entry for entry in sample_scenes_data["scenes"] if entry["id"] == "aurora_flow")
    data["fixture_states"]["ghost_light"] = {"on": True}
    scene = Scene.from_dict(data)

    plan = build_render_plan(scene, registry)

    assert "ghost_light" not in _plan_ids(plan)
    assert "ghost_light" not in plan.skipped_fixture_ids
    assert "fixture_states entry for unknown fixture id 'ghost_light' ignored" in plan.notes


# ---------------------------------------------------------------------------
# target restriction, unbound/missing skips, determinism
# ---------------------------------------------------------------------------


def test_target_override_restricts_to_a_single_fixture(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry, target_ids=["g_strip"])

    assert plan.target_ids == ["g_strip"]
    assert _plan_ids(plan) == ["g_strip"]


def test_unknown_override_target_records_note_and_plans_nothing(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry, target_ids=["nope"])

    assert plan.fixture_plans == []
    assert _notes_contain(plan, "target 'nope' did not resolve to any fixture")


def test_unbound_and_missing_fixtures_are_skipped_with_notes(scenes, registry, sample_registry_data):
    data = {
        **sample_registry_data,
        "fixtures": [
            *sample_registry_data["fixtures"],
            {"id": "spare_bulb", "name": "Spare Bulb", "groups": ["studio"]},  # no binding -> unbound
            {
                "id": "ghost_bulb",
                "name": "Ghost Bulb",
                "groups": ["studio"],
                "binding": {"provider": "hue_v2", "bridge_id": "001788demo000001", "resource_id": "ghost"},
                "health": "missing",
            },
        ],
    }
    plan = build_render_plan(scenes["twilight"], FixtureRegistry.from_dict(data))

    assert plan.skipped_fixture_ids == ["double_strip", "ghost_bulb", "spare_bulb"]
    assert _notes_contain(plan, "skipped fixture 'spare_bulb': unbound")
    assert _notes_contain(plan, "skipped fixture 'ghost_bulb': missing")


def test_render_plan_build_is_deterministic(scenes, registry):
    first = build_render_plan(scenes["twilight"], registry)
    second = build_render_plan(scenes["twilight"], registry)

    assert first == second
    assert first.to_dict() == second.to_dict()


def test_render_plan_round_trips_through_dicts(scenes, registry):
    plan = build_render_plan(scenes["aurora_flow"], registry)
    assert RenderPlan.from_dict(plan.to_dict()) == plan


def test_mixed_providers_dispatch_through_one_builder(sample_registry_data):
    registry_data = copy.deepcopy(sample_registry_data)
    registry_data["fixtures"].append(
        {
            "id": "desk_accent",
            "name": "Desk Accent",
            "groups": ["studio"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.desk_accent"},
        }
    )
    registry = FixtureRegistry.from_dict(registry_data)
    scene = Scene.from_dict(
        {
            "schema_version": 2,
            "id": "office_focus",
            "name": "Office Focus",
            "target_ids": ["studio"],
            "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
            "fixture_states": {
                "desk_accent": {"on": True, "brightness": 70.0, "color": "#2962ff"},
                "lamp": {"on": True, "brightness": 80.0, "color_temp_mirek": 220},
            },
        }
    )

    plan = build_render_plan(scene, registry)

    assert {fixture_plan.provider for fixture_plan in plan.fixture_plans} == {"hue_v2", "ha_light"}
    assert sorted(_plan_ids(plan)) == ["desk_accent", "lamp"]


def test_whole_house_sample_does_not_plan_aggregate_office_lights(scenes, registry):
    plan = build_render_plan(scenes["twilight"], registry, target_ids=["whole_house"])
    assert "office_lights" not in _plan_ids(plan)
    assert "office_lights" not in (plan.skipped_fixture_ids or [])
    assert all("office_lights" not in note for note in plan.notes)
