"""Ports (hexagonal interfaces) for the Scene Studio command service.

Integration wave 1 keeps the engine pure: it never opens a socket, never
reads the wall clock directly, and never imports AppDaemon or requests.
Everything environmental is injected through the small protocols here:

- :class:`ProviderExecutor` — the only device-contact boundary. The engine
  hands it a dry-run :class:`ProviderOperation` (from the render plan) plus
  the target :class:`Fixture`; the adapter realizes it against the live
  provider and returns an execution receipt.

- :class:`Clock` — UTC timestamp source (ISO-8601 ``Z``) used for events,
  playback stamps, and discovery run ids.

- :class:`DiscoveryFetchers` — optional provider payload sources for
  ``discovery.run``. Discovery itself is transport-free: the fetchers only
  deliver already-fetched provider payloads (Hue CLIP v2 JSON, WLED
  ``/json/info`` + ``/json/state``, HA state dump). A ``None`` fetcher means
  "provider not configured here" and the engine skips it with an event.

Small in-memory fakes (``RecordingExecutor``, ``SteppingClock``) live here
so tests and future tooling share one implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, runtime_checkable

from ..domain.fidelity import ProviderOperation
from ..domain.fixtures import Fixture

__all__ = [
    "Clock",
    "DiscoveryFetchers",
    "HaAutomationEntity",
    "HaAutomationGateway",
    "HaAutomationGatewayError",
    "MonotonicClock",
    "ProviderExecutor",
    "RECEIPT_KEYS",
    "RecordingExecutor",
    "SteppingClock",
    "receipt",
]


# ---------------------------------------------------------------------------
# Provider execution port
# ---------------------------------------------------------------------------

#: Required keys of an execution receipt returned by ``ProviderExecutor``.
RECEIPT_KEYS = ("ok", "provider", "op", "detail")


def receipt(ok: bool, provider: str, op: str, detail: str) -> dict:
    """Build a normalized execution receipt (engine collects these verbatim)."""
    return {"ok": bool(ok), "provider": provider, "op": op, "detail": str(detail)}


@runtime_checkable
class ProviderExecutor(Protocol):
    """Realizes one provider operation against a live fixture.

    Implementations live in adapters (``appdaemon_adapter``); the engine
    only ever sees this port and never performs I/O itself. Receipts must
    never raise: transport failures are reported as ``ok=False`` receipts
    so a scene apply can stay best-effort across fixtures.
    """

    def execute(self, operation: ProviderOperation, fixture: Fixture) -> dict:
        """Execute ``operation`` on ``fixture`` and return the receipt."""
        ...  # pragma: no cover - protocol


class RecordingExecutor:
    """In-memory :class:`ProviderExecutor` fake used by tests.

    Records every ``(operation, fixture)`` pair, returns ``ok=True`` receipts
    (except for ops listed in ``fail_ops``, which return ``ok=False``), and
    performs no I/O.
    """

    def __init__(
        self,
        fail_ops: tuple[str, ...] = (),
        detail: str = "recorded",
        data_by_op: dict | None = None,
    ) -> None:
        self.fail_ops = set(fail_ops)
        self.detail = detail
        # Optional {op: payload} for read-backed ops (e.g. hue.find_scene):
        # surfaced in the receipt's "data" key so orchestration layers can
        # consume provider payloads without a transport.
        self.data_by_op = dict(data_by_op) if data_by_op else {}
        self.calls: list[dict] = []

    def execute(self, operation: ProviderOperation, fixture: Fixture) -> dict:
        self.calls.append(
            {
                "provider": operation.provider,
                "op": operation.op,
                "resource_ref": operation.resource_ref,
                "payload": dict(operation.payload),
                # None for instance-scoped device control (hyperhdr); the
                # production executor never dereferences the fixture for those.
                "fixture_id": getattr(fixture, "id", None),
            }
        )
        ok = operation.op not in self.fail_ops
        detail = self.detail if ok else f"simulated failure for {operation.op}"
        result = receipt(ok, operation.provider, operation.op, detail)
        if ok and operation.op in self.data_by_op:
            result["data"] = self.data_by_op[operation.op]
        return result

    # convenience accessors -------------------------------------------------
    def ops(self) -> list[str]:
        return [call["op"] for call in self.calls]

    def calls_for(self, op: str) -> list[dict]:
        return [call for call in self.calls if call["op"] == op]


# ---------------------------------------------------------------------------
# Clock port
# ---------------------------------------------------------------------------


@runtime_checkable
class Clock(Protocol):
    """UTC timestamp source returning ISO-8601 ``Z`` strings."""

    def now_iso(self) -> str:
        ...  # pragma: no cover - protocol


class MonotonicClock:
    """Production clock: wall-clock UTC, second resolution, ``...Z`` format.

    The name refers to its role as the engine's ordered time source (event
    ordering, playback stamps); values are ISO-8601 UTC with a ``Z`` suffix,
    matching the store and event timestamp contracts.
    """

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SteppingClock:
    """Deterministic test clock: starts at ``start`` and advances one
    ``step_seconds`` per :meth:`now_iso` call, so every timestamp strictly
    increases (useful for ordering assertions)."""

    def __init__(self, start: str = "2026-09-10T12:00:00Z", step_seconds: int = 1) -> None:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
        self._current = parsed.replace(tzinfo=timezone.utc)
        self._step = timedelta(seconds=step_seconds)

    def now_iso(self) -> str:
        value = self._current.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._current += self._step
        return value


# ---------------------------------------------------------------------------
# HA automation gateway port (native routine awareness)
# ---------------------------------------------------------------------------

class HaAutomationGatewayError(Exception):
    """Raised by a :class:`HaAutomationGateway` when HA contact or a
    HA-native write fails. ``code`` is one of ``unavailable`` (capability
    absent/blocked), ``http_error`` (transport/HTTP failure), or
    ``invalid_response`` (HA answered with something unusable)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HaAutomationEntity:
    """One ``automation.*`` entity as HA reports it (state attributes only)."""

    entity_id: str
    state: str            # "on" | "off" | "unavailable" | ...
    automation_id: str | None   # attributes.id (None on legacy automations)
    alias: str | None           # attributes.friendly_name


class HaAutomationGateway(Protocol):
    """Home Assistant's own automation REST surface — the ONLY channel
    routine awareness and CRUD use. Implementations (adapter tree) perform
    the HTTP; the engine and routine service stay transport-free.

    Contract notes:

    - All methods may raise :class:`HaAutomationGatewayError`; nothing here
      is expected to swallow failures — the routine service maps them into
      honest command results.
    - No method ever touches YAML or the filesystem: this is the
      HA-supported config/state/services API only. If an installation does
      not permit API-managed automation editing, ``available()`` returns
      False and the routine service reports the capability as unavailable —
      there is no YAML fallback by design.
    """

    def available(self) -> bool:
        """Whether API-managed automation editing is possible in this runtime."""
        ...  # pragma: no cover - protocol

    def unavailable_reason(self) -> str | None:
        """Why the capability is absent (None when available)."""
        ...  # pragma: no cover - protocol

    def list_automation_entities(self) -> list[HaAutomationEntity]:
        """Every ``automation.*`` entity with its on/off state and config id."""
        ...  # pragma: no cover - protocol

    def get_automation_config(self, automation_id: str) -> dict | None:
        """The canonical stored automation config, or None when absent."""
        ...  # pragma: no cover - protocol

    def save_automation_config(self, automation_id: str, config: dict) -> None:
        """Create or replace one automation config (HA-native upsert)."""
        ...  # pragma: no cover - protocol

    def delete_automation_config(self, automation_id: str) -> None:
        """Delete one automation config (absent is acceptable)."""
        ...  # pragma: no cover - protocol

    def reload_automations(self) -> None:
        """Ask HA to reload automation state from stored config."""
        ...  # pragma: no cover - protocol

    def set_automation_enabled(self, entity_id: str, enabled: bool) -> None:
        """Enable/disable one automation entity via the automation service."""
        ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# Discovery fetcher port
# ---------------------------------------------------------------------------

Fetcher = Callable[[], "dict | None"]


@dataclass
class DiscoveryFetchers:
    """Optional provider payload sources for ``discovery.run``.

    All fields are optional; a ``None`` fetcher means the provider is not
    configured in this runtime and discovery skips it with a warning event
    (never an error). Payload shapes are exactly what the transport-free
    discovery builders expect:

    - ``fetch_hue`` / ``fetch_hue_rooms``: CLIP v2 ``{"data": [...]}``
      light/room payloads (plain CLIP v2 bridge responses);
    - ``fetch_hue_entertainment_configurations`` / ``fetch_hue_entertainment_services``:
      CLIP v2 ``{"data": [...]}`` ``entertainment_configuration`` /
      ``entertainment`` payloads, used only by contention (not discovery);
    - ``fetch_wled_info`` / ``fetch_wled_state``: WLED ``/json/info`` and
      ``/json/state`` payloads for one device;
    - ``fetch_ha_states``: ``{entity_id: {attributes...}}`` (or full HA
      state objects) for ``light.*`` entities.

    ``wled_endpoint_hint`` is the deployment's WLED host (informational
    only — endpoints are hints, never identity, contracts §2/§6).
    ``ignored_ha_entity_ids`` is an optional explicit list of HA light
    entities that must never be adopted as atomic fixtures (fallback when
    group-member metadata is missing).
    """

    fetch_hue: Fetcher | None = None
    fetch_hue_rooms: Fetcher | None = None
    fetch_hue_devices: Fetcher | None = None
    # Contention-only reads (service/contention.py): CLIP v2 entertainment
    # configuration + service resources, resolved against fetch_hue_devices
    # into the precise set of lights currently streamed by hyperHDR's
    # Entertainment API session (docs/scene_studio/CONTENTION_RUNBOOK.md).
    fetch_hue_entertainment_configurations: Fetcher | None = None
    fetch_hue_entertainment_services: Fetcher | None = None
    fetch_wled_info: Fetcher | None = None
    fetch_wled_state: Fetcher | None = None
    fetch_ha_states: Fetcher | None = None
    fetch_ha_device_metadata: Fetcher | None = None
    wled_endpoint_hint: str | None = None
    ignored_ha_entity_ids: tuple[str, ...] = ()
    # Optional deployment hint: the Hue bridge hardware id, stamped into hue
    # observation metadata so a first-run ``fixture.adopt`` (which has no
    # previous binding to fall back to) can derive a complete ``HueBinding``.
    # CLIP v2 light resources never carry it — it is bridge-level identity.
    hue_bridge_id: str | None = None
