"""Playback session domain contracts (R5A, plan v2 §1).

A persisted **collection** of playback sessions keyed by stable opaque
``session_id`` plus a derived fixture-ownership index — multiple disjoint
sessions coexist. This module owns validation, deterministic serialization,
bounded stopped-session retention, and the ownership rules; the engine owns
transitions and orchestration.

Session states:

- ``active``   — playing; owns its fixtures.
- ``paused``   — provider-natively paused where supported; owns its fixtures.
- ``held``     — superseded by an EXTERNAL realtime owner (e.g. hyperHDR
  grabbed the fixtures mid-playback); the engine deliberately performs NO
  provider writes in this state (a freeze would fight the external
  stream), owns nothing, and waits. When the external owner surrenders,
  the session becomes ``paused`` (manually resumable — never auto-resumed).
- ``orphaned`` — was active/paused when the engine restarted; the provider
  may still be animating. Owns nothing, but remembers its fixtures so a
  later start/apply that retakes them can supersede the stale record.
- ``stopped``  — terminal; retained for diagnostics (bounded), owns nothing.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from .serde import (
    ValidationError,
    join,
    reject_unknown_keys,
    require_bool,
    require_mapping,
    require_str,
    require_str_list,
)

__all__ = [
    "FixtureExecution",
    "PLAYBACK_STATE_SCHEMA_VERSION",
    "PlaybackSession",
    "PlaybackSessionState",
    "PlaybackState",
    "STOPPED_RETENTION_LIMIT",
    "new_session_id",
]

PLAYBACK_STATE_SCHEMA_VERSION = 2
#: Persistence documents written by the R5A/R5B wave (schema 1) are accepted
#: read-compatibly; the only v2 addition is the ``held`` state and the
#: session ``held_by`` field.
ACCEPTED_STATE_SCHEMA_VERSIONS = (1, 2)
STOPPED_RETENTION_LIMIT = 20

_SESSION_ID_RE = re.compile(r"^sess-[0-9TZ]{15}-[0-9a-f]{12}$")


class PlaybackSessionState:
    """Session lifecycle states (string constants for JSON-friendly use)."""

    ACTIVE = "active"
    PAUSED = "paused"
    HELD = "held"
    ORPHANED = "orphaned"
    STOPPED = "stopped"

    OWNERSHIP_STATES = (ACTIVE, PAUSED)
    ALL = (ACTIVE, PAUSED, HELD, ORPHANED, STOPPED)


#: Provider execution realizations. ``pending`` is the R5A placeholder: the
#: lifecycle is real, provider realization lands in R5B.
EXECUTION_KINDS = (
    "pending",
    "native_dynamic_palette",
    "native_scene",
    "native_effect",
    "native_preset",
    "approximate_static",
)


def new_session_id(now_utc: str) -> str:
    """Collision-resistant stable opaque session id.

    UTC timestamp segment for human ordering + 12 hex chars of CSPRNG
    entropy (not timestamp-only uniqueness).
    """
    compact = re.sub(r"[^0-9TZ]", "", now_utc)[:15]
    return f"sess-{compact}-{secrets.token_hex(6)}"


@dataclass
class FixtureExecution:
    """Per-fixture playback execution record (placeholder-aware)."""

    fixture_id: str
    provider: str
    execution: str  # one of EXECUTION_KINDS
    fidelity: str  # native | equivalent | approximate | unsupported
    ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        out = {
            "fixture_id": self.fixture_id,
            "provider": self.provider,
            "execution": self.execution,
            "fidelity": self.fidelity,
            "ok": self.ok,
        }
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "fixture_execution") -> "FixtureExecution":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"fixture_id", "provider", "execution", "fidelity", "ok", "detail"}, path)
        execution = data.get("execution")
        if execution not in EXECUTION_KINDS:
            allowed = ", ".join(EXECUTION_KINDS)
            raise ValidationError(join(path, "execution"), f"must be one of: {allowed}")
        fidelity = data.get("fidelity")
        if fidelity not in ("native", "equivalent", "approximate", "unsupported"):
            raise ValidationError(join(path, "fidelity"), "must be native/equivalent/approximate/unsupported")
        return cls(
            fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64),
            provider=require_str(data, "provider", join(path, "provider"), max_length=32),
            execution=execution,
            fidelity=fidelity,
            ok=require_bool(data, "ok", path),
            detail=str(data.get("detail", "")),
        )


@dataclass
class PlaybackSession:
    """One playback session: a scene animating over an owned fixture set."""

    session_id: str
    scene_id: str
    scene_name: str
    target_ids: list[str]
    fixture_ids: list[str]
    state: str  # PlaybackSessionState
    started_at: str
    paused_at: str | None = None
    stopped_at: str | None = None
    stop_reason: str | None = None
    preempted_by: str | None = None
    # External owner label while ``held`` (e.g. "hyperHDR instance 'Workstation
    # WLED Instance'"): who grabbed the fixtures, for honest surfacing.
    held_by: str | None = None
    fixture_executions: list[FixtureExecution] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "scene_id": self.scene_id,
            "scene_name": self.scene_name,
            "target_ids": list(self.target_ids),
            "fixture_ids": list(self.fixture_ids),
            "state": self.state,
            "started_at": self.started_at,
            "fixture_executions": [execution.to_dict() for execution in self.fixture_executions],
        }
        for optional in ("paused_at", "stopped_at", "stop_reason", "preempted_by", "held_by"):
            value = getattr(self, optional)
            if value is not None:
                out[optional] = value
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "session") -> "PlaybackSession":
        data = require_mapping(data, path)
        reject_unknown_keys(
            data,
            {
                "session_id", "scene_id", "scene_name", "target_ids", "fixture_ids", "state",
                "started_at", "paused_at", "stopped_at", "stop_reason", "preempted_by", "held_by",
                "fixture_executions",
            },
            path,
        )
        state = data.get("state")
        if state not in PlaybackSessionState.ALL:
            allowed = ", ".join(PlaybackSessionState.ALL)
            raise ValidationError(join(path, "state"), f"must be one of: {allowed}")
        executions = require_list_field(data, "fixture_executions", path, FixtureExecution.from_dict)
        return cls(
            session_id=require_str(data, "session_id", join(path, "session_id"), max_length=64),
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            scene_name=require_str(data, "scene_name", join(path, "scene_name"), max_length=128),
            target_ids=require_str_list(data, "target_ids", path),
            fixture_ids=require_str_list(data, "fixture_ids", path),
            state=state,
            started_at=require_str(data, "started_at", join(path, "started_at"), max_length=64),
            fixture_executions=executions,
            paused_at=data.get("paused_at"),
            stopped_at=data.get("stopped_at"),
            stop_reason=data.get("stop_reason"),
            preempted_by=data.get("preempted_by"),
            held_by=data.get("held_by"),
        )

    @property
    def owns_fixtures(self) -> bool:
        return self.state in PlaybackSessionState.OWNERSHIP_STATES


def require_list_field(data: dict, key: str, path: str, item_from_dict):
    items = data.get(key, [])
    if not isinstance(items, list):
        raise ValidationError(join(path, key), "expected a list")
    return [item_from_dict(item, join(path, f"{key}.{index}")) for index, item in enumerate(items)]


@dataclass
class PlaybackState:
    """Persisted multi-session collection + derived ownership index."""

    sessions: dict[str, PlaybackSession] = field(default_factory=dict)

    # -- ownership index --------------------------------------------------

    @property
    def ownership(self) -> dict[str, str]:
        """Derived fixture_id -> session_id (active/paused sessions only)."""
        index: dict[str, str] = {}
        for session in self.sessions.values():
            if session.owns_fixtures:
                for fixture_id in session.fixture_ids:
                    index[fixture_id] = session.session_id
        return index

    def owning_session_id(self, fixture_id: str) -> str | None:
        return self.ownership.get(fixture_id)

    def owning_sessions_for(self, fixture_ids: list[str]) -> list[PlaybackSession]:
        """Distinct live sessions owning any of the given fixtures."""
        seen: dict[str, PlaybackSession] = {}
        for fixture_id in fixture_ids:
            owner = self.owning_session_id(fixture_id)
            if owner and owner in self.sessions and owner not in seen:
                seen[owner] = self.sessions[owner]
        return list(seen.values())

    # -- mutation ---------------------------------------------------------

    def add(self, session: PlaybackSession) -> None:
        if session.session_id in self.sessions:
            raise ValidationError("playback", f"duplicate session id: {session.session_id}")
        self.sessions[session.session_id] = session
        try:
            self._validate_ownership_consistency()
        except ValidationError:
            del self.sessions[session.session_id]
            raise
        self.trim_stopped()

    def _validate_ownership_consistency(self) -> None:
        """Enforce the one-live-owner-per-fixture invariant.

        Two active/paused sessions claiming the same fixture is a corrupt
        collection and is rejected at construction/mutation time — the
        invariant must not depend on engine behavior alone.
        """
        claimed: dict[str, str] = {}
        for session in self.sessions.values():
            if not session.owns_fixtures:
                continue
            for fixture_id in session.fixture_ids:
                owner = claimed.get(fixture_id)
                if owner is not None and owner != session.session_id:
                    raise ValidationError(
                        "playback.ownership",
                        f"fixture '{fixture_id}' is claimed by both session "
                        f"'{owner}' and session '{session.session_id}'",
                    )
                claimed[fixture_id] = session.session_id

    def get(self, session_id: str) -> PlaybackSession | None:
        return self.sessions.get(session_id)

    def trim_stopped(self, limit: int = STOPPED_RETENTION_LIMIT) -> None:
        stopped = sorted(
            (s for s in self.sessions.values() if s.state == PlaybackSessionState.STOPPED),
            key=lambda s: (s.stopped_at or "", s.session_id),
        )
        excess = len(stopped) - limit
        for session in stopped[:max(0, excess)]:
            self.sessions.pop(session.session_id, None)

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": PLAYBACK_STATE_SCHEMA_VERSION,
            "sessions": {
                session_id: self.sessions[session_id].to_dict()
                for session_id in sorted(self.sessions)
            },
            "ownership": dict(sorted(self.ownership.items())),
        }

    @classmethod
    def from_dict(cls, data: Any, path: str = "playback_state") -> "PlaybackState":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"schema_version", "sessions", "ownership"}, path)
        version = data.get("schema_version")
        if version not in ACCEPTED_STATE_SCHEMA_VERSIONS:
            allowed = " or ".join(str(v) for v in ACCEPTED_STATE_SCHEMA_VERSIONS)
            raise ValidationError(join(path, "schema_version"), f"expected {allowed}, got {version!r}")
        raw_sessions = data.get("sessions", {})
        if not isinstance(raw_sessions, dict):
            raise ValidationError(join(path, "sessions"), "expected an object keyed by session_id")
        sessions: dict[str, PlaybackSession] = {}
        for session_id, session_data in raw_sessions.items():
            session = PlaybackSession.from_dict(session_data, join(path, f"sessions.{session_id}"))
            # Persistence key must equal the session's own identity; divergence
            # would make ownership-index lookups silently wrong.
            if session_id != session.session_id:
                raise ValidationError(
                    join(path, f"sessions.{session_id}"),
                    f"persistence key {session_id!r} does not match session identity {session.session_id!r}",
                )
            sessions[session_id] = session
        state = cls(sessions=sessions)
        state._validate_ownership_consistency()
        # ownership index validation: the recorded index for LIVE owners must
        # match the index derived from the sessions (it is a derived view).
        recorded = data.get("ownership", {})
        if not isinstance(recorded, dict):
            raise ValidationError(join(path, "ownership"), "expected an object")
        live_recorded = {
            fixture_id: session_id
            for fixture_id, session_id in recorded.items()
            if session_id in state.sessions and state.sessions[session_id].owns_fixtures
        }
        if live_recorded != state.ownership:
            raise ValidationError(
                join(path, "ownership"),
                f"recorded ownership index does not match sessions: {live_recorded} != {state.ownership}",
            )
        return state

    def status_view(self) -> dict:
        """Client-facing collection view for ``status().playback``."""
        sessions = sorted(
            self.sessions.values(), key=lambda s: (s.started_at, s.session_id)
        )
        counts = {state: 0 for state in PlaybackSessionState.ALL}
        for session in sessions:
            counts[session.state] += 1
        return {
            "sessions": [session.to_dict() for session in sessions],
            "counts": counts,
            "owned_fixture_count": len(self.ownership),
        }

    # -- serialization: deterministic (sorted session ids/keys) -------------

    def serialize(self) -> str:
        import json

        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def deserialize(cls, text: str) -> "PlaybackState":
        return cls.from_dict(json.loads(text))
