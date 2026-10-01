"""Thin AppDaemon adapter — glue only, no domain logic (plan §9.2).

DEPLOYMENT-PENDING (integration wave 1 exit gate is mock/local-only):
live HA validation of endpoint registration, legacy event listeners, and
real provider calls is deferred to R1. The engine and all logic live in
``scene_studio.service``; this module only wires configuration, transport,
and AppDaemon callbacks.

Import safety: this module MUST import without AppDaemon installed (pure
unit tests import :class:`RequestsProviderExecutor` from here). The
``hassapi`` import is guarded at module top with a stub fallback, and
re-imported lazily inside ``SceneStudioApp.initialize`` on the real
runtime. ``requests`` is imported LAZILY inside the executor's HTTP path —
the only place in the service tree allowed to touch it (stdlib-only rule).

Configuration (AppDaemon ``apps.yaml``; ``!secret``-compatible names, never
hardcoded)::

    scene_studio:
      module: scene_studio_app
      class: SceneStudioApp
      store_root: /config/scene_studio     # default
      read_only: false                     # production: provider writes enabled
      legacy_events_enabled: false         # default; keep off while legacy apps remain
      ignored_ha_entity_ids: []            # optional HA aggregate entity ids discovery must not re-adopt
      hue_ip: !secret hue_ip
      hue_username: !secret hue_username   # Hue CLIP v2 application key
      hue_bridge_id: 0017881234            # optional bridge hardware id; stamped into hue
                                           # discovery metadata so first-run fixture.adopt on a
                                           # clean install can derive a complete HueBinding
      ha_light_enabled: true               # false opts OUT of HA-light discovery entirely
                                           # (portable profile providers.ha_light.enabled=false)
      wled_host: !secret wled_host
      # External light-sync contention (hyperHDR ownership pass). With no
      # hyperhdr_host the contention layer is configured:false and every
      # gate is a pass-through.
      hyperhdr_host: hyperhdr.local:8090   # hyperHDR JSON-RPC (serverinfo probe + instance control)
      contention_default_policy: yield     # yield (default) | takeover | ignore
      hyperhdr_probe_ttl_seconds: 5        # probe cache TTL; heartbeat runs at 3x TTL
      # Optional explicit hyperHDR instance -> provider pinning (else auto-matched
      # by instance friendly name containing "wled" / "hue"):
      # hyperhdr_wled_instance_ids: [0]
      # hyperhdr_hue_instance_ids: [1]

Registered integration surfaces:

- optional legacy events (``apply_scene_event``, ``save_light_states_event``,
  ``delete_scene_event``, ``generate_office_scene``) mapped through
  ``service/legacy.py`` then dispatched via the engine. Registration is
  independent of provider-write capability: ``normal`` mode does **not**
  imply listeners. They register only when ``legacy_events_enabled: true``
  *and* the derived runtime mode is ``normal``. Restricted modes never
  register them, even if the flag is set;
- HTTP transport: ONE RPC-style named endpoint (``ENDPOINT_NAME`` = ``"scene_studio_api"``)
  registered via ``register_endpoint`` (guarded by ``hasattr``; LIVE VALIDATION
  PENDING R1). AppDaemon 4.5's REST model (verified against the 4.5.0 tag
  sources: ``appdaemon/http.py`` ``call_app_endpoint``/``dispatch_app_endpoint``
  and ``appdaemon/adapi.py`` ``ADAPI.register_endpoint``; docs:
  https://appdaemon.readthedocs.io/en/latest/APPGUIDE.html "RESTFul API Support"):

  - The endpoint is served at ``/api/appdaemon/<endpoint_name>`` (GET and POST;
    a single path segment — the app name is NOT part of the URL).
  - ``register_endpoint(callback, endpoint_name)``; the callback receives the
    decoded JSON body (POST) or the query-string mapping (GET) as its first
    argument (plus kwargs holding the aiohttp ``request`` object).
  - The callback returns ``(json_mappable, status_code)``. CRITICAL: AppDaemon
    replaces the response body with an HTML error page for status 404
    ("App Not Found") and 500 ("An Error occurred..."), so non-200 codes cannot
    carry JSON payloads. This adapter therefore always answers HTTP 200 and
    wraps the real status inside the body: ``{"status": <code>, "body": <payload>}``.

  Because the callback cannot see the HTTP method, the transport standardizes
  on a POST JSON envelope ``{"method": "GET"|"POST", "path": "/status"|...,
  "query": {...}?, "body": {...}?}`` delegating to ``service/api.route``;
  for robustness it also accepts a plain GET whose query parameters carry
  ``path`` (remaining params become the query string).
- legacy ``call_service`` bridge: AppDaemon's signature is
  ``call_service(service, **kwargs)`` with the service named
  ``"<domain>/<service>"`` (ADAPI docstring: "The service name in the format
  ``<domain>/<service>``. For example, ``light/turn_on``") — one positional
  argument, never split into domain + action.
"""

from __future__ import annotations

import logging
import threading
import time

from ..service.api import ROUTE_PREFIX, route as http_route
from ..service.engine import SceneStudioEngine
from ..service.legacy import LEGACY_EVENT_MAPPERS, map_generate_office_scene
from ..service.policy import (
    MODE_NORMAL,
    MODE_R2_VALIDATION,
    MODE_R5_VALIDATION,
    MODE_READ_ONLY,
    MODE_REGISTRY_ADMIN,
    RuntimePolicy,
)
from ..service.ports import DiscoveryFetchers, MonotonicClock, receipt
from ..stores import SceneStudioStore
from .ui_bridge import (
    UI_COMMAND_EVENT,
    UI_PROJECTION_ENTITY,
    build_projection_state,
    build_ui_command_envelope,
    project_canonical_targets,
)

try:  # guarded: module import must succeed without appdaemon (pure tests)
    import appdaemon.plugins.hass.hassapi as hassapi  # type: ignore

    HAS_APPDAEMON = True
except ImportError:  # pragma: no cover - exercised in non-AD environments
    hassapi = None
    HAS_APPDAEMON = False

_LOG = logging.getLogger("scene_studio_adapter")

_DEFAULT_STORE_ROOT = "/config/scene_studio"


# CLIP v2 managed-scene lifecycle ops (realized against
# /clip/v2/resource/scene; see R5 plan v2 section 2.2/2.4) plus the
# read-only group/light reads managed-scene realization needs to satisfy
# the bridge's full-coverage action requirement.
_HUE_SCENE_OPS = frozenset({
    "hue.find_scene", "hue.post_scene", "hue.put_scene",
    "hue.recall_scene", "hue.delete_scene",
    "hue.get_group_lights", "hue.get_light",
    "hue.get_groups_overview", "hue.post_zone", "hue.put_zone",
})

# Named AppDaemon REST endpoint (served at /api/appdaemon/scene_studio_api).
# Unique across the AppDaemon instance; see module docstring for the verified
# AppDaemon 4.5 contract.
ENDPOINT_NAME = "scene_studio_api"


def _parse_hue_body_errors(response) -> list[dict]:
    """Structured ``errors[]`` entries from a CLIP v2 response body, at any
    HTTP status — live-verified against a real bridge, a 207 Multi-Status
    (partial failure) carries the SAME ``{"errors": [...], "data": [...]}``
    shape as a clean 200/202, each entry with a ``description`` and a
    machine-readable ``error_code`` (e.g. ``"communication_error"`` when
    the bridge accepted the request but couldn't reach the device over
    Zigbee). Never raises — a non-JSON body just yields no errors."""
    try:
        body = response.json()
    except ValueError:
        return []
    errors = body.get("errors") if isinstance(body, dict) else None
    return [item for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []


def _hue_is_communication_error(response) -> bool:
    """True when the bridge itself reports it couldn't reach the device
    over Zigbee (not a validation/auth/payload problem retrying won't
    fix) — worth a more generous retry budget since a mesh route can
    self-heal within a couple of seconds."""
    return any(item.get("error_code") == "communication_error" for item in _parse_hue_body_errors(response))


def _hue_receipt_from_response(op: str, response) -> dict:
    """CLIP v2 can return HTTP 200/202 with a non-empty ``errors`` array —
    the bridge accepted and parsed the request but rejected part of the
    payload (e.g. a field it won't write, or "communication issues" with
    the device). The status code alone reports that as a success;
    live-verified against a real bridge the body always carries
    ``{"errors": [...], "data": [...]}`` even on a clean 200. Treat a
    non-empty ``errors`` list as a failure and surface the bridge's own
    message(s), not just "HTTP 200".

    This still does not confirm the physical light executed the command —
    CLIP v2's synchronous response only reports what the BRIDGE did with
    the request, not whether the Zigbee message reached the bulb. Catching
    that requires a follow-up read-back; ``_hue_put`` does one for
    non-dynamic-native fixtures (see ``_hue_light_needs_confirmation``).
    """
    ok = response.status_code in (200, 202)
    body_errors = _parse_hue_body_errors(response)
    descriptions = [item.get("description", str(item)) for item in body_errors]
    if ok and descriptions:
        ok = False
    if descriptions:
        detail = f"HTTP {response.status_code} with bridge errors: {'; '.join(descriptions)[:200]}"
    elif ok:
        detail = f"HTTP {response.status_code}"
    else:
        detail = f"HTTP {response.status_code}: {getattr(response, 'text', '')[:120]}"
    return receipt(ok, "hue_v2", op, detail)


# Read-back confirmation (user-requested "heavier hook", live-motivated by
# a real GLEDOPTO third-party light on this bridge that intermittently
# didn't respond): applied only to hue_v2 fixtures whose registry
# capabilities report `dynamic_native: false` — third-party Zigbee Light
# Link joins, not genuine Signify/Philips Hue hardware, are the ones
# observed to occasionally drop a command the bridge itself accepted.
# Genuine native Hue bulbs skip this entirely (no added latency).
_HUE_CONFIRM_READBACK_DELAY_S = 0.35  # time for a Zigbee command to actually land before checking
# Two retry budgets, chosen by WHY the attempt failed (live-verified: a
# real GLEDOPTO light on this bridge fails with error_code
# "communication_error" on every field, consistently, not a one-off):
#  - a bridge-reported communication_error gets an escalating, more
#    generous budget — a Zigbee mesh route can self-heal within a couple
#    of seconds, so it's worth waiting longer between attempts;
#  - anything else (validation error, a confirmed-accepted write whose
#    read-back just doesn't match yet) gets one conservative retry —
#    retrying an identical payload against a REJECTED request won't
#    change the outcome, so there is no reason to spend more time on it.
_HUE_COMM_ERROR_RETRY_DELAYS_S = (0.4, 0.8, 1.5)
_HUE_DEFAULT_RETRY_DELAYS_S = (0.3,)
# Tolerances absorb legitimate bridge-side rounding/gamut remapping, not
# real mismatches — a genuinely dropped command reads as no change at all,
# far outside these margins.
_HUE_BRIGHTNESS_TOLERANCE = 1.5
_HUE_XY_TOLERANCE = 0.015
_HUE_MIREK_TOLERANCE = 2


def _hue_light_needs_confirmation(op: str, fixture) -> bool:
    """Only the actual light-state write, and only for a fixture whose
    registry capabilities explicitly report `dynamic_native: false`. An
    unknown/missing capability set (fixture is None, or capabilities were
    never assessed) is NOT treated as non-native — confirmation is opt-in
    by evidence, never the default for fixtures we simply know nothing
    about."""
    if op != "hue.put_light" or fixture is None:
        return False
    capabilities = getattr(fixture, "capabilities", None)
    return capabilities is not None and capabilities.dynamic_native is False


def _hue_light_matches_payload(payload: dict, observed: dict) -> tuple[bool, str]:
    """Compare a hue.put_light payload against the light resource read back
    from the bridge. Only checks keys actually PRESENT in the payload —
    fields the command never touched aren't verified against it."""
    mismatches: list[str] = []

    if isinstance(payload.get("on"), dict) and "on" in payload["on"]:
        wanted = payload["on"]["on"]
        got = (observed.get("on") or {}).get("on")
        if got != wanted:
            mismatches.append(f"on: wanted {wanted}, observed {got}")

    if isinstance(payload.get("dimming"), dict) and "brightness" in payload["dimming"]:
        wanted = payload["dimming"]["brightness"]
        got = (observed.get("dimming") or {}).get("brightness")
        if not isinstance(got, (int, float)) or abs(got - wanted) > _HUE_BRIGHTNESS_TOLERANCE:
            mismatches.append(f"brightness: wanted {wanted}, observed {got}")

    if isinstance(payload.get("color"), dict) and isinstance(payload["color"].get("xy"), dict):
        wanted_xy = payload["color"]["xy"]
        got_xy = (observed.get("color") or {}).get("xy") or {}
        for axis in ("x", "y"):
            wanted = wanted_xy.get(axis)
            got = got_xy.get(axis)
            if wanted is not None and (not isinstance(got, (int, float)) or abs(got - wanted) > _HUE_XY_TOLERANCE):
                mismatches.append(f"color.{axis}: wanted {wanted}, observed {got}")

    if isinstance(payload.get("color_temperature"), dict) and "mirek" in payload["color_temperature"]:
        wanted = payload["color_temperature"]["mirek"]
        got = (observed.get("color_temperature") or {}).get("mirek")
        if not isinstance(got, (int, float)) or abs(got - wanted) > _HUE_MIREK_TOLERANCE:
            mismatches.append(f"mirek: wanted {wanted}, observed {got}")

    return (not mismatches, "; ".join(mismatches))


def _derive_mode_and_allowlist(
    *,
    read_only: bool,
    r2_validation: bool,
    r5_validation: bool,
    r2_allowlist,
    r5_allowlist,
    registry_admin: bool = False,
) -> tuple[str, set | None]:
    """Runtime mode + executor allowlist from the app configuration flags.

    Validation modes win over the plain flags (they ARE the narrower, more
    intentional configuration). ``registry_admin`` wins over ``read_only``
    for command policy, but the executor is still write-disabled in both.

    Fail-closed: ``r5_validation`` without a non-empty
    ``r5_fixture_allowlist`` raises — a missing/empty R5 allowlist must never
    degrade into ``None`` (unrestricted) executor allowlisting.
    """
    if r5_validation:
        if not r5_allowlist:
            raise ValueError(
                "r5_validation requires a non-empty r5_fixture_allowlist; "
                "refusing to start with an unrestricted executor allowlist"
            )
        return MODE_R5_VALIDATION, set(r5_allowlist)
    if r2_validation:
        return MODE_R2_VALIDATION, set(r2_allowlist) if r2_allowlist else None
    if registry_admin:
        return MODE_REGISTRY_ADMIN, None
    return (MODE_READ_ONLY if read_only else MODE_NORMAL), None


def _legacy_listeners_enabled(*, mode: str, legacy_events_enabled: bool) -> bool:
    """Whether to register the legacy Scene Studio HA event consumers.

    Independent of provider-write capability: ``normal`` can execute
    Workbench commands without consuming the old events that still-deployed
    legacy apps already handle. Restricted modes never register the
    listeners, even if the config flag is true.
    """
    return bool(legacy_events_enabled) and mode == MODE_NORMAL


class RequestsProviderExecutor:
    """ProviderExecutor over HTTP — the engine's only device boundary.

    - Hue CLIP v2: ``PUT https://{hue_ip}/clip/v2/resource/light/{id}`` with
      the ``hue-application-key`` header and ``verify=False`` (parity with
      the legacy scene tooling; TLS verification is a hardening TODO for R1).
    - WLED: ``POST http://{endpoint}/json/state`` where the endpoint is
      resolved per fixture: discovery-backed device_id map (if populated),
      then the binding's ``endpoint_hint``, then the configured global
      ``wled_host`` (backward-compatible single-controller default). The
      host is routing only — WLED identity is ``WledBinding.device_id``.
    - HA lights: ``light.turn_on`` / ``light.turn_off`` through the injected
      AppDaemon ``call_service`` bridge.
    - hyperHDR: ``POST http://{hyperhdr_host}/json-rpc`` instance stop/start
      (external light-sync takeover suspend + handback). hyperHDR is the
      ONE provider this executor writes to without a fixture — instance
      control is device-scoped, not fixture-scoped.

    Never raises: every failure becomes an ``ok=False`` receipt so a scene
    apply stays best-effort across fixtures.
    """

    def __init__(
        self,
        *,
        hue_ip: str | None = None,
        hue_username: str | None = None,
        wled_endpoint: str | None = None,
        wled_endpoints: dict | None = None,
        call_service=None,
        requests_module=None,
        timeout: float = 5.0,
        read_only: bool = False,
        fixture_allowlist: set | None = None,
        sleep_fn=None,
        hyperhdr_host: str | None = None,
    ) -> None:
        self._hue_ip = hue_ip
        self._hue_username = hue_username
        self._read_only = read_only
        # Injectable so tests exercising the confirm/retry loop don't
        # actually block on wall-clock sleeps (production default: time.sleep).
        self._sleep = sleep_fn or time.sleep
        self._fixture_allowlist = set(fixture_allowlist) if fixture_allowlist else None
        self._wled_endpoint = wled_endpoint  # backward-compatible global default (single-controller deployments)
        # Discovery seam: optional device_id -> endpoint map, ahead of the
        # global default. Populated later from discovery runs without any
        # registry-subsystem change.
        self._wled_endpoints = dict(wled_endpoints) if wled_endpoints else {}
        self._call_service = call_service
        self._requests = requests_module  # injectable for tests; lazily imported otherwise
        self._timeout = timeout
        self._hyperhdr_host = hyperhdr_host
        self._lock = threading.Lock()

    # -- ProviderExecutor port -----------------------------------------

    def execute(self, operation, fixture) -> dict:
        if operation.op == "hue.put_scene_dynamic":
            # This is a renderer intent marker, not a CLIP light endpoint.
            # PlaybackRealizer must consume it into managed scene operations.
            return receipt(False, "hue_v2", operation.op, "hue.put_scene_dynamic requires managed-scene realization")
        if self._read_only and operation.op not in (
            "hue.find_scene", "hue.get_group_lights", "hue.get_light",
            "hue.get_groups_overview",
        ):
            # Defense in depth: in R1/read-only mode EVERY provider operation
            # is refused before any transport is touched, so a mutating
            # command that slips past the command gate still cannot reach a
            # device. (All operations defined today are writes.)
            return receipt(False, operation.provider, operation.op, "read_only mode: provider writes disabled")
        if self._fixture_allowlist is not None and getattr(fixture, "id", None) not in self._fixture_allowlist:
            return receipt(
                False,
                operation.provider,
                operation.op,
                f"fixture {getattr(fixture, 'id', '?')!r} is not in the R2 validation allowlist; write refused",
            )
        try:
            with self._lock:
                if operation.provider == "hue_v2":
                    if operation.op in _HUE_SCENE_OPS:
                        return self._hue_scene_op(operation)
                    return self._hue_put(operation, fixture)
                if operation.provider == "wled":
                    return self._wled_post(operation, fixture)
                if operation.provider == "ha_light":
                    return self._ha_call(operation)
                if operation.provider == "hyperhdr":
                    return self._hyperhdr_op(operation)
            return receipt(False, operation.provider, operation.op, f"unknown provider {operation.provider!r}")
        except Exception as exc:  # transport or unexpected failure
            return receipt(False, operation.provider, operation.op, f"{type(exc).__name__}: {exc}")

    # -- transports ------------------------------------------------------

    def _http(self):
        # A persistent requests.Session (not the bare `requests` module) so
        # repeated writes to the same host (a scene apply's several Hue
        # lights, back-to-back identify taps, ...) reuse one pooled TCP/TLS
        # connection instead of paying a full handshake per call. Module-
        # level requests.put/post() each spin up and tear down their own
        # throwaway Session internally — live-measured against a real Hue
        # bridge, that produced an alternating ~0.2s/~1.0s pattern (fresh
        # TLS handshake every other call) for identical identify calls.
        # Test-injected fakes (requests_module=...) are used as-is; they
        # already model a persistent object across calls.
        if self._requests is None:
            import requests  # lazy: only needed on live execution paths

            self._requests = requests.Session()
        return self._requests

    def _hue_put(self, operation, fixture=None) -> dict:
        if not self._hue_ip or not self._hue_username:
            return receipt(False, "hue_v2", operation.op, "hue_ip/hue_username not configured")
        url = f"https://{self._hue_ip}/clip/v2/resource/light/{operation.resource_ref}"
        headers = {"hue-application-key": self._hue_username, "Content-Type": "application/json"}

        confirm = _hue_light_needs_confirmation(operation.op, fixture)
        result = None
        # Populated on the FIRST failure, classified by what kind of
        # failure it was; consumed one delay per subsequent retry. Carried
        # across the PUT-failure and confirm-mismatch branches on purpose —
        # once this exchange has shown signs of a flaky link, later
        # failures (whichever branch they show up in) get the same
        # generous budget rather than resetting to the conservative one.
        retry_delays: list[float] = []
        attempt = 0
        while True:
            attempt += 1
            response = self._http().put(
                url, json=operation.payload, headers=headers, verify=False, timeout=self._timeout
            )
            result = _hue_receipt_from_response(operation.op, response)
            if not result["ok"]:
                if not confirm:
                    return result
                if attempt == 1:
                    retry_delays = list(
                        _HUE_COMM_ERROR_RETRY_DELAYS_S if _hue_is_communication_error(response) else _HUE_DEFAULT_RETRY_DELAYS_S
                    )
                if not retry_delays:
                    return result
                self._sleep(retry_delays.pop(0))
                continue

            if not confirm:
                return result

            # The bridge accepted the write; give the Zigbee command time to
            # actually land, then read the light back and compare against
            # what we asked for. A non-native fixture that silently dropped
            # the command still comes back HTTP 200/errors:[] from the PUT
            # itself — this is the only way to catch that.
            self._sleep(_HUE_CONFIRM_READBACK_DELAY_S)
            observed = self._hue_get_light(operation.resource_ref)
            if observed is None:
                result = receipt(False, "hue_v2", operation.op, f"{result['detail']}; read-back for confirmation failed")
            else:
                matched, mismatch_detail = _hue_light_matches_payload(operation.payload, observed)
                if matched:
                    return receipt(True, "hue_v2", operation.op, f"{result['detail']}; confirmed by read-back")
                result = receipt(
                    False, "hue_v2", operation.op,
                    f"{result['detail']}; not confirmed by read-back ({mismatch_detail})",
                )
            if attempt == 1:
                retry_delays = list(_HUE_DEFAULT_RETRY_DELAYS_S)
            if not retry_delays:
                return result
            self._sleep(retry_delays.pop(0))

    def _hue_get_light(self, resource_ref: str) -> dict | None:
        """Read one light resource back for confirmation. Never raises —
        any failure here just means confirmation couldn't be established,
        handled by the caller as an honest 'unconfirmed' outcome."""
        url = f"https://{self._hue_ip}/clip/v2/resource/light/{resource_ref}"
        headers = {"hue-application-key": self._hue_username}
        try:
            response = self._http().get(url, headers=headers, verify=False, timeout=self._timeout)
        except Exception:
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None


    def _hue_scene_op(self, operation) -> dict:
        """Realize hue.find_scene / post_scene / put_scene / recall_scene /
        delete_scene against the bridge's CLIP v2 scene resource."""
        if not self._hue_ip or not self._hue_username:
            return receipt(False, "hue_v2", operation.op, "hue_ip/hue_username not configured")
        headers = {"hue-application-key": self._hue_username, "Content-Type": "application/json"}
        base = f"https://{self._hue_ip}/clip/v2/resource/scene"
        http = self._http()
        data_payload = None

        if operation.op == "hue.find_scene":
            response = http.get(base, headers=headers, verify=False, timeout=self._timeout)
            ok = response.status_code == 200
            if ok:
                try:
                    scenes = response.json().get("data", [])
                except Exception:
                    scenes = []
                wanted = operation.payload.get("name") if isinstance(operation.payload, dict) else None
                wanted_group = operation.payload.get("group") if isinstance(operation.payload, dict) else None
                def is_match(scene):
                    if not isinstance(scene, dict) or scene.get("metadata", {}).get("name") != wanted:
                        return False
                    if not isinstance(wanted_group, dict):
                        return True
                    group = scene.get("group") if isinstance(scene.get("group"), dict) else {}
                    return group.get("rid") == wanted_group.get("rid") and group.get("rtype") == wanted_group.get("rtype")
                match = next((scene for scene in scenes if is_match(scene)), None)
                data_payload = {"resource_id": match.get("id") if match else None, "matched": match is not None}
            detail = f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}")
            result = receipt(ok, "hue_v2", operation.op, detail)
            if ok:
                result["data"] = data_payload or {"resource_id": None, "matched": False}
            return result

        if operation.op == "hue.post_scene":
            response = http.post(base, json=operation.payload, headers=headers, verify=False, timeout=self._timeout)
            ok = response.status_code in (200, 201)
            if ok:
                try:
                    created = response.json().get("data", [])
                    data_payload = {"resource_id": created[0].get("rid")} if created else {}
                except Exception:
                    data_payload = {}
            detail = f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}")
            result = receipt(ok, "hue_v2", operation.op, detail)
            if ok and data_payload:
                result["data"] = data_payload
            return result

        if operation.op in ("hue.put_scene", "hue.recall_scene"):
            response = http.put(
                f"{base}/{operation.resource_ref}",
                json=operation.payload, headers=headers, verify=False, timeout=self._timeout,
            )
            ok = response.status_code in (200, 202)
            return receipt(
                ok, "hue_v2", operation.op,
                f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}"),
            )

        if operation.op == "hue.delete_scene":
            response = http.delete(
                f"{base}/{operation.resource_ref}", headers=headers, verify=False, timeout=self._timeout
            )
            ok = response.status_code in (200, 202, 404)  # 404 = already gone: fine for cleanup
            return receipt(ok, "hue_v2", operation.op, f"HTTP {response.status_code}")

        if operation.op == "hue.get_group_lights":
            # Read-only: the light rids belonging to a room/zone. The bridge
            # REQUIRES managed scene actions to cover every group light
            # (partial coverage -> 400 "Light action targets not matching
            # lights in referenced group"), so realization needs the full
            # membership. group/{rid}.children are DEVICE rids; map them to
            # light services through /device.
            # CLIP v2 has no /group resource: rooms and zones live under
            # their own paths keyed by the binding's hue_group_type.
            group_rtype = "zone"
            if isinstance(operation.payload, dict):
                group_rtype = operation.payload.get("group", {}).get("rtype", "room") or "room"
            if group_rtype not in ("room", "zone"):
                return receipt(False, "hue_v2", operation.op,
                               f"unsupported hue_group_type {group_rtype!r}")
            group_response = http.get(
                f"https://{self._hue_ip}/clip/v2/resource/{group_rtype}/{operation.resource_ref}",
                headers=headers, verify=False, timeout=self._timeout,
            )
            if group_response.status_code != 200:
                return receipt(False, "hue_v2", operation.op,
                               f"HTTP {group_response.status_code}: {getattr(group_response, 'text', '')[:120]}")
            device_response = http.get(
                f"https://{self._hue_ip}/clip/v2/resource/device",
                headers=headers, verify=False, timeout=self._timeout,
            )
            if device_response.status_code != 200:
                return receipt(False, "hue_v2", operation.op,
                               f"HTTP {device_response.status_code}: {getattr(device_response, 'text', '')[:120]}")
            try:
                group = group_response.json().get("data", [{}])[0]
                device_rids = {c.get("rid") for c in group.get("children", []) if isinstance(c, dict)}
                light_rids = []
                for device in device_response.json().get("data", []):
                    if device.get("id") not in device_rids:
                        continue
                    for service in device.get("services", []):
                        if service.get("rtype") == "light" and service.get("rid"):
                            light_rids.append(service["rid"])
            except Exception as exc:
                return receipt(False, "hue_v2", operation.op, f"payload error: {exc}")
            result = receipt(True, "hue_v2", operation.op, f"group {operation.resource_ref}: {len(light_rids)} light(s)")
            result["data"] = {"light_rids": light_rids}
            return result

        if operation.op == "hue.get_groups_overview":
            # Read-only: every room/zone with its light-service membership
            # plus the light->device map (zone create/repair payloads use
            # device children). Three GETs, no state change.
            rooms = http.get(f"https://{self._hue_ip}/clip/v2/resource/room",
                             headers=headers, verify=False, timeout=self._timeout)
            zones = http.get(f"https://{self._hue_ip}/clip/v2/resource/zone",
                             headers=headers, verify=False, timeout=self._timeout)
            devices = http.get(f"https://{self._hue_ip}/clip/v2/resource/device",
                               headers=headers, verify=False, timeout=self._timeout)
            if rooms.status_code != 200 or zones.status_code != 200 or devices.status_code != 200:
                return receipt(False, "hue_v2", operation.op,
                               f"HTTP room={rooms.status_code} zone={zones.status_code} device={devices.status_code}")
            try:
                groups = []
                light_to_device = {}
                for collection, rtype in ((rooms, "room"), (zones, "zone")):
                    for g in collection.json().get("data", []):
                        groups.append({
                            "rid": g.get("id"), "rtype": rtype,
                            "name": (g.get("metadata") or {}).get("name"),
                            "children": [c.get("rid") for c in g.get("children", []) if isinstance(c, dict)],
                        })
                for d in devices.json().get("data", []):
                    for s in d.get("services", []):
                        if s.get("rtype") == "light" and s.get("rid"):
                            light_to_device[s["rid"]] = d.get("id")
                for g in groups:
                    g["light_rids"] = sorted(
                        lid for lid, did in light_to_device.items() if did in set(g["children"])
                    )
                    del g["children"]
            except Exception as exc:
                return receipt(False, "hue_v2", operation.op, f"payload error: {exc}")
            result = receipt(True, "hue_v2", operation.op,
                             f"{len(groups)} group(s), {len(light_to_device)} light(s)")
            result["data"] = {"groups": groups, "light_to_device": light_to_device}
            return result

        if operation.op in ("hue.post_zone", "hue.put_zone"):
            # Zone create / membership repair. Ownership to call these is
            # enforced by the realizer (deterministic SS-Z label + exact
            # fingerprint); the endpoint itself is CLIP v2 /resource/zone.
            zone_base = f"https://{self._hue_ip}/clip/v2/resource/zone"
            if operation.op == "hue.post_zone":
                response = http.post(zone_base, json=operation.payload, headers=headers,
                                     verify=False, timeout=self._timeout)
                ok = response.status_code in (200, 201)
                data_payload = None
                if ok:
                    try:
                        created = response.json().get("data", [])
                        data_payload = {"zone_rid": created[0].get("rid")} if created else {}
                    except Exception:
                        data_payload = {}
                result = receipt(ok, "hue_v2", operation.op,
                                 f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}"))
                if ok and data_payload:
                    result["data"] = data_payload
                return result
            response = http.put(f"{zone_base}/{operation.resource_ref}", json=operation.payload,
                                headers=headers, verify=False, timeout=self._timeout)
            ok = response.status_code in (200, 202)
            return receipt(ok, "hue_v2", operation.op,
                           f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}"))

        if operation.op == "hue.get_light":
            # Read-only: the full current light resource (used to build
            # leave-unchanged anchors for non-participating group lights).
            response = http.get(
                f"https://{self._hue_ip}/clip/v2/resource/light/{operation.resource_ref}",
                headers=headers, verify=False, timeout=self._timeout,
            )
            ok = response.status_code == 200
            result = receipt(ok, "hue_v2", operation.op, f"HTTP {response.status_code}")
            if ok:
                try:
                    result["data"] = {"resource": response.json().get("data", [{}])[0]}
                except Exception as exc:
                    return receipt(False, "hue_v2", operation.op, f"payload error: {exc}")
            return result

        return receipt(False, "hue_v2", operation.op, f"unknown hue scene op {operation.op!r}")

    @staticmethod
    def _normalize_host(endpoint: str) -> str:
        """Accept a bare host or a URL hint; return the host for
        ``http://{host}/json/state``. The hint is an address, never identity."""
        host = endpoint.strip().rstrip("/")
        for scheme in ("http://", "https://"):
            if host.startswith(scheme):
                return host[len(scheme):]
        return host

    def _wled_endpoint_for(self, fixture) -> str | None:
        """Resolve the WLED host for one fixture.

        Precedence: discovery-backed device_id map (when populated), then the
        binding's ``endpoint_hint`` (per-device address hint), then the
        configured global ``wled_host`` (backward-compatible default for
        single-controller deployments). The host is routing information only
        — WLED identity is ``WledBinding.device_id``.
        """
        binding = getattr(fixture, "binding", None)
        device_id = getattr(binding, "device_id", None)
        if device_id and device_id in self._wled_endpoints:
            return self._normalize_host(self._wled_endpoints[device_id])
        hint = getattr(binding, "endpoint_hint", None)
        if hint:
            return self._normalize_host(hint)
        return self._wled_endpoint

    def _wled_post(self, operation, fixture) -> dict:
        endpoint = self._wled_endpoint_for(fixture)
        if not endpoint:
            return receipt(False, "wled", operation.op, f"no WLED endpoint resolvable for fixture {fixture.id!r}")
        url = f"http://{endpoint}/json/state"
        response = self._http().post(url, json=operation.payload, timeout=self._timeout)
        ok = response.status_code == 200
        return receipt(
            ok,
            "wled",
            operation.op,
            f"HTTP {response.status_code}" + ("" if ok else f": {getattr(response, 'text', '')[:120]}"),
        )

    def _ha_call(self, operation) -> dict:
        if self._call_service is None:
            return receipt(False, "ha_light", operation.op, "no call_service bridge configured")
        service = "light/turn_off" if operation.op == "ha.call_light_off" else "light/turn_on"
        self._call_service(service, **dict(operation.payload))
        return receipt(True, "ha_light", operation.op, f"called {service}")

    def _hyperhdr_op(self, operation) -> dict:
        """hyperHDR instance stop/start against the JSON-RPC HTTP endpoint
        (external light-sync takeover suspend + handback). These are WRITE
        ops: the read-only gate above already refuses them in restricted
        runtimes. hyperHDR v2x answers ``{"success": bool}`` — a 200 with
        ``success: false`` (e.g. instance already stopped) is an honest
        failure receipt, never coerced to ok."""
        if not self._hyperhdr_host:
            return receipt(False, "hyperhdr", operation.op, "hyperhdr_host not configured")
        subcommand = {
            "hyperhdr.stop_instance": "stopInstance",
            "hyperhdr.start_instance": "startInstance",
        }.get(operation.op)
        if subcommand is None:
            return receipt(False, "hyperhdr", operation.op, f"unknown hyperhdr op {operation.op!r}")
        payload = {"command": "instance", "subcommand": subcommand, "tan": 1}
        if isinstance(operation.payload, dict):
            payload.update(operation.payload)
        response = self._http().post(
            f"http://{self._hyperhdr_host}/json-rpc", json=payload, timeout=self._timeout
        )
        ok = response.status_code == 200
        detail = f"HTTP {response.status_code}"
        if ok:
            try:
                body = response.json()
            except ValueError:
                return receipt(False, "hyperhdr", operation.op, "HTTP 200 with a non-JSON body")
            body_ok = body.get("success") is True if isinstance(body, dict) else False
            detail = f"HTTP {response.status_code}; success={body.get('success') if isinstance(body, dict) else None}"
            if not body_ok and isinstance(body, dict) and body.get("error"):
                detail += f": {str(body.get('error'))[:160]}"
            ok = body_ok
        else:
            detail += f": {getattr(response, 'text', '')[:120]}"
        return receipt(ok, "hyperhdr", operation.op, detail)


def _http_get_json(url: str, headers: dict | None = None, timeout: float = 5.0, verify: bool = True):
    """GET-only JSON fetch used by discovery read paths.

    Returns the decoded JSON payload, or ``None`` on any failure (the caller
    logs and discovery skips the provider). This helper performs GET requests
    ONLY — it is the sole network path discovery is allowed to use.
    """
    try:
        import requests  # lazy, same rule as the executor

        response = requests.get(url, headers=headers or {}, timeout=timeout, verify=verify)
        if response.status_code != 200:
            _LOG.warning("GET %s -> HTTP %s", url, response.status_code)
            return None
        return response.json()
    except Exception as exc:
        _LOG.warning("GET %s failed: %s: %s", url, type(exc).__name__, exc)
        return None


def _build_discovery_fetchers(
    app,
    hue_ip: str | None,
    hue_username: str | None,
    wled_host: str | None,
    timeout: float,
    ignored_ha_entity_ids: tuple[str, ...] = (),
    hue_bridge_id: str | None = None,
    ha_light_enabled: bool = True,
):
    """Wire :class:`DiscoveryFetchers` to real GET-only provider reads.

    - Hue CLIP v2: GET ``/clip/v2/resource/light``, ``/resource/room``, and
      optional ``/resource/device`` evidence.
      (same ``verify=False`` parity as the legacy tooling; hardening is an
      R1+ TODO).
    - WLED: GET ``/json/info`` for the device identity block and GET
      ``/json`` for the state blob. The live controller nests state under
      ``"state"`` with ``effects``/``palettes`` catalogs at the root; the
      fetcher flattens ``seg`` + catalogs into one dict, which is the shape
      ``discovery.build_wled_observations`` expects. Single-controller for
      this deployment; multi-device extends the fetcher set later without
      touching the per-device routing in :class:`RequestsProviderExecutor`.
    - HA: ``get_state("light")`` through the AppDaemon plugin (a read).

    No function here issues a write; discovery must never mutate bindings
    (contracts §6).
    """
    hue_headers = {"hue-application-key": hue_username} if hue_username else None

    def fetch_hue():
        if not hue_ip or not hue_username:
            return None
        return _http_get_json(
            f"https://{hue_ip}/clip/v2/resource/light", headers=hue_headers, timeout=timeout, verify=False
        )

    def fetch_hue_rooms():
        if not hue_ip or not hue_username:
            return None
        return _http_get_json(
            f"https://{hue_ip}/clip/v2/resource/room", headers=hue_headers, timeout=timeout, verify=False
        )

    def fetch_hue_devices():
        if not hue_ip or not hue_username:
            return None
        return _http_get_json(
            f"https://{hue_ip}/clip/v2/resource/device", headers=hue_headers, timeout=timeout, verify=False
        )

    def fetch_hue_entertainment_configurations():
        if not hue_ip or not hue_username:
            return None
        return _http_get_json(
            f"https://{hue_ip}/clip/v2/resource/entertainment_configuration",
            headers=hue_headers, timeout=timeout, verify=False,
        )

    def fetch_hue_entertainment_services():
        if not hue_ip or not hue_username:
            return None
        return _http_get_json(
            f"https://{hue_ip}/clip/v2/resource/entertainment", headers=hue_headers, timeout=timeout, verify=False
        )

    def fetch_wled_info():
        if not wled_host:
            return None
        return _http_get_json(f"http://{wled_host}/json/info", timeout=timeout)

    def fetch_wled_state():
        if not wled_host:
            return None
        payload = _http_get_json(f"http://{wled_host}/json", timeout=timeout)
        if not isinstance(payload, dict):
            return payload
        if isinstance(payload.get("state"), dict):
            # Live full-/json shape: flatten state + root catalogs for the builder.
            flat = dict(payload["state"])
            flat["effects"] = payload.get("effects")
            flat["palettes"] = payload.get("palettes")
            return flat
        return payload  # already-flat state blob

    def fetch_ha_states():
        if not ha_light_enabled:
            # Deployment opted out of HA-light discovery (profile
            # providers.ha_light.enabled=false): report "not configured" so
            # discovery SKIPS the provider instead of enumerating lights.
            # Hue name enrichment (an optional read of the same payload) is
            # omitted with it.
            return None
        try:
            return app.get_state("light")
        except Exception as exc:
            _LOG.warning("get_state(light) failed: %s: %s", type(exc).__name__, exc)
            return None

    def fetch_ha_device_metadata():
        """Optional deployment-provided entity/device evidence.

        AppDaemon's state API does not expose HA's device registry.  A
        deployment may provide a read-only mapping in the app args, while
        installations without it remain fully functional state-only.
        """
        raw = getattr(app, "args", {}).get("ha_device_metadata") if isinstance(getattr(app, "args", {}), dict) else None
        return raw if isinstance(raw, dict) else None

    return DiscoveryFetchers(
        fetch_hue=fetch_hue,
        fetch_hue_rooms=fetch_hue_rooms,
        fetch_hue_devices=fetch_hue_devices,
        fetch_hue_entertainment_configurations=fetch_hue_entertainment_configurations,
        fetch_hue_entertainment_services=fetch_hue_entertainment_services,
        fetch_wled_info=fetch_wled_info,
        fetch_wled_state=fetch_wled_state,
        fetch_ha_states=fetch_ha_states,
        fetch_ha_device_metadata=fetch_ha_device_metadata,
        wled_endpoint_hint=wled_host,
        ignored_ha_entity_ids=tuple(ignored_ha_entity_ids),
        hue_bridge_id=hue_bridge_id,
    )


def _http_post_json(url: str, payload: dict, timeout: float = 5.0):
    """POST-only JSON fetch for read-shaped control probes.

    hyperHDR's ``serverinfo`` only exists behind its JSON-RPC POST endpoint
    (v2x serves no GET equivalent), so the contention probe needs a POST
    that mutates nothing. Returns the decoded body, or ``None`` on any
    failure (the engine treats an unavailable probe as fail-open).
    """
    try:
        import requests  # lazy, same rule as every transport here

        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code != 200:
            _LOG.warning("POST %s -> HTTP %s", url, response.status_code)
            return None
        return response.json()
    except Exception as exc:
        _LOG.warning("POST %s failed: %s: %s", url, type(exc).__name__, exc)
        return None


def _build_hyperhdr_probe(host: str | None, timeout: float):
    """Wire the engine's hyperHDR probe port (serverinfo ``info`` dict)."""
    if not host:
        return None

    def probe():
        body = _http_post_json(
            f"http://{host}/json-rpc", {"command": "serverinfo", "tan": 1}, timeout=timeout
        )
        if not isinstance(body, dict):
            return None
        info = body.get("info")
        return info if isinstance(info, dict) else None

    return probe


def _int_list_arg(value) -> list[int] | None:
    """apps.yaml ``[0]``-style instance lists (or a scalar) -> list[int]."""
    if value is None:
        return None
    if not isinstance(value, list):
        value = [value]
    out = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool):
            out.append(item)
    return out


class _TransportError(Exception):
    """Malformed transport envelope; carries an HTTP-style status + error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error_payload(code: str, message: str) -> dict:
    """Command-envelope-style error body, matching ``service/api.py``'s shape."""
    return {"command": None, "ok": False, "error": {"code": code, "message": message}}


def _coerce_mapping(ad_args):
    """Normalize the callback argument to a plain dict (or None).

    AppDaemon hands the callback decoded JSON (any type) for POST and an
    aiohttp MultiDict (a mapping, but not a dict) for GET.
    """
    if isinstance(ad_args, dict):
        return ad_args
    items = getattr(ad_args, "items", None)
    if callable(items):
        try:
            return dict(items())
        except Exception:
            return None
    return None


def _parse_transport_envelope(ad_args) -> tuple[str, str, dict | None, dict]:
    """Decode the callback argument into ``route()``'s (method, path, body, query).

    Raises :class:`_TransportError` (mapped to a 400-style body by the caller)
    for anything that is not a usable envelope. Route-relative paths are
    prefixed with :data:`ROUTE_PREFIX` so callers can send ``"/status"``.
    """
    args = _coerce_mapping(ad_args)
    if args is None:
        raise _TransportError(
            400, "validation_error", "endpoint payload must be a JSON object envelope"
        )

    raw_path = args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _TransportError(
            400, "validation_error", "envelope requires a string 'path' (e.g. \"/status\")"
        )
    cleaned_path = raw_path.strip()
    path = (
        cleaned_path
        if cleaned_path.startswith(ROUTE_PREFIX)
        else ROUTE_PREFIX + "/" + cleaned_path.lstrip("/")
    )

    explicit_method = args.get("method")
    if explicit_method is not None:
        # Standard POST JSON envelope: method is explicit, query/body are
        # structured members of the envelope.
        if not isinstance(explicit_method, str) or explicit_method.upper() not in ("GET", "POST"):
            raise _TransportError(
                400, "validation_error", "envelope 'method' must be \"GET\" or \"POST\""
            )
        method = explicit_method.upper()
        query_value = args.get("query")
        query = dict(query_value) if isinstance(query_value, dict) else {}
    else:
        # GET query fallback: 'path' selects the route; the remaining query
        # params (string-valued) become the query mapping. Method forced GET.
        method = "GET"
        query = {key: value for key, value in args.items() if key != "path"}

    body_value = args.get("body")
    body = dict(body_value) if isinstance(body_value, dict) else None
    return method, path, body, query


def _appdaemon_base():
    """Real ``hassapi.Hass`` when available, else an import-safe stub base."""
    if hassapi is not None:
        return hassapi.Hass

    class _HassStub:  # pragma: no cover - only used without AppDaemon
        """Minimal stand-in so pure tests can import/instantiate the class.

        Signatures mirror the real AppDaemon 4.5 API:
        ``register_endpoint(callback, endpoint=None)`` (ADAPI) and
        ``call_service(service, **kwargs)`` (service is ``"<domain>/<service>"``).
        """

        args: dict = {}

        def log(self, message, *args, **kwargs):
            _LOG.info(str(message))

        def error(self, message, *args, **kwargs):
            _LOG.error(str(message))

        def listen_event(self, callback, event_name, **kwargs):
            _LOG.debug("stub listen_event(%s)", event_name)

        def register_endpoint(self, callback, endpoint=None, **kwargs):
            _LOG.debug("stub register_endpoint(%s)", endpoint)

        def call_service(self, service, **kwargs):
            _LOG.debug("stub call_service(%s)", service)

        def set_state(self, entity_id, state=None, attributes=None, **kwargs):
            _LOG.debug("stub set_state(%s)", entity_id)

    return _HassStub


class SceneStudioApp(_appdaemon_base()):
    """AppDaemon application wiring the engine into Home Assistant.

    Class definition uses a stub base when AppDaemon is absent so the module
    imports cleanly; the real ``hassapi.Hass`` base is only needed at runtime.
    """

    def initialize(self):  # pragma: no cover - requires the AppDaemon runtime
        import appdaemon.plugins.hass.hassapi as hass  # lazy re-import on the live runtime

        args = dict(getattr(self, "args", {}) or {})
        store_root = args.get("store_root") or _DEFAULT_STORE_ROOT
        read_only = bool(args.get("read_only", False))
        registry_admin = bool(args.get("registry_admin", False))
        legacy_events_enabled = bool(args.get("legacy_events_enabled", False))
        self._read_only = read_only
        self._registry_admin = registry_admin
        self._legacy_events_enabled = legacy_events_enabled
        # R2 validation-write mode: narrowly scoped provider writes for
        # explicitly allowlisted fixtures only. Legacy listeners stay
        # disabled and the engine enforces the command policy (backend-owned
        # RuntimePolicy, exposed through status().runtime).
        r2_validation = bool(args.get("r2_validation", False))
        r5_validation = bool(args.get("r5_validation", False))
        r2_allowlist = args.get("r2_fixture_allowlist") or []
        r5_allowlist = args.get("r5_fixture_allowlist") or []
        self._r2_validation = r2_validation
        self._r5_validation = r5_validation
        # Restricted validation modes (r2/r5) keep their own fixture
        # allowlist for the executor. Legacy listeners are a separate flag.
        mode, allowlist = _derive_mode_and_allowlist(
            read_only=read_only,
            r2_validation=r2_validation,
            r5_validation=r5_validation,
            r2_allowlist=r2_allowlist,
            r5_allowlist=r5_allowlist,
            registry_admin=registry_admin,
        )
        legacy_listeners_on = _legacy_listeners_enabled(
            mode=mode, legacy_events_enabled=legacy_events_enabled
        )
        executor_read_only = read_only or registry_admin or mode in {MODE_READ_ONLY, MODE_REGISTRY_ADMIN}
        # External light-sync contention (hyperHDR ownership pass): the
        # probe host opts the deployment in; with no hyperhdr_host the
        # contention layer reports configured:false and never gates.
        hyperhdr_host = args.get("hyperhdr_host")
        hyperhdr_probe = _build_hyperhdr_probe(hyperhdr_host, timeout=3.0)
        contention_config = {
            "default_policy": args.get("contention_default_policy") or "yield",
            "ttl_seconds": float(args.get("hyperhdr_probe_ttl_seconds") or 5.0),
            "wled_instance_ids": _int_list_arg(args.get("hyperhdr_wled_instance_ids")),
            "hue_instance_ids": _int_list_arg(args.get("hyperhdr_hue_instance_ids")),
        }
        self._engine = SceneStudioEngine(
            store=SceneStudioStore(store_root),
            executor=RequestsProviderExecutor(
                hue_ip=args.get("hue_ip"),
                hue_username=args.get("hue_username"),
                wled_endpoint=args.get("wled_host"),
                call_service=self._bridge_call_service,
                read_only=executor_read_only,
                fixture_allowlist=allowlist,
                hyperhdr_host=hyperhdr_host,
            ),
            clock=MonotonicClock(),
            discovery_fetchers=_build_discovery_fetchers(
                self,
                hue_ip=args.get("hue_ip"),
                hue_username=args.get("hue_username"),
                wled_host=args.get("wled_host"),
                timeout=5.0,
                ignored_ha_entity_ids=tuple(
                    item for item in (args.get("ignored_ha_entity_ids") or []) if isinstance(item, str)
                ),
                hue_bridge_id=args.get("hue_bridge_id"),
                ha_light_enabled=args.get("ha_light_enabled", True) is not False,
            ),
            policy=RuntimePolicy.build(mode),
            hyperhdr_probe=hyperhdr_probe,
            contention_config=contention_config,
        )
        if mode == MODE_NORMAL:
            # Contention heartbeat (external owner grab/surrender transitions,
            # surrender auto-restore) + stale-frz reconciliation. Scheduled
            # only in normal mode: restricted runtimes never perform
            # autonomous provider writes.
            try:
                from datetime import datetime, timedelta

                self.run_every(
                    self._contention_heartbeat,
                    datetime.now() + timedelta(seconds=20),
                    int(max(5, contention_config["ttl_seconds"]) * 3),
                )
                self.run_every(
                    self._wled_freeze_reconcile_tick,
                    datetime.now() + timedelta(seconds=30),
                    60,
                )
            except Exception as exc:
                self.error(f"contention heartbeat scheduling failed: {type(exc).__name__}: {exc}")
        if read_only or registry_admin:
            # Registry administration and read_only both keep legacy event
            # listeners off (they are MUTATING provider entry points).
            # Provider writes are additionally blocked in the executor.
            label = "REGISTRY ADMIN" if registry_admin and not read_only else "READ ONLY (R1)"
            self.log(
                f"SceneStudioApp initialized {label}: no legacy event "
                "listeners; provider writes are disabled"
            )
        elif r5_validation:
            # R5 validation-write: legacy listeners stay off; the command
            # policy is r5_validation (playback lifecycle + scene.apply +
            # read-only base) and the executor writes ONLY to the allowlisted
            # fixtures. The allowlist is non-empty by construction (the mode
            # derivation above refuses r5 without one).
            self.log(
                "SceneStudioApp initialized R5 VALIDATION: legacy listeners "
                "off; command policy r5_validation; provider writes limited "
                f"to allowlist {sorted(allowlist)}"
            )
        elif r2_validation:
            # R2 validation-write: legacy listeners stay off; the executor
            # only writes to the allowlisted fixtures and the gate only
            # opens scene.apply (plus the read-only surface).
            self.log(
                "SceneStudioApp initialized R2 VALIDATION: legacy listeners off; "
                f"provider writes limited to allowlist {sorted(allowlist) if allowlist else '[]'}"
            )
        else:
            self.log(
                "SceneStudioApp initialized NORMAL: command policy normal; "
                "provider writes enabled; legacy listeners "
                f"{'on' if legacy_listeners_on else 'off'}"
            )
        if legacy_listeners_on:
            for event_name, mapper in LEGACY_EVENT_MAPPERS.items():
                self.listen_event(self._on_legacy_event, event_name)
            self.listen_event(self._on_generate_office_scene, "generate_office_scene")
        if hasattr(self, "register_endpoint"):
            # One RPC-style named endpoint; AppDaemon serves it beneath
            # /api/appdaemon/scene_studio_api (see module docstring).
            self.register_endpoint(self._serve_endpoint, ENDPOINT_NAME)
        # Compact HA-native control-surface bridge (HA card rework plan §4):
        # registered unconditionally, independent of legacy_events_enabled —
        # this is a NEW canonical seam, never the old APPLY_SCENE/DELETE_SCENE
        # listeners. The bridge's own allowlist (ui_bridge.UI_BRIDGE_ALLOWLIST)
        # plus the engine's RuntimePolicy gate what it can actually do in any
        # given runtime mode; registering the listener itself is always safe.
        self._last_projected_revision = None
        self._last_ui_command = None
        self.listen_event(self._on_ui_command, UI_COMMAND_EVENT)
        self._publish_projection(force=True)
        self.log(
            f"SceneStudioApp initialized (store_root={store_root}, mode={mode}, "
            f"read_only={read_only}, legacy_events={legacy_listeners_on})"
        )

    # -- AppDaemon bridges ------------------------------------------------

    def _bridge_call_service(self, service: str, **payload) -> None:
        """Executor bridge: ``"light/turn_on"`` + payload -> HA service call.

        AppDaemon 4.5 signature (ADAPI.call_service, verified on the 4.5.0 tag):
        ``call_service(service, **kwargs)`` where ``service`` is
        ``"<domain>/<service>"`` — e.g. ``self.call_service("light/turn_on",
        entity_id=...)``. One positional argument; the domain/action split is
        AppDaemon's job (``_check_service`` validates the ``domain/service``
        format), so splitting here would pass ``action`` as a second positional
        argument and raise.
        """
        self.call_service(service, **payload)

    def _contention_heartbeat(self, kwargs) -> None:
        """Scheduled contention heartbeat (normal mode only): fresh probe +
        WLED read, external-grab/surrender session transitions, surrender
        auto-restore. Listeners/schedulers must never break the daemon."""
        try:
            view = self._engine.refresh_contention()
            held = view.get("held_fixture_ids") or []
            if held:
                self.log(
                    f"contention heartbeat: {len(held)} fixture(s) held by external "
                    f"light sync (streaming={view.get('streaming')})"
                )
        except Exception as exc:
            self.error(f"contention heartbeat failed: {type(exc).__name__}: {exc}")

    def _wled_freeze_reconcile_tick(self, kwargs) -> None:
        """Scheduled stale-frz reconciliation: clear WLED segment freezes no
        live session owns (a stale freeze silently blocks BOTH Scene Studio
        renders and external light sync)."""
        try:
            result = self._engine.reconcile_wled_freezes()
            if result.get("ok") and result.get("cleared_segment_ids"):
                self.log(f"cleared stale WLED segment freezes: {result['cleared_segment_ids']}")
        except Exception as exc:
            self.error(f"wled freeze reconcile failed: {type(exc).__name__}: {exc}")

    def _on_legacy_event(self, event_name, data, kwargs) -> None:
        mapper = LEGACY_EVENT_MAPPERS.get(event_name)
        if mapper is None:
            return
        try:
            result = self._engine.handle(mapper(data or {}))
            self.log(f"legacy {event_name} -> {result.get('command')}: ok={result.get('ok')}")
            self._publish_projection()
        except Exception as exc:  # listeners must never break the daemon
            self.error(f"legacy {event_name} failed: {type(exc).__name__}: {exc}")

    def _on_generate_office_scene(self, event_name, data, kwargs) -> None:
        try:
            fixture_ids = self._engine.fixture_ids_for_target("office")
            mapped = map_generate_office_scene(data or {}, fixture_ids=fixture_ids)
            results = self._engine.handle_legacy(mapped)
            ok = all(result.get("ok") for result in results)
            self.log(f"legacy generate_office_scene: sequence ok={ok} ({len(results)} steps)")
            self._publish_projection()
        except ValueError as exc:  # e.g. fewer than two valid colors
            self.error(f"generate_office_scene rejected: {exc}")
        except Exception as exc:
            self.error(f"generate_office_scene failed: {type(exc).__name__}: {exc}")

    def _serve_endpoint(self, ad_args, *rest, **kwargs) -> tuple[dict, int]:
        """AppDaemon HTTP endpoint callback (``register_endpoint``).

        Verified contract (AppDaemon 4.5.0 tag, ``http.py.dispatch_app_endpoint``):
        the callback receives the decoded JSON body (POST) or the query-string
        mapping (GET) as its first argument — never the HTTP method itself —
        plus kwargs containing the aiohttp ``request`` object. It must return
        ``(json_mappable, status_code)``; AppDaemon substitutes an HTML error
        page (losing any JSON body) for 404/500 responses, so this shim ALWAYS
        answers HTTP 200 and wraps the real status inside the body envelope::

            {"status": <http route() status>, "body": <route() payload>}

        Accepted transports (both delegate to ``service/api.route``):

        - POST JSON envelope (standard)::

            {"method": "GET"|"POST", "path": "/status", "query": {...}?, "body": {...}?}

        - GET query fallback (robustness): ``?path=/diagnostics/recent&limit=5``
          — ``path`` selects the route, all remaining params become the query
          string, method is forced to GET.

        Never raises: every failure — malformed envelope, routing miss, or an
        unexpected exception — comes back as a 200/``{"status": ..., "body":
        {command-envelope-style error}}`` so a broken request can never take
        down the daemon or get its error body replaced by AppDaemon's HTML page.

        In read_only mode (R1) a server-side gate rejects mutating commands
        before the engine sees them; ``scene.preview``, dry-run apply, and
        the read/diagnostics/discovery paths stay available.
        LIVE VALIDATION PENDING R1: the ``hasattr`` guard means a contract
        mismatch cannot break non-HTTP deployments.
        """
        path = None
        try:
            method, path, body, query = _parse_transport_envelope(ad_args)
            status, payload = http_route(self._engine, method, path, body, query=query)
        except _TransportError as exc:
            status, payload = exc.status, _error_payload(exc.code, exc.message)
        except Exception as exc:  # never let an endpoint error escape the callback
            _LOG.exception("scene_studio endpoint failed")
            status, payload = 500, _error_payload(
                "internal_error", f"{type(exc).__name__}: {exc}"
            )
        if path is not None and path.rstrip("/").endswith("/command"):
            # A Workbench-originated mutation may have changed the engine
            # revision; refresh the compact HA projection so the card stays
            # in sync with backend state changed through OTHER transports
            # (plan §4 "when engine revision changes"). Cheap no-op when the
            # revision did not move.
            self._publish_projection()
        return {"status": status, "body": payload}, 200

    # -- HA card control-surface bridge (HA card rework plan §4) ----------

    def _on_ui_command(self, event_name, data, kwargs) -> None:
        """Handle one ``scene_studio_ui_command`` HA event from the compact card.

        The bridge allowlist (``ui_bridge.UI_BRIDGE_ALLOWLIST``) is enforced
        BEFORE the engine ever sees the request; a disallowed/malformed
        command never reaches ``engine.handle`` and is answered purely
        through the projection's ``last_command`` (never a raised
        exception — listeners must never break the daemon). The engine's own
        ``RuntimePolicy`` still applies on top for every allowlisted command
        that does reach it.
        """
        envelope, rejection = build_ui_command_envelope(data or {})
        if envelope is None:
            self.log(f"scene_studio_ui_command rejected: {rejection.get('error')}")
            self._publish_projection(force=True, last_command=rejection)
            return
        try:
            result = self._engine.handle(envelope)
        except Exception as exc:  # listeners must never break the daemon
            self.error(f"scene_studio_ui_command {envelope.get('command')} failed: {type(exc).__name__}: {exc}")
            result = {
                "command": envelope.get("command"),
                "ok": False,
                "request_id": envelope.get("request_id"),
                "error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"},
            }
        last_command = {
            "request_id": result.get("request_id"),
            "ok": result.get("ok"),
            "command": result.get("command"),
        }
        error = result.get("error")
        if not result.get("ok") and isinstance(error, dict):
            last_command["error"] = error.get("message")
        self.log(f"scene_studio_ui_command {envelope.get('command')} -> ok={result.get('ok')}")
        self._publish_projection(force=True, last_command=last_command)

    def _publish_projection(self, *, force: bool = False, last_command: dict | None = None) -> None:
        """Refresh :data:`ui_bridge.UI_PROJECTION_ENTITY` from ``engine.status()``.

        Cheap and idempotent: skips the ``set_state`` call when neither the
        engine revision moved nor a fresh ``last_command`` arrived, unless
        ``force`` is set (initialization, and always after a card command so
        the card can reconcile even a rejected/no-op request). Never raises
        — a projection publish failure must not break command handling.
        """
        if not hasattr(self, "set_state"):
            return  # pragma: no cover - defensive guard, mirrors register_endpoint
        try:
            if last_command is not None:
                self._last_ui_command = last_command
            status = self._engine.status()
            revision = (status.get("engine") or {}).get("revision")
            if not force and revision == getattr(self, "_last_projected_revision", None):
                return
            self._last_projected_revision = revision
            scenes = self._engine.scenes_catalog()
            targets = project_canonical_targets(self._engine.fixture_registry())
            state, attributes = build_projection_state(
                status,
                scenes,
                last_command=getattr(self, "_last_ui_command", None),
                targets=targets,
            )
            self.set_state(UI_PROJECTION_ENTITY, state=state, attributes=attributes)
        except Exception as exc:
            self.error(f"scene_studio_ui projection publish failed: {type(exc).__name__}: {exc}")
