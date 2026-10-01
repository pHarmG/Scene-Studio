"""Room-level target power policy used by script.scene_studio_target_power."""

from pathlib import Path

import pytest

from scene_studio.domain.target_power import resolve_target_power_action

REPO = Path(__file__).resolve().parents[2]
STAGED_SCRIPT = REPO / "backend" / "fixtures" / "ha_scripts" / "scripts.scene_studio.yaml"


def test_on_and_off_pass_through_regardless_of_member_state():
    assert resolve_target_power_action("on", ["off", "off"]) == "on"
    assert resolve_target_power_action("OFF", ["on", "on"]) == "off"


def test_toggle_all_off_becomes_all_on():
    assert resolve_target_power_action("toggle", ["off", "off", "unavailable"]) == "on"


def test_toggle_mixed_becomes_all_off():
    assert resolve_target_power_action("toggle", ["on", "off", "off"]) == "off"


def test_toggle_all_on_becomes_all_off():
    assert resolve_target_power_action("toggle", ["on", "on"]) == "off"


def test_toggle_rejects_unknown_action():
    with pytest.raises(ValueError, match="on, off, or toggle"):
        resolve_target_power_action("invert", ["on"])


def test_staged_ha_script_uses_room_level_toggle_not_per_entity():
    text = STAGED_SCRIPT.read_text(encoding="utf-8")
    assert "action: light.toggle" not in text
    assert "resolved_action" in text
    assert "select('is_state', 'on')" in text
    assert "light.turn_on" in text and "light.turn_off" in text
    assert '- "on"' in text and '- "off"' in text
    assert "condition: template" in text
