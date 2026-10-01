"""Legacy event mapper tests (plan §9.5) — pure mapping + engine end-to-end.

Covers: slugified name routing for apply/delete, save passthrough, the
deterministic generate_office_scene mapping (no randomness, even gradient
sampling, cycled palette at default brightness), and the full sequence
execution through ``SceneStudioEngine.handle_legacy`` including the
regenerate (upsert) path.
"""

import pytest

from scene_studio.service import legacy
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore


@pytest.fixture
def store(tmp_path):
    """Office target with a gradient fixture, a WLED fixture, and an HA light."""
    store = SceneStudioStore(tmp_path)
    fixtures = store.fixtures
    fixtures.add_fixture(
        {
            "id": "g_strip",
            "name": "Hue G Strip",
            "groups": ["office"],
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "g-strip-rid"},
            "capabilities": {
                "on_off": True,
                "brightness": True,
                "color_xy": True,
                "gradient": {"max_points": 5},
                "dynamic_native": True,
            },
        }
    )
    fixtures.add_fixture(
        {
            "id": "wled_seg",
            "name": "WLED Segment",
            "groups": ["office"],
            "binding": {"provider": "wled", "device_id": "aabbccddeeff", "segment_ids": [0]},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    fixtures.add_fixture(
        {
            "id": "lamp",
            "name": "Lamp",
            "groups": ["office"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    fixtures.add_target({"id": "office", "name": "Office"})
    return store


def make_engine(store):
    return SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(),
    )


# ---------------------------------------------------------------------------
# simple mappers
# ---------------------------------------------------------------------------


def test_apply_scene_event_slugifies_case_insensitively():
    envelope = legacy.map_apply_scene_event({"scene_name": "Twilight Mode!"})
    assert envelope == {"command": "scene.apply", "scene_id": "twilight_mode"}
    assert legacy.map_apply_scene_event({}) == {"command": "scene.apply", "scene_id": "scene"}


def test_apply_scene_event_unknown_name_flows_to_engine_not_found():
    engine = make_engine(_empty_store())
    result = engine.handle(legacy.map_apply_scene_event({"scene_name": "No Such Scene"}))
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def _empty_store(tmp=None):
    import tempfile

    return SceneStudioStore(tempfile.mkdtemp())


def test_save_light_states_event_maps_name():
    envelope = legacy.map_save_light_states_event({"scene_name": "movie night"})
    assert envelope == {"command": "scene.save", "name": "movie night"}
    assert legacy.map_save_light_states_event({}) == {"command": "scene.save", "name": "scene"}


def test_delete_scene_event_maps_to_archive():
    # v1 "delete" archived the scene file; v2 scene.archive is the analog.
    envelope = legacy.map_delete_scene_event({"scene_name": "Old Scene"})
    assert envelope == {"command": "scene.archive", "scene_id": "old_scene"}


def test_legacy_event_mapper_registry_covers_v1_entry_points():
    assert set(legacy.LEGACY_EVENT_MAPPERS) == {
        "apply_scene_event",
        "save_light_states_event",
        "delete_scene_event",
    }


# ---------------------------------------------------------------------------
# generate_office_scene mapping (deterministic)
# ---------------------------------------------------------------------------


def _generate(colors=None, **kwargs):
    payload = {"colors": colors if colors is not None else ["#ff0000", "#0000ff"]}
    return legacy.map_generate_office_scene(payload, **kwargs)


def test_generate_requires_two_valid_colors():
    with pytest.raises(ValueError):
        legacy.map_generate_office_scene({"colors": ["#ff0000"]})
    with pytest.raises(ValueError):
        legacy.map_generate_office_scene({"colors": ["nothex", "alsonot"]})
    # invalid entries are skipped, mirroring v1 per-entry validation
    with pytest.raises(ValueError):
        legacy.map_generate_office_scene({"colors": ["zzzzzz", "#ff0000", "12345"]})


def test_generate_mapping_is_deterministic():
    first = _generate(["#ff0000", "#00ff00", "#0000ff"])
    second = _generate(["#ff0000", "#00ff00", "#0000ff"])
    assert first == second  # v1 used random.choice/shuffle; v2 must not
    assert first["legacy"] == "generate_office_scene"
    assert [step["command"] for step in first["commands"]] == ["scene.save", "scene.apply"]
    assert first["commands"][1] == {"command": "scene.apply", "scene_id": "generated_office"}


def test_generate_scene_shape_gradient_and_cycled_palette():
    mapped = _generate(["#ff0000", "#0000ff"], fixture_ids=["g_strip", "wled_seg", "lamp"])
    scene = mapped["scene"]
    assert scene["id"] == legacy.GENERATED_SCENE_ID
    assert scene["motion"] == {"mode": "static", "speed": 0.0, "strategy": "static"}
    assert scene["target_ids"] == ["office"]
    assert len(scene["palette"]) == 14  # fixed sample count (v1 parity)

    states = scene["fixture_states"]
    # gradient fixture: evenly spaced samples, endpoints included
    gradient = states["g_strip"]["gradient"]
    assert len(gradient) == 5
    assert gradient[0] == "#ff0000"
    assert gradient[-1] == "#0000ff"
    # other fixtures: cycled palette entries at the default brightness
    for fixture_id in ("wled_seg", "lamp"):
        assert states[fixture_id]["brightness"] == 50.0
        assert states[fixture_id]["on"] is True
        assert states[fixture_id]["color"] in scene["palette"]


def test_generate_accepts_legacy_payload_shapes():
    as_string = legacy.map_generate_office_scene({"colors": '["#ff0000", "#0000ff"]'})
    as_dicts = legacy.map_generate_office_scene({"colors": [{"hex": "#ff0000"}, {"color": "#0000ff"}]})
    bare = legacy.map_generate_office_scene({"colors": ["ff0000", "0000ff"]})
    assert as_string["scene"] == as_dicts["scene"] == bare["scene"]


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_apply_scene_event_routes_through_engine(store):
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "twilight_mode",
            "name": "Twilight Mode",
            "target_ids": ["office"],
            "fixture_states": {"lamp": {"on": True, "brightness": 25.0}},
        }
    )
    engine = make_engine(store)
    result = engine.handle(legacy.map_apply_scene_event({"scene_name": "Twilight Mode"}))
    assert result["ok"] is True
    assert result["data"]["scene_id"] == "twilight_mode"
    assert engine.status()["current"]["scene_id"] == "twilight_mode"


def test_save_light_states_event_creates_draft_through_engine(store):
    engine = make_engine(store)
    result = engine.handle(legacy.map_save_light_states_event({"scene_name": "movie night"}))
    assert result["ok"] is True
    # draft targets all declared registry targets (documented default)
    assert result["data"]["scene"]["target_ids"] == ["office"]
    assert store.scenes.get_scene("movie_night").name == "movie night"


def test_delete_scene_event_archives_through_engine(store):
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "old_scene",
            "name": "Old Scene",
            "target_ids": ["office"],
            "fixture_states": {"lamp": {"on": False}},
        }
    )
    engine = make_engine(store)
    result = engine.handle(legacy.map_delete_scene_event({"scene_name": "Old Scene"}))
    assert result["ok"] is True
    assert store.scenes.list_scenes() == []
    assert [scene.id for scene in store.scenes.list_archived()] == ["old_scene"]


def test_generate_sequence_executes_end_to_end(store):
    engine = make_engine(store)
    executor = engine._executor
    mapped = _generate(["#ff0000", "#0000ff"], fixture_ids=["g_strip", "wled_seg", "lamp"])

    results = engine.handle_legacy(mapped)
    assert [result["command"] for result in results] == ["scene.save", "scene.apply"]
    assert all(result["ok"] for result in results)

    persisted = store.scenes.get_scene("generated_office").to_dict()
    assert persisted["fixture_states"]["g_strip"]["gradient"][0] == "#ff0000"
    assert engine.status()["current"]["scene_id"] == "generated_office"
    assert "hue.put_light" in executor.ops()  # static apply ran against providers
    assert "wled.post_state" in executor.ops()


def test_generate_regenerate_updates_existing_scene_states(store):
    engine = make_engine(store)
    first = engine.handle_legacy(_generate(["#ff0000", "#0000ff"], fixture_ids=["g_strip", "wled_seg", "lamp"]))
    assert all(result["ok"] for result in first)
    second = engine.handle_legacy(_generate(["#00ff00", "#0000ff"], fixture_ids=["g_strip", "wled_seg", "lamp"]))
    assert all(result["ok"] for result in second)

    states = store.scenes.get_scene("generated_office").fixture_states
    assert states["g_strip"].gradient[0] == "#00ff00"  # fixture_states replaced
    # documented gap: the frozen catalog/store has no full-scene replace, so a
    # regenerated palette keeps the original palette metadata (engine docstring).
    persisted = store.scenes.get_scene("generated_office").to_dict()
    assert persisted["palette"][0] == "#ff0000"


def test_handle_legacy_with_plain_envelope_mapping(store):
    engine = make_engine(store)
    results = engine.handle_legacy({"legacy": "apply_scene_event", "commands": [
        {"command": "scene.apply", "scene_id": "ghost"}
    ]})
    assert len(results) == 1
    assert results[0]["error"]["code"] == "not_found"


def test_handle_legacy_invalid_scene_becomes_failure_result_not_exception(store):
    # upsert must never raise: a malformed generated scene yields a failure
    # result and the sequence continues to the (failing) apply step.
    engine = make_engine(store)
    mapped = {
        "legacy": "generate_office_scene",
        "scene": {"id": "generated_office"},  # invalid: missing required v2 fields
        "commands": [
            {"command": "scene.save", "name": "Generated Office", "target_id": "office"},
            {"command": "scene.apply", "scene_id": "generated_office"},
        ],
    }
    results = engine.handle_legacy(mapped)
    assert [result["command"] for result in results] == ["scene.save", "scene.apply"]
    assert results[0]["ok"] is False
    assert results[0]["error"]["code"] == "validation_error"
    assert results[1]["ok"] is False
    assert results[1]["error"]["code"] == "not_found"
