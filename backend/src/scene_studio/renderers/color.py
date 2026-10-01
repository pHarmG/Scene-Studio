"""Color helpers shared by provider renderers (stdlib only).

`hex_to_xy` implements the same conversion as the live AppDaemon scene
tooling (the legacy scene tools' ``rgb_to_xy``): sRGB -> linear
-> XYZ via the Hue-documented sRGB-derived matrix -> xy chromaticity,
rounded to 4 decimals.

This is an *approximate* Hue-gamut mapping: it does not clip to a specific
bridge gamut (the bridge applies its own gamut mapping when a point falls
outside), so out-of-gamut colors land on the unclipped sRGB xy value.
"""

from __future__ import annotations

_HEX_DIGITS = frozenset("0123456789abcdef")


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse canonical ``#rrggbb`` (leading ``#`` optional) into an RGB triple.

    Raises ValueError on any other shape.
    """
    text = value.strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6 or any(char not in _HEX_DIGITS for char in text):
        raise ValueError(f"expected #rrggbb hex color, got {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def _srgb_to_linear(channel: float) -> float:
    """Decode one sRGB channel (0..1) to linear light."""
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def hex_to_xy(value: str) -> tuple[float, float]:
    """Convert ``#rrggbb`` to CIE 1931 xy chromaticity, rounded to 4 decimals.

    Uses the sRGB-derived matrix published in the Hue developer docs (same
    as the existing scene tooling). Approximate Hue-gamut mapping: no gamut
    clipping is performed here.
    """
    red, green, blue = (channel / 255.0 for channel in hex_to_rgb(value))
    r = _srgb_to_linear(red)
    g = _srgb_to_linear(green)
    b = _srgb_to_linear(blue)

    x = r * 0.649926 + g * 0.103455 + b * 0.197109
    y = r * 0.234327 + g * 0.743075 + b * 0.022598
    z = g * 0.053077 + b * 1.035763

    total = x + y + z
    if total == 0:
        return 0.0, 0.0
    return round(x / total, 4), round(y / total, 4)
