"""Provider renderers (Wave B): scene intent -> provider operation plans.

Renderers are pure planners — they never perform network or device I/O.
``base.select_renderer`` dispatches on the fixture binding provider
(``hue_v2`` -> HueRenderer, ``wled`` -> WledRenderer, ``ha_light`` ->
HaLightRenderer). Sibling renderer modules are loaded lazily and guarded so
a missing module during parallel development surfaces as
NotImplementedError at plan-build time instead of an import error.
"""

from .base import Renderer, select_renderer
from .color import hex_to_rgb, hex_to_xy
from .hue import HueRenderer
from .plan import build_render_plan

try:  # WLED renderer (B2); guarded so parallel development cannot break imports.
    from .wled import WledRenderer
except ImportError:  # pragma: no cover - module normally present
    WledRenderer = None

try:  # HA light renderer (B3); guarded so parallel development cannot break imports.
    from .halight import HaLightRenderer
except ImportError:  # pragma: no cover - module normally present
    HaLightRenderer = None

__all__ = [
    "HaLightRenderer",
    "HueRenderer",
    "Renderer",
    "WledRenderer",
    "build_render_plan",
    "hex_to_rgb",
    "hex_to_xy",
    "select_renderer",
]
