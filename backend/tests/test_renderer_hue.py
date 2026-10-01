"""Tests for the Hue CLIP v2 renderer (workstream B1).

All expectations are dry-run operation plans; no network access anywhere.
"""

import pytest

from scene_studio.domain.bindings import HaLightBinding, HueBinding, WledBinding
from scene_studio.domain.capabilities import Capabilities, ColorTempRange, GradientCapability
from scene_studio.domain.fixtures import Fixture
from scene_studio.domain.fidelity import FidelityLevel
from scene_studio.domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy
from scene_studio.renderers import HaLightRenderer, HueRenderer, Renderer, WledRenderer, hex_to_xy
from scene_studio.renderers.base import select_renderer

PALETTE = ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"]


def _hue_fixture(fixture_id="lamp", capabilities=None, resource_id="lamp-resource-id"):
    return Fixture(
        id=fixture_id,
        name=fixture_id.replace("_", " ").title(),
        binding=HueBinding(bridge_id="001788demo000001", resource_id=resource_id),
        capabilities=capabilities if capabilities is not None else Capabilities(),
    )


def _plan(fixture, state, motion=None, palette=None):
    return HueRenderer().plan_fixture(fixture, state, motion=motion or Motion(), palette=palette or [])


def _single_operation(plan):
    assert len(plan.operations) == 1
    return plan.operations[0]


def _xy(hex_color):
    x, y = hex_to_xy(hex_color)
    return {"x": x, "y": y}


# ---------------------------------------------------------------------------
# static intent
# ---------------------------------------------------------------------------


def test_single_rgb_color_payload_and_equivalent_fidelity():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 50.0, "color": "#ff0000"}))

    assert plan.fixture_id == "lamp"
    assert plan.provider == "hue_v2"
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    operation = _single_operation(plan)
    assert operation.op == "hue.put_light"
    assert operation.provider == "hue_v2"
    assert operation.resource_ref == "lamp-resource-id"
    assert operation.payload == {
        "on": {"on": True},
        "dimming": {"brightness": 50.0},
        "color": {"xy": {"x": 0.735, "y": 0.265}},
    }


def test_fields_absent_from_state_are_omitted():
    fixture = _hue_fixture(capabilities=Capabilities(brightness=True, color_xy=True))
    plan = _plan(fixture, FixtureState.from_dict({"on": True}))
    assert _single_operation(plan).payload == {"on": {"on": True}}


def test_gradient_static_preserves_point_order_and_is_native():
    capabilities = Capabilities(brightness=True, color_xy=True, gradient=GradientCapability(max_points=5))
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities, resource_id="strip-resource-id")
    colors = ["#1a237e", "#4527a0", "#7b1fa2"]
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 55.0, "gradient": colors}))

    assert plan.fidelity is FidelityLevel.NATIVE
    payload = _single_operation(plan).payload
    assert payload["on"] == {"on": True}
    assert payload["dimming"] == {"brightness": 55.0}
    gradient = payload["gradient"]
    assert gradient["mode"] == "interpolated_palette"
    assert [point["color"]["xy"] for point in gradient["points"]] == [_xy(color) for color in colors]


def test_gradient_points_clamp_to_capability_max_points():
    capabilities = Capabilities(brightness=True, color_xy=True, gradient=GradientCapability(max_points=5))
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities)
    colors = ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00", "#00e5ff", "#2979ff", "#00c853"]
    plan = _plan(fixture, FixtureState.from_dict({"gradient": colors}))

    points = _single_operation(plan).payload["gradient"]["points"]
    assert len(points) == 5
    assert [point["color"]["xy"] for point in points] == [_xy(color) for color in colors[:5]]
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "truncated" in plan.reason


def test_gradient_on_non_gradient_fixture_falls_back_to_first_point_as_solid_color():
    capabilities = Capabilities(brightness=True, color_xy=True)
    fixture = _hue_fixture(fixture_id="custom_gradient", capabilities=capabilities)
    plan = _plan(fixture, FixtureState.from_dict({"gradient": ["#1a237e", "#ff8f00"]}))

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "no gradient capability" in plan.reason
    payload = _single_operation(plan).payload
    assert "gradient" not in payload
    assert payload["color"] == {"xy": {"x": 0.1732, "y": 0.0686}}


def test_color_on_non_color_fixture_is_approximate_and_omitted():
    capabilities = Capabilities(on_off=True, brightness=True)  # no color_xy
    fixture = _hue_fixture(capabilities=capabilities)
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 40.0, "color": "#ff0000"}))

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "color" in plan.reason
    payload = _single_operation(plan).payload
    assert "color" not in payload
    assert payload["on"] == {"on": True}
    assert payload["dimming"] == {"brightness": 40.0}


def test_color_only_on_non_color_fixture_is_approximate_with_empty_payload():
    capabilities = Capabilities(on_off=True, brightness=True)
    fixture = _hue_fixture(capabilities=capabilities)
    plan = _plan(fixture, FixtureState.from_dict({"color": "#ff0000"}))

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert _single_operation(plan).payload == {}


def test_color_temperature_passthrough_and_clamping():
    ct_capabilities = Capabilities(
        on_off=True, brightness=True, color_temp=ColorTempRange(mirek_min=153, mirek_max=500)
    )
    plan = _plan(_hue_fixture(capabilities=ct_capabilities), FixtureState.from_dict({"color_temp_mirek": 220}))
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert _single_operation(plan).payload == {"color_temperature": {"mirek": 220}}

    clamped = _plan(_hue_fixture(capabilities=ct_capabilities), FixtureState.from_dict({"color_temp_mirek": 100}))
    assert clamped.fidelity is FidelityLevel.APPROXIMATE
    assert "clamped" in clamped.reason
    assert _single_operation(clamped).payload == {"color_temperature": {"mirek": 153}}


def test_color_temperature_on_fixture_without_ct_capability_is_approximate():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True))
    plan = _plan(fixture, FixtureState.from_dict({"color_temp_mirek": 220}))
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "color_temperature" not in _single_operation(plan).payload


def test_supported_effect_is_emitted_and_native():
    capabilities = Capabilities(on_off=True, brightness=True, effects=["no_effect", "candle"])
    plan = _plan(_hue_fixture(capabilities=capabilities), FixtureState.from_dict({"effect": "candle"}))
    assert plan.fidelity is FidelityLevel.NATIVE
    assert _single_operation(plan).payload == {"effects": {"status": "candle"}}


def test_unsupported_effect_makes_whole_plan_unsupported_without_operations():
    capabilities = Capabilities(on_off=True, brightness=True, effects=["no_effect", "candle"])
    plan = _plan(_hue_fixture(capabilities=capabilities), FixtureState.from_dict({"effect": "sunset"}))
    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []
    assert "sunset" in plan.reason


def test_effect_not_in_empty_capability_list_is_unsupported():
    plan = _plan(_hue_fixture(), FixtureState.from_dict({"effect": "candle"}))
    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []


# ---------------------------------------------------------------------------
# dynamic intent
# ---------------------------------------------------------------------------


def test_dynamic_native_without_gradient_capability_uses_bridge_scene_mechanism():
    capabilities = Capabilities(
        on_off=True, brightness=True, color_xy=True, dynamic_native=True
    )
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities, resource_id="strip-resource-id")
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 60.0}), motion=motion, palette=PALETTE)

    # No gradient capability: the palette cannot be rendered onto the light,
    # so the managed bridge-scene mechanism carries the dynamic intent.
    assert plan.fidelity is FidelityLevel.NATIVE
    assert "hue.put_scene_dynamic" in plan.reason
    assert len(plan.operations) == 2

    static_op, scene_op = plan.operations
    assert static_op.op == "hue.put_light"
    assert static_op.payload == {"on": {"on": True}, "dimming": {"brightness": 60.0}}
    assert scene_op.op == "hue.put_scene_dynamic"
    assert scene_op.resource_ref == "strip-resource-id"
    assert scene_op.payload == {"speed": 0.4, "palette": list(PALETTE)}


def test_dynamic_native_gradient_fixture_routes_through_managed_scene_mechanism():
    capabilities = Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=5),
        dynamic_native=True,
    )
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities, resource_id="strip-resource-id")
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 60.0}), motion=motion, palette=PALETTE)

    # Hue dynamic state is a scene-resource feature: even a gradient fixture
    # with a fitting palette routes through the managed scene mechanism.
    assert plan.fidelity is FidelityLevel.NATIVE
    assert "managed dynamic scene" in plan.reason
    assert len(plan.operations) == 2

    static_op, scene_op = plan.operations
    assert static_op.op == "hue.put_light"
    assert static_op.payload == {"on": {"on": True}, "dimming": {"brightness": 60.0}}
    assert "dynamics" not in static_op.payload
    assert scene_op.op == "hue.put_scene_dynamic"
    assert scene_op.resource_ref == "strip-resource-id"
    assert scene_op.payload == {"speed": 0.4, "palette": list(PALETTE)}


def test_no_hue_render_path_emits_dynamics_status_on_a_light_put():
    """Required regression: no dynamic Hue render produces a light PUT with
    dynamics.status — for any capability/gradient/palette combination."""
    combinations = [
        Capabilities(on_off=True, brightness=True, color_xy=True, dynamic_native=True),
        Capabilities(
            on_off=True, brightness=True, color_xy=True,
            gradient=GradientCapability(max_points=5), dynamic_native=True,
        ),
        Capabilities(
            on_off=True, brightness=True, color_xy=True,
            gradient=GradientCapability(max_points=2), dynamic_native=True,
        ),
        Capabilities(on_off=True, brightness=True, color_xy=True),
    ]
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    states = [
        FixtureState.from_dict({"on": True, "brightness": 60.0}),
        FixtureState.from_dict({"on": True, "gradient": ["#1a237e", "#ff8f00"]}),
        FixtureState.from_dict({"on": True, "provider_ext": {"hue_v2": {"dynamics": {"status": "dynamic_palette"}}}}),
    ]
    for capabilities in combinations:
        fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities)
        for state in states:
            for motion_variant in (motion, Motion(mode=MotionMode.EFFECT, speed=0.4, strategy=MotionStrategy.AUTO)):
                plan = _plan(fixture, state, motion=motion_variant, palette=PALETTE)
                for operation in plan.operations:
                    if operation.op == "hue.put_light":
                        assert "status" not in operation.payload.get("dynamics", {}), (
                            f"dynamics.status leaked into a light PUT for {capabilities}/{state}"
                        )


def test_dynamic_native_state_gradient_stays_in_the_static_action():
    capabilities = Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=5),
        dynamic_native=True,
    )
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    state_gradient = ["#1a237e", "#ff8f00"]
    plan = _plan(
        fixture,
        FixtureState.from_dict({"on": True, "gradient": state_gradient}),
        motion=motion,
        palette=PALETTE,
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    static_op, scene_op = plan.operations
    # the fixture state's own gradient is rendered into the scene action;
    # the scene palette does not overwrite the light gradient
    assert [point["color"]["xy"] for point in static_op.payload["gradient"]["points"]] == [
        _xy(color) for color in state_gradient
    ]
    assert "dynamics" not in static_op.payload
    assert scene_op.payload == {"speed": 0.4, "palette": list(PALETTE)}


def test_dynamic_native_scene_mechanism_regardless_of_palette_size():
    capabilities = Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=2),
        dynamic_native=True,
    )
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"on": True}), motion=motion, palette=PALETTE)  # 4 colors > 2 points

    assert plan.fidelity is FidelityLevel.NATIVE
    assert "managed dynamic scene" in plan.reason
    assert len(plan.operations) == 2
    static_op, scene_op = plan.operations
    assert static_op.op == "hue.put_light"
    assert "gradient" not in static_op.payload  # static state only
    assert scene_op.op == "hue.put_scene_dynamic"
    assert scene_op.payload == {"speed": 0.4, "palette": list(PALETTE)}


def test_ext_dynamics_status_is_stripped_but_other_ext_dynamics_merge():
    capabilities = Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=5),
        dynamic_native=True,
    )
    fixture = _hue_fixture(fixture_id="g_strip", capabilities=capabilities)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(
        fixture,
        FixtureState.from_dict({
            "on": True,
            "transition_ms": 400,
            "provider_ext": {"hue_v2": {"dynamics": {"status": "dynamic_palette", "duration": 100}}},
        }),
        motion=motion,
        palette=PALETTE,
    )

    static_op = next(op for op in plan.operations if op.op == "hue.put_light")
    # status stripped (Hue lights reject it); computed duration clamped and
    # ext duration override preserved
    assert static_op.payload["dynamics"] == {"duration": 100}
    assert any("dynamics.status ignored" in note for note in (plan.reason or "").split("; "))


def test_dynamic_native_preferred_without_native_capability_is_unsupported():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.NATIVE_PREFERRED)
    plan = _plan(fixture, FixtureState.from_dict({"on": True}), motion=motion, palette=PALETTE)

    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []
    assert "native_preferred" in plan.reason


def test_dynamic_auto_fallback_renders_static_first_palette_color():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 40.0}), motion=motion, palette=PALETTE)

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "static" in plan.reason
    payload = _single_operation(plan).payload
    assert payload["on"] == {"on": True}
    assert payload["dimming"] == {"brightness": 40.0}
    # palette_cycle renders the first palette color as a static color.
    assert payload["color"] == {"xy": _xy(PALETTE[0])}
    assert payload["color"] == {"xy": {"x": 0.1419, "y": 0.3085}}


def test_dynamic_effect_mode_fallback_renders_static_without_palette_injection():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    motion = Motion(mode=MotionMode.EFFECT, speed=0.5, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "brightness": 30.0}), motion=motion, palette=PALETTE)

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    payload = _single_operation(plan).payload
    assert "color" not in payload  # effect mode does not borrow palette colors
    assert payload["on"] == {"on": True}
    assert payload["dimming"] == {"brightness": 30.0}


def test_static_strategy_forces_plain_static_render_even_for_dynamic_mode():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.STATIC)
    plan = _plan(
        fixture,
        FixtureState.from_dict({"on": True, "brightness": 40.0, "color": "#ff8f00"}),
        motion=motion,
        palette=PALETTE,
    )

    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert plan.reason == ""
    payload = _single_operation(plan).payload
    assert payload["color"] == {"xy": {"x": 0.5996, "y": 0.3875}}


def test_state_color_wins_over_palette_injection():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True, color_xy=True))
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.4, strategy=MotionStrategy.AUTO)
    plan = _plan(fixture, FixtureState.from_dict({"color": "#7b1fa2"}), motion=motion, palette=PALETTE)
    assert _single_operation(plan).payload["color"] == {"xy": {"x": 0.3141, "y": 0.101}}


# ---------------------------------------------------------------------------
# transitions (CLIP v2 dynamics.duration)
# ---------------------------------------------------------------------------


def test_transition_ms_maps_to_dynamics_duration():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True))
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "transition_ms": 1500}))

    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert _single_operation(plan).payload == {"on": {"on": True}, "dynamics": {"duration": 1500}}


def test_transition_ms_rounds_and_clamps():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True))
    clamped = _plan(fixture, FixtureState(on=True, transition_ms=70_000))
    assert _single_operation(clamped).payload == {"on": {"on": True}, "dynamics": {"duration": 60000}}

    rounded = _plan(fixture, FixtureState(on=True, transition_ms=1494.6))
    assert _single_operation(rounded).payload == {"on": {"on": True}, "dynamics": {"duration": 1495}}


# ---------------------------------------------------------------------------
# grouped_light gating
# ---------------------------------------------------------------------------


def _grouped_light_fixture(capabilities=None) -> Fixture:
    return Fixture(
        id="studio_group",
        name="Studio Group",
        binding=HueBinding(bridge_id="001788demo000001", resource_id="group-resource-id", resource_type="grouped_light"),
        capabilities=capabilities
        if capabilities is not None
        else Capabilities(on_off=True, brightness=True, color_xy=True, effects=["no_effect", "candle"]),
    )


def test_grouped_light_omits_color_gradient_effects_with_note():
    plan = _plan(
        _grouped_light_fixture(),
        FixtureState.from_dict({"on": True, "brightness": 50.0, "color": "#ff0000", "gradient": ["#00e5ff"], "effect": "candle"}),
    )

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert (
        "grouped_light supports on/dimming/color_temperature only; set per-light states via individual fixtures"
        in plan.reason
    )
    payload = _single_operation(plan).payload
    assert payload["on"] == {"on": True}
    assert payload["dimming"] == {"brightness": 50.0}
    for field in ("color", "gradient", "effects"):
        assert field not in payload


def test_grouped_light_keeps_on_dimming_color_temperature():
    capabilities = Capabilities(
        on_off=True, brightness=True, color_temp=ColorTempRange(mirek_min=153, mirek_max=500)
    )
    plan = _plan(_grouped_light_fixture(capabilities), FixtureState.from_dict({"color_temp_mirek": 220}))

    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert _single_operation(plan).payload == {"color_temperature": {"mirek": 220}}


def test_grouped_light_known_effect_is_omitted_but_unknown_effect_still_unsupported():
    known = _plan(_grouped_light_fixture(), FixtureState.from_dict({"effect": "candle"}))
    assert known.fidelity is FidelityLevel.APPROXIMATE
    assert "effects" not in _single_operation(known).payload

    unknown = _plan(_grouped_light_fixture(), FixtureState.from_dict({"effect": "sunset"}))
    assert unknown.fidelity is FidelityLevel.UNSUPPORTED
    assert unknown.operations == []


# ---------------------------------------------------------------------------
# provider_ext["hue_v2"] escape hatch
# ---------------------------------------------------------------------------


def test_ext_gradient_mode_overrides_payload_gradient_mode():
    capabilities = Capabilities(brightness=True, color_xy=True, gradient=GradientCapability(max_points=5))
    fixture = _hue_fixture(capabilities=capabilities)
    plan = _plan(
        fixture,
        FixtureState.from_dict(
            {"gradient": ["#1a237e", "#ff8f00"], "provider_ext": {"hue_v2": {"gradient": {"mode": "repeated_linear"}}}}
        ),
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    payload = _single_operation(plan).payload
    assert payload["gradient"]["mode"] == "repeated_linear"


def test_ext_gradient_mode_unknown_mode_is_approximate_and_omitted():
    capabilities = Capabilities(brightness=True, color_xy=True, gradient=GradientCapability(max_points=5))
    fixture = _hue_fixture(capabilities=capabilities)
    plan = _plan(
        fixture,
        FixtureState.from_dict(
            {"gradient": ["#1a237e"], "provider_ext": {"hue_v2": {"gradient": {"mode": "sparkles"}}}}
        ),
    )

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "sparkles" in plan.reason
    payload = _single_operation(plan).payload
    assert payload["gradient"]["mode"] == "interpolated_palette"  # computed default kept


def test_ext_gradient_mode_without_gradient_in_payload_is_noted():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True))
    plan = _plan(
        fixture,
        FixtureState.from_dict({"on": True, "provider_ext": {"hue_v2": {"gradient": {"mode": "randomized"}}}}),
    )

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "no gradient in the payload" in plan.reason
    assert "gradient" not in _single_operation(plan).payload


def test_ext_dynamics_merges_into_dynamics_and_ext_wins():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True))
    plan = _plan(
        fixture,
        FixtureState.from_dict(
            {"on": True, "transition_ms": 1000, "provider_ext": {"hue_v2": {"dynamics": {"duration": 4321}}}}
        ),
    )

    payload = _single_operation(plan).payload
    assert payload["dynamics"] == {"duration": 4321}


def test_ext_raw_merges_top_level_and_wins_over_computed():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True, brightness=True))
    plan = _plan(
        fixture,
        FixtureState.from_dict(
            {"brightness": 50.0, "provider_ext": {"hue_v2": {"raw": {"dimming": {"brightness": 10.0}}}}}
        ),
    )

    assert _single_operation(plan).payload == {"dimming": {"brightness": 10.0}}


def test_unknown_ext_keys_are_ignored_with_approximate_note():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True))
    plan = _plan(fixture, FixtureState.from_dict({"on": True, "provider_ext": {"hue_v2": {"dynamic_scene": "native"}}}))

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "dynamic_scene" in plan.reason
    assert _single_operation(plan).payload == {"on": {"on": True}}


def test_ext_must_be_an_object():
    fixture = _hue_fixture(capabilities=Capabilities(on_off=True))
    with pytest.raises(ValueError, match="must be an object"):
        _plan(fixture, FixtureState(on=True, provider_ext={"hue_v2": ["not-an-object"]}))


# ---------------------------------------------------------------------------
# dispatch / interface
# ---------------------------------------------------------------------------


def test_base_renderer_interface_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        Renderer().plan_fixture(_hue_fixture(), FixtureState.from_dict({"on": True}), motion=Motion(), palette=[])


def test_select_renderer_dispatches_on_binding_provider():
    hue_fixture = _hue_fixture()
    assert isinstance(select_renderer(hue_fixture), HueRenderer)

    wled_fixture = Fixture(
        id="seg",
        name="Seg",
        binding=WledBinding(device_id="aabbccddeeff", segment_ids=[0]),
    )
    wled_renderer = select_renderer(wled_fixture)
    assert isinstance(wled_renderer, WledRenderer)
    assert wled_renderer.provider == "wled"

    ha_fixture = Fixture(id="ceiling", name="Ceiling", binding=HaLightBinding(ha_entity_id="light.ceiling"))
    ha_renderer = select_renderer(ha_fixture)
    assert isinstance(ha_renderer, HaLightRenderer)
    assert ha_renderer.provider == "ha_light"


def test_select_renderer_rejects_missing_binding_and_unknown_provider():
    with pytest.raises(ValueError, match="no binding"):
        select_renderer(Fixture(id="bare", name="Bare"))
    bogus = Fixture(id="odd", name="Odd", binding=WledBinding(device_id="x", provider="bogus"))
    with pytest.raises(ValueError, match="unknown provider"):
        select_renderer(bogus)


def test_hue_renderer_rejects_non_hue_binding():
    fixture = Fixture(id="seg", name="Seg", binding=WledBinding(device_id="aabbccddeeff"))
    with pytest.raises(ValueError, match="hue_v2"):
        HueRenderer().plan_fixture(fixture, FixtureState.from_dict({"on": True}), motion=Motion(), palette=[])
