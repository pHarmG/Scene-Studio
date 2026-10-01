"""Renderer interface and provider dispatch — frozen for Wave B.

Interface (master plan §8, workstream B1):

- A renderer carries a ``provider`` string and implements
  ``plan_fixture(fixture, state, *, motion, palette)`` returning a
  ``FixtureRenderPlan`` (fidelity + the operations an executor would run).
- ``select_renderer(fixture)`` dispatches on ``fixture.binding.provider``:
  ``hue_v2`` -> HueRenderer, ``wled`` -> WledRenderer, ``ha_light`` ->
  HaLightRenderer.

Renderers are pure planners: they NEVER open sockets or contact devices.
They only emit the operations a later executor (integration wave 1) would
perform. Sibling renderer modules (``wled.py`` for B2, ``halight.py`` for
B3) are loaded lazily and guarded so a missing/unimplemented sibling never
breaks imports or the shared plan builder — it surfaces as
NotImplementedError, which ``build_render_plan`` reports as an
``unsupported`` fixture plan during parallel development.
"""

from __future__ import annotations

from importlib import import_module

from ..domain.bindings import PROVIDER_HA_LIGHT, PROVIDER_HUE_V2, PROVIDER_WLED
from ..domain.fixtures import Fixture
from ..domain.fidelity import FixtureRenderPlan
from ..domain.scenes import FixtureState, Motion


class Renderer:
    """Base interface for provider renderers (frozen)."""

    provider: str

    def plan_fixture(
        self,
        fixture: Fixture,
        state: FixtureState,
        *,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        """Compile one fixture's scene state into a dry-run plan."""
        raise NotImplementedError


def _load_renderer(module_name: str, class_name: str, provider: str) -> Renderer:
    """Import a sibling renderer module lazily (guarded for parallel dev)."""
    try:
        module = import_module(f".{module_name}", __package__)
    except ImportError:
        raise NotImplementedError(
            f"provider renderer '{provider}' is not implemented yet (missing module '{module_name}.py')"
        ) from None
    renderer_class = getattr(module, class_name)
    return renderer_class()


def select_renderer(fixture: Fixture) -> Renderer:
    """Return the renderer for ``fixture.binding.provider``."""
    if fixture.binding is None:
        raise ValueError(f"fixture '{fixture.id}' has no binding; cannot select a renderer")
    provider = fixture.binding.provider
    if provider == PROVIDER_HUE_V2:
        from .hue import HueRenderer

        return HueRenderer()
    if provider == PROVIDER_WLED:
        return _load_renderer("wled", "WledRenderer", provider)
    if provider == PROVIDER_HA_LIGHT:
        return _load_renderer("halight", "HaLightRenderer", provider)
    raise ValueError(f"unknown provider {provider!r} for fixture '{fixture.id}'")
