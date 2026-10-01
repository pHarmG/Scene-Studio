#!/usr/bin/env python3
"""Scan the repository working tree for credential-shaped strings.

Added after live HA tokens and a Hue application key were found tracked in
the originating monorepo's working tree (see docs/operations/SECURITY.md).
Anything re-pulled from a live system must pass through this scanner before
being committed.

Usage:
    python scripts/security/scan_secrets.py                # scan git-tracked files, exit 1 on findings
    python scripts/security/scan_secrets.py --all          # scan every readable file, ignored or not
    python scripts/security/scan_secrets.py --json         # machine-readable output
    python scripts/security/scan_secrets.py path/to/dir    # scan a subtree

Detectors are shape-based (JWT segments, credential-key assignments) plus a
placeholder allowlist, so the scanner itself never contains a real secret.
By default only git-tracked (and not ignored) files are scanned; `--all`
scans the working tree regardless of ignore status.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "storybook-static",
    ".zcode",
    ".cursor",
}

# Binary / generated file types that never need scanning.
SKIPPED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".wav", ".onnx", ".pyc",
    ".zip", ".tar", ".gz", ".7z", ".db", ".sqlite",
}

MAX_FILE_BYTES = 2_000_000

# JWT-shaped strings: base64url header ".payload" (one staged copy of an HA
# token lost its leading "e", so the header match is deliberately loose).
JWT_RE = re.compile(r"\beyJ?[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b")
GITHUB_CREDENTIAL_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")

# Credential keys carrying long opaque values, e.g. hue_username: "Ab3xY...".
KEYED_SECRET_RE = re.compile(
    r"(?i)\b(hue[_-]?username|hue[_-]?application[_-]?key|app[_-]?key|"
    r"api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passphrase)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})[\"']?\s*$"
)

# Code references that read credentials from config/env rather than embed them.
CODE_REFERENCE_HINTS = (
    "args.get(",
    "os.environ",
    "environ.get(",
    "getattr(",
    "self.args",
    "$env:",
    "env:",
    "secrets.",
)


@dataclass
class Finding:
    path: str
    line: int
    detector: str
    excerpt: str


def is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(hint in lowered for hint in PLACEHOLDER_HINTS)


PLACEHOLDER_HINTS = (
    "redacted",
    "!secret",
    "<",
    "your-",
    "your_",
    "example",
    "changeme",
    "change-me",
    "dummy",
    "placeholder",
    "xxx",
    "test-only",
    "ha-token",
    "token(second",
)


def git_tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
    )
    return [REPO_ROOT / p for p in result.stdout.decode("utf-8").split("\0") if p]


def iter_candidate_files(roots: list[Path], scan_all: bool) -> list[Path]:
    if scan_all:
        candidates: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if root.is_file():
                candidates.append(root)
                continue
            for path in sorted(root.rglob("*")):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue
                if any(part in DEFAULT_EXCLUDED_DIR_NAMES for part in path.parts):
                    continue
                if any(part.startswith(".venv") for part in path.parts):
                    continue
                candidates.append(path)
        return candidates

    allowed = roots
    files = []
    for path in git_tracked_files():
        if not any(path == root or root in path.parents for root in allowed):
            continue
        if path.suffix.lower() in SKIPPED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


# Inline marker to silence the scanner on a known-fake line (like `# nosec`):
#   ... test fixture string ...  # secrets-scan:allow
ALLOW_MARKER = "secrets-scan:allow"


def scan(files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            if any(hint in line for hint in CODE_REFERENCE_HINTS):
                continue
            for match in JWT_RE.finditer(line):
                findings.append(Finding(str(path), lineno, "jwt-shaped", match.group(0)[:24] + "..."))
            for match in GITHUB_CREDENTIAL_RE.finditer(line):
                findings.append(Finding(str(path), lineno, "github-credential", "[redacted]"))
            for match in KEYED_SECRET_RE.finditer(line):
                value = match.group(2)
                if not is_placeholder(value):
                    findings.append(
                        Finding(str(path), lineno, match.group(1).lower(), value[:6] + "...")
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Files or directories to scan (default: repo root)")
    parser.add_argument("--all", action="store_true", help="Scan working tree regardless of git ignore status")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    args = parser.parse_args()

    roots = [Path(p).resolve() for p in args.paths] or [REPO_ROOT]
    findings = scan(iter_candidate_files(roots, scan_all=args.all))

    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    elif findings:
        for f in findings:
            print(f"{f.detector:14} {f.path}:{f.line}  {f.excerpt}")
        print(f"\n{len(findings)} potential secret(s) found.")
    else:
        print("No secret-shaped strings found.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
