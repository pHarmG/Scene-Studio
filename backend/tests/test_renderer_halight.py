"""Tests for the ordinary HA light renderer (Workstream B3).

Deterministic, dry-run only: every test asserts on the emitted render plan
without any device or network contact.
"""

from __future__ import annotations

import dataclasses

import pytest

from scene_studio.domain.bindings import HaLightBinding, HueBinding
from scene_studio.domain.capabilities import Capabilities, ColorTempRange
from scene_studio.domain.fidelity import FidelityLevel
from scene_studio.domain.fixtures import Fixture
from scene_studio.domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy
from scene_studio.renderers.halight import HaLightRenderer


def _sample_fixture(sample_registry_data: dict, fixture_id: str) -> Fixture:
    raw = next(item for item in sample_registry_data["fixtures"] if item["id"] == fixture_id)
    return Fixture.from_dict(raw)


@pytest.fixture()
def office_lights() -> Fixture:
    """Synthetic ha_light fixture (color_temp) — not the retired aggregate helper."""
    return Fixture(
        id="office_lights",
        name="Office Ceiling",
        binding=HaLightBinding(ha_entity_id="light.office_lights"),
        capabilities=Capabilities(
            on_off=True,
            brightness=True,
            color_xy=True,
            color_temp=ColorTempRange(153, 500),
        ),
    )


@pytest.fixture()
def dimmable_only_fixture() -> Fixture:
    """ha_light fixture whose capabilities show no color channel at all."""
    return Fixture(
        id="dumb_bulb",
        name="Dumb Bulb",
        binding=HaLightBinding(ha_entity_id="light.dumb_bulb"),
        capabilities=Capabilities(on_off=True, brightness=True),
    )


def _render(renderer: HaLightRenderer, fixture: Fixture, state: FixtureState, *, motion=None, palette=()):
    return renderer.plan_fixture(
        fixture,
        state,
        motion=motion if motion is not None else Motion(),
        palette=list(palette),
    )


# ---------------------------------------------------------------------------
# static payload exactness


def test_color_brightness_payload_exactness(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(on=True, brightness=42.0, color="#ff9900"),
    )
    assert plan.fixture_id == "office_lights"
    assert plan.provider == "ha_light"
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.provider == "ha_light"
    assert operation.op == "ha.call_light"
    assert operation.resource_ref == "light.office_lights"
    assert operation.payload == {
        "entity_id": "light.office_lights",
        "brightness_pct": 42,
        "rgb_color": [255, 153, 0],
    }
    assert isinstance(operation.payload["brightness_pct"], int)


def test_on_true_alone_is_bare_turn_on(office_lights: Fixture) -> None:
    plan = _render(HaLightRenderer(), office_lights, FixtureState(on=True))
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert plan.operations[0].payload == {"entity_id": "light.office_lights"}


def test_brightness_zero_is_omitted(office_lights: Fixture) -> None:
    plan = _render(HaLightRenderer(), office_lights, FixtureState(on=True, brightness=0))
    assert plan.operations[0].payload == {"entity_id": "light.office_lights"}


def test_off_uses_call_light_off_op(office_lights: Fixture) -> None:
    plan = _render(HaLightRenderer(), office_lights, FixtureState(on=False, brightness=30))
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.op == "ha.call_light_off"
    assert operation.resource_ref == "light.office_lights"
    assert operation.payload == {"entity_id": "light.office_lights"}


def test_color_temp_mired_passthrough(office_lights: Fixture) -> None:
    plan = _render(HaLightRenderer(), office_lights, FixtureState(color_temp_mirek=370))
    payload = plan.operations[0].payload
    assert payload["color_temp_mired"] == 370
    assert "color_temp_kelvin" not in payload
    assert plan.fidelity is FidelityLevel.EQUIVALENT


def test_transition_400ms_becomes_04_seconds(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(brightness=50, transition_ms=400),
    )
    payload = plan.operations[0].payload
    assert payload["transition"] == 0.4
    assert isinstance(payload["transition"], float)


# ---------------------------------------------------------------------------
# capability transparency


def test_gradient_reduced_to_first_color_approximate(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(gradient=["#ff0000", "#00ff00"]),
    )
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "gradient" in plan.reason
    operation = plan.operations[0]
    assert operation.op == "ha.call_light"
    assert operation.payload["rgb_color"] == [255, 0, 0]


def test_color_on_colorless_fixture_omitted_approximate(
    dimmable_only_fixture: Fixture,
) -> None:
    plan = _render(
        HaLightRenderer(),
        dimmable_only_fixture,
        FixtureState(color="#ff9900", brightness=60),
    )
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "color" in plan.reason
    operation = plan.operations[0]
    assert "rgb_color" not in operation.payload
    assert operation.payload["brightness_pct"] == 60


def test_color_only_on_colorless_fixture_unsupported(
    dimmable_only_fixture: Fixture,
) -> None:
    plan = _render(HaLightRenderer(), dimmable_only_fixture, FixtureState(color="#ff9900"))
    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []
    assert "does not support color" in plan.reason


def test_missing_capabilities_generic_dimmable_note(office_lights: Fixture) -> None:
    bare = dataclasses.replace(office_lights, capabilities=None)
    plan = _render(HaLightRenderer(), bare, FixtureState(brightness=25))
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert "assumed generic dimmable" in plan.reason
    assert plan.operations[0].payload["brightness_pct"] == 25


def test_effect_included_as_approximate(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(effect="candle", brightness=40),
    )
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "effect" in plan.reason
    assert plan.operations[0].payload["effect"] == "candle"


# ---------------------------------------------------------------------------
# dynamic intent


def test_dynamic_auto_approximate_static_first_palette_color(
    office_lights: Fixture,
) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(brightness=50),
        motion=Motion(mode=MotionMode.PALETTE_CYCLE, strategy=MotionStrategy.AUTO),
        palette=["#3366cc", "#ffcc00"],
    )
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert "static" in plan.reason
    operation = plan.operations[0]
    assert operation.payload["rgb_color"] == [51, 102, 204]
    assert operation.payload["brightness_pct"] == 50


def test_dynamic_auto_without_palette_still_approximate(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(brightness=50),
        motion=Motion(mode=MotionMode.EFFECT, strategy=MotionStrategy.AUTO),
    )
    assert plan.fidelity is FidelityLevel.APPROXIMATE
    assert plan.operations[0].payload == {
        "entity_id": "light.office_lights",
        "brightness_pct": 50,
    }


def test_dynamic_native_preferred_unsupported(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(brightness=50),
        motion=Motion(
            mode=MotionMode.PALETTE_CYCLE, strategy=MotionStrategy.NATIVE_PREFERRED
        ),
    )
    assert plan.fidelity is FidelityLevel.UNSUPPORTED
    assert plan.operations == []
    assert "native" in plan.reason


def test_dynamic_strategy_static_forces_static_render(office_lights: Fixture) -> None:
    plan = _render(
        HaLightRenderer(),
        office_lights,
        FixtureState(on=True),
        motion=Motion(mode=MotionMode.PALETTE_CYCLE, strategy=MotionStrategy.STATIC),
    )
    assert plan.fidelity is FidelityLevel.EQUIVALENT
    assert plan.operations[0].op == "ha.call_light"


def test_fidelity_is_never_native(office_lights: Fixture) -> None:
    states = [
        FixtureState(on=True),
        FixtureState(brightness=10, color="#102030", transition_ms=250),
        FixtureState(gradient=["#102030", "#405060"]),
        FixtureState(effect="candle"),
    ]
    motions = [
        Motion(),
        Motion(mode=MotionMode.PALETTE_CYCLE, strategy=MotionStrategy.AUTO),
        Motion(mode=MotionMode.PALETTE_CYCLE, strategy=MotionStrategy.STATIC),
    ]
    for state in states:
        for motion in motions:
            plan = _render(HaLightRenderer(), office_lights, state, motion=motion)
            assert plan.fidelity is not FidelityLevel.NATIVE


# ---------------------------------------------------------------------------
# guard rails + determinism


def test_wrong_binding_type_raises(sample_registry_data: dict) -> None:
    hue_fixture = _sample_fixture(sample_registry_data, "g_strip")
    assert isinstance(hue_fixture.binding, HueBinding)
    with pytest.raises(ValueError):
        _render(HaLightRenderer(), hue_fixture, FixtureState(on=True))

    unbound = Fixture(id="bare_light", name="Bare Light", binding=None)
    with pytest.raises(ValueError):
        _render(HaLightRenderer(), unbound, FixtureState(on=True))


def test_determinism(office_lights: Fixture) -> None:
    state = FixtureState(
        on=True, brightness=66.5, color="#aa5500", transition_ms=1150
    )
    first = _render(HaLightRenderer(), office_lights, state).to_dict()
    second = _render(HaLightRenderer(), office_lights, state).to_dict()
    third = _render(HaLightRenderer(), office_lights, state).to_dict()
    assert first == second == third
    assert first["operations"][0]["payload"] == {
        "entity_id": "light.office_lights",
        "brightness_pct": 67,
        "rgb_color": [170, 85, 0],
        "transition": 1.1,
    }
