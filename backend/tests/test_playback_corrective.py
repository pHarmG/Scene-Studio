"""R5A corrective-pass regression tests.

Covers the restart-recovery bug (persist helper dereferenced an unassigned
field), the one-live-owner-per-fixture invariant enforced inside
PlaybackState, persistence-key/session identity divergence, resume-on-active
conflict semantics, and retention consistency across all stop transitions.
"""

import json

import pytest

from scene_studio.domain.playback import (
    PlaybackSession,
    PlaybackSessionState,
    PlaybackState,
    new_session_id,
)
from scene_studio.service.engine import SceneStudioEngine
from scene_studio.service.ports import RecordingExecutor, SteppingClock
from scene_studio.stores import SceneStudioStore
from scene_studio.domain.serde import ValidationError
from test_service_engine import make_store


def _make_engine(tmp_path):
    """Engine over a store seeded with the shared sample registry + scenes."""
    return SceneStudioEngine(
        store=make_store(tmp_path / "store"),
        executor=RecordingExecutor(),
        clock=SteppingClock(),
    )


def _make_session(session_id, fixture_ids, state, scene_id="aurora"):
    return PlaybackSession(
        session_id=session_id,
        scene_id=scene_id,
        scene_name=scene_id,
        target_ids=["studio"],
        fixture_ids=list(fixture_ids),
        state=state,
        started_at="2026-09-12T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# real restart recovery (the R5A corrective-pass bug)
# ---------------------------------------------------------------------------


def test_restart_recovery_two_disjoint_sessions_orphan_and_persist(tmp_path):
    engine_a = _make_engine(tmp_path)

    # two disjoint sessions (aurora=studio bank; office_dyn=bathroom)
    store = engine_a._store
    store.scenes.add_scene(
        {"schema_version": 2, "id": "office_dyn", "name": "Office Dyn",
         "target_ids": ["bathroom"], "motion": {"mode": "palette_cycle", "speed": 0.3},
         "fixture_states": {"bathroom_main": {"on": True}}}
    )
    first = engine_a.handle({"command": "playback.start", "scene_id": "aurora"})
    second = engine_a.handle({"command": "playback.start", "scene_id": "office_dyn"})
    sid1, sid2 = first["data"]["session_id"], second["data"]["session_id"]
    assert sid1 != sid2

    # the persisted file contains both sessions in the active state
    on_disk = json.loads(engine_a._playback_path.read_text(encoding="utf-8"))
    assert on_disk["sessions"][sid1]["state"] == "active"
    assert on_disk["sessions"][sid2]["state"] == "active"

    # a completely new engine over the same store must initialize cleanly
    engine_b = SceneStudioEngine(
        store=SceneStudioStore(tmp_path / "store"),
        executor=RecordingExecutor(),
        clock=SteppingClock(),
    )

    # both sessions orphaned; ownership released
    status = engine_b.status()["playback"]
    assert status["counts"]["orphaned"] == 2
    assert status["counts"]["active"] == 0
    assert status["owned_fixture_count"] == 0
    states = {s["session_id"]: s["state"] for s in status["sessions"]}
    assert states[sid1] == "orphaned" and states[sid2] == "orphaned"

    # the persisted file itself now carries the orphaned states
    on_disk = json.loads(engine_b._playback_path.read_text(encoding="utf-8"))
    assert on_disk["sessions"][sid1]["state"] == "orphaned"
    assert on_disk["sessions"][sid2]["state"] == "orphaned"
    assert on_disk["ownership"] == {}

    # warning events are present
    warnings = [
        e for e in engine_b.recent_events(50)
        if e.level.value == "warning" and "interrupted by restart" in e.summary
    ]
    assert len(warnings) == 2


# ---------------------------------------------------------------------------
# ownership invariant + key identity enforced inside PlaybackState
# ---------------------------------------------------------------------------


def test_collection_rejects_two_live_sessions_claiming_same_fixture():
    state = PlaybackState()
    state.add(_make_session("sess-a", ["g_strip"], "active"))
    with pytest.raises(ValidationError, match="claimed by both session"):
        state.add(_make_session("sess-b", ["g_strip"], "active"))
    # sess-b must not remain in the collection after the rejected add
    assert state.get("sess-b") is None


def test_collection_rejects_duplicate_claims_on_load():
    a = _make_session("sess-a", ["g_strip"], "active")
    b = _make_session("sess-b", ["g_strip"], "active")
    doc = {
        "schema_version": 1,
        "sessions": {"sess-a": a.to_dict(), "sess-b": b.to_dict()},
        "ownership": {},
    }
    with pytest.raises(ValidationError, match="claimed by both"):
        PlaybackState.from_dict(doc)


def test_collection_rejects_persistence_key_identity_divergence():
    a = _make_session("sess-a", ["g_strip"], "active")
    doc = {
        "schema_version": 1,
        "sessions": {"wrong-key": a.to_dict()},
        "ownership": {},
    }
    with pytest.raises(ValidationError, match="does not match session identity"):
        PlaybackState.from_dict(doc)


def test_non_owning_states_may_share_fixtures_across_sessions():
    """Stopped/orphaned sessions hold no ownership, so two stopped sessions
    may reference the same fixture, and a stopped + an active session may
    coexist on the same fixture id (the active one owns it)."""
    state = PlaybackState()
    state.add(_make_session("sess-stopped", ["g_strip"], "stopped"))
    state.add(_make_session("sess-active", ["g_strip"], "active"))
    assert state.owning_session_id("g_strip") == "sess-active"


# ---------------------------------------------------------------------------
# resume-on-active conflict (pause stays idempotent)
# ---------------------------------------------------------------------------


def test_resume_on_active_conflicts_pause_on_paused_stays_idempotent(tmp_path):
    engine = _make_engine(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid = started["data"]["session_id"]

    # resume on active -> conflict
    resumed = engine.handle({"command": "playback.resume", "session_id": sid})
    assert resumed["ok"] is False
    assert resumed["error"]["code"] == "conflict"
    assert "already active" in resumed["error"]["message"]

    # pause on paused -> idempotent success
    engine.handle({"command": "playback.pause", "session_id": sid})
    paused_again = engine.handle({"command": "playback.pause", "session_id": sid})
    assert paused_again["ok"] is True
    assert paused_again["data"]["playback"]["state"] == "paused"


# ---------------------------------------------------------------------------
# retention consistency across every stop transition
# ---------------------------------------------------------------------------


def test_retention_applies_to_archive_and_supersession_transitions(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path)

    # archive transition: start+stop 20 sessions to fill retention, then
    # archive a scene with a live session and confirm trim runs (count capped)
    engine.handle({"command": "playback.start", "scene_id": "aurora"})
    for _ in range(20):
        started = engine.handle({"command": "playback.start", "scene_id": "aurora"})
        engine.handle({"command": "playback.stop", "session_id": started["data"]["session_id"]})
    assert engine.status()["playback"]["counts"]["stopped"] == 20

    store = engine._store
    store.scenes.add_scene(
        {"schema_version": 2, "id": "tmp_scene", "name": "Tmp",
         "target_ids": ["studio"], "motion": {"mode": "palette_cycle", "speed": 0.5},
         "fixture_states": {"lamp": {"on": True}}}
    )
    live = engine.handle({"command": "playback.start", "scene_id": "tmp_scene"})
    engine.handle({"command": "scene.archive", "scene_id": "tmp_scene"})
    stopped_count = engine.status()["playback"]["counts"]["stopped"]
    assert stopped_count <= 20

    # orphan-supersession transition: orphan a session, then supersede it via
    # a start retaking the same fixtures — count still bounded
    for _ in range(20):
        started = engine.handle({"command": "playback.start", "scene_id": "aurora"})
        engine.handle({"command": "playback.stop", "session_id": started["data"]["session_id"]})
    live = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    sid = live["data"]["session_id"]
    for session in engine._playback_state.sessions.values():
        if session.session_id == sid:
            session.state = "orphaned"  # simulate restart orphaning
    engine.handle({"command": "playback.start", "scene_id": "aurora"})  # supersedes
    assert engine.status()["playback"]["counts"]["stopped"] <= 20
