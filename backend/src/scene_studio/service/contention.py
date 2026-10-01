"""External light-sync contention service (hyperHDR ownership pass).

Engine-side orchestration seam for the pure :mod:`scene_studio.domain.contention`
contracts. Owns:

- **probe caching** — the injected ``probe`` callable (adapter transport:
  ``POST {hyperhdr_host}/json-rpc`` ``serverinfo``) is TTL-cached so the
  Workbench's ~3 s status polling and the adapter heartbeat share one
  bounded probe cadence. Mutating decisions (apply/start/resume) force a
  fresh probe. Probes never run under the engine's mutation lock (same
  pattern as live-state sampling).
- **hold bookkeeping** — the last composed per-fixture hold map plus the
  pending-restore record (a static scene apply that yielded fixtures
  re-asserts automatically when hyperHDR surrenders) and the handback
  record (which hyperHDR instances a takeover suspended, so
  ``sync.resume`` can restart exactly those).
- **persistence** — pending-restore/handback survive an AppDaemon restart
  via ``provider_state/contention.json`` (atomic, schema-versioned).

The engine keeps ALL provider writes and session transitions; this module
never touches the executor, the registry, or the playback collection.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain.contention import (
    CONTENTION_DEFAULT_POLICY,
    HyperHdrView,
    map_fixture_contention,
)
from ..domain.fixtures import FixtureRegistry
from ..stores.atomic import atomic_write_json, read_json

__all__ = ["ContentionService", "ContentionStateStore"]

_STATE_SCHEMA_VERSION = 1
_DEFAULT_TTL_SECONDS = 5.0


def _epoch(iso: str) -> float:
    try:
        from datetime import datetime, timezone

        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


class ContentionStateStore:
    """Persisted pending-restore + handback records (atomic JSON)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict:
        try:
            doc = read_json(self._path)
        except FileNotFoundError:
            return {"schema_version": _STATE_SCHEMA_VERSION, "pending_restore": None, "handback": None}
        if not isinstance(doc, dict) or doc.get("schema_version") != _STATE_SCHEMA_VERSION:
            return {"schema_version": _STATE_SCHEMA_VERSION, "pending_restore": None, "handback": None}
        return doc

    def _save(self, doc: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path, doc)

    # pending restore ------------------------------------------------------

    def pending_restore(self) -> dict | None:
        record = self._load().get("pending_restore")
        return dict(record) if isinstance(record, dict) else None

    def record_pending_restore(self, record: dict | None) -> None:
        doc = self._load()
        doc["pending_restore"] = record
        self._save(doc)

    # handback -------------------------------------------------------------

    def handback(self) -> dict | None:
        record = self._load().get("handback")
        return dict(record) if isinstance(record, dict) else None

    def record_handback(self, record: dict | None) -> None:
        doc = self._load()
        doc["handback"] = record
        self._save(doc)


@dataclass
class _HoldComputation:
    """Everything a decision point needs from one evaluation."""

    held_fixture_ids: frozenset
    owners: dict
    view: HyperHdrView
    wled_probe_ok: bool
    wled_probe_detail: str


class ContentionService:
    """Probe cache + hold map + contention bookkeeping (see module docstring)."""

    def __init__(
        self,
        *,
        probe,
        clock,
        store_root: Path | str,
        default_policy: str = CONTENTION_DEFAULT_POLICY,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        wled_instance_ids: list[int] | None = None,
        hue_instance_ids: list[int] | None = None,
    ) -> None:
        self._probe = probe  # callable -> serverinfo info dict | None (adapter transport)
        self._clock = clock
        self._default_policy = default_policy
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._wled_instance_ids = list(wled_instance_ids) if wled_instance_ids is not None else None
        self._hue_instance_ids = list(hue_instance_ids) if hue_instance_ids is not None else None
        self._state_store = ContentionStateStore(Path(store_root) / "provider_state" / "contention.json")
        self._lock = threading.Lock()
        self._view: HyperHdrView = HyperHdrView.unavailable("", "no probe result yet")
        self._view_epoch = 0.0
        self._last_hold: _HoldComputation | None = None

    # -- configuration accessors (engine reads these for instance mapping) --

    @property
    def default_policy(self) -> str:
        return self._default_policy

    @property
    def wled_instance_ids(self) -> list[int] | None:
        return self._wled_instance_ids

    @property
    def hue_instance_ids(self) -> list[int] | None:
        return self._hue_instance_ids

    @property
    def configured(self) -> bool:
        """True when a hyperHDR probe is wired (the WLED ``lor`` side works
        without it, but the deployment is only "contended-aware" when the
        probe host is configured)."""
        return self._probe is not None

    # -- probe cache ---------------------------------------------------------

    def refresh(self, *, force: bool = False) -> HyperHdrView:
        """Probe hyperHDR (when the cache is stale) and return the view.

        One probe in flight at a time; a concurrent caller gets the cached
        view immediately instead of queueing a second provider read.
        """
        if not self._lock.acquire(blocking=False):
            return self._view
        try:
            now = self._clock.now_iso()
            if not force and (time.time() - self._view_epoch) < self._ttl_seconds:
                return self._view
            if self._probe is None:
                self._view = HyperHdrView.unavailable(now, "hyperhdr_host is not configured")
                self._view_epoch = time.time()
                return self._view
            try:
                info = self._probe()
            except Exception as exc:
                info = None
                detail = f"{type(exc).__name__}: {exc}"
                self._view = HyperHdrView.unavailable(now, detail[:200])
                self._view_epoch = time.time()
                return self._view
            self._view = HyperHdrView.from_serverinfo(info, now)
            if not self._view.available and not self._view.detail:
                self._view = HyperHdrView.unavailable(now, "probe returned no usable serverinfo")
            self._view_epoch = time.time()
            return self._view
        finally:
            self._lock.release()

    def refresh_if_stale(self) -> HyperHdrView:
        return self.refresh(force=False)

    def cached_view(self) -> HyperHdrView:
        return self._view

    # -- hold composition ------------------------------------------------------

    def compute_holds(
        self,
        registry: FixtureRegistry,
        wled_states: dict | None,
        hue_entertainment_data: dict | None = None,
    ) -> _HoldComputation:
        """Compose the fresh per-fixture hold map (decision-time path).

        ``wled_states`` maps WLED device_id -> fetched ``/json/state``.
        ``hue_entertainment_data`` is ``{"configurations": ..., "services":
        ..., "devices": ...}`` (raw CLIP v2 payloads) resolved into the
        precise per-light Hue hold set. The hyperHDR view is probed fresh
        (forced) — a takeover/yield decision must never act on a stale
        "sync is idle" answer.
        """
        view = self.refresh(force=True)
        from ..domain.contention import hue_active_entertainment_light_ids, wled_held_device_ids

        held_devices = wled_held_device_ids(wled_states or {})
        hue_entertainment_data = hue_entertainment_data or {}
        held_hue_lights = hue_active_entertainment_light_ids(
            hue_entertainment_data.get("configurations"),
            hue_entertainment_data.get("services"),
            hue_entertainment_data.get("devices"),
        )
        mapping = map_fixture_contention(
            registry,
            view,
            held_wled_device_ids=held_devices,
            held_hue_light_resource_ids=held_hue_lights,
            wled_instance_ids=self._wled_instance_ids,
            hue_instance_ids=self._hue_instance_ids,
        )
        held = frozenset(fid for fid, entry in mapping.items() if entry.held)
        owners = {fid: entry.owner for fid, entry in mapping.items() if entry.held}
        computation = _HoldComputation(
            held_fixture_ids=held,
            owners=owners,
            view=view,
            wled_probe_ok=wled_states is not None,
            wled_probe_detail="" if wled_states is not None else "WLED state read unavailable",
        )
        self._last_hold = computation
        return computation

    def last_holds(self) -> _HoldComputation | None:
        return self._last_hold

    def held_ids_cached(self, registry: FixtureRegistry, last_live_snapshot=None) -> dict[str, str]:
        """Best-effort hold map for read surfaces (status/fixtures catalog):
        the last live snapshot's WLED external-owner evidence + the last
        COMPUTED per-light Hue hold set (``compute_holds``, refreshed on the
        heartbeat and at every apply/start decision) — no network on this
        read path itself. Before the first computation this simply reports
        nothing held for Hue (fail-open), rather than a blanket guess from
        the raw hyperHDR probe."""
        holds: dict[str, str] = {}
        if last_live_snapshot is not None:
            for state in getattr(last_live_snapshot, "fixtures", {}).values():
                owner = getattr(state, "external_owner", None)
                if isinstance(owner, dict) and owner.get("controller") == "wled":
                    holds[state.fixture_id] = (
                        f"WLED live override active (lor={owner.get('lor')})"
                    )
        if self._last_hold is not None:
            for fixture in registry.fixtures:
                provider = getattr(fixture.binding, "provider", None) if fixture.binding else None
                if provider == "hue_v2" and fixture.id in self._last_hold.held_fixture_ids:
                    holds.setdefault(
                        fixture.id, self._last_hold.owners.get(fixture.id, "external light sync")
                    )
        return holds

    # -- bookkeeping -------------------------------------------------------

    def pending_restore(self) -> dict | None:
        return self._state_store.pending_restore()

    def record_pending_restore(self, record: dict | None) -> None:
        self._state_store.record_pending_restore(record)

    def handback(self) -> dict | None:
        return self._state_store.handback()

    def record_handback(self, record: dict | None) -> None:
        self._state_store.record_handback(record)

    # -- status ------------------------------------------------------------

    def status_view(self) -> dict:
        view = self._view
        hold = self._last_hold
        return {
            "configured": self._probe is not None,
            "default_policy": self._default_policy,
            "checked_at": view.checked_at or None,
            "available": view.available,
            "detail": view.detail or None,
            "streaming": view.streaming,
            "streaming_sources": list(view.streaming_sources),
            "instances": [entry.to_dict() for entry in view.instances],
            "instance_mapping": {
                "wled_instance_ids": self._wled_instance_ids,
                "hue_instance_ids": self._hue_instance_ids,
            },
            "held_fixture_ids": sorted(hold.held_fixture_ids) if hold else [],
            "held_owners": dict(sorted(hold.owners.items())) if hold else {},
            "wled_probe_ok": hold.wled_probe_ok if hold else None,
            "pending_restore": self.pending_restore(),
            "handback": self.handback(),
        }
