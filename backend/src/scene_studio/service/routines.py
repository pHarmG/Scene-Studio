"""RoutineService — HA-native routine awareness + constrained CRUD.

The engine's routine subsystem. Home Assistant is canonical: this service
holds only a DERIVED, TTL-bounded projection cache (never a second routine
database) and performs every mutation through the injected
:class:`~scene_studio.service.ports.HaAutomationGateway` — the HA-supported
config/state/services API. There is no YAML path by construction.

Discipline enforced here:

- **Read-first**: every mutation re-fetches HA's canonical config
  immediately before writing and compares the
  :func:`~scene_studio.domain.routines.canonical_config_digest` the editor
  started from — a mismatch is a structured conflict, never an overwrite.
- **Verify-after-write**: after create/update/delete/enable/disable the
  service re-reads HA's canonical config/state and verifies the expected
  outcome BEFORE reporting success.
- **Grammar-gated writes**: only ``native_routine`` projections are
  mutable. ``recognized_advanced`` automations stay read-only through Scene
  Studio; structural complexity is never flattened or rewritten.
- **Honest capability**: an absent/blocked gateway reports the capability
  as unavailable — it never degrades into direct YAML mutation.

The bounded TTL (default 30 s) plus explicit invalidation on HA
``automation_reloaded`` / ``automation.*`` state-change signals (wired by the
adapter) keep HA-first edits visible without trusting any single event:
``catalog(refresh=True)`` forces a re-read and every Workbench command
follows with a fresh catalog read anyway.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from ..domain.routines import (
    CLASSIFICATION_ADVANCED,
    CLASSIFICATION_NATIVE,
    RoutineProjection,
    classify_automation,
    RoutineSchedule,
    canonical_config_digest,
    describe_schedule,
    generate_routine_config,
    new_automation_id,
    validate_automation_id,
    validate_routine_behavior,
    validate_routine_time,
    validate_routine_weekdays,
)
from .ports import HaAutomationGateway, HaAutomationGatewayError

__all__ = ["RoutineService", "ROUTINE_CACHE_TTL_SECONDS"]

ROUTINE_CACHE_TTL_SECONDS = 30.0

_CONFLICT_KIND = "routine_source_changed"


def _default_monotonic() -> float:
    return time.monotonic()


class RoutineCapabilityUnavailable(RuntimeError):
    """The HA automation-editing capability is absent in this runtime."""


class RoutineSourceChanged(RuntimeError):
    """HA's canonical automation changed since the editor loaded it."""

    def __init__(self, automation_id: str, current_digest: str) -> None:
        super().__init__(
            f"automation {automation_id!r} changed in Home Assistant since it was loaded"
        )
        self.automation_id = automation_id
        self.current_digest = current_digest


class RoutineNotEditable(RuntimeError):
    """The target automation is recognized but exceeds the editable grammar."""

    def __init__(self, automation_id: str, reasons: list[str]) -> None:
        super().__init__(
            f"automation {automation_id!r} is advanced (Home Assistant managed); "
            "Scene Studio does not edit it"
        )
        self.automation_id = automation_id
        self.reasons = list(reasons)


class RoutineVerificationFailed(RuntimeError):
    """HA did not confirm the expected post-write state."""


class RoutineService:
    """Derived routine projection + constrained native-automation CRUD."""

    def __init__(
        self,
        gateway: HaAutomationGateway | None,
        clock: Callable[[], str],
        *,
        emit: Callable[..., None] | None = None,
        cache_ttl_seconds: float = ROUTINE_CACHE_TTL_SECONDS,
    ) -> None:
        self._gateway = gateway
        self._clock = clock  # ISO timestamp source
        self._emit = emit
        self._ttl = cache_ttl_seconds
        self._lock = threading.Lock()
        self._cached_routines: list[RoutineProjection] | None = None
        self._cached_at_monotonic: float | None = None
        self._cached_refreshed_at: str | None = None
        self._monotonic = _default_monotonic

    # -- events -----------------------------------------------------------

    def _event(self, level: str, summary: str, detail: str = "", **kwargs) -> None:
        if self._emit is not None:
            self._emit(level, "automation", summary, detail, **kwargs)

    # -- reads -------------------------------------------------------------

    def invalidate(self) -> None:
        """Drop the cached projection (HA signalled a change, or a mutation)."""
        with self._lock:
            self._cached_routines = None
            self._cached_at_monotonic = None

    def catalog(self, *, refresh: bool = False) -> dict:
        """The derived routine projection for the Workbench.

        Bounded TTL cache; ``refresh=True`` forces a HA re-read (the
        explicit-refresh seam; plain polling never trusts one event). The
        response is honest about capability: ``available: false`` carries
        the reason and no routines are invented.
        """
        gateway = self._gateway
        if gateway is None or not gateway.available():
            with self._lock:
                self._cached_routines = None
            reason = gateway.unavailable_reason() if gateway is not None else (
                "no Home Assistant automation gateway is configured in this runtime"
            )
            return {
                "available": False,
                "unavailable_reason": reason,
                "routines": [],
                "refreshed_at": None,
                "stale": False,
            }
        with self._lock:
            now = self._monotonic()
            cached = self._cached_routines
            fresh = (
                cached is not None
                and not refresh
                and self._cached_at_monotonic is not None
                and (now - self._cached_at_monotonic) < self._ttl
            )
            if fresh:
                return {
                    "available": True,
                    "unavailable_reason": None,
                    "routines": [routine.to_dict() for routine in cached],
                    "refreshed_at": self._cached_refreshed_at,
                    "stale": False,
                }
        # Network reads happen OUTSIDE the projection lock (same discipline
        # as the contention probe): a slow HA must never block dispatch.
        try:
            routines = self._read_all_routines(gateway)
            refreshed_at = self._clock()
        except HaAutomationGatewayError as exc:
            with self._lock:
                self._cached_routines = None
            return {
                "available": False,
                "unavailable_reason": f"Home Assistant automation read failed: {exc.message}",
                "routines": [],
                "refreshed_at": None,
                "stale": False,
            }
        with self._lock:
            self._cached_routines = routines
            self._cached_refreshed_at = refreshed_at
            self._cached_at_monotonic = self._monotonic()
            return {
                "available": True,
                "unavailable_reason": None,
                "routines": [routine.to_dict() for routine in routines],
                "refreshed_at": refreshed_at,
                "stale": False,
            }

    def _read_all_routines(self, gateway: HaAutomationGateway) -> list[RoutineProjection]:
        """Enumerate HA automations (entities -> per-id config) and project
        only those that reference the Scene Studio bridge."""
        entities = gateway.list_automation_entities()
        routines: list[RoutineProjection] = []
        config_failures = 0
        for entity in entities:
            if entity.automation_id is None:
                continue  # legacy automation without a config id: not API-addressable
            try:
                config = gateway.get_automation_config(entity.automation_id)
            except HaAutomationGatewayError:
                config_failures += 1
                continue
            if config is None:
                continue  # config vanished between the two reads; next refresh settles it
            projection = classify_automation(
                config,
                automation_id=entity.automation_id,
                entity_id=entity.entity_id,
                entity_state=entity.state,
                alias=entity.alias,
            )
            if projection is not None:
                routines.append(projection)
        if config_failures and not routines and entities:
            raise HaAutomationGatewayError(
                "invalid_response",
                f"none of {config_failures} automation config(s) could be read",
            )
        routines.sort(key=lambda routine: (routine.scene_id or "", routine.schedule.time if routine.schedule else "", routine.alias))
        return routines

    # -- mutation helpers ---------------------------------------------------

    def _require_gateway(self) -> HaAutomationGateway:
        gateway = self._gateway
        if gateway is None or not gateway.available():
            reason = gateway.unavailable_reason() if gateway is not None else None
            raise RoutineCapabilityUnavailable(reason or "Home Assistant automation management is unavailable")
        return gateway

    def _recheck_concurrency(self, gateway: HaAutomationGateway, automation_id: str, source_digest: str) -> dict:
        """Re-fetch the canonical config immediately before a write and
        verify the editor's digest still matches. Returns the fresh config.
        The automation id is validated here — every mutating command's id
        flows into a gateway URL path, so it must satisfy the safe charset
        before any transport is touched."""
        validate_automation_id(automation_id)
        try:
            config = gateway.get_automation_config(automation_id)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"could not re-read automation before write: {exc.message}") from exc
        if config is None:
            raise RoutineSourceChanged(automation_id, "")
        current = canonical_config_digest(config)
        if current != source_digest:
            raise RoutineSourceChanged(automation_id, current)
        return config

    def _require_native(self, config: dict, automation_id: str) -> RoutineProjection:
        projection = classify_automation(config, automation_id=automation_id)
        if projection is None:
            raise RoutineNotEditable(automation_id, ["automation no longer references Scene Studio"])
        if projection.classification != CLASSIFICATION_NATIVE:
            raise RoutineNotEditable(automation_id, projection.unsupported_reasons)
        return projection

    def _require_scene(self, scene_id: str, behavior: str | None, scene_lookup: Callable[[str], tuple[str, bool] | None]) -> tuple[str, bool]:
        """Resolve scene_id -> (name, is_dynamic) through the injected lookup.

        The lookup returns None for unknown/absent scenes (-> not found),
        else ``(scene_name, motion_is_dynamic)``. ``play`` behavior requires
        a dynamic scene (static scenes cannot start playback).
        """
        resolved = scene_lookup(scene_id)
        if resolved is None:
            raise KeyError(scene_id)
        scene_name, is_dynamic = resolved
        if behavior == "play" and not is_dynamic:
            raise ValueError(
                f"scene {scene_id!r} is static (motion.mode=static); scheduled behavior "
                "must be 'apply' — dynamic play requires a dynamic scene"
            )
        return scene_name, is_dynamic

    def _entity_snapshot(self, gateway: HaAutomationGateway) -> dict:
        """``automation_id -> entity`` map from HA's entity state surface.
        A failing entity read degrades to an empty map (config-level truth
        stays usable); verification paths that NEED state re-check it."""
        try:
            entities = gateway.list_automation_entities()
        except HaAutomationGatewayError:
            return {}
        return {entity.automation_id: entity for entity in entities if entity.automation_id}

    def _verify_present(self, gateway: HaAutomationGateway, automation_id: str) -> RoutineProjection:
        try:
            config = gateway.get_automation_config(automation_id)
        except HaAutomationGatewayError as exc:
            raise RoutineVerificationFailed(f"could not re-read automation after write: {exc.message}") from exc
        if config is None:
            raise RoutineVerificationFailed(f"automation {automation_id!r} did not persist in Home Assistant")
        projection = classify_automation(config, automation_id=automation_id)
        if projection is None or projection.classification != CLASSIFICATION_NATIVE:
            raise RoutineVerificationFailed(
                f"automation {automation_id!r} did not round-trip through the routine grammar"
            )
        entity = self._entity_snapshot(gateway).get(automation_id)
        if entity is not None:
            projection.entity_id = entity.entity_id
            projection.enabled = entity.state != "off"
        return projection

    def _verify_absent(self, gateway: HaAutomationGateway, automation_id: str) -> None:
        try:
            config = gateway.get_automation_config(automation_id)
        except HaAutomationGatewayError as exc:
            raise RoutineVerificationFailed(f"could not re-read automation after delete: {exc.message}") from exc
        if config is not None:
            raise RoutineVerificationFailed(f"automation {automation_id!r} still exists in Home Assistant")

    def _verify_enabled(self, gateway: HaAutomationGateway, entity_id: str, enabled: bool) -> None:
        entities = gateway.list_automation_entities()
        state = next((entity.state for entity in entities if entity.entity_id == entity_id), None)
        if state is None:
            raise RoutineVerificationFailed(f"automation entity {entity_id!r} was not found after the state change")
        expected = "on" if enabled else "off"
        if state != expected:
            raise RoutineVerificationFailed(
                f"automation entity {entity_id!r} reports {state!r}, expected {expected!r}"
            )

    def _reload(self, gateway: HaAutomationGateway) -> str | None:
        """Best-effort automation reload so state/config stay in step.
        Returns a warning string when the reload could not be confirmed
        (the stored config is still canonical; HA applies it on next
        reload/restart)."""
        try:
            gateway.reload_automations()
            return None
        except HaAutomationGatewayError as exc:
            return f"HA automation reload could not be confirmed: {exc.message}"

    def _finish(self, projection: RoutineProjection, warning: str | None = None) -> dict:
        data = {"routine": projection.to_dict()}
        if warning:
            data["warning"] = warning
        return data

    # -- mutations -----------------------------------------------------------

    def create(
        self,
        *,
        scene_id: str,
        behavior: str,
        time_hhmm: str,
        weekdays,
        scene_lookup: Callable[[str], tuple[str, bool] | None],
    ) -> dict:
        """Create one native HA automation (``routine.create``)."""
        gateway = self._require_gateway()
        behavior = validate_routine_behavior(behavior, "params.behavior")
        time_value = validate_routine_time(time_hhmm, "params.time")
        weekday_tuple = validate_routine_weekdays(weekdays, "params.weekdays")
        scene_name, _ = self._require_scene(scene_id, behavior, scene_lookup)
        schedule = RoutineSchedule(time=time_value, weekdays=weekday_tuple)

        automation_id = self._allocate_id(gateway)
        config = generate_routine_config(
            automation_id=automation_id,
            scene_id=scene_id,
            scene_name=scene_name,
            behavior=behavior,
            schedule=schedule,
        )
        try:
            gateway.save_automation_config(automation_id, config)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"HA refused the automation create: {exc.message}") from exc
        warning = self._reload(gateway)
        try:
            projection = self._verify_present(gateway, automation_id)
        except RoutineVerificationFailed:
            # Roll the partial create back: never leave an unverified write behind.
            try:
                gateway.delete_automation_config(automation_id)
            except HaAutomationGatewayError:
                pass
            raise
        self.invalidate()
        self._event(
            "info",
            f"Scheduled {scene_name} · {describe_schedule(schedule)}",
            detail=f"created HA automation {automation_id} ({behavior} {scene_id})",
            scene_id=scene_id,
        )
        return self._finish(projection, warning)

    def _allocate_id(self, gateway: HaAutomationGateway) -> str:
        """Generate a unique stable automation id (config-absence check)."""
        import secrets

        for _ in range(8):
            candidate = new_automation_id(secrets.token_hex(6))
            try:
                existing = gateway.get_automation_config(candidate)
            except HaAutomationGatewayError as exc:
                raise RoutineCapabilityUnavailable(f"could not check id availability: {exc.message}") from exc
            if existing is None:
                return candidate
        raise RoutineCapabilityUnavailable("could not allocate a unique automation id")

    def update(
        self,
        *,
        automation_id: str,
        source_digest: str,
        time_hhmm: str | None = None,
        weekdays=...,
        behavior: str | None = None,
        scene_id: str | None = None,
        scene_lookup: Callable[[str], tuple[str, bool] | None],
    ) -> dict:
        """Update one native routine (``routine.update``) with optimistic
        concurrency on ``source_digest``. Only supported-grammar fields are
        regenerated; the automation must still classify as native."""
        gateway = self._require_gateway()
        config = self._recheck_concurrency(gateway, automation_id, source_digest)
        current = self._require_native(config, automation_id)

        new_time = validate_routine_time(time_hhmm, "params.time") if time_hhmm is not None else current.schedule.time
        new_weekdays = (
            validate_routine_weekdays(weekdays, "params.weekdays")
            if weekdays is not ...
            else current.schedule.weekdays
        )
        new_behavior = validate_routine_behavior(behavior, "params.behavior") if behavior is not None else current.behavior
        new_scene_id = scene_id if scene_id is not None else current.scene_id
        if new_scene_id is None:
            raise RoutineNotEditable(automation_id, ["automation does not reference a Scene Studio scene"])

        scene_name, _ = self._require_scene(new_scene_id, new_behavior, scene_lookup)
        schedule = RoutineSchedule(time=new_time, weekdays=new_weekdays)
        replacement = generate_routine_config(
            automation_id=automation_id,
            scene_id=new_scene_id,
            scene_name=scene_name,
            behavior=new_behavior,
            schedule=schedule,
        )
        # The edit regenerates the managed fields (alias/description/trigger/
        # condition/action) but must never silently drop HA-level settings
        # the grammar tolerates without modeling (user-set in the HA UI).
        if isinstance(config.get("initial_state"), bool):
            replacement["initial_state"] = config["initial_state"]
        if isinstance(config.get("icon"), str) and config["icon"].strip():
            replacement["icon"] = config["icon"]
        try:
            gateway.save_automation_config(automation_id, replacement)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"HA refused the automation update: {exc.message}") from exc
        warning = self._reload(gateway)
        projection = self._verify_present(gateway, automation_id)
        self._verify_round_trip(projection, schedule, new_behavior, new_scene_id)
        self.invalidate()
        self._event(
            "info",
            f"Scheduled {scene_name} · {describe_schedule(schedule)} updated",
            detail=f"updated HA automation {automation_id} ({new_behavior} {new_scene_id})",
            scene_id=new_scene_id,
        )
        return self._finish(projection, warning)

    def _verify_round_trip(
        self, projection: RoutineProjection, schedule: RoutineSchedule, behavior: str, scene_id: str
    ) -> None:
        """The re-read definition must match the intent exactly."""
        if projection.schedule != schedule or projection.behavior != behavior or projection.scene_id != scene_id:
            raise RoutineVerificationFailed(
                f"automation {projection.automation_id!r} did not round-trip the requested change"
            )

    def adopt(
        self,
        *,
        automation_id: str,
        source_digest: str,
        scene_lookup: Callable[[str], tuple[str, bool] | None],
    ) -> dict:
        """Adopt one recognized legacy automation (``routine.adopt``).

        The target must classify as an ``adoptable`` advanced projection —
        the recognized ``script.scene_studio_apply`` wrapper era with a
        readable whole-minute schedule. Adoption modernizes the automation
        into the SAME canonical form ``routine.create`` generates (bridge
        event action, Scene Studio alias + provenance description, singular
        storage keys) while keeping the automation id and the discovered
        schedule, and preserving HA-level extras the grammar tolerates
        (icon/initial_state). Same discipline as every mutation:
        read-first/source_digest concurrency, verify-after-write, reload.
        """
        gateway = self._require_gateway()
        config = self._recheck_concurrency(gateway, automation_id, source_digest)
        current = classify_automation(config, automation_id=automation_id)
        if current is None:
            raise RoutineNotEditable(automation_id, ["automation does not reference Scene Studio"])
        if current.adoptable is False:
            if current.classification == CLASSIFICATION_NATIVE:
                raise RoutineNotEditable(automation_id, ["automation is already a native routine"])
            raise RoutineNotEditable(
                automation_id,
                current.unsupported_reasons or ["automation is not a recognized legacy Scene Studio automation"],
            )
        if current.schedule is None or current.behavior != "apply" or not current.scene_id:
            raise RoutineNotEditable(
                automation_id,
                ["automation does not expose a readable whole-minute schedule; it cannot be adopted"],
            )
        scene_name, _ = self._require_scene(current.scene_id, "apply", scene_lookup)
        replacement = generate_routine_config(
            automation_id=automation_id,
            scene_id=current.scene_id,
            scene_name=scene_name,
            behavior="apply",
            schedule=current.schedule,
        )
        # HA-level settings the grammar tolerates without modeling survive
        # adoption, exactly like a native-routine update (update()).
        if isinstance(config.get("initial_state"), bool):
            replacement["initial_state"] = config["initial_state"]
        if isinstance(config.get("icon"), str) and config["icon"].strip():
            replacement["icon"] = config["icon"]
        try:
            gateway.save_automation_config(automation_id, replacement)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"HA refused the automation adopt: {exc.message}") from exc
        warning = self._reload(gateway)
        projection = self._verify_present(gateway, automation_id)
        self._verify_round_trip(projection, current.schedule, "apply", current.scene_id)
        self.invalidate()
        self._event(
            "info",
            f"Adopted '{current.alias}' · {describe_schedule(current.schedule)}",
            detail=(
                f"adopted legacy automation {automation_id} into the native routine grammar "
                f"(apply {current.scene_id})"
            ),
            scene_id=current.scene_id,
        )
        return self._finish(projection, warning)

    def delete(self, *, automation_id: str, source_digest: str) -> dict:
        """Delete one native routine (``routine.delete``) with concurrency
        check. Advanced automations are never deletable from Scene Studio."""
        gateway = self._require_gateway()
        config = self._recheck_concurrency(gateway, automation_id, source_digest)
        current = self._require_native(config, automation_id)
        try:
            gateway.delete_automation_config(automation_id)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"HA refused the automation delete: {exc.message}") from exc
        warning = self._reload(gateway)
        self._verify_absent(gateway, automation_id)
        self.invalidate()
        self._event(
            "info",
            "Scheduled scene routine removed",
            detail=f"deleted HA automation {automation_id}",
            scene_id=current.scene_id,
        )
        return {"removed": True}

    def set_enabled(
        self,
        *,
        automation_id: str,
        source_digest: str,
        enabled: bool,
    ) -> dict:
        """Enable/disable one native routine (``routine.enable``/``disable``)."""
        gateway = self._require_gateway()
        config = self._recheck_concurrency(gateway, automation_id, source_digest)
        projection = self._require_native(config, automation_id)
        entity = self._entity_snapshot(gateway).get(automation_id)
        entity_id = entity.entity_id if entity is not None else projection.entity_id
        if not entity_id:
            raise RoutineCapabilityUnavailable(
                f"automation {automation_id!r} has no HA entity to switch; refresh routines and retry"
            )
        try:
            gateway.set_automation_enabled(entity_id, enabled)
        except HaAutomationGatewayError as exc:
            raise RoutineCapabilityUnavailable(f"HA refused the automation state change: {exc.message}") from exc
        self._verify_enabled(gateway, entity_id, enabled)
        self.invalidate()
        self._event(
            "info",
            f"Routine '{projection.alias}' {'enabled' if enabled else 'disabled'}",
            detail=f"automation {automation_id} set to {'on' if enabled else 'off'}",
            scene_id=projection.scene_id,
        )
        # Verify-after-write for a toggle is the entity state read-back above
        # (the stored config is untouched by turn_on/turn_off, so the config
        # digest and the rest of the projection stand as re-checked).
        projection.entity_id = entity_id
        projection.enabled = enabled
        return self._finish(projection)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------