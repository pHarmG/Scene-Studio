"""HTTP API surface for the Scene Studio command service (plan §9.3).

Pure routing function — no server, no framework, no I/O. An adapter
(``appdaemon_adapter``) registers this :func:`route` with its HTTP
mechanism; the Workbench treats the same paths as its canonical remote API.

Route table (all paths prefixed ``/api/scene_studio``)::

    GET  /status                    -> 200 engine.status()
    GET  /fixtures                  -> 200 {"fixtures": [...], "targets": [...]}
    GET  /fixture-state             -> 200 engine.sample_live_state() (read-only; no revision bump)
    GET  /scenes[?archived=true]    -> 200 {"scenes": [...]}
    GET  /routines[?refresh=true]   -> 200 {"available", "routines": [...],
                                            ...} (derived HA routine projection)
    GET  /discovery                 -> 200 {"report": <report|null>}
    GET  /diagnostics/recent?limit  -> 200 {"events": [...], "count": n}
    POST /command                   -> 200 <CommandResult dict>

STATUS-CODE POLICY (documented for the Workbench client):

- Transport-level HTTP codes are reserved for ROUTING and BODY problems:
  ``404`` for an unknown path/method, ``400`` for a body that is not a JSON
  object envelope (malformed JSON never reaches this layer; the adapter's
  parser maps it to the same 400 shape).
- A successfully delivered command ALWAYS returns ``200`` even when the
  command itself failed — the CommandResult envelope carries
  ``ok: false`` plus ``error: {code, message}`` (contracts §4). Clients
  must branch on ``ok``, never on the HTTP status, for command outcomes.
- Read endpoints return ``200`` with a JSON body; ``GET /discovery`` with no
  report on record returns ``200 {"report": null}`` (the documented
  404-shape choice: an empty discovery state is a normal Workbench empty
  state, not an error).
- Read responses pass through ``sanitize_tree`` so no secret-shaped string
  can leak through registry metadata or scene documents.
"""

from __future__ import annotations

from ..domain.sanitize import sanitize_tree

__all__ = ["ROUTE_PREFIX", "route"]

ROUTE_PREFIX = "/api/scene_studio"

_MAX_RECENT_LIMIT = 500  # engine event deque default capacity; sane read cap


def route(engine, method: str, path: str, body: dict | None = None, query: dict | None = None) -> tuple[int, dict]:
    """Resolve one HTTP request against the engine. Returns ``(status, payload)``."""
    normalized_method = (method or "GET").upper()
    normalized_path = _normalize_path(path)
    query = query or {}

    if normalized_path is None:
        return _not_found(normalized_method, path)

    if normalized_path == "/status" and normalized_method == "GET":
        if _flag(query, "check_updates"):
            engine.check_updates()
        return 200, engine.status()

    if normalized_path == "/fixtures" and normalized_method == "GET":
        return 200, sanitize_tree(engine.fixtures_catalog())

    if normalized_path == "/fixture-state" and normalized_method == "GET":
        return 200, sanitize_tree(engine.sample_live_state())

    if normalized_path == "/scenes" and normalized_method == "GET":
        archived = _flag(query, "archived")
        return 200, sanitize_tree(engine.scenes_catalog(archived=archived))

    if normalized_path == "/routines" and normalized_method == "GET":
        # Derived routine projection over HA's native automations
        # (routines pass). `refresh=true` forces a HA re-read (mirrors the
        # documented `?check_updates=true` explicit-refresh pattern);
        # otherwise the engine's bounded TTL cache answers.
        return 200, sanitize_tree(engine.routines_catalog(refresh=_flag(query, "refresh")))

    if normalized_path == "/discovery" and normalized_method == "GET":
        report = engine.latest_discovery()
        return 200, {"report": report.to_dict() if report is not None else None}

    if normalized_path == "/diagnostics/recent" and normalized_method == "GET":
        try:
            limit = int(str(query.get("limit", "50")).strip() or "50")
        except ValueError:
            return 400, _error_payload("validation_error", "query parameter 'limit' must be an integer")
        limit = max(1, min(limit, _MAX_RECENT_LIMIT))
        events = [event.to_dict() for event in engine.recent_events(limit)]
        return 200, {"events": events, "count": len(events)}

    if normalized_path == "/command" and normalized_method == "POST":
        if not isinstance(body, dict):
            return 400, _error_payload(
                "validation_error",
                "request body must be a JSON object command envelope ({\"command\": ...})",
            )
        return 200, engine.handle(body)

    return _not_found(normalized_method, path)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> str | None:
    """Strip the route prefix and trailing slash; None when not our prefix."""
    cleaned = (path or "").split("?", 1)[0]
    if cleaned != ROUTE_PREFIX and not cleaned.startswith(ROUTE_PREFIX + "/"):
        return None
    if cleaned.startswith(ROUTE_PREFIX + "/"):
        cleaned = cleaned[len(ROUTE_PREFIX):]
    else:
        cleaned = ""
    while cleaned.endswith("/") and len(cleaned) > 1:
        cleaned = cleaned[:-1]
    return cleaned  # "" (bare prefix) matches no route -> 404


def _flag(query: dict, name: str) -> bool:
    value = query.get(name)
    return isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on")


def _error_payload(code: str, message: str) -> dict:
    return {"command": None, "ok": False, "error": {"code": code, "message": message}}


def _not_found(method: str, path: str) -> tuple[int, dict]:
    return 404, _error_payload("not_found", f"no route for {method} {path}")
