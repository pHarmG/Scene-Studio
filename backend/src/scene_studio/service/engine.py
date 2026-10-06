"""SceneStudioEngine — pure command service (integration wave 1, plan §9.1).

Coordinates scene lookup, target resolution, renderer planning, provider
execution through the :class:`~scene_studio.service.ports.ProviderExecutor`
port, persistence, structured events, and result summaries. The engine is
stdlib-only and dependency-free: no AppDaemon, no network, no wall clock
(see ``ports.py``).

Public surface
--------------

- ``handle(envelope_dict) -> dict`` — canonical entry point. Parses a raw
  command envelope via ``domain.commands.parse_command``, dispatches per
  ``COMMAND_CATALOG``, and returns the JSON-shaped ``CommandResult`` dict
  (``{"command", "ok", "request_id"?, "data"?, "error"?}``). ``handle``
  NEVER raises: every failure becomes a ``CommandResult`` with the proper
  ``ErrorCode`` (store ``NotFoundError`` -> ``not_found``, ``ConflictError``
  -> ``conflict``, domain ``ValidationError`` -> ``validation_error``,
  anything unexpected -> ``internal_error`` with a sanitized message).

- ``handle_result(envelope_dict) -> CommandResult`` — the typed variant of
  the same path. Both exist because the HTTP layer serializes dicts while
  in-process callers (tests, the adapter's legacy sequencing) want the
  object; ``handle`` is the documented public API, ``handle_result`` is the
  typed convenience and does not duplicate logic.

- ``handle_legacy(mapped) -> list[dict]`` — executes a legacy mapper output
  (see ``service/legacy.py``): persists the generated scene first when the
  mapping carries one, then runs the envelope sequence in order.

- ``status() -> dict`` — engine health snapshot powering the Workbench
  ``getStatus`` polling (plan §9.4). STABLE KEYS (do not rename)::

      {
        "product":       {"name", "build": {"version", "source_sha", "short_sha",
                          "channel", "dirty", "tag", "source_tree_sha256", "built_at"},
                          "update": {"state", "message", "release_url"}},
        "engine":        {"ok", "revision", "event_capacity", "events"},
        "fixtures":      {"total", "ready", "missing", "disabled",
                          "unbound", "degraded", "conflicting"},
        "providers":     {provider: {"total", "ready", "missing",
                                     "degraded", "other"}},
        "current":       {"scene_id", "target_id"} | None,
        "playback":      {"scene_id", "target_id", "started_at",
                          "revision", "paused"} | None,
        "last_discovery": {"run_id", "started_at", "finished_at",
                           "summary"} | None,
        "runtime":       {"mode": "normal"|"read_only"|"registry_admin"|"r2_validation"|"r5_validation",
                          "read_only": bool,
                          "provider_writes_blocked": bool,
                          "allowed_commands": [str, ...]},
      }

- ``recent_events(n)`` — bounded event deque, newest first.
- ``emit(...)`` — OperationalEvent helper; payloads pass through
  ``sanitize_tree`` before storage (defense in depth).

Revision semantics: ``revision`` is a monotonic int bumped once after every
successful state mutation (apply without dry-run, ``scene.play_draft``,
rename, archive, restore, save/upsert, create, update, playback
start/pause/stop, fixture enable/disable/rebind/reconcile, fixture.adopt,
target.create/update, registry migrate, discovery run). Read-only paths
(``scene.preview``,
``scene.preview_draft``, dry-run apply, ``diagnostics.export``,
``fixture.rebind_preview``, ``fixture.reconcile_preview``,
``registry.migration_preview``, ``status``, ``recent_events``) never bump it.

Documented wave-1 decisions:

- **Apply is best-effort across fixtures** (contracts §3.4: skip, never
  error). The result is ``ok=true`` whenever the plan was produced and the
  executor pass completed without an engine/store failure; per-operation
  failures surface in ``data["receipts"]`` (``ok: false`` entries) and as
  per-fixture error events. A fully-failed apply therefore still reads
  ``ok=true`` — callers must inspect receipts (this is the documented,
  deliberate choice; a strict mode can be added later without breaking
  the envelope).
- **`transition_ms`** is accepted and echoed in results/events, but wave-1
  renderers plan transitions from scene ``fixture_states`` only; the param
  is not injected into provider payloads.
- **`scene.save`** persists an EMPTY v2 draft (real provider capture is a
  later wave). Without an explicit ``target_id`` the draft targets all
  declared registry targets; with none declared it fails ``validation_error``.
- **`playback.pause`** performs the engine state transition (provider-native pause ops land in R5B);
  pausing native provider animation is deployment work (R1).
"""

from __future__ import annotations

import threading
from pathlib import Path
from ..build_info import get_build_info
from ..updates import check_updates, unchecked
from collections import deque
from dataclasses import dataclass

from ..domain.bindings import (
    KNOWN_PROVIDERS,
    HaLightBinding,
    HueBinding,
    WledBinding,
)
from ..domain.commands import (
    COMMAND_CATALOG,
    CommandEnvelope,
    CommandResult,
    ErrorCode,
    failure,
    parse_command,
    success,
)
from ..domain.contention import (
    CONTENTION_DEFAULT_POLICY,
    HyperHdrInstance,
    parse_contention_override,
    resolve_contention_policy,
    split_plan_by_contention,
)
from ..domain.discovery import DiscoveryObservation, DiscoveryReport
from ..domain.events import EventCategory, EventLevel, OperationalEvent
from ..domain.fidelity import FidelityLevel, ProviderOperation, RenderPlan
from ..domain.live_state import LiveStateSnapshot
from ..domain.fixtures import (
    BROAD_GROUP_ID,
    CapabilityAssessment, CapabilityStatus, Fixture, FixtureRegistry,
    HealthStatus, assess_capability_parity, derive_health,
)
from ..domain.palette_resolve import canonicalize_static_palette
from ..domain.reconcile import proposed_reconcile_revision
from ..domain.identities import normalize_name_to_id, validate_id
from ..domain.routines import HaAutomationIdError
from ..domain.playback import (
    FixtureExecution,
    PlaybackSession,
    PlaybackSessionState,
    PlaybackState,
    new_session_id,
)
from ..domain.sanitize import sanitize_string, sanitize_tree
from ..domain.scenes import MotionMode, Scene
from ..domain.serde import ValidationError, join, reject_unknown_keys, require_bool, require_int
from .contention import ContentionService
from .playback_realization import HueManagedSceneStore, PlaybackRealizer
from .policy import RuntimePolicy
from .ports import Clock, DiscoveryFetchers, HaAutomationGateway, ProviderExecutor, receipt
from .routines import (
    RoutineCapabilityUnavailable,
    RoutineNotEditable,
    RoutineService,
    RoutineSourceChanged,
    RoutineVerificationFailed,
)
from ..discovery import (
    build_halight_observations,
    build_hue_observations,
    build_wled_observations,
    observation_matches_binding,
    run_discovery,
)
from ..discovery.aggregates import is_aggregate_observation
from ..stores import ConflictError, NotFoundError, SceneStudioStore, StoreError
from .ports import Clock, DiscoveryFetchers, ProviderExecutor, receipt

__all__ = ["SceneStudioEngine"]

_PROVIDER_LABELS = {"hue_v2": "Hue", "wled": "WLED", "ha_light": "HA lights"}

# The one broad, every-fixture catch-all group, preserved untouched by a
# proposed reconcile group swap (see `_proposed_groups`) — matching the
# convention the Workbench itself already uses (its Scope control and
# room-grouping both special-case this exact id).
_BROAD_GROUP = BROAD_GROUP_ID

# Scene metadata keys the Builder does not own. ONE list, two uses:
# - `scene.update` re-attaches them from the stored document, so an ordinary
#   editor payload can never erase migration provenance, archival
#   bookkeeping, or duplicate provenance;
# - `scene.create` REFUSES them from a payload: a brand-new document must
#   never claim to be archived, v1-migrated, or a duplicate unless the engine
#   itself derived that (see the explicit `duplicate_of` param).
_SERVER_OWNED_METADATA_KEYS = ("archived_at", "migrated_from_v1", "duplicated_from")


class _UnknownCommandError(Exception):
    """Internal marker: envelope names a command outside COMMAND_CATALOG."""


class SceneStudioEngine:
    """Pure command service over a :class:`SceneStudioStore` + ports."""

    def __init__(
        self,
        store: SceneStudioStore,
        executor: ProviderExecutor,
        clock: Clock,
        *,
        event_capacity: int = 500,
        discovery_fetchers: DiscoveryFetchers | None = None,
        policy: RuntimePolicy | None = None,
        hyperhdr_probe=None,
        contention_config: dict | None = None,
        automation_gateway: HaAutomationGateway | None = None,
    ) -> None:
        self._store = store
        self._policy = policy or RuntimePolicy.build()
        self._executor = executor
        self._clock = clock
        self._fetchers = discovery_fetchers or DiscoveryFetchers()
        self._events: deque[OperationalEvent] = deque(maxlen=max(1, int(event_capacity)))
        self._revision = 0
        self._current_scene_id: str | None = None
        self._current_target_id: str | None = None
        self._playback_path = Path(store.root) / "playback_state.json"
        self._playback_state = self._load_playback_state()
        # Orphaning runs AFTER the field exists so the explicit persist below
        # can never dereference an unassigned attribute (R5A corrective pass).
        self._orphan_interrupted_sessions()
        # R5B: provider-native playback realization (Hue managed scenes +
        # per-light dynamics, WLED frz lifecycle). Static scene.apply keeps
        # its existing renderer/executor path.
        self._realizer = PlaybackRealizer(
            executor,
            HueManagedSceneStore(Path(store.root) / "provider_state" / "hue_scenes.json"),
            clock.now_iso,
        )
        # External light-sync contention (hyperHDR ownership pass). When no
        # probe is configured the contention layer reports
        # ``configured: false`` and every gate is a pass-through — existing
        # deployments behave exactly as before.
        contention_config = contention_config or {}
        self._contention = ContentionService(
            probe=hyperhdr_probe,
            clock=clock,
            store_root=store.root,
            default_policy=contention_config.get("default_policy", CONTENTION_DEFAULT_POLICY),
            ttl_seconds=contention_config.get("ttl_seconds", 5.0),
            wled_instance_ids=contention_config.get("wled_instance_ids"),
            hue_instance_ids=contention_config.get("hue_instance_ids"),
        )
        self._last_discovery: DiscoveryReport | None = None
        self._discovery_seq = 0
        self._lock = threading.RLock()
        # Live fixture color-state sampling (plan: live fixture color-state
        # pass) is a read-only side channel, deliberately independent of
        # ``self._lock``: a slow provider read must never block command
        # dispatch, and command dispatch must never block a live sample.
        self._live_sample_lock = threading.Lock()
        self._last_live_snapshot: LiveStateSnapshot | None = None
        # In-memory Scene documents for unsaved ``scene.play_draft``
        # playback sessions so pause/stop can still realize providers
        # without writing the draft into the catalog.
        self._ephemeral_scenes: dict[str, Scene] = {}
        # HA-native routine awareness (routines pass): derived projection +
        # constrained native-automation CRUD through the injected gateway.
        # A None gateway reports the capability honestly unavailable; the
        # projection cache is TTL-bounded, never a second routine database.
        self._routines = RoutineService(
            automation_gateway,
            clock.now_iso,
            emit=self.emit,
        )
        self._handlers = {
            "scene.apply": self._apply,
            "scene.preview": self._apply,  # forced dry-run inside
            "scene.rename": self._rename,
            "scene.archive": self._archive,
            "scene.restore": self._restore,
            "scene.save": self._save,
            "scene.preview_draft": self._preview_draft,
            "scene.play_draft": self._play_draft,
            "scene.create": self._create_scene,
            "scene.update": self._update_scene,
            "playback.start": self._playback_start,
            "playback.pause": self._playback_pause,
            "playback.resume": self._playback_resume,
            "playback.stop": self._playback_stop,
            "fixture.enable": self._fixture_enable,
            "fixture.disable": self._fixture_disable,
            "fixture.set_contention_policy": self._fixture_set_contention_policy,
            "sync.suspend": self._sync_suspend,
            "sync.resume": self._sync_resume,
            "fixture.retry": self._fixture_retry,
            "fixture.identify": self._fixture_identify,
            "fixture.rebind_preview": self._fixture_rebind_preview,
            "fixture.rebind": self._fixture_rebind,
            "fixture.rebind_rollback": self._fixture_rebind_rollback,
            "fixture.reconcile_preview": self._fixture_reconcile_preview,
            "fixture.reconcile": self._fixture_reconcile,
            "fixture.adopt": self._fixture_adopt,
            "target.create": self._target_create,
            "target.update": self._target_update,
            "registry.migration_preview": self._registry_migration_preview,
            "registry.migrate": self._registry_migrate,
            "discovery.run": self._discovery_run,
            "diagnostics.export": self._diagnostics_export,
            "routine.create": self._routine_create,
            "routine.update": self._routine_update,
            "routine.delete": self._routine_delete,
            "routine.enable": self._routine_enable,
            "routine.disable": self._routine_disable,
        }

    # ------------------------------------------------------------------
    # public entry points
    # ------------------------------------------------------------------

    def handle(self, envelope_dict: dict) -> dict:
        """Parse + dispatch a raw envelope; returns the result as a dict. Never raises."""
        return self.handle_result(envelope_dict).to_dict()


    # -- playback session collection (R5A) --------------------------------

    def _load_playback_state(self) -> PlaybackState:
        """Pure load: read persisted sessions; corrupt file -> fresh state.

        Performs NO mutation and NO persistence (the ``_playback_state``
        field does not exist yet during ``__init__``). Interrupted-session
        orphaning is a separate explicit step.
        """
        state = PlaybackState()
        if self._playback_path.exists():
            try:
                state = PlaybackState.deserialize(self._playback_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.emit(
                    EventLevel.WARNING,
                    EventCategory.PLAYBACK,
                    "Playback state file was corrupt; starting with no sessions",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                state = PlaybackState()
        return state

    def _orphan_interrupted_sessions(self) -> int:
        """Mark persisted active/paused sessions ``orphaned`` after a restart.

        The provider may still be animating, so the stale records are
        surfaced (needs-attention) rather than silently dropped; ownership
        is released because nothing is driving them from this engine.
        Persists the orphaned states. Returns the number orphaned.
        """
        orphaned = [
            session for session in self._playback_state.sessions.values()
            if session.state in (PlaybackSessionState.ACTIVE, PlaybackSessionState.PAUSED)
        ]
        for session in orphaned:
            session.state = PlaybackSessionState.ORPHANED
            session.paused_at = None
            self.emit(
                EventLevel.WARNING,
                EventCategory.PLAYBACK,
                f"Playback session '{session.scene_name}' was interrupted by restart",
                detail=(
                    "The provider may still be animating; use playback.stop on the "
                    "session or start/apply to the same fixtures to supersede it"
                ),
                scene_id=session.scene_id,
            )
        if orphaned:
            self._persist_playback_state()
        return len(orphaned)

    def _persist_playback_state(self) -> None:
        from ..stores.atomic import atomic_write_json

        atomic_write_json(self._playback_path, self._playback_state.to_dict())

    def handle_result(self, envelope_dict: dict) -> CommandResult:
        """Typed variant of :meth:`handle` returning a ``CommandResult``."""
        with self._lock:
            request_id = None
            try:
                if not isinstance(envelope_dict, dict):
                    raise ValidationError("command", "expected a JSON object envelope")
                command = envelope_dict.get("command")
                if not isinstance(command, str) or command not in COMMAND_CATALOG:
                    raise _UnknownCommandError(command)
                envelope = CommandEnvelope.from_dict(envelope_dict)
                request_id = envelope.request_id
                # Runtime policy gate (backend-owned, before param validation:
                # a malformed mutating command is still a mode rejection).
                if not self._policy.allows(
                    envelope.command, dry_run=envelope_dict.get("dry_run") is True
                ):
                    result = self._failure_event(
                        envelope.command,
                        ErrorCode.CONFLICT,
                        f"{self._policy.mode} mode: '{envelope.command}' is not permitted; "
                        "allowed commands: " + ", ".join(sorted(self._policy.allowed_commands)),
                        request_id,
                    )
                else:
                    _, params = parse_command(envelope_dict)
                    result = self._handlers[envelope.command](envelope, params)
            except _UnknownCommandError as exc:
                result = self._failure_event(
                    "unknown_command",
                    ErrorCode.UNKNOWN_COMMAND,
                    f"unknown command {str(exc)!r}; known commands: {', '.join(sorted(COMMAND_CATALOG))}",
                    request_id,
                )
            except NotFoundError as exc:
                result = self._failure_event(envelope_dict.get("command"), ErrorCode.NOT_FOUND, str(exc), request_id)
            except ConflictError as exc:
                result = self._failure_event(envelope_dict.get("command"), ErrorCode.CONFLICT, str(exc), request_id)
            except ValidationError as exc:
                result = self._failure_event(
                    envelope_dict.get("command") if isinstance(envelope_dict, dict) else None,
                    ErrorCode.VALIDATION_ERROR,
                    str(exc),
                    request_id,
                    details={"path": exc.path},
                )
            except StoreError as exc:  # CorruptStoreError and friends
                result = self._failure_event(
                    envelope_dict.get("command") if isinstance(envelope_dict, dict) else None,
                    ErrorCode.INTERNAL_ERROR,
                    sanitize_string(str(exc))[:512],
                    request_id,
                )
            except Exception as exc:  # never leak exceptions out of handle()
                result = self._failure_event(
                    envelope_dict.get("command") if isinstance(envelope_dict, dict) else None,
                    ErrorCode.INTERNAL_ERROR,
                    sanitize_string(f"{type(exc).__name__}: {exc}")[:512],
                    request_id,
                )
            return result

    def handle_legacy(self, mapped: dict) -> list[dict]:
        """Execute a legacy mapper output (``service/legacy.py``) in order.

        When the mapping carries a full ``scene`` dict (the generate flow),
        the leading ``scene.save`` envelope is fulfilled by
        :meth:`upsert_scene` (the frozen catalog has no full-content save
        command — see module notes), then the remaining envelopes run
        through :meth:`handle`. Returns the result dicts in order.
        """
        with self._lock:
            results: list[dict] = []
            scene_dict = mapped.get("scene") if isinstance(mapped, dict) else None
            commands = mapped.get("commands", []) if isinstance(mapped, dict) else []
            for envelope in commands:
                if scene_dict is not None and isinstance(envelope, dict) and envelope.get("command") == "scene.save":
                    # The upsert substitution must not bypass the runtime
                    # policy: it stands in for a scene.save, so it obeys the
                    # same permission check (rejected in restricted modes).
                    if not self._policy.allows("scene.save"):
                        results.append(
                            {
                                "command": "scene.save",
                                "ok": False,
                                "error": {
                                    "code": "conflict",
                                    "message": f"{self._policy.mode} mode: 'scene.save' is not permitted",
                                },
                            }
                        )
                        scene_dict = None
                        continue
                    results.append(self.upsert_scene(scene_dict).to_dict())
                    scene_dict = None
                    continue
                results.append(self.handle_result(envelope).to_dict())
            return results

    # ------------------------------------------------------------------
    # reads (status / events / catalogs)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Engine health snapshot — STABLE KEYS documented in the module docstring."""
        # Contention probe refresh runs BEFORE the mutation lock (TTL-guarded,
        # its own lock, one probe in flight): status polling is what drives
        # the heartbeat in probe-configured runtimes, and a slow hyperHDR
        # must never block command dispatch.
        if self._contention is not None:
            self._contention.refresh_if_stale()
        with self._lock:
            fixtures = self._store.fixtures.list_fixtures()
            counts = {status.value: 0 for status in HealthStatus}
            providers: dict[str, dict[str, int]] = {
                provider: {"total": 0, "ready": 0, "missing": 0, "degraded": 0, "other": 0}
                for provider in KNOWN_PROVIDERS
            }
            for fixture in fixtures:
                health = derive_health(fixture)
                counts[health.value] += 1
                if fixture.binding is not None:
                    bucket = providers.setdefault(
                        fixture.binding.provider,
                        {"total": 0, "ready": 0, "missing": 0, "degraded": 0, "other": 0},
                    )
                    bucket["total"] += 1
                    if health is HealthStatus.READY:
                        bucket["ready"] += 1
                    elif health is HealthStatus.MISSING:
                        bucket["missing"] += 1
                    elif health is HealthStatus.DEGRADED:
                        bucket["degraded"] += 1
                    else:
                        bucket["other"] += 1
            last_discovery = None
            if self._last_discovery is not None:
                last_discovery = {
                    "run_id": self._last_discovery.run_id,
                    "started_at": self._last_discovery.started_at,
                    "finished_at": self._last_discovery.finished_at,
                    "summary": dict(self._last_discovery.summary),
                }
            return {
                "product": {"name": "Scene Studio", "build": getattr(self, "_product_build", None) or get_build_info(),
                            "update": getattr(self, "_product_update", None) or unchecked()},
                "engine": {
                    "ok": True,
                    "revision": self._revision,
                    "event_capacity": self._events.maxlen,
                    "events": len(self._events),
                },
                "runtime": self._policy.status_view(),
                "fixtures": {
                    "total": len(fixtures),
                    "ready": counts["ready"],
                    "missing": counts["missing"],
                    "disabled": counts["disabled"],
                    "unbound": counts["unbound"],
                    "degraded": counts["degraded"],
                    "conflicting": counts["conflicting"],
                },
                "providers": providers,
                "current": (
                    {"scene_id": self._current_scene_id, "target_id": self._current_target_id}
                    if self._current_scene_id is not None
                    else None
                ),
                "playback": self._playback_state.status_view(),
                "contention": self._contention.status_view(),
                "last_discovery": last_discovery,
            }

    def check_updates(self) -> None:
        # Explicit request only; network I/O stays outside the command lock.
        build = getattr(self, "_product_build", None) or get_build_info()
        update = check_updates(build["version"])
        with self._lock:
            self._product_build = build
            self._product_update = update

    def recent_events(self, n: int = 50) -> list[OperationalEvent]:
        """Up to ``n`` most recent events, newest first."""
        if n <= 0:
            return []
        items = list(self._events)
        return list(reversed(items[-n:]))

    def fixtures_catalog(self) -> dict:
        """Registry fixtures + targets as dicts (HTTP read surface).

        Each fixture carries the derived contention view (``held`` + the
        external owner label) from the cached probe/live-state evidence —
        no network on this path (a ``status`` poll refreshes the cache)."""
        with self._lock:
            registry = self._store.fixtures.registry()
            holds = self._contention.held_ids_cached(registry, self._last_live_snapshot)
            fixture_dicts = []
            for fixture in registry.fixtures:
                entry = fixture.to_dict()
                if fixture.id in holds:
                    entry["contention"] = {"held": True, "owner": holds[fixture.id]}
                else:
                    entry["contention"] = {"held": False, "owner": None}
                fixture_dicts.append(entry)
            return {
                "fixtures": fixture_dicts,
                "targets": [target.to_dict() for target in registry.targets],
            }

    def fixture_registry(self) -> FixtureRegistry:
        """Detached copy of the canonical fixture registry."""
        with self._lock:
            return self._store.fixtures.registry()

    def scenes_catalog(self, *, archived: bool = False) -> dict:
        """Active (or archived) scene catalog as dicts (HTTP read surface)."""
        with self._lock:
            scenes = self._store.scenes.list_archived() if archived else self._store.scenes.list_scenes()
            return {"scenes": [scene.to_dict() for scene in scenes]}

    def latest_discovery(self) -> DiscoveryReport | None:
        """The most recent discovery report, if any discovery.run happened."""
        with self._lock:
            return self._last_discovery

    def sample_live_state(self) -> dict:
        """Read-only current-color sample across every bound provider
        (plan: live fixture color-state pass). Never mutates the registry,
        never bumps ``engine.revision``, never emits an operational event —
        a provider failure is represented in the response, not thrown or
        logged (this runs on a ~3 s Workbench cadence; event-log spam here
        would drown out real operational events).

        The mutation lock is held only long enough to snapshot the fixture
        list; provider network reads happen unlocked so a slow sample can
        never block command dispatch, and a command can never block a
        sample. If a sample is already in flight, this returns the last
        committed snapshot immediately rather than queue behind it or fire
        a second concurrent round of provider reads (plan §3: "the
        Workbench must not build up a queue of stale provider reads").
        """
        # Local import: `..live_state` imports `service.ports`, and importing
        # any `scene_studio.service.*` submodule runs this package's own
        # `__init__.py` (which imports this module) first — a module-level
        # import here would be circular. Deferring it until first call, once
        # `scene_studio.service` is already fully imported, breaks the cycle
        # (same pattern as `_persist_playback_state`'s local import above).
        from ..live_state import sample_live_state

        if not self._live_sample_lock.acquire(blocking=False):
            cached = self._last_live_snapshot
            return cached.to_dict() if cached is not None else LiveStateSnapshot(observed_at=self._clock.now_iso()).to_dict()
        try:
            with self._lock:
                fixtures = self._store.fixtures.list_fixtures()
            snapshot = sample_live_state(fixtures, self._fetchers, self._clock.now_iso())
            self._last_live_snapshot = snapshot
            return snapshot.to_dict()
        finally:
            self._live_sample_lock.release()

    def fixture_ids_for_target(self, target_id: str) -> list[str]:
        """Fixture ids resolving to ``target_id`` ([] when unknown) — helper
        for adapters feeding legacy mappers (generate_office_scene)."""
        try:
            return [fixture.id for fixture in self._store.resolve_target(target_id)]
        except (NotFoundError, ValidationError, ValueError):
            return []

    # -- HA-native routine awareness (routines pass) -----------------------

    def routines_catalog(self, *, refresh: bool = False) -> dict:
        """Derived HA routine projection for the read route (GET /routines).

        Bounded-TTL cache over HA's canonical automations; ``refresh=True``
        forces a re-read. Never mutates HA, never holds the mutation lock
        (HA reads run through the gateway outside it), and honestly reports
        ``available: false`` when no automation gateway is configured.
        """
        return self._routines.catalog(refresh=refresh)

    def invalidate_routines(self) -> None:
        """Drop the derived routine cache (HA signalled an automation
        change via the adapter's reload/state listeners). The next catalog
        read re-projects from HA."""
        self._routines.invalidate()

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def emit(
        self,
        level: EventLevel | str,
        category: EventCategory | str,
        summary: str,
        detail: str = "",
        *,
        scene_id: str | None = None,
        fixture_id: str | None = None,
        provider: str | None = None,
        data: dict | None = None,
    ) -> OperationalEvent:
        """Record an OperationalEvent (``data`` sanitized before storage)."""
        payload = sanitize_tree(data or {})
        event = OperationalEvent(
            timestamp=self._clock.now_iso(),
            level=EventLevel(level),
            category=EventCategory(category),
            summary=str(summary)[:512],
            detail=str(detail)[:1024],
            scene_id=scene_id,
            fixture_id=fixture_id,
            provider=provider,
            data=payload,
        )
        self._events.append(event)
        return event

    # ------------------------------------------------------------------
    # command handlers
    # ------------------------------------------------------------------

    def _apply(self, envelope: CommandEnvelope, params) -> CommandResult:
        preview = envelope.command == "scene.preview"
        dry_run = True if preview else bool(params.dry_run)
        scene = self._require_scene(params.scene_id)
        return self._apply_scene(
            envelope,
            scene,
            target_id=params.target_id,
            dry_run=dry_run,
            transition_ms=params.transition_ms,
            preview=preview,
            contention_override=getattr(params, "contention_override", None),
        )

    def _apply_scene(
        self,
        envelope: CommandEnvelope,
        scene: Scene,
        *,
        target_id: str | None,
        dry_run: bool,
        transition_ms: int | None,
        preview: bool = False,
        contention_override: str | None = None,
    ) -> CommandResult:
        if target_id is not None:
            self._store.resolve_target(target_id)  # unknown explicit target -> not_found
        registry = self._store.fixtures.registry()
        target_ids = [target_id] if target_id is not None else None
        plan = self._build_plan(scene, registry, target_ids)
        if dry_run:
            label = "previewed" if preview else "planned (dry run)"
            self.emit(
                EventLevel.INFO,
                EventCategory.SCENE,
                f"{scene.name} {label}",
                detail=self._plan_detail(plan, scene),
                scene_id=scene.id,
                provider=None,
                data={"render_plan": plan.to_dict()},
            )
            return success(
                envelope.command,
                {"dry_run": True, "render_plan": plan.to_dict(), "transition_ms": transition_ms},
                envelope.request_id,
            )

        # External light-sync contention gate (hyperHDR pass): held fixtures
        # yield (default), take over, or pass through per resolved policy.
        # Preview/dry-run paths above stay pure observations.
        plan, contention_view = self._contention_gate(
            envelope, scene, plan, registry, contention_override
        )
        if contention_view.get("refused"):
            return failure(
                envelope.command,
                ErrorCode.CONTENDED,
                contention_view["refused_detail"],
                details={"contention": contention_view},
                request_id=envelope.request_id,
            )

        planned_fixture_ids = [fp.fixture_id for fp in plan.fixture_plans]
        preempted_sessions = self._stop_overlapping_sessions(
            planned_fixture_ids, reason="superseded by scene.apply"
        )
        superseded_orphans = self._supersede_orphans(
            planned_fixture_ids, superseded_by="scene.apply"
        )
        executions = self._execute_plan(plan, registry)
        self._current_scene_id = scene.id
        self._current_target_id = target_id
        self._touch()
        for fixture_id, failed in executions.failures:
            self.emit(
                EventLevel.ERROR,
                EventCategory.SCENE,
                f"{scene.name}: fixture '{fixture_id}' failed",
                detail=failed["detail"],
                scene_id=scene.id,
                fixture_id=fixture_id,
                provider=failed.get("provider"),
                data={"op": failed.get("op"), "resource_ref": failed.get("resource_ref")},
            )
        yielded_fixture_ids = contention_view.get("yielded_fixture_ids") or []
        if yielded_fixture_ids:
            # Surrender auto-restore: remember the interrupted look so the
            # engine re-asserts it on the yielded fixtures when hyperHDR
            # goes idle (user decision: static scenes auto-restore).
            self._contention.record_pending_restore({
                "scene_id": scene.id,
                "fixture_ids": sorted(yielded_fixture_ids),
                "target_ids": list(plan.target_ids),
                "recorded_at": self._clock.now_iso(),
            })
        else:
            self._contention.record_pending_restore(None)
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{scene.name} started",
            detail=self._plan_detail(plan, scene),
            scene_id=scene.id,
            data={
                "target_ids": plan.target_ids,
                "skipped_fixture_ids": plan.skipped_fixture_ids,
                "ops_executed": executions.ok_count,
                "ops_failed": len(executions.failures),
                "preempted_session_ids": preempted_sessions,
                "superseded_orphan_count": superseded_orphans,
                "transition_ms": transition_ms,
                "contention": contention_view or None,
            },
        )
        return success(
            envelope.command,
            {
                "scene_id": scene.id,
                "scene_name": scene.name,
                "target_ids": plan.target_ids,
                "skipped_fixture_ids": plan.skipped_fixture_ids,
                "fixtures_planned": len(plan.fixture_plans),
                "ops_executed": executions.ok_count,
                "ops_failed": len(executions.failures),
                "receipts": executions.receipts,
                "preempted_session_ids": preempted_sessions,
                "superseded_orphan_count": superseded_orphans,
                "transition_ms": transition_ms,
                "contention": contention_view,
            },
            envelope.request_id,
        )

    def _rename(self, envelope: CommandEnvelope, params) -> CommandResult:
        scene = self._store.scenes.rename_scene(params.scene_id, params.name)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"Scene renamed to {scene.name}",
            detail=f"id {scene.id} (ids are immutable)",
            scene_id=scene.id,
        )
        return success(envelope.command, {"scene": scene.to_dict()}, envelope.request_id)

    def _archive(self, envelope: CommandEnvelope, params) -> CommandResult:
        # Stop active provider-native sessions while the source scene is still
        # available. Archiving the model alone must never leave an animation
        # running at the provider.
        active_scene = self._require_scene(params.scene_id)
        for playback_session in self._playback_state.sessions.values():
            if playback_session.scene_id == active_scene.id and playback_session.state in (
                PlaybackSessionState.ACTIVE,
                PlaybackSessionState.PAUSED,
            ):
                outcome = self._realizer.realize_stop(
                    playback_session, active_scene, self._store.fixtures.registry()
                )
                playback_session.fixture_executions = self._merge_lifecycle_executions(playback_session, outcome)
                self._emit_playback_failures(active_scene, outcome)
                playback_session.state = PlaybackSessionState.STOPPED
                playback_session.stopped_at = self._clock.now_iso()
                playback_session.stop_reason = "scene archived"
            elif playback_session.scene_id == active_scene.id and playback_session.state == PlaybackSessionState.HELD:
                # Held by the external owner: bookkeeping only, no provider writes.
                playback_session.state = PlaybackSessionState.STOPPED
                playback_session.stopped_at = self._clock.now_iso()
                playback_session.stop_reason = "scene archived (was held by external owner)"
                playback_session.held_by = None
        scene = self._store.scenes.archive(params.scene_id)
        for playback_session in self._playback_state.sessions.values():
            if playback_session.scene_id == scene.id and playback_session.state in (
                PlaybackSessionState.ACTIVE,
                PlaybackSessionState.PAUSED,
            ):
                # Defensive no-op for sessions added by a future code path.
                playback_session.state = PlaybackSessionState.STOPPED
                playback_session.stopped_at = self._clock.now_iso()
                playback_session.stop_reason = "scene archived"
        if any(
            s.stop_reason == "scene archived" and s.scene_id == scene.id
            for s in self._playback_state.sessions.values()
        ):
            self._playback_state.trim_stopped()
            self._persist_playback_state()
        if self._current_scene_id == scene.id:
            self._current_scene_id = None
            self._current_target_id = None
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{scene.name} archived",
            detail="moved to the scene archive; restore to re-activate",
            scene_id=scene.id,
        )
        return success(envelope.command, {"scene": scene.to_dict()}, envelope.request_id)

    def _restore(self, envelope: CommandEnvelope, params) -> CommandResult:
        scene = self._store.scenes.restore(params.scene_id)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{scene.name} restored",
            detail="returned to the active scene catalog",
            scene_id=scene.id,
        )
        return success(envelope.command, {"scene": scene.to_dict()}, envelope.request_id)

    def _save(self, envelope: CommandEnvelope, params) -> CommandResult:
        target_ids = self._save_target_ids(params.target_id)
        scene_id = normalize_name_to_id(params.name)
        draft = {
            "schema_version": 2,
            "id": scene_id,
            "name": params.name,
            "target_ids": target_ids,
            "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
        }
        stored = self._store.scenes.add_scene(draft)
        self._touch()
        note = "empty draft persisted; live provider capture arrives in a later wave"
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{stored.name} saved as empty draft",
            detail=f"id {stored.id}; {note}",
            scene_id=stored.id,
        )
        return success(envelope.command, {"scene": stored.to_dict(), "note": note}, envelope.request_id)

    def _save_target_ids(self, target_id: str | None) -> list[str]:
        if target_id is not None:
            self._store.resolve_target(target_id)  # explicit unknown target -> not_found
            return [target_id]
        declared = sorted(target.id for target in self._store.fixtures.list_targets())
        if not declared:
            raise ValidationError(
                "params.target_id",
                "scene.save requires target_id when the registry declares no targets",
            )
        return declared

    # -- scene authoring (Builder Pass 2) ----------------------------------

    def _authoring_document(self, scene_payload: dict, *, scene_id: str | None = None) -> dict:
        """Normalize a Builder scene payload into a full Scene v2 document dict.

        One canonical identity rule (Pass 2 plan §4.1): a missing/empty id is
        derived from the display name via the existing
        ``normalize_name_to_id`` helper — the same helper migration/seed
        tooling uses — so the frontend never re-implements slug rules. For
        updates ``scene_id`` is authoritative and is injected when the
        payload omits it; an explicitly conflicting payload id is rejected
        by the caller before this point.
        """
        doc = dict(scene_payload)
        raw_id = doc.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            name = doc.get("name")
            if isinstance(name, str) and name.strip():
                doc["id"] = normalize_name_to_id(name)
            # else: Scene.from_dict raises the precise name/id validation error
        if scene_id is not None:
            doc["id"] = scene_id
        return doc

    def _require_known_targets(self, scene: Scene) -> None:
        """Authoring gate: every declared target must resolve against the
        registry (a declared Target id or a single fixture id) so obvious
        unknown-target mistakes fail at save time instead of silently
        producing all-skip render plans."""
        registry = self._store.fixtures.registry()
        target_ids = {target.id for target in registry.targets}
        fixture_ids = {fixture.id for fixture in registry.fixtures}
        for index, target_id in enumerate(scene.target_ids):
            if target_id not in target_ids and target_id not in fixture_ids:
                raise ValidationError(
                    join("scene", f"target_ids.{index}"),
                    f"unknown target id {target_id!r}: not a declared target or fixture",
                )

    def _preview_draft(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Validate + render an UNSAVED authoring draft (Builder Pass 2).

        Non-persistent and non-writing by construction: the draft goes
        through ``Scene.from_dict`` and the exact ``_build_plan`` path used
        by stored-scene preview/apply, and nothing else. No event and no
        revision bump — debounced auto-preview must not flood the bounded
        event log. Unknown target ids surface as render-plan notes (the
        save-time ``_require_known_targets`` gate is stricter).
        """
        scene = Scene.from_dict(self._authoring_document(params.scene), "scene")
        registry = self._store.fixtures.registry()
        plan = self._build_plan(scene, registry, None)
        return success(
            envelope.command,
            {"dry_run": True, "scene": scene.to_dict(), "render_plan": plan.to_dict()},
            envelope.request_id,
        )

    def _play_draft(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Play an unsaved authoring draft on fixtures without persisting it.

        Static drafts take the apply executor path; dynamic drafts start
        playback against an in-memory Scene so pause/stop can still realize
        providers. Unknown targets fail at this gate (same as save). The
        catalog is never written.
        """
        scene = Scene.from_dict(self._authoring_document(params.scene), "scene")
        if not scene.target_ids:
            raise ValidationError(join("scene", "target_ids"), "at least one target is required to play a draft")
        self._require_known_targets(scene)
        if scene.motion.mode is MotionMode.STATIC:
            result = self._apply_scene(
                envelope, scene, target_id=None, dry_run=False, transition_ms=None
            )
            kind = "apply"
        else:
            result = self._playback_start_scene(envelope, scene, target_id=None, ephemeral=True)
            kind = "playback"
        data = dict(result.data or {})
        registry = self._store.fixtures.registry()
        plan = self._build_plan(scene, registry, None)
        data["dry_run"] = False
        data["played"] = True
        data["kind"] = kind
        data["scene"] = scene.to_dict()
        data["render_plan"] = plan.to_dict()
        return success(envelope.command, data, envelope.request_id)

    def _create_scene(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Atomically persist a NEW canonical Scene v2 document (Builder Pass 2).

        Never applies or plays the scene as a side effect; conflicts against
        active AND archived ids come from ``SceneStore.add_scene``.

        Duplicate/Save-as-New (Builder-expansion plan §4): the payload may
        name ``duplicate_of`` (an existing active or archived scene id). The
        engine — never the frontend — records that provenance as
        ``metadata.duplicated_from`` on the new document, so a duplicated
        migrated scene does not falsely claim to BE the migrated artifact.
        """
        scene = Scene.from_dict(self._authoring_document(params.scene), "scene")
        self._require_known_targets(scene)
        for key in _SERVER_OWNED_METADATA_KEYS:
            scene.metadata.pop(key, None)
        if params.duplicate_of is not None:
            source = self._store.scenes.get_scene(params.duplicate_of, include_archived=True)
            scene.metadata["duplicated_from"] = source.id
        stored = self._store.scenes.add_scene(scene)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{stored.name} created",
            detail=(
                f"id {stored.id}; duplicated from {params.duplicate_of} in the Scene Builder"
                if params.duplicate_of is not None
                else f"id {stored.id}; authored in the Scene Builder"
            ),
            scene_id=stored.id,
        )
        return success(envelope.command, {"scene": stored.to_dict()}, envelope.request_id)

    def _update_scene(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Atomically replace the editable intent of an ACTIVE scene (Builder Pass 2).

        The scene id is immutable; server/history metadata the Builder does
        not own is re-attached from the stored document. Live playback is
        deliberately left alone: the running session keeps its already
        realized provider state, and the next Apply/Play uses the new
        definition (documented Pass 2 behavior; lifecycle re-reads the
        stored document).
        """
        payload_id = params.scene.get("id")
        if isinstance(payload_id, str) and payload_id.strip() and payload_id != params.scene_id:
            raise ValidationError(
                join("params", "scene.id"),
                f"scene id is immutable: payload carries {payload_id!r}, "
                f"but the update targets {params.scene_id!r}",
            )
        existing = self._store.scenes.get_scene(params.scene_id, include_archived=True)
        if existing.metadata.get("archived_at") is not None:
            raise ConflictError(f"scene {params.scene_id!r} is archived; restore it before editing")
        scene = Scene.from_dict(self._authoring_document(params.scene, scene_id=params.scene_id), "scene")
        self._require_known_targets(scene)
        for key in _SERVER_OWNED_METADATA_KEYS:
            if key in existing.metadata:
                scene.metadata[key] = existing.metadata[key]
        stored = self._store.scenes.replace_scene(params.scene_id, scene)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SCENE,
            f"{stored.name} updated",
            detail=f"id {stored.id} (immutable); definition replaced by the Scene Builder",
            scene_id=stored.id,
        )
        return success(envelope.command, {"scene": stored.to_dict()}, envelope.request_id)

    def upsert_scene(self, scene_dict: dict) -> CommandResult:
        """Persist a full scene document: add, or update ``fixture_states`` in place.

        Fulfills the "scene-save-or-update" step of the legacy
        ``generate_office_scene`` mapping. The frozen catalog has no
        full-content save command (reported gap): for an EXISTING active
        scene only ``fixture_states`` can be refreshed through the store's
        public API, so ``palette``/``motion``/``name`` of an existing scene
        are intentionally left untouched (deterministic regeneration still
        updates the visible fixture colors) — EXCEPT that a degenerate
        stored palette (empty, or fewer than 2 distinct hexes, e.g. a
        migration artifact) is still rebuilt from the refreshed fixture
        colors via ``canonicalize_static_palette``, so refreshing a scene
        through this path can only heal a broken palette, never overwrite
        a deliberately curated one.
        """
        with self._lock:
            try:
                scene_id = scene_dict.get("id") if isinstance(scene_dict, dict) else None
                try:
                    existing = self._store.scenes.get_scene(scene_id) if scene_id else None
                except NotFoundError:
                    existing = None
                if existing is None:
                    scene = canonicalize_static_palette(Scene.from_dict(scene_dict, "scene"))
                    stored = self._store.scenes.add_scene(scene)
                    note = "scene created"
                else:
                    stored = self._store.scenes.update_fixture_states(
                        scene_id, scene_dict.get("fixture_states", {})
                    )
                    canonical = canonicalize_static_palette(stored)
                    if canonical is not stored:
                        stored = self._store.scenes.replace_scene(scene_id, canonical)
                    note = "existing scene updated (fixture_states replaced)"
            except ValidationError as exc:
                return failure("scene.save", ErrorCode.VALIDATION_ERROR, str(exc), details={"path": exc.path})
            except ConflictError as exc:
                return failure("scene.save", ErrorCode.CONFLICT, str(exc))
            except StoreError as exc:
                return failure("scene.save", ErrorCode.INTERNAL_ERROR, sanitize_string(str(exc))[:512])
            self._touch()
            self.emit(
                EventLevel.INFO,
                EventCategory.SCENE,
                f"{stored.name} saved",
                detail=f"id {stored.id}; {note}",
                scene_id=stored.id,
            )
            return success("scene.save", {"scene": stored.to_dict(), "note": note})

    # -- playback ---------------------------------------------------------

    def _playback_start(self, envelope: CommandEnvelope, params) -> CommandResult:
        scene = self._require_scene(params.scene_id)
        return self._playback_start_scene(
            envelope, scene, target_id=params.target_id, ephemeral=False,
            contention_override=getattr(params, "contention_override", None),
        )

    def _playback_start_scene(
        self,
        envelope: CommandEnvelope,
        scene: Scene,
        *,
        target_id: str | None,
        ephemeral: bool,
        contention_override: str | None = None,
    ) -> CommandResult:
        if scene.motion.mode is MotionMode.STATIC:
            raise ConflictError(
                f"scene '{scene.id}' has no motion (mode=static); use scene.apply for static scenes"
            )
        if target_id is not None:
            self._store.resolve_target(target_id)
        registry = self._store.fixtures.registry()
        target_ids = [target_id] if target_id is not None else None
        plan = self._build_plan(scene, registry, target_ids)

        # External light-sync contention gate: identical semantics to the
        # static apply (yield by default / takeover / ignore), applied
        # before any preemption or provider realization so a held start
        # never touches the externally-owned fixtures.
        plan, contention_view = self._contention_gate(
            envelope, scene, plan, registry, contention_override
        )
        if contention_view.get("refused"):
            return failure(
                envelope.command,
                ErrorCode.CONTENDED,
                contention_view["refused_detail"],
                details={"contention": contention_view},
                request_id=envelope.request_id,
            )

        session_id = new_session_id(self._clock.now_iso())
        owned = [fp.fixture_id for fp in plan.fixture_plans]

        # Preemption: every live session owning >=1 requested fixture stops in
        # full (newest session wins); disjoint sessions continue untouched.
        preempted = self._stop_overlapping_sessions(
            owned, preempted_by=session_id, reason="preempted by newer playback"
        )

        # Orphan supersession: retaking fixtures an orphaned session still
        # references resolves that stale record explicitly.
        self._supersede_orphans(owned, superseded_by=session_id)

        outcome = self._realizer.realize_start(scene, plan, registry)
        fixture_executions = outcome.fixture_executions

        session = PlaybackSession(
            session_id=session_id,
            scene_id=scene.id,
            scene_name=scene.name,
            target_ids=list(plan.target_ids),
            fixture_ids=list(owned),
            state=PlaybackSessionState.ACTIVE,
            started_at=self._clock.now_iso(),
            fixture_executions=fixture_executions,
        )
        self._playback_state.add(session)
        if ephemeral:
            self._ephemeral_scenes[session_id] = scene
        self._persist_playback_state()
        self._touch()

        for fixture_id, failed in outcome.failures:
            self.emit(
                EventLevel.ERROR,
                EventCategory.PLAYBACK,
                f"{scene.name} playback: fixture '{fixture_id}' failed",
                detail=failed.get("detail", ""),
                scene_id=scene.id,
                fixture_id=fixture_id,
                provider=failed.get("provider"),
                data={"op": failed.get("op")},
            )
        native = sum(1 for fp in plan.fixture_plans if fp.fidelity is FidelityLevel.NATIVE)
        approximate = sum(1 for fp in plan.fixture_plans if fp.fidelity is FidelityLevel.APPROXIMATE)
        self.emit(
            EventLevel.INFO,
            EventCategory.PLAYBACK,
            f"{scene.name} playback started",
            detail=(
                f"{len(plan.fixture_plans)} fixtures - native {native} / approximate {approximate}"
                + (f" (preempted {len(preempted)} session(s))" if preempted else "")
            ),
            scene_id=scene.id,
            data={
                "session_id": session_id,
                "target_ids": plan.target_ids,
                "skipped_fixture_ids": plan.skipped_fixture_ids,
                "native_fixtures": native,
                "approximate_fixtures": approximate,
                "ops_executed": outcome.ok_count,
                "ops_failed": len(outcome.failures),
                "preempted_session_ids": preempted,
                "contention": contention_view or None,
            },
        )
        return success(
            envelope.command,
            {
                "session_id": session_id,
                "playback": session.to_dict(),
                "target_ids": plan.target_ids,
                "skipped_fixture_ids": plan.skipped_fixture_ids,
                "native_fixtures": native,
                "approximate_fixtures": approximate,
                "receipts": outcome.receipts,
                "preempted_session_ids": preempted,
                "contention": contention_view,
            },
            envelope.request_id,
        )

    def _playback_pause(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._playback_transition(envelope, params, to=PlaybackSessionState.PAUSED)

    def _playback_resume(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._playback_transition(envelope, params, to=PlaybackSessionState.ACTIVE)

    def _require_live_session(self, session_id: str) -> PlaybackSession:
        session = self._playback_state.get(session_id)
        if session is None or session.state == PlaybackSessionState.STOPPED:
            raise ConflictError(f"no active/paused playback session {session_id!r}; use playback.start")
        return session


    def _scene_for_session(self, session, scene_id: str | None = None) -> Scene | None:
        """Resolve the Scene for a playback session.

        Unsaved ``scene.play_draft`` sessions keep an in-memory copy so
        pause/stop can still realize providers without a catalog document.
        """
        ephemeral = self._ephemeral_scenes.get(session.session_id)
        if ephemeral is not None:
            return ephemeral
        lookup_id = scene_id or session.scene_id
        try:
            return self._store.scenes.get_scene(lookup_id)
        except Exception:
            return None

    def _realize_session_lifecycle(self, session, *, scene_id: str, action: str):
        """Provider-native realization for pause/resume/stop. A missing scene
        document degrades to an honest no-realization outcome rather than
        destroying the session."""
        scene = self._scene_for_session(session, scene_id)
        if scene is None:
            from .playback_realization import PlaybackOutcome

            return PlaybackOutcome([], [], [], 0)
        registry = self._store.fixtures.registry()
        if action == "paused":
            return self._realizer.realize_pause(session, scene, registry)
        if action == "resumed":
            return self._realizer.realize_resume(session, scene, registry)
        return self._realizer.realize_stop(session, scene, registry)

    @staticmethod
    def _merge_lifecycle_executions(session: PlaybackSession, outcome) -> list[FixtureExecution]:
        """Lifecycle mutations cover only native provider work; preserve the
        complete start realization for fixtures that require no mutation."""
        previous = {execution.fixture_id: execution for execution in session.fixture_executions}
        changed = {execution.fixture_id: execution for execution in outcome.fixture_executions}
        return [changed.get(fixture_id, previous[fixture_id]) for fixture_id in session.fixture_ids if fixture_id in previous or fixture_id in changed]

    def _emit_playback_failures(self, scene: Scene, outcome) -> None:
        for fixture_id, failed in outcome.failures:
            self.emit(
                EventLevel.ERROR, EventCategory.PLAYBACK,
                f"{scene.name} playback: fixture '{fixture_id}' lifecycle failed",
                detail=failed.get("detail", ""), scene_id=scene.id, fixture_id=fixture_id,
                provider=failed.get("provider"), data={"op": failed.get("op")},
            )

    def _playback_transition(self, envelope: CommandEnvelope, params, *, to: PlaybackSessionState) -> CommandResult:
        session = self._require_live_session(params.session_id)
        if session.state == PlaybackSessionState.ORPHANED:
            raise ConflictError(
                f"session {session.session_id!r} is orphaned (interrupted by restart); "
                "use playback.stop or start/apply to the same fixtures"
            )
        if session.state == PlaybackSessionState.HELD:
            # An external realtime owner (hyperHDR) holds the fixtures: any
            # provider write here would fight it. Resume only with the
            # explicit one-shot takeover; pause is meaningless while held.
            override = getattr(params, "contention_override", None)
            if to is PlaybackSessionState.ACTIVE and override == "takeover":
                registry = self._store.fixtures.registry()
                suspension = self._suspend_instances_for_fixtures(session.fixture_ids, registry)
                failed_ids = suspension["remaining_held_fixture_ids"]
                if failed_ids:
                    return failure(
                        envelope.command,
                        ErrorCode.CONTENDED,
                        "takeover failed: " + suspension["failure_detail"],
                        details={"contention": {"suspension": suspension}},
                        request_id=envelope.request_id,
                    )
                session.held_by = None
                # fall through to the normal resume path (frz=false / recall)
            else:
                return failure(
                    envelope.command,
                    ErrorCode.CONTENDED,
                    f"session {session.session_id!r} is held by an external light-sync owner"
                    f" ({session.held_by or 'hyperHDR'}); it auto-resumes to 'paused' when the"
                    " owner surrenders, or use contention_override=takeover",
                    details={"held_by": session.held_by},
                    request_id=envelope.request_id,
                )
        if to is PlaybackSessionState.ACTIVE and session.state == PlaybackSessionState.ACTIVE:
            raise ConflictError(
                f"session {session.session_id!r} is already active; resume applies to paused sessions"
            )
        if to is PlaybackSessionState.PAUSED and session.state == PlaybackSessionState.PAUSED:
            # pause stays idempotent per the R5 v2 contract
            return success(
                envelope.command,
                {"session_id": session.session_id, "playback": session.to_dict()},
                envelope.request_id,
            )
        if to is PlaybackSessionState.PAUSED and session.state == PlaybackSessionState.HELD:
            # Pausing a held session would write frz/recall against the
            # external owner's stream — never fight it (documented hold rule).
            return failure(
                envelope.command,
                ErrorCode.CONTENDED,
                f"session {session.session_id!r} is held by an external light-sync owner"
                f" ({session.held_by or 'hyperHDR'}); pause is unavailable while held",
                details={"held_by": session.held_by},
                request_id=envelope.request_id,
            )
        session.state = to
        if to is PlaybackSessionState.PAUSED:
            session.paused_at = self._clock.now_iso()
        else:
            session.paused_at = None
        action_label = "resumed" if to is PlaybackSessionState.ACTIVE else "paused"
        outcome = self._realize_session_lifecycle(session, scene_id=session.scene_id, action=action_label)
        session.fixture_executions = self._merge_lifecycle_executions(session, outcome)
        source_scene = self._scene_for_session(session)
        if source_scene is not None:
            self._emit_playback_failures(source_scene, outcome)
        self._persist_playback_state()
        self._touch()
        verb = "paused" if to is PlaybackSessionState.PAUSED else "resumed"
        self.emit(
            EventLevel.INFO,
            EventCategory.PLAYBACK,
            f"Playback session '{session.scene_name}' {verb}",
            detail=f"session {session.session_id} {verb}",
            scene_id=session.scene_id,
        )
        return success(
            envelope.command,
            {"session_id": session.session_id, "playback": session.to_dict()},
            envelope.request_id,
        )

    def _playback_stop(self, envelope: CommandEnvelope, params) -> CommandResult:
        session = self._require_live_session(params.session_id)
        held = session.state == PlaybackSessionState.HELD
        session.state = PlaybackSessionState.STOPPED
        session.stopped_at = self._clock.now_iso()
        session.stop_reason = (
            "released while held by external light-sync owner" if held else "stopped by request"
        )
        if held:
            # The external owner owns the provider state; a stop realization
            # (frz / static recall) would fight it. Bookkeeping only.
            session.held_by = None
        else:
            outcome = self._realize_session_lifecycle(session, scene_id=session.scene_id, action="stopped")
            session.fixture_executions = self._merge_lifecycle_executions(session, outcome)
            source_scene = self._scene_for_session(session)
            if source_scene is not None:
                self._emit_playback_failures(source_scene, outcome)
        self._ephemeral_scenes.pop(session.session_id, None)
        self._playback_state.trim_stopped()
        self._persist_playback_state()
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.PLAYBACK,
            f"Playback session '{session.scene_name}' stopped",
            detail=f"session {session.session_id} terminated; fixtures released",
            scene_id=session.scene_id,
        )
        return success(
            envelope.command,
            {"session_id": session.session_id, "stopped": True},
            envelope.request_id,
        )

    def _stop_overlapping_sessions(
        self, fixture_ids: list[str], *, reason: str, preempted_by: str | None = None
    ) -> list[str]:
        """Stop (in full) every live session owning any of the fixtures.
        Disjoint sessions continue. Returns the stopped session ids."""
        stopped_ids: list[str] = []
        for session in self._playback_state.owning_sessions_for(fixture_ids):
            session.state = PlaybackSessionState.STOPPED
            session.stopped_at = self._clock.now_iso()
            session.stop_reason = reason
            if preempted_by:
                session.preempted_by = preempted_by
            stopped_ids.append(session.session_id)
            self._ephemeral_scenes.pop(session.session_id, None)
            self.emit(
                EventLevel.INFO,
                EventCategory.PLAYBACK,
                f"Playback session '{session.scene_name}' stopped",
                detail=f"session {session.session_id}: {reason}",
                scene_id=session.scene_id,
            )
        # HELD sessions own nothing, but a fresh command retaking their
        # fixtures resolves the stale record (bookkeeping only — the
        # external owner still owns the provider state).
        for session in self._playback_state.sessions.values():
            if session.state != PlaybackSessionState.HELD:
                continue
            if any(fixture_id in session.fixture_ids for fixture_id in fixture_ids):
                session.state = PlaybackSessionState.STOPPED
                session.stopped_at = self._clock.now_iso()
                session.stop_reason = reason
                session.held_by = None
                if preempted_by:
                    session.preempted_by = preempted_by
                stopped_ids.append(session.session_id)
        if stopped_ids:
            self._playback_state.trim_stopped()
            self._persist_playback_state()
        return stopped_ids

    def _supersede_orphans(self, fixture_ids: list[str], *, superseded_by: str) -> int:
        """Resolve orphaned sessions whose stale fixtures a new command now
        retakes, so diagnostics do not accumulate misleading 'possibly still
        animating' records. Returns the number superseded."""
        superseded = 0
        for session in self._playback_state.sessions.values():
            if session.state != PlaybackSessionState.ORPHANED:
                continue
            if any(fixture_id in session.fixture_ids for fixture_id in fixture_ids):
                session.state = PlaybackSessionState.STOPPED
                session.stopped_at = self._clock.now_iso()
                session.stop_reason = "superseded"
                session.preempted_by = superseded_by
                superseded += 1
        if superseded:
            self._playback_state.trim_stopped()
            self._persist_playback_state()
        return superseded

    # -- external light-sync contention (hyperHDR ownership pass) ----------

    def _fetch_wled_states(self, registry: FixtureRegistry) -> dict | None:
        """Fresh WLED ``/json/state`` read mapped per bound device id.

        Single-controller deployment: one fetch covers every WLED-bound
        fixture (the configured fetcher is device-scoped). Returns ``None``
        when no fetcher is configured or the read fails — the WLED side of
        the gate fails open and the unavailability is reported in the
        contention view (Hue-side truth is independent of this read).
        """
        fetch = self._fetchers.fetch_wled_state
        if fetch is None:
            return None
        try:
            state = fetch()
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        devices = sorted({
            str(fixture.binding.device_id)
            for fixture in registry.fixtures
            if fixture.binding is not None and getattr(fixture.binding, "provider", None) == "wled"
        })
        return {device_id: state for device_id in devices}

    def _fetch_hue_entertainment_data(self) -> dict | None:
        """Fresh CLIP v2 entertainment-configuration + service + device
        reads used to resolve exactly which ``hue_v2`` fixtures are members
        of a currently ACTIVE Entertainment Configuration — the precise
        per-light Hue hold signal (see domain/contention.py module
        docstring). Returns ``None`` when the configuration fetcher isn't
        wired; a failed/partial read degrades to fewer or zero held
        fixtures (fail-open), same as the WLED read.
        """
        fetch_configs = self._fetchers.fetch_hue_entertainment_configurations
        if fetch_configs is None:
            return None
        try:
            configurations = fetch_configs()
        except Exception:
            configurations = None
        fetch_services = self._fetchers.fetch_hue_entertainment_services
        try:
            services = fetch_services() if fetch_services is not None else None
        except Exception:
            services = None
        try:
            devices = self._fetchers.fetch_hue_devices() if self._fetchers.fetch_hue_devices is not None else None
        except Exception:
            devices = None
        return {"configurations": configurations, "services": services, "devices": devices}

    def _instances_for_providers(self, providers: set[str], view) -> list[tuple[str, HyperHdrInstance]]:
        """Running hyperHDR instances mapped to the given Scene Studio
        providers (configured instance ids win; friendly-name matching is
        the fallback). Empty when the probe is unavailable or no instance
        is running — takeover then has nothing to suspend and honestly
        reports the fixtures as still held."""
        if view is None or not view.available:
            return []
        out: list[tuple[str, HyperHdrInstance]] = []
        for provider in sorted(providers):
            if provider == "wled":
                configured = self._contention.wled_instance_ids
                instances = (
                    [view.instance(i) for i in configured]
                    if configured is not None
                    else view.running_instances_named("wled")
                )
            elif provider == "hue_v2":
                configured = self._contention.hue_instance_ids
                instances = (
                    [view.instance(i) for i in configured]
                    if configured is not None
                    else view.running_instances_named("hue")
                )
            else:
                continue
            out.extend((provider, instance) for instance in instances if instance is not None and instance.running)
        return out

    def _suspend_instances_for_fixtures(self, fixture_ids, registry: FixtureRegistry) -> dict:
        """Stop the hyperHDR instance(s) owning ``fixture_ids`` (takeover).

        Returns per-instance receipts plus the fixtures that REMAIN held
        (their instance stop failed, no matching instance was running, or
        the WLED device still reports a live override after the stop —
        ``lor`` can persist briefly). On any successful stop a handback
        record is persisted for ``sync.resume``.
        """
        fixture_ids = sorted(set(fixture_ids))
        providers = {
            getattr(fixture.binding, "provider", None)
            for fixture in registry.fixtures
            if fixture.id in set(fixture_ids) and fixture.binding is not None
        }
        view = self._contention.cached_view()
        mapped = self._instances_for_providers(providers, view)

        instance_results: list[dict] = []
        ok_by_provider: dict[str, list[HyperHdrInstance]] = {}
        for provider, instance in mapped:
            operation = ProviderOperation(
                provider="hyperhdr",
                op="hyperhdr.stop_instance",
                resource_ref=str(instance.instance),
                payload={"instance": instance.instance},
                description=f"suspend hyperHDR instance '{instance.name}' (light-sync takeover)",
            )
            one = self._execute_operation(operation, None, f"hyperhdr:{instance.instance}")
            instance_results.append({
                "instance": instance.instance,
                "name": instance.name,
                "providers": [provider],
                "ok": one["ok"],
                "detail": one["detail"],
            })
            if one["ok"]:
                ok_by_provider.setdefault(provider, []).append(instance)

        # A provider is cleared only when at least one of its mapped
        # instances stopped successfully. WLED additionally gets an
        # immediate re-read: ``lor`` can outlive the stream for a moment.
        # Only wled/hue_v2 can ever be externally held — everything else
        # (ha_light) passes through untouched.
        remaining: list[str] = []
        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}
        for fixture_id in fixture_ids:
            fixture = fixtures_by_id.get(fixture_id)
            provider = getattr(fixture.binding, "provider", None) if fixture.binding else None
            if provider not in ("wled", "hue_v2"):
                continue
            if not ok_by_provider.get(provider):
                remaining.append(fixture_id)
        wled_recheck = None
        if any(fixtures_by_id[fid].binding is not None and getattr(fixtures_by_id[fid].binding, "provider", None) == "wled"
               for fid in fixture_ids if fid in fixtures_by_id):
            from ..domain.contention import wled_held_device_ids

            recheck = self._fetch_wled_states(registry)
            held_devices = wled_held_device_ids(recheck or {})
            wled_recheck = {
                "ok": recheck is not None,
                "still_held_device_ids": sorted(held_devices),
            }
            for fixture_id in fixture_ids:
                if fixture_id in remaining:
                    continue
                fixture = fixtures_by_id.get(fixture_id)
                if fixture is None or fixture.binding is None:
                    continue
                if getattr(fixture.binding, "provider", None) == "wled":
                    if str(fixture.binding.device_id) in held_devices:
                        remaining.append(fixture_id)

        if ok_by_provider:
            self._contention.record_handback({
                "instance_ids": sorted({
                    instance.instance for instances in ok_by_provider.values() for instance in instances
                }),
                "fixture_ids": fixture_ids,
                "recorded_at": self._clock.now_iso(),
            })
        failed_bits = [
            f"instance {result['instance']} ({result['name']}): {result['detail']}"
            for result in instance_results if not result["ok"]
        ]
        failure_detail = "; ".join(failed_bits) or (
            "no running hyperHDR instance matched the held fixtures"
            if not instance_results else ""
        )
        return {
            "instances": instance_results,
            "remaining_held_fixture_ids": sorted(remaining),
            "failure_detail": failure_detail,
            "wled_recheck": wled_recheck,
        }

    def _contention_gate(
        self, envelope: CommandEnvelope, scene: Scene, plan: RenderPlan,
        registry: FixtureRegistry, override: str | None,
    ) -> tuple[RenderPlan, dict]:
        """Partition a render plan around externally-held fixtures.

        Per held fixture the resolved policy decides: ``yield`` (default —
        the fixture leaves the executable plan and is reported), ``takeover``
        (its hyperHDR instance is suspended first; a failed suspension
        degrades to yield), ``ignore`` (proceed anyway, reported). A command
        whose ENTIRE executable set yields is refused with ``contended`` so
        callers never mistake a no-op for an apply. Preview/dry-run paths
        never reach this gate.
        """
        candidates = {
            fp.fixture_id: fp for fp in plan.fixture_plans
            if fp.provider in ("wled", "hue_v2")
        }
        view_stub = {
            "evaluated": False,
            "override": override,
        }
        if not candidates:
            return plan, view_stub
        if not self._contention.configured and self._fetchers.fetch_wled_state is None:
            # No contention source configured at all: the deployment runs
            # exactly as before the contention pass existed.
            return plan, view_stub
        wled_states = self._fetch_wled_states(registry)
        hue_entertainment_data = self._fetch_hue_entertainment_data()
        holds = self._contention.compute_holds(registry, wled_states, hue_entertainment_data)
        held = holds.held_fixture_ids & set(candidates)
        contention_view = {
            "evaluated": True,
            "default_policy": self._contention.default_policy,
            "override": override,
            "probe_available": holds.view.available,
            "streaming": holds.view.streaming,
        }
        if not held:
            return plan, contention_view

        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}
        takeover_ids: set[str] = set()
        ignored_ids: set[str] = set()
        for fixture_id in sorted(held):
            fixture = fixtures_by_id.get(fixture_id)
            policy = override or resolve_contention_policy(
                fixture, registry.targets, engine_default=self._contention.default_policy
            )
            if policy == "takeover":
                takeover_ids.add(fixture_id)
            elif policy == "ignore":
                ignored_ids.add(fixture_id)

        suspension = None
        if takeover_ids:
            suspension = self._suspend_instances_for_fixtures(takeover_ids, registry)
            for fixture_id in suspension["remaining_held_fixture_ids"]:
                takeover_ids.discard(fixture_id)  # failed takeover degrades to yield
            self.emit(
                EventLevel.WARNING,
                EventCategory.SYSTEM,
                f"{scene.name}: took over {len(takeover_ids)} held fixture(s) from external light sync",
                detail="; ".join(
                    f"instance {result['instance']} ({result['name']}): "
                    + ("stopped" if result["ok"] else f"stop failed: {result['detail']}")
                    for result in (suspension["instances"] or [])
                ) or "no matching instance was running",
                scene_id=scene.id,
                data={"suspension": suspension},
            )

        filtered, yielded, owners = split_plan_by_contention(
            plan, held, holds.owners, takeover_fixture_ids=takeover_ids | ignored_ids
        )
        contention_view.update({
            "yielded_fixture_ids": sorted(yielded),
            "owners": {fixture_id: owners.get(fixture_id, "external sync owner") for fixture_id in yielded},
            "ignored_fixture_ids": sorted(ignored_ids),
            "takeover_fixture_ids": sorted(takeover_ids),
            "suspension": suspension,
            "refused": False,
            "refused_detail": None,
        })
        if yielded:
            self.emit(
                EventLevel.WARNING,
                EventCategory.SYSTEM,
                f"{scene.name}: yielded {len(yielded)} fixture(s) to external light sync",
                detail="; ".join(
                    f"'{fixture_id}' held by {contention_view['owners'][fixture_id]}"
                    for fixture_id in contention_view["yielded_fixture_ids"]
                )[:1024],
                scene_id=scene.id,
                data={"held_fixture_ids": contention_view["yielded_fixture_ids"]},
            )
        if yielded and not filtered.fixture_plans:
            contention_view["refused"] = True
            contention_view["refused_detail"] = (
                f"every planned fixture is held by external light sync ({len(yielded)} yielded,"
                f" owners: {', '.join(sorted({owners.get(fid, 'external sync owner') for fid in yielded}))[:300]});"
                " hyperHDR holds control until it surrenders — use contention_override='takeover' to suspend it"
            )
        return filtered, contention_view

    def _apply_scene_subset(self, scene: Scene, fixture_ids, *, reason: str) -> dict:
        """Execute a scene restricted to an explicit fixture subset (surrender
        auto-restore / pre-handback re-assert). Best-effort like apply."""
        registry = self._store.fixtures.registry()
        plan = self._build_plan(scene, registry, None)
        wanted = set(fixture_ids)
        subset = RenderPlan(
            scene_id=plan.scene_id,
            target_ids=list(plan.target_ids),
            fixture_plans=[fp for fp in plan.fixture_plans if fp.fixture_id in wanted],
            skipped_fixture_ids=list(plan.skipped_fixture_ids),
            notes=[reason],
        )
        executions = self._execute_plan(subset, registry)
        return {
            "scene_id": scene.id,
            "fixture_ids": sorted(wanted),
            "fixtures_planned": len(subset.fixture_plans),
            "ops_executed": executions.ok_count,
            "ops_failed": len(executions.failures),
        }

    def refresh_contention(self) -> dict:
        """Contention heartbeat: fresh probe + WLED read, then the hold and
        surrender transitions.

        - A live session whose fixtures become externally held flips to
          ``held`` (NO provider writes — the engine never fights the
          external stream).
        - A held session whose fixtures are no longer held flips to
          ``paused`` (manually resumable; never auto-resumed).
        - A pending static-scene restore whose fixtures are no longer held
          is re-applied to exactly those fixtures (surrender auto-restore).

        Called by the adapter scheduler and implicitly through ``status``
        polling (TTL-guarded probe), so it works with or without a daemon.
        """
        registry = self._store.fixtures.registry()
        wled_states = self._fetch_wled_states(registry)
        hue_entertainment_data = self._fetch_hue_entertainment_data()
        holds = self._contention.compute_holds(registry, wled_states, hue_entertainment_data)
        held = holds.held_fixture_ids
        restored = None
        with self._lock:
            changed = False
            for session in self._playback_state.sessions.values():
                overlap_held = [fixture_id for fixture_id in session.fixture_ids if fixture_id in held]
                if session.state in (PlaybackSessionState.ACTIVE, PlaybackSessionState.PAUSED) and overlap_held:
                    session.state = PlaybackSessionState.HELD
                    session.held_by = holds.owners.get(overlap_held[0], "external light sync")
                    session.paused_at = None
                    changed = True
                    self.emit(
                        EventLevel.WARNING,
                        EventCategory.PLAYBACK,
                        f"Playback session '{session.scene_name}' superseded by external light sync",
                        detail=f"held by {session.held_by}; the engine will not fight the external "
                               "owner — session becomes resumable when it surrenders",
                        scene_id=session.scene_id,
                        data={"held_fixture_ids": overlap_held, "held_by": session.held_by},
                    )
                elif session.state == PlaybackSessionState.HELD and not overlap_held:
                    session.state = PlaybackSessionState.PAUSED
                    session.paused_at = self._clock.now_iso()
                    changed = True
                    self.emit(
                        EventLevel.INFO,
                        EventCategory.PLAYBACK,
                        f"Playback session '{session.scene_name}' resumable — external light sync surrendered",
                        detail=f"previously held by {session.held_by or 'external light sync'}; "
                               "resume manually (never auto-resumed)",
                        scene_id=session.scene_id,
                    )
            if changed:
                self._persist_playback_state()
            pending = self._contention.pending_restore()
            if pending and not any(fixture_id in held for fixture_id in pending.get("fixture_ids", [])):
                self._contention.record_pending_restore(None)
                try:
                    scene = self._store.scenes.get_scene(pending["scene_id"], include_archived=True)
                except NotFoundError:
                    scene = None
                if scene is None:
                    self.emit(
                        EventLevel.WARNING,
                        EventCategory.SCENE,
                        "Pending contention restore dropped: scene no longer exists",
                        detail=f"scene_id {pending['scene_id']!r}",
                    )
                else:
                    restored = self._apply_scene_subset(
                        scene, pending.get("fixture_ids", []),
                        reason="auto-restore after external light sync surrendered",
                    )
                    self.emit(
                        EventLevel.INFO,
                        EventCategory.SCENE,
                        f"hyperHDR surrendered — auto-restored {scene.name}",
                        detail=f"re-applied to {restored['fixtures_planned']} fixture(s); "
                               "the yielded static scene look is re-asserted",
                        scene_id=scene.id,
                        data={"restore": restored},
                    )
        view = self._contention.status_view()
        view["restored_now"] = restored
        return view

    def reconcile_wled_freezes(self) -> dict:
        """Clear stale WLED segment freezes (``frz: true``) that no live
        Scene Studio session owns.

        A paused/stopped session freezes its segments provider-side; a
        crash, restart, or hand-edit can leave that freeze behind with no
        session to clear it — and a frozen segment silently swallows BOTH
        Scene Studio renders and external light sync. Observed live on this
        deployment (seg0 frozen with no owning session). Best-effort and
        idempotent; never bumps the engine revision (provider-only fixup,
        same class as fixture.identify)."""
        if self._fetchers.fetch_wled_state is None:
            return {"ok": False, "detail": "no WLED state fetcher configured"}
        try:
            state = self._fetchers.fetch_wled_state()
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        if not isinstance(state, dict):
            return {"ok": False, "detail": "WLED state read returned no payload"}
        registry = self._store.fixtures.registry()
        ownership = self._playback_state.ownership
        segments = state.get("seg") if isinstance(state.get("seg"), list) else []
        frozen_ids = [
            entry.get("id") if isinstance(entry.get("id"), int) else index
            for index, entry in enumerate(segments)
            if isinstance(entry, dict) and entry.get("frz") is True
        ]
        if not frozen_ids:
            return {"ok": True, "cleared_segment_ids": [], "owned_segment_ids": []}
        bound: dict[int, list[str]] = {}
        owned_segment_ids: list[int] = []
        for fixture in registry.fixtures:
            if fixture.binding is None or getattr(fixture.binding, "provider", None) != "wled":
                continue
            seg_ids = list(getattr(fixture.binding, "segment_ids", None) or frozen_ids)
            for seg_id in seg_ids:
                if seg_id not in frozen_ids:
                    continue
                bound.setdefault(seg_id, []).append(fixture.id)
                if fixture.id in ownership:
                    owned_segment_ids.append(seg_id)
        clear_ids = sorted(set(seg_id for seg_id in frozen_ids if seg_id not in owned_segment_ids and seg_id in bound))
        if not clear_ids:
            return {"ok": True, "cleared_segment_ids": [], "owned_segment_ids": sorted(set(owned_segment_ids))}
        representative = next(
            (fixture for fixture in registry.fixtures
             if fixture.binding is not None and getattr(fixture.binding, "provider", None) == "wled"),
            None,
        )
        operation = ProviderOperation(
            provider="wled",
            op="wled.post_state",
            resource_ref="stale-frz-reconcile",
            payload={"seg": [{"id": seg_id, "frz": False} for seg_id in clear_ids]},
            description=f"stale-freeze reconciliation: clear frz on segments {clear_ids}",
        )
        receipt = self._execute_operation(operation, representative, "stale-frz-reconcile")
        self.emit(
            EventLevel.INFO if receipt["ok"] else EventLevel.WARNING,
            EventCategory.SYSTEM,
            "WLED stale-freeze reconciliation "
            + ("cleared " + ", ".join(str(seg_id) for seg_id in clear_ids) if receipt["ok"] else "failed"),
            detail=receipt["detail"],
            data={"cleared_segment_ids": clear_ids, "owned_segment_ids": sorted(set(owned_segment_ids))},
        )
        return {
            "ok": receipt["ok"],
            "cleared_segment_ids": clear_ids,
            "owned_segment_ids": sorted(set(owned_segment_ids)),
            "detail": receipt["detail"],
        }

    def _sync_suspend(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Explicitly suspend the external light-sync owner for a scope."""
        registry = self._store.fixtures.registry()
        if params.fixture_id is not None:
            fixture = self._store.fixtures.get_fixture(params.fixture_id)
            scope_ids = [fixture.id]
        elif params.target_id is not None:
            scope_ids = [item.id for item in self._store.resolve_target(params.target_id)]
        else:
            scope_ids = [
                fixture.id for fixture in registry.fixtures
                if fixture.binding is not None
                and getattr(fixture.binding, "provider", None) in ("wled", "hue_v2")
            ]
        if not scope_ids:
            raise ConflictError("sync.suspend scope resolved to no wled/hue fixtures")
        wled_states = self._fetch_wled_states(registry)
        hue_entertainment_data = self._fetch_hue_entertainment_data()
        holds = self._contention.compute_holds(registry, wled_states, hue_entertainment_data)
        providers = {
            getattr(fixture.binding, "provider", None)
            for fixture in registry.fixtures
            if fixture.id in set(scope_ids) and fixture.binding is not None
        }
        mapped = self._instances_for_providers(providers, holds.view)
        results: list[dict] = []
        stopped_ids: list[int] = []
        for provider, instance in mapped:
            operation = ProviderOperation(
                provider="hyperhdr",
                op="hyperhdr.stop_instance",
                resource_ref=str(instance.instance),
                payload={"instance": instance.instance},
                description=f"suspend hyperHDR instance '{instance.name}' (explicit sync.suspend)",
            )
            one = self._execute_operation(operation, None, f"hyperhdr:{instance.instance}")
            results.append({
                "instance": instance.instance,
                "name": instance.name,
                "provider": provider,
                "ok": one["ok"],
                "detail": one["detail"],
            })
            if one["ok"]:
                stopped_ids.append(instance.instance)
        if stopped_ids:
            self._contention.record_handback({
                "instance_ids": sorted(set(stopped_ids)),
                "fixture_ids": sorted(scope_ids),
                "recorded_at": self._clock.now_iso(),
            })
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SYSTEM,
            f"External light sync suspended for {len(scope_ids)} fixture(s)",
            detail=(
                f"stopped instances {sorted(set(stopped_ids))}" if stopped_ids
                else "no running hyperHDR instance matched the scope"
            ),
            data={"scope_fixture_ids": sorted(scope_ids), "instances": results},
        )
        return success(
            envelope.command,
            {
                "scope_fixture_ids": sorted(scope_ids),
                "instances": results,
                "handback_recorded": bool(stopped_ids),
            },
            envelope.request_id,
        )

    def _sync_resume(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Restart the hyperHDR instance(s) recorded by the last suspend."""
        handback = self._contention.handback()
        if not handback or not handback.get("instance_ids"):
            raise ConflictError(
                "no suspend record to resume: nothing was suspended via sync.suspend "
                "or a takeover override (hyperHDR may simply be idle)"
            )
        reasserted = None
        if params.reassert_scene and self._current_scene_id:
            try:
                scene = self._store.scenes.get_scene(self._current_scene_id)
            except NotFoundError:
                scene = None
            if scene is not None:
                reasserted = self._apply_scene_subset(
                    scene, handback.get("fixture_ids", []),
                    reason="re-assert current scene before handing back to external light sync",
                )
        results: list[dict] = []
        for instance_id in sorted(handback["instance_ids"]):
            operation = ProviderOperation(
                provider="hyperhdr",
                op="hyperhdr.start_instance",
                resource_ref=str(instance_id),
                payload={"instance": instance_id},
                description="hand back to external light sync (sync.resume)",
            )
            one = self._execute_operation(operation, None, f"hyperhdr:{instance_id}")
            results.append({"instance": instance_id, "ok": one["ok"], "detail": one["detail"]})
        self._contention.record_handback(None)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.SYSTEM,
            "External light sync resumed",
            detail=(
                f"restarted instances {sorted(handback['instance_ids'])}"
                + (f"; re-asserted scene look first ({reasserted['fixtures_planned']} fixtures)" if reasserted else "")
            ),
            data={"instances": results, "reassert": reasserted, "handback": handback},
        )
        return success(
            envelope.command,
            {"instances": results, "reassert": reasserted, "handback": handback},
            envelope.request_id,
        )

    def _fixture_set_contention_policy(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Durable per-fixture external light-sync policy (registry mutation)."""
        policy = None if params.policy == "default" else params.policy
        fixture = self._store.fixtures.set_contention_policy(params.fixture_id, policy)
        self._touch()
        self.emit(
            EventLevel.INFO,
            EventCategory.FIXTURE,
            f"{fixture.name} contention policy set to {params.policy}",
            detail=(
                f"external light-sync policy for '{fixture.id}': {params.policy} "
                "(None inherits target/engine default)"
                if policy is None else
                f"external light-sync policy for '{fixture.id}': {params.policy}"
            ),
            fixture_id=fixture.id,
            provider=fixture.binding.provider if fixture.binding is not None else None,
        )
        return success(envelope.command, {"fixture": fixture.to_dict()}, envelope.request_id)

    # -- fixtures ---------------------------------------------------------

    def _fixture_enable(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._fixture_set_enabled(envelope, params, True)

    def _fixture_disable(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._fixture_set_enabled(envelope, params, False)

    def _fixture_set_enabled(self, envelope: CommandEnvelope, params, enabled: bool) -> CommandResult:
        fixture = self._store.fixtures.enable(params.fixture_id) if enabled else self._store.fixtures.disable(
            params.fixture_id
        )
        self._touch()
        verb = "enabled" if enabled else "disabled"
        self.emit(
            EventLevel.INFO,
            EventCategory.FIXTURE,
            f"{fixture.name} {verb}",
            detail=f"fixture '{fixture.id}' {verb}; derived health now '{derive_health(fixture).value}'",
            fixture_id=fixture.id,
            provider=fixture.binding.provider if fixture.binding is not None else None,
        )
        return success(envelope.command, {"fixture": fixture.to_dict()}, envelope.request_id)

    def _fixture_retry(self, envelope: CommandEnvelope, params) -> CommandResult:
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        if fixture.binding is None:
            raise ConflictError(f"fixture '{fixture.id}' has no provider binding to retry")
        provider = fixture.binding.provider
        report, skipped, errors = self._run_discovery(providers=[provider])
        entry = next((e for e in report.entries if e.fixture_id == fixture.id), None)
        candidates = [c.to_dict() for c in report.candidates if c.fixture_id == fixture.id]
        status = entry.status.value if entry is not None else None
        detail = entry.detail if entry is not None else "no discovery entry produced for the fixture"
        self.emit(
            EventLevel.INFO if status in ("bound_ready", "bound_degraded", "bound_reconcile_available") else EventLevel.WARNING,
            EventCategory.FIXTURE,
            f"Retry for {fixture.name}: {status or 'unknown'}",
            detail=detail,
            fixture_id=fixture.id,
            provider=provider,
            data={"run_id": report.run_id, "candidates": candidates},
        )
        self._touch()
        return success(
            envelope.command,
            {
                "fixture_id": fixture.id,
                "provider": provider,
                "run_id": report.run_id,
                "status": status,
                "detail": detail,
                "candidates": candidates,
                "provider_skipped": skipped,
                "provider_errors": errors,
            },
            envelope.request_id,
        )

    def _identify_operation(self, fixture: Fixture) -> ProviderOperation | None:
        """Provider-native "identify" op for ``fixture``, or ``None`` when the
        bound provider/resource has no native identify signal (never
        fabricated — see ``renderers/hue.py``'s truthful-to-provider-state
        rule). Hue: CLIP v2 ``identify`` action on a per-light resource only
        (grouped_light exposes no light-level properties, same restriction
        renderers already apply to color/gradient). HA: the ``light.turn_on``
        ``flash`` parameter, which HA reverts automatically. WLED has no
        native identify equivalent yet."""
        binding = fixture.binding
        if binding is None:
            return None
        if binding.provider == "hue_v2" and getattr(binding, "resource_type", None) == "light":
            return ProviderOperation(
                provider="hue_v2",
                op="hue.identify",
                resource_ref=binding.resource_id,
                payload={"identify": {"action": "identify"}},
            )
        if binding.provider == "ha_light":
            return ProviderOperation(
                provider="ha_light",
                op="ha.call_light_identify",
                resource_ref=binding.ha_entity_id,
                payload={"entity_id": binding.ha_entity_id, "flash": "short"},
            )
        return None

    def _fixture_identify(self, envelope: CommandEnvelope, params) -> CommandResult:
        """Ask the fixture's bound light to flash/blink for physical
        identification. A transient provider nudge, not a state mutation —
        never bumps the engine revision. Never raises on an unsupported
        provider/binding shape: it reports an honest ``ok=False`` receipt,
        the same pattern the executor itself uses for a dispatch it can't
        realize (ports.py's "receipts must never raise")."""
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        if fixture.binding is None:
            raise ConflictError(f"fixture '{fixture.id}' has no provider binding to identify")
        provider = fixture.binding.provider
        operation = self._identify_operation(fixture)
        if operation is None:
            result = receipt(
                False,
                provider,
                "identify",
                f"identify is not supported yet for provider {provider!r}"
                + (
                    " (grouped_light bindings expose no per-light identify)"
                    if provider == "hue_v2"
                    else ""
                ),
            )
        else:
            result = self._executor.execute(operation, fixture)
        self.emit(
            EventLevel.INFO if result["ok"] else EventLevel.WARNING,
            EventCategory.FIXTURE,
            f"Identify {fixture.name}: {'sent' if result['ok'] else 'unavailable'}",
            detail=result["detail"],
            fixture_id=fixture.id,
            provider=provider,
        )
        return success(
            envelope.command,
            {"fixture_id": fixture.id, "provider": provider, "receipt": result},
            envelope.request_id,
        )

    def _require_rebind_observation(self, fixture: Fixture, observation_id: str) -> DiscoveryObservation:
        if self._last_discovery is None:
            raise NotFoundError("discovery report", "latest", detail="run discovery.run before fixture.rebind")
        observation = next((obs for obs in self._last_discovery.observations if obs.observation_id == observation_id), None)
        if observation is None:
            raise NotFoundError("observation", observation_id, detail="not present in the latest discovery report")
        return observation

    def _fixture_rebind_preview(self, envelope: CommandEnvelope, params) -> CommandResult:
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        observation = self._require_rebind_observation(fixture, params.observation_id)
        preview = self._rebind_preview(fixture, observation)
        return success(envelope.command, preview, envelope.request_id)

    def _rebind_preview(self, fixture: Fixture, observation: DiscoveryObservation) -> dict:
        """Pure candidate comparison; it never contacts a provider or persists."""
        warnings: list[str] = []
        candidate_binding = None
        try:
            candidate_binding = self._binding_from_observation(fixture, observation)
        except ConflictError as exc:
            warnings.append(str(exc))
        if observation.capabilities is None:
            warnings.append("candidate observation has no effective capability set")
        safe_to_apply = candidate_binding is not None and observation.capabilities is not None
        post_profile = observation.device_profile or fixture.device_profile
        post_assessment = _assessment_for_binding(observation, post_profile)
        candidate_registry = None
        if safe_to_apply:
            raw = self._store.fixtures.registry().to_dict()
            for item in raw["fixtures"]:
                if item["id"] == fixture.id:
                    item["binding"] = candidate_binding.to_dict()
                    item["capabilities"] = observation.capabilities.to_dict()
                    item.pop("health", None)
                    if post_profile is not None:
                        item["device_profile"] = post_profile.to_dict()
                    item["capability_assessment"] = post_assessment.to_dict()
                    break
            candidate_registry = FixtureRegistry.from_dict(raw)
        impacts: list[dict] = []
        for scene in self._store.scenes.list_scenes():
            current = self._build_plan(scene, self._store.fixtures.registry(), None)
            current_plan = next((plan for plan in current.fixture_plans if plan.fixture_id == fixture.id), None)
            current_skipped = fixture.id in current.skipped_fixture_ids
            candidate_plan = None
            candidate_skipped = False
            if candidate_registry is not None:
                candidate = self._build_plan(scene, candidate_registry, None)
                candidate_plan = next((plan for plan in candidate.fixture_plans if plan.fixture_id == fixture.id), None)
                candidate_skipped = fixture.id in candidate.skipped_fixture_ids
            if current_plan is not None or current_skipped or candidate_plan is not None or candidate_skipped:
                before = current_plan.to_dict() if current_plan is not None else {"skipped": current_skipped}
                after = candidate_plan.to_dict() if candidate_plan is not None else {"skipped": candidate_skipped}
                if before != after:
                    impacts.append({"scene_id": scene.id, "before": before, "after": after})
        return {
            "fixture_id": fixture.id,
            "observation_id": observation.observation_id,
            "current_provider": fixture.binding.provider if fixture.binding else None,
            "candidate_provider": observation.provider,
            "current_binding": fixture.binding.to_dict() if fixture.binding else None,
            "candidate_binding": candidate_binding.to_dict() if candidate_binding else None,
            "current_effective_capabilities": fixture.capabilities.to_dict() if fixture.capabilities else None,
            "candidate_effective_capabilities": observation.capabilities.to_dict() if observation.capabilities else None,
            "capabilities_gained": _capability_changes(fixture.capabilities, observation.capabilities, kind="gained"),
            "capabilities_lost": _capability_changes(fixture.capabilities, observation.capabilities, kind="lost"),
            "capabilities_changed": _capability_changes(fixture.capabilities, observation.capabilities, kind="changed"),
            "device_profile_delta": _profile_delta(fixture.device_profile, post_profile),
            "capability_status_after": post_assessment.status.value,
            "affected_scene_ids": [impact["scene_id"] for impact in impacts],
            "render_fidelity_impact": impacts,
            "warnings": warnings,
            "safe_to_apply": safe_to_apply,
            "parity": _parity_for_capability_delta(fixture.capabilities, observation.capabilities),
            "requires_confirmation": _parity_for_capability_delta(fixture.capabilities, observation.capabilities) in ("changed", "reduced", "unknown"),
        }

    def _fixture_rebind(self, envelope: CommandEnvelope, params) -> CommandResult:
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        observation = self._require_rebind_observation(fixture, params.observation_id)
        preview = self._rebind_preview(fixture, observation)
        if not preview["safe_to_apply"]:
            raise ConflictError("unsafe rebind: " + "; ".join(preview["warnings"]))
        binding = self._binding_from_observation(fixture, observation)
        post_profile = observation.device_profile or fixture.device_profile
        assessment = _assessment_for_binding(observation, post_profile, observed_at=self._clock.now_iso())
        stored = self._store.fixtures.reconcile_binding(
            fixture.id, binding, observation.capabilities, capability_assessment=assessment,
            device_profile=post_profile, health=None, changed_at=self._clock.now_iso(),
            observation_id=observation.observation_id, reason="explicit rebind from discovery observation",
        )
        reasons = [
            candidate.reasons
            for candidate in self._last_discovery.candidates
            if candidate.fixture_id == fixture.id and candidate.observation_id == observation.observation_id
        ]
        flat_reasons = [reason for group in reasons for reason in group]
        self.emit(
            EventLevel.INFO,
            EventCategory.FIXTURE,
            f"{stored.name} rebound to {observation.observation_id}",
            detail="explicit rebind from discovery observation; " + ("; ".join(flat_reasons) or "no candidate notes"),
            fixture_id=stored.id,
            provider=binding.provider,
            data={"reasons": flat_reasons, "previous_provider": (
                fixture.binding.provider if fixture.binding is not None else None
            )},
        )
        self._touch()
        return success(
            envelope.command,
            {
                "fixture": stored.to_dict(),
                "observation_id": observation.observation_id,
                "candidate_reasons": flat_reasons,
                "preview": preview,
            },
            envelope.request_id,
        )

    def _fixture_rebind_rollback(self, envelope: CommandEnvelope, params) -> CommandResult:
        stored = self._store.fixtures.rollback_binding(params.fixture_id, changed_at=self._clock.now_iso())
        self.emit(EventLevel.WARNING, EventCategory.FIXTURE, f"{stored.name} binding rollback applied",
                  fixture_id=stored.id, provider=stored.binding.provider if stored.binding else None)
        self._touch()
        return success(envelope.command, {"fixture": stored.to_dict()}, envelope.request_id)

    # -- first-run registry bootstrap (portable install) -------------------
    #
    # An empty install starts with no fixtures and no targets. These narrow
    # commands let a setup operator turn discovery observations into the
    # first canonical fixtures/targets — WITHOUT ever exposing arbitrary
    # registry JSON replacement, and WITHOUT any provider write (policy keeps
    # them out of read_only and the executor stays write-disabled in
    # registry_admin). Adoption is always an explicit command; discovery
    # itself never mutates the registry (contracts §6).

    def _require_latest_observation(self, observation_id: str) -> DiscoveryObservation:
        if self._last_discovery is None:
            raise NotFoundError(
                "discovery report", "latest", detail="run discovery.run before adopting fixtures"
            )
        observation = next(
            (obs for obs in self._last_discovery.observations if obs.observation_id == observation_id),
            None,
        )
        if observation is None:
            raise NotFoundError(
                "observation", observation_id, detail="not present in the latest discovery report"
            )
        return observation

    def _fixture_adopt(self, envelope: CommandEnvelope, params) -> CommandResult:
        observation = self._require_latest_observation(params.observation_id)
        if is_aggregate_observation(observation):
            raise ConflictError(
                f"observation '{observation.observation_id}' is an HA aggregate/group helper, "
                "not an independently addressable fixture; model it as a target instead"
            )
        validate_id(params.fixture_id, "fixture_id", "params.fixture_id")
        for index, group in enumerate(params.groups):
            validate_id(group, "target_id", f"params.groups.{index}")
        # No previous binding exists, so the binding derivation must find its
        # provider identity entirely in the observation (e.g. a Hue bridge id
        # in observation metadata) — a missing one is a conflict, not a guess.
        placeholder = Fixture(id=params.fixture_id, name=params.name)
        binding = self._binding_from_observation(placeholder, observation)
        profile = observation.device_profile
        now = self._clock.now_iso()
        assessment = _assessment_for_binding(observation, profile, observed_at=now)
        fixture = Fixture(
            id=params.fixture_id,
            name=params.name,
            groups=list(params.groups),
            enabled=params.enabled,
            binding=binding,
            capabilities=observation.capabilities,
            device_profile=profile,
            capability_assessment=assessment,
            metadata={
                "adopted_from_observation": observation.observation_id,
                "adopted_at": now,
                "adopted_run_id": self._last_discovery.run_id,
            },
        )
        stored = self._store.fixtures.add_fixture(fixture)
        self.emit(
            EventLevel.INFO,
            EventCategory.FIXTURE,
            f"Adopted {stored.name} from {observation.observation_id}",
            detail=(
                f"first-run bootstrap; binding={binding.provider}; "
                f"groups={','.join(stored.groups) if stored.groups else 'none'}"
            ),
            fixture_id=stored.id,
            provider=binding.provider,
            data={"observation_id": observation.observation_id, "run_id": self._last_discovery.run_id},
        )
        self._touch()
        return success(
            envelope.command,
            {
                "fixture": stored.to_dict(),
                "observation_id": observation.observation_id,
                "run_id": self._last_discovery.run_id,
            },
            envelope.request_id,
        )

    def _target_create(self, envelope: CommandEnvelope, params) -> CommandResult:
        target_id = params.target_id or normalize_name_to_id(params.name)
        validate_id(target_id, "target_id", "params.target_id")
        for index, fixture_id in enumerate(params.fixture_ids):
            validate_id(fixture_id, "fixture_id", f"params.fixture_ids.{index}")
        # ONE atomic registry commit: the store validates the whole request
        # (duplicate target, every fixture id) before anything is written.
        stored = self._store.fixtures.create_target_with_members(
            {"id": target_id, "name": params.name, "description": params.description},
            list(params.fixture_ids),
        )
        assigned = [
            fixture_id
            for fixture_id in params.fixture_ids
            if target_id in self._store.fixtures.get_fixture(fixture_id).groups
        ]
        self.emit(
            EventLevel.INFO,
            EventCategory.SYSTEM,
            f"Target {stored.name} declared",
            detail=(
                f"first-run bootstrap; id={stored.id}; "
                f"members={len(assigned)} assigned"
            ),
            data={"target_id": stored.id, "assigned_fixture_ids": assigned},
        )
        self._touch()
        return success(
            envelope.command,
            {
                "target": stored.to_dict(),
                "assigned_fixture_ids": assigned,
            },
            envelope.request_id,
        )

    def _target_update(self, envelope: CommandEnvelope, params) -> CommandResult:
        validate_id(params.target_id, "target_id", "params.target_id")
        current = self._store.fixtures.get_target(params.target_id)  # NotFoundError when unknown
        # Pre-state membership: the receipt must report only fixtures THIS
        # command actually changed (an idempotent re-add/re-remove of an
        # already-member/non-member is a no-op, not an assignment/removal).
        pre_groups = {
            fixture_id: set(self._store.fixtures.get_fixture(fixture_id).groups)
            for fixture_id in {*params.add_fixture_ids, *params.remove_fixture_ids}
        }
        # ONE atomic registry commit: rename + membership delta are validated
        # in full before anything is written.
        stored = self._store.fixtures.update_target_definition(
            params.target_id, params.name, list(params.add_fixture_ids), list(params.remove_fixture_ids)
        )
        assigned = [
            fixture_id
            for fixture_id in params.add_fixture_ids
            if params.target_id not in pre_groups[fixture_id]
            and params.target_id in self._store.fixtures.get_fixture(fixture_id).groups
        ]
        removed = [
            fixture_id
            for fixture_id in params.remove_fixture_ids
            if params.target_id in pre_groups[fixture_id]
            and params.target_id not in self._store.fixtures.get_fixture(fixture_id).groups
        ]
        self.emit(
            EventLevel.INFO,
            EventCategory.SYSTEM,
            f"Target {stored.name} updated",
            detail=(
                f"id={stored.id}; renamed={current.name != stored.name}; "
                f"+{len(assigned)}/-{len(removed)} members"
            ),
            data={"target_id": stored.id, "assigned_fixture_ids": assigned, "removed_fixture_ids": removed},
        )
        self._touch()
        return success(
            envelope.command,
            {
                "target": stored.to_dict(),
                "assigned_fixture_ids": assigned,
                "removed_fixture_ids": removed,
            },
            envelope.request_id,
        )

    def _require_reconcile_observation(self, fixture: Fixture, observation_id: str) -> DiscoveryObservation:
        if fixture.binding is None:
            raise ConflictError(
                f"fixture '{fixture.id}' has no binding to reconcile; use fixture.rebind to assign a resource"
            )
        if self._last_discovery is None:
            raise NotFoundError("discovery report", "latest", detail="run discovery.run before fixture.reconcile")
        observation = next((obs for obs in self._last_discovery.observations if obs.observation_id == observation_id), None)
        if observation is None:
            raise NotFoundError("observation", observation_id, detail="not present in the latest discovery report")
        if not observation_matches_binding(fixture.binding, observation, self._last_discovery.observations):
            raise ConflictError(
                f"observation '{observation_id}' is a different provider/resource than the current binding; "
                "use fixture.rebind instead"
            )
        return observation

    def _proposed_groups(self, fixture: Fixture, observation: DiscoveryObservation) -> list[str] | None:
        """When a fresh discovery observation reports a different physical
        room than the fixture's current group membership, propose swapping
        the room-ish group for the matching CANONICAL target id.

        Matched by NAME against the registry's own declared targets —
        never invented by slugifying the observed room name.
        ``domain.identities.normalize_name_to_id`` is documented as
        "migration/seed tooling, never runtime rebinding" for exactly this
        reason: a naive slug can silently diverge from the target id the
        registry actually uses. No matching target -> no proposal (safer
        to surface nothing than to invent a new group).

        Returns ``None`` when there's nothing to propose: no
        ``location_hint`` on the observation, no matching known target, or
        the fixture is already a member of that target.
        """
        hint = observation.location_hint
        if not hint:
            return None
        targets = self._store.fixtures.registry().targets
        matched = next((t for t in targets if t.name.strip().lower() == hint.strip().lower()), None)
        if matched is None:
            return None
        if matched.id in fixture.groups:
            return None
        broad = [g for g in fixture.groups if g == _BROAD_GROUP]
        other_rooms = [g for g in fixture.groups if g != _BROAD_GROUP]
        # Swap the fixture's current "primary" room-ish group (first
        # non-broad entry) for the newly observed one; any FURTHER non-broad
        # group (rare, but the data model allows more than one) rides along
        # unchanged, and the broad group is always preserved.
        return [matched.id, *other_rooms[1:], *broad]

    def _reconcile_preview(self, fixture: Fixture, observation: DiscoveryObservation) -> dict:
        """Pure same-binding knowledge delta; never contacts a provider or persists."""
        observed_at = self._clock.now_iso()
        caps, profile, assessment, health, warnings = proposed_reconcile_revision(
            fixture, observation, observed_at=observed_at
        )
        current_health = derive_health(fixture)
        if not fixture.enabled:
            proposed_health = current_health
        else:
            proposed_health = HealthStatus.READY if fixture.binding is not None else HealthStatus.UNBOUND
        proposed_groups = self._proposed_groups(fixture, observation)
        raw_registry = self._store.fixtures.registry().to_dict()
        for item in raw_registry["fixtures"]:
            if item["id"] == fixture.id:
                if caps is not None:
                    item["capabilities"] = caps.to_dict()
                if profile is not None:
                    item["device_profile"] = profile.to_dict()
                item["capability_assessment"] = assessment.to_dict()
                if health is None:
                    item.pop("health", None)
                else:
                    item["health"] = health.value
                if proposed_groups is not None:
                    # Mutated here too (not just applied later) so the scene
                    # impact analysis below sees the retargeting for free —
                    # a room-membership change can add/drop this fixture
                    # from scenes targeting the old or new room, exactly
                    # the kind of thing that surface already exists to catch.
                    item["groups"] = list(proposed_groups)
                break
        candidate_registry = FixtureRegistry.from_dict(raw_registry)
        impacts: list[dict] = []
        for scene in self._store.scenes.list_scenes():
            current = self._build_plan(scene, self._store.fixtures.registry(), None)
            current_plan = next((plan for plan in current.fixture_plans if plan.fixture_id == fixture.id), None)
            current_skipped = fixture.id in current.skipped_fixture_ids
            candidate = self._build_plan(scene, candidate_registry, None)
            candidate_plan = next((plan for plan in candidate.fixture_plans if plan.fixture_id == fixture.id), None)
            candidate_skipped = fixture.id in candidate.skipped_fixture_ids
            if current_plan is not None or current_skipped or candidate_plan is not None or candidate_skipped:
                before = current_plan.to_dict() if current_plan is not None else {"skipped": current_skipped}
                after = candidate_plan.to_dict() if candidate_plan is not None else {"skipped": candidate_skipped}
                if before != after:
                    impacts.append({"scene_id": scene.id, "before": before, "after": after})
        current_profile = fixture.device_profile.to_dict() if fixture.device_profile else None
        proposed_profile = profile.to_dict() if profile else None
        current_assessment = fixture.capability_assessment.to_dict() if fixture.capability_assessment else None
        proposed_assessment = assessment.to_dict()
        changed = (
            current_health != proposed_health
            or (fixture.capabilities.to_dict() if fixture.capabilities else None) != (caps.to_dict() if caps else None)
            or current_profile != proposed_profile
            or (current_assessment or {}).get("status") != proposed_assessment.get("status")
            or (current_assessment or {}).get("reasons") != proposed_assessment.get("reasons")
            or proposed_groups is not None
        )
        if not changed:
            warnings.append("no registry drift to apply")
        safe_to_apply = changed
        requires_confirmation = bool(
            safe_to_apply
            and (
                any("conflicting" in warning for warning in warnings)
                or bool(impacts)
                or proposed_groups is not None  # a room/target reassignment, always worth a look
                or _parity_for_capability_delta(fixture.capabilities, caps) in ("changed", "reduced", "unknown")
            )
        )
        return {
            "fixture_id": fixture.id,
            "observation_id": observation.observation_id,
            "binding_unchanged": True,
            "current_binding": fixture.binding.to_dict() if fixture.binding else None,
            "proposed_binding": fixture.binding.to_dict() if fixture.binding else None,
            "current": {
                "operational_health": current_health.value,
                "effective_capabilities": fixture.capabilities.to_dict() if fixture.capabilities else None,
                "device_profile": current_profile,
                "capability_assessment": current_assessment,
                "groups": list(fixture.groups),
            },
            "proposed": {
                "operational_health": proposed_health.value,
                "effective_capabilities": caps.to_dict() if caps else None,
                "device_profile": proposed_profile,
                "capability_assessment": proposed_assessment,
                "groups": list(proposed_groups) if proposed_groups is not None else list(fixture.groups),
            },
            "groups_changed": proposed_groups is not None,
            "capabilities_gained": _capability_changes(fixture.capabilities, caps, kind="gained"),
            "capabilities_lost": _capability_changes(fixture.capabilities, caps, kind="lost"),
            "capabilities_changed": _capability_changes(fixture.capabilities, caps, kind="changed"),
            "device_profile_delta": _profile_delta(fixture.device_profile, profile),
            "assessment_changes": {
                "before": current_assessment,
                "after": proposed_assessment,
            },
            "affected_scene_ids": [impact["scene_id"] for impact in impacts],
            "render_fidelity_impact": impacts,
            "warnings": warnings,
            "safe_to_apply": safe_to_apply,
            "requires_confirmation": requires_confirmation,
        }

    def _fixture_reconcile_preview(self, envelope: CommandEnvelope, params) -> CommandResult:
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        observation = self._require_reconcile_observation(fixture, params.observation_id)
        return success(envelope.command, self._reconcile_preview(fixture, observation), envelope.request_id)

    def _fixture_reconcile(self, envelope: CommandEnvelope, params) -> CommandResult:
        fixture = self._store.fixtures.get_fixture(params.fixture_id)
        observation = self._require_reconcile_observation(fixture, params.observation_id)
        preview = self._reconcile_preview(fixture, observation)
        if not preview["safe_to_apply"]:
            raise ConflictError("unsafe reconcile: " + "; ".join(preview["warnings"]))
        prior_binding = fixture.binding.to_dict() if fixture.binding else None
        caps, profile, assessment, health, _warnings = proposed_reconcile_revision(
            fixture, observation, observed_at=self._clock.now_iso()
        )
        if caps is None:
            raise ConflictError("unsafe reconcile: effective capabilities are required")
        proposed_groups = self._proposed_groups(fixture, observation)
        stored = self._store.fixtures.reconcile_binding(
            fixture.id,
            fixture.binding,
            caps,
            capability_assessment=assessment,
            device_profile=profile,
            health=health,
            changed_at=self._clock.now_iso(),
            observation_id=observation.observation_id,
            reason="explicit reconcile from discovery observation",
            groups=proposed_groups,
        )
        after_binding = stored.binding.to_dict() if stored.binding else None
        if after_binding != prior_binding:
            raise ConflictError("reconcile mutated binding identity; rolled expectation violated")
        self.emit(
            EventLevel.INFO,
            EventCategory.FIXTURE,
            f"{stored.name} registry reconciled from {observation.observation_id}",
            detail="same-binding reconcile; provider was not contacted",
            fixture_id=stored.id,
            provider=stored.binding.provider if stored.binding else None,
            data={"observation_id": observation.observation_id, "preview": preview},
        )
        self._touch()
        return success(
            envelope.command,
            {"fixture": stored.to_dict(), "observation_id": observation.observation_id, "preview": preview},
            envelope.request_id,
        )

    def _registry_migration_preview(self, envelope: CommandEnvelope, params) -> CommandResult:
        preview = self._store.fixtures.inspect_schema_migration()
        return success(envelope.command, preview, envelope.request_id)

    def _registry_migrate(self, envelope: CommandEnvelope, params) -> CommandResult:
        result = self._store.fixtures.migrate_loaded_legacy_registry()
        if not result.get("noop"):
            self.emit(
                EventLevel.INFO,
                EventCategory.SYSTEM,
                "Registry schema migrated",
                detail=f"backup={result.get('backup_path')}",
            )
            self._touch()
        return success(envelope.command, result, envelope.request_id)

    def _binding_from_observation(self, fixture: Fixture, observation: DiscoveryObservation):
        """Build a provider binding from a discovery observation (contracts §2)."""
        provider = observation.provider
        if provider == "hue_v2":
            bridge = observation.metadata.get("bridge_id")
            if not bridge and isinstance(fixture.binding, HueBinding):
                bridge = fixture.binding.bridge_id
            if not bridge:
                raise ConflictError(
                    f"cannot determine bridge for hue observation '{observation.observation_id}': "
                    "no bridge_id in observation metadata and fixture has no previous hue binding; "
                    "set the deployment's hue_bridge_id (Hue app > Settings > bridge serial) "
                    "so first-run adoption can derive the binding"
                )
            resource_type = observation.metadata.get("resource_type")
            if resource_type not in ("light", "grouped_light"):
                resource_type = "light"
            linked = observation.metadata.get("ha_entity_ids")
            ha_entity_id = None
            if isinstance(linked, list) and linked and isinstance(linked[0], str):
                ha_entity_id = linked[0]
            hue_group_id = observation.metadata.get("hue_group_id")
            hue_group_type = observation.metadata.get("hue_group_type")
            if not isinstance(hue_group_id, str) or not hue_group_id:
                hue_group_id = None
                hue_group_type = None
            elif hue_group_type not in ("room", "zone"):
                hue_group_type = "room"
            return HueBinding(
                bridge_id=str(bridge),
                resource_id=observation.provider_resource_id,
                resource_type=resource_type,
                ha_entity_id=ha_entity_id,
                hue_group_id=hue_group_id,
                hue_group_type=hue_group_type,
            )
        if provider == "wled":
            resource = observation.provider_resource_id  # "<device_id>:seg:<n>" | "<device_id>:dev"
            segments: list[int] = []
            if ":seg:" in resource:
                device_id, _, tail = resource.rpartition(":seg:")
                if tail.isdigit():
                    segments = [int(tail)]
            elif resource.endswith(":dev"):
                device_id = resource[: -len(":dev")]
            else:
                device_id = resource
            linked = observation.metadata.get("ha_entity_ids")
            ha_entity_ids = [item for item in linked if isinstance(item, str)] if isinstance(linked, list) else []
            return WledBinding(
                device_id=str(device_id),
                segment_ids=segments,
                ha_entity_ids=ha_entity_ids,
                endpoint_hint=observation.endpoint_hint,
            )
        if provider == "ha_light":
            return HaLightBinding(ha_entity_id=observation.provider_resource_id)
        raise ConflictError(f"unknown provider {provider!r} on observation '{observation.observation_id}'")

    # -- discovery --------------------------------------------------------

    def _discovery_run(self, envelope: CommandEnvelope, params) -> CommandResult:
        report, skipped, errors = self._run_discovery(providers=params.providers)
        self._touch()
        summary = dict(report.summary)
        detail_bits = [
            f"{summary.get('observations_total', 0)} observations",
            f"{summary.get('fixtures_bound_ready', 0)} bound ready",
            f"{summary.get('fixtures_missing', 0)} missing",
        ]
        if skipped:
            detail_bits.append("skipped: " + ", ".join(skipped))
        self.emit(
            EventLevel.WARNING if (skipped or errors) else EventLevel.INFO,
            EventCategory.DISCOVERY,
            f"Discovery run {report.run_id} completed",
            detail="; ".join(detail_bits),
            data={"summary": summary, "skipped": skipped, "errors": errors},
        )
        return success(
            envelope.command,
            {
                "run_id": report.run_id,
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "providers": report.providers,
                "summary": summary,
                "skipped_providers": skipped,
                "provider_errors": errors,
            },
            envelope.request_id,
        )

    def _run_discovery(self, providers: list[str] | None = None):
        """Fetch payloads via the configured fetchers, build observations, run
        the pure discovery service, and store the report as the latest one.

        Returns ``(report, skipped_providers, provider_errors)``. Missing
        fetchers, None payloads, and fetch exceptions are provider-scoped:
        the provider is skipped (with an event) and the run continues.
        """
        registry = self._store.fixtures.registry()
        wanted = [provider for provider in KNOWN_PROVIDERS if providers is None or provider in providers]
        unknown = sorted(set(providers or ()) - set(KNOWN_PROVIDERS))
        observations: list[DiscoveryObservation] = []
        skipped: list[str] = []
        errors: list[dict] = []

        for provider in wanted:
            if provider == "hue_v2":
                clip_lights = self._safe_fetch(self._fetchers.fetch_hue, provider, skipped, errors)
                if clip_lights is None:
                    continue
                # rooms + HA states are OPTIONAL hue enrichment: a missing
                # fetcher never skips the provider, it just omits the extra.
                clip_rooms = self._optional_fetch(self._fetchers.fetch_hue_rooms, provider)
                ha_enrichment = self._optional_fetch(self._fetchers.fetch_ha_states, provider)
                clip_devices = self._optional_fetch(self._fetchers.fetch_hue_devices, provider)
                hue_observations = build_hue_observations(clip_lights, clip_rooms, ha_enrichment, clip_devices)
                if self._fetchers.hue_bridge_id:
                    # Bridge-level identity hint: light resources never carry
                    # it, and first-run adoption has no previous binding to
                    # fall back on (see ``_binding_from_observation``).
                    for observation in hue_observations:
                        observation.metadata["bridge_id"] = str(self._fetchers.hue_bridge_id)
                observations.extend(hue_observations)
            elif provider == "wled":
                if self._fetchers.fetch_wled_info is None or self._fetchers.fetch_wled_state is None:
                    skipped.append(provider)
                    self.emit(
                        EventLevel.WARNING,
                        EventCategory.DISCOVERY,
                        "Discovery skipped WLED",
                        detail="no info/state fetchers configured for provider 'wled'",
                        provider=provider,
                    )
                    continue
                info = self._safe_fetch(self._fetchers.fetch_wled_info, provider, skipped, errors)
                state = self._safe_fetch(self._fetchers.fetch_wled_state, provider, skipped, errors)
                if info is None or state is None:
                    continue
                observations.extend(
                    build_wled_observations(info, state, self._fetchers.wled_endpoint_hint)
                )
            elif provider == "ha_light":
                ha_states = self._safe_fetch(self._fetchers.fetch_ha_states, provider, skipped, errors)
                if ha_states is None:
                    continue
                metadata = self._optional_fetch(self._fetchers.fetch_ha_device_metadata, provider)
                observations.extend(
                    build_halight_observations(
                        ha_states,
                        metadata,
                        ignored_entity_ids=self._fetchers.ignored_ha_entity_ids,
                    )
                )

        self._discovery_seq += 1
        started_at = self._clock.now_iso()
        run_id = f"disc-{self._discovery_seq:04d}-{started_at.replace('-', '').replace(':', '')}"
        report = run_discovery(
            registry,
            observations,
            run_id=run_id[:64],
            started_at=started_at,
            finished_at=self._clock.now_iso(),
            previous=self._last_discovery,
        )
        self._last_discovery = report
        for provider in unknown:
            skipped.append(f"{provider} (unknown provider name)")
        return report, skipped, errors

    def _optional_fetch(self, fetcher, provider: str) -> dict | None:
        """Fetch an OPTIONAL enrichment payload: None fetcher/None payload ->
        None silently; exceptions become a warning event (never a skip)."""
        if fetcher is None:
            return None
        try:
            return fetcher()
        except Exception as exc:
            message = sanitize_string(f"{type(exc).__name__}: {exc}")[:256]
            self.emit(
                EventLevel.WARNING,
                EventCategory.DISCOVERY,
                f"Discovery enrichment fetch failed for {_PROVIDER_LABELS.get(provider, provider)}",
                detail=message,
                provider=provider,
            )
            return None

    def _safe_fetch(self, fetcher, provider: str, skipped: list, errors: list) -> dict | None:
        """Call one discovery fetcher; None fetcher / None payload / exception
        all become provider-scoped skips with events (never engine errors)."""
        if fetcher is None:
            skipped.append(provider)
            self.emit(
                EventLevel.WARNING,
                EventCategory.DISCOVERY,
                f"Discovery skipped {_PROVIDER_LABELS.get(provider, provider)}",
                detail=f"no fetcher configured for provider '{provider}'",
                provider=provider,
            )
            return None
        try:
            payload = fetcher()
        except Exception as exc:
            message = sanitize_string(f"{type(exc).__name__}: {exc}")[:256]
            skipped.append(provider)
            errors.append({"provider": provider, "error": message})
            self.emit(
                EventLevel.WARNING,
                EventCategory.DISCOVERY,
                f"Discovery fetch failed for {_PROVIDER_LABELS.get(provider, provider)}",
                detail=message,
                provider=provider,
            )
            return None
        if payload is None:
            skipped.append(provider)
            self.emit(
                EventLevel.WARNING,
                EventCategory.DISCOVERY,
                f"Discovery skipped {_PROVIDER_LABELS.get(provider, provider)}",
                detail="fetcher returned no data",
                provider=provider,
            )
            return None
        return payload

    # -- diagnostics ------------------------------------------------------

    def _diagnostics_export(self, envelope: CommandEnvelope, params) -> CommandResult:
        from ..discovery.drift import membership_drift_report

        registry = self._store.fixtures.registry()
        observations = list(self._last_discovery.observations) if self._last_discovery is not None else []
        snapshot = {
            "exported_at": self._clock.now_iso(),
            "status": self.status(),
            "events": [event.to_dict() for event in self.recent_events(params.recent_events)],
            "registry": registry.to_dict(),
            "discovery": self._last_discovery.to_dict() if self._last_discovery is not None else None,
            "membership_drift": membership_drift_report(
                registry,
                observations,
                scenes=self._store.scenes.list_scenes(),
            ),
        }
        if params.redact:
            snapshot = sanitize_tree(snapshot)
        return success(envelope.command, snapshot, envelope.request_id)

    # -- HA-native routine CRUD (routines pass) ------------------------------

    def _routine_scene_lookup(self, scene_id: str) -> tuple[str, bool] | None:
        """``scene_id -> (name, motion_is_dynamic)`` for routine validation.
        Returns None for unknown scenes (active catalog only)."""
        try:
            scene = self._store.scenes.get_scene(scene_id)
        except NotFoundError:
            return None
        return (scene.name, scene.motion.mode is not MotionMode.STATIC)

    def _routine_mutation(self, envelope: CommandEnvelope, operation) -> CommandResult:
        """Shared wrapper: run one RoutineService mutation and map its typed
        failures onto honest CommandResults. Success bumps the engine
        revision so every surface (Workbench polling, HA projection)
        notices the external-state change."""
        try:
            data = operation(self._routines)
        except RoutineCapabilityUnavailable as exc:
            return failure(
                envelope.command,
                ErrorCode.PROVIDER_UNAVAILABLE,
                str(exc),
                details={"capability": "ha_automation_management"},
                request_id=envelope.request_id,
            )
        except RoutineSourceChanged as exc:
            return failure(
                envelope.command,
                ErrorCode.CONFLICT,
                "Home Assistant changed this automation since it was loaded; "
                "refresh the routines and reapply your edit.",
                details={
                    "kind": "routine_source_changed",
                    "automation_id": exc.automation_id,
                    "current_digest": exc.current_digest,
                },
                request_id=envelope.request_id,
            )
        except RoutineNotEditable as exc:
            return failure(
                envelope.command,
                ErrorCode.CONFLICT,
                str(exc),
                details={
                    "kind": "routine_advanced",
                    "automation_id": exc.automation_id,
                    "unsupported_reasons": exc.reasons,
                },
                request_id=envelope.request_id,
            )
        except RoutineVerificationFailed as exc:
            return failure(
                envelope.command,
                ErrorCode.INTERNAL_ERROR,
                str(exc),
                details={"kind": "routine_verification_failed"},
                request_id=envelope.request_id,
            )
        except KeyError as exc:
            scene_id = exc.args[0] if exc.args else ""
            return failure(
                envelope.command,
                ErrorCode.NOT_FOUND,
                f"scene {str(scene_id)!r} was not found",
                request_id=envelope.request_id,
            )
        except ValueError as exc:
            # e.g. behavior=play against a static scene
            return failure(
                envelope.command,
                ErrorCode.VALIDATION_ERROR,
                str(exc),
                request_id=envelope.request_id,
            )
        except HaAutomationIdError as exc:
            return failure(
                envelope.command,
                ErrorCode.VALIDATION_ERROR,
                str(exc),
                request_id=envelope.request_id,
            )
        self._touch()
        return success(envelope.command, data, envelope.request_id)

    def _routine_create(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._routine_mutation(
            envelope,
            lambda service: service.create(
                scene_id=params.scene_id,
                behavior=params.behavior,
                time_hhmm=params.time,
                weekdays=params.weekdays,
                scene_lookup=self._routine_scene_lookup,
            ),
        )

    def _routine_update(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._routine_mutation(
            envelope,
            lambda service: service.update(
                automation_id=params.automation_id,
                source_digest=params.source_digest,
                time_hhmm=params.time,
                weekdays=params.weekdays,
                behavior=params.behavior,
                scene_id=params.scene_id,
                scene_lookup=self._routine_scene_lookup,
            ),
        )

    def _routine_delete(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._routine_mutation(
            envelope,
            lambda service: service.delete(
                automation_id=params.automation_id,
                source_digest=params.source_digest,
            ),
        )

    def _routine_enable(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._routine_mutation(
            envelope,
            lambda service: service.set_enabled(
                automation_id=params.automation_id,
                source_digest=params.source_digest,
                enabled=True,
            ),
        )

    def _routine_disable(self, envelope: CommandEnvelope, params) -> CommandResult:
        return self._routine_mutation(
            envelope,
            lambda service: service.set_enabled(
                automation_id=params.automation_id,
                source_digest=params.source_digest,
                enabled=False,
            ),
        )

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _require_scene(self, scene_id: str) -> Scene:
        return self._store.scenes.get_scene(scene_id)

    def _build_plan(self, scene: Scene, registry: FixtureRegistry, target_ids: list[str] | None) -> RenderPlan:
        from ..renderers import build_render_plan  # lazy: renderers import chain stays out of hot paths

        return build_render_plan(scene, registry, target_ids=target_ids)

    def _execute_plan(self, plan: RenderPlan, registry: FixtureRegistry):
        """Run every planned operation through the executor port (best-effort).

        ``hue.put_scene_dynamic`` operations are realization MARKERS, not
        executable ops: static ``scene.apply`` skips them (a dynamic scene
        applied statically lands on the per-fixture static state; dynamic
        execution belongs to playback realization). Skipping them here keeps
        the executor's marker refusal as defense in depth only.
        """
        fixtures_by_id = {fixture.id: fixture for fixture in registry.fixtures}
        receipts: list[dict] = []
        failures: list[tuple[str, dict]] = []
        ok_count = 0
        for fixture_plan in plan.fixture_plans:
            fixture = fixtures_by_id.get(fixture_plan.fixture_id)
            for operation in fixture_plan.operations:
                if operation.op == "hue.put_scene_dynamic":
                    continue
                one = self._execute_operation(operation, fixture, fixture_plan.fixture_id)
                receipts.append(one)
                if one["ok"]:
                    ok_count += 1
                else:
                    failures.append((fixture_plan.fixture_id, one))
        return _Executions(receipts=receipts, failures=failures, ok_count=ok_count)

    def _execute_operation(self, operation: ProviderOperation, fixture: Fixture | None, fixture_id: str) -> dict:
        try:
            if fixture is None and operation.provider != "hyperhdr":
                # hyperhdr ops are instance-scoped device control (no fixture);
                # every other provider op requires its fixture present.
                raise ValueError(f"fixture '{fixture_id}' disappeared from the registry mid-apply")
            raw = self._executor.execute(operation, fixture)
            if not isinstance(raw, dict):
                raise ValueError("executor returned a non-dict receipt")
            result = receipt(
                bool(raw.get("ok")),
                str(raw.get("provider", operation.provider)),
                str(raw.get("op", operation.op)),
                str(raw.get("detail", "")),
            )
        except Exception as exc:  # executor adapters must not raise, but never trust the boundary
            result = receipt(False, operation.provider, operation.op, sanitize_string(f"{type(exc).__name__}: {exc}")[:256])
        result["fixture_id"] = fixture_id
        result["resource_ref"] = operation.resource_ref
        return result

    @staticmethod
    def _plan_detail(plan: RenderPlan, scene: Scene) -> str:
        mode = "dynamic" if scene.motion.mode is not MotionMode.STATIC else "static"
        return f"{len(plan.fixture_plans)} fixtures • {mode}"

    def _touch(self) -> None:
        """Bump the monotonic revision — called exactly once per successful
        state mutation by the mutating handlers (read-only paths never call
        it), so callers can diff ``status()["engine"]["revision"]``."""
        self._revision += 1

    def _failure_event(
        self,
        command,
        code: ErrorCode,
        message: str,
        request_id: str | None,
        details: dict | None = None,
    ) -> CommandResult:
        result = failure(
            command if isinstance(command, str) else "unknown",
            code,
            message,
            details=details,
            request_id=request_id,
        )
        self.emit(
            EventLevel.ERROR if code in (ErrorCode.INTERNAL_ERROR, ErrorCode.CONFLICT) else EventLevel.WARNING,
            EventCategory.SYSTEM,
            f"Command failed: {result.command}",
            detail=sanitize_string(message)[:1024],
            data={"code": code.value},
        )
        return result


class _Executions:
    """Receipts + failure bookkeeping for one plan execution pass."""

    __slots__ = ("receipts", "failures", "ok_count")

    def __init__(self, receipts: list[dict], failures: list[tuple[str, dict]], ok_count: int) -> None:
        self.receipts = receipts
        self.failures = failures
        self.ok_count = ok_count


def _capability_changes(current, candidate, *, kind: str) -> list[str]:
    """Stable semantic capability delta for a provider-migration review."""
    if current is None or candidate is None:
        return []
    if kind not in ("gained", "lost", "changed"):
        raise ValueError(f"unknown capability comparison kind {kind!r}")
    out: list[str] = []
    for field in ("on_off", "brightness", "color_xy", "dynamic_native", "cct"):
        old = getattr(current, field)
        new = getattr(candidate, field)
        if kind == "gained" and not old and new:
            out.append(field)
        elif kind == "lost" and old and not new:
            out.append(field)
    for field in ("color_temp", "gradient"):
        old, new = getattr(current, field), getattr(candidate, field)
        if kind == "gained" and old is None and new is not None:
            out.append(field)
        elif kind == "lost" and old is not None and new is None:
            out.append(field)
        elif kind == "changed" and old is not None and new is not None and old.to_dict() != new.to_dict():
            out.append(f"{field} range changed ({old.to_dict()} -> {new.to_dict()})")
    for field in ("effects", "palettes"):
        old, new = set(getattr(current, field)), set(getattr(candidate, field))
        if kind == "gained" and new - old:
            out.append(f"{field}: " + ", ".join(sorted(new - old)) + " gained")
        elif kind == "lost" and old - new:
            out.append(f"{field}: " + ", ".join(sorted(old - new)) + " lost")
        elif kind == "changed" and old == new and list(getattr(current, field)) != list(getattr(candidate, field)):
            out.append(f"{field} catalog order changed")
    return out


def _profile_delta(before_profile, after_profile) -> dict:
    before = before_profile.to_dict() if before_profile else {}
    after = after_profile.to_dict() if after_profile else {}
    keys = sorted(set(before) | set(after))
    return {key: {"before": before.get(key), "after": after.get(key)} for key in keys if before.get(key) != after.get(key)}


def _parity_for_capability_delta(current, candidate) -> str:
    if current is None or candidate is None:
        return "unknown"
    if _capability_changes(current, candidate, kind="lost"):
        return "reduced"
    if _capability_changes(current, candidate, kind="gained") or _capability_changes(current, candidate, kind="changed"):
        return "changed"
    return "preserved"


def _assessment_for_binding(observation: DiscoveryObservation, profile, *, observed_at: str | None = None) -> CapabilityAssessment:
    """Compare only explicit physical-feature evidence to provider capabilities."""
    return assess_capability_parity(
        observation.capabilities,
        profile,
        provider=observation.provider,
        observation_id=observation.observation_id,
        observed_at=observed_at,
    )
