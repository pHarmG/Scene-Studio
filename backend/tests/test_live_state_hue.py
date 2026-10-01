"""Hue live-state normalization tests (plan §2.1, §12)."""

from scene_studio.domain.fixtures import Fixture
from scene_studio.live_state.hue import normalize_hue_live_state


def hue_fixture(fixture_id="g_strip", resource_id="rid-1", resource_type="light"):
    return Fixture.from_dict(
        {
            "id": fixture_id,
            "name": fixture_id,
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": resource_id, "resource_type": resource_type},
        }
    )


def test_ordinary_xy_color():
    fixtures = [hue_fixture()]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}, "dimming": {"brightness": 60.0}, "color": {"xy": {"x": 0.6, "y": 0.35}}}]}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.available is True
    assert state.on is True
    assert state.brightness == 60
    assert state.color_mode == "rgb"
    assert state.display_colors and state.display_colors[0].startswith("#")
    assert state.state_kind == "live"
    assert state.dynamic is False


def test_color_temperature_reports_kelvin_and_display_approximation():
    fixtures = [hue_fixture()]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}, "color_temperature": {"mirek": 250}}]}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.color_mode == "cct"
    assert state.color_temp_kelvin == round(1_000_000 / 250)
    assert state.display_colors and state.display_colors[0].startswith("#")


def test_gradient_preserves_order_and_caps_length():
    fixtures = [hue_fixture()]
    points = [{"color": {"xy": {"x": 0.1 * i, "y": 0.05}}} for i in range(1, 8)]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}, "gradient": {"points": points}}]}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.color_mode == "gradient"
    assert len(state.display_colors) == 5  # capped, not collapsed to one average color
    assert len(set(state.display_colors)) > 1  # order/distinctness preserved


def test_off_with_missing_color_fields_is_none_not_unknown():
    fixtures = [hue_fixture()]
    payload = {"data": [{"id": "rid-1", "on": {"on": False}}]}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.on is False
    assert state.color_mode == "none"
    assert state.state_kind == "live"  # the OFF observation itself is real/live


def test_missing_color_fields_while_on_is_unknown_not_fabricated():
    fixtures = [hue_fixture()]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}}]}
    out = normalize_hue_live_state(payload, fixtures)
    assert out["g_strip"].color_mode == "unknown"
    assert out["g_strip"].display_colors == []


def test_light_absent_from_payload_is_unavailable():
    fixtures = [hue_fixture()]
    payload = {"data": []}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.available is False
    assert state.state_kind == "unavailable"


def test_grouped_light_binding_has_no_per_light_state():
    fixtures = [hue_fixture(resource_type="grouped_light")]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}, "color": {"xy": {"x": 0.5, "y": 0.4}}}]}
    out = normalize_hue_live_state(payload, fixtures)
    state = out["g_strip"]
    assert state.available is False
    assert "grouped_light" in state.detail


def test_active_dynamics_status_marks_dynamic_true():
    fixtures = [hue_fixture()]
    payload = {"data": [{"id": "rid-1", "on": {"on": True}, "color": {"xy": {"x": 0.4, "y": 0.4}}, "dynamics": {"status": "dynamic_palette"}}]}
    out = normalize_hue_live_state(payload, fixtures)
    assert out["g_strip"].dynamic is True
    assert out["g_strip"].color_mode == "rgb"  # the reported instantaneous color, never claimed frame-accurate


def test_non_hue_fixture_is_skipped():
    fixtures = [
        Fixture.from_dict({"id": "lamp", "name": "Lamp", "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"}})
    ]
    out = normalize_hue_live_state({"data": []}, fixtures)
    assert out == {}
