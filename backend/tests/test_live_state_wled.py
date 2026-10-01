"""WLED live-state normalization tests (plan §2.2, §12)."""

from scene_studio.domain.fixtures import Fixture
from scene_studio.live_state.wled import normalize_wled_live_state


def wled_fixture(fixture_id="seg0", device_id="aabbccddeeff", segment_ids=(0,)):
    return Fixture.from_dict(
        {
            "id": fixture_id,
            "name": fixture_id,
            "binding": {"provider": "wled", "device_id": device_id, "segment_ids": list(segment_ids)},
        }
    )


def test_static_segment_rgb():
    fixtures = [wled_fixture()]
    state = {"on": True, "bri": 128, "seg": [{"id": 0, "on": True, "bri": 200, "fx": 0, "col": [[255, 0, 0]]}]}
    out = normalize_wled_live_state(state, fixtures)
    result = out["seg0"]
    assert result.available is True
    assert result.on is True
    assert result.color_mode == "rgb"
    assert result.display_colors == ["#ff0000"]
    assert result.state_kind == "live"
    assert result.dynamic is False


def test_rgbw_blends_white_channel_without_losing_hue():
    fixtures = [wled_fixture()]
    state = {"on": True, "bri": 255, "seg": [{"id": 0, "on": True, "bri": 255, "fx": 0, "col": [[255, 0, 0, 120]]}]}
    out = normalize_wled_live_state(state, fixtures)
    hexed = out["seg0"].display_colors[0]
    r, g, b = int(hexed[1:3], 16), int(hexed[3:5], 16), int(hexed[5:7], 16)
    assert r == 255  # already maxed
    assert g > 0 and b > 0  # white channel visibly lifts the other channels
    assert g == b  # white blends evenly


def test_segment_brightness_normalized_to_0_100():
    fixtures = [wled_fixture()]
    state = {"on": True, "seg": [{"id": 0, "on": True, "bri": 128, "fx": 0, "col": [[0, 255, 0]]}]}
    out = normalize_wled_live_state(state, fixtures)
    assert out["seg0"].brightness == round(128 / 255 * 100)


def test_device_off_makes_fixture_off_even_if_segment_on():
    fixtures = [wled_fixture()]
    state = {"on": False, "seg": [{"id": 0, "on": True, "fx": 0, "col": [[0, 0, 255]]}]}
    out = normalize_wled_live_state(state, fixtures)
    assert out["seg0"].on is False


def test_dynamic_fx_is_configured_dynamic_not_live():
    fixtures = [wled_fixture()]
    state = {"on": True, "seg": [{"id": 0, "on": True, "fx": 45, "col": [[255, 0, 0], [0, 255, 0]]}]}
    out = normalize_wled_live_state(state, fixtures)
    result = out["seg0"]
    assert result.dynamic is True
    assert result.state_kind == "configured_dynamic"  # never claims per-frame accuracy
    assert result.color_mode == "dynamic"
    assert result.display_colors == ["#ff0000", "#00ff00"]


def test_multiple_segments_mapped_from_one_controller_read():
    fixtures = [wled_fixture("seg0", segment_ids=(0,)), wled_fixture("seg1", segment_ids=(1,))]
    state = {
        "on": True,
        "seg": [
            {"id": 0, "on": True, "fx": 0, "col": [[255, 0, 0]]},
            {"id": 1, "on": True, "fx": 0, "col": [[0, 0, 255]]},
        ],
    }
    out = normalize_wled_live_state(state, fixtures)
    assert out["seg0"].display_colors == ["#ff0000"]
    assert out["seg1"].display_colors == ["#0000ff"]


def test_segment_missing_from_state_is_unavailable():
    fixtures = [wled_fixture(segment_ids=(3,))]
    state = {"on": True, "seg": [{"id": 0, "on": True, "fx": 0, "col": [[255, 0, 0]]}]}
    out = normalize_wled_live_state(state, fixtures)
    assert out["seg0"].available is False
    assert out["seg0"].state_kind == "unavailable"


def test_non_wled_fixture_is_skipped():
    fixtures = [
        Fixture.from_dict({"id": "lamp", "name": "Lamp", "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"}})
    ]
    out = normalize_wled_live_state({"on": True, "seg": []}, fixtures)
    assert out == {}
