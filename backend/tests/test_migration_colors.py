"""Golden-value tests for migration color conversions.

The xy->hex goldens are pinned to the v1 AppDaemon algorithm
(`scene_manager.xy_to_hex`), which this module deliberately mirrors. They are
algorithm-reference values, not perceptual targets (the Hue-gamut matrix maps
the sRGB red primary to `#ff5d30`); the analysis labels all xy-derived colors
`approximate` for exactly this reason.
"""

from pathlib import Path

import pytest

from scene_studio.migration.colors import rgb_to_hex, wled_col_to_hex, xy_to_hex

REPO = Path(__file__).resolve().parents[2]


def test_white_d65_golden():
    assert xy_to_hex(0.3127, 0.3290) == "#f5feff"


def test_srgb_primaries_golden():
    assert xy_to_hex(0.64, 0.33) == "#ff5d30"  # red primary (Hue-gamut matrix, mirrors v1)
    assert xy_to_hex(0.30, 0.60) == "#9cff44"  # green primary
    assert xy_to_hex(0.15, 0.06) == "#3935ff"  # blue primary


def test_twilight_g_strip_xy_golden():
    # Real payload value from twilight.json (Hue G Strip scene color).
    assert xy_to_hex(0.1967, 0.1534) == "#7482ff"


def test_brightness_changes_result_but_is_not_required():
    plain = xy_to_hex(0.3469, 0.2285)
    dimmed = xy_to_hex(0.3469, 0.2285, brightness=0.5)
    assert plain == "#f6a2ff"
    assert dimmed == "#ed9af6"
    assert plain != dimmed


def test_degenerate_y_is_total_and_deterministic():
    # v1 returned None for y == 0; migration must never drop a color silently.
    assert xy_to_hex(0.5, 0.0) == "#ff00e3"
    assert xy_to_hex(0.5, 0.0) == xy_to_hex(0.5, 0.0)


def test_rgb_to_hex_clamps_and_rounds():
    assert rgb_to_hex((300, -5, 12.6)) == "#ff000d"
    assert rgb_to_hex((0, 0, 0)) == "#000000"  # total: v1 returned None for black


def test_wled_col_primary_color():
    assert wled_col_to_hex([[197, 170, 85], [0, 0, 0], [0, 0, 0]]) == "#c5aa55"
    assert wled_col_to_hex([[0, 0, 0]]) == "#000000"
    assert wled_col_to_hex(None) is None
    assert wled_col_to_hex([]) is None
    assert wled_col_to_hex([[1, 2]]) is None


@pytest.mark.parametrize(
    "x,y,expected",
    [
        (0.1967, 0.1534, "#7482ff"),
        (0.3574, 0.3901, "#fbffc6"),  # twilight gradient point 2
        (0.4408, 0.4183, "#ffdb7f"),  # twilight gradient point 3
    ],
)
def test_gradient_point_goldens(x, y, expected):
    assert xy_to_hex(x, y) == expected
