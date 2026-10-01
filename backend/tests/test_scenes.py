import pytest

from scene_studio.domain.scenes import (
    Motion,
    MotionMode,
    MotionStrategy,
    FixtureState,
    Scene,
    SCENE_SCHEMA_VERSION,
)
from scene_studio.domain.serde import ValidationError


def _base_scene_data(**overrides) -> dict:
    data = {
        "schema_version": SCENE_SCHEMA_VERSION,
        "id": "twilight",
        "name": "Twilight",
        "target_ids": ["studio"],
        "palette": ["#1a237e", "#7b1fa2", "#ff8f00"],
        "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
        "fixture_states": {
            "g_strip": {"on": True, "brightness": 55.0, "gradient": ["#1a237e", "#7b1fa2"]},
            "lamp": {"on": True, "brightness": 40.0, "color": "#ff8f00"},
            "wled_seg_0": {"on": True, "brightness": 50.0, "color": "#7b1fa2"},
        },
        "metadata": {"migrated_from_v1": {"filename": "twilight.json"}},
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# scene document
# ---------------------------------------------------------------------------

def test_scene_round_trip():
    scene = Scene.from_dict(_base_scene_data())
    assert Scene.from_dict(scene.to_dict()) == scene


def test_scene_schema_version_must_be_2():
    data = _base_scene_data()
    data["schema_version"] = 1
    with pytest.raises(ValidationError, match="schema_version"):
        Scene.from_dict(data)


def test_scene_requires_targets_and_valid_ids():
    data = _base_scene_data()
    data["target_ids"] = []
    with pytest.raises(ValidationError, match="target_ids"):
        Scene.from_dict(data)
    data = _base_scene_data(target_ids=["Studio"])
    with pytest.raises(ValidationError, match="target_id"):
        Scene.from_dict(data)


def test_scene_fixture_states_keyed_by_valid_fixture_ids():
    data = _base_scene_data(fixture_states={"Not A Fixture": {"on": True}})
    with pytest.raises(ValidationError, match="fixture_id"):
        Scene.from_dict(data)


def test_scene_provider_uuids_are_not_identity_but_may_live_in_metadata():
    data = _base_scene_data()
    data["metadata"]["migrated_from_v1"]["hue_resource_map"] = {"g_strip": "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"}
    scene = Scene.from_dict(data)
    assert scene.metadata["migrated_from_v1"]["hue_resource_map"]["g_strip"].startswith("2a2c")


# ---------------------------------------------------------------------------
# motion / speed semantics
# ---------------------------------------------------------------------------

def test_motion_defaults():
    motion = Motion.from_dict({})
    assert motion.mode is MotionMode.STATIC
    assert motion.speed == 0.5
    assert motion.strategy is MotionStrategy.AUTO


def test_motion_speed_is_normalized_0_to_1():
    with pytest.raises(ValidationError, match="speed"):
        Motion.from_dict({"mode": "palette_cycle", "speed": 4.2})
    Motion.from_dict({"mode": "palette_cycle", "speed": 1.0})


def test_motion_rejects_unknown_mode_and_strategy():
    with pytest.raises(ValidationError):
        Motion.from_dict({"mode": "hyperspace"})
    with pytest.raises(ValidationError):
        Motion.from_dict({"strategy": "chaos"})


# ---------------------------------------------------------------------------
# palette and colors
# ---------------------------------------------------------------------------

def test_palette_colors_are_canonical_lowercase_hex():
    scene = Scene.from_dict(_base_scene_data(palette=["#FF8F00", "#1A237E"]))
    assert scene.palette == ["#ff8f00", "#1a237e"]


def test_palette_rejects_bad_colors():
    with pytest.raises(ValidationError, match="palette"):
        Scene.from_dict(_base_scene_data(palette=["orange"]))


def test_palette_rejects_too_many_colors():
    with pytest.raises(ValidationError, match="palette"):
        Scene.from_dict(_base_scene_data(palette=["#112233"] * 25))


# ---------------------------------------------------------------------------
# fixture states
# ---------------------------------------------------------------------------

def test_fixture_state_requires_at_least_one_field():
    with pytest.raises(ValidationError, match="at least one"):
        FixtureState.from_dict({})


def test_fixture_state_rejects_out_of_range_values():
    with pytest.raises(ValidationError, match="brightness"):
        FixtureState.from_dict({"brightness": 140.0})
    with pytest.raises(ValidationError, match="color_temp_mirek"):
        FixtureState.from_dict({"color_temp_mirek": 10})
    with pytest.raises(ValidationError, match="on"):
        FixtureState.from_dict({"on": "yes"})


def test_fixture_state_gradient_validated():
    state = FixtureState.from_dict({"gradient": ["#AABBCC", "#000000"]})
    assert state.gradient == ["#aabbcc", "#000000"]
    with pytest.raises(ValidationError):
        FixtureState.from_dict({"gradient": ["#12345"]})


def test_provider_ext_must_be_namespaced():
    state = FixtureState.from_dict({"provider_ext": {"wled": {"preset_id": 3}}})
    assert state.provider_ext == {"wled": {"preset_id": 3}}
    with pytest.raises(ValidationError, match="namespaced"):
        FixtureState.from_dict({"provider_ext": {"philips": {"x": 1}}})


def test_fixture_state_palette_index_round_trip():
    state = FixtureState.from_dict({"on": True, "palette_index": 2})
    assert state.palette_index == 2
    assert FixtureState.from_dict(state.to_dict()) == state


def test_fixture_state_palette_index_alone_satisfies_nonempty_rule():
    state = FixtureState.from_dict({"palette_index": 0})
    assert state.to_dict() == {"palette_index": 0}


def test_fixture_state_rejects_palette_index_out_of_range():
    with pytest.raises(ValidationError, match="palette_index"):
        FixtureState.from_dict({"palette_index": 24})
    with pytest.raises(ValidationError, match="palette_index"):
        FixtureState.from_dict({"palette_index": -1})


def test_fixture_state_rejects_color_and_palette_index_together():
    with pytest.raises(ValidationError, match="palette_index and color"):
        FixtureState.from_dict({"color": "#112233", "palette_index": 0})


# ---------------------------------------------------------------------------
# rename stability (identity contract §2.6)
# ---------------------------------------------------------------------------

def test_renaming_keeps_identity():
    scene = Scene.from_dict(_base_scene_data())
    renamed = Scene.from_dict({**scene.to_dict(), "name": "Twilight (Evening)"})
    assert renamed.id == scene.id
    assert renamed.name == "Twilight (Evening)"
