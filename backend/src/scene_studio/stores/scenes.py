"""SceneStore — per-scene JSON documents with archive/restore (Wave A1).

Storage layout:

    <root>/scenes/<scene_id>.json           # active scenes (schema_version 2)
    <root>/scenes_archive/<scene_id>.json   # archived scenes

Policy (documented for reviewers):

- Filenames are keyed by the stable scene id, never the display name.
  Renaming a scene changes `name` only; the file never moves. A document
  whose id disagrees with its filename is a corrupt store.
- Scene ids are unique across BOTH spaces (checked at load and on add), so
  `restore` can never collide and `add` can never resurrect over an
  archived document.
- `archive` stamps `metadata.archived_at` (ISO-8601 UTC with `Z`; the
  stamp source is injectable via the `clock` argument for deterministic
  tests), writes the stamped content back to the active path atomically,
  then moves the file into the archive directory with `os.replace` (same
  volume -> atomic). `restore` is the exact mirror image and clears
  `archived_at`. No crash window loses scene content: the worst case is a
  scene that is already stamped but not yet moved, which a repeated
  archive completes.
- Archival state is the file LOCATION. `get_scene`/`list_scenes` hide
  archived scenes unless `include_archived=True`; `list_archived` shows
  only them. All other content is preserved byte-for-byte through
  archive/restore apart from `metadata.archived_at`.
- Scene documents are NOT cross-checked against the fixture registry
  here: contract §3 resolution is fixture-driven and unknown
  `fixture_states` keys are inert. Cross-store rules (e.g. blocking
  fixture removal) live on `SceneStudioStore`.
- Fixture-state updates REPLACE the whole `fixture_states` map; every
  value is re-validated through `FixtureState.from_dict`. All returned
  models are detached copies; mutate through store methods.
- `replace_scene` (Builder Pass 2) is the canonical whole-document
  update: active scenes only, the stable id can never change, the
  replacement document is strictly re-validated through `Scene.from_dict`,
  and the write reuses the same atomic JSON path as every other mutation.
  `update_fixture_states()` remains for the legacy generation flow only.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..domain.scenes import FixtureState, Scene
from ..domain.serde import ValidationError
from .atomic import atomic_write_json, read_json
from .errors import ConflictError, CorruptStoreError, NotFoundError

__all__ = ["SceneStore"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copy_scene(scene: Scene) -> Scene:
    """Detached, strictly-validated copy (also the write-path validation gate)."""
    return Scene.from_dict(scene.to_dict(), "scene")


class SceneStore:
    """Persists scenes as one JSON document per scene id under `<root>/scenes/`."""

    def __init__(self, root: Path | str, *, clock: Callable[[], str] | None = None) -> None:
        self.root = Path(root)
        self.scenes_dir = self.root / "scenes"
        self.archive_dir = self.root / "scenes_archive"
        self._clock = clock or _utc_now
        self._active: dict[str, Scene] = {}
        self._archived: dict[str, Scene] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def _read_scene(self, path: Path) -> Scene:
        data = read_json(path)
        try:
            return Scene.from_dict(data, "scene")
        except ValidationError as exc:
            raise CorruptStoreError(path, f"scene failed validation: {exc}") from exc

    def _read_space(self, directory: Path) -> dict[str, Scene]:
        scenes: dict[str, Scene] = {}
        if not directory.is_dir():
            return scenes
        for path in sorted(directory.glob("*.json")):
            scene = self._read_scene(path)
            if path.stem != scene.id:
                raise CorruptStoreError(
                    path,
                    f"document id {scene.id!r} does not match filename stem {path.stem!r}; "
                    "scene files are keyed by stable scene id",
                )
            scenes[scene.id] = scene
        return scenes

    def _load_all(self) -> None:
        active = self._read_space(self.scenes_dir)
        archived = self._read_space(self.archive_dir)
        overlap = sorted(set(active) & set(archived))
        if overlap:
            scene_id = overlap[0]
            raise CorruptStoreError(
                self.scenes_dir / f"{scene_id}.json",
                f"scene id {scene_id!r} exists in both {self.scenes_dir} and "
                f"{self.archive_dir}; remove the stale copy",
            )
        self._active, self._archived = active, archived

    def reload(self) -> None:
        """Re-read both scene spaces from disk."""
        self._load_all()

    # ------------------------------------------------------------------
    # path helpers
    # ------------------------------------------------------------------

    def _active_path(self, scene_id: str) -> Path:
        return self.scenes_dir / f"{scene_id}.json"

    def _archive_path(self, scene_id: str) -> Path:
        return self.archive_dir / f"{scene_id}.json"

    # ------------------------------------------------------------------
    # scene semantics
    # ------------------------------------------------------------------

    def get_scene(self, scene_id: str, *, include_archived: bool = False) -> Scene:
        """The scene with `scene_id` (detached copy); archived scenes hidden by default."""
        scene = self._active.get(scene_id)
        if scene is not None:
            return _copy_scene(scene)
        if scene_id in self._archived:
            if include_archived:
                return _copy_scene(self._archived[scene_id])
            raise NotFoundError(
                "scene", scene_id, detail="scene is archived; pass include_archived=True or restore it"
            )
        raise NotFoundError("scene", scene_id)

    def list_scenes(self, *, include_archived: bool = False) -> list[Scene]:
        """Active scenes in stable id order (deterministic catalog output)."""
        pool = dict(self._archived)
        pool.update(self._active)
        visible = pool if include_archived else self._active
        return [_copy_scene(visible[scene_id]) for scene_id in sorted(visible)]

    def list_archived(self) -> list[Scene]:
        """Archived scenes in stable id order."""
        return [_copy_scene(self._archived[scene_id]) for scene_id in sorted(self._archived)]

    def add_scene(self, scene: Scene | dict) -> Scene:
        """Add a scene (dict or model; strictly validated). Duplicate ids conflict
        against active AND archived scenes."""
        validated = _copy_scene(scene) if isinstance(scene, Scene) else Scene.from_dict(scene, "scene")
        scene_id = validated.id
        if scene_id in self._active:
            raise ConflictError(f"active scene already exists: {scene_id!r}")
        if scene_id in self._archived:
            raise ConflictError(
                f"an archived scene already uses id {scene_id!r}; restore it instead of re-adding"
            )
        atomic_write_json(self._active_path(scene_id), validated.to_dict())
        self._active[scene_id] = validated
        return _copy_scene(validated)

    def rename_scene(self, scene_id: str, new_name: str) -> Scene:
        """Change the display label only; id and filename never move."""
        current = self._require_active(scene_id)
        updated = _copy_scene(current)
        updated.name = new_name
        validated = _copy_scene(updated)
        atomic_write_json(self._active_path(scene_id), validated.to_dict())
        self._active[scene_id] = validated
        return _copy_scene(validated)

    def update_fixture_states(self, scene_id: str, fixture_states: dict) -> Scene:
        """Replace the scene's whole `fixture_states` map (values: FixtureState|dict)."""
        current = self._require_active(scene_id)
        updated = _copy_scene(current)
        states: dict[str, FixtureState] = {}
        for fixture_id, state in fixture_states.items():
            if isinstance(state, FixtureState):
                states[fixture_id] = FixtureState.from_dict(state.to_dict(), f"scene.fixture_states.{fixture_id}")
            else:
                states[fixture_id] = FixtureState.from_dict(state, f"scene.fixture_states.{fixture_id}")
        updated.fixture_states = states
        validated = _copy_scene(updated)
        atomic_write_json(self._active_path(scene_id), validated.to_dict())
        self._active[scene_id] = validated
        return _copy_scene(validated)

    def replace_scene(self, scene_id: str, scene: Scene | dict) -> Scene:
        """Atomically replace the WHOLE document of an active scene (Builder Pass 2).

        - Active scenes only; archived scenes must be restored first.
        - The stable id is immutable: the replacement document must carry
          the same id (a payload attempting to change it is a
          ValidationError, not a silent rename).
        - The replacement is strictly re-validated through `Scene.from_dict`
          and written with the same atomic JSON path as every other store
          mutation. Archived-space uniqueness is unaffected because the id
          (and therefore the filename) never moves.
        """
        self._require_active(scene_id)
        validated = _copy_scene(scene) if isinstance(scene, Scene) else Scene.from_dict(scene, "scene")
        if validated.id != scene_id:
            raise ValidationError(
                "scene.id",
                f"scene id is immutable: replacement document carries {validated.id!r}, "
                f"but the stored scene is {scene_id!r}",
            )
        atomic_write_json(self._active_path(scene_id), validated.to_dict())
        self._active[scene_id] = validated
        return _copy_scene(validated)

    # ------------------------------------------------------------------
    # archive / restore
    # ------------------------------------------------------------------

    def archive(self, scene_id: str) -> Scene:
        """Move the scene to `<root>/scenes_archive/` and stamp `archived_at`.

        Content is preserved byte-for-byte apart from the new
        `metadata.archived_at` stamp.
        """
        current = self._require_active(scene_id)
        stamped = _copy_scene(current)
        stamped.metadata["archived_at"] = self._clock()
        validated = _copy_scene(stamped)
        active_path = self._active_path(scene_id)
        archive_path = self._archive_path(scene_id)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(active_path, validated.to_dict())  # 1. stamp in place (atomic)
        try:
            os.replace(active_path, archive_path)  # 2. atomic move, same volume
        except BaseException:
            # The move failed: disk now holds the stamped active scene; resync.
            self._active[scene_id] = self._read_scene(active_path)
            raise
        del self._active[scene_id]
        self._archived[scene_id] = validated
        return _copy_scene(validated)

    def restore(self, scene_id: str) -> Scene:
        """Move an archived scene back to `<root>/scenes/` and clear `archived_at`.

        Content is preserved byte-for-byte apart from the removed stamp.
        """
        current = self._archived.get(scene_id)
        if current is None:
            if scene_id in self._active:
                raise NotFoundError("scene", scene_id, detail="scene is not archived")
            raise NotFoundError("scene", scene_id)
        cleaned = _copy_scene(current)
        cleaned.metadata.pop("archived_at", None)
        validated = _copy_scene(cleaned)
        archive_path = self._archive_path(scene_id)
        active_path = self._active_path(scene_id)
        self.scenes_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(archive_path, validated.to_dict())  # 1. unstamp in place (atomic)
        try:
            os.replace(archive_path, active_path)  # 2. atomic move back, same volume
        except BaseException:
            # The move failed: disk now holds the unstamped archived scene; resync.
            self._archived[scene_id] = self._read_scene(archive_path)
            raise
        del self._archived[scene_id]
        self._active[scene_id] = validated
        return _copy_scene(validated)

    # ------------------------------------------------------------------
    # cross-store helpers (used by SceneStudioStore)
    # ------------------------------------------------------------------

    def _require_active(self, scene_id: str) -> Scene:
        scene = self._active.get(scene_id)
        if scene is not None:
            return scene
        if scene_id in self._archived:
            raise NotFoundError("scene", scene_id, detail="scene is archived; restore it first")
        raise NotFoundError("scene", scene_id)

    def scenes_referencing_fixture(self, fixture_id: str) -> list[str]:
        """Active scene ids whose `fixture_states` mention `fixture_id`, sorted.

        Archived scenes are intentionally excluded: they preserve history,
        and restoring into a registry without the fixture is safe because
        resolution is fixture-driven (contracts §3) and unknown
        `fixture_states` keys are inert.
        """
        return sorted(
            scene_id for scene_id, scene in self._active.items() if fixture_id in scene.fixture_states
        )
