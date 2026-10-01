"""Classify Home Assistant aggregate/group lights vs atomic fixtures.

Scene Studio fixtures are independently addressable endpoints. An HA Light
Group helper (or Hue grouped-light entity exposed through HA) whose
``attributes.entity_id`` is a list of ``light.*`` members is an aggregate,
not a physical fixture. Discovery must not silently adopt those as
``available_unbound`` candidates.

Positive evidence is the member list itself — never ``device_id is None``
alone, because legitimate helper/template lights can also lack a device.
An explicit ignored-entity list is a narrow fallback for aggregates that
do not publish members.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "ha_group_member_ids",
    "is_aggregate_observation",
    "is_ha_aggregate_light",
]


def ha_group_member_ids(attributes: dict | None) -> list[str] | None:
    """Return the ``light.*`` member list when ``attributes.entity_id`` is one.

    ``None`` means this state is not a group-of-lights. A string ``entity_id``
    (the entity's own id) is not a member list.
    """
    if not isinstance(attributes, dict):
        return None
    raw = attributes.get("entity_id")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        return None
    members: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.startswith("light."):
            return None
        if item not in members:
            members.append(item)
    return members or None


def is_ha_aggregate_light(
    entity_id: str,
    attributes: dict | None,
    *,
    ignored_entity_ids: Iterable[str] = (),
) -> bool:
    """True when this HA light is an aggregate helper or an explicit ignore."""
    ignored = {item for item in ignored_entity_ids if isinstance(item, str) and item}
    if entity_id in ignored:
        return True
    return ha_group_member_ids(attributes) is not None


def is_aggregate_observation(observation: Any) -> bool:
    """True when a discovery observation was marked as an HA aggregate."""
    metadata = getattr(observation, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("ha_aggregate"))
