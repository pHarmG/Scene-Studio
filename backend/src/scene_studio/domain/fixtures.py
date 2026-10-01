"""Logical fixtures, targets, and the fixture registry document.

Identity (§2.4/§2.5/§2.6):
- `Fixture.id` is the stable automation identity; `name` is an editable label.
- Physical `location` and logical `groups` (target membership) are separate.
- `enabled` is independent of location/binding.
- Health is a status snapshot: derived from enabled/binding when not explicit.

`fixture.groups` is the authoritative membership side: a target resolves to
every fixture whose `groups` contains the target id. `Target` carries the
display definition only.

Invariant: Scene Studio fixtures represent independently addressable
endpoints. Aggregate Home Assistant convenience groups whose children are
already canonical Scene Studio fixtures are not canonical fixtures;
Scene Studio `targets`/`groups` represent aggregation. HA group helpers
must not be rendered as extra physical fixtures and must not be adopted
from discovery as ordinary `ha_light` bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .bindings import Binding, binding_from_dict, binding_ha_entity_ids
from .capabilities import Capabilities
from .contention import parse_contention_policy
from .identities import validate_id
from .serde import (
    ValidationError,
    join,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_mapping,
    require_str,
    require_str_list,
    require_timestamp,
)


# Broad catch-all group. Same-binding room reconcile may swap a fixture's
# primary room group from provider/HA evidence; this id is preserved unless
# the operator changes it explicitly. HA convenience groups never write it.
BROAD_GROUP_ID = "whole_house"


class HealthStatus(str, Enum):
    UNBOUND = "unbound"          # no binding yet
    READY = "ready"              # bound, expected to work
    MISSING = "missing"          # bound but provider resource not observed
    DISABLED = "disabled"        # administratively disabled (e.g. leave-behind)
    DEGRADED = "degraded"        # bound but capabilities reduced vs stored
    CONFLICTING = "conflicting"  # ambiguous binding/candidate situation


# Health values the apply/render pipeline skips (contracts §3). Target HA
# projection uses the same set so a room toggle cannot address a fixture
# that canonical execution would itself skip.
RENDER_SKIP_HEALTH = (
    HealthStatus.DISABLED,
    HealthStatus.MISSING,
    HealthStatus.UNBOUND,
)


class CapabilityStatus(str, Enum):
    """How the bound provider's effective capabilities compare to evidence.

    This is explanatory only.  Renderers must always use ``Capabilities``.
    """

    NOMINAL = "nominal"
    LIMITED = "limited"
    UNKNOWN = "unknown"


@dataclass
class DeviceProfile:
    """Descriptive physical-device evidence; never a renderer authorization."""

    manufacturer: str | None = None
    model: str | None = None
    product_name: str | None = None
    protocol: str | None = None
    native_features: dict = field(default_factory=dict)
    source: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {}
        for key in ("manufacturer", "model", "product_name", "protocol", "source"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.native_features:
            out["native_features"] = dict(self.native_features)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "device_profile") -> "DeviceProfile":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"manufacturer", "model", "product_name", "protocol", "native_features", "source"}, path)
        native_features = data.get("native_features", {})
        if not isinstance(native_features, dict):
            raise ValidationError(join(path, "native_features"), "expected an object")
        if len(native_features) > 32 or not all(isinstance(key, str) and len(key) <= 64 for key in native_features):
            raise ValidationError(join(path, "native_features"), "must contain at most 32 short string keys")
        return cls(
            manufacturer=optional_str(data, "manufacturer", path, max_length=128),
            model=optional_str(data, "model", path, max_length=128),
            product_name=optional_str(data, "product_name", path, max_length=128),
            protocol=optional_str(data, "protocol", path, max_length=64),
            native_features=dict(native_features),
            source=optional_str(data, "source", path, max_length=64),
        )


@dataclass
class CapabilityAssessment:
    status: CapabilityStatus = CapabilityStatus.UNKNOWN
    provider: str | None = None
    observation_id: str | None = None
    observed_at: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"status": self.status.value}
        if self.provider:
            out["provider"] = self.provider
        if self.observation_id:
            out["observation_id"] = self.observation_id
        if self.observed_at:
            out["observed_at"] = self.observed_at
        if self.reasons:
            out["reasons"] = list(self.reasons)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "capability_assessment") -> "CapabilityAssessment":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"status", "provider", "observation_id", "observed_at", "reasons"}, path)
        try:
            status = CapabilityStatus(data.get("status", CapabilityStatus.UNKNOWN.value))
        except ValueError:
            raise ValidationError(join(path, "status"), "must be nominal, limited, or unknown") from None
        reasons = require_str_list(data, "reasons", path) if data.get("reasons") else []
        if len(reasons) > 16 or any(len(reason) > 256 for reason in reasons):
            raise ValidationError(join(path, "reasons"), "must contain at most 16 reasons of 256 characters")
        return cls(status, optional_str(data, "provider", path, max_length=32),
                   optional_str(data, "observation_id", path, max_length=300),
                   require_timestamp(data, "observed_at", path) if data.get("observed_at") else None, reasons)


@dataclass
class BindingRevision:
    """Bounded rollback record for an explicit provider migration."""
    changed_at: str
    binding: Binding | None
    capabilities: Capabilities | None
    capability_assessment: CapabilityAssessment | None = None
    device_profile: DeviceProfile | None = None
    health: HealthStatus | None = None
    observation_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"changed_at": self.changed_at}
        if self.binding is not None:
            out["binding"] = self.binding.to_dict()
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.to_dict()
        if self.capability_assessment is not None:
            out["capability_assessment"] = self.capability_assessment.to_dict()
        if self.device_profile is not None:
            out["device_profile"] = self.device_profile.to_dict()
        if self.health is not None:
            out["health"] = self.health.value
        if self.observation_id:
            out["observation_id"] = self.observation_id
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "binding_revision") -> "BindingRevision":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"changed_at", "binding", "capabilities", "capability_assessment", "device_profile", "health", "observation_id", "reason"}, path)
        return cls(
            changed_at=require_timestamp(data, "changed_at", path),
            binding=binding_from_dict(data["binding"], join(path, "binding")) if data.get("binding") is not None else None,
            capabilities=Capabilities.from_dict(data["capabilities"], join(path, "capabilities")) if data.get("capabilities") is not None else None,
            capability_assessment=CapabilityAssessment.from_dict(data["capability_assessment"], join(path, "capability_assessment")) if data.get("capability_assessment") is not None else None,
            device_profile=DeviceProfile.from_dict(data["device_profile"], join(path, "device_profile")) if data.get("device_profile") is not None else None,
            health=_parse_health(data["health"], join(path, "health")) if data.get("health") is not None else None,
            observation_id=optional_str(data, "observation_id", path, max_length=300),
            reason=optional_str(data, "reason", path, max_length=512),
        )


@dataclass
class Fixture:
    id: str
    name: str
    location: str | None = None
    groups: list[str] = field(default_factory=list)
    enabled: bool = True
    binding: Binding | None = None
    capabilities: Capabilities | None = None
    device_profile: DeviceProfile | None = None
    capability_assessment: CapabilityAssessment | None = None
    binding_history: list[BindingRevision] = field(default_factory=list)
    # Stored health override; when None, derive_health() computes it.
    health: HealthStatus | None = None
    # External light-sync contention policy (hyperHDR pass): None = inherit
    # (target policy, then the engine default "yield").
    contention_policy: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"id": self.id, "name": self.name}
        if self.location:
            out["location"] = self.location
        if self.groups:
            out["groups"] = list(self.groups)
        out["enabled"] = self.enabled
        if self.binding is not None:
            out["binding"] = self.binding.to_dict()
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities.to_dict()
        if self.device_profile is not None:
            out["device_profile"] = self.device_profile.to_dict()
        if self.capability_assessment is not None:
            out["capability_assessment"] = self.capability_assessment.to_dict()
        if self.binding_history:
            out["binding_history"] = [revision.to_dict() for revision in self.binding_history]
        if self.health is not None:
            out["health"] = self.health.value
        if self.contention_policy is not None:
            out["contention_policy"] = self.contention_policy
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "fixture") -> "Fixture":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data, {"id", "name", "location", "groups", "enabled", "binding", "capabilities", "device_profile", "capability_assessment", "binding_history", "health", "contention_policy", "metadata"}, path
        )
        fixture_id = validate_id(data.get("id"), "fixture_id", join(path, "id"))
        groups = require_str_list(data, "groups", path) if data.get("groups") else []
        for index, group in enumerate(groups):
            validate_id(group, "target_id", join(path, f"groups.{index}"))
        binding = None
        if data.get("binding") is not None:
            binding = binding_from_dict(data["binding"], join(path, "binding"))
        capabilities = None
        if data.get("capabilities") is not None:
            capabilities = Capabilities.from_dict(data["capabilities"], join(path, "capabilities"))
        health = None
        if data.get("health") is not None:
            health = _parse_health(data.get("health"), join(path, "health"))
        history = data.get("binding_history", [])
        if not isinstance(history, list) or len(history) > 8:
            raise ValidationError(join(path, "binding_history"), "expected a list of at most 8 revisions")
        return cls(
            id=fixture_id,
            name=require_str(data, "name", join(path, "name"), max_length=128),
            location=optional_str(data, "location", path, max_length=64),
            groups=groups,
            enabled=require_bool(data, "enabled", path) if "enabled" in data else True,
            binding=binding,
            capabilities=capabilities,
            device_profile=DeviceProfile.from_dict(data["device_profile"], join(path, "device_profile")) if data.get("device_profile") is not None else None,
            capability_assessment=CapabilityAssessment.from_dict(data["capability_assessment"], join(path, "capability_assessment")) if data.get("capability_assessment") is not None else None,
            binding_history=[BindingRevision.from_dict(item, join(path, f"binding_history.{index}")) for index, item in enumerate(history)],
            health=health,
            contention_policy=parse_contention_policy(data.get("contention_policy"), path),
            metadata=_require_metadata(data, path),
        )


def _parse_health(value: Any, field: str) -> HealthStatus:
    try:
        return HealthStatus(value)
    except ValueError:
        allowed = ", ".join(status.value for status in HealthStatus)
        raise ValidationError(field, f"must be one of: {allowed}") from None


def _require_metadata(data: dict, path: str) -> dict:
    metadata = data.get("metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValidationError(join(path, "metadata"), "expected an object")
    return dict(metadata)


def derive_health(fixture: Fixture) -> HealthStatus:
    """Effective health: explicit override wins, else derive from flags."""
    if fixture.health is not None:
        return fixture.health
    if not fixture.enabled:
        return HealthStatus.DISABLED
    if fixture.binding is None:
        return HealthStatus.UNBOUND
    return HealthStatus.READY


def is_render_skipped(fixture: Fixture) -> bool:
    """True when canonical apply/render would skip this fixture (contracts §3)."""
    return derive_health(fixture) in RENDER_SKIP_HEALTH


def project_target_membership(registry: FixtureRegistry) -> list[dict]:
    """Compact HA-bridge target projection from canonical target resolution.

    Membership comes from ``fixture.groups`` (the same rule ``resolve_target``
    uses). Disabled/missing/unbound fixtures are excluded, matching apply.
    HA entity ids are flattened from each remaining binding; a fixture with
    no HA entity still counts toward ``enabled_fixture_count`` so incomplete
    coverage cannot be silently claimed as parity.
    """
    projected: list[dict] = []
    for target in sorted(registry.targets, key=lambda item: item.id):
        members = sorted(
            (fixture for fixture in registry.fixtures if target.id in fixture.groups),
            key=lambda item: item.id,
        )
        executable = [fixture for fixture in members if not is_render_skipped(fixture)]
        ha_entity_ids: list[str] = []
        covered = 0
        for fixture in executable:
            entity_ids = binding_ha_entity_ids(fixture.binding)
            if entity_ids:
                covered += 1
            for entity_id in entity_ids:
                if entity_id not in ha_entity_ids:
                    ha_entity_ids.append(entity_id)
        enabled_count = len(executable)
        projected.append(
            {
                "id": target.id,
                "name": target.name,
                "fixture_ids": [fixture.id for fixture in executable],
                "ha_entity_ids": ha_entity_ids,
                "enabled_fixture_count": enabled_count,
                "ha_covered_fixture_count": covered,
                "complete_ha_coverage": enabled_count > 0 and covered == enabled_count,
            }
        )
    return projected


def assess_capability_parity(
    capabilities: Capabilities | None,
    profile: DeviceProfile | None,
    *,
    provider: str | None = None,
    observation_id: str | None = None,
    observed_at: str | None = None,
) -> CapabilityAssessment:
    """Compare only explicit physical-feature evidence to provider capabilities.

    Never infers gradient, dynamic, or addressable-pixel support from names
    or model strings. Missing physical evidence yields ``unknown``.
    """
    features = profile.native_features if profile is not None else {}
    feature_map = {
        "addressable_pixels": "gradient",
        "gradient": "gradient",
        "color": "color_xy",
        "color_xy": "color_xy",
        "color_temperature": "color_temp",
        "cct": "cct",
        "dynamic_native": "dynamic_native",
        "effects": "effects",
        "palettes": "palettes",
    }
    limitations: list[str] = []
    known_claims = False
    for claim, capability in feature_map.items():
        value = features.get(claim)
        if value is True or (isinstance(value, (list, tuple, set)) and len(value) > 0):
            known_claims = True
            actual = getattr(capabilities, capability, None) if capabilities is not None else None
            if actual is None or actual is False or actual == []:
                limitations.append(
                    f"Physical profile claims {claim}, but selected provider exposes no {capability} capability"
                )
    if limitations:
        status = CapabilityStatus.LIMITED
        reasons = limitations
    elif not known_claims:
        status = CapabilityStatus.UNKNOWN
        reasons = ["Physical feature evidence is insufficient to classify provider parity"]
    else:
        status = CapabilityStatus.NOMINAL
        reasons = ["Known physical feature evidence is available through the selected provider"]
    return CapabilityAssessment(
        status=status,
        provider=provider,
        observation_id=observation_id,
        observed_at=observed_at,
        reasons=reasons,
    )


@dataclass
class Target:
    """A logical target (named fixture grouping) — display definition only.

    Membership resolves through `fixture.groups`; never store member lists
    here to avoid dual bookkeeping. The optional ``contention_policy`` is
    the target-level external-sync override (fixture policy wins over it).
    """

    id: str
    name: str
    description: str | None = None
    contention_policy: str | None = None

    def to_dict(self) -> dict:
        out = {"id": self.id, "name": self.name}
        if self.description:
            out["description"] = self.description
        if self.contention_policy is not None:
            out["contention_policy"] = self.contention_policy
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "target") -> "Target":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"id", "name", "description", "contention_policy"}, path)
        return cls(
            id=validate_id(data.get("id"), "target_id", join(path, "id")),
            name=require_str(data, "name", join(path, "name"), max_length=128),
            description=optional_str(data, "description", path, max_length=512),
            contention_policy=parse_contention_policy(data.get("contention_policy"), path),
        )


REGISTRY_SCHEMA_VERSION = 2


@dataclass
class FixtureRegistry:
    """The registry document persisted by the store layer (Wave A1)."""

    fixtures: list[Fixture] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)
    updated: str | None = None
    schema_version: int = REGISTRY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "fixtures": [fixture.to_dict() for fixture in self.fixtures],
            "targets": [target.to_dict() for target in self.targets],
        }
        if self.updated:
            out["updated"] = self.updated
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "registry") -> "FixtureRegistry":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"schema_version", "fixtures", "targets", "updated"}, path)
        version = data.get("schema_version")
        if version == 1:
            data = _migrate_v1_registry(data)
        elif version != REGISTRY_SCHEMA_VERSION:
            raise ValidationError(join(path, "schema_version"), f"expected 1 or {REGISTRY_SCHEMA_VERSION}, got {version!r}")
        raw_fixtures = data.get("fixtures", [])
        if not isinstance(raw_fixtures, list):
            raise ValidationError(join(path, "fixtures"), "expected a list")
        raw_targets = data.get("targets", [])
        if not isinstance(raw_targets, list):
            raise ValidationError(join(path, "targets"), "expected a list")
        fixtures = [
            Fixture.from_dict(item, join(path, f"fixtures.{index}"))
            for index, item in enumerate(raw_fixtures)
        ]
        _reject_duplicate_ids(fixtures, "fixture", path)
        targets = [
            Target.from_dict(item, join(path, f"targets.{index}"))
            for index, item in enumerate(raw_targets)
        ]
        _reject_duplicate_ids(targets, "target", path)
        return cls(
            fixtures=fixtures,
            targets=targets,
            updated=require_timestamp(data, "updated", path) if data.get("updated") else None,
            schema_version=REGISTRY_SCHEMA_VERSION,
        )


def _migrate_v1_registry(data: dict) -> dict:
    """Pure, deterministic v1 -> v2 registry migration (no provider access)."""
    migrated = dict(data)
    migrated["schema_version"] = REGISTRY_SCHEMA_VERSION
    fixtures: list[dict] = []
    for raw in data.get("fixtures", []):
        if not isinstance(raw, dict):
            fixtures.append(raw)
            continue
        fixture = dict(raw)
        metadata = fixture.get("metadata") if isinstance(fixture.get("metadata"), dict) else {}
        profile_fields = {key: metadata[key] for key in ("manufacturer", "model", "product_name", "protocol") if isinstance(metadata.get(key), str)}
        if profile_fields:
            profile_fields["source"] = "legacy_metadata"
            fixture["device_profile"] = profile_fields
        if fixture.get("health") == HealthStatus.DEGRADED.value:
            # v1 never recorded *why* a fixture was degraded. Preserve that
            # operational legacy signal rather than inventing a capability
            # limitation; a later evidence-led reconciliation may classify it.
            fixture["capability_assessment"] = {
                "status": CapabilityStatus.UNKNOWN.value,
                "reasons": ["Legacy degraded health retained; cause was not recorded in schema v1"],
            }
        fixtures.append(fixture)
    migrated["fixtures"] = fixtures
    return migrated


def _reject_duplicate_ids(models: list, kind: str, path: str) -> None:
    seen: set[str] = set()
    for model in models:
        if model.id in seen:
            raise ValidationError(path, f"duplicate {kind} id: {model.id}")
        seen.add(model.id)
