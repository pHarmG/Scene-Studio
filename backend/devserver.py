"""Scene Studio development server (stdlib only).

Runs the pure command service (``scene_studio.service``) behind a tiny
``http.server`` so the Workbench can talk to a REAL engine over the same
HTTP surface it will use in production (plan §9.3/§9.4, §10.1):

    GET  /api/scene_studio/status               -> engine.status()
    GET  /api/scene_studio/fixtures             -> {"fixtures": [...], "targets": [...]}
    GET  /api/scene_studio/scenes?archived=...  -> {"scenes": [...]}
    GET  /api/scene_studio/discovery            -> {"report": <report|null>}
    GET  /api/scene_studio/diagnostics/recent   -> {"events": [...], "count": n}
    POST /api/scene_studio/command              -> CommandResult dict (always 200
                                                   for a delivered envelope; see
                                                   service/api.py status policy)

Status codes are exactly what ``service.api.route()`` returns: 404/400 only
for routing and body problems, 200 for every delivered command (including
command failures, which carry ``ok: false``).

When ``workbench/dist/`` exists (``vite build``
output), it is served at ``/`` on the SAME origin, so the built Workbench
runs against the live API with no CORS involved (hosting model §10.1).

The executor is a no-op ``RecordingExecutor`` (``service/ports.py``): scene
applies produce real render plans and receipts without ever touching a
provider, so no secret handling is needed and nothing leaves the machine.

Usage::

    python -m scene_studio.devserver --store <dir> [--port 8765]
            [--seed-sample] [--dist-dir <dir>] [--shutdown-after-seconds N]

``python -m scene_studio.devserver`` requires the ``backend/`` directory to
be importable (run it with ``backend/`` as cwd, or put ``backend`` on
``PYTHONPATH``); ``scene_studio`` is a PEP 420 namespace package at the
service root and this module extends its ``__path__`` with the real ``src``
tree. Direct script execution
(``python backend/devserver.py ...``) always works.

``--seed-sample`` populates an EMPTY store from
``backend/fixtures/*.sample.json`` through the store classes
(``FixtureStore.add_fixture`` / ``add_target``, ``SceneStore.add_scene``);
already-populated spaces are left untouched, so re-running with the same
``--store`` never duplicates data.

``--shutdown-after-seconds`` bounds the server lifetime (used by tests and
smoke runs); the server exits cleanly after the timer fires.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# ---------------------------------------------------------------------------
# import bootstrap (must run before the scene_studio imports below)
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parent
_SRC_PACKAGE = _SERVICE_ROOT / "src" / "scene_studio"


def _bootstrap_import_path() -> None:
    """Make ``scene_studio.*`` importable from this file's location.

    Two execution modes are supported:

    - ``python -m scene_studio.devserver``: the ``-m`` machinery imported
      ``scene_studio`` first. Depending on sys.path it may be the PEP 420
      namespace package at the service root (whose ``__path__`` lacks the
      actual source tree) or the regular ``src`` package. In both cases we
      make sure the ``src`` tree is part of the package ``__path__``.
    - direct script execution: no ``scene_studio`` package exists yet, so
      the ``src`` root is added to ``sys.path`` and the regular package is
      imported normally.
    """
    package = sys.modules.get("scene_studio")
    src_package = str(_SRC_PACKAGE)
    if package is not None and hasattr(package, "__path__"):
        if src_package not in package.__path__:
            package.__path__.append(src_package)
    else:
        src_root = str(_SRC_PACKAGE.parent)
        if src_root not in sys.path:
            sys.path.insert(0, src_root)


_bootstrap_import_path()

from scene_studio.service.api import ROUTE_PREFIX, route  # noqa: E402
from scene_studio.service.engine import SceneStudioEngine  # noqa: E402
from scene_studio.service.ports import MonotonicClock, RecordingExecutor  # noqa: E402
from scene_studio.stores import SceneStudioStore  # noqa: E402

__all__ = [
    "DEFAULT_PORT",
    "build_engine",
    "default_dist_dir",
    "default_fixtures_dir",
    "main",
    "make_server",
    "seed_store_from_fixtures",
]

DEFAULT_PORT = 8765

#: Static shell served at ``/`` (vite build output of the Workbench).
DEFAULT_DIST_DIR = _SERVICE_ROOT.parent / "workbench" / "dist"

#: Sample documents used by ``--seed-sample``.
DEFAULT_FIXTURES_DIR = _SERVICE_ROOT / "fixtures"

_STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


# ---------------------------------------------------------------------------
# store seeding + engine assembly
# ---------------------------------------------------------------------------


def seed_store_from_fixtures(store: SceneStudioStore, fixtures_dir: Path | str | None = None) -> dict:
    """Populate an EMPTY store from the sample documents via the store classes.

    Each storage space (registry fixtures, registry targets, active scenes)
    is seeded ONLY when that space is currently empty, so calling this on an
    already-populated store is a no-op and never duplicates data. Returns a
    counts dict reporting what was written: ``{"targets": n, "fixtures": n,
    "scenes": n}``.
    """
    fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else DEFAULT_FIXTURES_DIR
    counts = {"targets": 0, "fixtures": 0, "scenes": 0}

    space_is_empty = (
        not store.fixtures.list_fixtures()
        and not store.fixtures.list_targets()
    )
    if space_is_empty:
        registry_doc = json.loads((fixtures_dir / "registry.sample.json").read_text(encoding="utf-8"))
        for target in registry_doc.get("targets", []):
            store.fixtures.add_target(target)
            counts["targets"] += 1
        for fixture in registry_doc.get("fixtures", []):
            store.fixtures.add_fixture(fixture)
            counts["fixtures"] += 1

    if not store.scenes.list_scenes() and not store.scenes.list_archived():
        scenes_doc = json.loads((fixtures_dir / "scenes.sample.json").read_text(encoding="utf-8"))
        for scene in scenes_doc.get("scenes", []):
            store.scenes.add_scene(scene)
            counts["scenes"] += 1

    return counts


def build_engine(store_root: Path | str, *, store: SceneStudioStore | None = None) -> SceneStudioEngine:
    """Assemble the dev engine: real store + no-op executor + wall clock.

    The ``RecordingExecutor`` records every planned provider operation and
    returns ``ok`` receipts without any I/O — scene applies exercise the full
    plan/persist/event path while guaranteeing nothing leaves the machine.
    """
    store = store if store is not None else SceneStudioStore(store_root)
    return SceneStudioEngine(store, RecordingExecutor(detail="devserver no-op executor"), MonotonicClock())


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _DevSceneStudioHandler(BaseHTTPRequestHandler):
    """Request handler reading per-server state off the server instance.

    ``make_server`` attaches ``engine`` and ``dist_dir`` to the
    ``ThreadingHTTPServer`` instance, so one handler class serves any number
    of independent servers (tests bind one per store).
    """

    protocol_version = "HTTP/1.1"
    server_version = "scene_studio_devserver/0.1"

    # -- responses -------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error_payload(self, code: str, message: str) -> dict:
        # Same envelope shape as service/api.py transport errors.
        return {"command": None, "ok": False, "error": {"code": code, "message": message}}

    # -- API -------------------------------------------------------

    def _handle_api(self, method: str) -> None:
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    # Malformed JSON never reaches route(); map to the same 400 shape.
                    self._send_json(
                        400,
                        self._error_payload("validation_error", f"request body is not valid JSON: {exc}"),
                    )
                    return

        status, payload = route(self.server.engine, method, parsed.path, body=body, query=query)
        self._send_json(status, payload)

    # -- static ----------------------------------------------------

    def _handle_static(self) -> None:
        dist = self.server.dist_dir
        if not dist.is_dir():
            self._send_json(
                404,
                self._error_payload(
                    "not_found",
                    f"no static shell at {dist}; run `npm run build` in workbench/ "
                    "or pass --dist-dir",
                ),
            )
            return
        parsed = urlparse(self.path)
        relative = unquote(parsed.path)
        if relative in ("", "/"):
            relative = "/index.html"
        candidate = (dist / relative.lstrip("/")).resolve()
        if not candidate.is_relative_to(dist.resolve()) or not candidate.is_file():
            self._send_json(404, self._error_payload("not_found", f"no static file for {parsed.path}"))
            return
        content_type = _STATIC_CONTENT_TYPES.get(
            candidate.suffix.lower(), "application/octet-stream"
        )
        self._send_bytes(200, candidate.read_bytes(), content_type)

    # -- verb dispatch ----------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if urlparse(self.path).path.startswith(ROUTE_PREFIX):
            self._handle_api("GET")
        else:
            self._handle_static()

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if urlparse(self.path).path.startswith(ROUTE_PREFIX):
            self._handle_api("POST")
        else:
            self._send_json(404, self._error_payload("not_found", f"no route for POST {self.path}"))

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        print(f"scene_studio.devserver: {self.address_string()} {format % args}")


def make_server(
    store_root: Path | str,
    *,
    port: int = 0,
    dist_dir: Path | str | None = None,
    fixtures_dir: Path | str | None = None,
    seed_sample: bool = False,
    bind: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    """Build (and bind) the dev HTTP server; caller runs ``serve_forever()``.

    ``port=0`` picks a random free port (tests). ``dist_dir=None`` uses the
    repo Workbench ``dist/`` when present; pass a directory to serve a
    custom static tree, or a non-existing path to disable static hosting.
    """
    store = SceneStudioStore(store_root)
    if seed_sample:
        counts = seed_store_from_fixtures(store, fixtures_dir)
        print(
            f"scene_studio.devserver: seeded store at {Path(store_root)} "
            f"(fixtures={counts['fixtures']}, targets={counts['targets']}, scenes={counts['scenes']})"
        )
    engine = build_engine(store_root, store=store)
    resolved_dist = Path(dist_dir) if dist_dir is not None else DEFAULT_DIST_DIR

    server = ThreadingHTTPServer((bind, port), _DevSceneStudioHandler)
    server.daemon_threads = True
    server.engine = engine
    server.dist_dir = resolved_dist
    return server


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scene_studio.devserver",
        description="Serve the Scene Studio command service (and the built Workbench) over local HTTP.",
    )
    parser.add_argument("--store", required=True, help="store root directory (created when missing)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--seed-sample",
        action="store_true",
        help=f"populate an EMPTY store from {DEFAULT_FIXTURES_DIR}/*.sample.json",
    )
    parser.add_argument(
        "--seed-demo",
        action="store_true",
        help=(
            "populate an EMPTY store from the GENERIC demo topology "
            f"({DEFAULT_FIXTURES_DIR / 'demo'}/*.sample.json) — synthetic fixtures/rooms with "
            "no real household names, provider UUIDs, or LAN addresses"
        ),
    )
    parser.add_argument(
        "--dist-dir",
        default=None,
        help=f"static directory to serve at / (default: {DEFAULT_DIST_DIR} when it exists)",
    )
    parser.add_argument(
        "--shutdown-after-seconds",
        type=float,
        default=None,
        help="exit cleanly after N seconds (bounded lifetime for tests/smoke runs)",
    )
    args = parser.parse_args(argv)

    store_root = Path(args.store)
    store_root.mkdir(parents=True, exist_ok=True)

    server = make_server(
        store_root,
        port=args.port,
        dist_dir=args.dist_dir,
        seed_sample=args.seed_sample or args.seed_demo,
        fixtures_dir=(DEFAULT_FIXTURES_DIR / "demo") if args.seed_demo else None,
    )
    host, bound_port = server.server_address[:2]
    dist_note = f"serving static shell from {args.dist_dir or DEFAULT_DIST_DIR}" if (
        args.dist_dir or DEFAULT_DIST_DIR
    ).is_dir() else "static shell disabled (no dist build found)"
    print(f"scene_studio.devserver: API on http://{host}:{bound_port}{ROUTE_PREFIX}/status — {dist_note}")
    print(f"scene_studio.devserver: store root {store_root.resolve()}")

    if args.shutdown_after_seconds is not None:
        timer = threading.Timer(args.shutdown_after_seconds, server.shutdown)
        timer.daemon = True
        timer.start()
        print(f"scene_studio.devserver: shutting down after {args.shutdown_after_seconds}s")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("scene_studio.devserver: interrupted")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
