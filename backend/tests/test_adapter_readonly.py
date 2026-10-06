"""R1 read-only mode tests — pure, no AppDaemon, no network.

Covers the three independent enforcement layers plus the real discovery
fetcher wiring:

1. endpoint gate rejects mutating API commands server-side (with the
   dry-run/preview/discovery exceptions);
2. the executor refuses every provider operation before any transport;
3. ``initialize`` does not register the legacy mutating event listeners
   unless ``legacy_events_enabled`` is true *and* the derived mode is
   ``normal`` (verified through the stub base's registration log);
4. ``_build_discovery_fetchers`` performs GET-only reads and flattens the
   live WLED full-``/json`` shape (state nested + root catalogs) into the
   form ``discovery.build_wled_observations`` expects.

``_serve_endpoint`` returns ``(envelope, http_code)`` where ``envelope`` is
``{"status": <route status>, "body": <route payload>}`` and the HTTP code is
always 200 (AppDaemon replaces non-200 bodies with HTML).
"""

import sys
import types

import pytest

from scene_studio.appdaemon_adapter import (
    RequestsProviderExecutor,
    SceneStudioApp,
    _build_discovery_fetchers,
)
from scene_studio.appdaemon_adapter.adapter import _legacy_listeners_enabled
from scene_studio.appdaemon_adapter.ui_bridge import UI_COMMAND_EVENT
from scene_studio.domain.commands import COMMAND_CATALOG
from scene_studio.service.engine import SceneStudioEngine
from scene_studio.service.ports import MonotonicClock, RecordingExecutor
from scene_studio.stores import SceneStudioStore
from scene_studio.domain.fixtures import Fixture
from scene_studio.discovery.wled import build_wled_observations
from test_adapter_executor import FakeRequests, make_operation

ROUTE_COMMAND = {"method": "POST", "path": "/command"}


def _legacy_event_names(registered):
    """Registered event names EXCLUDING the always-on observational
    listeners — the assertion these tests care about is "no LEGACY
    (APPLY_SCENE/DELETE_SCENE/...) listeners", not "no listeners at all".
    Always registered in every runtime mode (engine policy still gates what
    they can do): the canonical UI bridge (HA card rework plan §4) and the
    HA automation change/reload signals that invalidate the derived routine
    cache (routines pass; bounded TTL + explicit refresh remain the
    backstop, so these listeners carry no write capability)."""
    always_on = {UI_COMMAND_EVENT, "automation_reloaded", "state_changed"}
    return [name for kind, name in registered if kind == "event" and name not in always_on]

MUTATING_COMMANDS = [
    "scene.apply",
    "scene.rename",
    "scene.archive",
    "scene.restore",
    "scene.save",
    "playback.start",
    "playback.pause",
    "playback.stop",
    "fixture.enable",
    "fixture.disable",
    "fixture.rebind",
    "fixture.reconcile",
    "registry.migrate",
]


# ---------------------------------------------------------------------------
# layer 1: server-side command gate
# ---------------------------------------------------------------------------


def _app_with_engine(read_only, tmp_path):
    """Gate tests run against a real policy-gated engine on an empty store."""
    from scene_studio.service.policy import RuntimePolicy

    app = SceneStudioApp()
    app._read_only = read_only
    app._engine = SceneStudioEngine(
        store=SceneStudioStore(tmp_path / "store"),
        executor=RecordingExecutor(),
        clock=MonotonicClock(),
        policy=RuntimePolicy.build("read_only" if read_only else "normal"),
    )
    return app


@pytest.fixture
def read_only_app(tmp_path):
    return _app_with_engine(read_only=True, tmp_path=tmp_path)


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_gate_rejects_every_mutating_command(read_only_app, command):
    ret, http_code = read_only_app._serve_endpoint(
        {**ROUTE_COMMAND, "body": {"command": command, "scene_id": "x"}}
    )
    assert http_code == 200  # AppDaemon replaces non-200 bodies with HTML
    assert ret["status"] == 200  # policy rejection rides in the result envelope
    assert ret["body"]["ok"] is False
    assert ret["body"]["error"]["code"] == "conflict"
    assert "read_only mode" in ret["body"]["error"]["message"]


def test_gate_allows_scene_apply_dry_run(read_only_app):
    ret, _ = read_only_app._serve_endpoint(
        {
            **ROUTE_COMMAND,
            "body": {"command": "scene.apply", "scene_id": "does-not-matter", "dry_run": True},
        }
    )
    assert ret["status"] == 200
    assert ret["body"]["ok"] is False  # empty store -> not_found, NOT read-only-rejected
    assert ret["body"]["error"]["code"] == "not_found"


def test_gate_allows_preview_discovery_retry_and_diagnostics(read_only_app):
    for command in (
        "scene.preview",
        "discovery.run",
        "diagnostics.export",
        "fixture.retry",
        "fixture.reconcile_preview",
        "registry.migration_preview",
    ):
        ret, _ = read_only_app._serve_endpoint({**ROUTE_COMMAND, "body": {"command": command}})
        assert ret["status"] in (200, 404), (command, ret)
        error = (ret["body"] or {}).get("error", {})
        assert error.get("code") != "conflict", (command, ret)


def test_gate_never_touches_read_routes(read_only_app):
    ret, _ = read_only_app._serve_endpoint({"method": "GET", "path": "/status"})
    assert ret["status"] == 200
    assert "error" not in ret["body"]


def test_gate_inert_when_not_read_only(tmp_path):
    app = _app_with_engine(read_only=False, tmp_path=tmp_path)
    ret, _ = app._serve_endpoint(
        {**ROUTE_COMMAND, "body": {"command": "scene.apply", "scene_id": "x"}}
    )
    # read-only off: the engine answers (not_found on the empty store), no gate rejection
    assert ret["status"] == 200
    assert ret["body"].get("error", {}).get("code") != "conflict"


# ---------------------------------------------------------------------------
# layer 2: executor refuses provider writes before transport
# ---------------------------------------------------------------------------


def test_read_only_executor_blocks_every_provider_before_transport():
    requests = FakeRequests(raise_exc=AssertionError("read-only executor must not touch the network"))
    executor = RequestsProviderExecutor(
        hue_ip="hue-bridge.local",
        hue_username="key",
        wled_endpoint="wled.local",
        call_service=lambda *a, **k: pytest.fail("read-only executor must not call HA"),
        requests_module=requests,
        read_only=True,
    )
    wled_fixture = Fixture.from_dict(
        {"id": "w", "name": "W", "binding": {"provider": "wled", "device_id": "d", "segment_ids": [0]}}
    )
    cases = [
        (make_operation("hue_v2", "hue.put_light", "rid"), Fixture.from_dict({"id": "h", "name": "H"})),
        (make_operation("wled", "wled.post_state", "d:seg:0"), wled_fixture),
        (
            make_operation("ha_light", "ha.call_light", "light.x"),
            Fixture.from_dict(
                {"id": "l", "name": "L", "binding": {"provider": "ha_light", "ha_entity_id": "light.x"}}
            ),
        ),
    ]
    for operation, fixture in cases:
        receipt = executor.execute(operation, fixture)
        assert receipt["ok"] is False, receipt
        assert receipt["detail"] == "read_only mode: provider writes disabled"
    assert requests.put_calls == [] and requests.post_calls == []


def test_executor_still_writes_when_not_read_only():
    requests = FakeRequests()
    executor = RequestsProviderExecutor(
        wled_endpoint="wled.local", requests_module=requests, read_only=False
    )
    fixture = Fixture.from_dict(
        {"id": "w", "name": "W", "binding": {"provider": "wled", "device_id": "d", "segment_ids": [0]}}
    )
    receipt = executor.execute(make_operation("wled", "wled.post_state", "d:seg:0"), fixture)
    assert receipt["ok"] is True
    assert len(requests.post_calls) == 1


# ---------------------------------------------------------------------------
# layer 3: legacy mutating listeners not registered in read_only mode
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_appdaemon(monkeypatch):
    """Install a minimal appdaemon.plugins.hass.hassapi stub for initialize()."""
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")

    class Hass:  # attribute stub
        pass

    hassapi.Hass = Hass
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "appdaemon.plugins.hass.hassapi", hassapi)
    sys.modules["appdaemon"].plugins = sys.modules["appdaemon.plugins"]
    sys.modules["appdaemon.plugins"].hass = sys.modules["appdaemon.plugins.hass"]
    sys.modules["appdaemon.plugins.hass"].hassapi = hassapi


def _make_init_app(logs, registered, tmp_path, read_only, extra=None):
    class InitApp(SceneStudioApp):
        def log(self, message, *a, **k):
            logs.append(str(message))

        def listen_event(self, callback, event_name, **kwargs):
            registered.append(("event", event_name))

        def register_endpoint(self, callback, endpoint=None, **kwargs):
            registered.append(("endpoint", endpoint))

    app = InitApp()
    app.args = {
        "read_only": read_only,
        "store_root": str(tmp_path / "store"),
        "hue_ip": "hue-bridge.local",
        "hue_username": "test-key",
        "wled_host": "wled.local",
        **(extra or {}),
    }
    return app


def test_legacy_listeners_independent_of_write_capability():
    assert _legacy_listeners_enabled(mode="read_only", legacy_events_enabled=False) is False
    assert _legacy_listeners_enabled(mode="read_only", legacy_events_enabled=True) is False
    assert _legacy_listeners_enabled(mode="normal", legacy_events_enabled=False) is False
    assert _legacy_listeners_enabled(mode="normal", legacy_events_enabled=True) is True
    for mode in ("registry_admin", "r2_validation", "r5_validation"):
        assert _legacy_listeners_enabled(mode=mode, legacy_events_enabled=True) is False
        assert _legacy_listeners_enabled(mode=mode, legacy_events_enabled=False) is False


def _stub_executor(monkeypatch, captured=None):
    def factory(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "scene_studio.appdaemon_adapter.adapter.RequestsProviderExecutor",
        factory,
    )


def test_read_only_initialize_skips_legacy_listeners(fake_appdaemon, monkeypatch, tmp_path):
    logs, registered = [], []
    _stub_executor(monkeypatch)
    app = _make_init_app(logs, registered, tmp_path, read_only=True)
    app.initialize()

    assert _legacy_event_names(registered) == [], (
        "read_only mode must not register ANY legacy event listeners"
    )
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert ("endpoint", "scene_studio_api") in registered
    assert any("READ ONLY" in line for line in logs)
    assert any("legacy_events=False" in line for line in logs)


def test_normal_mode_default_skips_legacy_listeners(fake_appdaemon, monkeypatch, tmp_path):
    logs, registered = [], []
    captured = {}
    _stub_executor(monkeypatch, captured)
    app = _make_init_app(logs, registered, tmp_path, read_only=False)
    app.initialize()

    assert _legacy_event_names(registered) == [], (
        "normal mode must not register legacy listeners unless explicitly enabled"
    )
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert ("endpoint", "scene_studio_api") in registered
    assert captured.get("read_only") is False
    assert captured.get("fixture_allowlist") is None
    assert app._engine._policy.mode == "normal"
    assert app._engine._policy.provider_writes_blocked is False
    assert app._engine._policy.allowed_commands == frozenset(COMMAND_CATALOG)
    assert any("NORMAL" in line and "legacy listeners off" in line for line in logs)
    assert any("legacy_events=False" in line for line in logs)


def test_normal_mode_registers_legacy_listeners_when_enabled(fake_appdaemon, monkeypatch, tmp_path):
    logs, registered = [], []
    captured = {}
    _stub_executor(monkeypatch, captured)
    app = _make_init_app(
        logs, registered, tmp_path, read_only=False, extra={"legacy_events_enabled": True}
    )
    app.initialize()

    event_names = [name for kind, name in registered if kind == "event"]
    assert "apply_scene_event" in event_names
    assert "save_light_states_event" in event_names
    assert "delete_scene_event" in event_names
    assert "generate_office_scene" in event_names
    assert UI_COMMAND_EVENT in event_names
    assert captured.get("read_only") is False
    assert captured.get("fixture_allowlist") is None
    assert app._engine._policy.mode == "normal"
    assert any("NORMAL" in line and "legacy listeners on" in line for line in logs)
    assert any("legacy_events=True" in line for line in logs)


@pytest.mark.parametrize(
    "read_only, extra",
    [
        (True, {"legacy_events_enabled": True}),
        (False, {"registry_admin": True, "legacy_events_enabled": True}),
        (
            False,
            {
                "r2_validation": True,
                "r2_fixture_allowlist": ["g_strip"],
                "legacy_events_enabled": True,
            },
        ),
        (
            False,
            {
                "r5_validation": True,
                "r5_fixture_allowlist": ["g_strip"],
                "legacy_events_enabled": True,
            },
        ),
    ],
)
def test_restricted_modes_never_register_legacy_listeners_even_when_flagged(
    fake_appdaemon, monkeypatch, tmp_path, read_only, extra
):
    logs, registered = [], []
    _stub_executor(monkeypatch)
    app = _make_init_app(logs, registered, tmp_path, read_only=read_only, extra=extra)
    app.initialize()
    assert _legacy_event_names(registered) == []
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert ("endpoint", "scene_studio_api") in registered
    assert any("legacy_events=False" in line for line in logs)


# ---------------------------------------------------------------------------
# real discovery fetchers: GET-only + live WLED shape flattening
# ---------------------------------------------------------------------------


def test_fetchers_use_get_only_and_flatten_live_wled_json(monkeypatch):
    calls = []

    class FakeGetRequests:
        def get(self, url, headers=None, timeout=None, verify=True):
            calls.append({"url": url, "verify": verify})
            return responses[url]

    responses = {
        "https://hue-bridge.local/clip/v2/resource/light": types.SimpleNamespace(
            status_code=200, json=lambda: {"data": [{"id": "hue-1", "metadata": {"name": "Lamp"}}]}
        ),
        "https://hue-bridge.local/clip/v2/resource/room": types.SimpleNamespace(
            status_code=200, json=lambda: {"data": []}
        ),
        "http://wled.local/json/info": types.SimpleNamespace(
            status_code=200, json=lambda: {"ver": "14.4", "mac": "aabbccddeeff"}
        ),
        "http://wled.local/json": types.SimpleNamespace(
            status_code=200,
            json=lambda: {
                "state": {"on": True, "bri": 128, "seg": [{"id": 0, "start": 0, "stop": 5}]},
                "effects": ["Solid", "Blink"],
                "palettes": ["Default"],
            },
        ),
    }
    monkeypatch.setitem(sys.modules, "requests", FakeGetRequests())

    class FakeApp:
        def get_state(self, domain):
            calls.append({"url": f"ha-get-state:{domain}"})
            return {"light.lamp": {"state": "on"}}

    fetchers = _build_discovery_fetchers(FakeApp(), "hue-bridge.local", "test-key", "wled.local", timeout=2.0)

    hue = fetchers.fetch_hue()
    rooms = fetchers.fetch_hue_rooms()
    info = fetchers.fetch_wled_info()
    state = fetchers.fetch_wled_state()
    ha = fetchers.fetch_ha_states()

    assert hue == {"data": [{"id": "hue-1", "metadata": {"name": "Lamp"}}]}
    assert rooms == {"data": []}
    assert info["mac"] == "aabbccddeeff"
    assert ha == {"light.lamp": {"state": "on"}}
    # flattening: seg at root + catalogs preserved (the builder's expected shape)
    assert state["seg"] == [{"id": 0, "start": 0, "stop": 5}]
    assert state["effects"] == ["Solid", "Blink"]
    assert state["palettes"] == ["Default"]

    # every network call was a GET; Hue reads carry verify=False (legacy parity)
    http_calls = [call for call in calls if not call["url"].startswith("ha-get-state:")]
    assert len(http_calls) == 4
    assert all(call["url"].startswith("http") for call in http_calls)
    hue_calls = [call for call in http_calls if call["url"].startswith("https://")]
    assert len(hue_calls) == 2 and all(call["verify"] is False for call in hue_calls)

    # the flattened state feeds the real builder end to end
    observations = build_wled_observations(info, state, "wled.local")
    assert len(observations) == 1
    assert observations[0].provider_resource_id == "aabbccddeeff:seg:0"
    assert observations[0].capabilities.effects == ["Solid", "Blink"]


def test_fetchers_return_none_when_unconfigured():
    class NoApp:
        def get_state(self, domain):  # pragma: no cover - must not be reached
            raise AssertionError("HA should not be queried when unconfigured")

    fetchers = _build_discovery_fetchers(NoApp(), None, None, None, timeout=1.0)
    assert fetchers.fetch_hue() is None
    assert fetchers.fetch_hue_rooms() is None
    assert fetchers.fetch_wled_info() is None
    assert fetchers.fetch_wled_state() is None


def test_fetchers_honor_ha_light_enabled_false():
    """providers.ha_light.enabled=false must actually disable HA-light discovery.

    The portable profile offered this toggle before the runtime honored it;
    this pins the wiring (corrective pass, review finding on 849c643).
    """

    class FakeApp:
        def __init__(self):
            self.domains = []

        def get_state(self, domain):
            self.domains.append(domain)
            return {"light.lamp": {"state": "on"}}

    app = FakeApp()
    enabled = _build_discovery_fetchers(app, None, None, None, timeout=1.0, ha_light_enabled=True)
    assert enabled.fetch_ha_states() == {"light.lamp": {"state": "on"}}

    disabled = _build_discovery_fetchers(app, None, None, None, timeout=1.0, ha_light_enabled=False)
    assert disabled.fetch_ha_states() is None
    assert app.domains == ["light"], "the disabled fetcher must never query HA"


def test_fetchers_tolerate_transport_failures(monkeypatch):
    class FailingRequests:
        def get(self, url, headers=None, timeout=None, verify=True):
            raise ConnectionError("controller offline")

    monkeypatch.setitem(sys.modules, "requests", FailingRequests())

    class NoApp:
        def get_state(self, domain):
            raise RuntimeError("HA unavailable")

    fetchers = _build_discovery_fetchers(NoApp(), "hue-bridge.local", "key", "wled.local", timeout=1.0)
    assert fetchers.fetch_hue() is None
    assert fetchers.fetch_wled_info() is None
    assert fetchers.fetch_wled_state() is None
    assert fetchers.fetch_ha_states() is None  # never raises out of a fetcher


# ---------------------------------------------------------------------------
# R2 validation-write mode: narrowly scoped writes for allowlisted fixtures
# ---------------------------------------------------------------------------


def _r2_app(tmp_path, allowlist=("g_strip", "lamp", "wled_seg_0")):
    app = SceneStudioApp()
    app._read_only = False
    app._r2_validation = True
    from scene_studio.service.policy import RuntimePolicy

    app._engine = SceneStudioEngine(
        store=SceneStudioStore(tmp_path / "store"),
        executor=RequestsProviderExecutor(
            requests_module=FakeRequests(), read_only=False, fixture_allowlist=set(allowlist)
        ),
        clock=MonotonicClock(),
        policy=RuntimePolicy.build("r2_validation"),
    )
    return app


def test_r2_gate_opens_scene_apply_but_keeps_other_mutations_locked(tmp_path):
    app = _r2_app(tmp_path)
    ret, _ = app._serve_endpoint(
        {**ROUTE_COMMAND, "body": {"command": "scene.apply", "scene_id": "r2_hue_color"}}
    )
    assert ret["status"] == 200  # apply passes the gate (not_found on empty store)
    assert ret["body"]["error"]["code"] == "not_found"

    for command, params in [
        ("scene.rename", {"scene_id": "x", "name": "y"}),
        ("scene.archive", {"scene_id": "x"}),
        ("scene.save", {"name": "x"}),
        ("playback.start", {"scene_id": "x"}),
        ("fixture.enable", {"fixture_id": "lamp"}),
        ("fixture.rebind", {"fixture_id": "lamp", "observation_id": "o"}),
    ]:
        ret, _ = app._serve_endpoint({**ROUTE_COMMAND, "body": {"command": command, **params}})
        assert ret["status"] == 200, command  # envelope carries the rejection
        assert ret["body"]["ok"] is False, command
        assert ret["body"]["error"]["code"] == "conflict", command
        assert "r2_validation mode" in ret["body"]["error"]["message"], command


def test_r2_executor_writes_allowlisted_fixture(tmp_path):
    requests = FakeRequests()
    executor = RequestsProviderExecutor(
        wled_endpoint="wled.local", requests_module=requests, read_only=False,
        fixture_allowlist={"g_strip", "lamp", "wled_seg_0"},
    )
    wled = Fixture.from_dict(
        {"id": "wled_seg_0", "name": "W0", "binding": {"provider": "wled", "device_id": "aabbccddeeff",
                                                        "segment_ids": [0], "endpoint_hint": "http://wled.local"}}
    )
    receipt = executor.execute(make_operation("wled", "wled.post_state", "aabbccddeeff:seg:0"), wled)
    assert receipt["ok"] is True
    assert len(requests.post_calls) == 1


def test_r2_executor_refuses_fixture_outside_allowlist(tmp_path):
    requests = FakeRequests()
    executor = RequestsProviderExecutor(
        hue_ip="hue-bridge.local", hue_username="key", requests_module=requests, read_only=False,
        fixture_allowlist={"g_strip", "lamp", "wled_seg_0"},
    )
    victim = Fixture.from_dict(
        {"id": "middle_bar", "name": "MB",
         "binding": {"provider": "hue_v2", "bridge_id": "b", "resource_id": "68ad5817"}}
    )
    receipt = executor.execute(make_operation("hue_v2", "hue.put_light", "68ad5817"), victim)
    assert receipt["ok"] is False
    assert "not in the R2 validation allowlist" in receipt["detail"]
    assert requests.put_calls == []


def test_r2_initialize_registers_no_legacy_listeners(fake_appdaemon, monkeypatch, tmp_path):
    logs, registered = [], []
    captured = {}

    class InitApp(SceneStudioApp):
        def log(self, message, *a, **k):
            logs.append(str(message))

        def listen_event(self, callback, event_name, **kwargs):
            registered.append(("event", event_name))

        def register_endpoint(self, callback, endpoint=None, **kwargs):
            registered.append(("endpoint", endpoint))

    app = InitApp()
    app.args = {
        "read_only": False,
        "r2_validation": True,
        "r2_fixture_allowlist": ["g_strip", "lamp", "wled_seg_0"],
        "store_root": str(tmp_path / "store"),
        "hue_ip": "hue-bridge.local",
        "hue_username": "test-key",
        "wled_host": "wled.local",
    }
    monkeypatch.setattr(
        "scene_studio.appdaemon_adapter.adapter.RequestsProviderExecutor",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    app.initialize()

    assert _legacy_event_names(registered) == []
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert captured.get("read_only") is False
    assert captured.get("fixture_allowlist") == {"g_strip", "lamp", "wled_seg_0"}
    assert any("R2 VALIDATION" in line for line in logs)


# ---------------------------------------------------------------------------
# runtime mode derivation (R5D preflight corrective: r5_validation must be
# reachable from the app config; extracted as a pure, testable helper)
# ---------------------------------------------------------------------------

def test_mode_derivation_r5_validation_is_reachable_and_wins_over_r2():
    from scene_studio.appdaemon_adapter.adapter import _derive_mode_and_allowlist

    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=False, r5_validation=True,
        r2_allowlist=[], r5_allowlist=["g_strip", "wled_seg_0"],
    )
    assert mode == "r5_validation"
    assert allowlist == {"g_strip", "wled_seg_0"}

    # r5 wins even when r2 flags linger in the config
    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=True, r5_validation=True,
        r2_allowlist=["lamp"], r5_allowlist=["g_strip"],
    )
    assert mode == "r5_validation"
    assert allowlist == {"g_strip"}

    # fail-closed: r5 with a missing/empty allowlist must NEVER degrade into
    # an unrestricted (None) executor allowlist — refuse the configuration.
    with pytest.raises(ValueError, match="r5_fixture_allowlist"):
        _derive_mode_and_allowlist(
            read_only=False, r2_validation=False, r5_validation=True,
            r2_allowlist=[], r5_allowlist=[],
        )
    with pytest.raises(ValueError, match="r5_fixture_allowlist"):
        _derive_mode_and_allowlist(
            read_only=False, r2_validation=False, r5_validation=True,
            r2_allowlist=[], r5_allowlist=None,
        )

    # the configured allowlist is preserved exactly — every configured id is
    # present and nothing is added, whatever the config order
    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=False, r5_validation=True,
        r2_allowlist=[], r5_allowlist=["wled_seg_0", "g_strip", "wled_seg_0"],
    )
    assert mode == "r5_validation"
    assert allowlist == {"g_strip", "wled_seg_0"}


def test_mode_derivation_r2_read_only_and_normal():
    from scene_studio.appdaemon_adapter.adapter import _derive_mode_and_allowlist

    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=True, r5_validation=False,
        r2_allowlist=["g_strip", "lamp", "wled_seg_0"], r5_allowlist=[],
    )
    assert mode == "r2_validation"
    assert allowlist == {"g_strip", "lamp", "wled_seg_0"}

    mode, allowlist = _derive_mode_and_allowlist(
        read_only=True, r2_validation=False, r5_validation=False,
        r2_allowlist=[], r5_allowlist=[],
    )
    assert mode == "read_only"
    assert allowlist is None

    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=False, r5_validation=False,
        r2_allowlist=[], r5_allowlist=[],
    )
    assert mode == "normal"
    assert allowlist is None

    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=False, r5_validation=False,
        r2_allowlist=[], r5_allowlist=[], registry_admin=True,
    )
    assert mode == "registry_admin"
    assert allowlist is None

    # validation modes still win over registry_admin
    mode, allowlist = _derive_mode_and_allowlist(
        read_only=False, r2_validation=True, r5_validation=False,
        r2_allowlist=["lamp"], r5_allowlist=[], registry_admin=True,
    )
    assert mode == "r2_validation"


def test_registry_admin_initialize_blocks_provider_writes_and_legacy_listeners(
    fake_appdaemon, monkeypatch, tmp_path
):
    logs, registered = [], []
    captured = {}
    monkeypatch.setattr(
        "scene_studio.appdaemon_adapter.adapter.RequestsProviderExecutor",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    app = _make_init_app(logs, registered, tmp_path, read_only=False, extra={"registry_admin": True})
    app.initialize()
    assert _legacy_event_names(registered) == []
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert captured.get("read_only") is True
    assert any("REGISTRY ADMIN" in line for line in logs)
    assert app._engine._policy.mode == "registry_admin"
    assert app._engine._policy.provider_writes_blocked is True


# ---------------------------------------------------------------------------
# R5 initialize behavior (R5D pre-live corrective pass): truthful R5 logging
# and fail-closed refusal when the fixture allowlist is missing/empty.
# ---------------------------------------------------------------------------

def test_r5_initialize_logs_validation_with_exact_allowlist(
    fake_appdaemon, monkeypatch, tmp_path
):
    logs, registered = [], []
    captured = {}
    monkeypatch.setattr(
        "scene_studio.appdaemon_adapter.adapter.RequestsProviderExecutor",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    app = _make_init_app(
        logs, registered, tmp_path, read_only=False,
        extra={
            "r5_validation": True,
            "r5_fixture_allowlist": ["g_strip", "wled_seg_0"],
        },
    )
    app.initialize()

    # the executor receives the configured allowlist exactly, and the
    # executor write gate is NOT the read-only blanket refusal
    assert captured.get("fixture_allowlist") == {"g_strip", "wled_seg_0"}
    assert captured.get("read_only") is False

    # legacy mutating listeners stay off; the endpoint is still registered
    assert _legacy_event_names(registered) == []
    assert ("event", UI_COMMAND_EVENT) in registered  # UI bridge: always on, engine policy still gates it
    assert ("endpoint", "scene_studio_api") in registered

    # the R5 message is explicit about validation mode + effective allowlist
    # and never claims READ ONLY / disabled provider writes
    r5_lines = [line for line in logs if "R5 VALIDATION" in line]
    assert len(r5_lines) == 1
    assert "['g_strip', 'wled_seg_0']" in r5_lines[0]
    assert not any("READ ONLY" in line for line in logs)


def test_r5_initialize_refused_without_allowlist(fake_appdaemon, tmp_path):
    logs, registered = [], []
    app = _make_init_app(
        logs, registered, tmp_path, read_only=False,
        extra={"r5_validation": True},
    )
    # fail closed: initialization itself raises a clear configuration error
    with pytest.raises(ValueError, match="r5_fixture_allowlist"):
        app.initialize()
    # nothing was registered and no executor was built (the derivation
    # precedes engine construction), so a misconfigured R5 start cannot go
    # writable
    assert registered == []
