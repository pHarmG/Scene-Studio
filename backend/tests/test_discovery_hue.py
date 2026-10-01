"""Hue CLIP v2 observation builder tests (recorded payloads, no network)."""

import json
from pathlib import Path

import pytest

from scene_studio.discovery import build_hue_observations

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"

G_STRIP_ID = "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
LAMP_ID = "73acf87a-8835-435d-b422-e889c5017dac"
MIRROR_ID = "026b71a8-f85a-455f-b317-90baf5f20e10"
DOUBLE_STRIP_ID = "b55f34cf-ce41-476b-a30e-44ab4972f0c1"


@pytest.fixture(scope="module")
def clip_lights() -> dict:
    return json.loads((RECORDED / "hue_clip_lights.twilight.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def clip_rooms() -> dict:
    return json.loads((RECORDED / "hue_clip_rooms.twilight.json").read_text(encoding="utf-8"))


def _by_id(observations, resource_id):
    return next(obs for obs in observations if obs.provider_resource_id == resource_id)


def test_observes_every_light_never_filtering_by_room(clip_lights, clip_rooms):
    observations = build_hue_observations(clip_lights, clip_rooms)
    assert {obs.provider for obs in observations} == {"hue_v2"}
    assert len(observations) == 4
    assert {obs.provider_resource_id for obs in observations} == {
        G_STRIP_ID,
        LAMP_ID,
        MIRROR_ID,
        DOUBLE_STRIP_ID,
    }


def test_stable_resource_id_and_metadata_name(clip_lights, clip_rooms):
    observations = build_hue_observations(clip_lights, clip_rooms)
    g_strip = _by_id(observations, G_STRIP_ID)
    assert g_strip.observation_id == f"hue_v2:{G_STRIP_ID}"
    assert g_strip.name == "Hue G Strip"
    assert g_strip.metadata["hue_id_v1"] == "/lights/11"
    assert g_strip.metadata["archetype"] == "hue_lightstrip"


def test_room_membership_becomes_location_hint(clip_lights, clip_rooms):
    observations = build_hue_observations(clip_lights, clip_rooms)
    assert _by_id(observations, G_STRIP_ID).location_hint == "Studio"
    assert _by_id(observations, MIRROR_ID).location_hint == "Bathroom"
    # Provider topology remains a provider RID, never the logical target id.
    g_strip = _by_id(observations, G_STRIP_ID)
    assert g_strip.metadata["hue_group_id"] == "room-001"
    assert g_strip.metadata["hue_group_type"] == "room"


def test_light_without_room_is_still_observed(clip_lights):
    # Rooms that reference only some devices: unreferenced lights still appear.
    rooms = {
        "data": [
            {
                "id": "room-001",
                "metadata": {"name": "Studio"},
                "children": [{"rid": "fe67ac77-2d0e-47bc-bd29-fed981383175", "rtype": "device"}],
            }
        ]
    }
    observations = build_hue_observations(clip_lights, rooms)
    lamp = _by_id(observations, LAMP_ID)
    assert lamp.location_hint is None
    assert lamp.name == "Lamp"
    assert len(observations) == 4


def test_gradient_capabilities_from_device_report(clip_lights):
    observations = build_hue_observations(clip_lights)
    g_strip = _by_id(observations, G_STRIP_ID).capabilities
    assert g_strip.gradient is not None
    assert g_strip.gradient.max_points == 5  # points_capable reported by the device
    assert g_strip.color_xy is True
    assert g_strip.color_temp is not None
    assert (g_strip.color_temp.mirek_min, g_strip.color_temp.mirek_max) == (153, 500)
    assert g_strip.brightness is True
    assert g_strip.dynamic_native is True  # dynamics report dynamic_palette
    assert "candle" in g_strip.effects and "prism" in g_strip.effects


def test_dynamic_native_flags(clip_lights):
    observations = build_hue_observations(clip_lights)
    # Lamp reports dynamic_palette -> native dynamic execution.
    assert _by_id(observations, LAMP_ID).capabilities.dynamic_native is True
    # Double Strip reports neither dynamics nor effects -> not native.
    assert _by_id(observations, DOUBLE_STRIP_ID).capabilities.dynamic_native is False
    # Mirror light supports only the candle effect (on-device animation).
    assert _by_id(observations, MIRROR_ID).capabilities.dynamic_native is True
    assert _by_id(observations, MIRROR_ID).capabilities.gradient is None
    assert _by_id(observations, MIRROR_ID).capabilities.color_temp is None  # fixed mired, no schema


def test_ha_states_enrich_metadata_without_touching_identity(clip_lights):
    ha_states = {
        "light.hue_g_strip": {"friendly_name": "Hue G Strip"},
        "light.other": {"friendly_name": "Unrelated"},
        "switch.plug": {"friendly_name": "Hue G Strip"},  # non-light: ignored
    }
    observations = build_hue_observations(clip_lights, None, ha_states)
    g_strip = _by_id(observations, G_STRIP_ID)
    assert g_strip.metadata["ha_entity_ids"] == ["light.hue_g_strip"]
    lamp = _by_id(observations, LAMP_ID)
    assert "ha_entity_ids" not in lamp.metadata


def test_missing_or_malformed_payloads_produce_no_observations():
    assert build_hue_observations({"data": []}) == []
    assert build_hue_observations({}) == []
    assert build_hue_observations({"data": [{"id": "abc", "metadata": {"name": "No Cap"}}]})
    assert build_hue_observations({"data": [{"metadata": {"name": "no id"}}]}) == []


def test_gradient_fallback_when_no_size_reported():
    payload = {"data": [{"id": "abc-1", "metadata": {"name": "Grad"}, "gradient": {}}]}
    (obs,) = build_hue_observations(payload)
    assert obs.capabilities.gradient.max_points == 5  # documented default


def test_builder_is_deterministic(clip_lights, clip_rooms):
    first = build_hue_observations(clip_lights, clip_rooms)
    second = build_hue_observations(clip_lights, clip_rooms)
    assert [obs.to_dict() for obs in first] == [obs.to_dict() for obs in second]


def test_room_payload_without_matching_devices_is_harmless(clip_lights):
    rooms = {"data": [{"id": "room-x", "metadata": {"name": "Nowhere"}, "children": [{"rid": "nope", "rtype": "device"}]}]}
    observations = build_hue_observations(clip_lights, rooms)
    assert all(obs.location_hint is None for obs in observations)
