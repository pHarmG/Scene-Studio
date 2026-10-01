import pytest

from scene_studio.domain.discovery import (
    BindingCandidate,
    CandidateCompatibility,
    DiscoveryEntry,
    DiscoveryEntryStatus,
    DiscoveryObservation,
    DiscoveryReport,
)
from scene_studio.domain.fidelity import (
    FidelityLevel,
    FixtureRenderPlan,
    ProviderOperation,
    RenderPlan,
)
from scene_studio.domain.serde import ValidationError


# ---------------------------------------------------------------------------
# discovery observations & candidates
# ---------------------------------------------------------------------------

def test_observation_id_is_provider_scoped_and_deterministic():
    observation = DiscoveryObservation(
        provider="hue_v2", provider_resource_id="2a2c45a9", name="Hue G Strip", location_hint="Studio"
    )
    assert observation.observation_id == "hue_v2:2a2c45a9"
    data = observation.to_dict()
    assert data["observation_id"] == "hue_v2:2a2c45a9"
    assert DiscoveryObservation.from_dict(data).observation_id == observation.observation_id


def test_candidate_confidence_is_bounded_and_reasons_required_shape():
    candidate = BindingCandidate(
        fixture_id="g_strip",
        observation_id="hue_v2:2a2c45a9",
        confidence=0.98,
        reasons=["previous binding resource_id match"],
    )
    data = candidate.to_dict()
    assert data["compatibility"] == "compatible"
    assert BindingCandidate.from_dict(data) == candidate

    with pytest.raises(ValidationError, match="confidence"):
        BindingCandidate.from_dict({**data, "confidence": 1.5})
    with pytest.raises(ValidationError, match="fixture_id"):
        BindingCandidate.from_dict({**data, "fixture_id": "Not Valid"})


def test_candidate_can_be_marked_incompatible():
    candidate = BindingCandidate(
        fixture_id="g_strip",
        observation_id="hue_v2:deadbeef",
        compatibility=CandidateCompatibility.INCOMPATIBLE,
        confidence=0.2,
        reasons=["capability downgrade: no gradient"],
    )
    assert candidate.to_dict()["compatibility"] == "incompatible"


def test_report_round_trip_with_all_statuses():
    report = DiscoveryReport(
        run_id="run-1",
        started_at="2026-09-10T21:00:00Z",
        finished_at="2026-09-10T21:00:05Z",
        providers=["hue_v2", "wled"],
        entries=[
            DiscoveryEntry(status=DiscoveryEntryStatus.BOUND_READY, fixture_id="g_strip"),
            DiscoveryEntry(status=DiscoveryEntryStatus.MISSING, fixture_id="double_strip", detail="not observed"),
            DiscoveryEntry(
                status=DiscoveryEntryStatus.CANDIDATE_REPLACEMENT,
                fixture_id="double_strip",
                candidate_ids=["double_strip:wled:ff"],
            ),
            DiscoveryEntry(status=DiscoveryEntryStatus.AVAILABLE_UNBOUND, observation_id="wled:ff:6"),
            DiscoveryEntry(status=DiscoveryEntryStatus.CONFLICT, detail="two strong candidates"),
        ],
    )
    data = report.to_dict()
    assert DiscoveryReport.from_dict(data) == report


def test_report_rejects_unknown_entry_status():
    with pytest.raises(ValidationError, match="status"):
        DiscoveryReport.from_dict(
            {"run_id": "r", "started_at": "2026-09-10T21:00:00Z", "entries": [{"status": "zombie"}]}
        )


# ---------------------------------------------------------------------------
# render plans (renderer contract, wave B exit gate)
# ---------------------------------------------------------------------------

def test_render_plan_round_trip():
    plan = RenderPlan(
        scene_id="twilight",
        target_ids=["studio"],
        fixture_plans=[
            FixtureRenderPlan(
                fixture_id="g_strip",
                provider="hue_v2",
                fidelity=FidelityLevel.NATIVE,
                operations=[
                    ProviderOperation(
                        provider="hue_v2",
                        op="hue.put_light",
                        resource_ref="2a2c45a9",
                        payload={"gradient": {"points": []}},
                        description="static gradient",
                    )
                ],
            ),
            FixtureRenderPlan(
                fixture_id="office_lights",
                provider="ha_light",
                fidelity=FidelityLevel.APPROXIMATE,
                reason="no gradient support; nearest single color",
            ),
        ],
        skipped_fixture_ids=["double_strip"],
        notes=["double_strip disabled; skipped"],
    )
    data = plan.to_dict()
    assert RenderPlan.from_dict(data) == plan


def test_render_plan_rejects_bad_fidelity():
    with pytest.raises(ValidationError, match="fidelity"):
        RenderPlan.from_dict(
            {
                "scene_id": "s",
                "target_ids": ["office"],
                "fixture_plans": [
                    {"fixture_id": "x", "provider": "ha_light", "fidelity": "perfect"}
                ],
            }
        )
