"""Scene Studio routine projection over native Home Assistant automations.

Canonical rule (routines pass): **Home Assistant is the source of truth for
automation definitions**; Scene Studio maintains a DERIVED, read-first
projection of the constrained subset it can safely understand and edit. This
module is the transport-free half of that projection — pure stdlib parsing,
classification, generation, and concurrency digests. HA contact lives behind
the :class:`~scene_studio.service.ports.HaAutomationGateway` port; nothing
here performs I/O and nothing here reads or writes ``automations.yaml``.

Supported routine grammar (routines pass, deliberately narrow):

- exactly one ``time`` trigger (``platform: time``, literal ``at`` with a
  whole-minute time — no templates, no ``sun.*`` triggers);
- no conditions, or exactly one ``time`` condition optionally carrying a
  ``weekday`` selection (the 7 HA weekday strings, ``holiday`` unsupported);
- exactly one action: the canonical Scene Studio execution bridge event
  (``event: scene_studio_ui_command`` with ``event_data.command`` =
  ``scene.apply`` | ``playback.start`` and ``event_data.scene_id``);
- benign automation-level keys only (id/alias/description/mode/
  initial_state/icon) — anything else (``variables``, blueprints, scripts,
  ``choose``/``repeat`` structures, extra event_data keys, ...) moves the
  automation to ``recognized_advanced``: still visible and readable, never
  flattened or rewritten through Scene Studio.

Recognition is STRUCTURAL: a user-created HA automation that fires the same
supported bridge event classifies as a native routine even when it carries no
Scene Studio provenance. Provenance (a versioned marker in the automation
description) is informative only — never the recognition test.

The normalized ``source_digest`` is the concurrency token: callers re-fetch
immediately before every mutation and compare digests, so an automation that
changed in HA since it was loaded is reported as a conflict instead of being
silently overwritten.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .serde import ValidationError, join

__all__ = [
    "ROUTINE_COMMAND_EVENT",
    "ROUTINE_SCHEMA_VERSION",
    "ROUTINE_AUTOMATION_ID_PREFIX",
    "SUPPORTED_BRIDGE_COMMANDS",
    "WEEKDAYS",
    "CLASSIFICATION_NATIVE",
    "CLASSIFICATION_ADVANCED",
    "RoutineProjection",
    "RoutineSchedule",
    "HaAutomationIdError",
    "build_alias",
    "canonical_config_digest",
    "classify_automation",
    "describe_schedule",
    "find_bridge_reference",
    "format_time_12h",
    "generate_routine_config",
    "new_automation_id",
    "parse_provenance",
    "validate_routine_behavior",
    "validate_routine_schedule",
]

# The canonical Scene Studio execution bridge event (same literal as
# ``appdaemon_adapter.ui_bridge.UI_COMMAND_EVENT``, which re-exports this
# constant so the seam name has exactly one definition).
ROUTINE_COMMAND_EVENT = "scene_studio_ui_command"

# Versioned provenance marker schema (embedded in the automation description).
ROUTINE_SCHEMA_VERSION = 1

# Scene Studio-generated HA automation ids carry this prefix. Recognition
# never depends on it (structure decides), but it keeps generated automations
# identifiable in the HA UI and satisfies `^[a-z][a-z0-9_]{0,63}$`.
ROUTINE_AUTOMATION_ID_PREFIX = "ssr_"

# Bridge command -> routine behavior. The values are the canonical
# `behavior` strings of the routine projection.
SUPPORTED_BRIDGE_COMMANDS = {"scene.apply": "apply", "playback.start": "play"}

# HA weekday strings in canonical order.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

CLASSIFICATION_NATIVE = "native_routine"
CLASSIFICATION_ADVANCED = "recognized_advanced"

_WEEKDAY_SET = frozenset(WEEKDAYS)

# Automation-level keys a native routine may carry. Everything else is
# structural complexity this grammar does not model -> advanced. The plural
# list aliases belong to HA's modernized storage era (>= 2024.8); the
# singular keys stay canonical and carrying both at once is ambiguous.
_NATIVE_TOP_LEVEL_KEYS = frozenset({
    "id", "alias", "description", "trigger", "condition", "action",
    "triggers", "conditions", "actions",
    "mode", "initial_state", "icon",
})
_NATIVE_CONDITION_KEYS = frozenset({"condition", "weekday"})
_NATIVE_MODES = frozenset({"single", "restart", "queued", "parallel"})

# Volatile / purely-inspection keys excluded from the concurrency digest.
# Everything else in the raw HA config participates.
_DIGEST_EXCLUDED_KEYS = frozenset({"trace", "source"})

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_PROVENANCE_RE = re.compile(r"Scene Studio routine \(schema (\d+)\)")
_PROVENANCE_KV_RE = re.compile(r"\b(scene_id|behavior)=([A-Za-z0-9_-]+)")
_AUTOMATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class HaAutomationIdError(ValueError):
    """A HA automation/config id does not satisfy the safe id charset."""


@dataclass
class RoutineSchedule:
    """The schedule slice of the supported grammar."""

    time: str                       # canonical "HH:MM" (24h)
    weekdays: tuple[str, ...] | None = None  # None = every day

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "weekdays": list(self.weekdays) if self.weekdays is not None else None,
        }


@dataclass
class RoutineProjection:
    """Normalized Scene Studio view of one HA automation.

    ``classification`` is ``native_routine`` (fully supported grammar) or
    ``recognized_advanced`` (references Scene Studio but exceeds the grammar;
    visible/readable, never rewritten). Automations without any Scene Studio
    bridge reference produce NO projection at all — they are not routines.
    """

    automation_id: str
    alias: str
    classification: str
    enabled: bool = True
    entity_id: str | None = None
    scene_id: str | None = None
    behavior: str | None = None      # "apply" | "play" (None when not extractable)
    schedule: RoutineSchedule | None = None   # None for advanced routines
    provenance: dict | None = None   # {"schema", "scene_id", "behavior"} when present
    source_digest: str = ""
    unsupported_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {
            "automation_id": self.automation_id,
            "entity_id": self.entity_id,
            "alias": self.alias,
            "enabled": self.enabled,
            "classification": self.classification,
            "scene_id": self.scene_id,
            "behavior": self.behavior,
            "schedule": self.schedule.to_dict() if self.schedule is not None else None,
            "provenance": dict(self.provenance) if self.provenance else None,
            "source_digest": self.source_digest,
        }
        if self.unsupported_reasons:
            out["unsupported_reasons"] = list(self.unsupported_reasons)
        return out


# ---------------------------------------------------------------------------
# normalization helpers
# ---------------------------------------------------------------------------

def _canonical_json(config: dict) -> str:
    cleaned = {key: value for key, value in config.items() if key not in _DIGEST_EXCLUDED_KEYS}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def canonical_config_digest(config: dict) -> str:
    """Normalized source/config digest of the raw HA automation config.

    The digest is the optimistic-concurrency token: normalize the source
    config (stable key order, volatile ``trace``/``source`` keys removed)
    and hash it. Callers re-fetch immediately before every mutation and
    compare digests — a mismatch means HA changed underneath the editor.
    """
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()[:16]


def parse_schedule_time(value) -> str | None:
    """Parse a HA ``at:`` value into canonical ``HH:MM``.

    Accepts ``H:MM`` / ``HH:MM`` / ``HH:MM:SS`` literals; seconds must be
    absent or zero (the Scene Studio grammar schedules whole minutes).
    Templates, dates, and ``sun.*`` values return ``None`` (-> advanced).
    """
    if not isinstance(value, str):
        return None
    match = _TIME_RE.match(value.strip())
    if match is None:
        return None
    hour, minute, second = int(match.group(1)), int(match.group(2)), match.group(3)
    if hour > 23 or minute > 59:
        return None
    if second is not None and int(second) != 0:
        return None
    return f"{hour:02d}:{minute:02d}"


def validate_routine_time(value, path: str) -> str:
    """Validate a user-supplied routine time (``HH:MM``) for create/update."""
    if not isinstance(value, str) or not _TIME_RE.match(value.strip()) or len(value.strip()) != 5:
        raise ValidationError(path, "expected a 'HH:MM' 24-hour time (e.g. '19:30')")
    normalized = parse_schedule_time(value)
    if normalized is None:
        raise ValidationError(path, "expected a 'HH:MM' 24-hour time (e.g. '19:30')")
    return normalized


def validate_routine_weekdays(value, path: str) -> tuple[str, ...] | None:
    """Validate a weekday selection; ``None`` = every day."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationError(path, "expected a non-empty list of weekdays (mon..sun) or null")
    if not all(isinstance(item, str) for item in value):
        raise ValidationError(path, "expected weekday strings (mon..sun)")
    normalized = [item.strip().lower() for item in value]
    unknown = [item for item in normalized if item not in _WEEKDAY_SET]
    if unknown:
        raise ValidationError(
            path, f"unknown weekday(s) {', '.join(sorted(set(unknown)))}; supported: {', '.join(WEEKDAYS)}"
        )
    if len(set(normalized)) != len(normalized):
        raise ValidationError(path, "duplicate weekdays are not allowed")
    return tuple(day for day in WEEKDAYS if day in set(normalized))


def validate_routine_behavior(value, path: str) -> str:
    """Validate the routine behavior; ``play`` requires a dynamic scene (caller checks)."""
    if value not in ("apply", "play"):
        raise ValidationError(path, "must be 'apply' or 'play'")
    return value


def validate_automation_id(value) -> str:
    """Validate a HA automation/config id carried through the API surface."""
    if not isinstance(value, str) or not _AUTOMATION_ID_RE.match(value.strip()):
        raise HaAutomationIdError(
            f"automation id {value!r} is not a safe Home Assistant automation id"
        )
    return value.strip()


def new_automation_id(random_hex: str) -> str:
    """Build a stable, unique Scene Studio automation id from 12 hex chars."""
    candidate = f"{ROUTINE_AUTOMATION_ID_PREFIX}{random_hex.lower()}"
    return validate_automation_id(candidate)


# ---------------------------------------------------------------------------
# bridge-reference detection (structural, provenance-independent)
# ---------------------------------------------------------------------------

def _extract_bridge_event(action) -> tuple[str, str] | None:
    """Extract ``(behavior, scene_id)`` from one action dict when it is the
    exact canonical bridge event with a fully supported payload."""
    if not isinstance(action, dict) or action.get("event") != ROUTINE_COMMAND_EVENT:
        return None
    if set(action) - {"event", "event_data"}:
        return None
    data = action.get("event_data")
    if not isinstance(data, dict):
        return None
    command = data.get("command")
    scene_id = data.get("scene_id")
    if not isinstance(command, str) or command not in SUPPORTED_BRIDGE_COMMANDS:
        return None
    if not isinstance(scene_id, str) or not scene_id.strip():
        return None
    if set(data) - {"command", "scene_id"}:
        return None
    return SUPPORTED_BRIDGE_COMMANDS[command], scene_id.strip()


def _loose_bridge_reference(node, depth: int = 0) -> tuple[str, str] | None:
    """Recursive scan for ANY Scene Studio bridge event, however nested or
    malformed the surrounding structure is. Used only to detect that an
    automation references Scene Studio (-> ``recognized_advanced``) and to
    surface the referenced scene id when it is readable."""
    if depth > 8:
        return None
    if isinstance(node, dict):
        if node.get("event") == ROUTINE_COMMAND_EVENT:
            data = node.get("event_data")
            if isinstance(data, dict):
                command = data.get("command")
                scene_id = data.get("scene_id")
                if isinstance(command, str) and command in SUPPORTED_BRIDGE_COMMANDS \
                        and isinstance(scene_id, str) and scene_id.strip():
                    return SUPPORTED_BRIDGE_COMMANDS[command], scene_id.strip()
        for value in node.values():
            found = _loose_bridge_reference(value, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _loose_bridge_reference(item, depth + 1)
            if found is not None:
                return found
    return None


def find_bridge_reference(config: dict) -> tuple[str, str] | None:
    """``(behavior, scene_id)`` when the automation references the Scene
    Studio bridge anywhere in its config, else ``None``. The scan is
    whole-config (not just the ``action`` list) so both storage eras —
    singular and plural list keys — are recognized."""
    return _loose_bridge_reference(config)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
# config-shape normalization (HA storage eras)
# ---------------------------------------------------------------------------

# HA 2024.8 modernized automation config storage: the trigger TYPE key was
# renamed ``platform`` -> ``trigger``, and newer builds also accept the
# plural list keys ``triggers``/``conditions``/``actions``. Scene Studio
# still GENERATES the legacy shape (universally readable), but the
# classifier must recognize BOTH eras — otherwise HA's own normalized
# read-back of our write fails verification. Scene-Studio-meaningful
# strictness (exactly one time trigger, one bridge action) is unchanged.
_NATIVE_TRIGGER_KEYS = frozenset({"platform", "trigger", "at", "id"})
_PLURAL_LIST_KEYS = {"trigger": "triggers", "condition": "conditions", "action": "actions"}


def _resolve_config_lists(config: dict, reasons: list[str]) -> tuple[object, object, object]:
    """Resolve the trigger/condition/action lists across storage eras.

    Accepts the singular keys (legacy and still canonical) or the plural
    aliases (newer HA); carrying BOTH forms at once is ambiguous -> advanced
    (reason recorded, ``None`` returned for that list).
    """
    resolved = []
    for singular, plural in _PLURAL_LIST_KEYS.items():
        has_singular = singular in config
        has_plural = plural in config
        if has_singular and has_plural:
            reasons.append(f"automation carries both {singular!r} and {plural!r}")
            resolved.append(None)
        else:
            resolved.append(config.get(plural) if has_plural else config.get(singular))
    return tuple(resolved)


# ---------------------------------------------------------------------------

def _classify_trigger(trigger, reasons: list[str]) -> str | None:
    """Return the canonical ``HH:MM`` when the trigger is the supported
    single time trigger; otherwise record reasons and return None."""
    if not isinstance(trigger, list) or len(trigger) != 1:
        reasons.append("automation must have exactly one time trigger")
        return None
    first = trigger[0]
    if not isinstance(first, dict):
        reasons.append("trigger is not a mapping")
        return None
    type_value = first.get("platform")
    if type_value is None:
        type_value = first.get("trigger")  # HA >= 2024.8 modernized key
    if type_value != "time":
        reasons.append(f"unsupported trigger type {type_value!r}")
        return None
    unsupported = sorted(set(first) - _NATIVE_TRIGGER_KEYS)
    if unsupported:
        reasons.append(f"trigger carries unsupported key(s): {', '.join(unsupported)}")
        return None
    return parse_schedule_time(first.get("at"))


def _classify_conditions(conditions, reasons: list[str]) -> tuple[str, ...] | None | bool:
    """Classify the condition list.

    Returns ``None`` for "no conditions" / all-clear-with-no-weekday,
    a weekday tuple when a supported weekday selection is present, and
    ``False`` when the conditions exceed the grammar (reason recorded).
    """
    if conditions in (None, []):
        return None
    if not isinstance(conditions, list) or len(conditions) != 1:
        reasons.append("automation conditions exceed the supported grammar")
        return False
    condition = conditions[0]
    if not isinstance(condition, dict) or condition.get("condition") != "time":
        reasons.append("unsupported condition type (only a single time condition is supported)")
        return False
    unsupported = sorted(set(condition) - _NATIVE_CONDITION_KEYS)
    if unsupported:
        reasons.append(f"condition carries unsupported key(s): {', '.join(unsupported)}")
        return False
    weekdays = condition.get("weekday")
    if weekdays is None:
        return None
    if not isinstance(weekdays, list) or not weekdays \
            or not all(isinstance(day, str) for day in weekdays) \
            or not set(weekdays) <= _WEEKDAY_SET:
        reasons.append("condition weekday selection exceeds the supported grammar")
        return False
    return tuple(day for day in WEEKDAYS if day in set(weekdays))


def classify_automation(
    config: dict,
    *,
    automation_id: str,
    entity_id: str | None = None,
    entity_state: str | None = None,
    alias: str | None = None,
) -> RoutineProjection | None:
    """Project one raw HA automation config into the routine projection.

    Returns ``None`` when the automation has no Scene Studio bridge
    reference (an unrelated HA automation — not a routine, ignored). Returns
    a ``native_routine`` projection when the whole config fits the supported
    grammar, or a ``recognized_advanced`` projection when it references
    Scene Studio but exceeds it (kept visible and readable, never rewritten).

    ``enabled`` comes from the automation ENTITY state (``on``/``off``);
    an unknown state (entity missing/unavailable) reads as enabled, which is
    HA's restore default for automations.
    """
    if not isinstance(config, dict):
        return None
    reference = find_bridge_reference(config)
    if reference is None:
        return None
    behavior_ref, scene_id_ref = reference
    digest = canonical_config_digest(config)
    display_alias = alias if isinstance(alias, str) and alias.strip() else (
        config.get("alias") if isinstance(config.get("alias"), str) and config.get("alias").strip()
        else automation_id
    )
    provenance = parse_provenance(config.get("description") if isinstance(config.get("description"), str) else "")
    enabled = entity_state != "off"

    reasons: list[str] = []
    trigger_list, condition_list, action_list = _resolve_config_lists(config, reasons)
    time_value = _classify_trigger(trigger_list, reasons)
    weekdays = _classify_conditions(condition_list, reasons)

    unsupported_top = sorted(set(config) - _NATIVE_TOP_LEVEL_KEYS)
    if unsupported_top:
        reasons.append(f"automation carries unsupported key(s): {', '.join(unsupported_top)}")
    mode = config.get("mode", "single")
    if mode not in _NATIVE_MODES:
        reasons.append(f"unsupported automation mode {mode!r}")
    initial_state = config.get("initial_state")
    if initial_state is not None and not isinstance(initial_state, bool):
        reasons.append("initial_state must be true/false")

    actions = action_list
    exact_behavior = exact_scene_id = None
    if isinstance(actions, list) and len(actions) == 1:
        extracted = _extract_bridge_event(actions[0])
        if extracted is not None:
            exact_behavior, exact_scene_id = extracted
    else:
        if isinstance(actions, list) and actions:
            reasons.append("automation has multiple actions")
        elif isinstance(actions, list):
            reasons.append("automation has no actions")

    native = (
        time_value is not None
        and weekdays is not False
        and not unsupported_top
        and mode in _NATIVE_MODES
        and (initial_state is None or isinstance(initial_state, bool))
        and exact_behavior is not None
        and not reasons
    )
    if native:
        return RoutineProjection(
            automation_id=automation_id,
            alias=display_alias,
            classification=CLASSIFICATION_NATIVE,
            enabled=enabled,
            entity_id=entity_id,
            scene_id=exact_scene_id,
            behavior=exact_behavior,
            schedule=RoutineSchedule(time=time_value, weekdays=weekdays),
            provenance=provenance,
            source_digest=digest,
        )
    # Advanced: keep the bridge reference readable even when the payload
    # shape exceeds the strict extractor (loose scan already found it).
    return RoutineProjection(
        automation_id=automation_id,
        alias=display_alias,
        classification=CLASSIFICATION_ADVANCED,
        enabled=enabled,
        entity_id=entity_id,
        scene_id=scene_id_ref,
        behavior=behavior_ref,
        schedule=None,
        provenance=provenance,
        source_digest=digest,
        unsupported_reasons=reasons or ["automation structure exceeds the supported routine grammar"],
    )


# ---------------------------------------------------------------------------
# generation (Scene Studio -> native HA automation config)
# ---------------------------------------------------------------------------

def format_time_12h(time_hhmm: str) -> str:
    """``"19:30"`` -> ``"7:00 PM"`` (display labels only)."""
    hour, minute = (int(part) for part in time_hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def describe_weekdays(weekdays: tuple[str, ...] | None) -> str:
    """Concise recurrence label: Daily / Weekdays / Weekends / Mondays / Mon, Thu."""
    if weekdays is None:
        return "Daily"
    days = set(weekdays)
    if days == set(WEEKDAYS):
        return "Daily"
    if days == {"mon", "tue", "wed", "thu", "fri"}:
        return "Weekdays"
    if days == {"sat", "sun"}:
        return "Weekends"
    labels = {"mon": "Mondays", "tue": "Tuesdays", "wed": "Wednesdays", "thu": "Thursdays",
              "fri": "Fridays", "sat": "Saturdays", "sun": "Sundays"}
    if len(days) == 1:
        return labels[weekdays[0]]
    return ", ".join(day.capitalize() for day in weekdays)


def describe_schedule(schedule: RoutineSchedule) -> str:
    """``"Weekdays 7:00 PM"``-style human summary."""
    return f"{describe_weekdays(schedule.weekdays)} {format_time_12h(schedule.time)}"


def build_alias(*, scene_name: str, schedule: RoutineSchedule) -> str:
    """Human-readable alias: ``Scene Studio · Evening Glow · Weekdays 7:00 PM``."""
    return f"Scene Studio · {scene_name} · {describe_schedule(schedule)}"


def build_provenance_description(*, scene_id: str, behavior: str) -> str:
    """Versioned Scene Studio provenance marker embedded in the description.

    Informative only — recognition is structural and never depends on it.
    """
    return (
        f"Scene Studio routine (schema {ROUTINE_SCHEMA_VERSION}) · "
        f"scene_id={scene_id} · behavior={behavior}. "
        "Edited safely from the Scene Studio Workbench; structural edits in "
        "Home Assistant move this routine to read-only."
    )


def parse_provenance(description: str | None) -> dict | None:
    """Parse the versioned provenance marker; ``None`` when absent."""
    if not isinstance(description, str):
        return None
    match = _PROVENANCE_RE.search(description)
    if match is None:
        return None
    provenance: dict = {"schema": int(match.group(1))}
    for key, value in _PROVENANCE_KV_RE.findall(description[: match.end() + 120]):
        if key == "scene_id" and "scene_id" not in provenance:
            provenance["scene_id"] = value
        elif key == "behavior" and "behavior" not in provenance:
            provenance["behavior"] = value
    return provenance


def generate_routine_config(
    *,
    automation_id: str,
    scene_id: str,
    scene_name: str,
    behavior: str,
    schedule: RoutineSchedule,
) -> dict:
    """Build the native HA automation config for one supported routine.

    The generated document is an ordinary HA automation: one time trigger,
    an optional weekday time condition, and exactly one action — the
    canonical ``scene_studio_ui_command`` bridge event. No second execution
    path, no Scene Studio-specific HA integration objects.
    """
    validate_automation_id(automation_id)
    validate_routine_behavior(behavior, "behavior")
    trigger_at = f"{schedule.time}:00"
    config: dict = {
        "id": automation_id,
        "alias": build_alias(scene_name=scene_name, schedule=schedule),
        "description": build_provenance_description(scene_id=scene_id, behavior=behavior),
        "mode": "single",
        "trigger": [{"platform": "time", "at": trigger_at}],
    }
    if schedule.weekdays is not None:
        config["condition"] = [{"condition": "time", "weekday": list(schedule.weekdays)}]
    config["action"] = [{
        "event": ROUTINE_COMMAND_EVENT,
        "event_data": {"command": "scene.apply" if behavior == "apply" else "playback.start",
                       "scene_id": scene_id},
    }]
    return config
