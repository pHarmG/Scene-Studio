"""Converter tests: v1 scene dict -> validated domain Scene v2 (all 7 scenes).

Converted scenes carry only the effective old apply scope (crosswalk-listed
resources: 8 hue + 6 wled = 14 fixtures); captured lights outside the
crosswalk are preserved as `migrated_from_v1.out_of_scope_lights` provenance.
"""

import copy
import json
from pathlib import Path

import pytest

from scene_studio import FixtureRegistry, Scene
from scene_studio.migration import convert_scene

from test_migration_analyzer import OUT_OF_SCOPE_NOT_IN_USE, OUT_OF_SCOPE_REGISTRY_KNOWN

REPO = Path(__file__).resolve().parents[2]
V1_DIR = REPO / "backend" / "fixtures" / "v1_migration" / "custom_scenes"
CROSSWALK_PATH = REPO / "backend" / "fixtures" / "v1_migration" / "crosswalk.json"
REGISTRY_PATH = REPO / "backend" / "fixtures" / "registry.sample.json"

EXPECTED_SCENES = [
    "banana_strawberry.json",
    "code_focus.json",
    "meeting_blue.json",
    "pink_and_orange_dream.json",
    "tropical_smoothie.json",
    "twilight.json",
    "winter_beauty.json",
]

G_STRIP_KEY = "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def registry() -> FixtureRegistry:
    return FixtureRegistry.from_dict(_load(REGISTRY_PATH))


@pytest.fixture(scope="module")
def crosswalk() -> dict:
    return _load(CROSSWALK_PATH)


@pytest.fixture(scope="module")
def v1_scenes() -> dict[str, dict]:
    return {path.name: _load(path) for path in sorted(V1_DIR.glob("*.json"))}


@pytest.fixture(scope="module")
def converted(v1_scenes, registry, crosswalk) -> dict[str, Scene]:
    return {
        name: convert_scene(scene, registry, crosswalk, filename=name) for name, scene in v1_scenes.items()
    }


# ---------------------------------------------------------------------------
# all 7 scenes convert and validate
# ---------------------------------------------------------------------------


def test_all_seven_scenes_convert(converted):
    assert sorted(converted) == EXPECTED_SCENES


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_scene_round_trip_is_lossless(converted, filename):
    scene = converted[filename]
    assert Scene.from_dict(scene.to_dict()) == scene


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_converted_scene_ids_and_names(converted, filename):
    scene = converted[filename]
    assert scene.id == filename[: -len(".json")]
    assert scene.name == filename[: -len(".json")].replace("_", " ").title()
    assert scene.schema_version == 2


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_converted_fixture_ids_exist_in_registry(converted, registry, filename):
    scene = converted[filename]
    registry_ids = {fixture.id for fixture in registry.fixtures}
    target_ids = {target.id for target in registry.targets}
    assert set(scene.fixture_states) <= registry_ids
    # Declared targets plus fixture-id fallback for mapped fixtures no
    # exact declared target covers.
    assert set(scene.target_ids) <= target_ids | registry_ids
    assert scene.target_ids  # at least one target


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_converted_targets_are_the_exact_semantic_cover(converted, filename):
    # All seven scenes share the same mapped scope, so they share one target
    # set: studio exactly covers the 13 enabled studio members (disabled
    # double_strip excluded from the accounting), including office_strip.
    assert converted[filename].target_ids == ["studio"]


# 14 fixtures per scene: the effective old apply scope (8 crosswalk hue,
# including the disabled double_strip whose state is preserved but which apply
# will skip) plus the 6 crosswalk WLED segments. The 10 other captured bridge
# lights never enter fixture_states.
EXPECTED_FIXTURE_STATES = {
    "g_strip",
    "middle_bar",
    "lamp",
    "lower_bar",
    "double_strip",
    "upper_bar",
    "custom_gradient",
    "office_strip",
    "wled_seg_0",
    "wled_seg_1",
    "wled_seg_2",
    "wled_seg_3",
    "wled_seg_4",
    "wled_seg_5",
}


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_converted_scene_maps_only_the_v1_apply_scope(converted, filename):
    scene = converted[filename]
    assert set(scene.fixture_states) == EXPECTED_FIXTURE_STATES
    # Out-of-scope captured lights never leak into fixture states.
    out_of_scope_fixtures = set(OUT_OF_SCOPE_REGISTRY_KNOWN.values()) | set(OUT_OF_SCOPE_NOT_IN_USE)
    assert set(scene.fixture_states) & out_of_scope_fixtures == set()


def test_disabled_double_strip_state_is_preserved_not_in_use_are_provenance_only(converted):
    """double_strip is in the old apply scope: its state migrates and apply
    skips it today (disabled) — correct and transparent. Water/Food Light are
    out of scope entirely: no fixture state, provenance metadata only."""
    twilight = converted["twilight.json"]
    assert twilight.fixture_states["double_strip"].to_dict() == {
        "on": True,
        "brightness": 50.2,
        "color": "#f4a5ff",
    }
    assert "water_light" not in twilight.fixture_states
    assert "food_light" not in twilight.fixture_states
    by_resource = {
        item["resource_id"]: item
        for item in twilight.metadata["migrated_from_v1"]["out_of_scope_lights"]
    }
    assert by_resource["ae1c4b40-9844-445f-ae52-bb7e30c1a16f"]["status"] == "not_in_use"
    assert by_resource["d7804c30-d7e7-4271-8964-cd96e3f4eaf4"]["fixture_id"] is None


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_motion_is_static_auto(converted, filename):
    motion = converted[filename].motion
    assert motion.mode.value == "static"
    assert motion.strategy.value == "auto"


@pytest.mark.parametrize("filename", EXPECTED_SCENES)
def test_provenance_metadata(converted, filename):
    """Provenance carries the filename plus identification-only records for
    every out-of-scope captured light (no color/brightness payloads)."""
    migrated = converted[filename].metadata["migrated_from_v1"]
    assert migrated["filename"] == filename
    out_of_scope = migrated["out_of_scope_lights"]
    assert len(out_of_scope) == 10
    assert [item["resource_id"] for item in out_of_scope] == sorted(
        list(OUT_OF_SCOPE_REGISTRY_KNOWN) + list(OUT_OF_SCOPE_NOT_IN_USE)
    )
    for item in out_of_scope:
        expected = {
            "resource_id": item["resource_id"],
            "name": item["name"],
            "fixture_id": OUT_OF_SCOPE_REGISTRY_KNOWN.get(item["resource_id"]),
            "status": (
                "registry_known_out_of_scope"
                if item["resource_id"] in OUT_OF_SCOPE_REGISTRY_KNOWN
                else "not_in_use"
            ),
        }
        assert item == expected


# ---------------------------------------------------------------------------
# content specifics (twilight goldens)
# ---------------------------------------------------------------------------


def test_twilight_hue_state_details(converted):
    states = converted["twilight.json"].fixture_states
    # xy -> hex (no brightness folded in), brightness kept verbatim.
    assert states["g_strip"].to_dict() == {
        "on": True,
        "brightness": 50.2,
        "color": "#7482ff",
        "gradient": ["#7482ff", "#fbffc6", "#ffdb7f"],
    }
    assert states["middle_bar"].brightness == 30.04
    assert states["middle_bar"].color == "#f6a2ff"
    assert states["lamp"].brightness == 50.2
    # Disabled leave-behind fixture still carries its migrated state (apply skips it later).
    assert states["double_strip"].to_dict() == {"on": True, "brightness": 50.2, "color": "#f4a5ff"}


def test_twilight_wled_segment_details(converted):
    states = converted["twilight.json"].fixture_states
    assert states["wled_seg_0"].to_dict() == {"on": True, "brightness": 50.2, "color": "#c5aa55"}
    assert states["wled_seg_1"].color == "#6270ab"  # 98,112,171 exact RGB
    assert states["wled_seg_3"].color == "#b581d5"  # 181,129,213
    # Segment brightness is WLED 0-255 scaled to 0-100 (128 -> 50.2).
    assert states["wled_seg_5"].brightness == 50.2


def test_twilight_targets_and_palette(converted):
    scene = converted["twilight.json"]
    # Smallest exact semantic cover: studio exactly covers the mapped
    # studio members including office_strip; whole_house would over-cover
    # the rest of the house.
    assert scene.target_ids == ["studio"]
    assert scene.palette == ["#7482ff", "#fbffc6", "#ffdb7f"]


def test_meeting_blue_palette_is_the_applied_mix_not_g_strip_cyan(converted):
    scene = converted["meeting_blue.json"]
    assert len(set(scene.palette)) > 1
    assert scene.palette != ["#39f3ff"] * 5
    assert "#56c1ff" in scene.palette
    assert "#ffffff" in scene.palette
    assert "#7aff8d" in scene.palette
    lamp = scene.fixture_states["lamp"]
    assert lamp.color is None
    assert scene.palette[lamp.palette_index] == "#56c1ff"
    white = scene.fixture_states["wled_seg_2"]
    assert white.color is None
    assert scene.palette[white.palette_index] == "#ffffff"
    g_strip = scene.fixture_states["g_strip"]
    assert g_strip.gradient == ["#39f3ff"] * 5
    assert g_strip.color is None
    assert scene.palette[g_strip.palette_index] == "#39f3ff"


def test_twilight_gradient_fixture_without_gradient_points_has_no_gradient(converted):
    states = converted["twilight.json"].fixture_states
    assert states["office_strip"].gradient is None
    assert states["g_strip"].gradient is not None


# ---------------------------------------------------------------------------
# determinism + refusals
# ---------------------------------------------------------------------------


def test_conversion_is_deterministic(v1_scenes, registry, crosswalk):
    for name, scene in v1_scenes.items():
        first = convert_scene(scene, registry, crosswalk, filename=name).to_dict()
        second = convert_scene(scene, registry, crosswalk, filename=name).to_dict()
        assert first == second
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_filename_is_required_for_stable_identity(v1_scenes, registry, crosswalk):
    with pytest.raises(ValueError, match="filename is required"):
        convert_scene(v1_scenes["twilight.json"], registry, crosswalk)


def test_scene_with_no_mappable_fixtures_is_refused(registry, crosswalk):
    orphan = {
        "hue": {
            "lights": {
                "ffff0000-0000-4000-8000-0000000000ff": {
                    "metadata": {"name": "Unrelated Bulb"},
                    "on": {"on": True},
                    "dimming": {"brightness": 10.0},
                }
            }
        },
        "wled": {"state": {"seg": []}},
    }
    with pytest.raises(ValueError, match="maps no fixtures"):
        convert_scene(orphan, registry, crosswalk, filename="orphan.json")


def test_unmapped_resources_do_not_enter_fixture_states(v1_scenes, registry, crosswalk):
    """Only crosswalk-listed resources (the v1 apply scope) produce fixture states."""
    scene = copy.deepcopy(v1_scenes["twilight.json"])
    # Replace every light outside the crosswalk with fresh unknown resource
    # ids; the WLED blob stays.
    from scene_studio.migration import build_crosswalk_index

    crosswalked = set(build_crosswalk_index(crosswalk))
    stripped = {"hue": {"lights": {}}, "wled": scene["wled"]}
    for index, (key, payload) in enumerate(scene["hue"]["lights"].items()):
        if key not in crosswalked:
            stripped["hue"]["lights"][f"bbbb0000-0000-4000-8000-{index:012x}"] = payload
    result = convert_scene(stripped, registry, crosswalk, filename="partial.json")
    # Only the WLED segments remain mapped; out-of-scope Hue lights never
    # leak into fixture states.
    assert set(result.fixture_states) == {f"wled_seg_{i}" for i in range(6)}
    # No declared target exactly covers a WLED-segments-only scope (studio
    # would over-cover the seven enabled studio members the scene does not
    # map), so the smallest exact semantic cover is the six individual
    # fixture ids (fixture ids are valid targets, contracts §1).
    assert result.target_ids == [f"wled_seg_{i}" for i in range(6)]
    # These renamed lights are unknown to the registry as well as the
    # crosswalk, so they carry no structured provenance status (plain
    # discarded, reported in the analysis only).
    assert result.metadata["migrated_from_v1"]["out_of_scope_lights"] == []
