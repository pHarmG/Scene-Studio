"""Hue CLIP v2 live-state normalization (plan §2.1).

Transport-free: parses an already-fetched ``GET /clip/v2/resource/light``
payload (the same shape/fetcher discovery already uses) into
:class:`FixtureLiveState` values for every ``hue_v2``-bound fixture.

Only per-light (``resource_type == "light"``) bindings can be observed here:
CLIP v2 ``grouped_light`` resources carry no per-light color state (the same
restriction the Hue renderer already applies to writes).
"""

from __future__ import annotations

from typing import Any

from ..domain.fixtures import Fixture
from ..domain.live_state import FixtureLiveState, kelvin_to_hex, mirek_to_kelvin
from ..migration.colors import xy_to_hex

# Cap the number of gradient points shown in the aura (plan §2.1: "cap the
# visual sample to a reasonable number of colors (for example 3-5)").
_MAX_GRADIENT_COLORS = 5


def normalize_hue_live_state(clip_lights: Any, fixtures: list[Fixture]) -> dict[str, FixtureLiveState]:
    """Build one :class:`FixtureLiveState` per ``hue_v2``-bound fixture.

    ``clip_lights``: ``{"data": [...CLIP v2 light resources...]}``, exactly
    what ``DiscoveryFetchers.fetch_hue`` already returns.
    """
    lights_by_id = _index_by_id(clip_lights)
    out: dict[str, FixtureLiveState] = {}
    for fixture in fixtures:
        binding = fixture.binding
        if binding is None or getattr(binding, "provider", None) != "hue_v2":
            continue
        if getattr(binding, "resource_type", "light") != "light":
            out[fixture.id] = FixtureLiveState(
                fixture_id=fixture.id,
                provider="hue_v2",
                available=False,
                state_kind="unavailable",
                detail="grouped_light bindings expose no per-light live color",
            )
            continue
        light = lights_by_id.get(binding.resource_id)
        if light is None:
            out[fixture.id] = FixtureLiveState(
                fixture_id=fixture.id,
                provider="hue_v2",
                available=False,
                state_kind="unavailable",
                detail="light resource not present in the last Hue read",
            )
            continue
        out[fixture.id] = _normalize_light(fixture.id, light)
    return out


def _normalize_light(fixture_id: str, light: dict) -> FixtureLiveState:
    on = _get(light, "on", "on")
    brightness = _normalized_brightness(_get(light, "dimming", "brightness"))
    dynamic = _is_dynamic(light)

    gradient_points = _gradient_points(light)
    if gradient_points:
        colors = [xy_to_hex(x, y) for x, y in gradient_points[:_MAX_GRADIENT_COLORS]]
        return FixtureLiveState(
            fixture_id=fixture_id, provider="hue_v2", available=True, on=on, brightness=brightness,
            color_mode="gradient", display_colors=colors, dynamic=dynamic, state_kind="live",
        )

    xy = _get(light, "color", "xy")
    if isinstance(xy, dict) and isinstance(xy.get("x"), (int, float)) and isinstance(xy.get("y"), (int, float)):
        return FixtureLiveState(
            fixture_id=fixture_id, provider="hue_v2", available=True, on=on, brightness=brightness,
            color_mode="rgb", display_colors=[xy_to_hex(xy["x"], xy["y"])], dynamic=dynamic, state_kind="live",
        )

    mirek = _get(light, "color_temperature", "mirek")
    if isinstance(mirek, (int, float)):
        kelvin = mirek_to_kelvin(mirek)
        return FixtureLiveState(
            fixture_id=fixture_id, provider="hue_v2", available=True, on=on, brightness=brightness,
            color_mode="cct", display_colors=[kelvin_to_hex(kelvin)], color_temp_kelvin=kelvin,
            dynamic=dynamic, state_kind="live",
        )

    return FixtureLiveState(
        fixture_id=fixture_id, provider="hue_v2", available=True, on=on, brightness=brightness,
        color_mode="none" if on is False else "unknown", dynamic=dynamic, state_kind="live",
    )


def _gradient_points(light: dict) -> list[tuple[float, float]]:
    gradient = light.get("gradient")
    if not isinstance(gradient, dict):
        return []
    points = gradient.get("points")
    if not isinstance(points, list):
        return []
    out: list[tuple[float, float]] = []
    for point in points:
        xy = point.get("color", {}).get("xy") if isinstance(point, dict) else None
        if isinstance(xy, dict) and isinstance(xy.get("x"), (int, float)) and isinstance(xy.get("y"), (int, float)):
            out.append((xy["x"], xy["y"]))
    return out


def _is_dynamic(light: dict) -> bool:
    dynamics_status = _get(light, "dynamics", "status")
    if isinstance(dynamics_status, str) and dynamics_status not in ("none", ""):
        return True
    effects_status = _get(light, "effects", "status")
    if isinstance(effects_status, str) and effects_status not in ("no_effect", ""):
        return True
    return False


def _normalized_brightness(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0, min(100, round(value)))


def _get(obj: dict, *path: str) -> Any:
    current: Any = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _index_by_id(clip_lights: Any) -> dict[str, dict]:
    data = clip_lights.get("data") if isinstance(clip_lights, dict) else None
    if not isinstance(data, list):
        return {}
    return {
        light["id"]: light
        for light in data
        if isinstance(light, dict) and isinstance(light.get("id"), str)
    }
