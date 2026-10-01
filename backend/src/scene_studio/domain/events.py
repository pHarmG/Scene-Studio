"""Structured operational events powering the Workbench Diagnostics view.

The engine emits human-meaningful events (§10.7): a concise summary plus
optional technical detail. Raw provider payloads stay out of the summary and
live only in Level-3 inspector data (and must pass through `sanitize`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .serde import (
    ValidationError,
    join,
    optional_str,
    reject_unknown_keys,
    require_mapping,
    require_str,
    require_timestamp,
)


class EventLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventCategory(str, Enum):
    SCENE = "scene"
    PLAYBACK = "playback"
    FIXTURE = "fixture"
    DISCOVERY = "discovery"
    SYSTEM = "system"


@dataclass
class OperationalEvent:
    timestamp: str  # ISO-8601
    level: EventLevel
    category: EventCategory
    summary: str                 # Level-1/2 text, e.g. "Twilight started"
    detail: str = ""             # Level-2 explanation, e.g. "14 fixtures • dynamic"
    scene_id: str | None = None
    fixture_id: str | None = None
    provider: str | None = None
    data: dict = field(default_factory=dict)  # sanitized technical detail

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "timestamp": self.timestamp,
            "level": self.level.value,
            "category": self.category.value,
            "summary": self.summary,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.scene_id:
            out["scene_id"] = self.scene_id
        if self.fixture_id:
            out["fixture_id"] = self.fixture_id
        if self.provider:
            out["provider"] = self.provider
        if self.data:
            out["data"] = dict(self.data)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "event") -> "OperationalEvent":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {"timestamp", "level", "category", "summary", "detail", "scene_id", "fixture_id", "provider", "data"},
            path,
        )
        try:
            level = EventLevel(data["level"])
            category = EventCategory(data["category"])
        except (ValueError, KeyError):
            allowed_levels = ", ".join(level.value for level in EventLevel)
            allowed_categories = ", ".join(category.value for category in EventCategory)
            raise ValidationError(
                path, f"level must be one of: {allowed_levels}; category must be one of: {allowed_categories}"
            ) from None
        event_data = data.get("data", {})
        if not isinstance(event_data, dict):
            raise ValidationError(join(path, "data"), "expected an object")
        return cls(
            timestamp=require_timestamp(data, "timestamp", path),
            level=level,
            category=category,
            summary=require_str(data, "summary", join(path, "summary"), max_length=512),
            detail=optional_str(data, "detail", path, max_length=1024) or "",
            scene_id=optional_str(data, "scene_id", path, max_length=64),
            fixture_id=optional_str(data, "fixture_id", path, max_length=64),
            provider=optional_str(data, "provider", path, max_length=32),
            data=dict(event_data),
        )
