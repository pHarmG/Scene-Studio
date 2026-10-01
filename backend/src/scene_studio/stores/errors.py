"""Store error hierarchy (Wave A1).

Mapping to the command API error codes (ARCHITECTURE_CONTRACTS §4):

- `NotFoundError`                -> `not_found`
- `ConflictError`                -> `conflict`
- `CorruptStoreError`/`StoreError` -> `internal_error`
- domain `ValidationError` raised while validating a store *method argument*
  maps to `validation_error` (on-disk validation failures are wrapped in
  `CorruptStoreError` instead, so the file path is always attached).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ConflictError", "CorruptStoreError", "NotFoundError", "StoreError"]


class StoreError(Exception):
    """Base class for all Scene Studio persistence-layer failures."""


class CorruptStoreError(StoreError):
    """A stored document is unreadable, unparsable, or fails domain validation.

    `path` is the offending file; `detail` carries the parse/validation
    message. Never raised for a *missing* file (callers decide what
    missing means, e.g. an empty store).
    """

    def __init__(self, path: Path | str, detail: str) -> None:
        self.path = Path(path)
        self.detail = detail
        super().__init__(f"{self.path}: {detail}")


class NotFoundError(StoreError):
    """No stored document with the requested id."""

    def __init__(self, kind: str, id_: str, detail: str | None = None) -> None:
        self.kind = kind
        self.id = id_
        message = f"no such {kind}: {id_!r}"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)


class ConflictError(StoreError):
    """The request collides with existing state (duplicate id, in-use resource)."""
