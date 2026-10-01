"""WLED discovery observations (Workstream A2).

Parses already-fetched WLED ``/json/info`` and ``/json/state`` payloads
into :class:`DiscoveryObservation` values. Transport-free.

Identity rules (frozen contracts §6):

- ``device_id`` comes from the provider-stable MAC address in the info
  payload (keys tried in order: ``mac``, ``esp_mac``). Separators and
  case are normalized away (``AA:BB:CC:DD:EE:FF`` -> ``aabbccddeeff``).
  If no MAC is reported, the documented fallback is a content-addressed
  id derived from the info payload (SHA-256 of its canonical JSON). The
  fallback is stable for an identical recorded payload but is NOT stable
  across firmware/configuration changes — MAC is the only real identity.
- The endpoint/IP is carried only as ``endpoint_hint``; it never
  participates in identity.
- One observation per REAL segment: entries in ``state["seg"]`` that
  carry a segment index and have ``stop > start``. Segment indexes are
  the device-reported ``id`` (positional index as fallback), never
  parsed from entity names.
- ``provider_resource_id`` = ``"{device_id}:seg:{index}"``.
- ``dynamic_native`` is true when the device/segments report effect
  support (``fx`` present), matching the ``Capabilities`` contract
  (WLED executes effects natively without a fallback animator).
- ``color_xy`` stays false: WLED natively speaks RGB; xy conversion is
  renderer work, not a provider capability.

Effect/palette catalogs (renderer-coverage review round):

- The device's ``effects`` and ``palettes`` arrays (``/json/effects`` and
  ``/json/palettes``) are captured order-preserving into
  ``capabilities.effects`` / ``capabilities.palettes`` — list position is
  the provider ``fx``/``pal`` id, which lets the renderer resolve scene
  effect/palette names to indexes without runtime calls.
- Sources, in order: a full ``/json`` state payload carrying root
  ``effects``/``palettes`` arrays wins; otherwise the explicit
  ``effects``/``palettes`` arguments are used (for the split
  ``/json/state`` + ``/json/effects`` fetch pattern).
- ``capabilities.cct`` mirrors ``info.leds.cct`` (relative white-channel
  color-temperature support) when the info payload reports it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..domain.capabilities import Capabilities
from ..domain.discovery import DiscoveryObservation

_MAC_KEYS = ("mac", "esp_mac")


def derive_wled_device_id(wled_info: dict) -> str:
    """Provider-stable WLED device identity (MAC-based, documented fallback)."""
    if isinstance(wled_info, dict):
        for key in _MAC_KEYS:
            mac = wled_info.get(key)
            normalized = _normalize_mac(mac)
            if normalized:
                return normalized
        # Documented fallback: content-addressed id over the info payload.
        # Stable per identical payload; not stable across reconfigurations.
        canonical = json.dumps(wled_info, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"wled-{digest[:12]}"
    return "wled-unknown"


def _normalize_mac(mac: Any) -> str:
    if not isinstance(mac, str):
        return ""
    cleaned = "".join(ch for ch in mac.casefold() if ch in "0123456789abcdef")
    return cleaned


def _name_catalog(value: Any) -> list[str] | None:
    """A string list (``/json/effects`` / ``/json/palettes``) or ``None``."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _info_cct(wled_info: dict) -> bool:
    leds = wled_info.get("leds") if isinstance(wled_info, dict) else None
    if not isinstance(leds, dict):
        return False
    return leds.get("cct") is True


def build_wled_observations(
    wled_info: dict,
    wled_state: dict,
    endpoint_hint: str | None,
    effects: list[str] | None = None,
    palettes: list[str] | None = None,
) -> list[DiscoveryObservation]:
    """Build one observation per real WLED segment on the device.

    ``effects`` / ``palettes`` accept the device's ``/json/effects`` and
    ``/json/palettes`` arrays (order preserved — index = id). When the
    state payload is a full ``/json`` document with root arrays, those
    take precedence over the arguments.
    """
    device_id = derive_wled_device_id(wled_info)
    state = wled_state if isinstance(wled_state, dict) else {}
    segments = state.get("seg")
    if not isinstance(segments, list):
        segments = []

    # Catalogs: root arrays of a full /json payload win over the arguments.
    effects_catalog = _name_catalog(state.get("effects")) or _name_catalog(effects) or []
    palettes_catalog = _name_catalog(state.get("palettes")) or _name_catalog(palettes) or []
    cct_supported = _info_cct(wled_info) if isinstance(wled_info, dict) else False

    state_has_fx = "fx" in state
    state_has_bri = "bri" in state

    observations: list[DiscoveryObservation] = []
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        index = segment.get("id")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            index = position  # documented fallback: positional index
        start = segment.get("start")
        stop = segment.get("stop")
        if not isinstance(start, int) or isinstance(start, bool):
            start = 0
        if not isinstance(stop, int) or isinstance(stop, bool):
            stop = 0
        if stop <= start:
            continue  # not a real segment (unused/empty range)

        name = segment.get("n")
        if not isinstance(name, str) or not name:
            name = f"Segment {index}"

        segment_has_fx = "fx" in segment
        segment_has_bri = "bri" in segment
        capabilities = Capabilities(
            on_off=True,  # a WLED segment can always be switched
            brightness=state_has_bri or segment_has_bri,
            color_xy=False,  # WLED speaks RGB natively; xy is renderer work
            effects=effects_catalog,
            palettes=palettes_catalog,
            cct=cct_supported,
            dynamic_native=state_has_fx or segment_has_fx,
        )
        observations.append(
            DiscoveryObservation(
                provider="wled",
                provider_resource_id=f"{device_id}:seg:{index}",
                name=name,
                location_hint=None,
                capabilities=capabilities,
                endpoint_hint=endpoint_hint,
                metadata={
                    "device_id": device_id,
                    "segment_start": start,
                    "segment_stop": stop,
                },
            )
        )
    observations.sort(key=lambda obs: obs.provider_resource_id)
    return observations
