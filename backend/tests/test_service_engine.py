"""Engine tests — integration wave 1 exit gate (plan §9).

Covers: explicit-ID scene.apply against a recording executor, dry-run
preview, rename/archive/restore, fixture disable/rebind (missing -> ready),
fixture.retry, discovery.run with fake fetchers, playback start/pause/stop
+ conflict paths, static apply cancels playback, revision semantics,
bounded events, redacted diagnostics export, and the never-raise guarantee.
"""

import json
import threading

import pytest

from scene_studio.domain.commands import CommandResult
from scene_studio.domain.events import EventCategory, EventLevel
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore

# A JWT-shaped fake (header.payload.signature) used ONLY to prove redaction.
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJzY2VuZS1zdHVkaW8tdGVzdCIsImV4cCI6OTk5OTk5OTk5OX0.abc123def456"  # secrets-scan:allow (fake test fixture)
)


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------


def make_store(root):
    """Registry + scene set covering hue/wled/ha_light providers and one disabled fixture."""
    store = SceneStudioStore(root)
    fixtures = store.fixtures
    fixtures.add_fixture(
        {
            "id": "g_strip",
            "name": "Hue G Strip",
            "groups": ["office", "studio"],
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "g-strip-rid"},
            "capabilities": {
                "on_off": True,
                "brightness": True,
                "color_xy": True,
                "gradient": {"max_points": 5},
                "dynamic_native": True,
            },
        }
    )
    fixtures.add_fixture(
        {
            "id": "middle_bar",
            "name": "Middle Bar",
            "groups": ["office", "studio"],
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "mid-rid"},
            "capabilities": {"on_off": True, "brightness": True, "color_xy": True},
        }
    )
    fixtures.add_fixture(
        {
            "id": "wled_seg",
            "name": "WLED Segment",
            "groups": ["office"],
            "binding": {"provider": "wled", "device_id": "aabbccddeeff", "segment_ids": [0]},
            "capabilities": {"on_off": True, "brightness": True, "dynamic_native": True},
        }
    )
    fixtures.add_fixture(
        {
            "id": "lamp",
            "name": "Lamp",
            "groups": ["office"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    fixtures.add_fixture(
        {
            "id": "double_strip",
            "name": "Double Strip",
            "groups": ["office"],
            "enabled": False,
            "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "ds-rid"},
            "metadata": {"note": "house-move leave-behind reference case"},
        }
    )
    fixtures.add_target({"id": "office", "name": "Office"})
    fixtures.add_target({"id": "studio", "name": "Studio"})

    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "twilight",
            "name": "Twilight",
            "target_ids": ["studio"],
            "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
            "fixture_states": {
                "g_strip": {"on": True, "brightness": 55.0, "color": "#112233"},
                "middle_bar": {"on": True, "brightness": 50.0, "color": "#445566"},
            },
        }
    )
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "aurora",
            "name": "Aurora Flow",
            "target_ids": ["office"],
            "palette": ["#00e5ff", "#2979ff"],
            "motion": {"mode": "palette_cycle", "speed": 0.4, "strategy": "auto"},
            "default_state": {"on": True, "brightness": 60.0},
        }
    )
    return store


def make_engine(store, fetchers=None, executor=None, capacity=500):
    return SceneStudioEngine(
        store,
        executor or RecordingExecutor(),
        SteppingClock(),
        event_capacity=capacity,
        discovery_fetchers=fetchers or DiscoveryFetchers(),
    )


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


@pytest.fixture
def executor():
    return RecordingExecutor()


@pytest.fixture
def engine(store, executor):
    return make_engine(store, executor=executor)


# ---------------------------------------------------------------------------
# scene.apply
# ---------------------------------------------------------------------------


def test_apply_explicit_target_executes_expected_ops(engine, executor):
    result = engine.handle({"command": "scene.apply", "scene_id": "aurora", "target_id": "office"})
    assert result["ok"] is True
    assert result["command"] == "scene.apply"
    data = result["data"]
    # double_strip is disabled -> skipped; every other office fixture planned.
    assert data["skipped_fixture_ids"] == ["double_strip"]
    assert data["fixtures_planned"] == 4
    assert data["ops_failed"] == 0
    ops = executor.ops()
    assert "hue.put_light" in ops  # g_strip native per-light dynamic palette + middle_bar static snapshot
    assert "wled.post_state" in ops
    assert "ha.call_light" in ops
    # per-op receipts carry ok flags and fixture attribution
    receipt_by_fixture = {entry["fixture_id"]: entry for entry in data["receipts"]}
    assert receipt_by_fixture["g_strip"]["ok"] is True
    assert receipt_by_fixture["g_strip"]["op"] == "hue.put_light"
    assert receipt_by_fixture["g_strip"]["resource_ref"] == "g-strip-rid"
    # current scene/target recorded for status()
    assert engine.status()["current"] == {"scene_id": "aurora", "target_id": "office"}


def test_apply_full_scene_uses_scene_targets_and_summary_event(engine):
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is True
    assert result["data"]["target_ids"] == ["studio"]
    started = next(event for event in engine.recent_events(5) if event.summary == "Twilight started")
    assert started.category is EventCategory.SCENE
    assert started.detail == "2 fixtures • static"


def test_apply_dry_run_returns_plan_without_executor_calls_or_revision(store, executor):
    engine = make_engine(store, executor=executor)
    before = engine.status()["engine"]["revision"]
    result = engine.handle({"command": "scene.apply", "scene_id": "aurora", "dry_run": True})
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    plan = result["data"]["render_plan"]
    assert plan["scene_id"] == "aurora"
    assert plan["skipped_fixture_ids"] == ["double_strip"]
    planned_ids = {fixture_plan["fixture_id"] for fixture_plan in plan["fixture_plans"]}
    assert "g_strip" in planned_ids
    assert executor.calls == []  # no device contact
    assert engine.status()["engine"]["revision"] == before  # read-only


def test_preview_forces_dry_run(engine, executor):
    result = engine.handle({"command": "scene.preview", "scene_id": "twilight", "target_id": "studio"})
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True
    assert "render_plan" in result["data"]
    assert executor.calls == []
    assert engine.status()["current"] is None  # preview never sets current scene


def test_apply_unknown_scene_or_target_is_not_found(engine, executor):
    missing_scene = engine.handle({"command": "scene.apply", "scene_id": "nope"})
    assert missing_scene["ok"] is False
    assert missing_scene["error"]["code"] == "not_found"
    missing_target = engine.handle({"command": "scene.apply", "scene_id": "twilight", "target_id": "nope"})
    assert missing_target["error"]["code"] == "not_found"
    assert executor.calls == []


def test_apply_reports_per_op_failures_in_receipts_but_stays_ok(store, executor):
    executor.fail_ops.add("wled.post_state")
    engine = make_engine(store, executor=executor)
    result = engine.handle({"command": "scene.apply", "scene_id": "aurora"})
    assert result["ok"] is True  # documented best-effort choice
    data = result["data"]
    assert data["ops_failed"] == 1
    failed = next(entry for entry in data["receipts"] if not entry["ok"])
    assert failed["fixture_id"] == "wled_seg"
    failure_events = [event for event in engine.recent_events(10) if event.level is EventLevel.ERROR]
    assert any(event.fixture_id == "wled_seg" for event in failure_events)


# ---------------------------------------------------------------------------
# rename / archive / restore / save
# ---------------------------------------------------------------------------


def test_rename_archive_restore_roundtrip(engine):
    renamed = engine.handle({"command": "scene.rename", "scene_id": "twilight", "name": "Dusk"})
    assert renamed["ok"] is True
    assert renamed["data"]["scene"]["name"] == "Dusk"
    assert renamed["data"]["scene"]["id"] == "twilight"  # ids immutable

    archived = engine.handle({"command": "scene.archive", "scene_id": "twilight"})
    assert archived["ok"] is True
    assert archived["data"]["scene"]["metadata"]["archived_at"]
    gone = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert gone["error"]["code"] == "not_found"

    restored = engine.handle({"command": "scene.restore", "scene_id": "twilight"})
    assert restored["ok"] is True
    assert "archived_at" not in restored["data"]["scene"].get("metadata", {})
    assert engine.handle({"command": "scene.preview", "scene_id": "twilight"})["ok"] is True


def test_archive_clears_playback_of_archived_scene(store, executor):
    engine = make_engine(store, executor=executor)
    engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert engine.status()["playback"]["counts"]["active"] == 1
    engine.handle({"command": "scene.archive", "scene_id": "aurora"})
    assert engine.status()["playback"]["counts"]["active"] == 0
    assert engine.status()["playback"]["counts"]["stopped"] == 1


def test_archive_clears_current_scene_pointer(store, executor):
    engine = make_engine(store, executor=executor)
    engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert engine.status()["current"] is not None
    engine.handle({"command": "scene.archive", "scene_id": "twilight"})
    assert engine.status()["current"] is None


def test_scene_save_creates_empty_draft(engine):
    result = engine.handle({"command": "scene.save", "name": "Movie Night", "target_id": "office"})
    assert result["ok"] is True
    scene = result["data"]["scene"]
    assert scene["id"] == "movie_night"
    assert scene["schema_version"] == 2
    assert scene.get("fixture_states", {}) == {}  # empty draft
    assert "later wave" in result["data"]["note"]

    duplicate = engine.handle({"command": "scene.save", "name": "Movie Night", "target_id": "office"})
    assert duplicate["error"]["code"] == "conflict"


def test_scene_save_without_target_defaults_to_declared_targets(engine):
    result = engine.handle({"command": "scene.save", "name": "Everything Draft"})
    assert result["ok"] is True
    assert result["data"]["scene"]["target_ids"] == ["office", "studio"]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def test_fixture_disable_skips_it_in_apply(store, executor):
    engine = make_engine(store, executor=executor)
    disabled = engine.handle({"command": "fixture.disable", "fixture_id": "lamp"})
    assert disabled["ok"] is True
    assert disabled["data"]["fixture"]["enabled"] is False

    result = engine.handle({"command": "scene.apply", "scene_id": "aurora"})
    assert "lamp" in result["data"]["skipped_fixture_ids"]
    assert all(call["fixture_id"] != "lamp" for call in executor.calls)

    enabled = engine.handle({"command": "fixture.enable", "fixture_id": "lamp"})
    assert enabled["data"]["fixture"]["enabled"] is True


def test_fixture_rebind_from_observation_flips_missing_to_ready(store, executor):
    # g_strip starts with a stale binding (resource not observed any more) and
    # an explicit health=missing override; discovery finds a candidate.
    store.fixtures.set_health("g_strip", "missing")
    hue_payload = {
        "data": [
            {
                "id": "new-rid",
                "metadata": {"name": "Hue G Strip"},
                "color": {"xy": {}},
                "dimming": {},
                "gradient": {"points_capable": 5},
                "dynamics": {"status_values": ["dynamic_palette"]},
            }
        ]
    }
    rooms = {"data": [{"metadata": {"name": "Office"}, "children": [{"rtype": "device", "rid": "owner-1"}]}]}
    fetchers = DiscoveryFetchers(fetch_hue=lambda: hue_payload, fetch_hue_rooms=lambda: rooms)
    engine = make_engine(store, fetchers=fetchers, executor=executor)
    run = engine.handle({"command": "discovery.run"})
    assert run["ok"] is True
    assert engine.status()["fixtures"]["missing"] == 1

    rebind = engine.handle(
        {"command": "fixture.rebind", "fixture_id": "g_strip", "observation_id": "hue_v2:new-rid"}
    )
    assert rebind["ok"] is True
    fixture = rebind["data"]["fixture"]
    assert fixture["binding"]["resource_id"] == "new-rid"
    assert fixture["binding"]["bridge_id"] == "bridge1"  # inherited from the previous hue binding
    assert fixture.get("health") is None  # stale override cleared so derive_health flips
    assert rebind["data"]["candidate_reasons"]  # discovery reasons reported

    after = engine.status()
    assert after["fixtures"]["missing"] == 0
    assert after["fixtures"]["ready"] == 4

    event = next(event for event in engine.recent_events(10) if "rebound" in event.summary)
    assert event.data["reasons"]


def test_fixture_rebind_requires_latest_report_and_known_observation(store, executor):
    engine = make_engine(store, executor=executor)
    no_report = engine.handle(
        {"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp"}
    )
    assert no_report["error"]["code"] == "not_found"

    engine.handle({"command": "discovery.run", "providers": ["ha_light"]})
    unknown_obs = engine.handle(
        {"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.ghost"}
    )
    assert unknown_obs["error"]["code"] == "not_found"
    unknown_fixture = engine.handle(
        {"command": "fixture.rebind", "fixture_id": "ghost", "observation_id": "ha_light:light.lamp"}
    )
    assert unknown_fixture["error"]["code"] == "not_found"


def test_fixture_retry_reruns_discovery_for_its_provider(store, executor):
    ha_states = {"light.lamp": {"attributes": {"friendly_name": "Lamp", "brightness": 128}}}
    fetchers = DiscoveryFetchers(fetch_ha_states=lambda: ha_states)
    engine = make_engine(store, fetchers=fetchers, executor=executor)
    result = engine.handle({"command": "fixture.retry", "fixture_id": "lamp"})
    assert result["ok"] is True
    data = result["data"]
    assert data["provider"] == "ha_light"
    assert data["status"] == "bound_ready"
    assert data["candidates"] == []
    # provider filter scoped the run: only ha_light fetched, nothing skipped
    assert data["provider_skipped"] == []
    report = engine.latest_discovery()
    assert report.providers == ["ha_light"]
    assert [obs.provider for obs in report.observations] == ["ha_light"]


def test_fixture_retry_without_binding_conflicts(store, executor):
    store.fixtures.add_fixture({"id": "spare_pad", "name": "Spare Pad"})  # no binding yet
    engine = make_engine(store, executor=executor)
    result = engine.handle({"command": "fixture.retry", "fixture_id": "spare_pad"})
    assert result["error"]["code"] == "conflict"


def test_fixture_identify_sends_hue_native_identify(store, executor):
    engine = make_engine(store, executor=executor)
    revision_before = engine.status()["engine"]["revision"]
    result = engine.handle({"command": "fixture.identify", "fixture_id": "g_strip"})
    assert result["ok"] is True
    assert result["data"]["receipt"]["ok"] is True
    assert executor.calls == [
        {
            "provider": "hue_v2",
            "op": "hue.identify",
            "resource_ref": "g-strip-rid",
            "payload": {"identify": {"action": "identify"}},
            "fixture_id": "g_strip",
        }
    ]
    # a transient provider nudge, not a state mutation
    assert engine.status()["engine"]["revision"] == revision_before


def test_fixture_identify_sends_ha_flash(store, executor):
    engine = make_engine(store, executor=executor)
    result = engine.handle({"command": "fixture.identify", "fixture_id": "lamp"})
    assert result["ok"] is True
    assert result["data"]["receipt"]["ok"] is True
    assert executor.calls == [
        {
            "provider": "ha_light",
            "op": "ha.call_light_identify",
            "resource_ref": "light.lamp",
            "payload": {"entity_id": "light.lamp", "flash": "short"},
            "fixture_id": "lamp",
        }
    ]


def test_fixture_identify_unsupported_for_wled_is_an_honest_failed_receipt(store, executor):
    engine = make_engine(store, executor=executor)
    result = engine.handle({"command": "fixture.identify", "fixture_id": "wled_seg"})
    assert result["ok"] is True  # the command dispatched; the receipt reports the real outcome
    receipt = result["data"]["receipt"]
    assert receipt["ok"] is False
    assert "not supported" in receipt["detail"]
    assert executor.calls == []  # never fabricated against a provider that can't do it


def test_fixture_identify_without_binding_conflicts(store, executor):
    store.fixtures.add_fixture({"id": "spare_pad", "name": "Spare Pad"})  # no binding yet
    engine = make_engine(store, executor=executor)
    result = engine.handle({"command": "fixture.identify", "fixture_id": "spare_pad"})
    assert result["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# discovery.run
# ---------------------------------------------------------------------------


def _full_fetchers():
    hue_payload = {
        "data": [
            {
                "id": "hue-1",
                "metadata": {"name": "Hue G Strip"},
                "color": {"xy": {}},
                "dimming": {},
                "gradient": {"points_capable": 5},
                "dynamics": {"status_values": ["dynamic_palette"]},
            }
        ]
    }
    return DiscoveryFetchers(
        fetch_hue=lambda: hue_payload,
        fetch_wled_info=lambda: {"mac": "aa:bb:cc:dd:ee:ff"},
        fetch_wled_state=lambda: {"bri": 128, "fx": 0, "seg": [{"id": 0, "start": 0, "stop": 30, "n": "WLED Segment"}]},
        fetch_ha_states=lambda: {"light.lamp": {"attributes": {"friendly_name": "Lamp", "brightness": 128}}},
        wled_endpoint_hint="wled-host",
    )


def test_discovery_run_with_fake_fetchers(store, executor):
    engine = make_engine(store, fetchers=_full_fetchers(), executor=executor)
    result = engine.handle({"command": "discovery.run"})
    assert result["ok"] is True
    data = result["data"]
    assert data["summary"]["observations_total"] == 3  # hue light + wled segment + ha light
    assert data["skipped_providers"] == []

    status = engine.status()
    assert status["last_discovery"]["run_id"] == data["run_id"]
    assert status["last_discovery"]["summary"] == data["summary"]
    # wled segment (device id + fx + bri observed) and lamp (brightness observed)
    # bind ready; g_strip's stale resource id yields a replacement candidate.
    assert status["last_discovery"]["summary"]["fixtures_bound_ready"] == 2
    assert status["last_discovery"]["summary"]["candidate_replacements"] == 1


def test_discovery_run_skips_unconfigured_providers_with_events(store, executor):
    fetchers = DiscoveryFetchers(fetch_ha_states=lambda: {"light.lamp": {"attributes": {"friendly_name": "Lamp"}}})
    engine = make_engine(store, fetchers=fetchers, executor=executor)
    result = engine.handle({"command": "discovery.run"})
    assert result["ok"] is True
    assert result["data"]["skipped_providers"] == ["hue_v2", "wled"]
    skipped_events = [e for e in engine.recent_events(20) if "skipped" in e.summary.lower()]
    assert len(skipped_events) == 2
    assert all(e.category is EventCategory.DISCOVERY for e in skipped_events)


def test_discovery_run_fetch_exception_becomes_skip_not_error(store, executor):
    def boom():
        raise RuntimeError("bridge unreachable")

    fetchers = DiscoveryFetchers(fetch_hue=boom)
    engine = make_engine(store, fetchers=fetchers, executor=executor)
    result = engine.handle({"command": "discovery.run"})
    assert result["ok"] is True
    # the broken fetcher AND the unconfigured providers all skip; run continues
    assert result["data"]["skipped_providers"] == ["hue_v2", "wled", "ha_light"]
    assert result["data"]["provider_errors"][0]["provider"] == "hue_v2"
    assert "bridge unreachable" in result["data"]["provider_errors"][0]["error"]


def test_discovery_provider_filter_reports_unknown_names(engine):
    result = engine.handle({"command": "discovery.run", "providers": ["not_a_provider"]})
    assert result["ok"] is True
    assert "not_a_provider (unknown provider name)" in result["data"]["skipped_providers"]


# ---------------------------------------------------------------------------
# playback
# ---------------------------------------------------------------------------


def test_playback_start_returns_session_and_collection_status(store, executor):
    engine = make_engine(store, executor=executor)

    started = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert started["ok"] is True
    session_id = started["data"]["session_id"]
    assert session_id.startswith("sess-")
    playback = started["data"]["playback"]
    assert playback["scene_id"] == "aurora"
    assert playback["state"] == "active"
    assert playback["started_at"].endswith("Z")

    status_playback = engine.status()["playback"]
    assert status_playback["counts"]["active"] == 1
    assert status_playback["owned_fixture_count"] == len(playback["fixture_ids"])
    sessions = status_playback["sessions"]
    assert len(sessions) == 1 and sessions[0]["session_id"] == session_id


def test_playback_pause_resume_stop_are_session_addressed(store, executor):
    engine = make_engine(store, executor=executor)
    started = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    session_id = started["data"]["session_id"]

    # unknown session -> conflict
    assert engine.handle({"command": "playback.pause", "session_id": "sess-nope"})["error"]["code"] == "conflict"
    assert engine.handle({"command": "playback.resume", "session_id": "sess-nope"})["error"]["code"] == "conflict"
    assert engine.handle({"command": "playback.stop", "session_id": "sess-nope"})["error"]["code"] == "conflict"

    paused = engine.handle({"command": "playback.pause", "session_id": session_id})
    assert paused["ok"] is True
    assert paused["data"]["playback"]["state"] == "paused"
    # idempotent pause
    paused_again = engine.handle({"command": "playback.pause", "session_id": session_id})
    assert paused_again["ok"] is True
    assert paused_again["data"]["playback"]["state"] == "paused"

    resumed = engine.handle({"command": "playback.resume", "session_id": session_id})
    assert resumed["data"]["playback"]["state"] == "active"

    stopped = engine.handle({"command": "playback.stop", "session_id": session_id})
    assert stopped["ok"] is True

    # stopped session is terminal for pause/resume/stop
    for command in ("playback.pause", "playback.resume", "playback.stop"):
        result = engine.handle({"command": command, "session_id": session_id})
        assert result["error"]["code"] == "conflict", command


def _add_office_dyn(store):
    store.scenes.add_scene(
        {"schema_version": 2, "id": "office_dyn", "name": "Office Dyn",
         "target_ids": ["bathroom"], "motion": {"mode": "palette_cycle", "speed": 0.3},
         "fixture_states": {"bathroom_main": {"on": True, "brightness": 40.0}}}
    )


def test_two_disjoint_sessions_coexist(store, executor):
    engine = make_engine(store, executor=executor)
    _add_office_dyn(store)
    first = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    second = engine.handle({"command": "playback.start", "scene_id": "office_dyn"})
    sid1, sid2 = first["data"]["session_id"], second["data"]["session_id"]
    assert sid1 != sid2

    status = engine.status()["playback"]
    assert status["counts"]["active"] == 2
    assert len(status["sessions"]) == 2
    owned1 = next(s for s in status["sessions"] if s["session_id"] == sid1)["fixture_ids"]
    owned2 = next(s for s in status["sessions"] if s["session_id"] == sid2)["fixture_ids"]
    assert not (set(owned1) & set(owned2))  # truly disjoint ownership

    # session-addressed pause of one leaves the other active
    engine.handle({"command": "playback.pause", "session_id": sid1})
    status = engine.status()["playback"]
    assert status["counts"]["paused"] == 1 and status["counts"]["active"] == 1


def test_overlap_preemption_stops_only_the_overlapping_session(store, executor):
    engine = make_engine(store, executor=executor)
    _add_office_dyn(store)
    first = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid_first = first["data"]["session_id"]
    disjoint = engine.handle({"command": "playback.start", "scene_id": "office_dyn"})
    sid_disjoint = disjoint["data"]["session_id"]

    # starting aurora again preempts only the first aurora session
    second = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid_new = second["data"]["session_id"]
    assert second["data"]["preempted_session_ids"] == [sid_first]

    status = engine.status()["playback"]
    assert status["counts"]["active"] == 2  # new aurora + disjoint office session
    states = {s["session_id"]: s["state"] for s in status["sessions"]}
    assert states[sid_first] == "stopped"
    assert states[sid_disjoint] == "active"
    assert states[sid_new] == "active"


def test_start_supersedes_orphaned_session_records(store, executor):
    engine = make_engine(store, executor=executor)
    first = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid_old = first["data"]["session_id"]

    # simulate a restart: active sessions become orphaned on reload
    for session in engine._playback_state.sessions.values():
        if session.state == "active":
            session.state = "orphaned"

    status = engine.status()["playback"]
    orphan = next(s for s in status["sessions"] if s["session_id"] == sid_old)
    assert orphan["state"] == "orphaned"

    # a new start retaking those fixtures supersedes the stale orphan record
    second = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid_new = second["data"]["session_id"]
    status = engine.status()["playback"]
    orphan = next(s for s in status["sessions"] if s["session_id"] == sid_old)
    assert orphan["state"] == "stopped"
    assert orphan["stop_reason"] == "superseded"
    assert orphan["preempted_by"] == sid_new
    assert status["counts"]["orphaned"] == 0


def test_stopped_session_retention_trims_oldest(store, executor):
    engine = make_engine(store, executor=executor)
    for _ in range(25):
        result = engine.handle({"command": "playback.start", "scene_id": "aurora"})
        sid = result["data"]["session_id"]
        engine.handle({"command": "playback.stop", "session_id": sid})
    status = engine.status()["playback"]
    assert status["counts"]["stopped"] <= 20  # bounded retention
    assert status["counts"]["active"] == 0


def test_corrupt_playback_state_file_is_handled_safely(store, executor):
    engine = make_engine(store, executor=executor)
    engine.handle({"command": "playback.start", "scene_id": "aurora"})
    engine._playback_path.write_text("{ corrupt", encoding="utf-8")

    fresh = make_engine(store, executor=executor)
    status = fresh.status()["playback"]
    assert status["sessions"] == []  # corrupt file -> fresh state, no crash


def test_static_scene_apply_cancels_only_overlapping_sessions(store, executor):
    engine = make_engine(store, executor=executor)
    _add_office_dyn(store)
    engine.handle({"command": "playback.start", "scene_id": "aurora"})      # studio
    office_session = engine.handle({"command": "playback.start", "scene_id": "office_dyn"})
    sid_office = office_session["data"]["session_id"]

    # static twilight targets studio -> cancels the studio session only
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is True
    status = engine.status()["playback"]
    states = {s["session_id"]: s["state"] for s in status["sessions"]}
    assert states[sid_office] == "active"  # disjoint session untouched
    assert status["counts"]["active"] == 1
    stopped = [s for s in status["sessions"] if s["state"] == "stopped"]
    assert len(stopped) == 1 and stopped[0]["stop_reason"] == "superseded by scene.apply"


def test_playback_start_on_static_scene_conflicts(engine):
    result = engine.handle({"command": "playback.start", "scene_id": "twilight"})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert "no motion" in result["error"]["message"]
    assert engine.status()["playback"]["counts"]["active"] == 0


def test_playback_start_executes_native_and_approximate_and_replaces(store, executor):
    engine = make_engine(store, executor=executor)
    first = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert first["data"]["native_fixtures"] == 1  # wled_seg only
    assert first["data"]["approximate_fixtures"] == 3  # middle_bar, g_strip, lamp
    # hue.get_groups_overview: the zone-model topology read for g_strip's
    # managed realization (fails honestly against the recording executor)
    assert set(executor.ops()) == {"hue.get_groups_overview", "hue.put_light",
                                   "wled.post_state", "ha.call_light"}

    calls_before = len(executor.calls)
    second = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert second["ok"] is True
    assert len(executor.calls) > calls_before  # replan + reexecute


def test_static_scene_apply_cancels_playback(store, executor):
    engine = make_engine(store, executor=executor)
    engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert engine.status()["playback"]["counts"]["active"] == 1
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight", "target_id": "studio"})
    assert result["ok"] is True
    assert result["data"]["preempted_session_ids"]
    status = engine.status()["playback"]
    assert status["counts"]["active"] == 0
    assert status["counts"]["stopped"] == 1
    stopped_events = [
        event for event in engine.recent_events(10)
        if "stopped" in event.summary or "superseded" in (event.detail or "")
    ]
    assert stopped_events


# ---------------------------------------------------------------------------
# status / revision / events
# ---------------------------------------------------------------------------


def test_status_revision_increments_on_mutations_only(store, executor):
    engine = make_engine(store, executor=executor)
    assert engine.status()["engine"]["revision"] == 0
    engine.handle({"command": "scene.preview", "scene_id": "twilight"})
    assert engine.status()["engine"]["revision"] == 0  # read-only
    engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    revision_after_apply = engine.status()["engine"]["revision"]
    assert revision_after_apply == 1
    engine.handle({"command": "scene.rename", "scene_id": "twilight", "name": "Dusk"})
    assert engine.status()["engine"]["revision"] == revision_after_apply + 1
    engine.handle({"command": "diagnostics.export"})
    assert engine.status()["engine"]["revision"] == revision_after_apply + 1  # read-only


def test_status_provider_health_and_fixture_counts(store, executor):
    store.fixtures.set_health("g_strip", "missing")
    engine = make_engine(store, executor=executor)
    status = engine.status()
    assert status["fixtures"]["total"] == 5
    assert status["fixtures"]["disabled"] == 1
    assert status["fixtures"]["missing"] == 1
    assert status["fixtures"]["ready"] == 3
    assert status["providers"]["hue_v2"] == {"total": 3, "ready": 1, "missing": 1, "degraded": 0, "other": 1}
    assert status["providers"]["wled"]["total"] == 1
    assert status["providers"]["ha_light"]["ready"] == 1


def test_recent_events_bounded_and_newest_first(store, executor):
    engine = make_engine(store, executor=executor, capacity=3)
    engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    engine.handle({"command": "scene.preview", "scene_id": "twilight"})
    engine.handle({"command": "scene.rename", "scene_id": "twilight", "name": "Dusk"})
    engine.handle({"command": "scene.rename", "scene_id": "twilight", "name": "Night"})
    events = engine.recent_events(100)
    assert len(events) == 3  # capacity bound
    timestamps = [event.timestamp for event in events]
    assert timestamps == sorted(timestamps, reverse=True)  # newest first
    assert "Night" in events[0].summary


def test_emit_sanitizes_event_data(store):
    engine = make_engine(store)
    engine.emit(
        EventLevel.WARNING,
        EventCategory.SYSTEM,
        "provider hiccup",
        data={"authorization": f"Bearer {FAKE_JWT}", "note": "safe"},
    )
    event = engine.recent_events(1)[0]
    assert FAKE_JWT not in json.dumps(event.to_dict())
    assert event.data["authorization"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# diagnostics.export
# ---------------------------------------------------------------------------


def test_diagnostics_export_contains_no_secret_shaped_strings(store, executor):
    # Plant a secret-shaped string in registry metadata; it must not survive export.
    store.fixtures.add_fixture(
        {
            "id": "note_holder",
            "name": "Note Holder",
            "groups": [],
            "metadata": {"integration_note": f"seen with {FAKE_JWT}"},
        }
    )
    engine = make_engine(store, executor=executor)
    engine.handle({"command": "scene.apply", "scene_id": "twilight"})

    result = engine.handle({"command": "diagnostics.export", "recent_events": 10})
    assert result["ok"] is True
    blob = json.dumps(result["data"])
    assert FAKE_JWT not in blob  # redacted everywhere (registry + events)
    assert "[REDACTED-JWT]" in blob
    assert result["data"]["status"]["engine"]["ok"] is True
    assert result["data"]["registry"]["schema_version"] == 2
    assert result["data"]["discovery"] is None
    assert "membership_drift" in result["data"]
    assert "rows" in result["data"]["membership_drift"]


# ---------------------------------------------------------------------------
# never-raise guarantee / envelope handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "nonsense",
        42,
        {},
        {"command": "nope"},
        {"command": "scene.apply"},
        {"command": "scene.apply", "scene_id": 123},
        {"command": "scene.apply", "scene_id": "twilight", "bogus_param": True},
        {"command": "scene.rename", "scene_id": "twilight"},
        {"command": "fixture.enable"},
        {"command": "playback.start", "scene_id": "twilight", "extra": 1},
    ],
)
def test_handle_never_raises_and_returns_proper_error_codes(engine, payload):
    result = engine.handle(payload)
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["error"]["code"] in {
        "validation_error",
        "unknown_command",
        "not_found",
        "conflict",
        "provider_unavailable",
        "internal_error",
    }


def test_error_code_mapping_for_store_and_validation_errors(engine):
    assert engine.handle({"command": "scene.rename", "scene_id": "ghost", "name": "X"})["error"]["code"] == "not_found"
    engine.handle({"command": "scene.save", "name": "Twilight", "target_id": "studio"})  # twilight exists
    duplicate = engine.handle({"command": "scene.save", "name": "Twilight", "target_id": "studio"})
    assert duplicate["error"]["code"] == "conflict"
    empty_name = engine.handle({"command": "scene.rename", "scene_id": "twilight", "name": ""})
    assert empty_name["error"]["code"] == "validation_error"


def test_request_id_round_trips(engine, executor):
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight", "request_id": "req-42"})
    assert result["request_id"] == "req-42"
    assert executor.calls


def test_handle_result_returns_command_result_object(engine):
    result = engine.handle_result({"command": "scene.preview", "scene_id": "twilight"})
    assert isinstance(result, CommandResult)
    assert result.ok is True


# ---------------------------------------------------------------------------
# live fixture color-state sampling (plan: live fixture color-state pass)
# ---------------------------------------------------------------------------


def test_sample_live_state_never_bumps_revision_or_emits_events(store):
    fetchers = DiscoveryFetchers(fetch_hue=lambda: {"data": [{"id": "g-strip-rid", "on": {"on": True}, "color": {"xy": {"x": 0.4, "y": 0.4}}}]})
    engine = make_engine(store, fetchers=fetchers)
    revision_before = engine.status()["engine"]["revision"]
    events_before = len(engine.recent_events(500))

    snapshot = engine.sample_live_state()

    assert engine.status()["engine"]["revision"] == revision_before
    assert len(engine.recent_events(500)) == events_before
    assert snapshot["fixtures"]["g_strip"]["color_mode"] == "rgb"


def test_sample_live_state_does_not_mutate_registry(store):
    engine = make_engine(store, fetchers=DiscoveryFetchers(fetch_hue=lambda: {"data": []}))
    before = engine.fixtures_catalog()
    engine.sample_live_state()
    after = engine.fixtures_catalog()
    assert before == after


def test_sample_live_state_coalesces_concurrent_calls(store):
    """A slow provider read must not queue a second concurrent sample
    (plan §3): a call arriving while one is in flight returns immediately
    with the last committed snapshot instead of waiting on/duplicating it."""
    call_count = 0
    started = threading.Event()
    release = threading.Event()

    def slow_fetch_hue():
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(timeout=5)
        return {"data": [{"id": "g-strip-rid", "on": {"on": True}, "color": {"xy": {"x": 0.4, "y": 0.4}}}]}

    engine = make_engine(store, fetchers=DiscoveryFetchers(fetch_hue=slow_fetch_hue))

    first_result = {}

    def run_first():
        first_result["snapshot"] = engine.sample_live_state()

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(timeout=5)

    # A second call while the first is still blocked on the provider must
    # return immediately (the last cached snapshot), not wait or refetch.
    second = engine.sample_live_state()
    assert call_count == 1  # no duplicate provider request was fired
    assert second["fixtures"] == {}  # nothing committed yet: honest empty snapshot, not a guess

    release.set()
    thread.join(timeout=5)
    assert call_count == 1
    assert first_result["snapshot"]["fixtures"]["g_strip"]["color_mode"] == "rgb"
