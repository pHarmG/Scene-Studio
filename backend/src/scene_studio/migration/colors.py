"""Color conversions for v1 migration (pure, deterministic).

`xy_to_hex` intentionally reuses the algorithm from the v1 AppDaemon tooling
(the legacy AppDaemon scene tools this pass migrated from)
so migrated colors stay visually comparable to the v1 apply/swatch path.
The transform is a Hue-gamut-oriented linear-RGB estimate with per-color
normalization: results are *approximate* sRGB values, never exact, and the
analysis reports them as such.

Divergences from the v1 reference (documented on purpose):

- The v1 `rgb_to_hex` returned ``None`` for pure black; this module always
  returns a total ``#rrggbb`` string so migrated state is never silently
  dropped (a black segment color is real intent, and ``on`` carries power).
- The v1 `xy_to_hex` returned ``None`` for ``y == 0``; here ``y`` is clamped
  to a tiny positive value so invalid CIE input stays deterministic.
"""

from __future__ import annotations

import math
from typing import Sequence

# Hue-ordered linear RGB matrix, identical to the v1 scene_manager reference.
_XY_MATRIX = (
    (1.656492, -0.354851, -0.255038),
    (-0.707196, 1.655397, 0.036152),
    (0.051713, -0.121364, 1.01153),
)


def _clamp_byte(value: float) -> int:
    return max(0, min(255, int(round(value))))


def rgb_to_hex(rgb: Sequence[float]) -> str:
    """Convert a 0-255 RGB triple to canonical ``#rrggbb`` (total function)."""
    if rgb is None or len(tuple(rgb)) < 3:
        raise ValueError("rgb triple with at least 3 channels required")
    r, g, b = (_clamp_byte(float(channel)) for channel in tuple(rgb)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def wled_col_to_hex(col: Sequence[Sequence[float]] | None) -> str | None:
    """Primary color of a WLED ``seg.col`` array (``col[0]``, 0-255 RGB).

    Returns ``None`` when no primary color exists (missing/empty ``col``).
    Black is a valid color and is returned as ``#000000``.
    """
    if not col:
        return None
    primary = tuple(col)[0]
    if primary is None or len(tuple(primary)) < 3:
        return None
    return rgb_to_hex(primary)


def _gamma_correct(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return (1.0 + 0.055) * math.pow(channel, 1.0 / 2.4) - 0.055


def xy_to_hex(x: float, y: float, brightness: float | None = None) -> str:
    """Convert CIE xy (Hue convention) to ``#rrggbb``.

    ``brightness`` is an optional 0..1 scalar exactly as the v1 tooling passed
    it (``dimming.brightness / 100``). Migration converts stored scene colors
    without folding brightness in (the domain keeps ``color`` and
    ``brightness`` separate), so callers that want the v1 swatch look can pass
    the scalar explicitly.
    """
    x = float(x)
    y = float(y)
    # Total-function divergence: v1 returned None for y == 0.
    if y == 0:
        y = 1e-6
    z = 1.0 - x - y
    level = 1.0 if brightness is None else max(0.15, min(1.0, float(brightness)))
    big_x = (level / y) * x
    big_y = level
    big_z = (level / y) * z

    r = big_x * _XY_MATRIX[0][0] + big_y * _XY_MATRIX[0][1] + big_z * _XY_MATRIX[0][2]
    g = big_x * _XY_MATRIX[1][0] + big_y * _XY_MATRIX[1][1] + big_z * _XY_MATRIX[1][2]
    b = big_x * _XY_MATRIX[2][0] + big_y * _XY_MATRIX[2][1] + big_z * _XY_MATRIX[2][2]

    r = max(0.0, r)
    g = max(0.0, g)
    b = max(0.0, b)

    r = _gamma_correct(r)
    g = _gamma_correct(g)
    b = _gamma_correct(b)

    peak = max(r, g, b, 1.0)
    return rgb_to_hex((r / peak * 255.0, g / peak * 255.0, b / peak * 255.0))
