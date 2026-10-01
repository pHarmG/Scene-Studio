"""HTTP API surface tests (plan §9.3) — pure route() function, no server.

Covers the documented status-code policy: transport 4xx only for routing and
body problems; command outcomes always 200 with ok/error in the envelope;
empty discovery is a 200 null-report empty state; reads pass sanitize_tree.
"""

import json

import pytest

from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.api import ROUTE_PREFIX, route
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore

FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJzY2VuZS1zdHVkaW8tdGVzdCIsImV4cCI6OTk5OTk5OTk5OX0.abc123def456"  # secrets-scan:allow (fake test fixture)
)


@pytest.fixture
def store(tmp_path):
    store = SceneStudioStore(tmp_path)
    store.fixtures.add_fixture(
        {
            "id": "lamp",
            "name": "Lamp",
            "groups": ["office"],
            "metadata": {"note": f"pulled while holding {FAKE_JWT}"},  # secrets-scan:allow (fake test fixture)
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
        }
    )
    store.fixtures.add_target({"id": "office", "name": "Office"})
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "twilight",
            "name": "Twilight",
            "target_ids": ["office"],
            "fixture_states": {"lamp": {"on": True, "brightness": 40.0}},
        }
    )
    return store


@pytest.fixture
def engine(store):
    return SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(),
    )


# ---------------------------------------------------------------------------
# GET routes
# ---------------------------------------------------------------------------


def test_status_route(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/status")
    assert status == 200
    assert payload["engine"]["ok"] is True
    assert payload["product"]["build"]["version"]
    assert payload["product"]["update"]["state"] == "unchecked"
    assert {"engine", "fixtures", "providers", "current", "playback", "last_discovery"} <= set(payload)


def test_update_check_explicit_status_contract(engine, monkeypatch):
    calls = []
    def check(version):
        calls.append(version)
        return {"state": "current"}
    monkeypatch.setattr("scene_studio.service.engine.check_updates", check)
    revision = engine.status()["engine"]["revision"]
    assert not calls
    code, body = route(engine, "GET", "/api/scene_studio/status", query={"check_updates": "true"})
    assert code == 200 and calls
    assert body["product"]["update"]["state"] == "current"
    assert body["engine"]["revision"] == revision


def test_fixtures_route_returns_registry_and_redacts(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/fixtures")
    assert status == 200
    assert [fixture["id"] for fixture in payload["fixtures"]] == ["lamp"]
    assert [target["id"] for target in payload["targets"]] == ["office"]
    blob = json.dumps(payload)
    assert FAKE_JWT not in blob  # secret-shaped metadata never leaks
    assert "[REDACTED-JWT]" in blob


def test_scenes_route_active_and_archived(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/scenes")
    assert status == 200
    assert [scene["id"] for scene in payload["scenes"]] == ["twilight"]

    route(engine, "POST", "/api/scene_studio/command", body={"command": "scene.archive", "scene_id": "twilight"})

    status, payload = route(engine, "GET", "/api/scene_studio/scenes")
    assert payload["scenes"] == []
    status, archived = route(engine, "GET", "/api/scene_studio/scenes", query={"archived": "true"})
    assert status == 200
    assert [scene["id"] for scene in archived["scenes"]] == ["twilight"]


def test_discovery_route_empty_state_is_200_null(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/discovery")
    assert status == 200
    assert payload == {"report": None}  # documented 404-shape choice


def test_diagnostics_recent_route_limit(engine):
    route(engine, "POST", "/api/scene_studio/command", body={"command": "scene.apply", "scene_id": "twilight"})
    status, payload = route(engine, "GET", "/api/scene_studio/diagnostics/recent", query={"limit": "1"})
    assert status == 200
    assert payload["count"] == 1
    assert payload["events"][0]["summary"] == "Twilight started"

    status, payload = route(engine, "GET", "/api/scene_studio/diagnostics/recent")
    assert status == 200
    assert payload["count"] >= 1

    status, payload = route(engine, "GET", "/api/scene_studio/diagnostics/recent", query={"limit": "zero"})
    assert status == 400
    assert payload["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# POST command route
# ---------------------------------------------------------------------------


def test_command_route_success_returns_200(engine):
    status, payload = route(
        engine,
        "POST",
        "/api/scene_studio/command",
        body={"command": "scene.preview", "scene_id": "twilight", "request_id": "req-1"},
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["request_id"] == "req-1"
    assert "render_plan" in payload["data"]


def test_command_route_failure_still_returns_200_with_error_envelope(engine):
    status, payload = route(
        engine,
        "POST",
        "/api/scene_studio/command",
        body={"command": "scene.apply", "scene_id": "ghost"},
    )
    assert status == 200  # documented policy: command outcome lives in the envelope
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


def test_command_route_non_object_body_is_400(engine):
    for bad in (None, "string", 42, [1, 2]):
        status, payload = route(engine, "POST", "/api/scene_studio/command", body=bad)
        assert status == 400
        assert payload["ok"] is False
        assert payload["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# routing policy
# ---------------------------------------------------------------------------


def test_unknown_path_and_method_are_404(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/bogus")
    assert status == 404
    assert payload["error"]["code"] == "not_found"

    status, _ = route(engine, "GET", "/other/status")
    assert status == 404

    status, _ = route(engine, "DELETE", "/api/scene_studio/status")
    assert status == 404  # unknown METHOD on a known path

    status, _ = route(engine, "GET", "/api/scene_studio/command")
    assert status == 404  # command is POST-only

    status, _ = route(engine, "POST", "/api/scene_studio/status")
    assert status == 404

    status, _ = route(engine, "GET", "/api/scene_studio")  # bare prefix
    assert status == 404


def test_fixture_state_route_shape(engine):
    status, payload = route(engine, "GET", "/api/scene_studio/fixture-state")
    assert status == 200
    assert {"observed_at", "fixtures", "providers"} <= set(payload)


def test_fixture_state_route_never_mutates_or_bumps_revision(engine):
    before = engine.status()["engine"]["revision"]
    route(engine, "GET", "/api/scene_studio/fixture-state")
    route(engine, "GET", "/api/scene_studio/fixture-state")
    after = engine.status()["engine"]["revision"]
    assert after == before


def test_fixture_state_route_sanitizes_provider_failure_detail(store):
    engine = SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(
            fetch_ha_states=lambda: (_ for _ in ()).throw(RuntimeError(f"auth failed with {FAKE_JWT}"))
        ),
    )
    status, payload = route(engine, "GET", "/api/scene_studio/fixture-state")
    assert status == 200
    detail = payload["providers"]["ha_light"]["detail"]
    assert FAKE_JWT not in detail


def test_trailing_slashes_are_tolerated(engine):
    status, _ = route(engine, "GET", "/api/scene_studio/status/")
    assert status == 200


def test_route_prefix_constant():
    assert ROUTE_PREFIX == "/api/scene_studio"
