"""Palette pin + auto-spread resolution (static color pointers)."""

import pytest

from scene_studio.domain.fidelity import FidelityLevel
from scene_studio.domain.fixtures import FixtureRegistry
from scene_studio.domain.palette_resolve import (
    canonicalize_static_palette,
    compute_spread_indices,
    resolve_fixture_state,
    unique_fixture_colors,
)
from scene_studio.domain.scenes import Scene
from scene_studio.domain.serde import ValidationError
from scene_studio.renderers import build_render_plan, hex_to_xy


def _static_palette_scene(**overrides) -> dict:
    data = {
        "schema_version": 2,
        "id": "palette_static",
        "name": "Palette Static",
        "target_ids": ["studio"],
        "palette": ["#ff0000", "#00ff00", "#0000ff"],
        "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
        "default_state": {"on": True, "brightness": 40.0},
        "fixture_states": {},
    }
    data.update(overrides)
    return data


@pytest.fixture()
def registry(sample_registry_data) -> FixtureRegistry:
    return FixtureRegistry.from_dict(sample_registry_data)


def test_auto_spread_walks_registry_order_within_target(registry):
    scene = Scene.from_dict(_static_palette_scene())
    resolved = {fixture.id: fixture for fixture in registry.fixtures if "studio" in fixture.groups}
    indices = compute_spread_indices(scene, registry, resolved, scene.target_ids)
    # double_strip is disabled; g_strip is first RGB-capable studio member.
    assert indices["g_strip"] == 0
    assert indices["middle_bar"] == 1
    assert indices["lamp"] == 2
    assert indices["lower_bar"] == 0  # wraps
    assert "double_strip" not in indices


def test_pin_clamps_and_skips_spread(registry):
    scene = Scene.from_dict(
        _static_palette_scene(
            fixture_states={"lamp": {"on": True, "brightness": 40.0, "palette_index": 20}}
        )
    )
    resolved = {fixture.id: fixture for fixture in registry.fixtures if "studio" in fixture.groups}
    indices = compute_spread_indices(scene, registry, resolved, scene.target_ids)
    assert "lamp" not in indices
    lamp = resolved["lamp"]
    resolved_state = resolve_fixture_state(
        scene, lamp, scene.fixture_states["lamp"], is_override=True, spread_index=None
    )
    assert resolved_state.color == "#0000ff"  # clamped to last of 3


def test_explicit_hex_wins_over_palette(registry):
    scene = Scene.from_dict(
        _static_palette_scene(
            fixture_states={"lamp": {"on": True, "color": "#abcdef"}}
        )
    )
    plan = build_render_plan(scene, registry)
    lamp = next(fp for fp in plan.fixture_plans if fp.fixture_id == "lamp")
    x, y = hex_to_xy("#abcdef")
    assert lamp.operations[0].payload["color"]["xy"] == {"x": x, "y": y}


def test_gradient_is_not_replaced_by_spread(registry):
    scene = Scene.from_dict(
        _static_palette_scene(
            fixture_states={"g_strip": {"on": True, "gradient": ["#111111", "#222222"]}}
        )
    )
    plan = build_render_plan(scene, registry)
    g_strip = next(fp for fp in plan.fixture_plans if fp.fixture_id == "g_strip")
    points = g_strip.operations[0].payload["gradient"]["points"]
    assert [point["color"]["xy"] for point in points] == [
        dict(zip(("x", "y"), hex_to_xy(color))) for color in ("#111111", "#222222")
    ]


def test_static_apply_uses_spread_not_palette_head(registry):
    scene = Scene.from_dict(_static_palette_scene())
    plan = build_render_plan(scene, registry)
    g_strip = next(fp for fp in plan.fixture_plans if fp.fixture_id == "g_strip")
    middle = next(fp for fp in plan.fixture_plans if fp.fixture_id == "middle_bar")
    x0, y0 = hex_to_xy("#ff0000")
    x1, y1 = hex_to_xy("#00ff00")
    assert g_strip.operations[0].payload["color"]["xy"] == {"x": x0, "y": y0}
    assert middle.operations[0].payload["color"]["xy"] == {"x": x1, "y": y1}


def test_default_state_hex_ignored_when_palette_present(registry):
    scene = Scene.from_dict(
        _static_palette_scene(default_state={"on": True, "brightness": 40.0, "color": "#ffffff"})
    )
    plan = build_render_plan(scene, registry)
    g_strip = next(fp for fp in plan.fixture_plans if fp.fixture_id == "g_strip")
    x0, y0 = hex_to_xy("#ff0000")
    assert g_strip.operations[0].payload["color"]["xy"] == {"x": x0, "y": y0}


def test_empty_palette_ignores_pins(registry):
    scene = Scene.from_dict(
        _static_palette_scene(
            palette=[],
            fixture_states={"lamp": {"on": True, "brightness": 40.0, "palette_index": 1}},
        )
    )
    plan = build_render_plan(scene, registry)
    lamp = next(fp for fp in plan.fixture_plans if fp.fixture_id == "lamp")
    assert "color" not in lamp.operations[0].payload


def test_approximate_dynamic_uses_spread_slot(registry, sample_scenes_data):
    aurora = next(item for item in sample_scenes_data["scenes"] if item["id"] == "aurora_flow")
    scene = Scene.from_dict(aurora)
    plan = build_render_plan(scene, registry)
    # g_strip has a gradient so it is not in the spread. Remaining RGB fixtures
    # walk the 4-color palette; custom_gradient is the 5th spread candidate -> slot 0.
    # office_strip is the 6th -> slot 1 (#2979ff), and is dynamic_native so this
    # asserts native action color; pick a non-native later WLED-less hue:
    custom = next(fp for fp in plan.fixture_plans if fp.fixture_id == "custom_gradient")
    assert custom.fidelity is FidelityLevel.APPROXIMATE
    x0, y0 = hex_to_xy("#00e5ff")
    assert custom.operations[0].payload["color"]["xy"] == {"x": x0, "y": y0}


def test_color_and_palette_index_rejected():
    with pytest.raises(ValidationError, match="palette_index and color"):
        Scene.from_dict(
            _static_palette_scene(fixture_states={"lamp": {"color": "#112233", "palette_index": 0}})
        )


def test_canonicalize_rebuilds_collapsed_palette_and_pins_matching_hexes():
    scene = Scene.from_dict(
        {
            "schema_version": 2,
            "id": "meeting_look",
            "name": "Meeting Look",
            "target_ids": ["studio"],
            "palette": ["#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff"],
            "motion": {"mode": "static", "speed": 0.5, "strategy": "auto"},
            "fixture_states": {
                "g_strip": {
                    "on": True,
                    "color": "#39f3ff",
                    "gradient": ["#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff"],
                },
                "lamp": {"on": True, "color": "#56c1ff"},
                "office_strip": {"on": True, "color": "#7aff8d"},
                "wled_seg_2": {"on": True, "color": "#ffffff"},
                "wled_seg_1": {"on": True, "color": "#99ffdf"},
            },
        }
    )
    canonical = canonicalize_static_palette(scene)
    assert canonical.palette == ["#39f3ff", "#56c1ff", "#7aff8d", "#99ffdf", "#ffffff"]
    assert unique_fixture_colors(canonical) == canonical.palette
    assert canonical.fixture_states["lamp"].palette_index == 1
    assert canonical.fixture_states["lamp"].color is None
    assert canonical.fixture_states["wled_seg_2"].palette_index == 4
    assert canonical.fixture_states["g_strip"].palette_index == 0
    assert canonical.fixture_states["g_strip"].gradient == ["#39f3ff"] * 5
    assert canonical.fixture_states["g_strip"].color is None


def test_canonicalize_does_not_pin_multi_stop_gradient_hex(registry):
    scene = Scene.from_dict(
        _static_palette_scene(
            palette=["#ff0000", "#00ff00", "#0000ff"],
            fixture_states={
                "g_strip": {"on": True, "color": "#ff0000", "gradient": ["#ff0000", "#00ff00", "#0000ff"]},
                "lamp": {"on": True, "color": "#00ff00"},
            },
        )
    )
    canonical = canonicalize_static_palette(scene)
    assert canonical.fixture_states["g_strip"].color == "#ff0000"
    assert canonical.fixture_states["g_strip"].palette_index is None
    assert canonical.fixture_states["lamp"].palette_index == 1
    assert canonical.fixture_states["lamp"].color is None
    plan = build_render_plan(canonical, registry)
    lamp = next(fp for fp in plan.fixture_plans if fp.fixture_id == "lamp")
    x, y = hex_to_xy("#00ff00")
    assert lamp.operations[0].payload["color"]["xy"] == {"x": x, "y": y}
