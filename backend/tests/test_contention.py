"""External light-sync contention tests (hyperHDR ownership pass).

Covers: the hyperHDR serverinfo parser (streaming vs idle vs malformed),
WLED ``lor`` hold detection, fixture mapping (hue held / wled held / probe
fail-open), policy resolution (fixture > target > engine default), plan
splitting, and the engine-level gates: partial-yield apply, contended
refusal, takeover suspension + handback, held session transitions,
surrender auto-restore, resume refusal/override, and the stale-freeze
reconciliation. Recorded live payloads back the parser tests.
"""

import json
from pathlib import Path

import pytest

from scene_studio.domain.contention import (
    CONTENTION_DEFAULT_POLICY,
    FixtureContention,
    HyperHdrView,
    hue_active_entertainment_light_ids,
    map_fixture_contention,
    resolve_contention_policy,
    split_plan_by_contention,
    wled_held_device_ids,
)
from scene_studio.domain.fidelity import FidelityLevel, RenderPlan
from scene_studio.domain.playback import (
    ACCEPTED_STATE_SCHEMA_VERSIONS,
    PLAYBACK_STATE_SCHEMA_VERSION,
    PlaybackSessionState,
)
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore
from tests.test_service_engine import make_store

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"


def _serverinfo(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))["info"]


# ---------------------------------------------------------------------------
# pure parsing / mapping
# ---------------------------------------------------------------------------


def test_serverinfo_streaming_view_matches_recorded_payload():
    view = HyperHdrView.from_serverinfo(_serverinfo("hyperhdr_serverinfo.streaming.json"), "2026-09-24T12:00:00Z")
    assert view.available is True
    assert view.streaming is True
    assert [entry.name for entry in view.instances] == ["Workstation WLED Instance", "Workstation HUE Instance"]
    assert view.running_instances_named("hue")[0].instance == 1
    assert view.running_instances_named("wled")[0].instance == 0
    assert any("FLATBUFSERVER" in source for source in view.streaming_sources)


def test_serverinfo_idle_view_is_not_streaming():
    view = HyperHdrView.from_serverinfo(_serverinfo("hyperhdr_serverinfo.idle.json"), "2026-09-24T12:00:00Z")
    assert view.available is True
    assert view.streaming is False
    assert view.streaming_sources == []


def test_serverinfo_malformed_payload_degrades_to_unavailable():
    for payload in (None, {}, {"instance": "nope"}, []):
        view = HyperHdrView.from_serverinfo(payload, "2026-09-24T12:00:00Z")
        assert view.available is False
        assert view.streaming is False
        assert view.detail


def test_wled_lor_hold_detection():
    state = json.loads((FIXTURES_DIR / "wled_state.lor_active.json").read_text(encoding="utf-8"))
    assert wled_held_device_ids({"aabbccddeeff": state}) == {"aabbccddeeff"}
    surrendered = dict(state, lor=0)
    assert wled_held_device_ids({"aabbccddeeff": surrendered}) == set()
    assert wled_held_device_ids({"aabbccddeeff": {"on": True}}) == set()
    assert wled_held_device_ids(None) == set()


_ENT_CONFIGS_ONE_ACTIVE = {
    "data": [
        {
            "id": "cfg-gradient",
            "status": "inactive",
            "channels": [{"members": [{"service": {"rid": "ent-svc-mid", "rtype": "entertainment"}}]}],
        },
        {
            "id": "cfg-hq",
            "status": "active",
            "channels": [{"members": [{"service": {"rid": "ent-svc-g", "rtype": "entertainment"}}]}],
        },
    ]
}
_ENT_SERVICES = {
    "data": [
        {"id": "ent-svc-g", "owner": {"rid": "dev-g", "rtype": "device"}},
        {"id": "ent-svc-mid", "owner": {"rid": "dev-mid", "rtype": "device"}},
    ]
}
_ENT_DEVICES = {
    "data": [
        {"id": "dev-g", "services": [{"rtype": "light", "rid": "g-strip-rid"}]},
        {"id": "dev-mid", "services": [{"rtype": "light", "rid": "mid-rid"}]},
    ]
}


def test_hue_active_entertainment_light_ids_only_active_config_members():
    # Two configurations exist (mirrors a real bridge with a "Gradient" and
    # an "HQ" area); only the ACTIVE one's member light is held, even
    # though the inactive one's channel is present in the same payload.
    held = hue_active_entertainment_light_ids(_ENT_CONFIGS_ONE_ACTIVE, _ENT_SERVICES, _ENT_DEVICES)
    assert held == {"g-strip-rid"}


def test_hue_active_entertainment_light_ids_no_active_config():
    inactive_only = {"data": [dict(cfg, status="inactive") for cfg in _ENT_CONFIGS_ONE_ACTIVE["data"]]}
    assert hue_active_entertainment_light_ids(inactive_only, _ENT_SERVICES, _ENT_DEVICES) == set()


def test_hue_active_entertainment_light_ids_fails_open_on_malformed_payloads():
    for configs, services, devices in (
        (None, _ENT_SERVICES, _ENT_DEVICES),
        (_ENT_CONFIGS_ONE_ACTIVE, None, _ENT_DEVICES),
        (_ENT_CONFIGS_ONE_ACTIVE, _ENT_SERVICES, None),
        ({}, {}, {}),
        ("not a dict", [1, 2, 3], 42),
    ):
        assert hue_active_entertainment_light_ids(configs, services, devices) == set()


@pytest.fixture
def registry(sample_store):
    return sample_store.fixtures.registry()


@pytest.fixture
def sample_store(tmp_path):
    return make_store(tmp_path)


def test_map_fixture_contention_wled_and_hue(registry):
    streaming_view = HyperHdrView.from_serverinfo(_serverinfo("hyperhdr_serverinfo.streaming.json"), "now")
    # g_strip's light is a member of the (fake) active Entertainment
    # Configuration; middle_bar's is not -- only membership decides Hue
    # holds now, never "some hue_v2 fixture somewhere is held".
    holds = map_fixture_contention(
        registry, streaming_view,
        held_wled_device_ids={"aabbccddeeff"},
        held_hue_light_resource_ids={"g-strip-rid"},
    )
    assert holds["wled_seg"].held is True
    assert "lor" in holds["wled_seg"].owner
    assert holds["g_strip"].held is True
    assert "HUE" in holds["g_strip"].owner
    assert holds["middle_bar"].held is False
    # ha_light fixture can never be externally held
    assert holds["lamp"].held is False

    idle_view = HyperHdrView.from_serverinfo(_serverinfo("hyperhdr_serverinfo.idle.json"), "now")
    holds_idle = map_fixture_contention(registry, idle_view, held_wled_device_ids=set())
    assert holds_idle["wled_seg"].held is False
    assert holds_idle["g_strip"].held is False


def test_map_fixture_contention_probe_unavailable_fails_open(registry):
    view = HyperHdrView.unavailable("now", "probe down")
    holds = map_fixture_contention(registry, view, held_wled_device_ids=set())
    assert holds["g_strip"].held is False  # fail-open, surfaced via status detail


def test_map_fixture_contention_precise_hue_membership(registry):
    """hyperHDR "an input is active" (server-wide) must not blanket-hold
    every hue_v2 fixture -- only lights that are members of the currently
    ACTIVE Entertainment Configuration are held; a light hyperHDR isn't
    actually streaming to participates in scenes normally even while
    hyperHDR is busy with something else."""
    streaming_view = HyperHdrView.from_serverinfo(_serverinfo("hyperhdr_serverinfo.streaming.json"), "now")
    holds = map_fixture_contention(
        registry, streaming_view, held_wled_device_ids=set(),
        held_hue_light_resource_ids={"g-strip-rid"},
    )
    assert holds["g_strip"].held is True
    assert holds["middle_bar"].held is False

    # Streaming but no Entertainment Configuration is active (or the read
    # failed/came back empty): every hue_v2 fixture stays free (fail-open),
    # never the old "hold everything hue_v2" fallback.
    holds_none = map_fixture_contention(
        registry, streaming_view, held_wled_device_ids=set(),
        held_hue_light_resource_ids=set(),
    )
    assert holds_none["g_strip"].held is False
    assert holds_none["middle_bar"].held is False


def test_resolve_contention_policy_precedence(registry):
    g_strip = next(fixture for fixture in registry.fixtures if fixture.id == "g_strip")
    assert resolve_contention_policy(g_strip, registry.targets) == CONTENTION_DEFAULT_POLICY
    g_strip.contention_policy = "ignore"
    assert resolve_contention_policy(g_strip, registry.targets) == "ignore"
    g_strip.contention_policy = None
    registry.targets[0].contention_policy = "takeover"  # office sorts before studio
    assert resolve_contention_policy(g_strip, registry.targets) == "takeover"


def _plan_for(sample_store, scene_id="twilight"):
    from scene_studio.renderers import build_render_plan

    scene = sample_store.scenes.get_scene(scene_id)
    return build_render_plan(scene, sample_store.fixtures.registry(), target_ids=None)


def test_split_plan_by_contention(sample_store):
    plan = _plan_for(sample_store)
    assert [fp.fixture_id for fp in plan.fixture_plans] == ["g_strip", "middle_bar"]
    filtered, yielded, owners = split_plan_by_contention(
        plan, {"g_strip", "middle_bar"}, {"g_strip": "hyperHDR", "middle_bar": "hyperHDR"}
    )
    assert [fp.fixture_id for fp in filtered.fixture_plans] == []
    assert yielded == ["g_strip", "middle_bar"]
    assert any("yielded fixture 'g_strip'" in note for note in filtered.notes)

    filtered_takeover, yielded_takeover, _ = split_plan_by_contention(
        plan, {"g_strip", "middle_bar"}, {}, takeover_fixture_ids={"g_strip", "middle_bar"}
    )
    assert yielded_takeover == []
    assert [fp.fixture_id for fp in filtered_takeover.fixture_plans] == ["g_strip", "middle_bar"]


# ---------------------------------------------------------------------------
# engine gates
# ---------------------------------------------------------------------------


class FakeProbe:
    """Programmable hyperHDR probe fake."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if not self.payloads:
            return None
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


STREAMING = _serverinfo("hyperhdr_serverinfo.streaming.json")
IDLE = _serverinfo("hyperhdr_serverinfo.idle.json")

LOR_STATE = json.loads((FIXTURES_DIR / "wled_state.lor_active.json").read_text(encoding="utf-8"))
IDLE_WLED_STATE = dict(LOR_STATE, lor=0)

# Fake CLIP v2 entertainment topology for the sample registry's hue_v2
# fixtures (test_service_engine.make_store): each light resource id gets
# its own fake entertainment service + owning device, exactly the
# indirection the real bridge requires (light <- device <- entertainment
# service <- channel member).
_HUE_ENTERTAINMENT_TOPOLOGY = {
    "g-strip-rid": ("ent-svc-g", "dev-g"),
    "mid-rid": ("ent-svc-mid", "dev-mid"),
}


def _hue_services_and_devices() -> tuple[dict, dict]:
    services = {
        "data": [
            {"id": svc_id, "owner": {"rid": dev_id, "rtype": "device"}}
            for svc_id, dev_id in _HUE_ENTERTAINMENT_TOPOLOGY.values()
        ]
    }
    devices = {
        "data": [
            {"id": dev_id, "services": [{"rtype": "light", "rid": light_rid}]}
            for light_rid, (_svc_id, dev_id) in _HUE_ENTERTAINMENT_TOPOLOGY.items()
        ]
    }
    return services, devices


def _hue_configurations(active_light_resource_ids) -> dict | None:
    if active_light_resource_ids is None:
        return None  # simulate a failed configurations read
    channels = [
        {"members": [{"service": {"rid": _HUE_ENTERTAINMENT_TOPOLOGY[light_rid][0], "rtype": "entertainment"}}]}
        for light_rid in active_light_resource_ids
    ]
    status = "active" if active_light_resource_ids else "inactive"
    return {"data": [{"id": "cfg-1", "status": status, "channels": channels}]}


class FakeHueEntertainment:
    """Programmable fake for the 3 contention-only Hue fetchers. Each item
    in ``configs_sequence`` is the set of light resource ids that should be
    members of the one ACTIVE configuration for that ``compute_holds``
    call (or ``None`` to simulate a failed configurations read); services
    and devices stay a static full catalog, matching how the real bridge's
    topology barely changes while only "what's streaming now" does."""

    def __init__(self, configs_sequence):
        self.queue = list(configs_sequence)
        self._services, self._devices = _hue_services_and_devices()

    def fetch_configurations(self):
        active = self.queue.pop(0) if self.queue else set()
        return _hue_configurations(active)

    def fetch_services(self):
        return self._services

    def fetch_devices(self):
        return self._devices


def make_contention_engine(tmp_path, probe_payloads, wled_states=None, hue_entertainment=None):
    """Engine + store with a scripted hyperHDR probe, WLED state read, and
    (optionally) a scripted Hue Entertainment Configuration membership
    sequence. ``hue_entertainment=None`` leaves those fetchers unwired
    (`_fetch_hue_entertainment_data` returns ``None``), so no hue_v2
    fixture is ever held -- the right default for tests that don't care
    about Hue contention specifically."""
    store = make_store(tmp_path)
    probe = FakeProbe(probe_payloads)
    wled_queue = list(wled_states if wled_states is not None else [IDLE_WLED_STATE] * 8)

    def fetch_wled_state():
        return wled_queue.pop(0) if wled_queue else IDLE_WLED_STATE

    fetcher_kwargs = dict(fetch_wled_state=fetch_wled_state, wled_endpoint_hint="wled.local")
    if hue_entertainment is not None:
        fake_ent = FakeHueEntertainment(hue_entertainment)
        fetcher_kwargs.update(
            fetch_hue_entertainment_configurations=fake_ent.fetch_configurations,
            fetch_hue_entertainment_services=fake_ent.fetch_services,
            fetch_hue_devices=fake_ent.fetch_devices,
        )
    fetchers = DiscoveryFetchers(**fetcher_kwargs)
    engine = SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=fetchers,
        hyperhdr_probe=probe,
    )
    return engine, store, engine._executor


def test_apply_yields_only_the_actual_entertainment_member(tmp_path):
    # Regression for the "silently drops every Hue light" bug: hyperHDR
    # reports server-wide streaming (STREAMING probe), but only g_strip's
    # light is actually a member of the active Entertainment Configuration
    # -- middle_bar is a hue_v2 fixture hyperHDR simply isn't touching
    # right now and must execute normally, not get lumped in as "held"
    # just because it shares a provider with a light that IS held.
    engine, store, executor = make_contention_engine(
        tmp_path, [STREAMING], wled_states=[IDLE_WLED_STATE],
        hue_entertainment=[{"g-strip-rid"}],
    )
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is True
    contention = result["data"]["contention"]
    assert contention["yielded_fixture_ids"] == ["g_strip"]
    assert result["data"]["fixtures_planned"] == 1  # middle_bar only
    ops = executor.calls_for("hue.put_light")
    assert [call["fixture_id"] for call in ops] == ["middle_bar"]


def test_apply_partial_yield_records_pending_restore(tmp_path):
    # Hue side idle (probe), WLED under a realtime owner (lor=1): a mixed
    # office apply executes hue+ha fixtures and yields only the WLED one.
    engine, store, executor = make_contention_engine(
        tmp_path, [IDLE], wled_states=[LOR_STATE]
    )
    result = engine.handle({"command": "scene.apply", "scene_id": "aurora", "target_id": "office"})
    assert result["ok"] is True
    data = result["data"]
    contention = data["contention"]
    assert contention["evaluated"] is True
    assert contention["refused"] is False
    assert contention["yielded_fixture_ids"] == ["wled_seg"]
    assert "lor" in contention["owners"]["wled_seg"]
    # g_strip + middle_bar (hue, idle) + lamp (ha, never held) still executed.
    assert data["fixtures_planned"] == 3
    ops = executor.ops()
    assert "hue.put_light" in ops
    assert "ha.call_light" in ops
    assert "wled.post_state" not in ops
    pending = engine._contention.pending_restore()
    assert pending is not None
    assert pending["scene_id"] == "aurora"
    assert pending["fixture_ids"] == ["wled_seg"]


def test_apply_refused_when_every_fixture_is_held(tmp_path):
    # Both g_strip and middle_bar are members of the active Entertainment
    # Configuration; twilight's studio is hue-only, so the whole
    # executable set yields -> refused.
    engine, store, executor = make_contention_engine(
        tmp_path, [STREAMING], wled_states=[IDLE_WLED_STATE],
        hue_entertainment=[{"g-strip-rid", "mid-rid"}],
    )
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is False
    assert result["error"]["code"] == "contended"
    contention = result["error"]["details"]["contention"]
    assert contention["refused"] is True
    assert sorted(contention["yielded_fixture_ids"]) == ["g_strip", "middle_bar"]
    assert executor.ops() == []


def test_apply_takeover_suspends_hyperhdr_and_records_handback(tmp_path):
    engine, store, executor = make_contention_engine(
        tmp_path, [STREAMING], wled_states=[IDLE_WLED_STATE],
        hue_entertainment=[{"g-strip-rid", "mid-rid"}],
    )
    result = engine.handle({
        "command": "scene.apply",
        "scene_id": "twilight",
        "contention_override": "takeover",
    })
    assert result["ok"] is True
    contention = result["data"]["contention"]
    assert contention["override"] == "takeover"
    assert contention["yielded_fixture_ids"] == []
    stop_calls = executor.calls_for("hyperhdr.stop_instance")
    assert stop_calls, "takeover must suspend the overlapping hyperHDR instance"
    assert [call["payload"]["instance"] for call in stop_calls] == [1]  # hue side only
    handback = engine._contention.handback()
    assert handback is not None
    assert handback["instance_ids"] == [1]
    assert result["data"]["fixtures_planned"] == 2


def test_refresh_contention_transitions_sessions_and_restores(tmp_path):
    # Probe/WLED timeline: idle at start, streaming (grab), idle (surrender).
    engine, store, executor = make_contention_engine(
        tmp_path, [IDLE, STREAMING, IDLE],
        wled_states=[IDLE_WLED_STATE, IDLE_WLED_STATE, IDLE_WLED_STATE],
        # Membership sequence matching the probe timeline: nothing active
        # for the initial playback.start, both bars active for the grab,
        # nothing active again once hyperHDR's Entertainment session ends.
        hue_entertainment=[set(), {"g-strip-rid", "mid-rid"}, set()],
    )
    # Seed a pending restore as a yielded apply would.
    engine._contention.record_pending_restore({
        "scene_id": "twilight",
        "fixture_ids": ["g_strip", "middle_bar"],
        "target_ids": ["studio"],
        "recorded_at": "2026-09-24T12:00:00Z",
    })
    # Start playback while nothing is held...
    start = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert start["ok"] is True
    session_id = start["data"]["session_id"]
    # ...hyperHDR grabs (streaming probe) -> session HELD, no provider writes.
    ops_before = set(executor.ops())
    view = engine.refresh_contention()
    assert set(executor.ops()) == ops_before  # hold transition never fights the stream
    sessions = engine.status()["playback"]["sessions"]
    held = next(session for session in sessions if session["session_id"] == session_id)
    assert held["state"] == PlaybackSessionState.HELD
    assert held["held_by"]

    # TV off: hyperHDR idle -> held session becomes resumable and the
    # pending static look is auto-restored.
    view = engine.refresh_contention()
    assert view["restored_now"] is not None
    assert view["restored_now"]["scene_id"] == "twilight"
    sessions = engine.status()["playback"]["sessions"]
    released = next(session for session in sessions if session["session_id"] == session_id)
    assert released["state"] == PlaybackSessionState.PAUSED
    ops = executor.ops()
    assert "hue.put_light" in ops  # the auto-restore painted the held hue lights
    assert engine._contention.pending_restore() is None
    # ...and the session is resumable, never auto-resumed.
    assert released["state"] == PlaybackSessionState.PAUSED


def test_resume_refused_while_held_but_override_takes_over(tmp_path):
    engine, store, executor = make_contention_engine(
        tmp_path, [IDLE, STREAMING], wled_states=[IDLE_WLED_STATE]
    )
    start = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert start["ok"] is True
    session_id = start["data"]["session_id"]
    # Force the held transition bookkeeping.
    session = engine._playback_state.get(session_id)
    session.state = PlaybackSessionState.HELD
    session.held_by = "hyperHDR instance 'Workstation HUE Instance'"
    engine._persist_playback_state()

    paused = engine.handle({"command": "playback.pause", "session_id": session_id})
    assert paused["ok"] is False
    assert paused["error"]["code"] == "contended"

    resumed = engine.handle({"command": "playback.resume", "session_id": session_id})
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "contended"

    taken = engine.handle({
        "command": "playback.resume",
        "session_id": session_id,
        "contention_override": "takeover",
    })
    assert taken["ok"] is True
    assert "hyperhdr.stop_instance" in executor.ops()
    assert engine._playback_state.get(session_id).state == PlaybackSessionState.ACTIVE


def test_sync_suspend_then_resume_restarts_instances(tmp_path):
    # First probe idle so the seeding apply succeeds (nothing held).
    engine, store, executor = make_contention_engine(tmp_path, [IDLE, STREAMING])
    engine.handle({"command": "scene.apply", "scene_id": "twilight"})  # sets current scene
    suspended = engine.handle({"command": "sync.suspend", "target_id": "studio"})
    assert suspended["ok"] is True
    assert suspended["data"]["handback_recorded"] is True
    assert [call["payload"]["instance"] for call in executor.calls_for("hyperhdr.stop_instance")] == [1]

    executor.calls.clear()
    resumed = engine.handle({"command": "sync.resume"})
    assert resumed["ok"] is True
    starts = executor.calls_for("hyperhdr.start_instance")
    assert [call["payload"]["instance"] for call in starts] == [1]
    assert resumed["data"]["reassert"] is not None  # current scene re-asserted first
    assert engine._contention.handback() is None

    nothing = engine.handle({"command": "sync.resume"})
    assert nothing["ok"] is False  # no second suspend record


def test_fixture_set_contention_policy_persists(tmp_path):
    engine, store, _ = make_contention_engine(
        tmp_path, [STREAMING], hue_entertainment=[{"g-strip-rid", "mid-rid"}]
    )
    result = engine.handle({
        "command": "fixture.set_contention_policy",
        "fixture_id": "g_strip",
        "policy": "ignore",
    })
    assert result["ok"] is True
    stored = store.fixtures.get_fixture("g_strip")
    assert stored.contention_policy == "ignore"
    # policy "ignore": g_strip proceeds onto held fixtures, middle_bar yields.
    applied = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert applied["ok"] is True
    assert applied["data"]["contention"]["ignored_fixture_ids"] == ["g_strip"]
    assert applied["data"]["contention"]["yielded_fixture_ids"] == ["middle_bar"]
    assert applied["data"]["fixtures_planned"] == 1

    cleared = engine.handle({
        "command": "fixture.set_contention_policy",
        "fixture_id": "g_strip",
        "policy": "default",
    })
    assert cleared["ok"] is True
    assert store.fixtures.get_fixture("g_strip").contention_policy is None


def test_reconcile_wled_freezes_clears_unowned_segments(tmp_path):
    # Stateful device fake: the first read shows the stale freeze, the read
    # after a clear shows the surrendered device (as the real one would).
    device_state = dict(LOR_STATE)
    engine, store, executor = make_contention_engine(
        tmp_path, [IDLE], wled_states=[device_state, device_state]
    )

    result = engine.reconcile_wled_freezes()
    assert result["ok"] is True
    assert result["cleared_segment_ids"] == [0]
    clear_calls = executor.calls_for("wled.post_state")
    assert clear_calls
    assert clear_calls[0]["payload"] == {"seg": [{"id": 0, "frz": False}]}

    # Idempotent: once the device is unfrozen there is nothing to clear.
    device_state["seg"][0]["frz"] = False
    device_state["lor"] = 0
    result = engine.reconcile_wled_freezes()
    assert result["cleared_segment_ids"] == []


def test_playback_state_schema_v1_documents_still_load():
    from scene_studio.domain.playback import PlaybackState

    v1_doc = {
        "schema_version": 1,
        "sessions": {},
        "ownership": {},
    }
    state = PlaybackState.from_dict(v1_doc)
    assert state.sessions == {}
    assert PLAYBACK_STATE_SCHEMA_VERSION == 2
    assert ACCEPTED_STATE_SCHEMA_VERSIONS == (1, 2)


def test_contention_disabled_without_probe(tmp_path):
    store = make_store(tmp_path)
    engine = SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(),
    )
    status = engine.status()
    assert status["contention"]["configured"] is False
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is True
    assert result["data"]["contention"]["evaluated"] is False
