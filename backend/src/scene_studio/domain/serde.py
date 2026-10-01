"""Shared serialization/validation helpers for Scene Studio domain models.

Every domain model implements `to_dict()` (JSON-safe plain dict) and
`from_dict(data, path)` (validated construction). Validation failures raise
:class:`ValidationError` with a dotted path (e.g. `scene.motion.speed`) so
callers (API, migration, UI) can report precisely what was wrong.

Policy: structural keys are strict (unknown keys rejected); `metadata` and
`extra` dicts are open by contract.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable


class ValidationError(ValueError):
    """A domain document failed validation.

    `path` is a dotted locator such as `scene.fixture_states.g_strip.color`;
    empty string means the document root.
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path or '<root>'}: {message}" if path else message)


def join(path: str, key: str | int) -> str:
    return f"{path}.{key}" if path else str(key)


# ---------------------------------------------------------------------------
# primitive field readers
# ---------------------------------------------------------------------------

def require_mapping(data: Any, path: str) -> dict:
    if not isinstance(data, dict):
        raise ValidationError(path, "expected a JSON object")
    return data


def reject_unknown_keys(data: dict, allowed: Iterable[str], path: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValidationError(path, f"unknown key(s): {', '.join(unknown)}")


def require_str(data: dict, key: str, path: str, *, max_length: int = 512) -> str:
    value = data.get(key)
    field = join(path, key)
    if not isinstance(value, str):
        raise ValidationError(field, "expected a string")
    if not value:
        raise ValidationError(field, "must not be empty")
    if len(value) > max_length:
        raise ValidationError(field, f"exceeds {max_length} characters")
    return value


def optional_str(data: dict, key: str, path: str, *, max_length: int = 512) -> str | None:
    if key not in data or data[key] is None:
        return None
    return require_str(data, key, path, max_length=max_length)


def require_bool(data: dict, key: str, path: str) -> bool:
    value = data.get(key)
    field = join(path, key)
    if not isinstance(value, bool):
        raise ValidationError(field, "expected a boolean")
    return value


def optional_bool(data: dict, key: str, path: str) -> bool | None:
    if key not in data or data[key] is None:
        return None
    return require_bool(data, key, path)


def require_int(data: dict, key: str, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value = data.get(key)
    field = join(path, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(field, "expected an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(field, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(field, f"must be <= {maximum}")
    return value


def require_float(data: dict, key: str, path: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    value = data.get(key)
    field = join(path, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(field, "expected a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValidationError(field, f"must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ValidationError(field, f"must be <= {maximum}")
    return number


def optional_float(
    data: dict, key: str, path: str, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    if key not in data or data[key] is None:
        return None
    return require_float(data, key, path, minimum=minimum, maximum=maximum)


def optional_int(
    data: dict, key: str, path: str, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    if key not in data or data[key] is None:
        return None
    return require_int(data, key, path, minimum=minimum, maximum=maximum)


def require_enum(data: dict, key: str, path: str, enum_cls: type[Enum]) -> Enum:
    value = data.get(key)
    field = join(path, key)
    try:
        return enum_cls(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_cls)
        raise ValidationError(field, f"must be one of: {allowed}") from None


def require_str_list(
    data: dict, key: str, path: str, *, min_length: int | None = None, max_length: int | None = None
) -> list[str]:
    value = data.get(key)
    field = join(path, key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(field, "expected a list of strings")
    if min_length is not None and len(value) < min_length:
        raise ValidationError(field, f"must contain at least {min_length} item(s)")
    if max_length is not None and len(value) > max_length:
        raise ValidationError(field, f"must contain at most {max_length} item(s)")
    return list(value)


def optional_str_list(
    data: dict, key: str, path: str, *, max_length: int | None = None
) -> list[str]:
    if key not in data or data[key] is None:
        return []
    return require_str_list(data, key, path, max_length=max_length)


def require_list(
    data: dict, key: str, path: str, item_fn: Callable[[Any, str], Any], *, item_path_prefix: str | None = None
) -> list:
    value = data.get(key)
    field = join(path, key)
    if not isinstance(value, list):
        raise ValidationError(field, "expected a list")
    prefix = item_path_prefix or key
    return [item_fn(item, join(path, f"{prefix}.{index}")) for index, item in enumerate(value)]


def require_timestamp(data: dict, key: str, path: str) -> str:
    """Validate an ISO-8601 timestamp string (UTC `Z` recommended)."""
    value = require_str(data, key, path, max_length=64)
    field = join(path, key)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(field, "expected an ISO-8601 timestamp") from None
    return value


# ---------------------------------------------------------------------------
# colors
# ---------------------------------------------------------------------------

_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


def is_hex_color(value: str) -> bool:
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))


def normalize_hex_color(value: str, path: str) -> str:
    """Accept `#RRGGBB` or `#rrggbb`; return canonical lowercase `#rrggbb`."""
    if not isinstance(value, str):
        raise ValidationError(path, "expected a hex color string")
    candidate = value.strip().lower()
    if not _HEX_COLOR_RE.match(candidate):
        raise ValidationError(path, f"expected #rrggbb hex color, got {value!r}")
    return candidate


def require_hex_color(data: dict, key: str, path: str) -> str:
    return normalize_hex_color(data.get(key), join(path, key))


def optional_hex_color(data: dict, key: str, path: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    return require_hex_color(data, key, path)


def require_hex_color_list(
    data: dict, key: str, path: str, *, min_length: int = 1, max_length: int = 24
) -> list[str]:
    raw = data.get(key)
    field = join(path, key)
    if not isinstance(raw, list):
        raise ValidationError(field, "expected a list of hex colors")
    if not min_length <= len(raw) <= max_length:
        raise ValidationError(field, f"must contain {min_length}..{max_length} colors")
    return [
        normalize_hex_color(item, join(field, str(index)))
        for index, item in enumerate(raw)
    ]


# ---------------------------------------------------------------------------
# dict output helpers
# ---------------------------------------------------------------------------

def enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def drop_none(mapping: dict) -> dict:
    return {key: value for key, value in mapping.items() if value is not None}
