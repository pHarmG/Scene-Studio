"""WLED observation builder tests (recorded payloads, no network)."""

import json
from pathlib import Path

import pytest

from scene_studio.discovery import build_wled_observations, derive_wled_device_id

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"


@pytest.fixture(scope="module")
def wled_info() -> dict:
    return json.loads((RECORDED / "wled_info.studio.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def wled_state() -> dict:
    return json.loads((RECORDED / "wled_state.studio.json").read_text(encoding="utf-8"))


def test_device_id_from_mac_is_normalized(wled_info):
    assert derive_wled_device_id(wled_info) == "aabbccddeeff"


def test_observations_use_real_segment_indexes(wled_info, wled_state):
    observations = build_wled_observations(wled_info, wled_state, "http://wled.local")
    assert len(observations) == 6
    assert [obs.provider_resource_id for obs in observations] == [
        f"aabbccddeeff:seg:{index}" for index in range(6)
    ]
    segment_2 = observations[2]
    assert segment_2.observation_id == "wled:aabbccddeeff:seg:2"
    assert segment_2.metadata["segment_start"] == 10
    assert segment_2.metadata["segment_stop"] == 15
    assert segment_2.metadata["device_id"] == "aabbccddeeff"
    assert segment_2.endpoint_hint == "http://wled.local"


def test_segment_names_from_device_with_fallback(wled_info, wled_state):
    observations = build_wled_observations(wled_info, wled_state, None)
    by_id = {obs.provider_resource_id: obs for obs in observations}
    assert by_id["aabbccddeeff:seg:0"].name == "Bar Left"
    assert by_id["aabbccddeeff:seg:1"].name == "Segment 1"  # empty device name -> fallback


def test_capabilities_from_state(wled_info, wled_state):
    observations = build_wled_observations(wled_info, wled_state, None)
    for obs in observations:
        assert obs.capabilities.on_off is True
        assert obs.capabilities.brightness is True
        assert obs.capabilities.color_xy is False  # WLED speaks RGB; xy is renderer work
        assert obs.capabilities.dynamic_native is True  # fx support reported


def test_full_json_payload_captures_effect_and_palette_catalogs(wled_info, wled_state):
    # The recorded payload is full-/json shaped: root effects/palettes arrays
    # are captured order-preserving (list position = fx/pal id).
    observations = build_wled_observations(wled_info, wled_state, None)
    capabilities = observations[0].capabilities
    assert capabilities.effects[:5] == ["Solid", "Blink", "Breathe", "Wipe", "Wipe Random"]
    assert capabilities.effects[9] == "Rainbow"  # 0.14 ordering: Rainbow is fx 9
    assert capabilities.palettes[0] == "Default"
    assert capabilities.palettes[6] == "Party"
    assert capabilities.dynamic_native is True


def test_explicit_effects_and_palettes_params_captured(wled_info):
    state = {
        "on": True,
        "bri": 128,
        "seg": [{"id": 0, "start": 0, "stop": 10, "n": "Only", "fx": 0}],
    }
    observations = build_wled_observations(
        wled_info,
        state,
        None,
        effects=["Solid", "Blink", "Breathe", "Rainbow"],
        palettes=["Default", "Party"],
    )
    capabilities = observations[0].capabilities
    assert capabilities.effects == ["Solid", "Blink", "Breathe", "Rainbow"]
    assert capabilities.palettes == ["Default", "Party"]
    assert capabilities.dynamic_native is True  # fx still reported by the segment


def test_full_json_root_arrays_win_over_explicit_params(wled_info, wled_state):
    observations = build_wled_observations(
        wled_info,
        wled_state,
        None,
        effects=["Not", "The", "Real", "Catalog"],
        palettes=["Also", "Not"],
    )
    capabilities = observations[0].capabilities
    assert capabilities.effects[0] == "Solid"
    assert capabilities.palettes[0] == "Default"


def test_cct_captured_from_info_leds(wled_state):
    info = {"ver": "14.4", "arch": "esp32", "mac": "AA:BB:CC:DD:EE:FF", "leds": {"count": 30, "cct": True}}
    observations = build_wled_observations(info, wled_state, None)
    assert all(obs.capabilities.cct is True for obs in observations)

    # recorded device does not report cct support
    recorded_info = json.loads((RECORDED / "wled_info.studio.json").read_text(encoding="utf-8"))
    observations = build_wled_observations(recorded_info, wled_state, None)
    assert all(obs.capabilities.cct is False for obs in observations)


def test_unused_segment_ranges_are_skipped(wled_info):
    state = {
        "on": True,
        "bri": 128,
        "seg": [
            {"id": 0, "start": 0, "stop": 10, "n": "Real", "fx": 0},
            {"id": 1, "start": 10, "stop": 0, "n": "Unused"},  # stop <= start -> not real
            {"id": 2, "start": 10, "stop": 20, "n": "", "fx": 45},
        ],
    }
    observations = build_wled_observations(wled_info, state, None)
    assert [obs.provider_resource_id for obs in observations] == [
        "aabbccddeeff:seg:0",
        "aabbccddeeff:seg:2",
    ]


def test_mac_fallback_is_stable_per_payload_and_payload_sensitive():
    info_a = {"ver": "14.4", "arch": "esp32", "leds": {"count": 30}}
    info_b = {"ver": "14.4", "arch": "esp32", "leds": {"count": 60}}
    id_a1 = derive_wled_device_id(info_a)
    id_a2 = derive_wled_device_id(info_a)
    id_b = derive_wled_device_id(info_b)
    assert id_a1 == id_a2  # stable for the identical recorded payload
    assert id_a1.startswith("wled-")
    assert id_a1 != id_b  # different payload -> different fallback id
    assert derive_wled_device_id({"mac": "AA-BB-0C-DD-EE-FF"}) == "aabb0cddeeff"


def test_builder_is_deterministic(wled_info, wled_state):
    first = build_wled_observations(wled_info, wled_state, "http://wled.local")
    second = build_wled_observations(wled_info, wled_state, "http://wled.local")
    assert [obs.to_dict() for obs in first] == [obs.to_dict() for obs in second]


def test_device_without_segments_yields_no_observations(wled_info):
    assert build_wled_observations(wled_info, {"on": True, "seg": []}, None) == []
    assert build_wled_observations(wled_info, {}, None) == []


def test_halight_color_temp_without_range_uses_ha_default():
    """R3 live finding: office_lights supports color_temp but reports
    min/max_mireds = None; the builder must use HA's default range instead of
    reporting a capability downgrade."""
    from scene_studio.discovery.halight import build_halight_observations

    obs = build_halight_observations(
        {"light.office_lights": {"state": "on", "attributes": {
            "friendly_name": "Office Lights",
            "supported_color_modes": ["color_temp", "rgb", "xy"],
            "min_mireds": None,
            "max_mireds": None,
        }}}
    )
    caps = obs[0].capabilities
    assert caps.color_temp is not None
    assert (caps.color_temp.mirek_min, caps.color_temp.mirek_max) == (153, 500)
    assert caps.color_xy is True  # rgb/xy color modes present
