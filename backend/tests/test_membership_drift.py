"""HA-area / Scene-Studio-group membership drift report."""

from scene_studio.discovery.drift import membership_drift_report
from scene_studio.discovery.halight import build_halight_observations
from scene_studio.domain.fixtures import FixtureRegistry


def test_drift_flags_aggregate_helper_duplicate_entity():
    registry = FixtureRegistry.from_dict(
        {
            "schema_version": 2,
            "targets": [
                {"id": "studio", "name": "Studio"},
                {"id": "office", "name": "Office"},
                {"id": "whole_house", "name": "Whole House"},
            ],
            "fixtures": [
                {
                    "id": "lamp",
                    "name": "Lamp",
                    "groups": ["studio", "whole_house"],
                    "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
                },
                {
                    "id": "office_lights",
                    "name": "Office Lights",
                    "groups": ["office", "whole_house"],
                    "binding": {"provider": "ha_light", "ha_entity_id": "light.office_lights"},
                },
                {
                    "id": "dup",
                    "name": "Dup",
                    "groups": ["studio"],
                    "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
                },
            ],
        }
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
            },
            "light.lamp": {"state": "on", "attributes": {"friendly_name": "Lamp", "supported_color_modes": ["xy"]}},
        }
    )
    report = membership_drift_report(registry, observations)
    assert "duplicate_ha_entity" in report["flags"]
    assert report["duplicate_ha_entities"]["light.lamp"] == ["dup", "lamp"]
    office_row = next(row for row in report["rows"] if row["fixture_id"] == "office_lights")
    assert "aggregate_helper_as_fixture" in office_row["flags"]


def test_drift_flags_empty_target_after_aggregate_removed():
    registry = FixtureRegistry.from_dict(
        {
            "schema_version": 2,
            "targets": [{"id": "office", "name": "Office"}, {"id": "studio", "name": "Studio"}],
            "fixtures": [
                {
                    "id": "lamp",
                    "name": "Lamp",
                    "groups": ["studio"],
                    "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
                }
            ],
        }
    )
    report = membership_drift_report(registry, [])
    assert report["empty_targets"] == ["office"]
    assert "empty_stale_target" in report["flags"]


def test_drift_flags_room_mismatch_and_incomplete_coverage():
    registry = FixtureRegistry.from_dict(
        {
            "schema_version": 2,
            "targets": [{"id": "studio", "name": "Studio"}],
            "fixtures": [
                {
                    "id": "lamp",
                    "name": "Lamp",
                    "groups": ["studio"],
                    "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
                },
                {
                    "id": "orphan",
                    "name": "Orphan",
                    "groups": ["studio"],
                    "binding": {"provider": "hue_v2", "bridge_id": "b", "resource_id": "rid-1"},
                },
            ],
        }
    )
    observations = build_halight_observations(
        {
            "light.lamp": {
                "state": "on",
                "attributes": {
                    "friendly_name": "Lamp",
                    "supported_color_modes": ["xy"],
                },
            }
        }
    )
    observations[0].location_hint = "Office"
    report = membership_drift_report(registry, observations)
    lamp_row = next(row for row in report["rows"] if row["fixture_id"] == "lamp")
    assert "room_mismatch" in lamp_row["flags"]
    assert "incomplete_ha_coverage" in report["flags"]
    assert report["incomplete_targets"][0]["id"] == "studio"
