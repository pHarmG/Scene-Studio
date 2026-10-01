"""Discovery service (Workstream A2): observations, matching, candidates.

Transport-free and stdlib-only. Builders turn already-fetched provider
payloads into :class:`DiscoveryObservation` values; :func:`run_discovery`
is the pure orchestration entry point producing a deterministic
:class:`DiscoveryReport`. Discovery never writes bindings.
"""

from .aggregates import ha_group_member_ids, is_ha_aggregate_light, is_aggregate_observation
from .drift import membership_drift_report
from .halight import build_halight_observations
from .hue import DEFAULT_GRADIENT_POINTS, build_hue_observations
from .service import (
    CANDIDATE_FLOOR,
    CONFLICT_GAP,
    capability_downgrades,
    capability_mismatch,
    observation_matches_binding,
    run_discovery,
)
from .wled import build_wled_observations, derive_wled_device_id

__all__ = [
    "CANDIDATE_FLOOR",
    "CONFLICT_GAP",
    "DEFAULT_GRADIENT_POINTS",
    "build_halight_observations",
    "build_hue_observations",
    "build_wled_observations",
    "capability_downgrades",
    "capability_mismatch",
    "derive_wled_device_id",
    "ha_group_member_ids",
    "is_aggregate_observation",
    "is_ha_aggregate_light",
    "membership_drift_report",
    "observation_matches_binding",
    "run_discovery",
]
