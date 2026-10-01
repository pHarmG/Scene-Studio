"""Crash-safe JSON primitives for the Scene Studio store (Wave A1).

- Writes go to a hidden temp file (`.\\<name>.tmp`) in the destination
  directory, are flushed + fsynced, then moved into place with
  `os.replace` — atomic on POSIX and on Windows (same volume). A crash
  mid-write can therefore only ever leave the previous good file plus a
  temp leftover, never a partial document.
- Readers treat temp leftovers as junk: they are never authoritative and
  are removed opportunistically on read ("repair").
- Serialization is canonical: sorted keys, 2-space indent, UTF-8,
  `ensure_ascii=False`, trailing newline. Non-finite constants
  (NaN/Infinity) are rejected on both write and read, so the same
  document state always yields the same bytes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import CorruptStoreError

__all__ = [
    "atomic_write_bytes",
    "atomic_write_json",
    "dump_canonical_json",
    "read_json",
    "tmp_path_for",
]


def tmp_path_for(path: Path) -> Path:
    """The temp-file path used while atomically writing `path` (same directory)."""
    return path.with_name(f".{path.name}.tmp")


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value!r} is not allowed")


def dump_canonical_json(payload: Any) -> bytes:
    """Serialize to the store's canonical byte form (stable key order)."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return (text + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` via temp file + `os.replace`.

    The temp file is removed on any failure, so a failed write leaves the
    previous file (if any) intact and never leaves a partial document or a
    `.tmp` leftover behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Atomically persist `payload` in the store's canonical JSON form."""
    atomic_write_bytes(path, dump_canonical_json(payload))


def read_json(path: Path) -> Any:
    """Parse a stored JSON document.

    Raises FileNotFoundError when absent (callers decide what missing
    means, e.g. an empty store) and CorruptStoreError (with the path) for
    anything unreadable, unparsable, or containing non-finite constants.
    Stale `.tmp` leftovers next to the document are removed on read.
    """
    tmp = tmp_path_for(path)
    if tmp.exists():
        tmp.unlink(missing_ok=True)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptStoreError(path, f"unreadable: {exc}") from exc
    try:
        return json.loads(text, parse_constant=_reject_non_finite)
    except ValueError as exc:
        raise CorruptStoreError(path, f"invalid JSON: {exc}") from exc
