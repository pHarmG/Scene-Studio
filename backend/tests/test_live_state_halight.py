"""HA light live-state normalization tests (plan §2.3, §12)."""

from scene_studio.domain.fixtures import Fixture
from scene_studio.live_state.halight import normalize_ha_live_state


def ha_fixture(fixture_id="lamp", entity_id="light.lamp"):
    return Fixture.from_dict(
        {"id": fixture_id, "name": fixture_id, "binding": {"provider": "ha_light", "ha_entity_id": entity_id}}
    )


def test_rgb_color_attribute():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "on", "attributes": {"rgb_color": [10, 20, 30], "brightness": 128}}}
    out = normalize_ha_live_state(states, fixtures)
    result = out["lamp"]
    assert result.on is True
    assert result.color_mode == "rgb"
    assert result.display_colors == ["#0a141e"]
    assert result.brightness == round(128 / 255 * 100)


def test_xy_color_attribute_uses_shared_conversion():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "on", "attributes": {"xy_color": [0.4, 0.4]}}}
    out = normalize_ha_live_state(states, fixtures)
    assert out["lamp"].color_mode == "rgb"
    assert out["lamp"].display_colors[0].startswith("#")


def test_color_temp_kelvin_attribute():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "on", "attributes": {"color_temp_kelvin": 2700}}}
    out = normalize_ha_live_state(states, fixtures)
    result = out["lamp"]
    assert result.color_mode == "cct"
    assert result.color_temp_kelvin == 2700


def test_legacy_mireds_color_temp_attribute_converted_to_kelvin():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "on", "attributes": {"color_temp": 370}}}  # ~2703K
    out = normalize_ha_live_state(states, fixtures)
    assert out["lamp"].color_mode == "cct"
    assert out["lamp"].color_temp_kelvin == round(1_000_000 / 370)


def test_off_state_reports_no_color_but_stays_available():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "off", "attributes": {}}}
    out = normalize_ha_live_state(states, fixtures)
    result = out["lamp"]
    assert result.available is True
    assert result.on is False
    assert result.color_mode == "none"


def test_unavailable_entity():
    fixtures = [ha_fixture()]
    states = {"light.lamp": {"state": "unavailable", "attributes": {}}}
    out = normalize_ha_live_state(states, fixtures)
    result = out["lamp"]
    assert result.available is False
    assert result.state_kind == "unavailable"


def test_entity_absent_from_states_payload_is_unavailable():
    fixtures = [ha_fixture()]
    out = normalize_ha_live_state({}, fixtures)
    assert out["lamp"].available is False


def test_non_ha_fixture_is_skipped():
    fixtures = [
        Fixture.from_dict(
            {"id": "strip", "name": "Strip", "binding": {"provider": "wled", "device_id": "aabbcc", "segment_ids": [0]}}
        )
    ]
    out = normalize_ha_live_state({}, fixtures)
    assert out == {}
