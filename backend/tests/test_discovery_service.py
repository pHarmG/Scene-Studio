"""run_discovery orchestration tests (master plan §16.2 scenarios).

Pure-function coverage: matching statuses, candidate scoring, conflicts,
determinism, and the no-mutation guarantee. All payloads are in-memory
recorded-style dicts; no network access.
"""

import copy
import json
from pathlib import Path

import pytest

from scene_studio import (
    CandidateCompatibility,
    Capabilities,
    ColorTempRange,
    DiscoveryEntryStatus,
    DiscoveryObservation,
    DiscoveryReport,
    Fixture,
    FixtureRegistry,
    GradientCapability,
    HaLightBinding,
    HealthStatus,
    HueBinding,
    WledBinding,
)
from scene_studio.discovery import capability_downgrades, run_discovery

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

STARTED = "2026-09-10T19:00:00Z"
FINISHED = "2026-09-10T19:00:04Z"


# ---------------------------------------------------------------------------
# Builders


def simple_caps(**overrides) -> Capabilities:
    kwargs = dict(on_off=True, brightness=True, color_xy=True)
    kwargs.update(overrides)
    return Capabilities(**kwargs)


def gradient_caps(max_points: int = 5, dynamic_native: bool = True) -> Capabilities:
    return Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=max_points),
        dynamic_native=dynamic_native,
    )


def hue_fixture(
    fixture_id,
    name,
    resource_id=None,
    *,
    location=None,
    enabled=True,
    capabilities=None,
    ha_entity_id=None,
    name_similar_reference=None,
):
    binding = None
    if resource_id is not None:
        binding = HueBinding(
            bridge_id="001788demo000001",
            resource_id=resource_id,
            ha_entity_id=ha_entity_id,
        )
    return Fixture(
        id=fixture_id,
        name=name,
        location=location,
        groups=[],
        enabled=enabled,
        binding=binding,
        capabilities=capabilities,
    )


def wled_fixture(fixture_id, name, segment_ids, *, device_id="aabbccddeeff", location=None, capabilities=None):
    return Fixture(
        id=fixture_id,
        name=name,
        location=location,
        groups=[],
        enabled=True,
        binding=WledBinding(
            device_id=device_id,
            segment_ids=list(segment_ids),
            endpoint_hint="http://wled.local",
        ),
        capabilities=capabilities or Capabilities(on_off=True, brightness=True, dynamic_native=True),
    )


def hue_obs(resource_id, name, *, location=None, capabilities=None, metadata=None):
    return DiscoveryObservation(
        provider="hue_v2",
        provider_resource_id=resource_id,
        name=name,
        location_hint=location,
        capabilities=capabilities or simple_caps(),
        metadata=dict(metadata or {}),
    )


def wled_obs(index, name="Segment", *, device_id="aabbccddeeff", endpoint="http://wled.local"):
    return DiscoveryObservation(
        provider="wled",
        provider_resource_id=f"{device_id}:seg:{index}",
        name=name,
        capabilities=Capabilities(on_off=True, brightness=True, dynamic_native=True),
        endpoint_hint=endpoint,
        metadata={"device_id": device_id, "segment_start": index * 5, "segment_stop": index * 5 + 5},
    )


def entry_for(report, fixture_id):
    return next(entry for entry in report.entries if entry.fixture_id == fixture_id)


def status_for(report, fixture_id) -> str:
    return entry_for(report, fixture_id).status.value


def candidates_for(report, fixture_id):
    return [candidate for candidate in report.candidates if candidate.fixture_id == fixture_id]


# ---------------------------------------------------------------------------
# Matching: bound_ready / bound_degraded / missing / disabled


def test_exact_previous_binding_found_is_bound_ready():
    fixture = hue_fixture(
        "g_strip",
        "Hue G Strip",
        "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a",
        capabilities=simple_caps(),
    )
    observations = [hue_obs("2a2c45a9-8a61-4c04-bdaf-9bd928f9316a", "Hue G Strip")]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "g_strip")
    assert entry.status is DiscoveryEntryStatus.BOUND_READY
    assert entry.observation_id == "hue_v2:2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
    assert entry.detail == "unchanged"
    assert report.candidates == []


def test_equal_or_better_observed_capabilities_are_ready_not_degraded():
    # Extra effect-catalog entries are not operational drift; same control set stays ready.
    fixture = hue_fixture(
        "g_strip",
        "Hue G Strip",
        "aaa-1",
        capabilities=gradient_caps(max_points=5),
    )
    observed = Capabilities(
        on_off=True,
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=5),
        effects=["candle"],
        dynamic_native=True,
    )
    report = run_discovery(
        FixtureRegistry(fixtures=[fixture]),
        [hue_obs("aaa-1", "Hue G Strip", capabilities=observed)],
        run_id="r1",
        started_at=STARTED,
    )
    assert status_for(report, "g_strip") == "bound_ready"


def test_same_resource_with_stale_registry_knowledge_is_reconcile_available():
    fixture = hue_fixture(
        "g_strip",
        "Hue G Strip",
        "aaa-1",
        capabilities=gradient_caps(max_points=3),
    )
    fixture.health = HealthStatus.DEGRADED
    observed = gradient_caps(max_points=5)
    report = run_discovery(
        FixtureRegistry(fixtures=[fixture]),
        [hue_obs("aaa-1", "Hue G Strip", capabilities=observed)],
        run_id="r1",
        started_at=STARTED,
    )
    entry = entry_for(report, "g_strip")
    assert entry.status is DiscoveryEntryStatus.BOUND_RECONCILE_AVAILABLE
    assert "same resource" in entry.detail
    assert report.summary["fixtures_reconcile_available"] == 1
    assert report.summary["fixtures_degraded"] == 0


def test_capability_downgrade_is_bound_degraded():
    fixture = hue_fixture("custom_gradient", "Custom Gradient", "grad-1", capabilities=gradient_caps(max_points=5))
    # Same resource id, but gradient and dynamic execution vanished (firmware update).
    report = run_discovery(
        FixtureRegistry(fixtures=[fixture]),
        [hue_obs("grad-1", "Custom Gradient", capabilities=simple_caps())],
        run_id="r1",
        started_at=STARTED,
    )
    entry = entry_for(report, "custom_gradient")
    assert entry.status is DiscoveryEntryStatus.BOUND_DEGRADED
    assert "gradient removed" in entry.detail
    assert "dynamic_native lost" in entry.detail


def test_gradient_point_reduction_is_degraded():
    fixture = hue_fixture("g_strip", "G Strip", "grad-2", capabilities=gradient_caps(max_points=5))
    report = run_discovery(
        FixtureRegistry(fixtures=[fixture]),
        [hue_obs("grad-2", "G Strip", capabilities=gradient_caps(max_points=3))],
        run_id="r1",
        started_at=STARTED,
    )
    entry = entry_for(report, "g_strip")
    assert entry.status is DiscoveryEntryStatus.BOUND_DEGRADED
    assert "gradient points reduced (5 -> 3)" in entry.detail


def test_capability_downgrades_rule_is_documented_and_direct():
    assert capability_downgrades(None, None) == []
    assert capability_downgrades(simple_caps(), None) == []
    assert capability_downgrades(simple_caps(), simple_caps()) == []
    downgrades = capability_downgrades(
        Capabilities(on_off=True, brightness=True, color_xy=True, color_temp=ColorTempRange(153, 500)),
        Capabilities(on_off=True, brightness=True, color_xy=False),
    )
    assert downgrades == ["color_xy lost", "color_temp lost"]


def test_missing_resource_without_candidates():
    fixture = hue_fixture("lamp", "Lamp", "gone-1", location="attic")
    # Unrelated unbound resource that shares nothing with the fixture.
    observations = [hue_obs("other-1", "Totally Different", location="basement")]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "lamp")
    assert entry.status is DiscoveryEntryStatus.MISSING
    assert "no candidates" in entry.detail
    assert candidates_for(report, "lamp") == []


def test_disabled_fixture_skips_matching_and_candidates():
    fixture = hue_fixture("double_strip", "Double Strip", "ds-1", enabled=False)
    # A tempting same-name replacement exists, but disabled fixtures are skipped.
    observations = [hue_obs("ds-new", "Double Strip")]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    assert status_for(report, "double_strip") == "disabled"
    assert report.candidates == []
    assert [entry.status for entry in report.entries if entry.fixture_id == "double_strip"] == [
        DiscoveryEntryStatus.DISABLED
    ]


# ---------------------------------------------------------------------------
# §16.2: friendly name changed / endpoint changed / topology change


def test_friendly_name_changed_found_via_previous_report():
    """Resource id changed AND friendly name changed on the fixture itself.

    The previous report supplies the old observed name, which is the
    strongest remaining signal for the replacement candidate.
    """
    fixture = hue_fixture("studio_strip", "Bias Light", "old-1", location="studio")
    previous = DiscoveryReport(
        run_id="r0",
        started_at="2026-09-09T19:00:00Z",
        observations=[hue_obs("old-1", "Studio Strip", location="Studio")],
    )
    observations = [
        hue_obs("new-1", "Studio Strip", location="Studio"),  # renamed resource, old friendly name
        hue_obs("distractor", "Random Lamp"),
    ]
    report = run_discovery(
        FixtureRegistry(fixtures=[fixture]),
        observations,
        run_id="r1",
        started_at=STARTED,
        previous=previous,
    )
    entry = entry_for(report, "studio_strip")
    assert entry.status is DiscoveryEntryStatus.CANDIDATE_REPLACEMENT
    candidates = candidates_for(report, "studio_strip")
    assert [c.observation_id for c in candidates] == ["hue_v2:new-1"]
    assert any("previous observed name" in reason for reason in candidates[0].reasons)
    assert candidates[0].confidence >= 0.5
    # The distractor stays unproposed but is reported as available.
    assert any(
        entry.status is DiscoveryEntryStatus.AVAILABLE_UNBOUND and entry.observation_id == "hue_v2:distractor"
        for entry in report.entries
    )


def test_wled_endpoint_change_still_bound_ready():
    """IP/endpoint changed but device identity (MAC) is the same: ready."""
    fixture = wled_fixture("wled_seg_0", "WLED Segment 0", [0])
    moved = [wled_obs(index, endpoint="http://192.0.2.99") for index in range(6)]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), moved, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "wled_seg_0")
    assert entry.status is DiscoveryEntryStatus.BOUND_READY
    assert entry.observation_id == "wled:aabbccddeeff:seg:0"
    assert entry.detail == "unchanged"
    assert report.candidates == []


def test_wled_whole_device_binding_matches_any_observed_segment():
    fixture = Fixture(
        id="wled_all",
        name="WLED Device",
        binding=WledBinding(device_id="aabbccddeeff", segment_ids=[]),
        capabilities=Capabilities(on_off=True, brightness=True, dynamic_native=True),
    )
    observations = [wled_obs(index) for index in range(3)]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "wled_all")
    assert entry.status is DiscoveryEntryStatus.BOUND_READY
    assert entry.observation_id == "wled:aabbccddeeff:seg:0"
    assert report.summary["unbound_observations"] == 0


def test_wled_segment_topology_change_yields_conflict():
    """Segment 5 disappeared; remaining segments are equally plausible -> conflict."""
    fixture = wled_fixture("wled_seg_5", "WLED Segment 5", [5])
    shrunk = [wled_obs(index) for index in range(5)]  # segments 0..4 remain
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), shrunk, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "wled_seg_5")
    assert entry.status is DiscoveryEntryStatus.CONFLICT
    candidates = candidates_for(report, "wled_seg_5")
    assert len(entry.candidate_ids) == 5
    assert len(candidates) == 5
    assert all(c.observation_id.startswith("wled:aabbccddeeff:seg:") for c in candidates)
    assert all("wled device_id matches previous binding" in c.reasons for c in candidates)
    best, second_best = candidates[0].confidence, candidates[1].confidence
    assert best - second_best < 0.15


def test_multiple_candidates_within_gap_conflict():
    fixture = hue_fixture("studio_strip", "Studio Strip", "old-2", location="studio")
    observations = [
        hue_obs("cand-a", "Strip A", location="Studio"),
        hue_obs("cand-b", "Strip B", location="Studio"),
    ]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "studio_strip")
    assert entry.status is DiscoveryEntryStatus.CONFLICT
    candidates = candidates_for(report, "studio_strip")
    assert len(candidates) == 2
    assert candidates[0].confidence - candidates[1].confidence < 0.15


def test_clear_winner_produces_candidate_replacement():
    fixture = hue_fixture("studio_strip", "Studio Strip", "old-3", location="studio")
    observations = [
        hue_obs("winner", "Studio Strip", location="Studio"),
        hue_obs("weak", "Desk Lamp", location="Studio"),
    ]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "studio_strip")
    assert entry.status is DiscoveryEntryStatus.CANDIDATE_REPLACEMENT
    candidates = candidates_for(report, "studio_strip")
    assert candidates[0].observation_id == "hue_v2:winner"
    assert candidates[0].confidence - candidates[1].confidence >= 0.15


def test_incompatible_candidate_is_marked():
    fixture = hue_fixture(
        "g_strip_new",
        "G Strip",
        "old-4",
        location="studio",
        capabilities=gradient_caps(max_points=5),
    )
    # Same name and room but no gradient: visible candidate, flagged incompatible.
    observations = [hue_obs("flat-1", "G Strip", location="Studio", capabilities=simple_caps())]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = entry_for(report, "g_strip_new")
    assert entry.status is DiscoveryEntryStatus.CANDIDATE_REPLACEMENT
    (candidate,) = candidates_for(report, "g_strip_new")
    assert candidate.compatibility is CandidateCompatibility.INCOMPATIBLE
    assert any("gradient required" in reason for reason in candidate.reasons)


def test_unbound_fixture_gets_candidates_and_unmatched_is_missing():
    adoptable = Fixture(id="mystery", name="Office Ceiling", location=None)
    unreachable = Fixture(id="ghost", name="Never Seen Light")
    observations = [
        DiscoveryObservation(
            provider="ha_light",
            provider_resource_id="light.office_lights",
            name="Office Ceiling",
            capabilities=simple_caps(),
        )
    ]
    report = run_discovery(FixtureRegistry(fixtures=[adoptable, unreachable]), observations, run_id="r1", started_at=STARTED)
    assert status_for(report, "mystery") == "candidate_replacement"
    assert "unbound" in entry_for(report, "mystery").detail
    assert status_for(report, "ghost") == "missing"
    assert report.summary["fixtures_missing"] == 1


def test_new_provider_resource_with_no_fixture_is_available_unbound():
    fixture = hue_fixture("lamp", "Lamp", "lamp-1")
    observations = [
        hue_obs("lamp-1", "Lamp"),
        hue_obs(
            "brand-new",
            "Unknown Gradient Strip",
            capabilities=Capabilities(
                on_off=True,
                brightness=True,
                color_xy=True,
                gradient=GradientCapability(max_points=7),
                dynamic_native=True,
            ),
        ),
    ]
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    unbound = [entry for entry in report.entries if entry.status is DiscoveryEntryStatus.AVAILABLE_UNBOUND]
    assert [entry.observation_id for entry in unbound] == ["hue_v2:brand-new"]
    assert report.summary["unbound_observations"] == 1


def test_ha_light_binding_matches_entity_observation():
    fixture = Fixture(
        id="office_lights",
        name="Office Ceiling",
        binding=HaLightBinding(ha_entity_id="light.office_lights"),
        capabilities=Capabilities(on_off=True, brightness=True, color_temp=ColorTempRange(153, 454)),
    )
    observation = DiscoveryObservation(
        provider="ha_light",
        provider_resource_id="light.office_lights",
        name="Office Ceiling",
        capabilities=Capabilities(on_off=True, brightness=True, color_temp=ColorTempRange(153, 454)),
    )
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), [observation], run_id="r1", started_at=STARTED)
    assert status_for(report, "office_lights") == "bound_ready"
    assert entry_for(report, "office_lights").observation_id == "ha_light:light.office_lights"


# ---------------------------------------------------------------------------
# Determinism, purity, contract round-trip


def _full_observation_set():
    return [
        hue_obs("2a2c45a9-8a61-4c04-bdaf-9bd928f9316a", "Hue G Strip", location="Studio", capabilities=gradient_caps()),
        hue_obs("73acf87a-8835-435d-b422-e889c5017dac", "Lamp", location="Studio"),
        hue_obs("brand-new", "Unknown Gradient Strip", capabilities=gradient_caps(max_points=7)),
        wled_obs(0),
        wled_obs(1),
        DiscoveryObservation(
            provider="ha_light",
            provider_resource_id="light.office_lights",
            name="Office Ceiling",
            capabilities=simple_caps(color_xy=False),
        ),
    ]


def _full_registry():
    return FixtureRegistry(
        fixtures=[
            hue_fixture(
                "g_strip",
                "Hue G Strip",
                "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a",
                location="studio",
                capabilities=gradient_caps(),
            ),
            hue_fixture("lamp", "Lamp", "73acf87a-8835-435d-b422-e889c5017dac", location="studio"),
            hue_fixture("double_strip", "Double Strip", "missing-1", enabled=False),
            hue_fixture("gone", "Vanished Light", "missing-2", location="attic"),
            wled_fixture("wled_seg_0", "WLED Segment 0", [0]),
            wled_fixture("wled_seg_1", "WLED Segment 1", [1]),
            Fixture(
                id="office_lights",
                name="Office Ceiling",
                binding=HaLightBinding(ha_entity_id="light.office_lights"),
            ),
        ]
    )


def test_report_is_deterministic_and_timestamps_come_only_from_args():
    registry = _full_registry()
    observations = _full_observation_set()
    first = run_discovery(
        registry, observations, run_id="run-a", started_at=STARTED, finished_at=FINISHED
    )
    second = run_discovery(
        registry, observations, run_id="run-a", started_at=STARTED, finished_at=FINISHED
    )
    assert json.dumps(first.to_dict()) == json.dumps(second.to_dict())
    assert first == second

    open_ended = run_discovery(registry, observations, run_id="run-b", started_at=STARTED)
    assert "finished_at" not in open_ended.to_dict()
    assert open_ended.started_at == STARTED
    assert open_ended.providers == sorted(open_ended.providers)


def test_run_discovery_never_mutates_registry_or_inputs():
    registry = FixtureRegistry.from_dict(json.loads((FIXTURES_DIR / "registry.sample.json").read_text(encoding="utf-8")))
    snapshot = copy.deepcopy(registry.to_dict())
    observations = _full_observation_set()
    list_snapshot = list(observations)

    run_discovery(registry, observations, run_id="run-x", started_at=STARTED, finished_at=FINISHED)

    assert registry.to_dict() == snapshot
    assert json.dumps(registry.to_dict()) == json.dumps(snapshot)
    assert observations == list_snapshot  # order and contents untouched


def test_report_round_trips_through_domain_model():
    report = run_discovery(
        _full_registry(), _full_observation_set(), run_id="run-a", started_at=STARTED, finished_at=FINISHED
    )
    restored = DiscoveryReport.from_dict(report.to_dict())
    assert restored == report
    assert {entry.status.value for entry in report.entries} >= {
        "bound_ready",
        "missing",
        "available_unbound",
        "disabled",
    }


def test_summary_counts_match_entries_and_sample_keys():
    report = run_discovery(
        _full_registry(), _full_observation_set(), run_id="run-a", started_at=STARTED, finished_at=FINISHED
    )
    statuses = [entry.status for entry in report.entries]
    assert report.summary["fixtures_disabled"] == sum(1 for s in statuses if s is DiscoveryEntryStatus.DISABLED)
    assert report.summary["fixtures_degraded"] == sum(1 for s in statuses if s is DiscoveryEntryStatus.BOUND_DEGRADED)
    assert report.summary["fixtures_missing"] == sum(1 for s in statuses if s is DiscoveryEntryStatus.MISSING)
    assert report.summary["candidate_replacements"] == sum(
        1 for s in statuses if s is DiscoveryEntryStatus.CANDIDATE_REPLACEMENT
    )
    assert report.summary["conflicts"] == sum(1 for s in statuses if s is DiscoveryEntryStatus.CONFLICT)
    assert report.summary["unbound_observations"] == sum(
        1 for s in statuses if s is DiscoveryEntryStatus.AVAILABLE_UNBOUND
    )
    assert report.summary["fixtures_bound_ready"] == sum(1 for s in statuses if s is DiscoveryEntryStatus.BOUND_READY)
    assert report.summary["fixtures_reconcile_available"] == sum(
        1 for s in statuses if s is DiscoveryEntryStatus.BOUND_RECONCILE_AVAILABLE
    )
    assert report.summary["observations_total"] == len(report.observations)

    # Summary keys are a superset of the frozen discovery.sample.json keys.
    sample = json.loads((FIXTURES_DIR / "discovery.sample.json").read_text(encoding="utf-8"))
    assert set(sample["summary"]) <= set(report.summary)


def test_candidates_and_entries_are_sorted_deterministically():
    report = run_discovery(
        _full_registry(), _full_observation_set(), run_id="run-a", started_at=STARTED, finished_at=FINISHED
    )
    candidate_keys = [(c.fixture_id, -c.confidence, c.observation_id) for c in report.candidates]
    assert candidate_keys == sorted(candidate_keys)
    fixture_entry_ids = [entry.fixture_id for entry in report.entries if entry.fixture_id]
    unbound_ids = [entry.observation_id for entry in report.entries if entry.status is DiscoveryEntryStatus.AVAILABLE_UNBOUND]
    assert unbound_ids == sorted(unbound_ids)
    # Registry order is preserved for fixture entries (input order is deterministic).
    assert fixture_entry_ids == [f.id for f in _full_registry().fixtures if f.id in set(fixture_entry_ids)]


@pytest.mark.parametrize(
    "observation",
    [
        hue_obs("x-1", "X Light"),
        wled_obs(0, device_id="ffeeddccbbaa"),
        DiscoveryObservation(provider="ha_light", provider_resource_id="light.x", name="X"),
    ],
)
def test_observation_without_matching_fixture_is_available_unbound(observation):
    report = run_discovery(FixtureRegistry(), [observation], run_id="r1", started_at=STARTED)
    (entry,) = report.entries
    assert entry.status is DiscoveryEntryStatus.AVAILABLE_UNBOUND
    assert entry.observation_id == observation.observation_id
