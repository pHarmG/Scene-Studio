"""Installed build identity. Packaged JSON wins; checkouts use root VERSION."""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from functools import lru_cache

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")


def version_key(version: str) -> tuple:
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError("Invalid semantic version")
    major, minor, patch, pre = match.groups()
    identifiers = []
    for part in (pre or "").split("."):
        if pre and (not part or (part.isdigit() and len(part) > 1 and part.startswith("0"))):
            raise ValueError("Invalid semantic version prerelease")
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return (int(major), int(minor), int(patch), pre is None, tuple(identifiers))


def checkout_build(root: Path, channel: str = "local", tag: str | None = None) -> dict:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    version_key(version)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        sha = git("rev-parse", "HEAD")
        dirty = bool(git("status", "--porcelain"))
        digest = hashlib.sha256()
        for relative in sorted(git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()):
            path = root / relative
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
            digest.update(b"\0")
        source_digest = digest.hexdigest()
    except (OSError, subprocess.CalledProcessError):
        sha, dirty, source_digest = None, True, None
    if channel not in ("local", "release"):
        raise ValueError("Build channel must be local or release")
    if channel == "release":
        if tag != "v" + version:
            raise ValueError("Release tag must match root VERSION")
        if dirty or not sha:
            raise ValueError("Release builds require a clean source checkout")
        if git("rev-parse", tag + "^{commit}") != sha:
            raise ValueError("Release tag must identify HEAD")
        if SEMVER.fullmatch(version).group(4):
            raise ValueError("Stable release builds require a stable VERSION")
    return {"version": version, "update_protocol": 1, "source_sha": sha, "short_sha": sha[:7] if sha else None,
            "source_tree_sha256": source_digest,
            "channel": channel, "dirty": dirty, "tag": tag if channel == "release" else None,
            "built_at": datetime.now(timezone.utc).isoformat()}


@lru_cache(maxsize=1)
def get_build_info() -> dict:
    bundled = Path(__file__).with_name("build-info.json")
    if bundled.is_file():
        return json.loads(bundled.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[3]
    if (root / "VERSION").is_file():
        return checkout_build(root)
    # Incomplete source copies must never claim an official version/build.
    return {"version": None, "source_sha": None, "short_sha": None, "channel": "unknown", "dirty": None, "built_at": None, "tag": None, "source_tree_sha256": None}
