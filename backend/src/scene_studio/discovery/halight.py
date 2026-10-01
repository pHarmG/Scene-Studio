"""Home Assistant light entity discovery observations (Workstream A2).

Parses an already-fetched HA light-state mapping ``{entity_id: {...}}``
into :class:`DiscoveryObservation` values. Transport-free.

- ``provider_resource_id`` is the HA entity id itself (``light.xxx``);
  for ``ha_light`` bindings this IS the stable provider identity.
- ``name`` comes from ``friendly_name`` and falls back to the entity id.
- Capability flags derive from ``supported_color_modes`` / present
  attributes; ``dynamic_native`` stays false (an ordinary HA light does
  not execute dynamic scenes natively).
"""

from __future__ import annotations

from typing import Any

from ..domain.capabilities import Capabilities, ColorTempRange
from ..domain.discovery import DiscoveryObservation
from ..domain.fixtures import DeviceProfile
from .aggregates import ha_group_member_ids, is_ha_aggregate_light

_XY_MODES = frozenset({"hs", "rgb", "rgbw", "rgbww", "xy"})
_COLOR_ATTR_KEYS = ("hs_color", "rgb_color", "xy_color", "rgbw_color", "rgbww_color")
_MODE_KEY = "supported_color_modes"


def build_halight_observations(
    ha_states: dict,
    device_metadata: dict | None = None,
    *,
    ignored_entity_ids: tuple[str, ...] | list[str] = (),
) -> list[DiscoveryObservation]:
    """Build observations from HA light entity states.

    Each value may be a bare attributes mapping or a full HA state object
    (``{"state": "on", "attributes": {...}}``); both are accepted.

    Aggregate HA light groups (member list in ``attributes.entity_id``, or
    an explicit ignored entity id) are still observed so a currently-bound
    helper can be flagged, but they carry ``metadata.ha_aggregate`` so
    discovery will not adopt them as ordinary unbound fixtures.
    """
    observations: list[DiscoveryObservation] = []
    if not isinstance(ha_states, dict):
        return observations
    for entity_id, raw_state in ha_states.items():
        if not isinstance(entity_id, str) or not entity_id.startswith("light."):
            continue
        attributes = _attributes_of(raw_state)
        name = attributes.get("friendly_name")
        if not isinstance(name, str) or not name:
            name = entity_id
        metadata = _build_metadata(attributes)
        if is_ha_aggregate_light(entity_id, attributes, ignored_entity_ids=ignored_entity_ids):
            metadata = dict(metadata)
            metadata["ha_aggregate"] = True
            members = ha_group_member_ids(attributes) or []
            if members:
                metadata["ha_group_members"] = members
        observations.append(
            DiscoveryObservation(
                provider="ha_light",
                provider_resource_id=entity_id,
                name=name,
                capabilities=_build_capabilities(attributes),
                device_profile=_device_profile((device_metadata or {}).get(entity_id), attributes),
                metadata=metadata,
            )
        )
    observations.sort(key=lambda obs: obs.observation_id)
    return observations


def _attributes_of(raw_state: Any) -> dict:
    if isinstance(raw_state, dict) and isinstance(raw_state.get("attributes"), dict):
        return raw_state["attributes"]
    if isinstance(raw_state, dict):
        return raw_state
    return {}


def _color_modes(attributes: dict) -> set[str]:
    raw = attributes.get(_MODE_KEY)
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {mode for mode in raw if isinstance(mode, str)}


def _build_metadata(attributes: dict) -> dict:
    modes = _color_modes(attributes)
    if not modes:
        return {}
    return {"supported_color_modes": sorted(modes)}


def _device_profile(enrichment: Any, attributes: dict) -> DeviceProfile | None:
    """Optional entity/device enrichment wins; state attributes remain a fallback."""
    source = enrichment if isinstance(enrichment, dict) else attributes
    manufacturer = source.get("manufacturer") or source.get("manufacturer_name")
    model = source.get("model") or source.get("model_id")
    integration = source.get("integration") or source.get("source")
    if not any(isinstance(value, str) and value for value in (manufacturer, model, integration)):
        return None
    return DeviceProfile(
        manufacturer=manufacturer if isinstance(manufacturer, str) else None,
        model=model if isinstance(model, str) else None,
        protocol=integration if isinstance(integration, str) else None,
        source="ha_device_metadata" if isinstance(enrichment, dict) else "ha_state_attributes",
    )


def _build_capabilities(attributes: dict) -> Capabilities:
    modes = _color_modes(attributes)
    brightness = "brightness" in attributes or bool(modes - {"onoff"})
    color_xy = bool(modes & _XY_MODES) or any(key in attributes for key in _COLOR_ATTR_KEYS)
    effects = [value for value in attributes.get("effect_list", []) if isinstance(value, str)]
    return Capabilities(
        on_off=True,
        brightness=brightness,
        color_xy=color_xy,
        color_temp=_build_color_temp(attributes, modes),
        effects=effects,
        dynamic_native=False,
        extra={},
    )


# HA's documented fallback mirek range when a light supports color_temp but
# does not report min_mireds/max_mireds (Home Assistant constants).
_HA_DEFAULT_MIREDS = (153, 500)


def _build_color_temp(attributes: dict, modes: set[str]) -> ColorTempRange | None:
    if "color_temp" not in modes and "color_temp" not in attributes and "color_temp_kelvin" not in attributes:
        return None
    mirek_min = attributes.get("min_mireds")
    mirek_max = attributes.get("max_mireds")
    if not isinstance(mirek_min, int) or not isinstance(mirek_max, int):
        # Supported but rangeless: use HA's own default range instead of
        # reporting a capability downgrade (R3 live finding, office_lights).
        mirek_min, mirek_max = _HA_DEFAULT_MIREDS

    mirek_min = max(100, min(1000, mirek_min))
    mirek_max = max(100, min(1000, mirek_max))
    if mirek_min > mirek_max:
        mirek_min, mirek_max = mirek_max, mirek_min
    return ColorTempRange(mirek_min=mirek_min, mirek_max=mirek_max)
