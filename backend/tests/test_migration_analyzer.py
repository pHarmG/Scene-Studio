"""Analyzer tests against the real (sanitized, tracked) 2026-09-10 v1 pull.

Covers: the crosswalk as scope authority (the v1 apply path filtered saved Hue
states to crosswalk room "Studio", so only crosswalk-listed resources map),
out-of-scope provenance labeling (registry_known_out_of_scope vs not_in_use),
crosswalk-known resolution to registry fixtures, unresolved/ambiguous
resources (never guessed), fidelity concerns, palette inference labeling,
deterministic target selection, analysis determinism, and the backup plan
(pure data).
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scene_studio import FixtureRegistry
from scene_studio.migration import (
    analyze_scene,
    plan_backup,
    scene_id_from_filename,
    scene_name_from_filename,
    select_targets,
)

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

# The effective old apply scope: the 8 crosswalk hue fixtures (including the
# disabled double_strip, which apply skips today) plus the 6 crosswalk WLED
# segments = 14 mapped fixtures per scene.
EXPECTED_MAPPED = [
    "custom_gradient",
    "double_strip",
    "g_strip",
    "lamp",
    "lower_bar",
    "middle_bar",
    "office_strip",
    "upper_bar",
    "wled_seg_0",
    "wled_seg_1",
    "wled_seg_2",
    "wled_seg_3",
    "wled_seg_4",
    "wled_seg_5",
]

# Captured lights OUTSIDE the v1 apply scope (absent from the crosswalk).
# The registry knows an enabled fixture for each of these 8 — provenance
# labels them registry_known_out_of_scope and carries the fixture id.
OUT_OF_SCOPE_REGISTRY_KNOWN = {
    "026b71a8-f85a-455f-b317-90baf5f20e10": "bathroom_mirror_left",
    "50f25ba9-1b46-47b3-af8d-dda9b7afdc55": "living_room_lamp",
    "5602549d-ffa2-46dd-b453-22b7e4ac1529": "bathroom_mirror_right",
    "5fc3af70-d9c7-4cf8-ac18-6d08fecd7410": "desk_lamp",
    "5fd91f87-0a0f-45e3-a050-0f9f2f1d6012": "bathroom_main",
    "5fdc04d4-baa2-4f27-a28c-a48dac323144": "reading_lamp",
    "8490611a-5c77-40cc-b836-429667be6cc6": "bathroom_mirror_mid",
    "9fc58253-0598-4dd4-98b1-b07febfacb89": "bedroom_closet",
}

# Water/Food Light are bound to disabled registry fixtures ("confirmed not in
# use"): provenance labels them not_in_use with no fixture id.
OUT_OF_SCOPE_NOT_IN_USE = {
    "ae1c4b40-9844-445f-ae52-bb7e30c1a16f": None,
    "d7804c30-d7e7-4271-8964-cd96e3f4eaf4": None,
}

# Captured display names for the out-of-scope lights; the bedroom resource's
# captured name varies across the v1 pull (Bedroom Closet / Bedroom Light).
EXPECTED_OUT_OF_SCOPE_NAME_VARIANTS = {
    "Bathroom Mirror Left",
    "Bathroom Mirror Right",
    "Bathroom Mirror Mid",
    "Bathroom Main",
    "Living Room Lamp",
    "Desk Lamp",
    "Reading Lamp",
    "Bedroom Closet",
    "Bedroom Light",
    "Water Light",
    "Food Light",
}

EXPECTED_OUT_OF_SCOPE_IDS = set(OUT_OF_SCOPE_REGISTRY_KNOWN) | set(OUT_OF_SCOPE_NOT_IN_USE)


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


# ---------------------------------------------------------------------------
# verified id-space fact
# ---------------------------------------------------------------------------


def test_v1_scene_files_present(v1_scenes):
    assert sorted(v1_scenes) == EXPECTED_SCENES


def test_scene_keys_share_crosswalk_registry_id_space(v1_scenes, crosswalk, registry):
    """The empirically verified fact: scene keys == crosswalk resource ids == registry bindings.

    The registry also binds the crosswalk-absent bridge lights (real fixtures
    the v1 apply path never wrote), so the crosswalk is a strict subset of the
    registry's bound resource ids.
    """
    cw_resources = {entry["hue_resource_id"] for entry in crosswalk["hue"].values()}
    registry_resources = {
        fixture.binding.resource_id
        for fixture in registry.fixtures
        if fixture.binding is not None and fixture.binding.provider == "hue_v2"
    }
    assert cw_resources <= registry_resources
    assert len(registry_resources - cw_resources) == 10  # out-of-scope captured lights
    for name, scene in v1_scenes.items():
        keys = set((scene.get("hue") or {}).get("lights") or {})
        assert registry_resources <= keys, f"{name}: registry resources must all appear as scene keys"


# ---------------------------------------------------------------------------
# per-scene analysis over all 7 scenes
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def analyses(v1_scenes, registry, crosswalk) -> dict[str, dict]:
    return {
        name: analyze_scene(scene, registry, crosswalk, filename=name).to_dict()
        for name, scene in v1_scenes.items()
    }


def test_all_seven_scenes_analyze_with_expected_shape(analyses):
    assert sorted(analyses) == EXPECTED_SCENES
    for name, report in analyses.items():
        assert report["scene_file"] == name
        assert len(report["hue_resources"]) == 18
        mapped = [m for m in report["hue_resources"] if m["status"] == "mapped"]
        discarded = [m for m in report["hue_resources"] if m["status"] == "discarded"]
        unresolved = [m for m in report["hue_resources"] if m["status"] == "unresolved"]
        # Effective old apply scope: 8 crosswalk hue + 6 wled = 14 mapped
        # fixtures; the 10 other captured bridge lights are out of scope.
        assert len(mapped) == 8
        assert len(discarded) == 10
        assert unresolved == []
        assert [s["segment_index"] for s in report["wled_segments"]] == [0, 1, 2, 3, 4, 5]
        assert all(s["status"] == "mapped" for s in report["wled_segments"])


def test_mapped_fixture_set_is_stable_across_scenes(analyses):
    for name, report in analyses.items():
        assert report["mapped_fixture_ids"] == EXPECTED_MAPPED, name


def test_out_of_scope_lights_are_labeled_not_mapped(analyses):
    """Captured lights absent from the crosswalk never map — they are provenance.

    The 8 registry-known lights carry their seeded fixture ids and status
    ``registry_known_out_of_scope``; Water/Food Light carry no fixture id and
    status ``not_in_use`` (registry fixture disabled).
    """
    for name, report in analyses.items():
        by_resource = {m["resource_id"]: m for m in report["hue_resources"]}
        for resource_id, fixture_id in OUT_OF_SCOPE_REGISTRY_KNOWN.items():
            entry = by_resource[resource_id]
            assert entry["status"] == "discarded", (name, resource_id)
            assert entry["scope_status"] == "registry_known_out_of_scope", (name, resource_id)
            assert entry["fixture_id"] == fixture_id, (name, resource_id)
            assert entry["reason"] == "known to the registry but outside the v1 crosswalk apply scope", (name, resource_id)
        for resource_id in OUT_OF_SCOPE_NOT_IN_USE:
            entry = by_resource[resource_id]
            assert entry["status"] == "discarded", (name, resource_id)
            assert entry["scope_status"] == "not_in_use", (name, resource_id)
            assert "fixture_id" not in entry, (name, resource_id)
            assert entry["reason"] == "confirmed not in use; registry fixture disabled", (name, resource_id)


def test_exactly_ten_out_of_scope_lights_are_discarded_per_scene(analyses):
    """Discarded = captured outside the v1 crosswalk apply scope: Water Light,
    Food Light, plus the 8 registry-known out-of-scope lights."""
    for name, report in analyses.items():
        discarded = report["discarded_lights"]
        assert len(discarded) == 10, name
        assert {m["resource_id"] for m in discarded} == EXPECTED_OUT_OF_SCOPE_IDS, name
        names = {m["name"] for m in discarded}
        assert {"Water Light", "Food Light"} <= names, name
        assert names <= EXPECTED_OUT_OF_SCOPE_NAME_VARIANTS, name


def test_only_double_strip_is_disabled_but_still_mapped(analyses):
    """double_strip is in the old apply scope (crosswalk): its state migrates
    and the fidelity concerns flag that apply will skip it (disabled). The
    not-in-use lights are out of scope entirely and are not flagged here."""
    for name, report in analyses.items():
        assert "double_strip" in set(report["mapped_fixture_ids"]), name
        assert "water_light" not in set(report["mapped_fixture_ids"]), name
        assert "food_light" not in set(report["mapped_fixture_ids"]), name
        codes = {c["code"]: c for c in report["fidelity_concerns"]}
        assert codes["fixture_disabled"]["fixture_ids"] == ["double_strip"], name
        assert codes["fixture_disabled"]["level"] == "warning"


def test_analysis_determinism(v1_scenes, registry, crosswalk):
    scene = v1_scenes["twilight.json"]
    first = analyze_scene(scene, registry, crosswalk, filename="twilight.json").to_dict()
    second = analyze_scene(scene, registry, crosswalk, filename="twilight.json").to_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# unknown / ambiguous resources are reported, never guessed
# ---------------------------------------------------------------------------


def test_crosswalk_known_but_unregistered_resource_is_unresolved(v1_scenes, registry, crosswalk):
    """Crosswalk maps the resource, but no registry fixture claims it -> unresolved.

    Adding a crosswalk entry for a resource that no registry fixture binds
    (and the v1 saver captured anyway) promotes it to "known to the migration";
    because no registry fixture binds that entity or resource id, it must be
    reported unresolved, never guessed.
    """
    ghost_resource = "aaaabbbb-0000-4000-8000-00000000aaaa"
    extended = copy.deepcopy(crosswalk)
    extended["hue"]["light.unregistered_thing"] = {
        "hue_friendly_name": "Attic Bulb",
        "hue_resource_id": ghost_resource,
        "hue_type": "single",
        "room": "Attic",
    }
    scene = copy.deepcopy(v1_scenes["twilight.json"])
    scene["hue"]["lights"][ghost_resource] = {"metadata": {"name": "Attic Bulb"}}
    report = analyze_scene(scene, registry, extended, filename="twilight.json").to_dict()
    unresolved = {m["resource_id"]: m for m in report["unresolved_resources"]}
    entry = unresolved[ghost_resource]
    assert "no seeded registry fixture binds" in entry["reason"]
    assert "light.unregistered_thing" in entry["reason"]
    assert entry["name"] == "Attic Bulb"


def test_ambiguous_resource_is_reported_not_guessed(v1_scenes, registry, crosswalk):
    """Two registry fixtures claiming the same resource -> unresolved with reason."""
    registry_data = _load(REGISTRY_PATH)
    registry_data["fixtures"].append(
        {
            "id": "ghost_strip",
            "name": "Ghost Strip",
            "location": "studio",
            "groups": ["studio"],
            "enabled": True,
            "binding": {
                "provider": "hue_v2",
                "bridge_id": "001788demo000001",
                "resource_id": "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a",
                "ha_entity_id": "light.hue_g_strip_ghost",
            },
        }
    )
    ambiguous_registry = FixtureRegistry.from_dict(registry_data)
    report = analyze_scene(v1_scenes["twilight.json"], ambiguous_registry, crosswalk, filename="twilight.json").to_dict()
    unresolved = {m["resource_id"]: m for m in report["unresolved_resources"]}
    entry = unresolved["2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"]
    assert "ambiguous" in entry["reason"]
    assert "g_strip" in entry["reason"] and "ghost_strip" in entry["reason"]
    assert "g_strip" not in report["mapped_fixture_ids"]


def test_scene_key_absent_everywhere_is_discarded_without_scope_status(v1_scenes, registry, crosswalk):
    """A light neither crosswalk nor registry knows is out of scope with no
    structured provenance status (nothing to label it with)."""
    scene = copy.deepcopy(v1_scenes["twilight.json"])
    scene["hue"]["lights"]["ffffffff-0000-4000-8000-000000000000"] = {
        "metadata": {"name": "Mystery Bulb"},
    }
    report = analyze_scene(scene, registry, crosswalk, filename="twilight.json").to_dict()
    entry = next(m for m in report["discarded_lights"] if m["name"] == "Mystery Bulb")
    assert "scope_status" not in entry
    assert "v1 crosswalk filter" in entry["reason"]


def test_wled_segments_outside_crosswalk_scope_are_not_mapped(v1_scenes, registry, crosswalk):
    """The crosswalk is scope authority for WLED too: registry-bound segments
    the crosswalk does not list stay unmapped."""
    gated = copy.deepcopy(crosswalk)
    gated["wled"] = {
        key: entry
        for key, entry in gated["wled"].items()
        if key not in ("light.lg_wled_segment_4", "light.lg_wled_segment_5")
    }
    report = analyze_scene(v1_scenes["twilight.json"], registry, gated, filename="twilight.json").to_dict()
    by_index = {s["segment_index"]: s for s in report["wled_segments"]}
    assert by_index[0]["status"] == "mapped"
    assert by_index[4]["status"] == "unmapped"
    assert "outside the v1 crosswalk apply scope" in by_index[4]["reason"]
    assert "wled_seg_4" not in report["mapped_fixture_ids"]


# ---------------------------------------------------------------------------
# fidelity concerns
# ---------------------------------------------------------------------------


def test_disabled_and_degraded_fidelity_concerns(analyses):
    for name, report in analyses.items():
        codes = {c["code"]: c for c in report["fidelity_concerns"]}
        assert "double_strip" in codes["fixture_disabled"]["fixture_ids"], name
        assert "custom_gradient" in codes["fixture_capability_limited"]["fixture_ids"], name
        assert "xy_color_conversion_approximate" in codes
        assert codes["xy_color_conversion_approximate"]["level"] == "info"


def test_gradient_capability_mismatch_is_flagged(v1_scenes, registry, crosswalk):
    """A gradient payload on a fixture without gradient capability is a warning."""
    scene = copy.deepcopy(v1_scenes["twilight.json"])
    lamp_key = "73acf87a-8835-435d-b422-e889c5017dac"  # registry: lamp, no gradient capability
    scene["hue"]["lights"][lamp_key]["gradient"] = {
        "points": [
            {"color": {"xy": {"x": 0.1967, "y": 0.1534}}},
            {"color": {"xy": {"x": 0.3574, "y": 0.3901}}},
        ]
    }
    report = analyze_scene(scene, registry, crosswalk, filename="twilight.json").to_dict()
    codes = {c["code"]: c for c in report["fidelity_concerns"]}
    assert "lamp" in codes["capability_mismatch_gradient"]["fixture_ids"]
    assert codes["capability_mismatch_gradient"]["level"] == "warning"


def test_gradient_overflow_is_flagged(v1_scenes, registry, crosswalk):
    """g_strip caps at 5 points; a 6-point gradient must be flagged."""
    scene = copy.deepcopy(v1_scenes["twilight.json"])
    g_strip_key = "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
    scene["hue"]["lights"][g_strip_key]["gradient"]["points"] = [
        {"color": {"xy": {"x": 0.2, "y": 0.2}}} for _ in range(6)
    ]
    report = analyze_scene(scene, registry, crosswalk, filename="twilight.json").to_dict()
    codes = {c["code"]: c for c in report["fidelity_concerns"]}
    assert "g_strip" in codes["gradient_points_exceed_capability"]["fixture_ids"]


# ---------------------------------------------------------------------------
# palette + motion inference
# ---------------------------------------------------------------------------


def test_palette_inferred_from_gradient_order_labeled_approximate(analyses):
    report = analyses["twilight.json"]
    palette = report["palette_inference"]
    assert palette["source"] == "gradient:g_strip"
    assert palette["approximate"] is True
    assert palette["colors"] == ["#7482ff", "#fbffc6", "#ffdb7f"]
    assert "approximate" in palette["note"] or "not palettes" in palette["note"]
    # code_focus carries 5 gradient points on the same strip.
    assert len(analyses["code_focus.json"]["palette_inference"]["colors"]) == 5


def test_uniform_gradient_falls_through_to_fixture_colors(analyses):
    """Meeting Blue's G Strip gradient is one cyan repeated; that is not the look."""
    palette = analyses["meeting_blue.json"]["palette_inference"]
    assert palette["source"] == "fixture_colors"
    assert palette["approximate"] is True
    assert len(set(palette["colors"])) > 1
    assert palette["colors"] != ["#39f3ff"] * 5
    assert "#56c1ff" in palette["colors"]  # lamp / bars — light blue
    assert "#ffffff" in palette["colors"]  # WLED whites
    assert "#39f3ff" in palette["colors"]  # G Strip cyan still present, once
    # Unique fixture colors in fixture-id order, not WLED-only.
    assert palette["colors"].index("#56c1ff") < palette["colors"].index("#ffffff")


def test_motion_inference_is_static_auto(analyses):
    for report in analyses.values():
        assert report["motion_inference"]["mode"] == "static"
        assert report["motion_inference"]["strategy"] == "auto"


# ---------------------------------------------------------------------------
# target selection
# ---------------------------------------------------------------------------

# Smallest exact semantic cover of the 13 mapped enabled fixtures (12 enabled
# studio members + office_strip, now itself a studio member): studio is
# exact once the disabled double_strip is excluded from over-coverage
# accounting. whole_house would over-cover the rest of the house.
EXPECTED_TARGETS = ["studio"]


def test_target_selection_exact_semantic_cover_never_whole_house(analyses):
    for name, report in analyses.items():
        selection = report["target_selection"]
        assert selection["target_ids"] == EXPECTED_TARGETS, name
        assert "whole_house" not in selection["target_ids"], name  # a covering target must not win
        assert selection["overcovered_fixture_ids"] == [], name  # exact cover: chosen targets over-cover nothing
        assert selection["uncovered_fixture_ids"] == [], name
        assert selection["rule"]


def _mini_registry(targets, fixtures):
    return FixtureRegistry.from_dict(
        {
            "schema_version": 1,
            "targets": [{"id": t, "name": t} for t in targets],
            "fixtures": [
                {"id": fixture_id, "name": fixture_id, "groups": groups} for fixture_id, groups in fixtures
            ],
        }
    )


def test_target_selection_rules():
    # Exact single cover wins outright.
    registry = _mini_registry(["room"], [("f1", ["room"]), ("f2", ["room"])])
    assert select_targets(["f1", "f2"], registry).target_ids == ["room"]
    # No single cover -> greedy picks the largest new coverage, then the rest.
    registry = _mini_registry(["t_a", "t_b"], [("f1", ["t_a"]), ("f2", ["t_a", "t_b"]), ("f3", ["t_b"])])
    assert select_targets(["f1", "f2", "f3"], registry).target_ids == ["t_a", "t_b"]
    # Fixture in no target falls back to its own id (declared targets first, then fixture ids).
    registry = _mini_registry(["room"], [("f1", ["room"]), ("lonely", [])])
    selection = select_targets(["f1", "lonely"], registry)
    assert selection.target_ids == ["room", "lonely"]
    assert selection.uncovered_fixture_ids == ["lonely"]
    # Empty mapping -> no targets (converter refuses to emit such a scene).
    assert select_targets([], registry).target_ids == []


def test_target_selection_prefers_exact_targets_over_a_covering_target():
    # whole_house-shaped case: "big" covers every mapped fixture but reaches
    # beyond the scene ("spare"); the exact pair t_a + t_b wins even though it
    # costs one more target — over-coverage minimization ranks above target
    # count, so the covering target is never chosen.
    registry = _mini_registry(
        ["big", "t_a", "t_b"],
        [
            ("f1", ["big", "t_a"]),
            ("f2", ["big", "t_a"]),
            ("f3", ["big", "t_b"]),
            ("spare", ["big"]),
        ],
    )
    selection = select_targets(["f1", "f2", "f3"], registry)
    assert selection.target_ids == ["t_a", "t_b"]
    assert selection.overcovered_fixture_ids == []
    assert selection.uncovered_fixture_ids == []


def test_target_selection_prefers_fixture_id_over_overcovering_target():
    # Miniature live case: studio exactly covers g_strip; the only declared
    # target reaching office_strip ("office") would over-cover office_lights,
    # so office_strip is emitted as its own fixture id instead.
    registry = _mini_registry(
        ["studio", "office"],
        [
            ("g_strip", ["studio"]),
            ("office_strip", ["office"]),
            ("office_lights", ["office"]),
        ],
    )
    selection = select_targets(["g_strip", "office_strip"], registry)
    assert selection.target_ids == ["studio", "office_strip"]
    assert selection.overcovered_fixture_ids == []
    assert selection.uncovered_fixture_ids == ["office_strip"]


def test_target_selection_ignores_disabled_members_in_overcover_accounting():
    # Live-pull shape: the exact target's only extra member is disabled
    # (double_strip), and disabled fixtures stay excluded from coverage and
    # over-coverage accounting, so the target remains eligible while its own
    # disabled mapped fixture is neither targeted nor reported over-covered.
    registry = FixtureRegistry.from_dict(
        {
            "schema_version": 1,
            "targets": [{"id": "room", "name": "room"}],
            "fixtures": [
                {"id": "f1", "name": "f1", "groups": ["room"]},
                {"id": "f2", "name": "f2", "groups": ["room"]},
                {"id": "off", "name": "off", "groups": ["room"], "enabled": False},
                {"id": "solo", "name": "solo", "groups": []},
            ],
        }
    )
    selection = select_targets(["f1", "f2", "off", "solo"], registry)
    assert selection.target_ids == ["room", "solo"]
    assert selection.overcovered_fixture_ids == []  # disabled "off" is not over-coverage
    assert selection.uncovered_fixture_ids == ["solo"]


# ---------------------------------------------------------------------------
# filename helpers
# ---------------------------------------------------------------------------


def test_scene_identity_helpers():
    assert scene_id_from_filename("twilight.json") == "twilight"
    assert scene_id_from_filename("pink_and_orange_dream.json") == "pink_and_orange_dream"
    assert scene_name_from_filename("twilight.json") == "Twilight"
    assert scene_name_from_filename("pink_and_orange_dream.json") == "Pink And Orange Dream"


# ---------------------------------------------------------------------------
# backup plan (pure data, read-only)
# ---------------------------------------------------------------------------


def test_backup_plan_is_pure_data_and_deterministic():
    plan = plan_backup(V1_DIR)
    data = plan.to_dict()
    assert len(data["files"]) == 7
    assert [item["filename"] for item in data["files"]] == EXPECTED_SCENES
    for item in data["files"]:
        raw = (V1_DIR / item["filename"]).read_bytes()
        assert item["bytes"] == len(raw)
        assert item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert plan_backup(V1_DIR).to_dict() == data
    joined = "\n".join(data["procedure"])
    assert "read-only" in joined
    assert "sha256" in joined
    # Destination is data, not an executed write.
    assert data["destination_root"]
