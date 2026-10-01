"""Immutable backup planning for v1 scene directories (Workstream A3).

Pure data: describes the copy procedure and fingerprints the current files.
``plan_backup`` never writes, moves, chmods, or contacts any live system —
executing the plan is a separate, explicit operator step (master plan §11
step 5 happens after dry-run review, before any v2 write).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BACKUP_TIMESTAMP_FORMAT = "UTC <yyyyMMdd>T<HHmmss>Z captured once at execution start"


@dataclass
class BackupPlan:
    """Immutable, timestamped snapshot plan for a v1 scene directory."""

    source_dir: str
    destination_root: str
    timestamp_format: str
    files: list[dict[str, Any]] = field(default_factory=list)  # {filename, bytes, sha256}
    procedure: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "source_dir": self.source_dir,
            "destination_root": self.destination_root,
            "timestamp_format": self.timestamp_format,
            "files": [dict(item) for item in self.files],
            "procedure": list(self.procedure),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plan_backup(scene_dir: str | Path, destination_root: str | Path = "./backups/v1_scenes") -> BackupPlan:
    """Build a deterministic backup plan (read-only; hashes current bytes)."""
    directory = Path(scene_dir)
    files: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            data = path.read_bytes()
            files.append({"filename": path.name, "bytes": len(data), "sha256": _sha256(data)})
    procedure = [
        (
            "1. Capture one UTC timestamp at execution start; create "
            "<destination_root>/<timestamp>-v1-custom-scenes/ (fail if it already exists)."
        ),
        "2. Copy each listed file byte-for-byte into the snapshot directory (sorted order).",
        "3. Re-read each copy and verify its sha256 matches this manifest; abort on any mismatch.",
        "4. Write manifest.json (this plan's file list) into the snapshot directory.",
        "5. Set the snapshot directory and all contents read-only to enforce immutability.",
        "6. Never mutate or delete an existing snapshot; later backups use fresh timestamps.",
    ]
    return BackupPlan(
        source_dir=str(directory),
        destination_root=str(destination_root),
        timestamp_format=BACKUP_TIMESTAMP_FORMAT,
        files=files,
        procedure=procedure,
    )
