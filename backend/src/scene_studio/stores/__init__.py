"""Persistence layer for Scene Studio (Wave A1).

Pure-stdlib storage of the frozen Phase 1 domain documents:

- `FixtureStore`     — `<root>/registry/registry.json` (FixtureRegistry v1)
- `SceneStore`       — `<root>/scenes/<scene_id>.json` + `<root>/scenes_archive/<scene_id>.json`
- `SceneStudioStore` — facade wiring both; owns cross-store rules
  (fixture removal blocked while an active scene references it, target
  resolution).

Guarantees:

- atomic writes (temp file + `os.replace` in the same directory);
  crash-mid-write never leaves a partial file, and stale `.tmp`
  leftovers are tolerated/removed on read;
- every load validates through the domain `from_dict` constructors —
  corrupt/invalid documents raise `CorruptStoreError` carrying the file
  path and the validation message, never silently skipped;
- storage filenames are keyed by stable document id (renames change
  display names only);
- deterministic output: listing APIs sort by id and serialization is
  canonical (sorted keys, 2-space indent, UTF-8, trailing newline), so
  the same state always produces the same bytes.

No AppDaemon imports, no network, no third-party dependencies.
"""

from .atomic import (
    atomic_write_bytes,
    atomic_write_json,
    dump_canonical_json,
    read_json,
    tmp_path_for,
)
from .errors import ConflictError, CorruptStoreError, NotFoundError, StoreError
from .fixtures import FixtureStore
from .scenes import SceneStore
from .store import SceneStudioStore

__all__ = [
    "ConflictError",
    "CorruptStoreError",
    "FixtureStore",
    "NotFoundError",
    "SceneStudioStore",
    "SceneStore",
    "StoreError",
    "atomic_write_bytes",
    "atomic_write_json",
    "dump_canonical_json",
    "read_json",
    "tmp_path_for",
]
