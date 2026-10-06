"""HA automation gateway over Home Assistant's supported REST API.

The :class:`~scene_studio.service.ports.HaAutomationGateway` implementation
for the AppDaemon runtime. Every routine read/write goes through HA's own
REST surface — the same API the HA frontend uses:

- ``GET  /api/states``                                   (entity enumeration)
- ``GET  /api/config/automation/config/{id}``            (canonical config)
- ``POST /api/config/automation/config/{id}``            (native upsert)
- ``DELETE /api/config/automation/config/{id}``          (native delete)
- ``POST /api/services/automation/{reload,turn_on,turn_off}``

There is deliberately NO YAML path: if the capability cannot be resolved
(no HA URL/token in this runtime) the gateway reports itself unavailable and
the routine service surfaces that honestly instead of falling back to direct
``automations.yaml`` mutation.

Credential resolution lives with the adapter wiring (``adapter.py``): the
AppDaemon hass-plugin config first, then the HAOS/Supervisor add-on
environment (``SUPERVISOR_TOKEN`` → ``http://supervisor/core``). Tokens stay
in process memory — never in files, logs, errors, or diagnostics.

``requests`` is imported lazily (the only place in the routine tree allowed
to touch it), and a ``requests_module``/``session`` can be injected for
tests, mirroring :class:`~scene_studio.appdaemon_adapter.adapter.RequestsProviderExecutor`.
"""

from __future__ import annotations

import logging

from ..service.ports import (
    HaAutomationEntity,
    HaAutomationGatewayError,
)

__all__ = ["RequestsHaAutomationGateway"]

_LOG = logging.getLogger("scene_studio_ha_automation")

_STATE_PREFIX = "automation."


class RequestsHaAutomationGateway:
    """Gateway implementation over HA's REST config/state/services API.

    Never raises anything except :class:`HaAutomationGatewayError`; every
    transport / HTTP / payload failure maps to a coded error so the routine
    service can render honest results.
    """

    def __init__(
        self,
        *,
        base_url: str | None,
        token: str | None,
        requests_module=None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token
        self._timeout = timeout
        self._requests = requests_module  # injectable for tests; lazily imported otherwise
        self._unavailable_reason: str | None = None
        if not self._base_url or not self._token:
            self._unavailable_reason = (
                "no Home Assistant REST credentials are available in this runtime "
                "(hass plugin config or add-on Supervisor token required)"
            )

    # -- port surface ------------------------------------------------------

    def available(self) -> bool:
        return self._unavailable_reason is None

    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def list_automation_entities(self) -> list[HaAutomationEntity]:
        """Enumerate ``automation.*`` entities from ``GET /api/states``."""
        payload = self._request_json("GET", "/api/states")
        if not isinstance(payload, list):
            raise HaAutomationGatewayError("invalid_response", "GET /api/states did not return a list")
        entities: list[HaAutomationEntity] = []
        for state in payload:
            if not isinstance(state, dict):
                continue
            entity_id = state.get("entity_id")
            if not isinstance(entity_id, str) or not entity_id.startswith(_STATE_PREFIX):
                continue
            attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
            automation_id = attributes.get("id")
            if automation_id is not None and not isinstance(automation_id, str):
                automation_id = str(automation_id)
            alias = attributes.get("friendly_name")
            entities.append(
                HaAutomationEntity(
                    entity_id=entity_id,
                    state=state.get("state") if isinstance(state.get("state"), str) else "",
                    automation_id=automation_id or None,
                    alias=alias if isinstance(alias, str) and alias.strip() else None,
                )
            )
        return entities

    def get_automation_config(self, automation_id: str) -> dict | None:
        """The canonical stored automation config (``None`` when absent)."""
        status, payload = self._request("GET", f"/api/config/automation/config/{automation_id}")
        if status == 404:
            return None
        if status != 200:
            raise HaAutomationGatewayError(
                "http_error", f"GET automation config {automation_id!r} -> HTTP {status}"
            )
        if not isinstance(payload, dict):
            raise HaAutomationGatewayError(
                "invalid_response", f"automation config {automation_id!r} is not an object"
            )
        return payload

    def save_automation_config(self, automation_id: str, config: dict) -> None:
        status, payload = self._request("POST", f"/api/config/automation/config/{automation_id}", json_body=config)
        if status != 200:
            raise HaAutomationGatewayError(
                "http_error",
                f"POST automation config {automation_id!r} -> HTTP {status}"
                + (f": {str(payload)[:160]}" if payload else ""),
            )

    def delete_automation_config(self, automation_id: str) -> None:
        status, _ = self._request("DELETE", f"/api/config/automation/config/{automation_id}")
        if status not in (200, 404):  # 404 = already gone: fine for deletion
            raise HaAutomationGatewayError(
                "http_error", f"DELETE automation config {automation_id!r} -> HTTP {status}"
            )

    def reload_automations(self) -> None:
        status, payload = self._request("POST", "/api/services/automation/reload", json_body={})
        if status != 200:
            raise HaAutomationGatewayError(
                "http_error", f"automation/reload -> HTTP {status}"
            )
        if not isinstance(payload, list):
            raise HaAutomationGatewayError("invalid_response", "automation/reload did not return a state list")

    def set_automation_enabled(self, entity_id: str, enabled: bool) -> None:
        service = "turn_on" if enabled else "turn_off"
        status, payload = self._request(
            "POST", f"/api/services/automation/{service}", json_body={"entity_id": entity_id}
        )
        if status != 200:
            raise HaAutomationGatewayError(
                "http_error", f"automation/{service} {entity_id!r} -> HTTP {status}"
            )
        if not isinstance(payload, list):
            raise HaAutomationGatewayError(
                "invalid_response", f"automation/{service} did not return a state list"
            )

    # -- transport ----------------------------------------------------------

    def _http(self):
        if self._requests is None:
            import requests  # lazy: same rule as every adapter transport

            self._requests = requests.Session()
        return self._requests

    def _request(self, method: str, path: str, json_body: dict | None = None):
        """One HA REST call. Returns ``(status, decoded_payload_or_None)``.
        Raises ``HaAutomationGatewayError`` on transport failure or a
        non-JSON body where JSON is required. Never includes credentials in
        error messages."""
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._http().request(
                method, url, headers=headers, json=json_body, timeout=self._timeout
            )
        except Exception as exc:
            raise HaAutomationGatewayError(
                "http_error", f"{method} {path} failed: {type(exc).__name__}"
            ) from exc
        if response.status_code == 204:
            return response.status_code, None
        try:
            payload = response.json()
        except ValueError:
            payload = None
            if response.status_code == 200:
                raise HaAutomationGatewayError(
                    "invalid_response", f"{method} {path} returned a non-JSON body"
                ) from None
        return response.status_code, payload

    def _request_json(self, method: str, path: str, json_body: dict | None = None):
        status, payload = self._request(method, path, json_body=json_body)
        if status != 200:
            raise HaAutomationGatewayError("http_error", f"{method} {path} -> HTTP {status}")
        return payload
