"""Domain contracts: models, validation, serialization (Phase 1)."""

from .bindings import Binding, HaLightBinding, HueBinding, WledBinding, binding_from_dict, binding_ha_entity_ids
from .capabilities import Capabilities, ColorTempRange, GradientCapability
from .commands import (
    COMMAND_CATALOG,
    CommandEnvelope,
    CommandError,
    CommandResult,
    ErrorCode,
    failure,
    parse_command,
    success,
    validation_error_result,
)
from .discovery import (
    BindingCandidate,
    CandidateCompatibility,
    DiscoveryEntry,
    DiscoveryEntryStatus,
    DiscoveryObservation,
    DiscoveryReport,
)
from .events import EventCategory, EventLevel, OperationalEvent
from .fidelity import FidelityLevel, FixtureRenderPlan, ProviderOperation, RenderPlan
from .fixtures import (
    BROAD_GROUP_ID,
    BindingRevision, CapabilityAssessment, CapabilityStatus, DeviceProfile,
    Fixture, FixtureRegistry, HealthStatus, Target, assess_capability_parity, derive_health,
    is_render_skipped, project_target_membership,
)
from .identities import is_valid_id, normalize_name_to_id, validate_id
from .sanitize import sanitize_tree
from .scenes import FixtureState, Motion, MotionMode, MotionStrategy, Scene
from .serde import ValidationError
from .target_power import resolve_target_power_action

__all__ = [
    "BROAD_GROUP_ID",
    "COMMAND_CATALOG",
    "Binding",
    "BindingRevision",
    "BindingCandidate",
    "CandidateCompatibility",
    "Capabilities",
    "CapabilityAssessment",
    "CapabilityStatus",
    "ColorTempRange",
    "CommandEnvelope",
    "CommandError",
    "CommandResult",
    "DiscoveryEntry",
    "DiscoveryEntryStatus",
    "DiscoveryObservation",
    "DiscoveryReport",
    "DeviceProfile",
    "ErrorCode",
    "EventCategory",
    "EventLevel",
    "Fixture",
    "FixtureRegistry",
    "FixtureRenderPlan",
    "FixtureState",
    "FidelityLevel",
    "GradientCapability",
    "HaLightBinding",
    "HealthStatus",
    "HueBinding",
    "Motion",
    "MotionMode",
    "MotionStrategy",
    "OperationalEvent",
    "ProviderOperation",
    "RenderPlan",
    "Scene",
    "Target",
    "ValidationError",
    "WledBinding",
    "binding_from_dict",
    "binding_ha_entity_ids",
    "assess_capability_parity",
    "derive_health",
    "is_render_skipped",
    "project_target_membership",
    "resolve_target_power_action",
    "failure",
    "is_valid_id",
    "normalize_name_to_id",
    "parse_command",
    "sanitize_tree",
    "success",
    "validate_id",
    "validation_error_result",
]
