"""Provider bindings — the resource currently fulfilling a logical fixture.

A binding is provider-specific and separate from fixture identity (master
plan §2.4/§6.2). Endpoints/IPs are address hints, never identity. Bindings
are only mutated through explicit registry actions (`fixture.rebind`,
discovery candidate acceptance) — never silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from .serde import (
    ValidationError,
    join,
    optional_str,
    optional_str_list,
    reject_unknown_keys,
    require_mapping,
    require_str,
    require_str_list,
)

PROVIDER_HUE_V2 = "hue_v2"
PROVIDER_WLED = "wled"
PROVIDER_HA_LIGHT = "ha_light"
KNOWN_PROVIDERS = (PROVIDER_HUE_V2, PROVIDER_WLED, PROVIDER_HA_LIGHT)


@dataclass
class HueBinding:
    """A Hue CLIP v2 resource (light or grouped light) on a specific bridge."""

    bridge_id: str
    resource_id: str
    resource_type: str = "light"  # "light" | "grouped_light"
    ha_entity_id: str | None = None
    # Authoritative CLIP v2 topology; distinct from a logical Fixture group.
    hue_group_id: str | None = None
    hue_group_type: str | None = None  # "room" | "zone"
    provider: str = PROVIDER_HUE_V2

    def to_dict(self) -> dict:
        out = {
            "provider": self.provider,
            "bridge_id": self.bridge_id,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
        }
        if self.ha_entity_id:
            out["ha_entity_id"] = self.ha_entity_id
        if self.hue_group_id:
            out["hue_group_id"] = self.hue_group_id
        if self.hue_group_type:
            out["hue_group_type"] = self.hue_group_type
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "binding") -> "HueBinding":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"provider", "bridge_id", "resource_id", "resource_type", "ha_entity_id", "hue_group_id", "hue_group_type"}, path)
        resource_type = data.get("resource_type", "light")
        if resource_type not in ("light", "grouped_light"):
            raise ValidationError(join(path, "resource_type"), "must be 'light' or 'grouped_light'")
        hue_group_id = optional_str(data, "hue_group_id", path, max_length=64)
        hue_group_type = optional_str(data, "hue_group_type", path, max_length=16)
        if hue_group_type is not None and hue_group_type not in ("room", "zone"):
            raise ValidationError(join(path, "hue_group_type"), "must be 'room' or 'zone'")
        if hue_group_type is not None and hue_group_id is None:
            raise ValidationError(join(path, "hue_group_type"), "requires hue_group_id")
        return cls(
            bridge_id=require_str(data, "bridge_id", join(path, "bridge_id"), max_length=64),
            resource_id=require_str(data, "resource_id", join(path, "resource_id"), max_length=64),
            resource_type=resource_type,
            ha_entity_id=optional_str(data, "ha_entity_id", path, max_length=128),
            hue_group_id=hue_group_id,
            hue_group_type=hue_group_type,
        )


@dataclass
class WledBinding:
    """One or more segments on a WLED device.

    `device_id` is the provider-stable device identity (e.g. MAC-derived);
    discovery populates it. `endpoint_hint` is informational only.
    An empty `segment_ids` list means the whole device.
    """

    device_id: str
    segment_ids: list[int] = field(default_factory=list)
    ha_entity_ids: list[str] = field(default_factory=list)
    endpoint_hint: str | None = None
    provider: str = PROVIDER_WLED

    def to_dict(self) -> dict:
        out = {
            "provider": self.provider,
            "device_id": self.device_id,
            "segment_ids": list(self.segment_ids),
        }
        if self.ha_entity_ids:
            out["ha_entity_ids"] = list(self.ha_entity_ids)
        if self.endpoint_hint:
            out["endpoint_hint"] = self.endpoint_hint
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "binding") -> "WledBinding":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"provider", "device_id", "segment_ids", "ha_entity_ids", "endpoint_hint"}, path)
        segments = data.get("segment_ids", [])
        if not isinstance(segments, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 63 for item in segments
        ):
            raise ValidationError(join(path, "segment_ids"), "expected a list of segment indexes (0..63)")
        return cls(
            device_id=require_str(data, "device_id", join(path, "device_id"), max_length=64),
            segment_ids=list(segments),
            ha_entity_ids=optional_str_list(data, "ha_entity_ids", path, max_length=64),
            endpoint_hint=optional_str(data, "endpoint_hint", path, max_length=255),
        )


@dataclass
class HaLightBinding:
    """An ordinary Home Assistant light controlled at HA service level."""

    ha_entity_id: str
    provider: str = PROVIDER_HA_LIGHT

    def to_dict(self) -> dict:
        return {"provider": self.provider, "ha_entity_id": self.ha_entity_id}

    @classmethod
    def from_dict(cls, data: Any, path: str = "binding") -> "HaLightBinding":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"provider", "ha_entity_id"}, path)
        return cls(ha_entity_id=require_str(data, "ha_entity_id", join(path, "ha_entity_id"), max_length=128))


Binding = Union[HueBinding, WledBinding, HaLightBinding]


def binding_ha_entity_ids(binding: Binding | None) -> list[str]:
    """Flatten ``ha_entity_id`` / ``ha_entity_ids`` from any binding.

    Order-preserving and de-duplicated. Missing/blank values are skipped.
    Hue and HA-light bindings expose a single id; WLED may expose several
    segment entities. A fixture with no HA representation returns ``[]``.
    """
    if binding is None:
        return []
    seen: list[str] = []

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        entity_id = value.strip()
        if entity_id and entity_id not in seen:
            seen.append(entity_id)

    add(getattr(binding, "ha_entity_id", None))
    for item in getattr(binding, "ha_entity_ids", None) or []:
        add(item)
    return seen


def binding_from_dict(data: Any, path: str = "binding") -> Binding:
    """Parse the tagged binding union on its `provider` discriminator."""
    data = require_mapping(data, path)
    provider = data.get("provider")
    if provider == PROVIDER_HUE_V2:
        return HueBinding.from_dict(data, path)
    if provider == PROVIDER_WLED:
        return WledBinding.from_dict(data, path)
    if provider == PROVIDER_HA_LIGHT:
        return HaLightBinding.from_dict(data, path)
    raise ValidationError(
        join(path, "provider"),
        f"unknown provider {provider!r}; must be one of: {', '.join(KNOWN_PROVIDERS)}",
    )
