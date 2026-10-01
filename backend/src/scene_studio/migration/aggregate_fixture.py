"""Dry-run-first removal of an aggregate HA helper fixture.

Scene Studio fixtures represent independently addressable endpoints.
Aggregate HA convenience groups whose children are already canonical Scene
Studio fixtures are not canonical fixtures; Scene Studio targets/groups
represent aggregation.

For each scene that still carries an aggregate override:

- compare it with ``default_state`` and child fixture overrides;
- drop it when it is redundant with default intent;
- otherwise expand unique intent onto child fixtures that do not already
  carry a more specific override (child-specific intent always wins);
- preserve advanced fields and server-owned metadata.

Never maps one target id onto another (no silent ``office -> living_room``).
An emptied target is retired only when no *active* scene still lists it
and dropping it would not leave an active scene with zero targets.

``safe_to_apply`` is false whenever applying would drop unique intent,
leave an active scene on an empty target, or otherwise lose semantics.
The CLI must refuse ``--apply`` before any write in that case.
"""

from __future__ import annotations

from ..domain.bindings import binding_ha_entity_ids
from ..domain.fixtures import BROAD_GROUP_ID, Fixture, FixtureRegistry
from ..domain.scenes import FixtureState, Scene
from ..stores import SceneStudioStore
from ..stores.atomic import atomic_write_json

__all__ = [
    "apply_aggregate_fixture_plan",
    "infer_child_fixture_ids",
    "plan_aggregate_fixture_removal",
]


def _state_dict(state: FixtureState | None) -> dict | None:
    if state is None:
        return None
    return state.to_dict()


def infer_child_fixture_ids(
    registry: FixtureRegistry,
    fixture: Fixture,
    *,
    child_ids: list[str] | None = None,
    ha_member_entity_ids: list[str] | None = None,
) -> list[str]:
    """Children the aggregate was standing in for, in stable id order.

    Priority: explicit ``child_ids``, then HA group members mapped through
    canonical fixture bindings, then other fixtures sharing a non-broad
    primary group. ``whole_house`` is never used as the parent group.
    """
    if child_ids is not None:
        return sorted({item for item in child_ids if item != fixture.id})

    if ha_member_entity_ids:
        wanted = [item for item in ha_member_entity_ids if isinstance(item, str)]
        mapped: list[str] = []
        for candidate in registry.fixtures:
            if candidate.id == fixture.id:
                continue
            entity_ids = binding_ha_entity_ids(candidate.binding)
            if any(entity_id in wanted for entity_id in entity_ids):
                mapped.append(candidate.id)
        return sorted(set(mapped))

    primary = [group for group in fixture.groups if group != BROAD_GROUP_ID]
    if not primary:
        return []
    return sorted(
        candidate.id
        for candidate in registry.fixtures
        if candidate.id != fixture.id and any(group in candidate.groups for group in primary)
    )


def _plan_scene(
    scene: Scene,
    *,
    fixture_id: str,
    child_ids: list[str],
    archived: bool,
) -> dict:
    before_states = {key: value.to_dict() for key, value in scene.fixture_states.items()}
    after_states = dict(before_states)
    notes: list[str] = []
    expanded_onto: list[str] = []
    unique_intent_unassigned = False

    override = before_states.get(fixture_id)
    default = _state_dict(scene.default_state)
    if override is not None:
        redundant = default is not None and override == default
        if redundant:
            notes.append(f"removed redundant {fixture_id} override (matches default_state)")
        else:
            for child_id in child_ids:
                if child_id in after_states:
                    notes.append(f"kept child-specific override on {child_id}")
                    continue
                after_states[child_id] = dict(override)
                expanded_onto.append(child_id)
            if not expanded_onto:
                unique_intent_unassigned = True
                notes.append(
                    f"unique {fixture_id} intent had no child without a more specific override"
                )
        after_states.pop(fixture_id, None)

    before_targets = list(scene.target_ids)
    after_targets = [target_id for target_id in before_targets if target_id != fixture_id]
    if before_targets != after_targets:
        notes.append(f"removed fixture-as-target id {fixture_id}")

    changed = after_states != before_states or after_targets != before_targets
    return {
        "scene_id": scene.id,
        "archived": archived,
        "changed": changed,
        "before_target_ids": before_targets,
        "after_target_ids": after_targets,
        "removed_override": override,
        "expanded_onto": expanded_onto,
        "unique_intent_unassigned": unique_intent_unassigned,
        "after_fixture_states": after_states if changed else before_states,
        "notes": notes,
    }


def _drop_empty_target(scene_plan: dict, target_id: str, *, active: bool) -> None:
    after = list(scene_plan["after_target_ids"])
    if target_id not in after:
        return
    remaining = [item for item in after if item != target_id]
    if not remaining:
        scene_plan["notes"].append(
            f"kept target {target_id!r} because dropping it would leave the scene with no targets"
        )
        scene_plan["kept_last_empty_target"] = True
        return
    if not active:
        scene_plan["after_target_ids"] = remaining
        scene_plan["changed"] = True
        scene_plan["notes"].append(f"dropped emptied target {target_id!r} from archived scene")
        return
    scene_plan["after_target_ids"] = remaining
    scene_plan["changed"] = True
    scene_plan["notes"].append(f"dropped emptied target {target_id!r}")


def plan_aggregate_fixture_removal(
    store: SceneStudioStore,
    fixture_id: str,
    *,
    child_ids: list[str] | None = None,
    ha_member_entity_ids: list[str] | None = None,
) -> dict:
    """Build a deterministic dry-run plan. Mutates nothing."""
    registry = store.fixtures.registry()
    fixture = next((item for item in registry.fixtures if item.id == fixture_id), None)
    if fixture is None:
        return {
            "fixture_id": fixture_id,
            "found": False,
            "child_ids": [],
            "scenes": [],
            "retire_targets": [],
            "warnings": [f"fixture {fixture_id!r} is not in the registry"],
            "safe_to_apply": False,
        }

    children = infer_child_fixture_ids(
        registry, fixture, child_ids=child_ids, ha_member_entity_ids=ha_member_entity_ids
    )
    warnings: list[str] = []

    scene_plans = []
    for scene in store.scenes.list_scenes():
        scene_plans.append(_plan_scene(scene, fixture_id=fixture_id, child_ids=children, archived=False))
    for scene in store.scenes.list_archived():
        scene_plans.append(_plan_scene(scene, fixture_id=fixture_id, child_ids=children, archived=True))

    remaining_groups = {
        item.id: list(item.groups)
        for item in registry.fixtures
        if item.id != fixture_id
    }
    retire_targets: list[str] = []
    for target in registry.targets:
        members = [fid for fid, groups in remaining_groups.items() if target.id in groups]
        if members:
            continue
        active_refs = [
            plan for plan in scene_plans if not plan["archived"] and target.id in plan["after_target_ids"]
        ]
        for plan in scene_plans:
            if target.id in plan["after_target_ids"]:
                _drop_empty_target(plan, target.id, active=not plan["archived"])
        active_refs_after = [
            plan for plan in scene_plans if not plan["archived"] and target.id in plan["after_target_ids"]
        ]
        if not active_refs_after:
            retire_targets.append(target.id)
        elif active_refs:
            warnings.append(
                f"target {target.id!r} would be empty but is still listed by "
                f"{[plan['scene_id'] for plan in active_refs_after]}"
            )

    unique_unassigned = [plan["scene_id"] for plan in scene_plans if plan["unique_intent_unassigned"]]
    if unique_unassigned:
        warnings.append(
            "unique aggregate intent could not be assigned for scenes: " + ", ".join(unique_unassigned)
        )
        if not children:
            warnings.append(
                f"no child fixtures inferred for {fixture_id!r}; unique overrides cannot be expanded"
            )

    kept_empty = [
        plan["scene_id"]
        for plan in scene_plans
        if not plan["archived"] and plan.get("kept_last_empty_target")
    ]
    if kept_empty and not any("would be empty but is still listed" in item for item in warnings):
        warnings.append(
            "removal would leave active scene(s) dependent on an empty target: " + ", ".join(kept_empty)
        )

    return {
        "fixture_id": fixture_id,
        "found": True,
        "fixture_name": fixture.name,
        "child_ids": children,
        "scenes": scene_plans,
        "retire_targets": retire_targets,
        "warnings": warnings,
        "safe_to_apply": not warnings,
    }


def apply_aggregate_fixture_plan(store: SceneStudioStore, plan: dict) -> dict:
    """Apply a plan produced by :func:`plan_aggregate_fixture_removal`.

    Scene documents are rewritten first so ``remove_fixture`` is not blocked
    by remaining active ``fixture_states`` keys. Archived documents keep
    their archive location (no restore/re-archive, so ``archived_at`` stays).
    """
    if not plan.get("safe_to_apply"):
        raise ValueError("plan is not safe to apply: " + "; ".join(plan.get("warnings") or []))
    fixture_id = plan["fixture_id"]

    for scene_plan in plan.get("scenes") or []:
        if not scene_plan.get("changed"):
            continue
        scene_id = scene_plan["scene_id"]
        if scene_plan.get("archived"):
            current = store.scenes.get_scene(scene_id, include_archived=True)
            payload = current.to_dict()
            payload["target_ids"] = list(scene_plan["after_target_ids"])
            payload["fixture_states"] = dict(scene_plan["after_fixture_states"])
            atomic_write_json(store.scenes.archive_dir / f"{scene_id}.json", payload)
        else:
            current = store.scenes.get_scene(scene_id)
            payload = current.to_dict()
            payload["target_ids"] = list(scene_plan["after_target_ids"])
            payload["fixture_states"] = dict(scene_plan["after_fixture_states"])
            store.scenes.replace_scene(scene_id, payload)

    store.reload()
    store.remove_fixture(fixture_id)
    retired = []
    for target_id in plan.get("retire_targets") or []:
        try:
            store.fixtures.remove_target(target_id)
            retired.append(target_id)
        except Exception as exc:  # keep going; report
            plan.setdefault("warnings", []).append(f"could not retire target {target_id!r}: {exc}")
    store.reload()
    return {"removed_fixture": fixture_id, "retired_targets": retired, "warnings": list(plan.get("warnings") or [])}
