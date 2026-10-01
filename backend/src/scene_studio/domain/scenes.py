"""Scene v2 — the canonical provider-neutral scene intent (master plan §6.4).

A scene stores what the lighting should do, targeting stable logical IDs.
Provider UUIDs/IPs never appear as identity. Provider-specific escape hatches
must live under `provider_ext` keyed by provider name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .identities import validate_id
from .serde import (
    ValidationError,
    join,
    optional_float,
    optional_hex_color,
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_float,
    require_hex_color_list,
    require_mapping,
    require_str,
    require_str_list,
    require_timestamp,
)

SCENE_SCHEMA_VERSION = 2
KNOWN_PROVIDERS = ("hue_v2", "wled", "ha_light")


class MotionMode(str, Enum):
    STATIC = "static"            # no animation
    PALETTE_CYCLE = "palette_cycle"  # animate the scene palette
    EFFECT = "effect"            # per-fixture native effect named in fixture_states


class MotionStrategy(str, Enum):
    AUTO = "auto"                  # renderer picks the best available execution
    NATIVE_PREFERRED = "native_preferred"  # only native; report unsupported otherwise
    STATIC = "static"              # force static rendering (no animation)


@dataclass
class Motion:
    """Dynamic intent. `speed` is one normalized 0.0..1.0 user-facing scale;
    renderers own translation into native provider ranges (master plan §12)."""

    mode: MotionMode = MotionMode.STATIC
    speed: float = 0.5
    strategy: MotionStrategy = MotionStrategy.AUTO

    def to_dict(self) -> dict:
        return {"mode": self.mode.value, "speed": self.speed, "strategy": self.strategy.value}

    @classmethod
    def from_dict(cls, data: Any, path: str = "scene.motion") -> "Motion":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"mode", "speed", "strategy"}, path)
        try:
            mode = MotionMode(data["mode"]) if "mode" in data and data["mode"] is not None else MotionMode.STATIC
            strategy = (
                MotionStrategy(data["strategy"])
                if "strategy" in data and data["strategy"] is not None
                else MotionStrategy.AUTO
            )
        except ValueError:
            allowed_modes = ", ".join(m.value for m in MotionMode)
            allowed_strategies = ", ".join(s.value for s in MotionStrategy)
            raise ValidationError(
                path, f"mode must be one of: {allowed_modes}; strategy must be one of: {allowed_strategies}"
            ) from None
        speed = require_float(data, "speed", path, minimum=0.0, maximum=1.0) if "speed" in data else 0.5
        return cls(mode=mode, speed=speed, strategy=strategy)


@dataclass
class FixtureState:
    """What one fixture should do. All fields optional; at least one must be
    set (empty overrides are rejected as authoring mistakes)."""

    on: bool | None = None
    brightness: float | None = None  # 0..100
    color: str | None = None         # canonical #rrggbb
    palette_index: int | None = None  # 0..23 pointer into scene.palette
    color_temp_mirek: int | None = None
    gradient: list[str] | None = None  # ordered hex list for gradient fixtures
    effect: str | None = None        # canonical/provider effect name
    transition_ms: int | None = None
    provider_ext: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        from .serde import drop_none

        out = drop_none(
            {
                "on": self.on,
                "brightness": self.brightness,
                "color": self.color,
                "palette_index": self.palette_index,
                "color_temp_mirek": self.color_temp_mirek,
            }
        )
        if self.gradient:
            out["gradient"] = list(self.gradient)
        if self.effect:
            out["effect"] = self.effect
        if self.transition_ms is not None:
            out["transition_ms"] = self.transition_ms
        if self.provider_ext:
            out["provider_ext"] = {k: dict(v) for k, v in self.provider_ext.items()}
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "fixture_state") -> "FixtureState":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {
                "on",
                "brightness",
                "color",
                "palette_index",
                "color_temp_mirek",
                "gradient",
                "effect",
                "transition_ms",
                "provider_ext",
            },
            path,
        )
        gradient = None
        if data.get("gradient") is not None:
            gradient = require_hex_color_list({"gradient": data["gradient"]}, "gradient", path, max_length=64)
        provider_ext = _require_provider_ext(data, path)
        state = cls(
            on=data.get("on") if isinstance(data.get("on"), bool) else None,
            brightness=optional_float(data, "brightness", path, minimum=0.0, maximum=100.0),
            color=optional_hex_color(data, "color", path),
            palette_index=optional_int(data, "palette_index", path, minimum=0, maximum=23),
            color_temp_mirek=optional_int(data, "color_temp_mirek", path, minimum=100, maximum=1000),
            gradient=gradient,
            effect=optional_str(data, "effect", path, max_length=64),
            transition_ms=optional_int(data, "transition_ms", path, minimum=0, maximum=60000),
            provider_ext=provider_ext,
        )
        if "on" in data and not isinstance(data["on"], bool):
            raise ValidationError(join(path, "on"), "expected a boolean")
        if state.color is not None and state.palette_index is not None:
            raise ValidationError(path, "palette_index and color cannot both be set")
        if all(
            value is None
            for value in (
                state.on,
                state.brightness,
                state.color,
                state.palette_index,
                state.color_temp_mirek,
                state.gradient,
                state.effect,
            )
        ) and not state.provider_ext:
            raise ValidationError(path, "fixture state must set at least one field")
        return state


def _require_provider_ext(data: dict, path: str) -> dict[str, dict]:
    raw = data.get("provider_ext")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValidationError(join(path, "provider_ext"), "expected an object")
    for provider in raw:
        if provider not in KNOWN_PROVIDERS:
            raise ValidationError(
                join(path, f"provider_ext.{provider}"),
                f"provider extension must be namespaced to one of: {', '.join(KNOWN_PROVIDERS)}",
            )
        if not isinstance(raw[provider], dict):
            raise ValidationError(join(path, f"provider_ext.{provider}"), "expected an object")
    return {provider: dict(payload) for provider, payload in raw.items()}


@dataclass
class Scene:
    schema_version: int = SCENE_SCHEMA_VERSION
    id: str = ""
    name: str = ""
    target_ids: list[str] = field(default_factory=list)
    palette: list[str] = field(default_factory=list)  # ordered color intent (static pointers + dynamic cycle)
    brightness: float | None = None  # default 0..100 for fixtures without override
    motion: Motion = field(default_factory=Motion)
    fixture_states: dict[str, FixtureState] = field(default_factory=dict)
    default_state: FixtureState | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "target_ids": list(self.target_ids),
        }
        if self.palette:
            out["palette"] = list(self.palette)
        if self.brightness is not None:
            out["brightness"] = self.brightness
        out["motion"] = self.motion.to_dict()
        if self.fixture_states:
            out["fixture_states"] = {k: v.to_dict() for k, v in self.fixture_states.items()}
        if self.default_state is not None:
            out["default_state"] = self.default_state.to_dict()
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "scene") -> "Scene":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {
                "schema_version",
                "id",
                "name",
                "target_ids",
                "palette",
                "brightness",
                "motion",
                "fixture_states",
                "default_state",
                "metadata",
            },
            path,
        )
        version = data.get("schema_version")
        if version != SCENE_SCHEMA_VERSION:
            raise ValidationError(join(path, "schema_version"), f"expected {SCENE_SCHEMA_VERSION}, got {version!r}")
        scene_id = validate_id(data.get("id"), "scene_id", join(path, "id"))
        target_ids = require_str_list(data, "target_ids", path, min_length=1)
        for index, target_id in enumerate(target_ids):
            validate_id(target_id, "target_id", join(path, f"target_ids.{index}"))
        palette = require_hex_color_list(data, "palette", path, min_length=0, max_length=24) if data.get("palette") else []
        motion = Motion.from_dict(data.get("motion", {}), join(path, "motion"))
        raw_states = data.get("fixture_states", {})
        if not isinstance(raw_states, dict):
            raise ValidationError(join(path, "fixture_states"), "expected an object keyed by fixture_id")
        fixture_states = {
            validate_id(fixture_id, "fixture_id", join(path, f"fixture_states.{fixture_id}")): FixtureState.from_dict(
                state, join(path, f"fixture_states.{fixture_id}")
            )
            for fixture_id, state in raw_states.items()
        }
        default_state = None
        if data.get("default_state") is not None:
            default_state = FixtureState.from_dict(data["default_state"], join(path, "default_state"))
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValidationError(join(path, "metadata"), "expected an object")
        if metadata.get("archived_at") is not None:
            require_timestamp(metadata, "archived_at", join(path, "metadata"))
        if metadata.get("migrated_from_v1") is not None:
            migrated = metadata["migrated_from_v1"]
            if not isinstance(migrated, dict):
                raise ValidationError(join(path, "metadata.migrated_from_v1"), "expected an object")
        return cls(
            schema_version=SCENE_SCHEMA_VERSION,
            id=scene_id,
            name=require_str(data, "name", join(path, "name"), max_length=128),
            target_ids=target_ids,
            palette=palette,
            brightness=optional_float(data, "brightness", path, minimum=0.0, maximum=100.0),
            motion=motion,
            fixture_states=fixture_states,
            default_state=default_state,
            metadata=dict(metadata),
        )
