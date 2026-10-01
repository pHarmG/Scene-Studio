"""Secret sanitization for diagnostics exports (Phase 0 standing contract).

Anything the engine serializes for download, sharing, or persistence outside
the trusted runtime must pass through `sanitize_tree`. Detectors are
shape-based and key-based; sanitized output is covered by unit tests and the
repo scanner (`scripts/security/scan_secrets.py`).
"""

from __future__ import annotations

import re
from typing import Any

JWT_RE = re.compile(r"\beyJ?[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|password|passwd|secret|api[_-]?key|apikey|authorization|"
    r"credential|hue[_-]?username|application[_-]?key|bearer|cookie)"
)

REDACTED = "[REDACTED]"
REDACTED_JWT = "[REDACTED-JWT]"


def contains_secret_shape(value: str) -> bool:
    return bool(JWT_RE.search(value))


def sanitize_string(value: str) -> str:
    return JWT_RE.sub(REDACTED_JWT, value)


def sanitize_tree(obj: Any) -> Any:
    """Recursively redact secret-shaped strings and sensitive-keyed values.

    - Dict values whose key looks credential-bearing -> `[REDACTED]`.
    - Any string containing a JWT shape -> JWT occurrences replaced.
    - Lists/tuples preserved; everything else returned as-is.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str) and not isinstance(value, (dict, list)):
                out[key_str] = REDACTED
            else:
                out[key_str] = sanitize_tree(value)
        return out
    if isinstance(obj, (list, tuple)):
        sanitized = [sanitize_tree(item) for item in obj]
        return sanitized if isinstance(obj, list) else type(obj)(sanitized)  # type: ignore[return-value]
    if isinstance(obj, str):
        return sanitize_string(obj)
    return obj
