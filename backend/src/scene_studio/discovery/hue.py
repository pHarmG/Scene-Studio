"""Hue CLIP v2 discovery observations (Workstream A2).

Parses an already-fetched ``GET /clip/v2/resource/light`` payload (shape
``{"data": [ ...light resources... ]}``) into :class:`DiscoveryObservation`
values. Transport-free: no network calls happen here.

Semantics (frozen contracts §6):

- Stable resource id: CLIP v2 ``id`` (UUID). ``id_v1`` is retained in
  metadata as a cross-reference only, never as identity.
- Room membership is captured as ``location_hint`` and is NEVER used to
  filter the observation set. Lights without a room are still reported.
- ``dynamic_native`` is true when the device reports native dynamic
  execution support: any ``dynamics.status_values`` entry besides
  ``none`` (e.g. ``dynamic_palette``) or any effect besides
  ``no_effect`` (on-device animation, matching the ``Capabilities``
  contract "without a fallback animator").
- HA state enrichment is name-based metadata only; it never becomes
  identity.
"""

from __future__ import annotations

from typing import Any

from ..domain.capabilities import Capabilities, ColorTempRange, GradientCapability
from ..domain.discovery import DiscoveryObservation
from ..domain.fixtures import DeviceProfile

# Gradient default when a device reports a gradient but no explicit size.
DEFAULT_GRADIENT_POINTS = 5

_COLOR_KEYS = ("product_name", "product_id", "manufacturer_name", "model_id", "software_version")


def build_hue_observations(
    clip_lights: dict,
    clip_rooms: dict | None = None,
    ha_states: dict | None = None,
    clip_devices: dict | None = None,
) -> list[DiscoveryObservation]:
    """Build observations from Hue CLIP v2 light (and optional room) payloads.

    ``clip_lights``: ``{"data": [ ...CLIP v2 light resources... ]}``.
    ``clip_rooms``: optional ``{"data": [ ...room resources... ]}``; room
    membership becomes ``location_hint`` (room metadata name) plus authoritative
    ``hue_group_id`` provider metadata. Never filters.
    ``ha_states``: optional ``{entity_id: {attributes...}}`` (or full HA
    state dicts with an ``attributes`` object). Entities whose friendly
    name equals the light name are recorded in ``metadata["ha_entity_ids"]``.
    """
    room_by_device_rid = _room_index(clip_rooms)
    ha_by_name = _ha_name_index(ha_states)
    devices_by_rid = _device_index(clip_devices)

    observations: list[DiscoveryObservation] = []
    for light in _iter_resources(clip_lights):
        if not isinstance(light, dict):
            continue
        resource_id = light.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            continue  # a light without a stable id cannot participate
        metadata_block = light.get("metadata") if isinstance(light.get("metadata"), dict) else {}
        name = metadata_block.get("name")
        if not isinstance(name, str) or not name:
            name = resource_id

        owner_rid = _owner_device_rid(light)
        device = devices_by_rid.get(owner_rid, {})
        group = room_by_device_rid.get(owner_rid) if owner_rid else None
        location_hint = group["name"] if group else None

        metadata: dict[str, Any] = {}
        archetype = metadata_block.get("archetype")
        if isinstance(archetype, str) and archetype:
            metadata["archetype"] = archetype
        id_v1 = light.get("id_v1")
        if isinstance(id_v1, str) and id_v1:
            metadata["hue_id_v1"] = id_v1
        product_data = light.get("product_data") if isinstance(light.get("product_data"), dict) else {}
        for key in _COLOR_KEYS:
            value = product_data.get(key)
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
        linked = _match_ha_entities(name, ha_by_name)
        if linked:
            metadata["ha_entity_ids"] = linked
        if owner_rid:
            metadata["hue_device_rid"] = owner_rid
        if group:
            metadata["hue_group_id"] = group["id"]
            metadata["hue_group_type"] = group["type"]

        observations.append(
            DiscoveryObservation(
                provider="hue_v2",
                provider_resource_id=resource_id,
                name=name,
                location_hint=location_hint,
                capabilities=_build_capabilities(light),
                endpoint_hint=None,  # bridge-level, not per-light
                device_profile=_device_profile(device, product_data),
                metadata=metadata,
            )
        )
    observations.sort(key=lambda obs: obs.provider_resource_id)
    return observations


def _device_index(payload: dict | None) -> dict[str, dict]:
    return {item["id"]: item for item in _iter_resources(payload or {}) if isinstance(item, dict) and isinstance(item.get("id"), str)}


def _device_profile(device: dict, fallback_product_data: dict) -> DeviceProfile | None:
    """Use optional device resources first; light product data is fallback evidence."""
    product_data = device.get("product_data") if isinstance(device.get("product_data"), dict) else fallback_product_data
    metadata = device.get("metadata") if isinstance(device.get("metadata"), dict) else {}
    manufacturer = product_data.get("manufacturer_name") or metadata.get("manufacturer_name")
    model = product_data.get("model_id") or metadata.get("model_id")
    product_name = product_data.get("product_name") or metadata.get("product_name")
    if not any(isinstance(value, str) and value for value in (manufacturer, model, product_name)):
        return None
    return DeviceProfile(
        manufacturer=manufacturer if isinstance(manufacturer, str) else None,
        model=model if isinstance(model, str) else None,
        product_name=product_name if isinstance(product_name, str) else None,
        protocol="hue_zigbee",
        source="hue_device" if device else "hue_light_product_data",
    )


def _iter_resources(payload: dict) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    return data


def _owner_device_rid(light: dict) -> str | None:
    owner = light.get("owner")
    if isinstance(owner, dict):
        rid = owner.get("rid")
        if isinstance(rid, str) and rid:
            return rid
    return None


def _room_index(clip_rooms: dict | None) -> dict[str, dict[str, str]]:
    """Map device rid -> authoritative CLIP v2 room/zone topology."""
    index: dict[str, dict[str, str]] = {}
    if not isinstance(clip_rooms, dict):
        return index
    data = clip_rooms.get("data")
    if not isinstance(data, list):
        return index
    for room in data:
        if not isinstance(room, dict):
            continue
        group_id = room.get("id")
        group_type = room.get("type", "room")
        room_metadata = room.get("metadata") if isinstance(room.get("metadata"), dict) else {}
        room_name = room_metadata.get("name")
        children = room.get("children")
        if (
            not isinstance(group_id, str) or not group_id
            or group_type not in ("room", "zone")
            or not isinstance(room_name, str) or not isinstance(children, list)
        ):
            continue
        for child in children:
            if isinstance(child, dict) and child.get("rtype") == "device" and isinstance(child.get("rid"), str):
                index[child["rid"]] = {"id": group_id, "type": group_type, "name": room_name}
    return index


def _ha_name_index(ha_states: dict | None) -> dict[str, list[str]]:
    """Map normalized friendly name -> sorted HA light entity ids."""
    index: dict[str, list[str]] = {}
    if not isinstance(ha_states, dict):
        return index
    for entity_id, raw_state in ha_states.items():
        if not isinstance(entity_id, str) or not entity_id.startswith("light."):
            continue
        attributes = _ha_attributes(raw_state)
        friendly = attributes.get("friendly_name")
        if not isinstance(friendly, str) or not friendly:
            continue
        index.setdefault(_normalize_name(friendly), []).append(entity_id)
    for key in index:
        index[key] = sorted(index[key])
    return index


def _ha_attributes(raw_state: Any) -> dict:
    """Accept both ``{attributes...}`` and full HA state (``{attributes: {...}}``)."""
    if isinstance(raw_state, dict) and isinstance(raw_state.get("attributes"), dict):
        return raw_state["attributes"]
    if isinstance(raw_state, dict):
        return raw_state
    return {}


def _match_ha_entities(name: str, ha_by_name: dict[str, list[str]]) -> list[str]:
    return list(ha_by_name.get(_normalize_name(name), ()))


def _normalize_name(name: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name).casefold().split())


def _build_capabilities(light: dict) -> Capabilities:
    color_temp = _build_color_temp(light)
    gradient = _build_gradient(light)
    effects = _extract_effects(light)
    dynamics = _string_values(light.get("dynamics"), "status_values")
    dynamic_native = any(value != "none" for value in dynamics) or any(
        value != "no_effect" for value in effects
    )
    return Capabilities(
        on_off=True,
        brightness="dimming" in light,
        color_xy="color" in light,
        color_temp=color_temp,
        gradient=gradient,
        effects=effects,
        dynamic_native=dynamic_native,
        extra={},
    )


def _build_color_temp(light: dict) -> ColorTempRange | None:
    block = light.get("color_temperature")
    if not isinstance(block, dict):
        return None
    schema = block.get("mirek_schema")
    if not isinstance(schema, dict):
        return None
    mirek_min = schema.get("mirek_minimum")
    mirek_max = schema.get("mirek_maximum")
    if not isinstance(mirek_min, int) or not isinstance(mirek_max, int):
        return None
    # Clamp into the model's validated range; Hue mirek schemas fall inside.
    mirek_min = max(100, min(1000, mirek_min))
    mirek_max = max(100, min(1000, mirek_max))
    if mirek_min > mirek_max:
        mirek_min, mirek_max = mirek_max, mirek_min
    return ColorTempRange(mirek_min=mirek_min, mirek_max=mirek_max)


def _build_gradient(light: dict) -> GradientCapability | None:
    block = light.get("gradient")
    if not isinstance(block, dict):
        return None
    points_capable = block.get("points_capable")
    if isinstance(points_capable, int) and not isinstance(points_capable, bool) and points_capable > 0:
        max_points = points_capable
    else:
        points = block.get("points")
        max_points = len(points) if isinstance(points, list) and points else DEFAULT_GRADIENT_POINTS
    # Keep the value inside the Capabilities model's validated range (1..64).
    max_points = max(1, min(64, max_points))
    return GradientCapability(max_points=max_points)


def _extract_effects(light: dict) -> list[str]:
    block = light.get("effects")
    values = _string_values(block, "status_values") or _string_values(block, "effect_values")
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _string_values(block: Any, key: str) -> list[str]:
    if not isinstance(block, dict):
        return []
    raw = block.get(key)
    if not isinstance(raw, list):
        return []
    return [value for value in raw if isinstance(value, str)]
