"""WLED live-state normalization (plan §2.2).

Transport-free: parses an already-fetched ``GET /json/state`` payload (the
same shape/fetcher discovery already uses) into :class:`FixtureLiveState`
values for every ``wled``-bound fixture, mapping segment ids to fixtures —
never one HTTP request per segment.

WLED's state JSON describes the *effect configuration*, not the
instantaneous LED-frame color. A segment running an effect (``fx != 0``) is
reported as ``state_kind="configured_dynamic"``, never upgraded to
``"live"`` — see the module docstring in the plan for why that distinction
matters (WLED does not expose per-frame color here).
"""

from __future__ import annotations

from typing import Any

from ..domain.fixtures import Fixture
from ..domain.live_state import FixtureLiveState
from ..migration.colors import rgb_to_hex

# Cap the number of configured colors surfaced per fixture (plan §6.3: don't
# let a busy effect/multi-segment binding become a noisy aura).
_MAX_DISPLAY_COLORS = 3
# A documented display-only weight for blending WLED's white channel (RGBW)
# into the RGB approximation shown in the aura; the underlying provider
# data (four raw channels) is never altered, only this visualization.
_WHITE_BLEND_WEIGHT = 0.6


def normalize_wled_live_state(wled_state: Any, fixtures: list[Fixture]) -> dict[str, FixtureLiveState]:
    """Build one :class:`FixtureLiveState` per ``wled``-bound fixture.

    ``wled_state``: the root WLED ``/json/state`` payload (device ``on``/
    ``bri`` plus a ``seg`` array), exactly what
    ``DiscoveryFetchers.fetch_wled_state`` already returns.
    """
    if not isinstance(wled_state, dict):
        return {}
    device_on = wled_state.get("on")
    external_owner = _external_owner(wled_state)
    segments_by_id = _index_segments(wled_state)
    out: dict[str, FixtureLiveState] = {}
    for fixture in fixtures:
        binding = fixture.binding
        if binding is None or getattr(binding, "provider", None) != "wled":
            continue
        segment_ids = list(getattr(binding, "segment_ids", None) or [])
        if not segment_ids:
            # Whole-device binding: no segment ids to target, so the device
            # itself is the "segment" (mirrors the renderer's write path).
            segments = [seg for seg in segments_by_id.values()]
        else:
            segments = [segments_by_id[i] for i in segment_ids if i in segments_by_id]
        if not segments:
            out[fixture.id] = FixtureLiveState(
                fixture_id=fixture.id,
                provider="wled",
                available=False,
                state_kind="unavailable",
                detail="segment(s) not present in the last WLED state read",
            )
            continue
        out[fixture.id] = _normalize_fixture(fixture.id, device_on, segments, external_owner)
    return out


def _normalize_fixture(fixture_id: str, device_on: Any, segments: list[dict], external_owner: dict | None = None) -> FixtureLiveState:
    segment_on = any(seg.get("on", True) for seg in segments)
    on = bool(device_on) and segment_on if isinstance(device_on, bool) else segment_on

    brightness = _normalized_brightness(segments[0].get("bri"))
    dynamic = any(isinstance(seg.get("fx"), int) and seg.get("fx") != 0 for seg in segments)

    colors: list[str] = []
    for seg in segments:
        for hexed in _segment_colors(seg):
            if hexed not in colors:
                colors.append(hexed)
            if len(colors) >= _MAX_DISPLAY_COLORS:
                break
        if len(colors) >= _MAX_DISPLAY_COLORS:
            break

    if dynamic:
        return FixtureLiveState(
            fixture_id=fixture_id, provider="wled", available=True, on=on, brightness=brightness,
            color_mode="dynamic", display_colors=colors, dynamic=True, state_kind="configured_dynamic",
            external_owner=external_owner,
            detail="configured effect colors; WLED does not report per-frame LED color here",
        )
    return FixtureLiveState(
        fixture_id=fixture_id, provider="wled", available=True, on=on, brightness=brightness,
        color_mode="rgb" if colors else "none", display_colors=colors, dynamic=False, state_kind="live",
        external_owner=external_owner,
    )


def _external_owner(wled_state: dict) -> dict | None:
    """Realtime-owner evidence from the state root (R5 §3.3 seam).

    ``lor`` (live override): 0 = none, non-zero = a realtime source (e.g.
    hyperHDR streaming) currently owns the device. ``mainseg`` names the
    segment a realtime stream targets. Surfaced read-only; the render path
    never arbitrates on its own."""
    lor = wled_state.get("lor")
    if not isinstance(lor, int) or isinstance(lor, bool) or lor == 0:
        return None
    mainseg = wled_state.get("mainseg")
    owner = {"controller": "wled", "lor": lor}
    if isinstance(mainseg, int) and not isinstance(mainseg, bool):
        owner["mainseg"] = mainseg
    return owner


def _segment_colors(segment: dict) -> list[str]:
    col = segment.get("col")
    if not isinstance(col, list):
        return []
    out: list[str] = []
    for channels in col:
        if not isinstance(channels, list) or len(channels) < 3:
            continue
        out.append(_channels_to_hex(channels))
    return out


def _channels_to_hex(channels: list) -> str:
    r, g, b = (float(c) for c in channels[:3])
    if len(channels) >= 4 and isinstance(channels[3], (int, float)):
        blend = float(channels[3]) * _WHITE_BLEND_WEIGHT
        r = min(255.0, r + blend)
        g = min(255.0, g + blend)
        b = min(255.0, b + blend)
    return rgb_to_hex((r, g, b))


def _normalized_brightness(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0, min(100, round(float(value) / 255.0 * 100.0)))


def _index_segments(wled_state: dict) -> dict[int, dict]:
    seg = wled_state.get("seg")
    if not isinstance(seg, list):
        return {}
    out: dict[int, dict] = {}
    for index, entry in enumerate(seg):
        if not isinstance(entry, dict):
            continue
        seg_id = entry.get("id")
        out[seg_id if isinstance(seg_id, int) else index] = entry
    return out
