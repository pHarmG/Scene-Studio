"""Scene authoring contract tests — Builder Pass 2 (plan §4/§8).

Covers the canonical authoring commands (``scene.preview_draft``,
``scene.play_draft``, ``scene.create``, ``scene.update``), the
whole-document store replacement (``SceneStore.replace_scene``),
runtime-policy gating, and the compatibility guarantees the Workbench
Builder relies on:

- draft preview validates/renderers through the canonical Scene model and
  the exact stored-scene render-plan path, persisting nothing and touching
  no provider;
- draft play applies/starts the unsaved document on fixtures without
  writing the catalog, so Builder Preview is a real preview;
- create derives/validates the stable id server-side, rejects unknown
  targets, and conflicts against active AND archived ids;
- update atomically replaces the whole document, never changes identity,
  requires an active scene, and preserves server/history metadata
  (``migrated_from_v1``) the Builder does not own;
- legacy ``scene.save`` / upsert behavior is untouched.
"""

import pytest

from scene_studio.domain.commands import COMMAND_CATALOG
from scene_studio.domain.events import EventCategory, EventLevel
from scene_studio.renderers import build_render_plan
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.policy import RuntimePolicy
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore

from test_service_engine import make_store


def make_engine(store, executor=None, policy=None):
    return SceneStudioEngine(
        store,
        executor or RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(),
        policy=policy,
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


VALID_DRAFT = {
    "schema_version": 2,
    "id": "",
    "name": "Evening Glow",
    "target_ids": ["office"],
    "palette": ["#ff7a45", "#6f4bff"],
    "brightness": 65.0,
    "motion": {"mode": "palette_cycle", "speed": 0.45, "strategy": "auto"},
    "default_state": {"on": True, "brightness": 60.0},
}


# ---------------------------------------------------------------------------
# scene.preview_draft
# ---------------------------------------------------------------------------


def test_preview_draft_derives_id_and_returns_render_plan(engine):
    result = engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    data = result["data"]
    assert data["dry_run"] is True
    # The candidate stable id is derived server-side from the name.
    assert data["scene"]["id"] == "evening_glow"
    plan = data["render_plan"]
    assert plan["scene_id"] == "evening_glow"
    planned = [fp["fixture_id"] for fp in plan["fixture_plans"]]
    assert set(planned) == {"g_strip", "middle_bar", "wled_seg", "lamp", "double_strip"} - {"double_strip"}
    assert "double_strip" in plan["skipped_fixture_ids"]


def test_preview_draft_matches_canonical_renderer_path(engine):
    result = engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    plan = result["data"]["render_plan"]
    from scene_studio.domain.scenes import Scene

    scene = Scene.from_dict({**VALID_DRAFT, "id": "evening_glow"}, "scene")
    expected = build_render_plan(scene, engine._store.fixtures.registry(), target_ids=None)
    assert plan == expected.to_dict()


def test_preview_draft_does_not_persist(engine):
    before = {scene.id for scene in engine._store.scenes.list_scenes()}
    result = engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    after = {scene.id for scene in engine._store.scenes.list_scenes()}
    assert before == after
    assert "evening_glow" not in after
    # Reload from disk to prove nothing was written either.
    engine._store.scenes.reload()
    assert "evening_glow" not in {scene.id for scene in engine._store.scenes.list_scenes()}


def test_preview_draft_never_contacts_providers(engine, executor):
    engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    assert executor.calls == []


def test_preview_draft_does_not_bump_revision_or_emit(engine):
    revision = engine.status()["engine"]["revision"]
    events_before = len(engine.recent_events(500))
    engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    assert engine.status()["engine"]["revision"] == revision
    assert len(engine.recent_events(500)) == events_before


def test_preview_draft_reports_unknown_target_as_plan_note(engine):
    draft = {**VALID_DRAFT, "target_ids": ["office", "ghost_room"]}
    result = engine.handle({"command": "scene.preview_draft", "scene": draft})
    assert result["ok"] is True
    assert any("ghost_room" in note for note in result["data"]["render_plan"]["notes"])


def test_preview_draft_validation_failure_is_precise(engine):
    result = engine.handle({"command": "scene.preview_draft", "scene": {"schema_version": 2, "name": "x", "target_ids": []}})
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert result["error"]["details"]["path"] == "scene.target_ids"
    bad_palette = {**VALID_DRAFT, "palette": ["nope"]}
    result = engine.handle({"command": "scene.preview_draft", "scene": bad_palette})
    assert result["error"]["details"]["path"] == "scene.palette.0"


# ---------------------------------------------------------------------------
# scene.play_draft
# ---------------------------------------------------------------------------


STATIC_DRAFT = {
    **VALID_DRAFT,
    "name": "Static Glow",
    "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
}


def test_play_draft_static_applies_without_persist(engine, executor):
    before = {scene.id for scene in engine._store.scenes.list_scenes()}
    result = engine.handle({"command": "scene.play_draft", "scene": dict(STATIC_DRAFT)})
    assert result["ok"] is True
    data = result["data"]
    assert data["played"] is True
    assert data["dry_run"] is False
    assert data["kind"] == "apply"
    assert data["scene"]["id"] == "static_glow"
    assert data["render_plan"]["scene_id"] == "static_glow"
    assert executor.calls, "static play_draft must contact providers"
    after = {scene.id for scene in engine._store.scenes.list_scenes()}
    assert before == after
    assert "static_glow" not in after
    engine._store.scenes.reload()
    assert "static_glow" not in {scene.id for scene in engine._store.scenes.list_scenes()}


def test_play_draft_dynamic_starts_playback_without_persist(engine, executor):
    before = {scene.id for scene in engine._store.scenes.list_scenes()}
    result = engine.handle({"command": "scene.play_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    data = result["data"]
    assert data["played"] is True
    assert data["dry_run"] is False
    assert data["kind"] == "playback"
    assert data["session_id"]
    assert data["scene"]["id"] == "evening_glow"
    assert executor.calls, "dynamic play_draft must contact providers"
    after = {scene.id for scene in engine._store.scenes.list_scenes()}
    assert before == after
    assert "evening_glow" not in after
    session_id = data["session_id"]
    paused = engine.handle({"command": "playback.pause", "session_id": session_id})
    assert paused["ok"] is True
    stopped = engine.handle({"command": "playback.stop", "session_id": session_id})
    assert stopped["ok"] is True
    assert session_id not in engine._ephemeral_scenes


def test_play_draft_unknown_target_rejected(engine, executor):
    draft = {**VALID_DRAFT, "target_ids": ["not_a_target"]}
    result = engine.handle({"command": "scene.play_draft", "scene": draft})
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert executor.calls == []
    assert "evening_glow" not in {scene.id for scene in engine._store.scenes.list_scenes()}


def test_play_draft_bumps_revision(engine):
    revision = engine.status()["engine"]["revision"]
    engine.handle({"command": "scene.play_draft", "scene": dict(STATIC_DRAFT)})
    assert engine.status()["engine"]["revision"] == revision + 1


# ---------------------------------------------------------------------------
# scene.create
# ---------------------------------------------------------------------------


def test_create_persists_canonical_scene(engine):
    result = engine.handle({"command": "scene.create", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    stored = result["data"]["scene"]
    assert stored["id"] == "evening_glow"
    assert stored["name"] == "Evening Glow"
    assert stored["palette"] == ["#ff7a45", "#6f4bff"]
    assert stored["motion"] == {"mode": "palette_cycle", "speed": 0.45, "strategy": "auto"}
    # Stored document equals the returned one (round-trip through the store).
    assert engine._store.scenes.get_scene("evening_glow").to_dict() == stored
    # Operational event, revision bump, and NO automatic apply/play.
    assert any(
        event.summary == "Evening Glow created" and event.category is EventCategory.SCENE
        for event in engine.recent_events(50)
    )
    assert engine.status()["current"] is None
    assert engine.status()["playback"]["counts"]["active"] == 0


def test_create_bumps_revision(engine):
    revision = engine.status()["engine"]["revision"]
    result = engine.handle({"command": "scene.create", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    assert engine.status()["engine"]["revision"] == revision + 1


def test_create_duplicate_active_id_conflicts(engine):
    engine.handle({"command": "scene.create", "scene": dict(VALID_DRAFT)})
    result = engine.handle({"command": "scene.create", "scene": {**VALID_DRAFT, "palette": ["#112233"]}})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert "evening_glow" in result["error"]["message"]


def test_create_collision_with_archived_id_conflicts(engine):
    engine._store.scenes.archive("twilight")
    result = engine.handle(
        {
            "command": "scene.create",
            "scene": {
                "schema_version": 2,
                "id": "",
                "name": "Twilight",
                "target_ids": ["office"],
                "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
            },
        }
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert "archived" in result["error"]["message"]


def test_create_unknown_target_rejected(engine):
    result = engine.handle({"command": "scene.create", "scene": {**VALID_DRAFT, "target_ids": ["ghost_room"]}})
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert result["error"]["details"]["path"] == "scene.target_ids.0"
    assert "evening_glow" not in {scene.id for scene in engine._store.scenes.list_scenes()}


def test_create_accepts_fixture_id_target(engine, executor):
    result = engine.handle({"command": "scene.create", "scene": {**VALID_DRAFT, "target_ids": ["lamp"]}})
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# scene.create — Duplicate / Save-as-New provenance (Builder-expansion §4)
# ---------------------------------------------------------------------------


def test_create_duplicate_of_records_server_derived_provenance(engine):
    result = engine.handle(
        {
            "command": "scene.create",
            "duplicate_of": "twilight",
            "scene": {**VALID_DRAFT, "name": "Evening Glow"},
        }
    )
    assert result["ok"] is True
    assert result["data"]["scene"]["metadata"]["duplicated_from"] == "twilight"
    # Original untouched.
    assert engine._store.scenes.get_scene("twilight").name == "Twilight"
    assert "duplicated_from" not in engine._store.scenes.get_scene("twilight").metadata


def test_create_strips_client_supplied_server_owned_provenance(engine):
    payload = {
        **VALID_DRAFT,
        "metadata": {
            "archived_at": "2026-09-01T12:00:00Z",
            "migrated_from_v1": {"filename": "twilight.json"},
            "duplicated_from": "twilight",
            "origin": "authored",
        },
    }
    result = engine.handle({"command": "scene.create", "scene": payload})
    assert result["ok"] is True
    metadata = result["data"]["scene"]["metadata"]
    assert metadata == {"origin": "authored"}


def test_create_duplicate_of_unknown_scene_not_found(engine):
    result = engine.handle(
        {"command": "scene.create", "duplicate_of": "ghost_scene", "scene": dict(VALID_DRAFT)}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"
    assert "evening_glow" not in {scene.id for scene in engine._store.scenes.list_scenes()}


def test_create_duplicate_of_archived_source_is_allowed(engine, store):
    store.scenes.archive("aurora")
    result = engine.handle(
        {"command": "scene.create", "duplicate_of": "aurora", "scene": {**VALID_DRAFT, "name": "Evening Glow"}}
    )
    assert result["ok"] is True
    assert result["data"]["scene"]["metadata"]["duplicated_from"] == "aurora"


def test_create_duplicate_does_not_inherit_source_id_or_history_provenance(engine, store):
    current = store.scenes.get_scene("twilight")
    current.metadata["origin"] = "migrated"
    current.metadata["migrated_from_v1"] = {"filename": "twilight.json"}
    store.scenes.replace_scene("twilight", current)
    source = store.scenes.get_scene("twilight").to_dict()
    # A Builder duplicate clears the id and carries the (deep-copied) intent,
    # including the source's server/history metadata — which the engine must
    # refuse to let a NEW document claim.
    payload = {key: value for key, value in source.items() if key != "id"}
    payload["name"] = "Twilight Copy"
    result = engine.handle({"command": "scene.create", "duplicate_of": "twilight", "scene": payload})
    assert result["ok"] is True
    stored = result["data"]["scene"]
    assert stored["id"] == "twilight_copy"
    # User-authored metadata survives; server/history provenance does not, and
    # the only provenance recorded is the server-derived duplicate link.
    assert stored["metadata"] == {"origin": "migrated", "duplicated_from": "twilight"}
    assert stored["fixture_states"] == source["fixture_states"]
    # The original is untouched.
    original = store.scenes.get_scene("twilight")
    assert "duplicated_from" not in original.metadata
    assert original.metadata["migrated_from_v1"] == {"filename": "twilight.json"}


def test_update_preserves_duplicated_from_through_edits(engine, store):
    engine.handle(
        {"command": "scene.create", "duplicate_of": "twilight", "scene": {**VALID_DRAFT, "name": "Evening Glow"}}
    )
    payload = store.scenes.get_scene("evening_glow").to_dict()
    payload["name"] = "Evening Glow Deep"
    payload.pop("metadata", None)
    result = engine.handle({"command": "scene.update", "scene_id": "evening_glow", "scene": payload})
    assert result["ok"] is True
    assert result["data"]["scene"]["metadata"]["duplicated_from"] == "twilight"


# ---------------------------------------------------------------------------
# scene.update
# ---------------------------------------------------------------------------


def updated_twilight_payload(store, **overrides):
    current = store.scenes.get_scene("twilight").to_dict()
    current.update(overrides)
    return current


def test_update_replaces_whole_document(engine, store):
    payload = updated_twilight_payload(
        store,
        name="Twilight Deep",
        palette=["#112233", "#445566", "#778899"],
        brightness=42.0,
        motion={"mode": "palette_cycle", "speed": 0.8, "strategy": "auto"},
        fixture_states={"g_strip": {"on": True, "brightness": 70.0, "color": "#112233"}},
    )
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is True
    stored = result["data"]["scene"]
    assert stored["name"] == "Twilight Deep"
    assert stored["palette"] == ["#112233", "#445566", "#778899"]
    assert stored["brightness"] == 42.0
    assert stored["motion"]["mode"] == "palette_cycle"
    assert stored["fixture_states"] == {"g_strip": {"on": True, "brightness": 70.0, "color": "#112233"}}
    assert store.scenes.get_scene("twilight").to_dict() == stored


def test_update_cannot_change_id(engine, store):
    payload = updated_twilight_payload(store, id="renamed_scene")
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert "immutable" in result["error"]["message"]
    # Store untouched: same id, same name, no stray new documents.
    assert store.scenes.get_scene("twilight").name == "Twilight"
    assert [scene.id for scene in store.scenes.list_scenes(include_archived=True)].count("twilight") == 1


def test_update_archived_scene_conflicts(engine, store):
    payload = updated_twilight_payload(store)
    store.scenes.archive("twilight")
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert "restore" in result["error"]["message"]


def test_update_missing_scene_not_found(engine):
    result = engine.handle(
        {
            "command": "scene.update",
            "scene_id": "ghost_scene",
            "scene": {
                "schema_version": 2,
                "name": "Ghost",
                "target_ids": ["office"],
                "motion": {"mode": "static", "speed": 0.0, "strategy": "auto"},
            },
        }
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "not_found"


def test_update_preserves_server_metadata(engine, store):
    # Simulate migration provenance the Builder never round-trips.
    current = store.scenes.get_scene("twilight")
    current.metadata["migrated_from_v1"] = {"filename": "twilight.json", "saved_at": "2026-03-18T20:40:12Z"}
    store.scenes.replace_scene("twilight", current)
    payload = updated_twilight_payload(store, name="Twilight Deep")
    payload.pop("metadata", None)
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is True
    stored = result["data"]["scene"]
    assert stored["metadata"]["migrated_from_v1"] == {"filename": "twilight.json", "saved_at": "2026-03-18T20:40:12Z"}


def test_update_server_metadata_wins_over_payload(engine, store):
    current = store.scenes.get_scene("twilight")
    current.metadata["migrated_from_v1"] = {"filename": "twilight.json"}
    store.scenes.replace_scene("twilight", current)
    payload = updated_twilight_payload(store, metadata={"migrated_from_v1": {"filename": "tampered.json"}})
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is True
    assert result["data"]["scene"]["metadata"]["migrated_from_v1"] == {"filename": "twilight.json"}


def test_update_unknown_target_rejected(engine, store):
    payload = updated_twilight_payload(store, target_ids=["studio", "ghost_room"])
    result = engine.handle({"command": "scene.update", "scene_id": "twilight", "scene": payload})
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert store.scenes.get_scene("twilight").target_ids == ["studio"]


def test_update_does_not_touch_live_playback_state(engine, store):
    result = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert result["ok"] is True
    session_id = result["data"]["session_id"]
    payload = store.scenes.get_scene("aurora").to_dict()
    payload["palette"] = ["#010203", "#040506"]
    updated = engine.handle({"command": "scene.update", "scene_id": "aurora", "scene": payload})
    assert updated["ok"] is True
    playback = engine.status()["playback"]
    assert playback["counts"]["active"] == 1
    assert playback["sessions"][0]["session_id"] == session_id


# ---------------------------------------------------------------------------
# store: SceneStore.replace_scene semantics
# ---------------------------------------------------------------------------


def test_store_replace_scene_atomic_round_trip(store, tmp_path):
    payload = updated_twilight_payload(store, name="Twilight Deep")
    stored = store.scenes.replace_scene("twilight", payload)
    assert stored.name == "Twilight Deep"
    # Re-read from disk: the atomic write landed.
    store.scenes.reload()
    assert store.scenes.get_scene("twilight").name == "Twilight Deep"
    # Detached copy: mutating the result cannot corrupt the store.
    stored.name = "corrupt"
    assert store.scenes.get_scene("twilight").name == "Twilight Deep"


def test_store_replace_scene_rejects_id_mismatch(store):
    payload = updated_twilight_payload(store, id="other_id")
    with pytest.raises(Exception) as excinfo:
        store.scenes.replace_scene("twilight", payload)
    assert "immutable" in str(excinfo.value)


def test_store_replace_scene_archived_not_found(store):
    payload = updated_twilight_payload(store)
    store.scenes.archive("twilight")
    from scene_studio.stores.errors import NotFoundError

    with pytest.raises(NotFoundError):
        store.scenes.replace_scene("twilight", payload)


# ---------------------------------------------------------------------------
# runtime policy
# ---------------------------------------------------------------------------


def test_read_only_permits_preview_draft_rejects_create_update(store):
    engine = make_engine(store, policy=RuntimePolicy.build("read_only"))
    result = engine.handle({"command": "scene.preview_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    result = engine.handle({"command": "scene.play_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    result = engine.handle({"command": "scene.create", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    result = engine.handle(
        {"command": "scene.update", "scene_id": "twilight", "scene": updated_twilight_payload(store)}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"


def test_registry_admin_permits_create_update_blocks_provider_writes(store):
    engine = make_engine(store, policy=RuntimePolicy.build("registry_admin"))
    result = engine.handle({"command": "scene.create", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is True
    result = engine.handle(
        {"command": "scene.update", "scene_id": "twilight", "scene": updated_twilight_payload(store)}
    )
    assert result["ok"] is True
    result = engine.handle({"command": "scene.apply", "scene_id": "twilight"})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    result = engine.handle({"command": "scene.play_draft", "scene": dict(VALID_DRAFT)})
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"


def test_normal_derives_authoring_commands_from_catalog(store):
    policy = RuntimePolicy.build("normal")
    for command in ("scene.preview_draft", "scene.play_draft", "scene.create", "scene.update"):
        assert command in policy.allowed_commands
        assert command in COMMAND_CATALOG


# ---------------------------------------------------------------------------
# legacy seams untouched
# ---------------------------------------------------------------------------


def test_scene_save_still_creates_empty_draft(engine):
    result = engine.handle({"command": "scene.save", "name": "Legacy Draft", "target_id": "office"})
    assert result["ok"] is True
    stored = result["data"]["scene"]
    assert stored["id"] == "legacy_draft"
    assert not stored.get("fixture_states")
    assert stored["motion"]["mode"] == "static"


def test_upsert_scene_still_updates_fixture_states_only(engine, store):
    before = store.scenes.get_scene("twilight")
    scene_dict = before.to_dict()
    scene_dict["palette"] = ["#999999"]
    scene_dict["name"] = "Should Not Change"
    result = engine.upsert_scene(scene_dict)
    assert result.ok is True
    after = store.scenes.get_scene("twilight")
    assert after.name == "Twilight"
    assert after.palette == []
    assert set(after.fixture_states) == set(before.fixture_states)
