"""Discovery orchestration: matching, candidate scoring, reporting (A2).

``run_discovery`` is a pure function. It never mutates the registry,
never writes bindings, and performs no I/O: the same inputs always
produce byte-identical ``DiscoveryReport`` output (all iteration is
sorted; timestamps come only from the caller's arguments).

Status semantics (frozen contracts §6, ``DiscoveryEntryStatus``):

- ``bound_ready``            exact current binding observed; effective
                             provider capability agrees; no actionable
                             registry drift
- ``bound_reconcile_available`` same exact resource observed; registry
                             profile/assessment/health knowledge can be
                             improved. This is not operational failure.
- ``bound_degraded``         genuine provider-effective capability regression
                             vs ``fixture.capabilities``
- ``missing``                binding resource (or WLED segment(s)) not observed
- ``available_unbound``      observation no fixture currently claims
- ``disabled``               fixture disabled; skipped for matching entirely
- ``candidate_replacement``  binding missing (or fixture unbound) and one
                             unambiguous candidate exists
- ``conflict``               two or more candidates within 0.15 confidence of
                             the best one

Candidate scoring is explainable: every candidate carries its reasons and
a confidence in 0..1 built from documented additive weights. Discovery
never rebinds silently — applying a candidate is an explicit command.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .aggregates import is_aggregate_observation

from ..domain.bindings import (
    Binding,
    HaLightBinding,
    HueBinding,
    WledBinding,
)
from ..domain.capabilities import Capabilities
from ..domain.discovery import (
    BindingCandidate,
    CandidateCompatibility,
    DiscoveryEntry,
    DiscoveryEntryStatus,
    DiscoveryObservation,
    DiscoveryReport,
)
from ..domain.fixtures import Fixture, FixtureRegistry
from ..domain.reconcile import knowledge_drift_reasons

PROVIDER_HUE_V2 = "hue_v2"
PROVIDER_WLED = "wled"
PROVIDER_HA_LIGHT = "ha_light"

# Two candidates within this confidence distance of each other are ambiguous.
CONFLICT_GAP = 0.15
# Candidates below this confidence are noise and are not proposed.
CANDIDATE_FLOOR = 0.30

# Additive scoring weights (documented, deterministic).
_BASE_CONFIDENCE = 0.10        # present in the current provider inventory
_WEIGHT_NAME_EXACT = 0.25      # normalized friendly name unchanged
_WEIGHT_NAME_FACTOR = 0.25     # partial: weight * similarity when similarity >= 0.5
_NAME_SIMILAR_THRESHOLD = 0.5
_WEIGHT_LOCATION = 0.10        # location hint equals fixture location
_WEIGHT_CAPABILITY = 0.10      # capabilities known-compatible
_WEIGHT_ENDPOINT = 0.05        # weak: endpoint hint equals previous hint
_WEIGHT_DEVICE_ID = 0.55       # strong: WLED device_id matches previous binding
_WEIGHT_HA_ENTITY = 0.60       # strong: HA entity linkage matches previous binding


def run_discovery(
    registry: FixtureRegistry,
    observations: list[DiscoveryObservation],
    *,
    run_id: str,
    started_at: str,
    finished_at: str | None = None,
    previous: DiscoveryReport | None = None,
) -> DiscoveryReport:
    """Match the registry against current observations and explain gaps.

    Pure: the registry object is only read; the report is built from the
    passed observations (copied, sorted deterministically).
    """
    obs_sorted = sorted(observations, key=lambda obs: obs.observation_id)
    obs_index: dict[str, DiscoveryObservation] = {obs.observation_id: obs for obs in obs_sorted}
    prev_names: dict[str, str] = {}
    if previous is not None:
        prev_names = {
            obs.observation_id: obs.name for obs in sorted(previous.observations, key=lambda o: o.observation_id)
        }

    entries: list[DiscoveryEntry] = []
    candidates: list[BindingCandidate] = []
    claimed: set[str] = set()

    for fixture in registry.fixtures:
        if not fixture.enabled:
            entries.append(
                DiscoveryEntry(
                    status=DiscoveryEntryStatus.DISABLED,
                    fixture_id=fixture.id,
                    detail="administratively disabled; discovery skipped provider lookup",
                )
            )
            continue

        expected = _expected_observation_ids(fixture.binding, obs_index)
        found = [obs_id for obs_id in expected if obs_id in obs_index]
        claimed.update(found)

        if expected and len(found) == len(expected):
            primary = obs_index[found[0]]
            downgrades = capability_downgrades(fixture.capabilities, primary.capabilities)
            if downgrades:
                entries.append(
                    DiscoveryEntry(
                        status=DiscoveryEntryStatus.BOUND_DEGRADED,
                        fixture_id=fixture.id,
                        observation_id=primary.observation_id,
                        detail="capability downgrade: " + "; ".join(downgrades),
                    )
                )
            else:
                drift = knowledge_drift_reasons(fixture, primary, capability_downgrades=downgrades)
                if drift:
                    entries.append(
                        DiscoveryEntry(
                            status=DiscoveryEntryStatus.BOUND_RECONCILE_AVAILABLE,
                            fixture_id=fixture.id,
                            observation_id=primary.observation_id,
                            detail="same resource; registry update available: " + "; ".join(drift),
                        )
                    )
                else:
                    entries.append(
                        DiscoveryEntry(
                            status=DiscoveryEntryStatus.BOUND_READY,
                            fixture_id=fixture.id,
                            observation_id=primary.observation_id,
                            detail="unchanged",
                        )
                    )
            continue

        # Binding resource not observed (or fixture has no binding):
        # propose candidates from unclaimed observations.
        prev_name = None
        if expected:
            prev_name = prev_names.get(expected[0])
        fixture_candidates = _score_candidates(fixture, obs_sorted, claimed, prev_name=prev_name)
        kept = sorted(
            (c for c in fixture_candidates if c.confidence >= CANDIDATE_FLOOR),
            key=lambda c: (-c.confidence, c.observation_id),
        )
        candidates.extend(kept)
        entries.append(_replacement_entry(fixture, kept, unbound=fixture.binding is None))

    unbound_entries = [
        DiscoveryEntry(
            status=DiscoveryEntryStatus.AVAILABLE_UNBOUND,
            observation_id=obs.observation_id,
            detail="provider resource with no fixture bound",
        )
        for obs in obs_sorted
        if obs.observation_id not in claimed and not is_aggregate_observation(obs)
    ]
    unbound_entries.sort(key=lambda entry: entry.observation_id or "")
    entries.extend(unbound_entries)

    providers = sorted({obs.provider for obs in obs_sorted})
    candidates.sort(key=lambda c: (c.fixture_id, -c.confidence, c.observation_id))
    summary = _summarize(providers, obs_sorted, candidates, entries)

    return DiscoveryReport(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        providers=providers,
        observations=obs_sorted,
        candidates=candidates,
        entries=entries,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Binding -> expected observation ids


def _expected_observation_ids(binding: Binding | None, obs_index: dict[str, DiscoveryObservation]) -> list[str]:
    """Observation ids that satisfy this binding, primary (first) ahead."""
    if binding is None:
        return []
    if isinstance(binding, HueBinding):
        return [f"{PROVIDER_HUE_V2}:{binding.resource_id}"]
    if isinstance(binding, HaLightBinding):
        return [f"{PROVIDER_HA_LIGHT}:{binding.ha_entity_id}"]
    if isinstance(binding, WledBinding):
        device_segments = sorted(
            index
            for obs in obs_index.values()
            if obs.provider == PROVIDER_WLED and obs.metadata.get("device_id") == binding.device_id
            and (index := _segment_index_of(obs)) is not None
        )
        wanted = binding.segment_ids if binding.segment_ids else device_segments  # empty = whole device
        return [f"{PROVIDER_WLED}:{binding.device_id}:seg:{index}" for index in wanted]
    return []


def observation_matches_binding(
    binding: Binding | None,
    observation: DiscoveryObservation,
    observations: list[DiscoveryObservation] | None = None,
) -> bool:
    """True when ``observation`` is the fixture's existing bound resource."""
    if binding is None:
        return False
    pool = observations if observations is not None else [observation]
    obs_index = {obs.observation_id: obs for obs in pool}
    expected = _expected_observation_ids(binding, obs_index)
    return observation.observation_id in expected


def _segment_index_of(obs: DiscoveryObservation) -> int | None:
    """Segment index from the observation id (``wled:<device>:seg:<n>``)."""
    prefix = f"{PROVIDER_WLED}:{obs.metadata.get('device_id')}:seg:"
    if not obs.observation_id.startswith(prefix):
        return None
    tail = obs.observation_id[len(prefix):]
    if not tail.isdigit():
        return None
    return int(tail)


def _replacement_entry(
    fixture: Fixture,
    kept: list[BindingCandidate],
    *,
    unbound: bool,
) -> DiscoveryEntry:
    if not kept:
        if unbound:
            return DiscoveryEntry(
                status=DiscoveryEntryStatus.MISSING,
                fixture_id=fixture.id,
                detail="fixture unbound and no candidates found",
            )
        return DiscoveryEntry(
            status=DiscoveryEntryStatus.MISSING,
            fixture_id=fixture.id,
            detail="binding resource not observed; no candidates found",
        )
    candidate_ids = [f"{fixture.id}:{candidate.observation_id}" for candidate in kept]
    if len(kept) >= 2 and (kept[0].confidence - kept[1].confidence) < CONFLICT_GAP:
        return DiscoveryEntry(
            status=DiscoveryEntryStatus.CONFLICT,
            fixture_id=fixture.id,
            candidate_ids=candidate_ids,
            detail=f"ambiguous candidates within {CONFLICT_GAP} confidence (review required)",
        )
    return DiscoveryEntry(
        status=DiscoveryEntryStatus.CANDIDATE_REPLACEMENT,
        fixture_id=fixture.id,
        candidate_ids=candidate_ids,
        detail=(
            "fixture unbound; candidate(s) available (review before rebinding)"
            if unbound
            else "previous binding missing; candidate(s) available (review before rebinding)"
        ),
    )


# ---------------------------------------------------------------------------
# Capability comparison (documented rule)


def capability_downgrades(stored: Capabilities | None, observed: Capabilities | None) -> list[str]:
    """Capabilities the binding lost vs the stored fixture capabilities.

    Comparison rule: every capability stored on the fixture must still be
    reported by the observation; extra observed capabilities are never a
    downgrade. ``gradient`` compares presence and then ``max_points``.
    Effect-list changes are ignored (they fluctuate independently of
    scene execution). Unknown (``None``) observed capabilities cannot be
    compared and count as no downgrade.
    """
    if stored is None or observed is None:
        return []
    downgrades: list[str] = []
    if stored.gradient is not None:
        if observed.gradient is None:
            downgrades.append("gradient removed")
        elif observed.gradient.max_points < stored.gradient.max_points:
            downgrades.append(f"gradient points reduced ({stored.gradient.max_points} -> {observed.gradient.max_points})")
    if stored.dynamic_native and not observed.dynamic_native:
        downgrades.append("dynamic_native lost")
    if stored.color_xy and not observed.color_xy:
        downgrades.append("color_xy lost")
    if stored.color_temp is not None and observed.color_temp is None:
        downgrades.append("color_temp lost")
    if stored.brightness and not observed.brightness:
        downgrades.append("brightness lost")
    return downgrades


def capability_mismatch(stored: Capabilities | None, observed: Capabilities | None) -> str | None:
    """Why a candidate observation cannot fulfill the fixture, if ever."""
    if stored is None or observed is None:
        return None
    if stored.gradient is not None and observed.gradient is None:
        return "gradient required"
    if stored.color_xy and not observed.color_xy:
        return "color_xy required"
    if stored.color_temp is not None and observed.color_temp is None:
        return "color_temp required"
    if stored.dynamic_native and not observed.dynamic_native:
        return "dynamic_native required"
    if stored.brightness and not observed.brightness:
        return "brightness required"
    return None


# ---------------------------------------------------------------------------
# Candidate scoring


def _score_candidates(
    fixture: Fixture,
    obs_sorted: list[DiscoveryObservation],
    claimed: set[str],
    *,
    prev_name: str | None,
) -> list[BindingCandidate]:
    provider = _binding_provider(fixture.binding)
    out: list[BindingCandidate] = []
    for obs in obs_sorted:
        if obs.observation_id in claimed:
            continue
        if is_aggregate_observation(obs):
            continue  # HA group helpers are not adoptable atomic fixtures
        if provider is not None and obs.provider != provider:
            continue  # candidates keep the previous provider
        out.append(_score_candidate(fixture, obs, prev_name=prev_name))
    return out


def _binding_provider(binding: Binding | None) -> str | None:
    if isinstance(binding, (HueBinding, WledBinding, HaLightBinding)):
        return binding.provider
    return None


def _score_candidate(
    fixture: Fixture,
    obs: DiscoveryObservation,
    *,
    prev_name: str | None,
) -> BindingCandidate:
    confidence = _BASE_CONFIDENCE
    reasons: list[str] = []

    binding = fixture.binding
    if isinstance(binding, WledBinding) and binding.device_id == obs.metadata.get("device_id"):
        confidence += _WEIGHT_DEVICE_ID
        reasons.append("wled device_id matches previous binding")
    if _ha_entity_linkage(binding, obs):
        confidence += _WEIGHT_HA_ENTITY
        reasons.append("HA entity linkage matches previous binding")

    similarity, similarity_label = _name_similarity(obs.name, fixture.name, prev_name)
    if similarity >= 0.999:
        confidence += _WEIGHT_NAME_EXACT
        reasons.append(f"friendly name unchanged ({similarity_label})")
    elif similarity >= _NAME_SIMILAR_THRESHOLD:
        confidence += _WEIGHT_NAME_FACTOR * similarity
        reasons.append(f"friendly name similar to {similarity_label} ({similarity:.2f})")

    if fixture.location and obs.location_hint and _norm(fixture.location) == _norm(obs.location_hint):
        confidence += _WEIGHT_LOCATION
        reasons.append("location hint matches fixture location")

    compatibility = CandidateCompatibility.COMPATIBLE
    mismatch = capability_mismatch(fixture.capabilities, obs.capabilities)
    if mismatch is not None:
        compatibility = CandidateCompatibility.INCOMPATIBLE
        reasons.append(f"capability mismatch ({mismatch})")
    elif obs.capabilities is not None:
        confidence += _WEIGHT_CAPABILITY
        reasons.append("capabilities compatible")
    else:
        reasons.append("observed capabilities unknown")

    if (
        isinstance(binding, WledBinding)
        and binding.endpoint_hint
        and obs.endpoint_hint
        and binding.endpoint_hint == obs.endpoint_hint
    ):
        confidence += _WEIGHT_ENDPOINT
        reasons.append("endpoint hint matches previous binding")

    return BindingCandidate(
        fixture_id=fixture.id,
        observation_id=obs.observation_id,
        compatibility=compatibility,
        confidence=round(min(confidence, 1.0), 4),
        reasons=reasons,
    )


def _ha_entity_linkage(binding: Binding | None, obs: DiscoveryObservation) -> bool:
    linked = obs.metadata.get("ha_entity_ids")
    linked_ids = set(linked) if isinstance(linked, (list, tuple)) else set()
    if not linked_ids:
        return False
    if isinstance(binding, HueBinding) and binding.ha_entity_id:
        return binding.ha_entity_id in linked_ids
    if isinstance(binding, WledBinding):
        return bool(set(binding.ha_entity_ids) & linked_ids)
    if isinstance(binding, HaLightBinding):
        return binding.ha_entity_id in linked_ids
    return False


def _name_similarity(
    obs_name: str,
    fixture_name: str,
    prev_name: str | None,
) -> tuple[float, str]:
    """Best normalized similarity and which reference produced it."""
    best = SequenceMatcher(None, _norm(obs_name), _norm(fixture_name)).ratio()
    label = "fixture name"
    if prev_name:
        prev_similarity = SequenceMatcher(None, _norm(obs_name), _norm(prev_name)).ratio()
        if prev_similarity > best:
            best = prev_similarity
            label = "previous observed name"
    return best, label


def _norm(name: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name).casefold().split())


# ---------------------------------------------------------------------------
# Summary


def _summarize(
    providers: list[str],
    observations: list[DiscoveryObservation],
    candidates: list[BindingCandidate],
    entries: list[DiscoveryEntry],
) -> dict:
    counts = {status: 0 for status in DiscoveryEntryStatus}
    for entry in entries:
        counts[entry.status] += 1
    return {
        "providers": len(providers),
        "observations_total": len(observations),
        "candidates_total": len(candidates),
        "fixtures_bound_ready": counts[DiscoveryEntryStatus.BOUND_READY],
        "fixtures_reconcile_available": counts[DiscoveryEntryStatus.BOUND_RECONCILE_AVAILABLE],
        "fixtures_missing": counts[DiscoveryEntryStatus.MISSING],
        "fixtures_disabled": counts[DiscoveryEntryStatus.DISABLED],
        "fixtures_degraded": counts[DiscoveryEntryStatus.BOUND_DEGRADED],
        "candidate_replacements": counts[DiscoveryEntryStatus.CANDIDATE_REPLACEMENT],
        "unbound_observations": counts[DiscoveryEntryStatus.AVAILABLE_UNBOUND],
        "conflicts": counts[DiscoveryEntryStatus.CONFLICT],
    }
