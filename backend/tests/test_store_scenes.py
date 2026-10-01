"""SceneStore semantics: per-id files, archive/restore, stable names, corruption (A1)."""

import json
import re
from pathlib import Path

import pytest

from scene_studio.domain.scenes import FixtureState, Scene
from scene_studio.domain.serde import ValidationError
from scene_studio.stores import (
    ConflictError,
    CorruptStoreError,
    NotFoundError,
    SceneStore,
    dump_canonical_json,
)

ARCHIVED_AT = "2026-09-10T12:00:00Z"
SAMPLE_IDS = ["aurora_flow", "meeting_blue", "twilight"]


@pytest.fixture()
def store(tmp_path, sample_scenes_data) -> SceneStore:
    scene_store = SceneStore(tmp_path)
    for data in sample_scenes_data["scenes"]:
        scene_store.add_scene(data)
    return scene_store


# ---------------------------------------------------------------------------
# add / get / list
# ---------------------------------------------------------------------------

def test_add_sample_scenes_round_trip(store, sample_scenes_data):
    assert [s.id for s in store.list_scenes()] == sorted(SAMPLE_IDS)
    sample = {data["id"]: data for data in sample_scenes_data["scenes"]}
    for scene in store.list_scenes():
        assert scene.to_dict() == sample[scene.id]


def test_fresh_root_is_empty(tmp_path):
    scene_store = SceneStore(tmp_path / "deep" / "nested")
    assert scene_store.list_scenes() == []
    assert scene_store.list_archived() == []


def test_files_keyed_by_stable_id(store, tmp_path):
    assert {p.name for p in (tmp_path / "scenes").glob("*.json")} == {
        "twilight.json",
        "aurora_flow.json",
        "meeting_blue.json",
    }


def test_add_rejects_duplicate_active_and_archived(store):
    with pytest.raises(ConflictError, match="already exists"):
        store.add_scene(
            {"schema_version": 2, "id": "twilight", "name": "Dup", "target_ids": ["studio"]}
        )
    store.archive("meeting_blue")
    with pytest.raises(ConflictError, match="archived"):
        store.add_scene(
            {"schema_version": 2, "id": "meeting_blue", "name": "Dup", "target_ids": ["office"]}
        )


def test_add_scene_validates_strictly(store):
    with pytest.raises(ValidationError, match="scene_id"):
        store.add_scene({"schema_version": 2, "id": "bad id", "name": "x", "target_ids": ["studio"]})
    with pytest.raises(ValidationError, match="schema_version"):
        store.add_scene({"schema_version": 1, "id": "legacy", "name": "x", "target_ids": ["studio"]})
    assert [s.id for s in store.list_scenes()] == sorted(SAMPLE_IDS)


def test_get_unknown_scene(store):
    with pytest.raises(NotFoundError):
        store.get_scene("nope")


# ---------------------------------------------------------------------------
# rename / update fixture_states
# ---------------------------------------------------------------------------

def test_rename_scene_keeps_id_and_filename(store, tmp_path):
    path = tmp_path / "scenes" / "twilight.json"
    assert path.exists()

    renamed = store.rename_scene("twilight", "Twilight Evening")

    assert renamed.id == "twilight"
    assert renamed.name == "Twilight Evening"
    assert path.exists(), "stable-id filename must not move on rename"
    assert not (tmp_path / "scenes" / "Twilight Evening.json").exists()
    fresh = SceneStore(tmp_path)
    assert fresh.get_scene("twilight").name == "Twilight Evening"


def test_update_fixture_states_replaces_map(store, tmp_path):
    updated = store.update_fixture_states(
        "meeting_blue",
        {
            "office_strip": {"on": True, "brightness": 10.0},
            "lamp": FixtureState(on=False),
        },
    )
    assert set(updated.fixture_states) == {"office_strip", "lamp"}
    assert updated.fixture_states["office_strip"].brightness == 10.0

    fresh = SceneStore(tmp_path)
    reloaded = fresh.get_scene("meeting_blue")
    assert set(reloaded.fixture_states) == {"office_strip", "lamp"}
    assert reloaded.fixture_states["lamp"].on is False


def test_update_fixture_states_validates_values(store):
    with pytest.raises(ValidationError, match="at least one field"):
        store.update_fixture_states("meeting_blue", {"office_strip": {}})
    with pytest.raises(ValidationError, match="fixture_id"):
        store.update_fixture_states("meeting_blue", {"Bad Id": {"on": True}})
    # failed update leaves the stored map untouched
    assert set(store.get_scene("meeting_blue").fixture_states) == {"office_strip"}


# ---------------------------------------------------------------------------
# archive / restore
# ---------------------------------------------------------------------------

def test_archive_moves_file_and_stamps_metadata(tmp_path, sample_scenes_data):
    scene_store = SceneStore(tmp_path, clock=lambda: ARCHIVED_AT)
    for data in sample_scenes_data["scenes"]:
        scene_store.add_scene(data)
    pre = scene_store.get_scene("meeting_blue").to_dict()

    archived = scene_store.archive("meeting_blue")

    assert not (tmp_path / "scenes" / "meeting_blue.json").exists()
    archive_path = tmp_path / "scenes_archive" / "meeting_blue.json"
    assert archive_path.exists()
    expected = json.loads(json.dumps(pre))  # deep copy
    expected["metadata"]["archived_at"] = ARCHIVED_AT
    assert archived.to_dict() == expected  # content preserved apart from archived_at
    assert archive_path.read_bytes() == dump_canonical_json(expected)


def test_restore_moves_back_and_preserves_content(store, tmp_path):
    pre = store.get_scene("twilight").to_dict()

    archived = store.archive("twilight")
    assert "archived_at" in archived.metadata
    assert not (tmp_path / "scenes" / "twilight.json").exists()

    restored = store.restore("twilight")

    assert restored.to_dict() == pre  # archived_at cleared, everything else identical
    assert (tmp_path / "scenes" / "twilight.json").exists()
    assert not (tmp_path / "scenes_archive" / "twilight.json").exists()
    # byte-for-byte: the canonical file equals the canonical pre-archive form
    assert (tmp_path / "scenes" / "twilight.json").read_bytes() == dump_canonical_json(pre)


def test_archive_timestamp_defaults_to_utc_z(store):
    archived = store.archive("meeting_blue")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", archived.metadata["archived_at"])


def test_archive_unknown_scene_raises(store):
    with pytest.raises(NotFoundError):
        store.archive("nope")


def test_restore_requires_archived_scene(store):
    with pytest.raises(NotFoundError, match="not archived"):
        store.restore("twilight")
    with pytest.raises(NotFoundError):
        store.restore("nope")


def test_archived_scenes_hidden_by_default(store):
    store.archive("meeting_blue")

    with pytest.raises(NotFoundError, match="archived"):
        store.get_scene("meeting_blue")
    assert store.get_scene("meeting_blue", include_archived=True).metadata["archived_at"]

    assert [s.id for s in store.list_scenes()] == ["aurora_flow", "twilight"]
    assert [s.id for s in store.list_scenes(include_archived=True)] == sorted(
        [*SAMPLE_IDS]
    )
    assert [s.id for s in store.list_archived()] == ["meeting_blue"]


# ---------------------------------------------------------------------------
# corruption detection
# ---------------------------------------------------------------------------

def test_scene_id_filename_mismatch_is_corrupt(tmp_path):
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(parents=True)
    document = Scene(id="twilight", name="Twilight", target_ids=["studio"]).to_dict()
    (scenes_dir / "lamp.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(CorruptStoreError) as excinfo:
        SceneStore(tmp_path)
    assert excinfo.value.path == scenes_dir / "lamp.json"


def test_scene_id_in_both_spaces_is_corrupt(store, tmp_path):
    active = tmp_path / "scenes" / "twilight.json"
    archive_copy = tmp_path / "scenes_archive" / "twilight.json"
    archive_copy.parent.mkdir()
    archive_copy.write_bytes(active.read_bytes())  # simulate a stale crash copy
    with pytest.raises(CorruptStoreError, match="both"):
        SceneStore(tmp_path)


def test_invalid_scene_document_reports_path_and_message(tmp_path):
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(parents=True)
    path = scenes_dir / "broken.json"
    path.write_text(
        json.dumps({"schema_version": 2, "id": "broken", "name": "x", "target_ids": [], "wat": 1}),
        encoding="utf-8",
    )
    with pytest.raises(CorruptStoreError, match="broken.json"):
        SceneStore(tmp_path)


# ---------------------------------------------------------------------------
# cross-store helper
# ---------------------------------------------------------------------------

def test_scenes_referencing_fixture_excludes_archived(store):
    assert store.scenes_referencing_fixture("g_strip") == ["aurora_flow", "twilight"]
    # twilight (target studio) also overrides office_strip directly in fixture_states
    assert store.scenes_referencing_fixture("office_strip") == ["meeting_blue", "twilight"]
    assert store.scenes_referencing_fixture("unbound_thing") == []
    store.archive("twilight")
    assert store.scenes_referencing_fixture("g_strip") == ["aurora_flow"]
