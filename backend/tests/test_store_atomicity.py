"""Store atomicity, corruption handling, determinism, and facade rules (A1)."""

import json
import os
from pathlib import Path

import pytest

from scene_studio.stores import (
    ConflictError,
    CorruptStoreError,
    FixtureStore,
    NotFoundError,
    SceneStudioStore,
    StoreError,
    tmp_path_for,
)


def _seed_registry(root: Path, registry_data: dict) -> None:
    directory = root / "registry"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "registry.json").write_text(json.dumps(registry_data), encoding="utf-8")


def _build_store(root: Path, registry_data: dict, scenes_data: dict) -> SceneStudioStore:
    """Seed a facade store from raw sample dicts (exercises the dict add APIs)."""
    facade = SceneStudioStore(root)
    for fixture in registry_data["fixtures"]:
        facade.fixtures.add_fixture(fixture)
    for target in registry_data["targets"]:
        facade.fixtures.add_target(target)
    for scene in scenes_data["scenes"]:
        facade.scenes.add_scene(scene)
    return facade


def _materialize(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*.json"))
    }


def _explode(src, dst):
    raise RuntimeError("simulated crash between temp write and replace")


# ---------------------------------------------------------------------------
# error hierarchy
# ---------------------------------------------------------------------------

def test_store_error_hierarchy():
    for exc in (
        CorruptStoreError(Path("x.json"), "bad"),
        NotFoundError("fixture", "x"),
        ConflictError("dup"),
    ):
        assert isinstance(exc, StoreError)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_same_content_produces_identical_bytes(tmp_path, sample_registry_data, sample_scenes_data):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _build_store(root_a, sample_registry_data, sample_scenes_data)
    _build_store(root_b, sample_registry_data, sample_scenes_data)
    assert _materialize(root_a) == _materialize(root_b)

    # re-loading the same store and re-saving (no-op mutation) keeps bytes stable
    before = _materialize(root_a)
    facade = SceneStudioStore(root_a)
    facade.fixtures.set_enabled("lamp", True)
    assert _materialize(root_a) == before


def test_canonical_document_shape(tmp_path, sample_registry_data):
    _seed_registry(tmp_path, sample_registry_data)
    store = FixtureStore(tmp_path)
    store.set_enabled("lamp", False)

    document = json.loads((tmp_path / "registry" / "registry.json").read_text(encoding="utf-8"))
    assert list(document) == sorted(document)  # stable key order
    fixture_ids = [fixture["id"] for fixture in document["fixtures"]]
    target_ids = [target["id"] for target in document["targets"]]
    assert fixture_ids == sorted(fixture_ids)
    assert target_ids == sorted(target_ids)


def test_no_tmp_files_after_normal_operation(tmp_path, sample_registry_data, sample_scenes_data):
    _build_store(tmp_path, sample_registry_data, sample_scenes_data)
    facade = SceneStudioStore(tmp_path)
    facade.scenes.archive("meeting_blue")
    for directory in (tmp_path / "registry", tmp_path / "scenes", tmp_path / "scenes_archive"):
        leftovers = [entry.name for entry in directory.iterdir() if entry.name.endswith(".tmp")]
        assert leftovers == []


# ---------------------------------------------------------------------------
# crash-mid-write behavior
# ---------------------------------------------------------------------------

def test_failed_replace_preserves_original(tmp_path, monkeypatch, sample_registry_data):
    _seed_registry(tmp_path, sample_registry_data)
    store = FixtureStore(tmp_path)
    registry_file = tmp_path / "registry" / "registry.json"
    original = registry_file.read_bytes()

    monkeypatch.setattr(os, "replace", _explode)
    with pytest.raises(RuntimeError):
        store.set_enabled("lamp", False)

    # original document untouched, no partial file, memory consistent with disk
    assert registry_file.read_bytes() == original
    assert len(store.list_fixtures()) == 24
    assert store.get_fixture("lamp").enabled is True
    assert not [e for e in (tmp_path / "registry").iterdir() if e.name.endswith(".tmp")]

    monkeypatch.undo()
    store.set_enabled("lamp", False)
    assert store.get_fixture("lamp").enabled is False


def test_failed_first_write_leaves_no_file(tmp_path, monkeypatch):
    store = FixtureStore(tmp_path)
    monkeypatch.setattr(os, "replace", _explode)
    with pytest.raises(RuntimeError):
        store.add_fixture({"id": "solo", "name": "Solo"})
    assert not (tmp_path / "registry" / "registry.json").exists()
    assert not [e for e in (tmp_path / "registry").iterdir() if e.name.endswith(".tmp")]
    assert store.list_fixtures() == []  # memory stayed consistent with disk


def test_tmp_leftover_is_repaired_on_load(tmp_path, sample_registry_data):
    _seed_registry(tmp_path, sample_registry_data)
    leftover = tmp_path_for(tmp_path / "registry" / "registry.json")
    leftover.write_text('{"schema_version": 1, "fix', encoding="utf-8")

    store = FixtureStore(tmp_path)

    assert len(store.list_fixtures()) == 24
    assert not leftover.exists()


# ---------------------------------------------------------------------------
# corruption detection
# ---------------------------------------------------------------------------

def test_corrupt_json_reports_path(tmp_path):
    target = tmp_path / "registry" / "registry.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"schema_version": 1, "fixtures": [', encoding="utf-8")

    with pytest.raises(CorruptStoreError) as excinfo:
        FixtureStore(tmp_path)

    assert excinfo.value.path == target
    assert str(target) in str(excinfo.value)
    assert isinstance(excinfo.value, StoreError)


def test_schema_invalid_document_reports_validation_message(tmp_path):
    target = tmp_path / "registry" / "registry.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {"schema_version": 1, "fixtures": [{"id": "Bad Id", "name": "x"}], "targets": []}
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorruptStoreError, match="fixture_id"):
        FixtureStore(tmp_path)


def test_non_finite_constants_rejected(tmp_path):
    target = tmp_path / "registry" / "registry.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        '{"schema_version": 1, "fixtures": [], "targets": [], "updated": NaN}',
        encoding="utf-8",
    )
    with pytest.raises(CorruptStoreError, match="invalid JSON"):
        FixtureStore(tmp_path)


# ---------------------------------------------------------------------------
# facade: cross-store referential rules + target resolution
# ---------------------------------------------------------------------------

@pytest.fixture()
def facade(tmp_path, sample_registry_data, sample_scenes_data) -> SceneStudioStore:
    return _build_store(tmp_path, sample_registry_data, sample_scenes_data)


def test_facade_remove_blocked_by_active_scene(facade):
    with pytest.raises(ConflictError, match="twilight"):
        facade.remove_fixture("g_strip")
    assert facade.fixtures.get_fixture("g_strip").id == "g_strip"


def test_facade_remove_allowed_when_only_archived_scenes_reference(facade):
    # A fixture referenced only by an archived scene must not block removal.
    facade.fixtures.add_fixture(
        {
            "id": "spare_lamp",
            "name": "Spare Lamp",
            "groups": ["studio"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.spare_lamp"},
            "capabilities": {"on_off": True, "brightness": True},
        }
    )
    facade.scenes.update_fixture_states(
        "meeting_blue",
        {
            "office_strip": {"on": True, "brightness": 70.0, "color": "#2962ff"},
            "spare_lamp": {"on": False},
        },
    )
    facade.scenes.archive("meeting_blue")
    facade.remove_fixture("spare_lamp")
    with pytest.raises(NotFoundError):
        facade.fixtures.get_fixture("spare_lamp")


def test_facade_remove_unreferenced_fixture(facade):
    # custom_gradient and double_strip appear in no scene fixture_states
    facade.remove_fixture("double_strip")
    with pytest.raises(NotFoundError):
        facade.fixtures.get_fixture("double_strip")


def test_facade_resolve_target_delegates(facade):
    assert "office_strip" in {f.id for f in facade.resolve_target("studio")}
    assert [f.id for f in facade.resolve_target("g_strip")] == ["g_strip"]
    with pytest.raises(NotFoundError):
        facade.resolve_target("podium")
