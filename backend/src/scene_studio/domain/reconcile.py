"""Same-binding registry reconciliation (not rebind).

Reconcile updates Scene Studio's knowledge of a fixture whose bound
provider/resource identity is already correct. It never writes a provider
and never changes the binding. Rebind is the command for a *different*
resource or provider.
"""

from __future__ import annotations

from .capabilities import Capabilities
from .discovery import DiscoveryObservation
from .fixtures import (
    CapabilityAssessment,
    CapabilityStatus,
    DeviceProfile,
    Fixture,
    HealthStatus,
    assess_capability_parity,
    derive_health,
)

__all__ = [
    "SOURCE_STRENGTH",
    "assessment_signature",
    "knowledge_drift_reasons",
    "merge_device_profiles",
    "proposed_reconcile_revision",
]

# Stronger verified physical evidence wins over a weaker observation.
SOURCE_STRENGTH = {
    "manual_verified": 3,
    "legacy_metadata": 2,
    "hue_device": 1,
    "hue_light_product_data": 1,
    "discovery": 1,
}


def _source_rank(source: str | None) -> int:
    if not source:
        return 0
    return SOURCE_STRENGTH.get(source, 1)


def merge_device_profiles(
    existing: DeviceProfile | None,
    observed: DeviceProfile | None,
) -> tuple[DeviceProfile | None, list[str]]:
    """Merge observation profile into stored profile without inventing features.

    Physical claims are copied only when the observation actually carries
    them. Conflicting fields surface as warnings and keep the stronger
    existing verified evidence instead of silently overwriting it.
    """
    warnings: list[str] = []
    if observed is None:
        return existing, warnings
    if existing is None:
        return observed, warnings

    keep_existing = _source_rank(existing.source) >= _source_rank(observed.source)

    def pick(field: str) -> str | None:
        old = getattr(existing, field)
        new = getattr(observed, field)
        if old and new and old != new:
            kept, ignored = (old, new) if keep_existing else (new, old)
            warnings.append(
                f"conflicting {field}: kept {kept!r} (source {existing.source or 'unknown'}), "
                f"ignored {ignored!r}"
            )
            return kept
        return old or new

    features = dict(existing.native_features or {})
    for key, value in (observed.native_features or {}).items():
        if key not in features:
            features[key] = value
        elif features[key] != value:
            kept, ignored = (features[key], value) if keep_existing else (value, features[key])
            warnings.append(
                f"conflicting native_features.{key}: kept {kept!r}, ignored {ignored!r}"
            )
            features[key] = kept

    if keep_existing and existing.source:
        source = existing.source
    else:
        source = observed.source or existing.source

    return (
        DeviceProfile(
            manufacturer=pick("manufacturer"),
            model=pick("model"),
            product_name=pick("product_name"),
            protocol=pick("protocol"),
            native_features=features,
            source=source,
        ),
        warnings,
    )


def assessment_signature(assessment: CapabilityAssessment | None) -> tuple:
    if assessment is None:
        return (None, ())
    return (assessment.status.value, tuple(assessment.reasons or ()))


def _caps_for_knowledge_drift(caps: Capabilities | None) -> dict | None:
    """Compare operational capability, ignoring volatile name catalogs."""
    if caps is None:
        return None
    data = caps.to_dict()
    data.pop("effects", None)
    data.pop("palettes", None)
    return data


def proposed_reconcile_revision(
    fixture: Fixture,
    observation: DiscoveryObservation,
    *,
    observed_at: str | None = None,
) -> tuple[Capabilities | None, DeviceProfile | None, CapabilityAssessment, HealthStatus | None, list[str]]:
    """Compute the registry fields a same-binding reconcile would commit.

    Binding is not returned: callers must keep the existing binding
    byte-for-byte. Capabilities come only from the observation's effective
    provider set (or the stored set when the observation has none).
    """
    warnings: list[str] = []
    if observation.capabilities is None:
        warnings.append("candidate observation has no effective capability set")
        caps = fixture.capabilities
    else:
        caps = observation.capabilities
    profile, profile_warnings = merge_device_profiles(fixture.device_profile, observation.device_profile)
    warnings.extend(profile_warnings)
    assessment = assess_capability_parity(
        caps,
        profile,
        provider=observation.provider,
        observation_id=observation.observation_id,
        observed_at=observed_at,
    )
    health = None if fixture.enabled else fixture.health
    return caps, profile, assessment, health, warnings


def knowledge_drift_reasons(
    fixture: Fixture,
    observation: DiscoveryObservation,
    *,
    capability_downgrades: list[str],
) -> list[str]:
    """Why the same observed resource can improve registry knowledge.

    Empty means bound_ready (no actionable drift). Capability *downgrades*
    are a different discovery status and are not listed here.
    """
    if capability_downgrades:
        return []
    caps, profile, assessment, health, _warnings = proposed_reconcile_revision(fixture, observation)
    reasons: list[str] = []
    current_health = derive_health(fixture)
    if fixture.health in (HealthStatus.DEGRADED, HealthStatus.MISSING, HealthStatus.CONFLICTING) and fixture.enabled:
        reasons.append("stale operational health override")
    elif health is None and current_health is not HealthStatus.READY and fixture.enabled:
        reasons.append("operational health can be derived as ready")
    current_caps = _caps_for_knowledge_drift(fixture.capabilities)
    proposed_caps = _caps_for_knowledge_drift(caps)
    if current_caps != proposed_caps:
        reasons.append("effective capabilities can be updated from the observation")
    current_profile = fixture.device_profile.to_dict() if fixture.device_profile else {}
    proposed_profile = profile.to_dict() if profile else {}
    if current_profile != proposed_profile:
        reasons.append("device profile can be improved")
    if fixture.capability_assessment is None:
        if assessment.status is not CapabilityStatus.UNKNOWN:
            reasons.append("capability assessment can be updated")
    elif fixture.capability_assessment.status != assessment.status:
        reasons.append("capability assessment can be updated")
    return reasons
