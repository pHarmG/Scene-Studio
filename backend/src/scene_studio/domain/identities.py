"""Stable logical identity rules.

IDs are the automation contract (master plan §2.6). Display names are
editable labels; IDs never change after creation. All ID kinds share one
charset: lowercase ASCII, start with a letter, then letters/digits/underscore,
max 64 chars. Examples: `g_strip`, `living_room`, `twilight`.
"""

from __future__ import annotations

import re

from .serde import ValidationError, require_str

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ID_MAX_LENGTH = 64


def is_valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.match(value))


def validate_id(value: str, kind: str, path: str) -> str:
    """Validate an identifier of `kind` (e.g. "fixture_id", "scene_id", "target_id")."""
    checked = require_str({"value": value}, "value", path, max_length=ID_MAX_LENGTH)
    if not ID_PATTERN.match(checked):
        raise ValidationError(
            path,
            f"invalid {kind} {checked!r}: must match {ID_PATTERN.pattern} "
            "(lowercase, start with a letter, letters/digits/underscore)",
        )
    return checked


def normalize_name_to_id(name: str) -> str:
    """Derive an ID candidate from a display name (best effort, may need review).

    Used by migration/seed tooling, never by runtime rebinding.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return "unnamed"
    if not ID_PATTERN.match(slug):
        slug = "f_" + re.sub(r"[^a-z0-9_]", "", slug)
    return slug[:ID_MAX_LENGTH]
