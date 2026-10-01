"""Resolve scene.palette onto per-fixture colors (static pointers + auto-spread).

Renderers stay hex-oriented: this module fills ``FixtureState.color`` from a
palette pin or a deterministic spread *before* ``plan_fixture``. Native Hue
``dynamic_palette`` still receives the full palette list separately.
"""

from __future__ import annotations

from dataclasses import replace

from .bindings import HueBinding, WledBinding
from .fixtures import Fixture, FixtureRegistry, HealthStatus, derive_health
from .scenes import FixtureState, MotionMode, Scene

_HEALTH_SKIP = {
    HealthStatus.DISABLED,
    HealthStatus.MISSING,
    HealthStatus.UNBOUND,
}


def fixture_takes_rgb(fixture: Fixture) -> bool:
    """True when the binding can accept a solid RGB/XY color."""
    binding = fixture.binding
    if isinstance(binding, HueBinding) and binding.resource_type == "grouped_light":
        return False
    if isinstance(binding, WledBinding):
        return True
    capabilities = fixture.capabilities
    return bool(capabilities and capabilities.color_xy)


def fixtures_in_target_order(
    registry: FixtureRegistry,
    requested_targets: list[str],
    resolved: dict[str, Fixture],
) -> list[Fixture]:
    """Walk resolved fixtures in scene target order, then registry order within a target."""
    seen: set[str] = set()
    ordered: list[Fixture] = []
    declared = {target.id for target in registry.targets}
    for target_id in requested_targets:
        if target_id in declared:
            for fixture in registry.fixtures:
                if target_id in fixture.groups and fixture.id in resolved and fixture.id not in seen:
                    seen.add(fixture.id)
                    ordered.append(fixture)
        elif target_id in resolved and target_id not in seen:
            seen.add(target_id)
            ordered.append(resolved[target_id])
    for fixture in registry.fixtures:
        if fixture.id in resolved and fixture.id not in seen:
            seen.add(fixture.id)
            ordered.append(fixture)
    return ordered


def _uses_palette_resolution(scene: Scene) -> bool:
    if not scene.palette:
        return False
    return scene.motion.mode in (MotionMode.STATIC, MotionMode.PALETTE_CYCLE)


def _clamp_index(index: int, size: int) -> int:
    if size <= 0:
        return 0
    if index < 0:
        return 0
    if index >= size:
        return size - 1
    return index


def _is_spread_candidate(fixture: Fixture, state: FixtureState, *, is_override: bool) -> bool:
    if not fixture_takes_rgb(fixture):
        return False
    if state.gradient:
        return False
    if is_override and state.color is not None:
        return False
    if state.palette_index is not None:
        return False
    return True


def compute_spread_indices(
    scene: Scene,
    registry: FixtureRegistry,
    resolved: dict[str, Fixture],
    requested_targets: list[str],
) -> dict[str, int]:
    """Map unpinned RGB fixtures to wrapping palette slots (target/registry order)."""
    if not _uses_palette_resolution(scene):
        return {}
    indices: dict[str, int] = {}
    slot = 0
    size = len(scene.palette)
    for fixture in fixtures_in_target_order(registry, requested_targets, resolved):
        if derive_health(fixture) in _HEALTH_SKIP:
            continue
        is_override = fixture.id in scene.fixture_states
        state = scene.fixture_states.get(fixture.id, scene.default_state)
        if state is None:
            continue
        if not _is_spread_candidate(fixture, state, is_override=is_override):
            continue
        indices[fixture.id] = slot % size
        slot += 1
    return indices


def resolve_fixture_state(
    scene: Scene,
    fixture: Fixture,
    state: FixtureState,
    *,
    is_override: bool,
    spread_index: int | None,
) -> FixtureState:
    """Return a copy of ``state`` with ``color`` filled from pin/spread when applicable.

    Gradient and explicit override hex are left untouched. ``default_state.color``
    is ignored when a palette is active (spread/pointers are the color story).
    Empty palettes ignore pins.
    """
    if not _uses_palette_resolution(scene):
        return state
    if state.gradient:
        return state
    if is_override and state.color is not None:
        return state
    palette = scene.palette
    if state.palette_index is not None:
        return replace(state, color=palette[_clamp_index(state.palette_index, len(palette))])
    if spread_index is not None:
        return replace(state, color=palette[_clamp_index(spread_index, len(palette))])
    if not is_override and state.color is not None:
        # Palette is the static color story; drop the shared default hex.
        return replace(state, color=None)
    return state


def _is_multi_gradient(state: FixtureState) -> bool:
    if not state.gradient:
        return False
    return len({color for color in state.gradient if color}) >= 2


def unique_fixture_colors(scene: Scene) -> list[str]:
    """Distinct applied hexes in fixture-id order (solids, pins, gradient stops)."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(color: str | None) -> None:
        if not color or color in seen:
            return
        seen.add(color)
        ordered.append(color)

    for fixture_id in sorted(scene.fixture_states):
        state = scene.fixture_states[fixture_id]
        if state.gradient:
            distinct = list(dict.fromkeys(color for color in state.gradient if color))
            if len(distinct) >= 2:
                for color in distinct:
                    add(color)
                continue
            add(distinct[0] if distinct else None)
            continue
        if state.color:
            add(state.color)
            continue
        if state.palette_index is not None and scene.palette:
            add(scene.palette[_clamp_index(state.palette_index, len(scene.palette))])
    return ordered


def canonicalize_static_palette(scene: Scene) -> Scene:
    """Make a static/palette-cycle scene's palette the assignment source of truth.

    When the stored palette is non-empty but degenerate (a single hue
    repeated, e.g. a migration placeholder that never got a real palette),
    rebuild it from unique fixture colors. A genuinely EMPTY palette is left
    alone: that is a scene that was authored without any palette concept at
    all (pure per-fixture overrides), and populating one would flip
    ``_uses_palette_resolution`` on for a scene that never opted into it.
    Solid ``color`` overrides that match a palette slot become
    ``palette_index`` pins (the Builder assignment model). Multi-stop gradients
    stay custom. Apply is unchanged: pins resolve to the same hex.
    """
    if scene.motion.mode not in (MotionMode.STATIC, MotionMode.PALETTE_CYCLE):
        return scene
    palette = list(scene.palette)
    if palette and len(set(palette)) < 2:
        rebuilt = unique_fixture_colors(scene)
        if rebuilt:
            palette = rebuilt
    if not palette:
        return scene

    states = dict(scene.fixture_states)
    changed = palette != list(scene.palette)
    for fixture_id, state in scene.fixture_states.items():
        if _is_multi_gradient(state) or not state.color:
            continue
        try:
            index = palette.index(state.color)
        except ValueError:
            continue
        states[fixture_id] = replace(state, color=None, palette_index=index)
        changed = True
    if not changed:
        return scene
    return replace(scene, palette=palette, fixture_states=states)
