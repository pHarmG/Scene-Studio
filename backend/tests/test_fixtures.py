import pytest

from scene_studio.domain.bindings import (
    HaLightBinding,
    HueBinding,
    WledBinding,
    binding_from_dict,
)
from scene_studio.domain.capabilities import Capabilities
from scene_studio.domain.fixtures import Fixture, FixtureRegistry, HealthStatus, Target, derive_health
from scene_studio.domain.serde import ValidationError


# ---------------------------------------------------------------------------
# bindings
# ---------------------------------------------------------------------------

def test_hue_binding_round_trip():
    binding = HueBinding(bridge_id="bridge-001", resource_id="2a2c45a9-8a61", ha_entity_id="light.hue_g_strip")
    data = binding.to_dict()
    assert data["provider"] == "hue_v2"
    assert HueBinding.from_dict(data) == binding


def test_wled_binding_round_trip_and_empty_segments_mean_whole_device():
    binding = WledBinding(device_id="aabbccddeeff", segment_ids=[0, 1], endpoint_hint="http://wled.local")
    assert WledBinding.from_dict(binding.to_dict()) == binding
    whole = WledBinding.from_dict({"provider": "wled", "device_id": "aabbccddeeff"})
    assert whole.segment_ids == []


def test_wled_binding_rejects_bad_segment_indexes():
    with pytest.raises(ValidationError, match="segment_ids"):
        WledBinding.from_dict({"provider": "wled", "device_id": "x", "segment_ids": [-1]})
    with pytest.raises(ValidationError, match="segment_ids"):
        WledBinding.from_dict({"provider": "wled", "device_id": "x", "segment_ids": ["0"]})


def test_binding_union_dispatches_on_provider():
    assert isinstance(binding_from_dict({"provider": "hue_v2", "bridge_id": "b", "resource_id": "r"}), HueBinding)
    assert isinstance(binding_from_dict({"provider": "wled", "device_id": "d"}), WledBinding)
    assert isinstance(binding_from_dict({"provider": "ha_light", "ha_entity_id": "light.x"}), HaLightBinding)


def test_binding_union_rejects_unknown_provider():
    with pytest.raises(ValidationError, match="provider"):
        binding_from_dict({"provider": "trådfri", "resource_id": "r"})


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------

def test_capabilities_round_trip():
    caps = Capabilities(
        brightness=True,
        color_xy=True,
        color_temp=None,
        gradient=None,
        effects=["candle"],
        dynamic_native=True,
        extra={"manufacturer": "signify"},
    )
    assert Capabilities.from_dict(caps.to_dict()) == caps


def test_capabilities_defaults_are_conservative():
    caps = Capabilities.from_dict({})
    assert caps.on_off is True
    assert caps.brightness is False
    assert caps.color_xy is False
    assert caps.dynamic_native is False


# ---------------------------------------------------------------------------
# fixture health derivation
# ---------------------------------------------------------------------------

def _fixture(**overrides) -> Fixture:
    defaults = dict(id="g_strip", name="Hue G Strip")
    defaults.update(overrides)
    return Fixture(**defaults)


def test_health_disabled_wins_over_binding():
    fixture = _fixture(enabled=False, binding=HueBinding(bridge_id="b", resource_id="r"))
    assert derive_health(fixture) is HealthStatus.DISABLED


def test_health_unbound_when_no_binding():
    assert derive_health(_fixture()) is HealthStatus.UNBOUND


def test_health_ready_when_bound_and_enabled():
    fixture = _fixture(
        binding=HueBinding(bridge_id="b", resource_id="r"),
        capabilities=Capabilities(brightness=True),
    )
    assert derive_health(fixture) is HealthStatus.READY


def test_explicit_health_override_wins():
    fixture = _fixture(health=HealthStatus.MISSING)
    assert derive_health(fixture) is HealthStatus.MISSING


def test_double_strip_leave_behind_is_registry_only():
    """Reference case (master plan §3): disabling a fixture is a registry change;
    scene documents never mention it."""
    fixture = _fixture(id="double_strip", name="Double Strip", enabled=False)
    assert derive_health(fixture) is HealthStatus.DISABLED
    assert fixture.to_dict()["enabled"] is False
    assert "double_strip" not in Fixture.from_dict(fixture.to_dict()).groups


# ---------------------------------------------------------------------------
# fixture serialization
# ---------------------------------------------------------------------------

def test_fixture_round_trip_full():
    fixture = _fixture(
        location="studio",
        groups=["studio", "whole_house"],
        binding=HueBinding(bridge_id="bridge-001", resource_id="2a2c45a9", ha_entity_id="light.hue_g_strip"),
        capabilities=Capabilities(brightness=True, color_xy=True),
        metadata={"note": "migrated from crosswalk"},
    )
    assert Fixture.from_dict(fixture.to_dict()) == fixture


def test_fixture_rejects_unknown_keys():
    with pytest.raises(ValidationError, match="unknown key"):
        Fixture.from_dict({"id": "g_strip", "name": "x", "hue_resource_id": "abc"})


def test_fixture_requires_valid_id_and_name():
    with pytest.raises(ValidationError, match="fixture_id"):
        Fixture.from_dict({"id": "Bad Id", "name": "x"})
    with pytest.raises(ValidationError, match="name"):
        Fixture.from_dict({"id": "g_strip"})


# ---------------------------------------------------------------------------
# registry document
# ---------------------------------------------------------------------------

def test_registry_round_trip_and_duplicate_detection():
    registry = FixtureRegistry(
        fixtures=[_fixture(id="g_strip", name="A"), _fixture(id="lamp", name="B")],
        targets=[Target(id="studio", name="Studio")],
        updated="2026-09-10T20:00:00Z",
    )
    data = registry.to_dict()
    assert data["schema_version"] == 2
    assert FixtureRegistry.from_dict(data) == registry

    duplicate = dict(data)
    duplicate["fixtures"] = data["fixtures"] + [data["fixtures"][0]]
    with pytest.raises(ValidationError, match="duplicate fixture id"):
        FixtureRegistry.from_dict(duplicate)


def test_registry_rejects_wrong_schema_version():
    with pytest.raises(ValidationError, match="schema_version"):
        FixtureRegistry.from_dict({"schema_version": 99, "fixtures": [], "targets": []})
