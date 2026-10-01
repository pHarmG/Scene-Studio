"""Live fixture color-state sampling — transport-free normalizers plus the
fetch/normalize orchestrator. See ``domain/live_state.py`` for the read
model and ``.cursor/plans/scene_studio_live_fixture_color_state.plan.md``
for the design this package implements.
"""

from __future__ import annotations

from .halight import normalize_ha_live_state
from .hue import normalize_hue_live_state
from .sampler import sample_live_state
from .wled import normalize_wled_live_state

__all__ = [
    "normalize_ha_live_state",
    "normalize_hue_live_state",
    "normalize_wled_live_state",
    "sample_live_state",
]
