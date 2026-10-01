"""Contract smoke tests: the sample dataset must validate against Phase 1 models.

These files are also the seed data for Wave A (store), A2 (discovery),
A3 (migration), and A4 (Workbench mocks) — keep them realistic.
"""

import json
from pathlib import Path

import pytest

from scene_studio import (
    DiscoveryReport,
    FixtureRegistry,
    HealthStatus,
    Scene,
    derive_health,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def registry_data() -> dict:
    return json.loads((FIXTURES_DIR / "registry.sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def scenes_data() -> dict:
    return json.loads((FIXTURES_DIR / "scenes.sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def discovery_data() -> dict:
    return json.loads((FIXTURES_DIR / "discovery.sample.json").read_text(encoding="utf-8"))


def test_registry_sample_validates(registry_data):
    registry = FixtureRegistry.from_dict(registry_data)
    assert len(registry.fixtures) == 24
    assert len(registry.targets) == 5
    assert {target.id for target in registry.targets} == {
        "studio",
        "whole_house",
        "bathroom",
        "bedroom",
        "living_room",
    }
    assert registry == FixtureRegistry.from_dict(registry.to_dict())


def test_registry_sample_targets_resolve_through_fixture_groups(registry_data):
    """Target membership is the authoritative group resolution rule."""
    registry = FixtureRegistry.from_dict(registry_data)
    by_id = {fixture.id: fixture for fixture in registry.fixtures}

    studio = [f.id for f in registry.fixtures if "studio" in f.groups]
    whole = [f.id for f in registry.fixtures if "whole_house" in f.groups]
    bathroom = [f.id for f in registry.fixtures if "bathroom" in f.groups]

    assert set(studio) >= {"g_strip", "wled_seg_0", "wled_seg_5", "double_strip", "office_strip"}
    assert not any("office" in f.groups for f in registry.fixtures)
    assert "office_lights" not in by_id
    # whole_house covers every fixture except the two confirmed-not-in-use
    # lights (water_light / food_light carry no groups at all); the disabled
    # double_strip keeps its groups, so 24 fixtures - 2 = 22 members.
    assert set(whole) == set(by_id) - {"water_light", "food_light"}
    assert len(whole) == 22
    assert set(bathroom) == {
        "bathroom_mirror_left",
        "bathroom_mirror_right",
        "bathroom_mirror_mid",
        "bathroom_main",
    }
    assert {f.id for f in registry.fixtures if "bedroom" in f.groups} == {
        "bedroom_closet", "desk_lamp", "reading_lamp"
    }
    assert {f.id for f in registry.fixtures if "living_room" in f.groups} == {"living_room_lamp"}
    # Provisional-location fixtures stay out of room groups until binding review.
    # authoritative HA areas resolved 2026-09-11 (both lamps are Bedroom)
    assert by_id["desk_lamp"].location == "bedroom" and "bedroom" in by_id["desk_lamp"].groups
    assert by_id["reading_lamp"].location == "bedroom" and "bedroom" in by_id["reading_lamp"].groups

    disabled = {f.id for f in registry.fixtures if not f.enabled}
    assert disabled == {"double_strip", "water_light", "food_light"}
    assert derive_health(by_id["double_strip"]) is HealthStatus.DISABLED
    assert derive_health(by_id["water_light"]) is HealthStatus.DISABLED
    assert derive_health(by_id["food_light"]) is HealthStatus.DISABLED
    assert derive_health(by_id["custom_gradient"]) is HealthStatus.READY
    assert by_id["custom_gradient"].capability_assessment.status.value == "limited"


def test_registry_sample_new_hue_bindings_are_captured_from_the_live_pull(registry_data):
    """The 2026-09-11 registry extension binds the real bridge resource ids (no HA crosswalk yet)."""
    registry = FixtureRegistry.from_dict(registry_data)
    expected_resource_ids = {
        "bathroom_mirror_left": "026b71a8-f85a-455f-b317-90baf5f20e10",
        "living_room_lamp": "50f25ba9-1b46-47b3-af8d-dda9b7afdc55",
        "bathroom_mirror_right": "5602549d-ffa2-46dd-b453-22b7e4ac1529",
        "desk_lamp": "5fc3af70-d9c7-4cf8-ac18-6d08fecd7410",
        "bathroom_main": "5fd91f87-0a0f-45e3-a050-0f9f2f1d6012",
        "reading_lamp": "5fdc04d4-baa2-4f27-a28c-a48dac323144",
        "bathroom_mirror_mid": "8490611a-5c77-40cc-b836-429667be6cc6",
        "bedroom_closet": "9fc58253-0598-4dd4-98b1-b07febfacb89",
        "water_light": "ae1c4b40-9844-445f-ae52-bb7e30c1a16f",
        "food_light": "d7804c30-d7e7-4271-8964-cd96e3f4eaf4",
    }
    by_id = {fixture.id: fixture for fixture in registry.fixtures}
    for fixture_id, resource_id in expected_resource_ids.items():
        binding = by_id[fixture_id].binding
        assert binding is not None and binding.provider == "hue_v2", fixture_id
        assert binding.resource_id == resource_id, fixture_id
        # v1 crosswalk has no entity mapping for these lights.
        # reconciliation resolved HA entities by unique friendly-name match;
        # unresolved on purpose: bedroom_closet ("Closet"/"Main Light" ambiguity)
        # and living_room_lamp (no HA light entity exists for that Hue name)
        if fixture_id not in ("bedroom_closet", "living_room_lamp"):
            assert getattr(binding, "ha_entity_id", None), fixture_id


def test_discovery_sample_matches_registry_sample(registry_data, discovery_data):
    """The two samples describe the same fixture population (drift guard)."""
    registry = FixtureRegistry.from_dict(registry_data)
    report = DiscoveryReport.from_dict(discovery_data)

    fixture_ids = {fixture.id for fixture in registry.fixtures}
    entry_fixture_ids = {entry.fixture_id for entry in report.entries if entry.fixture_id}
    assert entry_fixture_ids == fixture_ids

    # Every hue observation (except the deliberate unknown strip) is bound by
    # a registry fixture, and capability shape matches the registry.
    bound_resource_ids = {
        fixture.binding.resource_id
        for fixture in registry.fixtures
        if fixture.binding is not None and fixture.binding.provider == "hue_v2"
    }
    observed_hue_ids = {
        obs.provider_resource_id for obs in report.observations if obs.provider == "hue_v2"
    }
    unknown_strips = observed_hue_ids - bound_resource_ids
    assert len(unknown_strips) == 1  # the unbound candidate strip stays unbound

    statuses = {}
    for entry in report.entries:
        if entry.fixture_id:
            statuses.setdefault(entry.fixture_id, set()).add(entry.status)
    from scene_studio import DiscoveryEntryStatus

    for fixture in registry.fixtures:
        if not fixture.enabled:
            # double_strip also carries a candidate_replacement entry; a
            # disabled fixture must always have its disabled entry.
            assert DiscoveryEntryStatus.DISABLED in statuses[fixture.id], fixture.id
    enabled_bound_ready = sorted(
        fixture.id
        for fixture in registry.fixtures
        if fixture.enabled and DiscoveryEntryStatus.BOUND_READY in statuses.get(fixture.id, set())
    )
    assert len(enabled_bound_ready) == 21


def test_scene_samples_validate_and_reference_declared_fixtures(scenes_data, registry_data):
    scenes = [Scene.from_dict(data) for data in scenes_data["scenes"]]
    assert {scene.id for scene in scenes} >= {"twilight", "aurora_flow"}

    known = {fixture["id"] for fixture in registry_data["fixtures"]}
    for scene in scenes:
        unknown = set(scene.fixture_states) - known
        assert not unknown, f"{scene.id} references unknown fixtures: {sorted(unknown)}"


def test_dynamic_scene_contract(scenes_data):
    aurora = next(Scene.from_dict(d) for d in scenes_data["scenes"] if d["id"] == "aurora_flow")
    assert aurora.motion.mode.value == "palette_cycle"
    assert 0.0 <= aurora.motion.speed <= 1.0
    assert aurora.palette, "dynamic scenes carry the canonical palette"
    assert aurora.default_state is not None


def test_migrated_scene_carries_provenance(scenes_data):
    twilight = next(Scene.from_dict(d) for d in scenes_data["scenes"] if d["id"] == "twilight")
    assert twilight.metadata["migrated_from_v1"]["filename"] == "twilight.json"
    assert twilight.schema_version == 2


def test_sample_registry_has_no_aggregate_office_lights(registry_data, scenes_data):
    ids = {fixture["id"] for fixture in registry_data["fixtures"]}
    assert "office_lights" not in ids
    assert "office" not in {target["id"] for target in registry_data["targets"]}
    office_strip = next(item for item in registry_data["fixtures"] if item["id"] == "office_strip")
    assert "studio" in office_strip["groups"]
    assert "office" not in office_strip["groups"]
    for scene in scenes_data["scenes"]:
        assert "office_lights" not in (scene.get("fixture_states") or {})
        assert "office" not in scene["target_ids"]


def test_discovery_sample_validates(discovery_data):
    report = DiscoveryReport.from_dict(discovery_data)
    assert report.finished_at is not None
    assert {entry.status.value for entry in report.entries} >= {
        "bound_ready",
        "candidate_replacement",
        "available_unbound",
        "disabled",
    }
    assert DiscoveryReport.from_dict(report.to_dict()) == report


def test_discovery_sample_summary_matches_entries(discovery_data):
    """Summary counts must be derivable — discovery service keeps them deterministic."""
    report = DiscoveryReport.from_dict(discovery_data)
    statuses = [entry.status for entry in report.entries]
    from scene_studio import DiscoveryEntryStatus

    assert report.summary["fixtures_disabled"] == sum(1 for s in statuses if s == DiscoveryEntryStatus.DISABLED)
    assert report.summary["fixtures_degraded"] == sum(1 for s in statuses if s == DiscoveryEntryStatus.BOUND_DEGRADED)
    assert report.summary["candidate_replacements"] == sum(
        1 for s in statuses if s == DiscoveryEntryStatus.CANDIDATE_REPLACEMENT
    )
    assert report.summary["observations_total"] == len(report.observations)
