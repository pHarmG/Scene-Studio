"""Canonical target membership projection for the HA bridge."""

from scene_studio.domain.bindings import HaLightBinding, HueBinding, WledBinding, binding_ha_entity_ids
from scene_studio.domain.fixtures import Fixture, FixtureRegistry, Target, project_target_membership


def _registry(*fixtures, targets=None):
    return FixtureRegistry(
        fixtures=list(fixtures),
        targets=list(targets or [Target(id="studio", name="Studio")]),
    )


def test_binding_ha_entity_ids_flattens_and_dedupes():
    hue = HueBinding(bridge_id="b", resource_id="r", ha_entity_id="light.hue_g_strip")
    wled = WledBinding(device_id="abc", segment_ids=[0], ha_entity_ids=["light.seg_0", "light.seg_0", "light.seg_1"])
    ha = HaLightBinding(ha_entity_id="light.lamp")
    assert binding_ha_entity_ids(hue) == ["light.hue_g_strip"]
    assert binding_ha_entity_ids(wled) == ["light.seg_0", "light.seg_1"]
    assert binding_ha_entity_ids(ha) == ["light.lamp"]
    assert binding_ha_entity_ids(None) == []


def test_projection_uses_canonical_groups_and_skips_disabled():
    registry = _registry(
        Fixture(
            id="g_strip",
            name="G Strip",
            groups=["studio", "whole_house"],
            binding=HueBinding(bridge_id="b", resource_id="r1", ha_entity_id="light.hue_g_strip"),
        ),
        Fixture(
            id="lamp",
            name="Lamp",
            groups=["studio"],
            binding=HaLightBinding(ha_entity_id="light.lamp"),
        ),
        Fixture(
            id="double_strip",
            name="Double Strip",
            groups=["studio"],
            enabled=False,
            binding=HueBinding(bridge_id="b", resource_id="r2", ha_entity_id="light.double_strip"),
        ),
        Fixture(
            id="wled_seg_0",
            name="WLED 0",
            groups=["studio"],
            binding=WledBinding(device_id="dev", segment_ids=[0], ha_entity_ids=["light.lg_wled_segment_0"]),
        ),
        targets=[Target(id="studio", name="Studio"), Target(id="whole_house", name="Whole House")],
    )
    projected = {item["id"]: item for item in project_target_membership(registry)}
    studio = projected["studio"]
    assert studio["fixture_ids"] == ["g_strip", "lamp", "wled_seg_0"]
    assert studio["ha_entity_ids"] == ["light.hue_g_strip", "light.lamp", "light.lg_wled_segment_0"]
    assert studio["enabled_fixture_count"] == 3
    assert studio["ha_covered_fixture_count"] == 3
    assert studio["complete_ha_coverage"] is True
    assert "double_strip" not in studio["fixture_ids"]


def test_incomplete_ha_coverage_is_reported_honestly():
    registry = _registry(
        Fixture(
            id="g_strip",
            name="G Strip",
            groups=["studio"],
            binding=HueBinding(bridge_id="b", resource_id="r1", ha_entity_id="light.hue_g_strip"),
        ),
        Fixture(
            id="orphan",
            name="Orphan",
            groups=["studio"],
            binding=HueBinding(bridge_id="b", resource_id="r2"),  # no HA entity
        ),
    )
    studio = project_target_membership(registry)[0]
    assert studio["fixture_ids"] == ["g_strip", "orphan"]
    assert studio["ha_entity_ids"] == ["light.hue_g_strip"]
    assert studio["enabled_fixture_count"] == 2
    assert studio["ha_covered_fixture_count"] == 1
    assert studio["complete_ha_coverage"] is False


def test_empty_target_is_not_complete_coverage():
    registry = FixtureRegistry(targets=[Target(id="office", name="Office")], fixtures=[])
    office = project_target_membership(registry)[0]
    assert office["fixture_ids"] == []
    assert office["complete_ha_coverage"] is False
    assert office["enabled_fixture_count"] == 0
