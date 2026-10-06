"""Command envelope and result contracts (master plan §6.6, §9).

The API exposes a single validated `POST command` endpoint carrying a compact
envelope. Reserved envelope keys: `command`, `request_id`; everything else is
command-specific params, validated against the catalog below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar

from .contention import CONTENTION_POLICIES, parse_contention_override
from .routines import (
    validate_routine_behavior,
    validate_routine_time,
    validate_routine_weekdays,
)
from .serde import (
    ValidationError,
    join,
    optional_float,
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_mapping,
    require_str,
)


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    UNKNOWN_COMMAND = "unknown_command"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    INTERNAL_ERROR = "internal_error"
    CONTENDED = "contended"  # every requested fixture is held by an external light-sync owner


# ---------------------------------------------------------------------------
# command payload schemas
# ---------------------------------------------------------------------------

@dataclass
class ApplySceneParams:
    scene_id: str
    target_id: str | None = None
    dry_run: bool = False          # true -> return render plan only
    transition_ms: int | None = None
    # One-shot external-sync override ("takeover" = suspend the overlapping
    # hyperHDR instance(s) for this action instead of yielding). None = the
    # registry policies / engine default apply.
    contention_override: str | None = None

    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id", "target_id", "dry_run", "transition_ms", "contention_override"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "ApplySceneParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            target_id=optional_str(data, "target_id", path, max_length=64),
            dry_run=require_bool(data, "dry_run", path) if "dry_run" in data else False,
            transition_ms=optional_int(data, "transition_ms", path, minimum=0, maximum=60000),
            contention_override=parse_contention_override(data.get("contention_override"), path),
        )


@dataclass
class SceneIdParams:
    scene_id: str
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "SceneIdParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64))


@dataclass
class RenameSceneParams:
    scene_id: str
    name: str
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id", "name"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "RenameSceneParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            name=require_str(data, "name", join(path, "name"), max_length=128),
        )


@dataclass
class SaveSceneParams:
    """Capture current provider state as a new scene draft (v1 parity)."""

    name: str
    target_id: str | None = None
    ALLOWED_KEYS: ClassVar[set[str]] = {"name", "target_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "SaveSceneParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            name=require_str(data, "name", join(path, "name"), max_length=128),
            target_id=optional_str(data, "target_id", path, max_length=64),
        )


@dataclass
class PreviewDraftParams:
    """Validate/render an unsaved Scene v2 authoring draft (Builder Pass 2).

    Purely observational: no persistence, no provider contact, and the
    draft's `scene.id` may be absent/empty (the engine derives the
    candidate id from `scene.name` before Scene validation).
    """

    scene: dict
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "PreviewDraftParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(scene=require_mapping(data.get("scene"), join(path, "scene")))


class PlayDraftParams(PreviewDraftParams):
    """Play an unsaved Scene v2 draft on fixtures without persisting it.

    Same payload as ``scene.preview_draft``. Static drafts take the apply
    executor path; dynamic drafts start playback against an in-memory Scene
    so pause/stop can still realize providers. The catalog is not mutated.
    """


@dataclass
class CreateSceneParams:
    """Atomically persist a new canonical Scene v2 document (Builder Pass 2).

    `scene.id` may be absent/empty (derived from `scene.name` server-side);
    collisions against active AND archived scene ids are conflicts.

    `duplicate_of` is the Builder's Duplicate/Save-as-New seam: the client
    says which scene it copied, and the SERVER records that provenance
    (`metadata.duplicated_from`) on the new document. The payload cannot
    claim provenance itself — server/history metadata keys are stripped from
    create payloads (a brand-new scene is neither archived, nor v1-migrated,
    nor a duplicate unless the engine itself derives that).
    """

    scene: dict
    duplicate_of: str | None = None
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene", "duplicate_of"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "CreateSceneParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            scene=require_mapping(data.get("scene"), join(path, "scene")),
            duplicate_of=optional_str(data, "duplicate_of", path, max_length=64),
        )


@dataclass
class UpdateSceneParams:
    """Atomically replace the editable intent of an existing active scene.

    `scene_id` is authoritative and immutable; the replacement `scene`
    document must carry the same id.
    """

    scene_id: str
    scene: dict
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id", "scene"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "UpdateSceneParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            scene=require_mapping(data.get("scene"), join(path, "scene")),
        )


@dataclass
class PlaybackParams:
    scene_id: str
    target_id: str | None = None
    contention_override: str | None = None
    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id", "target_id", "contention_override"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "PlaybackParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            target_id=optional_str(data, "target_id", path, max_length=64),
            contention_override=parse_contention_override(data.get("contention_override"), path),
        )


@dataclass
class PlaybackSessionParams:
    """Session-addressed playback control (pause/resume/stop).

    ``contention_override`` applies to ``playback.resume`` only: resuming a
    session whose fixtures are HELD by an external owner is refused unless
    the caller explicitly takes over.
    """

    session_id: str
    contention_override: str | None = None

    ALLOWED_KEYS: ClassVar[set[str]] = {"session_id", "contention_override"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "PlaybackSessionParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            session_id=require_str(data, "session_id", join(path, "session_id"), max_length=64),
            contention_override=parse_contention_override(data.get("contention_override"), path),
        )


@dataclass
class SyncSuspendParams:
    """Explicitly suspend the external light-sync owner (hyperHDR) for a
    scope of fixtures — the standalone half of the takeover flow (the
    apply/playback override suspends implicitly). Records a handback
    snapshot that ``sync.resume`` consumes."""

    target_id: str | None = None
    fixture_id: str | None = None
    ALLOWED_KEYS: ClassVar[set[str]] = {"target_id", "fixture_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "SyncSuspendParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            target_id=optional_str(data, "target_id", path, max_length=64),
            fixture_id=optional_str(data, "fixture_id", path, max_length=64),
        )


@dataclass
class SyncResumeParams:
    """Restart the hyperHDR instance(s) recorded by the last suspend
    (implicit via takeover, or explicit ``sync.suspend``). By default the
    current Scene Studio look is re-asserted on the affected fixtures
    first, so the lights show the scene until sync grabs again."""

    reassert_scene: bool = True
    ALLOWED_KEYS: ClassVar[set[str]] = {"reassert_scene"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "SyncResumeParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            reassert_scene=require_bool(data, "reassert_scene", path) if "reassert_scene" in data else True,
        )


@dataclass
class SetContentionPolicyParams:
    """Durable per-fixture external-sync policy (registry mutation).

    ``default`` clears the fixture override (inherit target/engine).
    """

    fixture_id: str
    policy: str  # yield | takeover | ignore | default

    ALLOWED_KEYS: ClassVar[set[str]] = {"fixture_id", "policy"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "SetContentionPolicyParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        policy = data.get("policy")
        if policy not in (*CONTENTION_POLICIES, "default"):
            allowed = ", ".join((*CONTENTION_POLICIES, "default"))
            raise ValidationError(join(path, "policy"), f"must be one of: {allowed}")
        return cls(
            fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64),
            policy=policy,
        )


@dataclass
class FixtureIdParams:
    fixture_id: str
    ALLOWED_KEYS: ClassVar[set[str]] = {"fixture_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "FixtureIdParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64))


@dataclass
class RebindFixtureParams:
    """Explicit rebind decision: never triggered automatically."""

    fixture_id: str
    observation_id: str
    ALLOWED_KEYS: ClassVar[set[str]] = {"fixture_id", "observation_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "RebindFixtureParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64),
            observation_id=require_str(data, "observation_id", join(path, "observation_id"), max_length=300),
        )


class RebindRollbackParams(FixtureIdParams):
    """Explicit restoration of the latest bounded binding revision."""


@dataclass
class EmptyParams:
    """Commands with no parameters (registry migration preview/apply)."""

    ALLOWED_KEYS: ClassVar[set[str]] = set()

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "EmptyParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls()


class ReconcileFixtureParams(RebindFixtureParams):
    """Same-binding registry knowledge update; observation must match the current binding."""


@dataclass
class AdoptFixtureParams:
    """First-run bootstrap: create a canonical fixture from a discovery observation.

    The server derives the binding, capabilities, device profile, capability
    assessment, and safe endpoint hint from the selected observation. The
    client supplies only stable identity (`fixture_id`), a display `name`,
    initial target membership (`groups`), and `enabled` — never provider
    credentials or free-form binding payloads.
    """

    observation_id: str
    fixture_id: str
    name: str
    groups: list[str] = field(default_factory=list)
    enabled: bool = True

    ALLOWED_KEYS: ClassVar[set[str]] = {"observation_id", "fixture_id", "name", "groups", "enabled"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "AdoptFixtureParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        groups = data.get("groups", [])
        if not isinstance(groups, list) or not all(isinstance(item, str) for item in groups):
            raise ValidationError(join(path, "groups"), "expected a list of target/group ids")
        return cls(
            observation_id=require_str(data, "observation_id", join(path, "observation_id"), max_length=300),
            fixture_id=require_str(data, "fixture_id", join(path, "fixture_id"), max_length=64),
            name=require_str(data, "name", join(path, "name"), max_length=128),
            groups=list(groups),
            enabled=require_bool(data, "enabled", path) if "enabled" in data else True,
        )


@dataclass
class CreateTargetParams:
    """First-run bootstrap: declare a semantic target (room/group).

    `target_id` may be absent (derived from `name` server-side through
    ``normalize_name_to_id`` — the one canonical identity rule). Membership
    is assigned on the authoritative side (`fixture.groups`) for every id in
    `fixture_ids`; no second membership model is created.
    """

    name: str
    target_id: str | None = None
    description: str | None = None
    fixture_ids: list[str] = field(default_factory=list)

    ALLOWED_KEYS: ClassVar[set[str]] = {"name", "target_id", "description", "fixture_ids"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "CreateTargetParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        fixture_ids = data.get("fixture_ids", [])
        if not isinstance(fixture_ids, list) or not all(isinstance(item, str) for item in fixture_ids):
            raise ValidationError(join(path, "fixture_ids"), "expected a list of fixture ids")
        return cls(
            name=require_str(data, "name", join(path, "name"), max_length=128),
            target_id=optional_str(data, "target_id", path, max_length=64),
            description=optional_str(data, "description", path, max_length=512),
            fixture_ids=list(fixture_ids),
        )


@dataclass
class UpdateTargetParams:
    """Narrow target administration: rename and/or a membership delta.

    Membership deltas mutate `fixture.groups` for the listed fixtures only;
    arbitrary registry JSON replacement stays impossible by construction.
    """

    target_id: str
    name: str | None = None
    add_fixture_ids: list[str] = field(default_factory=list)
    remove_fixture_ids: list[str] = field(default_factory=list)

    ALLOWED_KEYS: ClassVar[set[str]] = {"target_id", "name", "add_fixture_ids", "remove_fixture_ids"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "UpdateTargetParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        add_ids = data.get("add_fixture_ids", [])
        remove_ids = data.get("remove_fixture_ids", [])
        for key, value in (("add_fixture_ids", add_ids), ("remove_fixture_ids", remove_ids)):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValidationError(join(path, key), "expected a list of fixture ids")
        return cls(
            target_id=require_str(data, "target_id", join(path, "target_id"), max_length=64),
            name=optional_str(data, "name", path, max_length=128),
            add_fixture_ids=list(add_ids),
            remove_fixture_ids=list(remove_ids),
        )


@dataclass
class DiscoveryRunParams:
    providers: list[str] | None = None  # None/empty = all configured
    ALLOWED_KEYS: ClassVar[set[str]] = {"providers"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "DiscoveryRunParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        raw = data.get("providers")
        if raw is None:
            return cls(providers=None)
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValidationError(join(path, "providers"), "expected a list of provider names")
        return cls(providers=list(raw))


@dataclass
class ExportDiagnosticsParams:
    redact: bool = True  # sanitization is mandatory in production; kept explicit
    recent_events: int = 100
    ALLOWED_KEYS: ClassVar[set[str]] = {"redact", "recent_events"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "ExportDiagnosticsParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            redact=require_bool(data, "redact", path) if "redact" in data else True,
            recent_events=require_int(data, "recent_events", path, minimum=1, maximum=1000),
        )


@dataclass
class RoutineCreateParams:
    """Create one native HA automation over the supported routine grammar.

    ``time`` is ``HH:MM`` (24h); ``weekdays`` absent/None = every day, else
    a subset of HA weekday strings (mon..sun). ``behavior`` is ``apply`` or
    ``play`` — ``play`` is refused for static scenes (they cannot start
    playback). HA generates a fresh stable automation id; the alias and the
    versioned provenance description are derived server-side.
    """

    scene_id: str
    behavior: str  # apply | play
    time: str      # HH:MM 24h
    weekdays: list[str] | None = None

    ALLOWED_KEYS: ClassVar[set[str]] = {"scene_id", "behavior", "time", "weekdays"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "RoutineCreateParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        weekdays = data.get("weekdays")
        if weekdays is not None and (not isinstance(weekdays, list) or not all(isinstance(item, str) for item in weekdays)):
            raise ValidationError(join(path, "weekdays"), "expected a list of weekday strings (mon..sun) or null")
        return cls(
            scene_id=require_str(data, "scene_id", join(path, "scene_id"), max_length=64),
            behavior=validate_routine_behavior(
                require_str(data, "behavior", join(path, "behavior"), max_length=16),
                join(path, "behavior"),
            ),
            time=validate_routine_time(
                require_str(data, "time", join(path, "time"), max_length=5),
                join(path, "time"),
            ),
            weekdays=weekdays,
        )


@dataclass
class RoutineUpdateParams:
    """Update one native routine with optimistic concurrency.

    ``source_digest`` is the automation's ``source_digest`` from the last
    routine read; a mismatch with HA's current config is a structured
    conflict — never an overwrite. Omitted fields keep their current value;
    ``weekdays: null`` explicitly resets to every day.
    """

    automation_id: str
    source_digest: str
    time: str | None = None
    weekdays: list[str] | None = ...
    behavior: str | None = None
    scene_id: str | None = None

    ALLOWED_KEYS: ClassVar[set[str]] = {"automation_id", "source_digest", "time", "weekdays", "behavior", "scene_id"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "RoutineUpdateParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        weekdays = data.get("weekdays", ...)
        if weekdays is not ... and weekdays is not None \
                and (not isinstance(weekdays, list) or not all(isinstance(item, str) for item in weekdays)):
            raise ValidationError(join(path, "weekdays"), "expected a list of weekday strings (mon..sun) or null")
        time_value = optional_str(data, "time", path, max_length=5)
        if time_value is not None:
            time_value = validate_routine_time(time_value, join(path, "time"))
        behavior = optional_str(data, "behavior", path, max_length=16)
        if behavior is not None:
            behavior = validate_routine_behavior(behavior, join(path, "behavior"))
        return cls(
            automation_id=require_str(data, "automation_id", join(path, "automation_id"), max_length=64),
            source_digest=require_str(data, "source_digest", join(path, "source_digest"), max_length=64),
            time=time_value,
            weekdays=list(weekdays) if weekdays is not ... and weekdays is not None else weekdays,
            behavior=behavior,
            scene_id=optional_str(data, "scene_id", path, max_length=64),
        )


@dataclass
class RoutineAddressedParams:
    """Automation-addressed routine mutation carrying the editor's
    concurrency token (``source_digest``)."""

    automation_id: str
    source_digest: str

    ALLOWED_KEYS: ClassVar[set[str]] = {"automation_id", "source_digest"}

    @classmethod
    def from_dict(cls, data: dict, path: str = "params") -> "RoutineAddressedParams":
        data = require_mapping(data, path)
        reject_unknown_keys(data, cls.ALLOWED_KEYS, path)
        return cls(
            automation_id=require_str(data, "automation_id", join(path, "automation_id"), max_length=64),
            source_digest=require_str(data, "source_digest", join(path, "source_digest"), max_length=64),
        )


class RoutineDeleteParams(RoutineAddressedParams):
    """Delete one native routine (native_routine classification only)."""


class RoutineEnableParams(RoutineAddressedParams):
    """Enable one native routine."""


class RoutineDisableParams(RoutineAddressedParams):
    """Disable one native routine."""


# ---------------------------------------------------------------------------
# catalog: command name -> payload parser
# ---------------------------------------------------------------------------

COMMAND_CATALOG: dict[str, Any] = {
    "scene.apply": ApplySceneParams,
    "scene.rename": RenameSceneParams,
    "scene.archive": SceneIdParams,
    "scene.restore": SceneIdParams,
    "scene.save": SaveSceneParams,
    "scene.preview": ApplySceneParams,  # dry-run apply, never persists state change
    "scene.preview_draft": PreviewDraftParams,  # Builder: unsaved-draft render, never persists
    "scene.play_draft": PlayDraftParams,  # Builder Preview: play unsaved draft, never persists
    "scene.create": CreateSceneParams,  # Builder: persist a new Scene v2 document
    "scene.update": UpdateSceneParams,  # Builder: atomic whole-document replacement
    "playback.start": PlaybackParams,
    "playback.pause": PlaybackSessionParams,
    "playback.resume": PlaybackSessionParams,
    "playback.stop": PlaybackSessionParams,
    "fixture.enable": FixtureIdParams,
    "fixture.disable": FixtureIdParams,
    "fixture.set_contention_policy": SetContentionPolicyParams,
    "sync.suspend": SyncSuspendParams,
    "sync.resume": SyncResumeParams,
    "fixture.retry": FixtureIdParams,
    "fixture.identify": FixtureIdParams,
    "fixture.rebind": RebindFixtureParams,
    "fixture.rebind_preview": RebindFixtureParams,
    "fixture.rebind_rollback": RebindRollbackParams,
    "fixture.reconcile": ReconcileFixtureParams,
    "fixture.reconcile_preview": ReconcileFixtureParams,
    "fixture.adopt": AdoptFixtureParams,  # first-run bootstrap: create fixture from an observation
    "target.create": CreateTargetParams,  # first-run bootstrap: declare a semantic target
    "target.update": UpdateTargetParams,  # narrow target rename/membership administration
    "registry.migration_preview": EmptyParams,
    "registry.migrate": EmptyParams,
    "discovery.run": DiscoveryRunParams,
    "diagnostics.export": ExportDiagnosticsParams,
    "routine.create": RoutineCreateParams,  # native HA automation over the supported grammar
    "routine.update": RoutineUpdateParams,  # concurrency-checked native routine edit
    "routine.delete": RoutineDeleteParams,  # remove a native routine from HA
    "routine.enable": RoutineEnableParams,
    "routine.disable": RoutineDisableParams,
}

RESERVED_ENVELOPE_KEYS = {"command", "request_id"}


@dataclass
class CommandEnvelope:
    command: str
    params: dict = field(default_factory=dict)
    request_id: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"command": self.command}
        if self.request_id:
            out["request_id"] = self.request_id
        out.update(self.params)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "command") -> "CommandEnvelope":
        data = require_mapping(data, path)
        name = require_str(data, "command", join(path, "command"), max_length=64)
        if name not in COMMAND_CATALOG:
            known = ", ".join(sorted(COMMAND_CATALOG))
            raise ValidationError(join(path, "command"), f"unknown command {name!r}; known commands: {known}")
        params = {key: value for key, value in data.items() if key not in RESERVED_ENVELOPE_KEYS}
        request_id = optional_str(data, "request_id", path, max_length=64)
        return cls(command=name, params=params, request_id=request_id)


def parse_command(data: Any, path: str = "command") -> tuple[CommandEnvelope, Any]:
    """Parse and validate an envelope + typed params against the catalog."""
    envelope = CommandEnvelope.from_dict(data, path)
    param_cls = COMMAND_CATALOG[envelope.command]
    typed_params = param_cls.from_dict(envelope.params, f"{path}.params")
    return envelope, typed_params


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

@dataclass
class CommandError:
    code: ErrorCode
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"code": self.code.value, "message": self.message}
        if self.details:
            out["details"] = dict(self.details)
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "error") -> "CommandError":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"code", "message", "details"}, path)
        raw_code = data.get("code")
        try:
            code = ErrorCode(raw_code)
        except ValueError:
            allowed = ", ".join(code.value for code in ErrorCode)
            raise ValidationError(join(path, "code"), f"must be one of: {allowed}") from None
        details = data.get("details", {})
        if not isinstance(details, dict):
            raise ValidationError(join(path, "details"), "expected an object")
        return cls(code=code, message=require_str(data, "message", join(path, "message"), max_length=1024), details=dict(details))


@dataclass
class CommandResult:
    """Result envelope returned by the command service / HTTP API."""

    command: str
    ok: bool
    request_id: str | None = None
    data: dict = field(default_factory=dict)
    error: CommandError | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"command": self.command, "ok": self.ok}
        if self.request_id:
            out["request_id"] = self.request_id
        if self.data:
            out["data"] = self.data
        if self.error is not None:
            out["error"] = self.error.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Any, path: str = "result") -> "CommandResult":
        data = require_mapping(data, path)
        reject_unknown_keys(data, {"command", "ok", "request_id", "data", "error"}, path)
        ok = require_bool(data, "ok", path)
        error = None
        if data.get("error") is not None:
            error = CommandError.from_dict(data["error"], join(path, "error"))
        result_data = data.get("data", {})
        if not isinstance(result_data, dict):
            raise ValidationError(join(path, "data"), "expected an object")
        return cls(
            command=require_str(data, "command", join(path, "command"), max_length=64),
            ok=ok,
            request_id=optional_str(data, "request_id", path, max_length=64),
            data=dict(result_data),
            error=error,
        )


def success(command: str, data: dict | None = None, request_id: str | None = None) -> CommandResult:
    return CommandResult(command=command, ok=True, request_id=request_id, data=data or {})


def failure(command: str, code: ErrorCode, message: str, details: dict | None = None, request_id: str | None = None) -> CommandResult:
    return CommandResult(
        command=command,
        ok=False,
        request_id=request_id,
        error=CommandError(code=code, message=message, details=details or {}),
    )


def validation_error_result(command: str, exc: ValidationError, request_id: str | None = None) -> CommandResult:
    return failure(
        command,
        ErrorCode.VALIDATION_ERROR,
        str(exc),
        details={"path": exc.path, "message": exc.message},
        request_id=request_id,
    )
