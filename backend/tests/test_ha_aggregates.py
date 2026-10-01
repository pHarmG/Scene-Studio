"""HA aggregate/group helpers must not be adopted as atomic fixtures."""

from scene_studio.discovery.aggregates import is_ha_aggregate_light
from scene_studio.discovery.halight import build_halight_observations
from scene_studio.discovery.service import run_discovery
from scene_studio.domain.discovery import DiscoveryEntryStatus
from scene_studio.domain.fixtures import Fixture, FixtureRegistry


STARTED = "2026-09-16T00:00:00Z"


def test_member_list_is_positive_aggregate_evidence():
    assert is_ha_aggregate_light(
        "light.office_lights",
        {"entity_id": ["light.lamp", "light.hue_g_strip"], "friendly_name": "Office Lights"},
    )
    assert not is_ha_aggregate_light("light.lamp", {"friendly_name": "Lamp"})
    assert not is_ha_aggregate_light("light.lamp", {"entity_id": "light.lamp"})


def test_explicit_ignore_list_is_fallback_without_member_metadata():
    assert is_ha_aggregate_light(
        "light.office_lights",
        {"friendly_name": "Office Lights"},
        ignored_entity_ids=("light.office_lights",),
    )


def test_aggregate_observation_is_not_available_unbound():
    observations = build_halight_observations(
        {
            "light.office_lights": {
                "state": "on",
                "attributes": {
                    "friendly_name": "Office Lights",
                    "entity_id": ["light.lamp", "light.hue_g_strip"],
                    "supported_color_modes": ["brightness"],
                },
            },
            "light.lamp": {
                "state": "on",
                "attributes": {"friendly_name": "Lamp", "supported_color_modes": ["xy"]},
            },
        }
    )
    aggregate = next(obs for obs in observations if obs.provider_resource_id == "light.office_lights")
    assert aggregate.metadata["ha_aggregate"] is True
    assert aggregate.metadata["ha_group_members"] == ["light.lamp", "light.hue_g_strip"]

    report = run_discovery(FixtureRegistry(fixtures=[]), observations, run_id="r1", started_at=STARTED)
    unbound = [entry.observation_id for entry in report.entries if entry.status is DiscoveryEntryStatus.AVAILABLE_UNBOUND]
    assert "ha_light:light.office_lights" not in unbound
    assert "ha_light:light.lamp" in unbound


def test_currently_bound_aggregate_still_matches_but_is_flagged():
    from scene_studio.domain.bindings import HaLightBinding
    from scene_studio.domain.capabilities import Capabilities

    fixture = Fixture(
        id="office_lights",
        name="Office Ceiling",
        binding=HaLightBinding(ha_entity_id="light.office_lights"),
        capabilities=Capabilities(on_off=True, brightness=True),
    )
    observations = build_halight_observations(
        {
            "light.office_lights": {
                "state": "on",
                "attributes": {
                    "friendly_name": "Office Lights",
                    "entity_id": ["light.lamp"],
                    "supported_color_modes": ["brightness"],
                },
            }
        }
    )
    report = run_discovery(FixtureRegistry(fixtures=[fixture]), observations, run_id="r1", started_at=STARTED)
    entry = next(item for item in report.entries if item.fixture_id == "office_lights")
    assert entry.status is DiscoveryEntryStatus.BOUND_READY
    assert observations[0].metadata["ha_aggregate"] is True
