"""Renderer-coverage review round: whole-pipeline assertions (contracts §3).

These tests lock the renderer fixes at the ``build_render_plan`` level, not
just per-renderer: a dynamic scene whose WLED fixture carries a discovered
effect catalog must produce a native ``fx`` operation without any
``provider_ext``, and a Hue gradient fixture with ``dynamic_native`` must
produce an executable per-light ``dynamic_palette`` operation.
"""

import pytest

from scene_studio import FixtureRegistry, Scene
from scene_studio.domain.fidelity import FidelityLevel
from scene_studio.renderers import build_render_plan

# Real WLED 0.14 effect list head (positions are fx ids).
EFFECTS_014 = [
    "Solid",
    "Blink",
    "Breathe",
    "Wipe",
    "Wipe Random",
    "Random Colors",
    "Sweep",
    "Dynamic",
    "Colorloop",
    "Rainbow",
]

PALETTE = ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"]

REGISTRY_DATA = {
    "schema_version": 1,
    "updated": "2026-09-10T00:00:00Z",
    "targets": [{"id": "studio", "name": "Studio"}],
    "fixtures": [
        {
            "id": "wled_seg",
            "name": "WLED Segment",
            "groups": ["studio"],
            "enabled": True,
            "binding": {
                "provider": "wled",
                "device_id": "aabbccddeeff",
                "segment_ids": [0],
            },
            "capabilities": {
                "on_off": True,
                "brightness": True,
                "effects": EFFECTS_014,
                "palettes": ["Default", "Party"],
                "dynamic_native": True,
            },
        },
        {
            "id": "g_strip",
            "name": "Hue G Strip",
            "groups": ["studio"],
            "enabled": True,
            "binding": {
                "provider": "hue_v2",
                "bridge_id": "001788demo000001",
                "resource_id": "strip-resource-id",
                "resource_type": "light",
            },
            "capabilities": {
                "on_off": True,
                "brightness": True,
                "color_xy": True,
                "gradient": {"max_points": 5},
                "dynamic_native": True,
            },
        },
    ],
}

SCENE_DATA = {
    "schema_version": 2,
    "id": "aurora_like",
    "name": "Aurora Like",
    "target_ids": ["studio"],
    "palette": PALETTE,
    "motion": {"mode": "palette_cycle", "speed": 0.5, "strategy": "auto"},
    "fixture_states": {
        # No provider_ext: the effect name alone must resolve to fx 9.
        "wled_seg": {"on": True, "brightness": 50.0, "effect": "Rainbow"},
        "g_strip": {"on": True, "brightness": 60.0},
    },
}


@pytest.fixture(scope="module")
def plan():
    registry = FixtureRegistry.from_dict(REGISTRY_DATA)
    scene = Scene.from_dict(SCENE_DATA)
    return build_render_plan(scene, registry)


def test_wled_dynamic_scene_resolves_effect_name_to_native_fx(plan):
    wled_plan = next(p for p in plan.fixture_plans if p.fixture_id == "wled_seg")

    assert wled_plan.provider == "wled"
    assert wled_plan.fidelity is FidelityLevel.NATIVE
    assert len(wled_plan.operations) == 1

    operation = wled_plan.operations[0]
    assert operation.op == "wled.post_state"
    assert operation.resource_ref == "aabbccddeeff:seg:0"
    payload = operation.payload
    # native fx without any provider_ext: brightness stays per-segment;
    # palette auto-spread supplies the segment color.
    assert payload == {
        "seg": [{"id": 0, "on": True, "bri": 128, "col": [[0, 229, 255]], "frz": False, "fx": 9, "sx": 128}],
    }


def test_hue_gradient_fixture_routes_dynamic_intent_through_scene_mechanism(plan):
    hue_plan = next(p for p in plan.fixture_plans if p.fixture_id == "g_strip")

    assert hue_plan.provider == "hue_v2"
    assert hue_plan.fidelity is FidelityLevel.NATIVE
    assert len(hue_plan.operations) == 2

    static_op, scene_op = hue_plan.operations
    # static state (the scene action contribution) applies via light PUT;
    # dynamic intent is carried by the managed-scene marker op
    assert static_op.op == "hue.put_light"
    assert static_op.resource_ref == "strip-resource-id"
    assert "dynamics" not in static_op.payload
    assert static_op.payload["on"] == {"on": True}
    assert static_op.payload["dimming"] == {"brightness": 60.0}
    assert scene_op.op == "hue.put_scene_dynamic"
    assert scene_op.payload == {"speed": 0.5, "palette": list(PALETTE)}


def test_pipeline_plan_is_deterministic_and_round_trips(plan):
    registry = FixtureRegistry.from_dict(REGISTRY_DATA)
    scene = Scene.from_dict(SCENE_DATA)
    assert build_render_plan(scene, registry) == plan
