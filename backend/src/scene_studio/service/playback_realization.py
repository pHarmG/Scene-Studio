"""R5B — provider-native playback realization.

Orchestration seam between the R5A session lifecycle and the providers.
Takes the dynamic scene (intent + resolved states), the R5A render plan
(already capability-gated by the renderers), the fixture registry, and
produces per-fixture :class:`FixtureExecution` records plus the provider
operations realized through the executor port. Static ``scene.apply``
keeps its existing renderer/executor path untouched.

Realization rules (R5 plan v2 §2/§3, corrected by the R5D Hue corrective pass):

Hue (``hue_v2``)
- **Hue dynamic state is a scene-resource feature.** Every dynamic-native
  Hue fixture — gradient-capable or not — contributes its static state as a
  CLIP v2 scene **action**; actions are aggregated per group and realized
  through a managed bridge scene keyed by ``(scene_id, hue_group_id)`` with
  the bridge resource id persisted in the provider-state store
  (``store/provider_state/hue_scenes.json``). One managed resource per
  group per Scene Studio scene: created once (full deterministic payload:
  actions + metadata + group + palette + scene-level speed), updated in
  place with the same complete representation, recalled once per group —
  never per fixture.
- The managed scene carries the canonical palette (hex converted to Hue XY)
  and the scene-level speed; recall uses ``{"recall": {"action":
  "dynamic_palette"}}`` for start/resume and ``{"recall": {"action":
  "static"}}`` for pause/stop. No light PUT ever carries ``dynamics.status``
  (refuted live: the bridge rejects it with 207 "cannot be written").
- Pause/resume/stop fidelity is recorded **approximately** until the second
  R5D live run proves whether static→dynamic transitions behave well enough
  to classify native/equivalent.
- fixtures whose plan carries no dynamic marker (approximate static
  snapshots) execute their static per-light ops as ``approximate_static``.

WLED (``wled``)
- plan ops carry native ``fx/pal/sx`` data; realization adds the
  stale-freeze guarantee: start and static-apply segment ops always emit
  ``frz: false``; pause emits ``frz: true``; resume emits ``frz: false``;
  stop **freezes the segment at its current frame** (``frz: true``) and the
  provider remains physically frozen until the next Scene Studio command
  affecting that segment clears ``frz``. Segments on one controller are
  batched into a single ``wled.post_state`` mutation; unrelated segments
  are never included. ``lor`` (live override) is surfaced in execution
  detail when present in fetched state — read-only, no arbitration.

No universal animation ticker. Provider failure is per-fixture/per-provider
degradation recorded honestly in the execution records. Hue speed-0 pause
and managed-scene update/recall semantics are verified against the live
bridge/controller in R5D and classified honestly if they differ.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..domain.fixtures import Fixture, FixtureRegistry
from ..domain.fidelity import FidelityLevel, ProviderOperation, RenderPlan
from ..domain.playback import FixtureExecution, PlaybackSession
from ..domain.scenes import FixtureState, Motion, MotionMode
from ..renderers.color import hex_to_xy
from .ports import receipt

__all__ = [
    "HueManagedSceneStore", "HueManagedZoneStore", "PlaybackOutcome",
    "PlaybackRealizer", "zone_membership_fingerprint",
]

_HUE_STATE_SCHEMA_VERSION = 1


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class HueManagedSceneStore:
    """Persisted ``(scene_id, hue_group_id) -> bridge scene resource id`` map.

    The bridge resource id is canonical provider-managed state; the readable
    ``SS-<scene_id[:18]>-<group[:8]>`` label (bridge name rules: maxLength
    32, ASCII separators) is a
    recovery hint only.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict:
        doc = _load_json(self._path, {"schema_version": _HUE_STATE_SCHEMA_VERSION, "managed": {}})
        if not isinstance(doc, dict) or not isinstance(doc.get("managed"), dict):
            return {"schema_version": _HUE_STATE_SCHEMA_VERSION, "managed": {}}
        return doc

    def _save(self, doc: dict) -> None:
        from ..stores.atomic import atomic_write_json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, doc)

    @staticmethod
    def key(scene_id: str, hue_group_id: str) -> str:
        return f"{scene_id}::{hue_group_id}"

    def resource_id(self, scene_id: str, hue_group_id: str) -> str | None:
        entry = self._load()["managed"].get(self.key(scene_id, hue_group_id))
        if not isinstance(entry, dict):
            return None
        resource_id = entry.get("resource_id")
        # Labels are recovery hints, never bridge resource identifiers. Older
        # R5B drafts wrote ``by-label:...`` here; do not send those to Hue.
        return resource_id if (
            isinstance(resource_id, str) and resource_id and not resource_id.startswith("by-label:")
        ) else None

    def record(
        self, scene_id: str, hue_group_id: str, resource_id: str, label: str, now_iso: str
    ) -> None:
        doc = self._load()
        doc["managed"][self.key(scene_id, hue_group_id)] = {
            "scene_id": scene_id,
            "hue_group_id": hue_group_id,
            "resource_id": resource_id,
            "label": label,
            "updated_at": now_iso,
        }
        self._save(doc)

    def groups_for_scene(self, scene_id: str) -> list[str]:
        """The resolved Hue execution group rids a scene has managed-scene
        resources for (lifecycle recall targets, in stable order)."""
        doc = self._load()
        rids = {
            entry.get("hue_group_id")
            for entry in doc.get("managed", {}).values()
            if isinstance(entry, dict) and entry.get("scene_id") == scene_id
            and isinstance(entry.get("hue_group_id"), str) and entry["hue_group_id"]
        }
        return sorted(rids)


def zone_membership_fingerprint(light_rids: list[str]) -> str:
    """Deterministic hash of the sorted participating Hue light service RIDs.

    The stable logical key of a Scene Studio isolation zone: two scenes with
    the exact same participant set share one zone; any difference in the set
    yields a different identity."""
    return hashlib.sha256("\n".join(sorted(light_rids)).encode("utf-8")).hexdigest()[:12]


class HueManagedZoneStore:
    """Persisted Scene-Studio-owned isolation zones keyed by membership
    fingerprint (sorted participating light service RIDs).

    Zones are the provider-native isolation boundary for group-wide Hue
    dynamic_palette animation: a managed scene hangs on a zone whose members
    are exactly the scene's participating lights. Zones are reused across
    scenes with identical participant sets and are never created/deleted per
    playback start/stop."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict:
        doc = _load_json(self._path, {"schema_version": self._SCHEMA_VERSION, "zones": {}})
        if not isinstance(doc, dict) or not isinstance(doc.get("zones"), dict):
            return {"schema_version": self._SCHEMA_VERSION, "zones": {}}
        return doc

    def _save(self, doc: dict) -> None:
        from ..stores.atomic import atomic_write_json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, doc)

    @staticmethod
    def label(fingerprint: str) -> str:
        return f"SS-Z-{fingerprint}"

    def by_fingerprint(self, fingerprint: str) -> dict | None:
        entry = self._load()["zones"].get(fingerprint)
        if not isinstance(entry, dict):
            return None
        return entry if entry.get("zone_rid") else None

    def record(
        self, fingerprint: str, light_rids: list[str], zone_rid: str, label: str, now_iso: str
    ) -> None:
        doc = self._load()
        previous = doc["zones"].get(fingerprint, {})
        doc["zones"][fingerprint] = {
            "fingerprint": fingerprint,
            "light_rids": sorted(light_rids),
            "zone_rid": zone_rid,
            "label": label,
            "created_at": previous.get("created_at") or now_iso,
            "updated_at": now_iso,
        }
        self._save(doc)


@dataclass
class PlaybackOutcome:
    """Everything the engine needs to update a session after realization."""

    fixture_executions: list[FixtureExecution]
    receipts: list[dict]
    failures: list[tuple[str, dict]]
    ok_count: int

    def extend(self, other: "PlaybackOutcome") -> None:
        self.fixture_executions.extend(other.fixture_executions)
        self.receipts.extend(other.receipts)
        self.failures.extend(other.failures)

    def counts(self) -> dict:
        native = sum(1 for e in self.fixture_executions if e.execution.startswith("native"))
        approximate = sum(1 for e in self.fixture_executions if e.execution == "approximate_static")
        return {
            "native": native,
            "approximate": approximate,
            "failed": sum(1 for e in self.fixture_executions if not e.ok),
        }


class PlaybackRealizer:
    """Realizes dynamic playback intent through provider-native mechanisms."""

    def __init__(self, executor, managed_scenes: HueManagedSceneStore, now_iso_fn,
                 managed_zones: "HueManagedZoneStore | None" = None) -> None:
        self._executor = executor
        self._managed = managed_scenes
        self._now_iso = now_iso_fn
        if managed_zones is None:
            managed_zones = HueManagedZoneStore(managed_scenes._path.parent / "hue_zones.json")
        self._zones = managed_zones

    # -- start -------------------------------------------------------------

    def realize_start(self, scene, plan: RenderPlan, registry: FixtureRegistry) -> PlaybackOutcome:
        """Partition planned fixtures across the Hue/WLED realization paths,
        execute, and return per-fixture execution outcomes."""
        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}
        outcome = PlaybackOutcome([], [], [], 0)

        per_light_plans: list[FixtureRenderPlan] = []   # executed via plan ops
        hue_dynamic: list[dict] = []                    # managed-scene participants
        approx_plans: list[FixtureRenderPlan] = []

        for fp in plan.fixture_plans:
            fixture = fixtures_by_id.get(fp.fixture_id)
            if fixture is None:
                outcome.fixture_executions.append(
                    FixtureExecution(fp.fixture_id, fp.provider, "approximate_static",
                                     "unsupported", False, "fixture missing from registry")
                )
                continue
            if fp.provider == "hue_v2":
                if any(operation.op == "hue.put_scene_dynamic" for operation in fp.operations):
                    hue_dynamic.append({"fixture": fixture, "plan": fp})
                    continue
                approx_plans.append(fp)
                continue
            # wled / ha_light / others: plan ops are already the realization
            per_light_plans.append(fp)

        # 1. per-light plans (wled effects, static approximations)
        for fp in [*per_light_plans, *approx_plans]:
            execution, fidelity = self._classify_plan_execution(fp)
            ops_ok, last_failure = self._execute_ops(fp, fixtures_by_id, outcome)
            if not fp.operations:
                ops_ok, last_failure = False, fp.reason or "render plan has no executable provider operation"
            outcome.fixture_executions.append(
                FixtureExecution(
                    fixture_id=fp.fixture_id,
                    provider=fp.provider,
                    execution=execution,
                    fidelity=fidelity,
                    ok=ops_ok,
                    detail=last_failure or fp.reason or "",
                )
            )

        # 2. Hue managed scenes: one execution group (zone/room) for the
        # exact dynamic participant set — the Scene Studio isolation
        # boundary for group-wide Hue dynamic_palette animation.
        if hue_dynamic:
            group_result = self._realize_hue_managed_scene(scene, hue_dynamic)
            outcome.fixture_executions.extend(group_result.fixture_executions)
            outcome.receipts.extend(group_result.receipts)
            outcome.failures.extend(group_result.failures)

        outcome.ok_count = sum(1 for e in outcome.fixture_executions if e.ok)
        planned = {fp.fixture_id for fp in plan.fixture_plans}
        realized = {execution.fixture_id for execution in outcome.fixture_executions}
        if planned != realized:
            raise AssertionError(f"playback realization fixture coverage mismatch: planned={planned} realized={realized}")
        return outcome

    # -- pause / resume / stop ----------------------------------------------

    def realize_pause(self, session: PlaybackSession, scene, registry: FixtureRegistry) -> PlaybackOutcome:
        outcome = self._wled_freeze(session, registry, freeze=True, verb="paused")
        outcome.extend(self._hue_lifecycle(session, scene, registry, action="pause"))
        outcome.ok_count = sum(1 for e in outcome.fixture_executions if e.ok)
        return outcome

    def realize_resume(self, session: PlaybackSession, scene, registry: FixtureRegistry) -> PlaybackOutcome:
        outcome = self._wled_freeze(session, registry, freeze=False, verb="resumed")
        outcome.extend(self._hue_lifecycle(session, scene, registry, action="resume"))
        outcome.ok_count = sum(1 for e in outcome.fixture_executions if e.ok)
        return outcome

    def realize_stop(self, session: PlaybackSession, scene, registry: FixtureRegistry) -> PlaybackOutcome:
        """Stop freezes WLED segments at their current frame; the provider
        remains physically frozen until the next Scene Studio command
        affecting that segment clears frz. Hue sessions stop via provider
        stop operations (last frame becomes static)."""
        outcome = self._wled_freeze(session, registry, freeze=True, verb="stopped")
        outcome.extend(self._hue_lifecycle(session, scene, registry, action="stop"))
        outcome.ok_count = sum(1 for e in outcome.fixture_executions if e.ok)
        return outcome

    # -- internals -----------------------------------------------------------

    def _classify_plan_execution(self, fp: FixtureRenderPlan) -> tuple[str, str]:
        """Map a planned per-light fixture to its execution kind/fidelity."""
        ops = [operation.op for operation in fp.operations]
        if fp.provider == "wled" and any(
            operation.payload.get("seg", [{}])[0].get("fx") is not None
            for operation in fp.operations
            if operation.payload.get("seg")
        ):
            return "native_effect", "native"
        fidelity = fp.fidelity.value if fp.fidelity is not FidelityLevel.UNSUPPORTED else "unsupported"
        return "approximate_static", fidelity

    def _execute_ops(self, fp: FixtureRenderPlan, fixtures_by_id: dict, outcome: PlaybackOutcome) -> tuple[bool, str]:
        fixture = fixtures_by_id.get(fp.fixture_id)
        ops_ok = True
        last_failure = ""
        for operation in fp.operations:
            one = self._execute_operation(operation, fixture, fp.fixture_id)
            outcome.receipts.append(one)
            if one["ok"]:
                continue
            ops_ok = False
            last_failure = one["detail"]
            outcome.failures.append((fp.fixture_id, one))
        return ops_ok, last_failure

    def _execute_operation(self, operation: ProviderOperation, fixture: Fixture | None, fixture_id: str) -> dict:
        raw: dict | None = None
        try:
            if fixture is None:
                raise ValueError(f"fixture '{fixture_id}' disappeared from the registry mid-realization")
            raw = self._executor.execute(operation, fixture)
            if not isinstance(raw, dict):
                raise ValueError("executor returned a non-dict receipt")
            from .ports import receipt

            result = receipt(
                bool(raw.get("ok")),
                str(raw.get("provider", operation.provider)),
                str(raw.get("op", operation.op)),
                str(raw.get("detail", "")),
            )
        except Exception as exc:  # executor adapters must not raise, but never trust the boundary
            from .ports import receipt

            result = receipt(False, operation.provider, operation.op, f"{type(exc).__name__}: {exc}")
            raw = None
        result["fixture_id"] = fixture_id
        result["resource_ref"] = operation.resource_ref
        # GET-backed read ops surface provider payloads here (e.g. scene
        # resource id lookups); write receipts carry no data.
        if isinstance(raw, dict) and raw.get("data") is not None:
            result["data"] = raw["data"]
        return result

    # -- hue managed scenes ---------------------------------------------------

    def _realize_hue_managed_scene(self, scene, members: list[dict]) -> PlaybackOutcome:
        """Realize dynamic Hue playback through a managed scene bound to a
        resolved isolation group.

        The execution group is resolved from the exact sorted set of
        participating Hue light service RIDs:

        - exact membership of an authoritative room -> use that room
          directly (no manufactured zone, no anchors: everyone participates);
        - otherwise ensure/reuse the Scene-Studio-owned zone whose
          membership fingerprint matches (ownership-verified recovery:
          deterministic SS-Z label AND exact membership), so the group-wide
          dynamic_palette animation can only ever touch the participants.
        """
        executions: list[FixtureExecution] = []
        receipts: list[dict] = []
        failures: list[tuple[str, dict]] = []

        now = self._now_iso()

        participants: list[str] = []
        contributing: list[dict] = []
        actions: list[dict] = []
        for member in members:
            fixture: Fixture = member["fixture"]
            resource_id = getattr(fixture.binding, "resource_id", None)
            contribution = self._action_contribution(scene, fixture, member["plan"])
            if not resource_id or contribution is None:
                executions.append(
                    FixtureExecution(
                        fixture_id=fixture.id,
                        provider="hue_v2",
                        execution="approximate_static",
                        fidelity="unsupported",
                        ok=False,
                        detail="no representable CLIP v2 scene action for this fixture state",
                    )
                )
                continue
            participants.append(resource_id)
            actions.append(
                {"target": {"rid": resource_id, "rtype": "light"}, "action": contribution}
            )
            contributing.append(member)
        if not actions:
            return PlaybackOutcome(executions, receipts, failures, 0)

        participants = sorted(participants)
        representative = contributing[0]["fixture"]
        fingerprint = zone_membership_fingerprint(participants)

        overview = self._execute_operation(
            ProviderOperation(
                provider="hue_v2", op="hue.get_groups_overview", resource_ref="all",
                payload={}, description="read rooms/zones membership + light/device topology",
            ),
            representative, f"managed:{scene.id}",
        )
        receipts.append(overview)
        overview_data = (overview.get("data") or {}) if overview["ok"] else {}
        groups = overview_data.get("groups") if isinstance(overview_data, dict) else None
        light_to_device = overview_data.get("light_to_device") if isinstance(overview_data, dict) else None
        if not isinstance(groups, list) or not isinstance(light_to_device, dict):
            for member in members:
                executions.append(FixtureExecution(
                    fixture_id=member["fixture"].id, provider="hue_v2",
                    execution="native_scene", fidelity="unsupported", ok=False,
                    detail=f"Hue group topology read failed: {overview['detail']}",
                ))
            failures.append((f"managed:{scene.id}", overview))
            return PlaybackOutcome(executions, receipts, failures, 0)

        resolved_group, group_note = self._resolve_execution_group(
            scene.id, participants, fingerprint, groups, light_to_device, receipts, failures,
            representative, now,
        )
        if resolved_group is None:
            for member in members:
                executions.append(FixtureExecution(
                    fixture_id=member["fixture"].id, provider="hue_v2",
                    execution="native_scene", fidelity="unsupported", ok=False,
                    detail=f"Hue isolation group resolution failed: {group_note}",
                ))
            return PlaybackOutcome(executions, receipts, failures, 0)
        group_rid, group_rtype = resolved_group

        label = f"SS-{scene.id[:18]}-{group_rid[:8]}"
        resource_id, verb, resolution_receipts, failure = self._resolve_managed_scene(
            scene, group_rid, group_rtype, label, actions, representative, now
        )
        receipts.extend(resolution_receipts)
        if failure is not None:
            for member in members:
                fixture = member["fixture"]
                executions.append(
                    FixtureExecution(
                        fixture_id=fixture.id,
                        provider="hue_v2",
                        execution="native_scene",
                        fidelity="unsupported",
                        ok=False,
                        detail=f"managed scene {verb} failed: {failure['detail']}",
                    )
                )
            failures.append((f"managed:{scene.id}:{group_rid}", failure))
            return PlaybackOutcome(executions, receipts, failures, 0)

        # Dynamic start: recall the managed scene as a dynamic palette. The
        # animation speed lives on the scene resource (scene-level `speed`
        # property), never as a light-style dynamics member on the recall.
        recall_op = ProviderOperation(
            provider="hue_v2",
            op="hue.recall_scene",
            resource_ref=resource_id,
            payload={"recall": {"action": "dynamic_palette"}},
            description=f"recall managed scene as dynamic palette ({verb})",
        )
        recall = self._execute_operation(recall_op, representative, f"managed:{scene.id}:{group_rid}")
        receipts.append(recall)

        for member in members:
            fixture = member["fixture"]
            executions.append(
                FixtureExecution(
                    fixture_id=fixture.id,
                    provider="hue_v2",
                    execution="native_scene",
                    fidelity="native",
                    ok=recall["ok"],
                    detail=(
                        f"managed scene {verb} + recalled dynamic_palette "
                        f"(group {group_rtype} {group_rid}, resource {resource_id})"
                        if recall["ok"]
                        else f"recall failed: {recall['detail']}"
                    ),
                )
            )
        if not recall["ok"]:
            failures.append((f"managed:{scene.id}:{group_rid}", recall))
        return PlaybackOutcome(executions, receipts, failures, 1 if recall["ok"] else 0)

    def _resolve_execution_group(self, scene_id, participants, fingerprint, groups,
                                 light_to_device, receipts, failures, representative, now):
        """Resolve the isolation group for the exact participant set.

        Returns ``((group_rid, group_rtype), note)``; the group rid is None
        on failure with ``note`` carrying the reason.
        """
        exact = next(
            (g for g in groups
             if g.get("rtype") in ("room", "zone") and sorted(g.get("light_rids") or []) == participants),
            None,
        )
        if exact is not None and exact.get("rtype") == "room":
            # Participants exactly cover an authoritative room: use it
            # directly rather than manufacturing a redundant zone. Everyone
            # in the group participates, so no anchors are needed.
            return (exact["rid"], "room"), "authoritative room exact membership"

        # Zone path: reuse/repair/create the Scene-Studio-owned zone keyed
        # by the membership fingerprint. Never adopt a zone without both the
        # deterministic SS-Z ownership label AND exact membership.
        zone_label = HueManagedZoneStore.label(fingerprint)
        persisted = self._zones.by_fingerprint(fingerprint)
        persisted_rid = (persisted or {}).get("zone_rid")

        def zone_by_rid(rid):
            return next((g for g in groups if g.get("rid") == rid and g.get("rtype") == "zone"), None)

        def zone_membership(zone):
            return sorted(zone.get("light_rids") or [])

        # Live-verified: zone create/repair children reference the lights
        # themselves ({"rid": <light service rid>, "rtype": "light"});
        # device-rtype children are rejected with "Invalid children".
        children = [{"rid": rid, "rtype": "light"} for rid in participants]

        # 1. persisted zone (recorded by fingerprint = verified ownership):
        #    reuse as-is, or repair its membership to the exact set.
        persisted_zone = zone_by_rid(persisted_rid) if persisted_rid else None
        if persisted_zone is not None:
            if zone_membership(persisted_zone) != participants:
                fixed = self._execute_operation(
                    ProviderOperation(
                        provider="hue_v2", op="hue.put_zone", resource_ref=persisted_rid,
                        payload={"children": children},
                        description=f"repair SS zone membership to fingerprint {fingerprint}",
                    ),
                    representative, f"zone:{zone_label}",
                )
                receipts.append(fixed)
                if not fixed["ok"]:
                    return None, f"zone membership repair failed: {fixed['detail']}"
            self._zones.record(fingerprint, participants, persisted_rid, zone_label, now)
            return (persisted_rid, "zone"), f"reused SS zone {zone_label}"

        # 2. recovery by deterministic ownership label: adopt ONLY when the
        # membership is exactly the participant set (label alone never
        # adopts; a same-labelled zone with different membership is treated
        # as user property and left untouched).
        adoptable = next(
            (g for g in groups
             if g.get("rtype") == "zone" and g.get("name") == zone_label
             and zone_membership(g) == participants),
            None,
        )
        if adoptable is not None:
            self._zones.record(fingerprint, participants, adoptable["rid"], zone_label, now)
            return (adoptable["rid"], "zone"), f"recovered SS zone {zone_label}"

        # 3. create our own zone (a same-labelled user zone with different
        # membership is never modified; bridge zone names need not be unique
        # and future runs prefer the persisted rid above).
        created = self._execute_operation(
            ProviderOperation(
                provider="hue_v2", op="hue.post_zone", resource_ref=zone_label,
                payload={"metadata": {"name": zone_label, "archetype": "other"},
                         "children": children},
                description=f"create SS isolation zone {zone_label}",
            ),
            representative, f"zone:{zone_label}",
        )
        receipts.append(created)
        created_rid = (created.get("data") or {}).get("zone_rid") if created["ok"] else None
        if isinstance(created_rid, str) and created_rid:
            self._zones.record(fingerprint, participants, created_rid, zone_label, now)
            return (created_rid, "zone"), f"created SS zone {zone_label}"
        return None, f"zone creation failed: {created.get('detail', 'no zone rid returned')}"

        created = self._execute_operation(
            ProviderOperation(
                provider="hue_v2", op="hue.post_zone", resource_ref=zone_label,
                payload={"metadata": {"name": zone_label, "archetype": "other"},
                         "children": children},
                description=f"create SS isolation zone {zone_label}",
            ),
            representative, f"zone:{zone_label}",
        )
        receipts.append(created)
        created_rid = (created.get("data") or {}).get("zone_rid") if created["ok"] else None
        if isinstance(created_rid, str) and created_rid:
            self._zones.record(fingerprint, participants, created_rid, zone_label, now)
            return (created_rid, "zone"), f"created SS zone {zone_label}"
        return None, f"zone creation failed: {created.get('detail', 'no zone rid returned')}"

    def _resolve_managed_scene(self, scene, hue_group_id, hue_group_type, label, actions, fixture, now):
        """Resolve/update a Scene Studio-owned bridge scene without label IDs.

        A persisted RID is preferred, but a failed update is treated as stale.
        Recovery lookup is constrained by both our deterministic label and the
        authoritative provider group before a new scene is created. Create
        and update both send the complete deterministic Scene Studio-owned
        representation (actions + metadata + palette + speed) so behavior
        never depends on accidental merge state from an older resource.
        """
        receipts: list[dict] = []
        dynamic = self._managed_scene_dynamic(scene)
        existing = self._managed.resource_id(scene.id, hue_group_id)
        if existing:
            updated = self._put_managed_scene(existing, actions, label, dynamic, fixture, hue_group_id)
            receipts.append(updated)
            if updated["ok"]:
                return existing, "updated", receipts, None

        found = self._find_managed_scene(label, hue_group_id, hue_group_type, fixture)
        receipts.append(found)
        if not found["ok"]:
            return None, "recovery", receipts, found
        found_id = found.get("data", {}).get("resource_id")
        if found.get("data", {}).get("matched") and isinstance(found_id, str) and found_id:
            self._managed.record(scene.id, hue_group_id, found_id, label, now)
            updated = self._put_managed_scene(found_id, actions, label, dynamic, fixture, hue_group_id)
            receipts.append(updated)
            if updated["ok"]:
                return found_id, "recovered", receipts, None
            return None, "recovered", receipts, updated

        create = ProviderOperation(
            provider="hue_v2",
            op="hue.post_scene",
            resource_ref=label,
            payload={
                "metadata": {"name": label},
                "group": {"rid": hue_group_id, "rtype": hue_group_type},
                "actions": actions,
                **dynamic,
            },
            description=f"create managed dynamic scene for group {hue_group_id}",
        )
        posted = self._execute_operation(create, fixture, f"managed:{scene.id}:{hue_group_id}")
        receipts.append(posted)
        created_id = posted.get("data", {}).get("resource_id") if posted["ok"] else None
        if isinstance(created_id, str) and created_id:
            self._managed.record(scene.id, hue_group_id, created_id, label, now)
            return created_id, "created", receipts, None
        if not posted["ok"]:
            return None, "created", receipts, posted

        # A successful POST without a bridge RID must not invent an ID. Make
        # one bounded recovery lookup; otherwise report the ambiguity honestly.
        recovered = self._find_managed_scene(label, hue_group_id, hue_group_type, fixture)
        receipts.append(recovered)
        recovered_id = recovered.get("data", {}).get("resource_id") if recovered["ok"] else None
        if recovered.get("data", {}).get("matched") and isinstance(recovered_id, str) and recovered_id:
            self._managed.record(scene.id, hue_group_id, recovered_id, label, now)
            return recovered_id, "created/recovered", receipts, None
        if not recovered["ok"]:
            return None, "created/recovery", receipts, recovered
        return None, "created", receipts, {
            "ok": False, "provider": "hue_v2", "op": "hue.post_scene",
            "detail": "POST succeeded but bridge returned no usable scene resource id and recovery found no match",
        }

    @staticmethod
    def _managed_scene_dynamic(scene) -> dict:
        """The scene-level dynamic members of the managed Hue scene payload.

        ``palette`` uses the real Hue CLIP v2 ScenePalette shape — a ``color``
        ARRAY of ``{"color": {"xy": {...}}}`` targets (verified against the
        live bridge's own scene resources; NOT a ``color_targets`` wrapper) —
        built from the canonical Scene Studio palette converted through the
        existing hex -> Hue XY conversion (never copied as hex). ``speed`` is
        the canonical motion speed (normalized 0..1, the scene-level property
        Hue animates with). ``palette`` is omitted when the scene carries no
        palette (an actions-only scene), keeping the payload deterministic
        either way.
        """
        dynamic: dict = {"speed": float(scene.motion.speed)}
        if scene.palette:
            dynamic["palette"] = {
                "color": [
                    {
                        "color": {"xy": {"x": x, "y": y}},
                        # PalettePost: every color target carries the scene
                        # dimming level it animates at.
                        "dimming": {"brightness": float(scene.brightness) if scene.brightness is not None else 100.0},
                    }
                    for x, y in (hex_to_xy(hex_color) for hex_color in scene.palette)
                ],
                # PalettePost requires these members even when empty.
                "dimming": [],
                "color_temperature": [],
            }
        return dynamic

    def _put_managed_scene(self, resource_id, actions, label, dynamic, fixture, hue_group_id):
        operation = ProviderOperation(
            provider="hue_v2", op="hue.put_scene", resource_ref=resource_id,
            payload={"actions": actions, "metadata": {"name": label}, **dynamic},
            description=f"update managed dynamic scene for group {hue_group_id}",
        )
        return self._execute_operation(operation, fixture, f"managed:{resource_id}")

    def _find_managed_scene(self, label, hue_group_id, hue_group_type, fixture):
        operation = ProviderOperation(
            provider="hue_v2", op="hue.find_scene", resource_ref=label,
            payload={"name": label, "group": {"rid": hue_group_id, "rtype": hue_group_type}},
            description=f"recover Scene Studio managed scene for group {hue_group_id}",
        )
        return self._execute_operation(operation, fixture, f"managed-find:{hue_group_id}")

    def _action_contribution(self, scene, fixture: Fixture, fp: FixtureRenderPlan) -> dict | None:
        """The fixture's static hue.put_light payload = its CLIP v2 scene
        action contribution (capability-gated by the renderer). Extracted
        from the plan when present; re-rendered from the scene state
        otherwise (covers fixtures whose plan carried the managed-scene
        placeholder instead of a static snapshot)."""
        for operation in fp.operations:
            if operation.op == "hue.put_light" and "dynamics" not in operation.payload:
                return dict(operation.payload)
        state = scene.fixture_states.get(fixture.id) or scene.default_state
        if state is None:
            return None
        try:
            from .renderers.hue import HueRenderer
            from ..domain.scenes import Motion, MotionMode

            fixture_plan = HueRenderer().plan_fixture(
                fixture, state,
                motion=Motion(mode=MotionMode.STATIC, speed=0.0),
                palette=list(scene.palette),
            )
        except Exception:
            return None
        for operation in fixture_plan.operations:
            if operation.op == "hue.put_light":
                return dict(operation.payload)
        return None

    def _hue_lifecycle(self, session: PlaybackSession, scene, registry: FixtureRegistry, *, action: str) -> PlaybackOutcome:
        """Hue-native pause/resume/stop for a session's fixtures.

        All dynamic Hue state lives in managed scene resources, so lifecycle
        transitions are scene recalls:

        - pause: ``{"recall": {"action": "static"}}`` — the animation stops,
          but the animation position is not preserved. Fidelity is recorded
          **approximately**; the second R5D live run must determine whether
          static→dynamic transitions are good enough to classify the
          lifecycle native/equivalent/approximate.
        - resume: ``{"recall": {"action": "dynamic_palette"}}`` — restarts
          the scene animation. Also approximate until proven (continuity
          from the static pause is not established).
        - stop: ``{"recall": {"action": "static"}}``.

        The scene-level speed property stays on the resource; recall payloads
        never carry a light-style ``dynamics`` member. Legacy persisted
        sessions may still record ``native_dynamic_palette`` fixtures from
        the refuted per-light path — those are reported unsupported rather
        than given a provider write that cannot succeed.
        """
        outcome = PlaybackOutcome([], [], [], 0)
        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}

        group_rids = self._managed.groups_for_scene(session.scene_id)
        for execution in session.fixture_executions:
            if execution.execution in ("approximate_static", "pending"):
                continue
            fixture = fixtures_by_id.get(execution.fixture_id)
            if fixture is None:
                continue
            if execution.execution == "native_scene":
                if not group_rids:
                    failed = {
                        "ok": False, "provider": "hue_v2", "op": "hue.recall_scene",
                        "detail": "no managed scene resource recorded for this scene; re-start playback",
                    }
                    outcome.failures.append((fixture.id, failed))
                    outcome.fixture_executions.append(FixtureExecution(
                        fixture.id, "hue_v2", "native_scene", "unsupported", False, failed["detail"]
                    ))
                continue
            if execution.execution == "native_dynamic_palette":
                # Refuted provider path (bridge rejects dynamics.status on
                # lights): never re-issue those writes.
                failed = {
                    "ok": False, "provider": "hue_v2", "op": "hue.recall_scene",
                    "detail": (
                        "per-light Hue dynamics is not writable on this bridge; "
                        "re-start the scene to realize playback through a managed scene"
                    ),
                }
                outcome.failures.append((fixture.id, failed))
                outcome.fixture_executions.append(FixtureExecution(
                    fixture.id, "hue_v2", "native_dynamic_palette", "unsupported", False, failed["detail"]
                ))

        recall_action = "dynamic_palette" if action == "resume" else "static"
        fidelity_note = {
            "pause": "static recall stops the animation but does not preserve its position; "
                     "second R5D live run classifies pause fidelity (native/equivalent/approximate)",
            "resume": "dynamic_palette recall restarts the scene animation; continuity from the "
                      "static pause is not yet proven — second R5D live run classifies resume fidelity",
            "stop": "static recall ends the animation; the final frame is not preserved",
        }[action]
        # One provider recall per managed scene resource recorded for the
        # session's scene (usually exactly one: the resolved isolation
        # group), with an outcome for every Hue member fixture.
        hue_members = [
            (fixtures_by_id.get(e.fixture_id), e)
            for e in session.fixture_executions
            if e.execution == "native_scene" and fixtures_by_id.get(e.fixture_id) is not None
        ]
        for group_rid in group_rids:
            resource_id = self._managed.resource_id(session.scene_id, group_rid)
            detail = f"{action} via managed-scene recall action={recall_action} ({fidelity_note})"
            if not resource_id:
                one = {"ok": False, "provider": "hue_v2", "op": "hue.recall_scene",
                       "detail": "no persisted real bridge scene resource id for managed Hue lifecycle"}
            else:
                representative = hue_members[0][0] if hue_members else None
                operation = ProviderOperation(
                    provider="hue_v2", op="hue.recall_scene", resource_ref=resource_id,
                    payload={"recall": {"action": recall_action}}, description=detail,
                )
                one = self._execute_operation(operation, representative, operation.resource_ref)
                outcome.receipts.append(one)
            for fixture, _previous in hue_members:
                outcome.fixture_executions.append(FixtureExecution(
                    fixture.id, "hue_v2", "native_scene",
                    "approximate" if one["ok"] else "unsupported",
                    one["ok"], detail if one["ok"] else f"{action} failed: {one['detail']}"
                ))
                if not one["ok"]:
                    outcome.failures.append((fixture.id, one))
        return outcome

    # -- wled freeze ------------------------------------------------------------

    def _wled_freeze(self, session: PlaybackSession, registry: FixtureRegistry, *, freeze: bool, verb: str) -> PlaybackOutcome:
        """Batched per-controller frz realization for a session's WLED fixtures."""
        outcome = PlaybackOutcome([], [], [], 0)
        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}

        controllers: dict[str, dict[int, Fixture]] = {}
        for execution in session.fixture_executions:
            fixture = fixtures_by_id.get(execution.fixture_id)
            if fixture is None or fixture.binding is None:
                continue
            binding = fixture.binding
            if getattr(binding, "provider", None) != "wled":
                continue
            controller_key = f"{binding.device_id}|{binding.endpoint_hint or ''}"
            controllers.setdefault(controller_key, {})
            for seg_id in binding.segment_ids or [0]:
                controllers[controller_key][seg_id] = fixture

        for controller_key, segments in sorted(controllers.items()):
            representative = next(iter(segments.values()))
            seg_ids = sorted(segments)
            operation = ProviderOperation(
                provider="wled",
                op="wled.post_state",
                resource_ref=f"{representative.binding.device_id}:seg:{','.join(str(s) for s in seg_ids)}",
                payload={"seg": [{"id": seg_id, "frz": freeze} for seg_id in seg_ids]},
                description=f"{verb}: frz={'true' if freeze else 'false'} on {len(seg_ids)} segment(s) of {representative.binding.device_id}",
            )
            one = self._execute_operation(operation, representative, operation.resource_ref)
            outcome.receipts.append(one)
            for seg_id in seg_ids:
                fixture = segments[seg_id]
                outcome.fixture_executions.append(
                    FixtureExecution(
                        fixture_id=fixture.id,
                        provider="wled",
                        execution="native_effect",
                        fidelity="native" if one["ok"] else "unsupported",
                        ok=one["ok"],
                        detail=(
                            f"{verb} via segment frz={'true' if freeze else 'false'}"
                            + ("" if one["ok"] else f": {one['detail']}")
                        ),
                    )
                )
                if not one["ok"]:
                    outcome.failures.append((fixture.id, one))
        return outcome
