"""Shared scene -> RenderPlan builder (architecture contracts §3).

Pure and deterministic: no device contact anywhere, fixture plans ordered
by fixture id, stable note ordering. Resolution rules (§3):

1. Resolve targets: the ``target_ids`` override when given, else the
   scene's own ``target_ids``. A declared ``Target.id`` resolves to every
   fixture whose ``groups`` contain it; a fixture id resolves to that
   single fixture; an id matching neither produces a note.
2. Skip (never error) fixtures whose derived health is ``disabled``,
   ``missing``, or ``unbound`` (no binding) into
   ``skipped_fixture_ids`` plus a note. Degraded/conflicting fixtures are
   still planned.
3. Per-fixture state = ``scene.fixture_states[id]`` else
   ``scene.default_state``; if neither, skip the fixture with a note
   (no-state skips are notes only — ``skipped_fixture_ids`` is reserved
   for disabled/missing/unbound). When ``scene.palette`` is non-empty and
   motion is static or palette_cycle, fill RGB from ``palette_index`` or
   auto-spread before dispatch. Explicit override hex and gradient win.
4. Hand each fixture to ``select_renderer(...).plan_fixture(...)``.
5. ``fixture_states`` keys matching no registered fixture produce a note;
   registered keys not resolved by the targets produce a note as well.

A provider renderer that is not implemented/available (NotImplementedError,
e.g. while sibling renderers land in parallel) is reported as an
``unsupported`` fixture plan instead of failing the whole build.
"""

from __future__ import annotations

from ..domain.fixtures import Fixture, FixtureRegistry, HealthStatus, derive_health
from ..domain.fidelity import FidelityLevel, FixtureRenderPlan, RenderPlan
from ..domain.palette_resolve import compute_spread_indices, resolve_fixture_state
from ..domain.scenes import Scene
from .base import select_renderer

_SKIP_REASON = {
    HealthStatus.DISABLED: "disabled",
    HealthStatus.MISSING: "missing (provider resource not observed)",
    HealthStatus.UNBOUND: "unbound (no binding)",
}


def build_render_plan(
    scene: Scene,
    registry: FixtureRegistry,
    *,
    target_ids: list[str] | None = None,
) -> RenderPlan:
    """Compile a scene into a dry-run RenderPlan (contracts §3)."""
    fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}
    requested_targets = list(target_ids) if target_ids is not None else list(scene.target_ids)

    notes: list[str] = []

    # 1. Resolve targets to a deduplicated fixture set.
    resolved: dict[str, Fixture] = {}
    for target_id in requested_targets:
        if any(target.id == target_id for target in registry.targets):
            members = [fixture for fixture in registry.fixtures if target_id in fixture.groups]
            if not members:
                notes.append(f"target '{target_id}' has no member fixtures")
            for fixture in members:
                resolved[fixture.id] = fixture
        elif target_id in fixtures_by_id:
            resolved[target_id] = fixtures_by_id[target_id]
        else:
            notes.append(f"target '{target_id}' did not resolve to any fixture (unknown target id)")

    # 2-4. Per-fixture skip/state/dispatch, in deterministic id order.
    spread_indices = compute_spread_indices(scene, registry, resolved, requested_targets)
    fixture_plans: list[FixtureRenderPlan] = []
    skipped_fixture_ids: list[str] = []
    consumed_state_ids: set[str] = set()
    for fixture_id in sorted(resolved):
        fixture = resolved[fixture_id]
        health = derive_health(fixture)
        skip_reason = _SKIP_REASON.get(health)
        if skip_reason is not None:
            skipped_fixture_ids.append(fixture_id)
            notes.append(f"skipped fixture '{fixture_id}': {skip_reason}")
            continue
        is_override = fixture_id in scene.fixture_states
        state = scene.fixture_states.get(fixture_id, scene.default_state)
        if state is None:
            notes.append(f"skipped fixture '{fixture_id}': no fixture_states entry and scene has no default_state")
            continue
        if is_override:
            consumed_state_ids.add(fixture_id)
        state = resolve_fixture_state(
            scene,
            fixture,
            state,
            is_override=is_override,
            spread_index=spread_indices.get(fixture_id),
        )
        try:
            plan = select_renderer(fixture).plan_fixture(
                fixture, state, motion=scene.motion, palette=scene.palette
            )
        except NotImplementedError:
            provider = fixture.binding.provider if fixture.binding is not None else "unknown"
            plan = FixtureRenderPlan(
                fixture_id=fixture_id,
                provider=provider,
                fidelity=FidelityLevel.UNSUPPORTED,
                reason=f"provider renderer '{provider}' is not implemented yet (stub)",
            )
        fixture_plans.append(plan)

    # 5. Report fixture_states entries that never reached a fixture.
    for state_id in sorted(scene.fixture_states):
        if state_id not in fixtures_by_id:
            notes.append(f"fixture_states entry for unknown fixture id '{state_id}' ignored")
        elif state_id not in resolved and state_id not in consumed_state_ids:
            notes.append(f"fixture_states entry for '{state_id}' not applied: fixture not in resolved targets")

    return RenderPlan(
        scene_id=scene.id,
        target_ids=requested_targets,
        fixture_plans=fixture_plans,
        skipped_fixture_ids=skipped_fixture_ids,
        notes=notes,
    )
