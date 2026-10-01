"""Dev server tests — HTTP hosting of the pure command service (plan §9.3/§10.1).

The server is exercised in-process via ``devserver.make_server`` (bound to a
random free port, driven with ``urllib``): no subprocess, no sockets beyond
loopback. Covers:

- ``--seed-sample`` store population through the store classes (and no-op on
  an already-populated store);
- the documented route surface: GET status/fixtures/scenes(archived)/
  discovery/diagnostics + POST command with real status codes;
- engine-shaped status body (the Workbench ``getStatus`` contract);
- command round trip (rename) with revision bump;
- transport-level status codes: 404 unknown route/method, 400 malformed JSON;
- same-origin static hosting of a ``dist`` directory (content types, missing
  file 404, path-traversal rejection) and the no-dist 404 shape.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = SERVICE_ROOT / "fixtures"

# conftest.py already puts src/ on sys.path for the scene_studio package;
# the devserver module lives at the service root, so load it by path.
if "devserver" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("devserver", SERVICE_ROOT / "devserver.py")
    devserver = importlib.util.module_from_spec(_spec)
    sys.modules["devserver"] = devserver
    _spec.loader.exec_module(devserver)
else:  # pragma: no cover - reimport guard
    devserver = sys.modules["devserver"]

from scene_studio.stores import SceneStudioStore  # noqa: E402


def _request(base_url: str, method: str, path: str, body=None):
    """One HTTP round trip; returns (status, body, headers).

    JSON bodies are parsed; anything else (e.g. static HTML/JS) is returned
    as raw text.
    """
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)

    def _read(resp):
        raw = resp.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _read(resp), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, _read(exc), exc.headers


@pytest.fixture
def store(tmp_path):
    return SceneStudioStore(tmp_path / "store")


@pytest.fixture
def server(tmp_path):
    """A seeded dev server on a random port; no dist dir (API only)."""
    srv = devserver.make_server(
        tmp_path / "store",
        port=0,
        dist_dir=tmp_path / "no-dist",
        fixtures_dir=FIXTURES_DIR,
        seed_sample=True,
    )
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base_url
    srv.shutdown()
    srv.server_close()
    worker.join(timeout=5)


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def test_seed_sample_populates_empty_store(tmp_path):
    store = SceneStudioStore(tmp_path / "store")
    counts = devserver.seed_store_from_fixtures(store, FIXTURES_DIR)
    assert counts == {"targets": 5, "fixtures": 24, "scenes": 3}
    assert len(store.fixtures.list_fixtures()) == 24
    assert len(store.fixtures.list_targets()) == 5
    assert [s.id for s in store.scenes.list_scenes()] == ["aurora_flow", "meeting_blue", "twilight"]
    # persisted to disk, not just memory
    assert (tmp_path / "store" / "registry" / "registry.json").is_file()
    assert (tmp_path / "store" / "scenes" / "twilight.json").is_file()


def test_seed_sample_is_noop_on_populated_store(tmp_path):
    store = SceneStudioStore(tmp_path / "store")
    first = devserver.seed_store_from_fixtures(store, FIXTURES_DIR)
    second = devserver.seed_store_from_fixtures(store, FIXTURES_DIR)
    assert first["fixtures"] == 24 and second == {"targets": 0, "fixtures": 0, "scenes": 0}
    assert len(store.fixtures.list_fixtures()) == 24
    assert len(store.scenes.list_scenes()) == 3


# ---------------------------------------------------------------------------
# GET routes -> engine shapes
# ---------------------------------------------------------------------------


def test_get_status_returns_engine_shape(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/status")
    assert status == 200
    # STABLE KEYS documented in engine.py status() docstring
    assert set(body.keys()) == {"engine", "runtime", "fixtures", "providers", "current", "playback", "contention", "last_discovery", "product"}
    assert body["runtime"]["mode"] == "normal"
    assert "scene.apply" in body["runtime"]["allowed_commands"]
    assert set(body["engine"].keys()) == {"ok", "revision", "event_capacity", "events"}
    assert body["engine"]["ok"] is True
    assert isinstance(body["engine"]["revision"], int)
    assert set(body["fixtures"].keys()) == {
        "total", "ready", "missing", "disabled", "unbound", "degraded", "conflicting",
    }
    assert body["fixtures"]["total"] == 24
    assert body["fixtures"]["ready"] + body["fixtures"]["missing"] + body["fixtures"]["disabled"] + \
        body["fixtures"]["unbound"] + body["fixtures"]["degraded"] + body["fixtures"]["conflicting"] == 24
    assert set(body["providers"].keys()) == {"hue_v2", "wled", "ha_light"}
    for counts in body["providers"].values():
        assert set(counts.keys()) == {"total", "ready", "missing", "degraded", "other"}
    assert body["current"] is None  # nothing applied yet
    assert body["playback"]["sessions"] == []  # empty collection, never None
    assert body["playback"]["counts"]["active"] == 0
    assert body["last_discovery"] is None  # no discovery.run on a fresh store


def test_get_fixtures_returns_registry_catalog(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/fixtures")
    assert status == 200
    assert set(body.keys()) == {"fixtures", "targets"}
    assert len(body["fixtures"]) == 24
    assert len(body["targets"]) == 5
    fixture = body["fixtures"][0]
    assert {"id", "name", "location", "groups", "enabled", "binding", "capabilities"} <= set(fixture.keys())


def test_get_scenes_active_and_archived(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/scenes")
    assert status == 200
    assert set(body.keys()) == {"scenes"}
    assert sorted(s["id"] for s in body["scenes"]) == ["aurora_flow", "meeting_blue", "twilight"]

    status, archived, _ = _request(server, "GET", "/api/scene_studio/scenes?archived=true")
    assert status == 200
    assert archived == {"scenes": []}  # nothing archived yet


def test_get_discovery_empty_state(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/discovery")
    assert status == 200
    assert body == {"report": None}  # documented empty state, not a 404


def test_get_recent_events_empty(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/diagnostics/recent?limit=10")
    assert status == 200
    assert body == {"events": [], "count": 0}


# ---------------------------------------------------------------------------
# POST command round trip
# ---------------------------------------------------------------------------


def test_post_command_rename_round_trip(server):
    _, before, _ = _request(server, "GET", "/api/scene_studio/status")
    revision_before = before["engine"]["revision"]

    status, result, _ = _request(
        server,
        "POST",
        "/api/scene_studio/command",
        {"command": "scene.rename", "scene_id": "twilight", "name": "Twilight Dev", "request_id": "req-dev-1"},
    )
    assert status == 200  # delivered commands are always 200
    assert result["command"] == "scene.rename"
    assert result["ok"] is True
    assert result["request_id"] == "req-dev-1"
    assert result["data"]["scene"]["name"] == "Twilight Dev"
    assert result["data"]["scene"]["id"] == "twilight"  # ids are immutable

    # the rename is visible in the catalog and bumped the revision
    _, catalog, _ = _request(server, "GET", "/api/scene_studio/scenes")
    assert catalog["scenes"][[s["id"] for s in catalog["scenes"]].index("twilight")]["name"] == "Twilight Dev"
    _, after, _ = _request(server, "GET", "/api/scene_studio/status")
    assert after["engine"]["revision"] == revision_before + 1
    # and it produced an operational event
    _, events, _ = _request(server, "GET", "/api/scene_studio/diagnostics/recent?limit=5")
    assert events["count"] >= 1
    assert any("renamed" in e["summary"] for e in events["events"])


def test_post_command_failure_is_200_with_ok_false(server):
    status, result, _ = _request(
        server, "POST", "/api/scene_studio/command", {"command": "scene.apply", "scene_id": "nope"}
    )
    assert status == 200
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_post_dry_run_apply_returns_render_plan(server):
    status, result, _ = _request(
        server, "POST", "/api/scene_studio/command", {"command": "scene.preview", "scene_id": "twilight"}
    )
    assert status == 200
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    plan = result["data"]["render_plan"]
    assert plan["scene_id"] == "twilight"
    assert len(plan["fixture_plans"]) >= 1
    # dry runs never bump the revision
    _, after, _ = _request(server, "GET", "/api/scene_studio/status")
    assert after["engine"]["revision"] == 0


# ---------------------------------------------------------------------------
# transport-level status codes
# ---------------------------------------------------------------------------


def test_unknown_path_returns_404_envelope(server):
    status, body, _ = _request(server, "GET", "/api/scene_studio/bogus")
    assert status == 404
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"


def test_wrong_method_returns_404(server):
    status, body, _ = _request(server, "POST", "/api/scene_studio/status", {})
    assert status == 404
    assert body["error"]["code"] == "not_found"


def test_malformed_json_returns_400(server):
    req = urllib.request.Request(
        server + "/api/scene_studio/command",
        data=b"{not json",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S110 - asserted below
            status_code, body = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        status_code, body = exc.code, json.loads(exc.read().decode())
    assert status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# static hosting (same origin as the API)
# ---------------------------------------------------------------------------


def test_static_hosting_serves_dist_at_root(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html><body>workbench-shell</body></html>", encoding="utf-8")
    (dist / "assets" / "app-HASH.js").write_text("export default 1;", encoding="utf-8")
    (dist / "assets" / "style-HASH.css").write_text("body{}", encoding="utf-8")

    srv = devserver.make_server(tmp_path / "store", port=0, dist_dir=dist, seed_sample=True)
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, raw, headers = _request(base, "GET", "/")
        assert status == 200
        assert "workbench-shell" in raw
        assert headers["Content-Type"].startswith("text/html")

        _, raw_js, js_headers = _request(base, "GET", "/assets/app-HASH.js")
        assert raw_js == "export default 1;"
        assert js_headers["Content-Type"].startswith("application/javascript")

        _, raw_css, css_headers = _request(base, "GET", "/assets/style-HASH.css")
        assert css_headers["Content-Type"].startswith("text/css")

        # API and static live on ONE origin
        status_api, body_api, _ = _request(base, "GET", "/api/scene_studio/status")
        assert status_api == 200 and body_api["engine"]["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=5)


def test_static_hosting_missing_file_and_traversal_are_404(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("shell", encoding="utf-8")
    srv = devserver.make_server(tmp_path / "store", port=0, dist_dir=dist)
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body, _ = _request(base, "GET", "/missing.js")
        assert status == 404
        assert body["error"]["code"] == "not_found"

        status, _, _ = _request(base, "GET", "/../secrets.txt")
        assert status == 404

        status, _, _ = _request(base, "POST", "/index.html")
        assert status == 404
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=5)


def test_no_dist_returns_documented_404(tmp_path):
    srv = devserver.make_server(tmp_path / "store", port=0, dist_dir=tmp_path / "absent")
    worker = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body, _ = _request(base, "GET", "/")
        assert status == 404
        assert "static shell" in body["error"]["message"]
    finally:
        srv.shutdown()
        srv.server_close()
        worker.join(timeout=5)
