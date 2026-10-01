"""Provider-agnostic capability model (what a bound resource can actually do)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .serde import (
    ValidationError,
    join,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_list,
    require_mapping,
    require_str,
)


@dataclass
class ColorTempRange:
    """Manufacturer mirek range for color-temperature-capable lights."""

    mirek_min: int
    mirek_max: int

    def to_dict(self) -> dict:
        return {"mirek_min": self.mirek_min, "mirek_max": self.mirek_max}

    @classmethod
    def from_dict(cls, data: Any, path: str = "capabilities.color_temp") -> "ColorTempRange":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"mirek_min", "mirek_max"}, path)
        return cls(
            mirek_min=require_int(data, "mirek_min", path, minimum=100, maximum=1000),
            mirek_max=require_int(data, "mirek_max", path, minimum=100, maximum=1000),
        )


@dataclass
class GradientCapability:
    """Linear gradient support (e.g. Hue gradient light tubes)."""

    max_points: int

    def to_dict(self) -> dict:
        return {"max_points": self.max_points}

    @classmethod
    def from_dict(cls, data: Any, path: str = "capabilities.gradient") -> "GradientCapability":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"max_points"}, path)
        return cls(max_points=require_int(data, "max_points", path, minimum=1, maximum=64))


@dataclass
class Capabilities:
    """Normalized capability set of a bound resource.

    Name catalogs (`effects`, `palettes`) come from discovery and are ordered:
    list position is the provider's numeric id, so renderers can resolve a
    scene effect/palette name to a provider index without runtime calls.

    `extra` holds raw provider-reported capability data for Level-3 UI
    inspection. It must be sanitized on ingest and export — it must never
    carry credentials.
    """

    on_off: bool = True
    brightness: bool = False
    color_xy: bool = False
    color_temp: ColorTempRange | None = None
    gradient: GradientCapability | None = None
    effects: list[str] = field(default_factory=list)
    # Effect-palette catalog (e.g. WLED `/json/palettes`); list position is
    # the provider palette index so renderers can resolve names to ids.
    palettes: list[str] = field(default_factory=list)
    # WLED: relative color-temperature support via the white channel
    # (`info.leds.cct`). Non-RGB (tunable-white) only.
    cct: bool = False
    # Provider can execute dynamic scenes natively (Hue dynamic scenes,
    # WLED effects/playlists) without a fallback animator.
    dynamic_native: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "on_off": self.on_off,
            "brightness": self.brightness,
            "color_xy": self.color_xy,
        }
        if self.color_temp is not None:
            out["color_temp"] = self.color_temp.to_dict()
        if self.gradient is not None:
            out["gradient"] = self.gradient.to_dict()
        if self.effects:
            out["effects"] = list(self.effects)
        if self.palettes:
            out["palettes"] = list(self.palettes)
        if self.cct:
            out["cct"] = True
        out["dynamic_native"] = self.dynamic_native
        if self.extra:
            out["extra"] = dict(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "capabilities") -> "Capabilities":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {
                "on_off",
                "brightness",
                "color_xy",
                "color_temp",
                "gradient",
                "effects",
                "palettes",
                "cct",
                "dynamic_native",
                "extra",
            },
            path,
        )
        color_temp = None
        if data.get("color_temp") is not None:
            color_temp = ColorTempRange.from_dict(data["color_temp"], join(path, "color_temp"))
        gradient = None
        if data.get("gradient") is not None:
            gradient = GradientCapability.from_dict(data["gradient"], join(path, "gradient"))
        return cls(
            on_off=require_bool(data, "on_off", path) if "on_off" in data else True,
            brightness=require_bool(data, "brightness", path) if "brightness" in data else False,
            color_xy=require_bool(data, "color_xy", path) if "color_xy" in data else False,
            color_temp=color_temp,
            gradient=gradient,
            effects=require_list(
                data,
                "effects",
                path,
                lambda item, item_path: require_str({"v": item}, "v", item_path),
            )
            if data.get("effects")
            else [],
            palettes=require_list(
                data,
                "palettes",
                path,
                lambda item, item_path: require_str({"v": item}, "v", item_path),
            )
            if data.get("palettes")
            else [],
            cct=require_bool(data, "cct", path) if "cct" in data else False,
            dynamic_native=require_bool(data, "dynamic_native", path) if "dynamic_native" in data else False,
            extra=_require_extra(data, path),
        )


def _require_extra(data: dict, path: str) -> dict:
    extra = data.get("extra")
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise ValidationError(join(path, "extra"), "expected an object")
    return dict(extra)
