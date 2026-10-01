"""Home Assistant light live-state normalization (plan §2.3).

Transport-free: parses an already-fetched HA state mapping
``{entity_id: {"state": ..., "attributes": {...}}}`` (the same shape/fetcher
discovery already uses) into :class:`FixtureLiveState` values for every
``ha_light``-bound fixture.

Priority order matches the entity's own reported ``color_mode`` /
attributes; a color is never guessed from a *supported* mode when the
current attributes don't actually carry one (plan §2.3).
"""

from __future__ import annotations

from typing import Any

from ..domain.fixtures import Fixture
from ..domain.live_state import FixtureLiveState, kelvin_to_hex, mirek_to_kelvin
from ..migration.colors import rgb_to_hex, xy_to_hex


def normalize_ha_live_state(ha_states: Any, fixtures: list[Fixture]) -> dict[str, FixtureLiveState]:
    """Build one :class:`FixtureLiveState` per ``ha_light``-bound fixture.

    ``ha_states``: ``{entity_id: {"state": "on"|"off"|"unavailable", "attributes": {...}}}``,
    exactly what ``DiscoveryFetchers.fetch_ha_states`` already returns.
    """
    states = ha_states if isinstance(ha_states, dict) else {}
    out: dict[str, FixtureLiveState] = {}
    for fixture in fixtures:
        binding = fixture.binding
        if binding is None or getattr(binding, "provider", None) != "ha_light":
            continue
        entity_id = getattr(binding, "ha_entity_id", None)
        raw = states.get(entity_id) if entity_id else None
        if raw is None:
            out[fixture.id] = FixtureLiveState(
                fixture_id=fixture.id,
                provider="ha_light",
                available=False,
                state_kind="unavailable",
                detail=f"no state reported for entity '{entity_id}'",
            )
            continue
        out[fixture.id] = _normalize_entity(fixture.id, raw)
    return out


def _normalize_entity(fixture_id: str, raw: Any) -> FixtureLiveState:
    state_str = raw.get("state") if isinstance(raw, dict) else None
    attributes = raw.get("attributes") if isinstance(raw, dict) and isinstance(raw.get("attributes"), dict) else {}

    if state_str in ("unavailable", "unknown", None):
        return FixtureLiveState(
            fixture_id=fixture_id, provider="ha_light", available=False, state_kind="unavailable",
            detail=f"entity state is {state_str!r}",
        )

    on = state_str == "on"
    brightness = _normalized_brightness(attributes.get("brightness"))

    if not on:
        return FixtureLiveState(
            fixture_id=fixture_id, provider="ha_light", available=True, on=False, brightness=brightness,
            color_mode="none", state_kind="live",
        )

    rgb = attributes.get("rgb_color")
    if isinstance(rgb, (list, tuple)) and len(rgb) >= 3:
        return FixtureLiveState(
            fixture_id=fixture_id, provider="ha_light", available=True, on=True, brightness=brightness,
            color_mode="rgb", display_colors=[rgb_to_hex(rgb)], state_kind="live",
        )

    xy = attributes.get("xy_color")
    if isinstance(xy, (list, tuple)) and len(xy) >= 2:
        return FixtureLiveState(
            fixture_id=fixture_id, provider="ha_light", available=True, on=True, brightness=brightness,
            color_mode="rgb", display_colors=[xy_to_hex(xy[0], xy[1])], state_kind="live",
        )

    kelvin = _color_temp_kelvin(attributes)
    if kelvin is not None:
        return FixtureLiveState(
            fixture_id=fixture_id, provider="ha_light", available=True, on=True, brightness=brightness,
            color_mode="cct", display_colors=[kelvin_to_hex(kelvin)], color_temp_kelvin=kelvin, state_kind="live",
        )

    return FixtureLiveState(
        fixture_id=fixture_id, provider="ha_light", available=True, on=True, brightness=brightness,
        color_mode="unknown", state_kind="live",
    )


def _color_temp_kelvin(attributes: dict) -> int | None:
    kelvin = attributes.get("color_temp_kelvin")
    if isinstance(kelvin, (int, float)):
        return round(kelvin)
    mirek = attributes.get("color_temp")
    if isinstance(mirek, (int, float)):
        return mirek_to_kelvin(mirek)
    return None


def _normalized_brightness(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0, min(100, round(float(value) / 255.0 * 100.0)))
