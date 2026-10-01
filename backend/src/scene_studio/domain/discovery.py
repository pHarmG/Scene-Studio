"""Discovery observations and binding candidates (master plan §6.3, §7-A2).

Discovery produces observations without mutating bindings. Candidate scoring
must be explainable (`confidence` + `reasons`). Automatic silent rebinding is
prohibited: applying a candidate is always an explicit command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .capabilities import Capabilities
from .fixtures import DeviceProfile
from .identities import validate_id
from .serde import (
    ValidationError,
    join,
    optional_str,
    reject_unknown_keys,
    require_float,
    require_mapping,
    require_str,
    require_str_list,
    require_timestamp,
)


@dataclass
class DiscoveryObservation:
    """A provider resource currently visible to the system."""

    provider: str                    # hue_v2 | wled | ha_light
    provider_resource_id: str        # stable within provider (UUID, MAC+segment, entity id)
    name: str
    location_hint: str | None = None
    capabilities: Capabilities | None = None
    endpoint_hint: str | None = None
    device_profile: DeviceProfile | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def observation_id(self) -> str:
        return f"{self.provider}:{self.provider_resource_id}"

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "provider": self.provider,
            "provider_resource_id": self.provider_resource_id,
            "name": self.name,
            "observation_id": self.observation_id,
        }
        if self.location_hint:
            out["location_hint"] = self.location_hint
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.to_dict()
        if self.endpoint_hint:
            out["endpoint_hint"] = self.endpoint_hint
        if self.device_profile is not None:
            out["device_profile"] = self.device_profile.to_dict()
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "observation") -> "DiscoveryObservation":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {
                "provider",
                "provider_resource_id",
                "name",
                "location_hint",
                "capabilities",
                "endpoint_hint",
                "metadata", "device_profile",
                "observation_id",
            },
            path,
        )
        capabilities = None
        if data.get("capabilities") is not None:
            capabilities = Capabilities.from_dict(data["capabilities"], join(path, "capabilities"))
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValidationError(join(path, "metadata"), "expected an object")
        observation = cls(
            provider=require_str(data, "provider", join(path, "provider"), max_length=32),
            provider_resource_id=require_str(
                data, "provider_resource_id", join(path, "provider_resource_id"), max_length=255
            ),
            name=require_str(data, "name", join(path, "name"), max_length=128),
            location_hint=optional_str(data, "location_hint", path, max_length=64),
            capabilities=capabilities,
            endpoint_hint=optional_str(data, "endpoint_hint", path, max_length=255),
            device_profile=DeviceProfile.from_dict(data["device_profile"], join(path, "device_profile")) if data.get("device_profile") is not None else None,
            metadata=dict(metadata),
        )
        # `observation_id` is a computed convenience emitted by to_dict(); when
        # present it must agree with provider + provider_resource_id.
        if data.get("observation_id") is not None:
            stated = require_str(data, "observation_id", join(path, "observation_id"), max_length=300)
            if stated != observation.observation_id:
                raise ValidationError(
                    join(path, "observation_id"),
                    f"expected {observation.observation_id!r}, got {stated!r}",
                )
        return observation


class CandidateCompatibility(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass
class BindingCandidate:
    """An explainable proposal to bind a fixture to an observation."""

    fixture_id: str
    observation_id: str
    compatibility: CandidateCompatibility = CandidateCompatibility.COMPATIBLE
    confidence: float = 0.0  # 0.0..1.0
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fixture_id": self.fixture_id,
            "observation_id": self.observation_id,
            "compatibility": self.compatibility.value,
            "confidence": round(self.confidence, 4),
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: Any, path: str = "candidate") -> "BindingCandidate":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"fixture_id", "observation_id", "compatibility", "confidence", "reasons"}, path)
        raw_compatibility = data.get("compatibility", CandidateCompatibility.COMPATIBLE.value)
        try:
            compatibility = CandidateCompatibility(raw_compatibility)
        except ValueError:
            allowed = ", ".join(c.value for c in CandidateCompatibility)
            raise ValidationError(join(path, "compatibility"), f"must be one of: {allowed}") from None
        return cls(
            fixture_id=validate_id(data.get("fixture_id"), "fixture_id", join(path, "fixture_id")),
            observation_id=require_str(data, "observation_id", join(path, "observation_id"), max_length=300),
            compatibility=compatibility,
            confidence=require_float(data, "confidence", path, minimum=0.0, maximum=1.0),
            reasons=require_str_list(data, "reasons", path) if data.get("reasons") else [],
        )


class DiscoveryEntryStatus(str, Enum):
    """Per-fixture / per-observation discovery status (master plan §7-A2)."""

    BOUND_READY = "bound_ready"                    # binding observed; no actionable registry drift
    BOUND_RECONCILE_AVAILABLE = "bound_reconcile_available"  # same resource; registry knowledge is stale
    BOUND_DEGRADED = "bound_degraded"              # binding observed but capability downgrade
    MISSING = "missing"                            # binding not found in observations
    AVAILABLE_UNBOUND = "available_unbound"        # observation with no fixture
    DISABLED = "disabled"                          # fixture disabled; discovery skipped it
    CANDIDATE_REPLACEMENT = "candidate_replacement"  # fixture missing; candidate(s) exist
    CONFLICT = "conflict"                          # ambiguous candidates / competing matches


@dataclass
class DiscoveryEntry:
    """One line of a discovery report (fixture-centric or observation-centric)."""

    status: DiscoveryEntryStatus
    fixture_id: str | None = None
    observation_id: str | None = None
    detail: str = ""
    candidate_ids: list[str] = field(default_factory=list)  # fixture_id:observation_id pairs

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"status": self.status.value}
        if self.fixture_id:
            out["fixture_id"] = self.fixture_id
        if self.observation_id:
            out["observation_id"] = self.observation_id
        if self.detail:
            out["detail"] = self.detail
        if self.candidate_ids:
            out["candidate_ids"] = list(self.candidate_ids)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "entry") -> "DiscoveryEntry":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"status", "fixture_id", "observation_id", "detail", "candidate_ids"}, path)
        raw_status = data.get("status")
        try:
            status = DiscoveryEntryStatus(raw_status)
        except ValueError:
            allowed = ", ".join(s.value for s in DiscoveryEntryStatus)
            raise ValidationError(join(path, "status"), f"must be one of: {allowed}") from None
        fixture_id = optional_str(data, "fixture_id", path, max_length=64)
        if fixture_id is not None:
            validate_id(fixture_id, "fixture_id", join(path, "fixture_id"))
        return cls(
            status=status,
            fixture_id=fixture_id,
            observation_id=optional_str(data, "observation_id", path, max_length=300),
            detail=optional_str(data, "detail", path, max_length=512) or "",
            candidate_ids=require_str_list(data, "candidate_ids", path) if data.get("candidate_ids") else [],
        )


@dataclass
class DiscoveryReport:
    """Complete, deterministic result of one discovery run."""

    run_id: str
    started_at: str
    finished_at: str | None = None
    providers: list[str] = field(default_factory=list)
    observations: list[DiscoveryObservation] = field(default_factory=list)
    candidates: list[BindingCandidate] = field(default_factory=list)
    entries: list[DiscoveryEntry] = field(default_factory=list)
    # summary counts; discovery service fills these deterministically
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "providers": list(self.providers),
            "observations": [observation.to_dict() for observation in self.observations],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "entries": [entry.to_dict() for entry in self.entries],
            "summary": dict(self.summary),
        }
        if self.finished_at:
            out["finished_at"] = self.finished_at
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "discovery_report") -> "DiscoveryReport":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {"run_id", "started_at", "finished_at", "providers", "observations", "candidates", "entries", "summary"},
            path,
        )
        observations = _require_model_list(data, "observations", path, DiscoveryObservation.from_dict)
        candidates = _require_model_list(data, "candidates", path, BindingCandidate.from_dict)
        entries = _require_model_list(data, "entries", path, DiscoveryEntry.from_dict)
        summary = data.get("summary", {})
        if not isinstance(summary, dict):
            raise ValidationError(join(path, "summary"), "expected an object")
        return cls(
            run_id=require_str(data, "run_id", join(path, "run_id"), max_length=64),
            started_at=require_timestamp(data, "started_at", path),
            finished_at=require_timestamp(data, "finished_at", path) if data.get("finished_at") else None,
            providers=require_str_list(data, "providers", path) if data.get("providers") else [],
            observations=observations,
            candidates=candidates,
            entries=entries,
            summary=dict(summary),
        )


def _require_model_list(data: dict, key: str, path: str, from_dict_fn) -> list:
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ValidationError(join(path, key), "expected a list")
    return [from_dict_fn(item, join(path, f"{key}.{index}")) for index, item in enumerate(raw)]
