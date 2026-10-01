"""SceneStudioApp <-> ui_bridge integration — pure, no AppDaemon, no network.

Covers the adapter-level wiring `test_ui_bridge.py` cannot: the
`scene_studio_ui_command` event handler, the `sensor.scene_studio_ui`
projection publish (revision-gated, forced after a card command), and the
narrow allowlist being enforced BEFORE the engine is ever called (HA card
rework plan §4/§9/§13).
"""

from scene_studio.appdaemon_adapter import SceneStudioApp
from scene_studio.appdaemon_adapter.ui_bridge import BRIDGE_SCHEMA_VERSION, UI_PROJECTION_ENTITY
from scene_studio.service.engine import SceneStudioEngine
from scene_studio.service.policy import RuntimePolicy
from scene_studio.service.ports import MonotonicClock, RecordingExecutor
from scene_studio.stores import SceneStudioStore


class RecordingSetState(SceneStudioApp):
    """Captures every set_state call instead of touching a real HA."""

    def __init__(self):
        self.published = []

    def log(self, message, *a, **k):
        pass

    def error(self, message, *a, **k):
        self.published.append(("__error__", message))

    def set_state(self, entity_id, state=None, attributes=None, **kwargs):
        self.published.append((entity_id, state, attributes))


def _app_with_scene(tmp_path, *, mode="normal", scene_id="twilight", dynamic=False):
    app = RecordingSetState()
    store = SceneStudioStore(tmp_path / "store")
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    store.fixtures.add_fixture(
        {
            "id": "lamp",
            "name": "Lamp",
            "groups": ["studio"],
            "enabled": True,
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    motion = {"mode": "palette_cycle", "speed": 0.5, "strategy": "auto"} if dynamic else {"mode": "static", "speed": 0.0, "strategy": "auto"}
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": scene_id,
            "name": scene_id.title(),
            "target_ids": ["studio"],
            "palette": ["#112233", "#445566"],
            "motion": motion,
        }
    )
    app._engine = SceneStudioEngine(
        store=store,
        executor=RecordingExecutor(),
        clock=MonotonicClock(),
        policy=RuntimePolicy.build(mode),
    )
    return app


# ---------------------------------------------------------------------------
# allowlist enforced before the engine is ever called (plan §4/§9)
# ---------------------------------------------------------------------------


def test_disallowed_command_never_reaches_the_engine(tmp_path):
    app = _app_with_scene(tmp_path)
    calls_before = app._engine._revision
    app._on_ui_command("scene_studio_ui_command", {"command": "fixture.enable", "fixture_id": "lamp"}, {})
    assert app._engine._revision == calls_before  # nothing was dispatched
    entity, state, attrs = app.published[-1]
    assert entity == UI_PROJECTION_ENTITY
    assert attrs["last_command"]["ok"] is False
    assert attrs["last_command"]["command"] is None or attrs["last_command"]["command"] == "fixture.enable"
    assert "not permitted through the UI bridge" in attrs["last_command"]["error"]


def test_malformed_event_payload_publishes_an_honest_rejection(tmp_path):
    app = _app_with_scene(tmp_path)
    app._on_ui_command("scene_studio_ui_command", None, {})
    entity, _state, attrs = app.published[-1]
    assert entity == UI_PROJECTION_ENTITY
    assert attrs["last_command"]["ok"] is False


# ---------------------------------------------------------------------------
# scene type/action mapping actually executes (plan §5/§13)
# ---------------------------------------------------------------------------


def test_scene_apply_routes_through_the_real_engine(tmp_path):
    app = _app_with_scene(tmp_path, scene_id="twilight")
    app._on_ui_command(
        "scene_studio_ui_command",
        {"command": "scene.apply", "scene_id": "twilight", "request_id": "r1"},
        {},
    )
    entity, state, attrs = app.published[-1]
    assert entity == UI_PROJECTION_ENTITY
    assert attrs["last_command"] == {"request_id": "r1", "ok": True, "command": "scene.apply"}
    assert state == "normal"


def test_playback_start_then_pause_uses_the_backend_issued_session_id(tmp_path):
    app = _app_with_scene(tmp_path, scene_id="aurora_flow", dynamic=True)
    app._on_ui_command(
        "scene_studio_ui_command", {"command": "playback.start", "scene_id": "aurora_flow"}, {}
    )
    started = app.published[-1][2]["last_command"]
    assert started["ok"] is True
    live_sessions = [s for s in app._engine.status()["playback"]["sessions"] if s["state"] == "active"]
    assert len(live_sessions) == 1
    session_id = live_sessions[0]["session_id"]

    app._on_ui_command("scene_studio_ui_command", {"command": "playback.pause", "session_id": session_id}, {})
    paused = app.published[-1][2]["last_command"]
    assert paused == {"request_id": None, "ok": True, "command": "playback.pause"}

    app._on_ui_command("scene_studio_ui_command", {"command": "playback.resume", "session_id": session_id}, {})
    resumed = app.published[-1][2]["last_command"]
    assert resumed["ok"] is True

    app._on_ui_command("scene_studio_ui_command", {"command": "playback.stop", "session_id": session_id}, {})
    stopped = app.published[-1][2]["last_command"]
    assert stopped["ok"] is True


def test_pause_with_a_wrong_session_id_is_a_conflict_never_a_fabricated_success(tmp_path):
    """Plural-session correctness (plan §6): an unknown/stale session_id must
    be rejected by the real engine, not silently no-op'd into 'ok'."""
    app = _app_with_scene(tmp_path, scene_id="aurora_flow", dynamic=True)
    app._on_ui_command(
        "scene_studio_ui_command", {"command": "playback.pause", "session_id": "sess-does-not-exist"}, {}
    )
    result = app.published[-1][2]["last_command"]
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# runtime policy truth: the bridge allowlists the command shape, the engine
# still enforces what the current mode actually permits (plan §9)
# ---------------------------------------------------------------------------


def test_read_only_projection_truthfully_reports_empty_allowed_commands(tmp_path):
    app = _app_with_scene(tmp_path, mode="read_only")
    app._publish_projection(force=True)
    _entity, _state, attrs = app.published[-1]
    assert attrs["allowed_commands"] == []


def test_normal_mode_projection_reports_the_full_bridge_allowlist(tmp_path):
    app = _app_with_scene(tmp_path, mode="normal")
    app._publish_projection(force=True)
    _entity, _state, attrs = app.published[-1]
    assert attrs["allowed_commands"] == [
        "playback.pause", "playback.resume", "playback.start", "playback.stop",
        "scene.apply", "scene.archive",
    ]
    assert attrs["bridge_schema_version"] == BRIDGE_SCHEMA_VERSION
    assert attrs["targets"][0]["id"] == "studio"
    assert attrs["targets"][0]["complete_ha_coverage"] is True
    assert attrs["current"] is None or "scene_id" in (attrs["current"] or {})


def test_read_only_mode_rejects_scene_apply_through_the_bridge(tmp_path):
    app = _app_with_scene(tmp_path, mode="read_only")
    app._on_ui_command(
        "scene_studio_ui_command", {"command": "scene.apply", "scene_id": "twilight"}, {}
    )
    result = app.published[-1][2]["last_command"]
    assert result["ok"] is False
    assert "read_only mode" in result["error"]


# ---------------------------------------------------------------------------
# projection publish rules (plan §4 "Projection rules")
# ---------------------------------------------------------------------------


def test_projection_is_forced_after_every_card_command_even_a_rejection(tmp_path):
    app = _app_with_scene(tmp_path)
    published_before = len(app.published)
    app._on_ui_command("scene_studio_ui_command", {"command": "fixture.enable"}, {})
    assert len(app.published) == published_before + 1  # published despite zero engine mutation


def test_unforced_publish_skips_when_the_revision_has_not_moved(tmp_path):
    app = _app_with_scene(tmp_path)
    app._publish_projection(force=True)  # establishes a baseline revision
    published_before = len(app.published)
    app._publish_projection()  # no mutation happened in between
    assert len(app.published) == published_before


def test_unforced_publish_fires_when_the_revision_moved(tmp_path):
    app = _app_with_scene(tmp_path)
    app._publish_projection(force=True)
    app._engine.handle({"command": "scene.apply", "scene_id": "twilight"})  # bumps the revision
    published_before = len(app.published)
    app._publish_projection()
    assert len(app.published) == published_before + 1


def test_serve_endpoint_publishes_projection_after_a_command_route(tmp_path):
    """A mutation arriving through the OTHER transport (the Workbench, via
    _serve_endpoint) must still refresh the projection (plan §4 'when
    engine revision changes') — not only card-originated commands."""
    app = _app_with_scene(tmp_path, scene_id="twilight")
    published_before = len(app.published)
    app._serve_endpoint({"method": "POST", "path": "/command", "body": {"command": "scene.apply", "scene_id": "twilight"}})
    assert len(app.published) == published_before + 1
    assert app.published[-1][0] == UI_PROJECTION_ENTITY


def test_serve_endpoint_does_not_publish_for_plain_reads(tmp_path):
    app = _app_with_scene(tmp_path)
    app._publish_projection(force=True)
    published_before = len(app.published)
    app._serve_endpoint({"method": "GET", "path": "/status"})
    app._serve_endpoint({"method": "GET", "path": "/scenes"})
    assert len(app.published) == published_before


def test_projection_scenes_reflect_the_active_catalog(tmp_path):
    app = _app_with_scene(tmp_path, scene_id="twilight")
    app._publish_projection(force=True)
    _entity, _state, attrs = app.published[-1]
    assert attrs["scenes"] == [
        {
            "id": "twilight",
            "name": "Twilight",
            "palette": ["#112233", "#445566"],
            "motion_mode": "static",
            "target_ids": ["studio"],
        }
    ]


def test_publish_projection_never_raises_when_set_state_is_unavailable(tmp_path):
    """Defensive guard parity with register_endpoint's hasattr check: a base
    without set_state (mirrors an older/partial AppDaemon surface) must
    never raise out of the publish path."""

    class BareApp:
        def __init__(self, engine):
            self._engine = engine

        def error(self, message, *a, **k):
            pass

    store = SceneStudioStore(tmp_path / "store")
    engine = SceneStudioEngine(store=store, executor=RecordingExecutor(), clock=MonotonicClock())
    bare = BareApp(engine)
    assert not hasattr(bare, "set_state")

    SceneStudioApp._publish_projection(bare, force=True)  # must not raise
