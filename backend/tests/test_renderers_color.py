"""Tests for scene_studio.renderers.color (hex -> rgb/xy conversion)."""

import pytest

from scene_studio.renderers.color import hex_to_rgb, hex_to_xy


def test_hex_to_rgb_parses_canonical_colors():
    assert hex_to_rgb("#1a237e") == (26, 35, 126)
    assert hex_to_rgb("#ff8f00") == (255, 143, 0)
    assert hex_to_rgb("#00e5ff") == (0, 229, 255)


def test_hex_to_rgb_accepts_missing_hash_and_uppercase():
    assert hex_to_rgb("1A237E") == (26, 35, 126)
    assert hex_to_rgb("#FFFFFF") == (255, 255, 255)


@pytest.mark.parametrize("bad", ["", "#", "#12345", "#1234567", "12345g", "#12 456", "zzzzzz", "#abc"])
def test_hex_to_rgb_rejects_invalid_shapes(bad):
    with pytest.raises(ValueError, match="rrggbb"):
        hex_to_rgb(bad)


def test_hex_to_xy_white_matches_d65_chromaticity():
    # sRGB white must land on the D65 white point (~0.3127, 0.3290).
    assert hex_to_xy("#ffffff") == (0.3127, 0.329)


def test_hex_to_xy_black_is_zero():
    assert hex_to_xy("#000000") == (0.0, 0.0)


def test_hex_to_xy_red_is_unclipped_srgb_chromaticity():
    # Approximate Hue-gamut mapping: no gamut clipping, so pure sRGB red
    # lands outside the Hue gamut on purpose (documented behavior).
    assert hex_to_xy("#ff0000") == (0.735, 0.265)


def test_hex_to_xy_matches_reference_tooling_values():
    # Same matrix/rounding as rgb_to_xy in the live AppDaemon scene tooling.
    assert hex_to_xy("#1a237e") == (0.1732, 0.0686)
    assert hex_to_xy("#4527a0") == (0.2149, 0.0722)
    assert hex_to_xy("#7b1fa2") == (0.3141, 0.101)
    assert hex_to_xy("#ff8f00") == (0.5996, 0.3875)
    assert hex_to_xy("#00e5ff") == (0.1419, 0.3085)


def test_hex_to_xy_rounds_to_four_decimals():
    x, y = hex_to_xy("#0f1e2d")
    assert x == round(x, 4)
    assert y == round(y, 4)
