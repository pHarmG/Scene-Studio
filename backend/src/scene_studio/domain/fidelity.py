"""Fidelity classification and render plans.

Every renderer reports how faithfully it can execute a scene on a fixture
(master plan §6.5). Dry-run `render plan` output lets the Workbench, tests,
and migration verify exactly what would execute without touching devices
(§8 renderer exit gate).
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
    require_str_list,
)


class FidelityLevel(str, Enum):
    NATIVE = "native"                # provider executes the intent directly
    EQUIVALENT = "equivalent"        # same visible result via provider primitives
    APPROXIMATE = "approximate"      # visibly close but not exact (or fallback)
    UNSUPPORTED = "unsupported"      # cannot be represented on this fixture


@dataclass
class ProviderOperation:
    """One provider call that would be issued (dry-run plan entry)."""

    provider: str              # hue_v2 | wled | ha_light | fallback
    op: str                    # e.g. "hue.put_light", "wled.post_state", "ha.call_light"
    resource_ref: str          # provider resource pointer (id/entity/segment set)
    payload: dict = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict:
        out = {"provider": self.provider, "op": self.op, "resource_ref": self.resource_ref}
        if self.payload:
            out["payload"] = dict(self.payload)
        if self.description:
            out["description"] = self.description
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "operation") -> "ProviderOperation":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"provider", "op", "resource_ref", "payload", "description"}, path)
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValidationError(join(path, "payload"), "expected an object")
        return cls(
            provider=require_str(data, "provider", join(path, "provider"), max_length=32),
            op=require_str(data, "op", join(path, "op"), max_length=64),
            resource_ref=require_str(data, "resource_ref", join(path, "resource_ref"), max_length=255),
            payload=dict(payload),
            description=optional_str(data, "description", path, max_length=512) or "",
        )


@dataclass
class FixtureRenderPlan:
    """How one fixture in one scene would be executed by a renderer."""

    fixture_id: str
    provider: str
    fidelity: FidelityLevel
    reason: str = ""
    operations: list[ProviderOperation] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {
            "fixture_id": self.fixture_id,
            "provider": self.provider,
            "fidelity": self.fidelity.value,
        }
        if self.reason:
            out["reason"] = self.reason
        if self.operations:
            out["operations"] = [operation.to_dict() for operation in self.operations]
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "fixture_plan") -> "FixtureRenderPlan":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"fixture_id", "provider", "fidelity", "reason", "operations"}, path)
        fidelity = data.get("fidelity")
        try:
            fidelity = FidelityLevel(fidelity)
        except ValueError:
            allowed = ", ".join(level.value for level in FidelityLevel)
            raise ValidationError(join(path, "fidelity"), f"must be one of: {allowed}") from None
        raw_operations = data.get("operations", [])
        if not isinstance(raw_operations, list):
            raise ValidationError(join(path, "operations"), "expected a list")
        return cls(
            fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64),
            provider=require_str(data, "provider", join(path, "provider"), max_length=32),
            fidelity=fidelity,
            reason=optional_str(data, "reason", path, max_length=512) or "",
            operations=[
                ProviderOperation.from_dict(item, join(path, f"operations.{index}"))
                for index, item in enumerate(raw_operations)
            ],
        )


@dataclass
class RenderPlan:
    """Complete dry-run plan for applying a scene to targets."""

    scene_id: str
    target_ids: list[str]
    fixture_plans: list[FixtureRenderPlan] = field(default_factory=list)
    skipped_fixture_ids: list[str] = field(default_factory=list)  # disabled/missing
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "scene_id": self.scene_id,
            "target_ids": list(self.target_ids),
            "fixture_plans": [plan.to_dict() for plan in self.fixture_plans],
        }
        if self.skipped_fixture_ids:
            out["skipped_fixture_ids"] = list(self.skipped_fixture_ids)
        if self.notes:
            out["notes"] = list(self.notes)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "render_plan") -> "RenderPlan":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"scene_id", "target_ids", "fixture_plans", "skipped_fixture_ids", "notes"}, path)
        raw_plans = data.get("fixture_plans", [])
        if not isinstance(raw_plans, list):
            raise ValidationError(join(path, "fixture_plans"), "expected a list")
        raw_skipped = data.get("skipped_fixture_ids", [])
        raw_notes = data.get("notes", [])
        if not isinstance(raw_skipped, list) or not isinstance(raw_notes, list):
            raise ValidationError(path, "skipped_fixture_ids and notes must be lists")
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            target_ids=require_str_list(data, "target_ids", path),
            fixture_plans=[
                FixtureRenderPlan.from_dict(item, join(path, f"fixture_plans.{index}"))
                for index, item in enumerate(raw_plans)
            ],
            skipped_fixture_ids=require_str_list(data, "skipped_fixture_ids", path)
            if raw_skipped
            else [],
            notes=require_str_list(data, "notes", path) if raw_notes else [],
        )
