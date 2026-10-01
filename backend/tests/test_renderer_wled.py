"""WLED renderer tests — dry-run plans only, no network, recorded sample data."""

import copy
import json

import pytest

from scene_studio.domain.capabilities import Capabilities
from scene_studio.domain.fixtures import Fixture
from scene_studio.domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy
from scene_studio.domain.fidelity import FidelityLevel
from scene_studio.domain.bindings import WledBinding
from scene_studio.renderers.wled import WledRenderer

DEVICE_ID = "aabbccddeeff"  # real controller identity (R2 reconciliation)

# Real WLED 0.14 effect list head (Solid..Rainbow Runner); positions are fx ids.
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
    "Scan",
    "Scan Dual",
    "Fade",
    "Theater",
    "Theater Rainbow",
    "Running",
    "Saw",
    "Twinkle",
    "Dissolve",
    "Dissolve Rnd",
    "Sparkle",
    "Sparkle Dark",
    "Sparkle+",
    "Strobe",
    "Strobe Rainbow",
    "Strobe Mega",
    "Blink Rainbow",
    "Android",
    "Chase",
    "Chase Random",
    "Chase Rainbow",
    "Chase Flash",
    "Chase Flash Rnd",
    "Rainbow Runner",
]

# Real WLED 0.14 palette list head; positions are pal ids.
PALETTES_014 = [
    "Default",
    "* Random Cycle",
    "* Color 1",
    "* Colors 1&2",
    "* Color Gradient",
    "* Colors Only",
    "Party",
    "Cloud",
    "Lava",
    "Ocean",
]


def _fixture_from_sample(sample_registry_data, fixture_id="wled_seg_0", **overrides):
    """Load a sample registry fixture, applying shallow surgery on top."""
    raw = None
    for item in sample_registry_data["fixtures"]:
        if item["id"] == fixture_id:
            raw = copy.deepcopy(item)
            break
    assert raw is not None, f"{fixture_id} missing from sample registry"
    raw.update(copy.deepcopy(overrides))
    return Fixture.from_dict(raw)


def _capabilities_fixture(segment_ids=(0,), capabilities=None):
    """A WLED fixture built directly, for capability-driven resolution tests."""
    return Fixture(
        id="wled_fx",
        name="WLED FX",
        binding=WledBinding(device_id=DEVICE_ID, segment_ids=list(segment_ids)),
        capabilities=capabilities
        if capabilities is not None
        else Capabilities(on_off=True, brightness=True, dynamic_native=True),
    )


def _whole_device_fixture(sample_registry_data):
    return _fixture_from_sample(
        sample_registry_data,
        binding={"provider": "wled", "device_id": DEVICE_ID, "segment_ids": []},
    )


def _multi_segment_fixture(sample_registry_data):
    return _fixture_from_sample(
        sample_registry_data,
        binding={"provider": "wled", "device_id": DEVICE_ID, "segment_ids": [0, 2, 4]},
    )


def _state(**fields) -> FixtureState:
    return FixtureState.from_dict(fields)


def _static_motion() -> Motion:
    return Motion(mode=MotionMode.STATIC, speed=0.0, strategy=MotionStrategy.AUTO)


def _plan(renderer, fixture, state, motion=None, palette=None):
    return renderer.plan_fixture(
        fixture,
        state,
        motion=motion or _static_motion(),
        palette=palette or [],
    )


@pytest.fixture()
def renderer() -> WledRenderer:
    return WledRenderer()


def test_single_segment_static_color_brightness_exact_payload(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)  # wled_seg_0 -> segment 0
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, brightness=50.0, color="#1a237e"),
    )

    assert plan.fixture_id == "wled_seg_0"
    assert plan.provider == "wled"
    assert plan.fidelity is FidelityLevel.NATIVE
    assert len(plan.operations) == 1

    operation = plan.operations[0]
    assert operation.op == "wled.post_state"
    assert operation.provider == "wled"
    assert operation.resource_ref == f"{DEVICE_ID}:seg:0"
    # 50.0 * 2.55 == 127.5 -> 128; #1a237e -> (26, 35, 126)
    # segment-scoped bindings write per-segment bri: a top-level bri would
    # dim every device segment, including segments owned by other fixtures.
    assert operation.payload == {
        "seg": [{"id": 0, "on": True, "bri": 128, "frz": False, "col": [[26, 35, 126]]}],
    }
    assert "bri" not in operation.payload  # never top-level for segment bindings


def test_operations_never_carry_an_endpoint(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)  # endpoint_hint http://wled.local
    plan = _plan(renderer, fixture, _state(on=True, color="#ffffff"))
    encoded = json.dumps(plan.to_dict())
    assert "wled.local" not in encoded
    assert "http" not in encoded


def test_multi_segment_binding_merges_into_one_op(renderer, sample_registry_data):
    fixture = _multi_segment_fixture(sample_registry_data)
    plan = _plan(renderer, fixture, _state(on=True, brightness=60.0, color="#ff8f00"))

    assert plan.fidelity is FidelityLevel.NATIVE
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.resource_ref == f"{DEVICE_ID}:seg:0,2,4"
    assert "bri" not in operation.payload  # per-segment bri only
    assert [entry["id"] for entry in operation.payload["seg"]] == [0, 2, 4]
    for entry in operation.payload["seg"]:
        assert entry["on"] is True
        assert entry["bri"] == 153  # 60.0 * 2.55 = 153.0
        assert entry["col"] == [[255, 143, 0]]


def test_whole_device_binding_targets_device(renderer, sample_registry_data):
    fixture = _whole_device_fixture(sample_registry_data)
    plan = _plan(renderer, fixture, _state(on=True, brightness=50.0))

    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert plan.reason  # non-native plans carry a reason
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.resource_ref == f"{DEVICE_ID}:dev"
    # Device-level on/bri; no seg entries needed when nothing is per-segment.
    assert operation.payload == {"on": True, "bri": 128}


def test_on_off_placement_follows_binding_scope(renderer, sample_registry_data):
    segment_fixture = _fixture_from_sample(sample_registry_data)
    plan = _plan(renderer, segment_fixture, _state(on=False))
    assert plan.fidelity is FidelityLevel.NATIVE
    assert plan.operations[0].payload == {"seg": [{"id": 0, "on": False, "frz": False}]}

    whole_fixture = _whole_device_fixture(sample_registry_data)
    plan = _plan(renderer, whole_fixture, _state(on=False))
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert plan.operations[0].payload == {"on": False}


def test_gradient_is_approximate_using_first_color(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, brightness=50.0, gradient=["#1a237e", "#4527a0", "#ff8f00"]),
    )

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "gradient" in plan.reason.lower()
    operation = plan.operations[0]
    assert operation.payload["seg"][0]["col"] == [[26, 35, 126]]


def test_color_temp_is_approximate_and_omitted(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    plan = _plan(renderer, fixture, _state(on=True, brightness=50.0, color_temp_mirek=350))

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "color_temp" in plan.reason.lower()
    payload = plan.operations[0].payload
    assert set(payload) == {"seg"}
    assert set(payload["seg"][0]) == {"id", "on", "bri", "frz"}


def test_color_temp_maps_to_native_segment_cct_when_capability_reports_it(renderer):
    fixture = _capabilities_fixture(capabilities=Capabilities(on_off=True, brightness=True, cct=True))
    plan = _plan(renderer, fixture, _state(on=True, brightness=50.0, color_temp_mirek=200))

    assert plan.fidelity is FidelityLevel.NATIVE
    assert "cct" in plan.reason.lower()
    segment = plan.operations[0].payload["seg"][0]
    # mirek 200 -> 5000 K -> round((5000 - 1900) / (10091 - 1900) * 255) == 97
    assert segment["cct"] == 97


def test_provider_ext_fx_runs_native_dynamic(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)  # dynamic_native: true
    state = _state(
        on=True,
        brightness=50.0,
        provider_ext={"wled": {"fx": 45, "pal": 3}},
    )
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.AUTO)
    plan = _plan(
        renderer,
        fixture,
        state,
        motion=motion,
        palette=["#1a237e", "#ff8f00"],
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    operation = plan.operations[0]
    segment = operation.payload["seg"][0]
    assert segment["fx"] == 45
    assert segment["sx"] == 128  # round(0.5 * 255) = 127.5 -> 128
    assert segment["pal"] == 3  # pal only via provider_ext, never from palette hexes
    # scene palette colors are not mapped into the payload
    assert "[[26, 35, 126]]" not in json.dumps(operation.payload)


def test_palette_cycle_native_preferred_without_fx_is_unsupported(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.NATIVE_PREFERRED)
    plan = _plan(renderer, fixture, _state(on=True, brightness=50.0), motion=motion)

    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.reason
    payload = plan.operations[0].payload
    assert "fx" not in payload["seg"][0]
    assert "sx" not in payload["seg"][0]


def test_palette_cycle_auto_without_fx_approximates_first_palette_color(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.AUTO)
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, brightness=40.0),  # no fixture color -> palette fills in
        motion=motion,
        palette=["#4527a0", "#ff8f00"],
    )

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "palette" in plan.reason.lower()
    segment = plan.operations[0].payload["seg"][0]
    assert segment["col"] == [[69, 39, 160]]  # #4527a0
    assert "fx" not in segment


def test_palette_cycle_auto_without_fx_and_empty_palette_still_approximates(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.AUTO)
    plan = _plan(renderer, fixture, _state(on=True), motion=motion, palette=[])

    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert plan.reason


def test_motion_static_strategy_forces_static_render(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.9, strategy=MotionStrategy.STATIC)
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, brightness=50.0, color="#7b1fa2"),
        motion=motion,
        palette=["#ff8f00"],
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    segment = plan.operations[0].payload["seg"][0]
    assert segment["col"] == [[123, 31, 162]]  # state color, not palette fallback
    assert "fx" not in segment
    assert "sx" not in segment


def test_static_effect_with_explicit_fx_is_native(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    plan = _plan(
        renderer,
        fixture,
        _state(effect="Rainbow", provider_ext={"wled": {"fx": 9, "ix": 128}}),
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    segment = plan.operations[0].payload["seg"][0]
    assert segment["fx"] == 9
    assert segment["ix"] == 128


def test_sample_effect_name_not_in_live_catalog_is_unsupported(renderer, sample_registry_data):
    # The reconciled sample fixture carries the live 220-effect catalog;
    # a name outside it stays unsupported even with the catalog present.
    fixture = _fixture_from_sample(sample_registry_data)
    plan = _plan(renderer, fixture, _state(effect="Definitely Not An Effect"))

    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []


def test_non_wled_binding_raises_valueerror(renderer, sample_registry_data):
    hue_fixture = _fixture_from_sample(sample_registry_data, fixture_id="g_strip")  # hue_v2
    with pytest.raises(ValueError):
        _plan(renderer, hue_fixture, _state(on=True))

    unbound = _fixture_from_sample(sample_registry_data, binding=None)
    with pytest.raises(ValueError):
        _plan(renderer, unbound, _state(on=True))


def test_plan_is_deterministic(renderer, sample_registry_data):
    fixture = _fixture_from_sample(sample_registry_data)
    state = _state(on=True, brightness=50.0, color="#1a237e")
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.AUTO)
    palette = ["#1a237e", "#ff8f00"]

    first = renderer.plan_fixture(fixture, state, motion=motion, palette=palette)
    second = WledRenderer().plan_fixture(fixture, state, motion=motion, palette=palette)

    assert first == second
    assert first.to_dict() == second.to_dict()


# ---------------------------------------------------------------------------
# effect + palette name resolution via discovered capabilities
# ---------------------------------------------------------------------------


def test_effect_name_resolves_via_discovered_capabilities(renderer):
    fixture = _capabilities_fixture(capabilities=Capabilities(on_off=True, effects=EFFECTS_014))
    plan = _plan(renderer, fixture, _state(effect="Rainbow"))

    assert plan.fidelity is FidelityLevel.NATIVE
    assert "resolved" in plan.reason
    segment = plan.operations[0].payload["seg"][0]
    assert segment["fx"] == 9  # list position in the 0.14 catalog


def test_effect_name_resolution_is_case_insensitive(renderer):
    fixture = _capabilities_fixture(capabilities=Capabilities(on_off=True, effects=EFFECTS_014))
    plan = _plan(renderer, fixture, _state(effect="rainbow"))
    assert plan.fidelity is FidelityLevel.NATIVE
    assert plan.operations[0].payload["seg"][0]["fx"] == 9


def test_effect_name_not_in_capabilities_is_unsupported(renderer):
    fixture = _capabilities_fixture(capabilities=Capabilities(on_off=True, effects=["Solid", "Blink"]))
    plan = _plan(renderer, fixture, _state(effect="Rainbow"))

    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []


def test_explicit_ext_fx_wins_over_resolved_name(renderer):
    fixture = _capabilities_fixture(capabilities=Capabilities(on_off=True, effects=EFFECTS_014))
    plan = _plan(renderer, fixture, _state(effect="Rainbow", provider_ext={"wled": {"fx": 45}}))

    assert plan.fidelity is FidelityLevel.NATIVE
    assert "explicit provider_ext fx" in plan.reason
    assert plan.operations[0].payload["seg"][0]["fx"] == 45


def test_palette_name_resolves_via_discovered_capabilities(renderer):
    fixture = _capabilities_fixture(
        capabilities=Capabilities(on_off=True, effects=EFFECTS_014, palettes=PALETTES_014)
    )
    plan = _plan(renderer, fixture, _state(color="#ffffff", provider_ext={"wled": {"pal": "Party"}}))

    assert plan.fidelity is FidelityLevel.NATIVE
    segment = plan.operations[0].payload["seg"][0]
    assert segment["pal"] == 6  # Party at position 6 in the 0.14 catalog


def test_palette_numeric_passthrough_ignores_catalog(renderer):
    fixture = _capabilities_fixture(
        capabilities=Capabilities(on_off=True, effects=EFFECTS_014, palettes=PALETTES_014)
    )
    plan = _plan(renderer, fixture, _state(color="#ffffff", provider_ext={"wled": {"pal": 2}}))

    segment = plan.operations[0].payload["seg"][0]
    assert segment["pal"] == 2


def test_unknown_palette_name_is_unsupported(renderer):
    fixture = _capabilities_fixture(
        capabilities=Capabilities(on_off=True, effects=EFFECTS_014, palettes=PALETTES_014)
    )
    plan = _plan(renderer, fixture, _state(color="#ffffff", provider_ext={"wled": {"pal": "Nope"}}))

    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []


def test_palette_cycle_resolves_state_effect_name_natively_without_ext(renderer):
    fixture = _capabilities_fixture(
        capabilities=Capabilities(on_off=True, brightness=True, effects=EFFECTS_014, dynamic_native=True)
    )
    motion = Motion(mode=MotionMode.PALETTE_CYCLE, speed=0.5, strategy=MotionStrategy.AUTO)
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, brightness=50.0, effect="Rainbow"),
        motion=motion,
        palette=["#1a237e", "#ff8f00"],
    )

    assert plan.fidelity is FidelityLevel.NATIVE
    segment = plan.operations[0].payload["seg"][0]
    assert segment["fx"] == 9
    assert segment["sx"] == 128  # round(0.5 * 255)


# ---------------------------------------------------------------------------
# transitions (top-level, 100 ms units)
# ---------------------------------------------------------------------------


def test_transition_ms_maps_to_top_level_100ms_units(renderer):
    fixture = _capabilities_fixture()
    plan = _plan(renderer, fixture, _state(on=True, transition_ms=1500))

    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert "100 ms" in plan.reason
    payload = plan.operations[0].payload
    assert payload["transition"] == 15


def test_transition_ms_clamps_to_wled_range(renderer):
    # Direct construction: FixtureState.from_dict caps transition_ms at 60000,
    # but the renderer still clamps defensively for programmatic states.
    fixture = _capabilities_fixture()
    plan = _plan(renderer, fixture, FixtureState(on=True, transition_ms=7_000_000))
    assert plan.operations[0].payload["transition"] == 65535


# ---------------------------------------------------------------------------
# provider_ext["wled"].top escape hatch
# ---------------------------------------------------------------------------


def test_ext_top_merges_into_top_level_state(renderer):
    fixture = _capabilities_fixture()
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, provider_ext={"wled": {"top": {"ps": 2}}}),
    )

    payload = plan.operations[0].payload
    assert payload["ps"] == 2  # native preset recall
    assert payload["seg"][0]["on"] is True
    assert "top" not in payload["seg"][0]


def test_ext_top_wins_over_computed_keys(renderer):
    fixture = _capabilities_fixture()
    plan = _plan(
        renderer,
        fixture,
        _state(on=True, transition_ms=1500, provider_ext={"wled": {"top": {"transition": 4}}}),
    )

    assert plan.operations[0].payload["transition"] == 4


def test_ext_top_unknown_key_is_rejected(renderer):
    fixture = _capabilities_fixture()
    with pytest.raises(ValueError, match="unknown key"):
        _plan(renderer, fixture, _state(on=True, provider_ext={"wled": {"top": {"bogus": 1}}}))


def test_ext_top_must_be_an_object(renderer):
    fixture = _capabilities_fixture()
    with pytest.raises(ValueError, match="expected an object"):
        _plan(renderer, fixture, _state(on=True, provider_ext={"wled": {"top": 5}}))
