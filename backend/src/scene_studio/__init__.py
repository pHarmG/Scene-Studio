"""Scene Studio — provider-neutral lighting scene domain core.

Phase 1 contract layer. See docs/scene_studio/ARCHITECTURE_CONTRACTS.md.
"""

from .domain.bindings import (
    Binding,
    HaLightBinding,
    HueBinding,
    WledBinding,
    binding_from_dict,
    KNOWN_PROVIDERS,
)
from .domain.capabilities import Capabilities, ColorTempRange, GradientCapability
from .domain.discovery import (
    BindingCandidate,
    CandidateCompatibility,
    DiscoveryEntry,
    DiscoveryEntryStatus,
    DiscoveryObservation,
    DiscoveryReport,
)
from .domain.events import EventCategory, EventLevel, OperationalEvent
from .domain.fidelity import FidelityLevel, FixtureRenderPlan, ProviderOperation, RenderPlan
from .domain.fixtures import (
    Fixture,
    FixtureRegistry,
    HealthStatus,
    REGISTRY_SCHEMA_VERSION,
    Target,
    derive_health,
)
from .domain.scenes import (
    FixtureState,
    Motion,
    MotionMode,
    MotionStrategy,
    Scene,
    SCENE_SCHEMA_VERSION,
)
from .domain.serde import ValidationError
from .stores import (
    ConflictError,
    CorruptStoreError,
    FixtureStore,
    NotFoundError,
    SceneStudioStore,
    SceneStore,
    StoreError,
)

from .build_info import get_build_info

__version__ = get_build_info()["version"]

__all__ = [
    "Binding",
    "BindingCandidate",
    "CandidateCompatibility",
    "Capabilities",
    "ColorTempRange",
    "ConflictError",
    "CorruptStoreError",
    "DiscoveryEntry",
    "DiscoveryEntryStatus",
    "DiscoveryObservation",
    "DiscoveryReport",
    "EventCategory",
    "EventLevel",
    "Fixture",
    "FixtureRegistry",
    "FixtureRenderPlan",
    "FixtureState",
    "FixtureStore",
    "FidelityLevel",
    "GradientCapability",
    "HaLightBinding",
    "HealthStatus",
    "HueBinding",
    "KNOWN_PROVIDERS",
    "Motion",
    "MotionMode",
    "MotionStrategy",
    "NotFoundError",
    "OperationalEvent",
    "ProviderOperation",
    "REGISTRY_SCHEMA_VERSION",
    "RenderPlan",
    "SCENE_SCHEMA_VERSION",
    "Scene",
    "SceneStore",
    "SceneStudioStore",
    "StoreError",
    "Target",
    "ValidationError",
    "WledBinding",
    "binding_from_dict",
    "derive_health",
]
