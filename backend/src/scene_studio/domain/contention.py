"""External light-sync contention domain (hyperHDR ownership pass).

Pure contracts + helpers for the "hyperHDR frequently controls the same
lights" problem (R5 plan §3.3 seam, finally implemented). The engine never
fights an external realtime owner: while hyperHDR is actively streaming it
holds the contended fixtures, Scene Studio yields them (partial apply with
an explicit receipt), and the hold persists until hyperHDR surrenders on
its own (its input goes idle, e.g. TV off).

Signals, per provider (asymmetric by design):

- **WLED**: authoritative from the device itself — the root ``lor``
  (live override) member of ``GET /json/state``. ``lor != 0`` means a
  realtime source currently owns the device's main segment rendering, so
  every fixture bound to that device is held. hyperHDR is not consulted
  for WLED contention at all.
- **Hue (``hue_v2``)**: authoritative from the BRIDGE, per light —
  ``GET /clip/v2/resource/entertainment_configuration``. hyperHDR streams
  to Hue over the Entertainment API (DTLS) against one named configuration
  (e.g. an Entertainment Area covering only the TV-adjacent bias-lighting
  fixtures); the bridge reports that configuration's own ``status`` as
  ``"active"`` for exactly as long as it is receiving that stream, and its
  ``channels`` list exactly which lights are wired into it (resolved via
  ``GET /clip/v2/resource/entertainment`` service->device, then
  ``GET /clip/v2/resource/device`` device->light). Only THOSE lights are
  held — every other ``hue_v2`` fixture participates in scenes normally,
  even while hyperHDR is actively streaming to its own configuration.
  (Superseded design note: an earlier pass used hyperHDR's own
  ``serverinfo`` "an input is active" flag and held EVERY ``hue_v2``
  fixture whenever any hue-mapped instance was running+streaming — that
  server-wide signal says nothing about which specific lights are on the
  wire, so it silently yielded fixtures hyperHDR was never touching. The
  ``serverinfo`` probe is still used for ``status().contention.streaming``
  display and for WLED instance identification, but no longer decides
  which Hue fixtures are held.)

When the entertainment-configuration read is unavailable or empty the Hue
side fails OPEN (no hold) and the unavailability is surfaced in the
contention status view — a broken read must not make scenes permanently
inapplicable, and WLED truth (``lor``) stays independent of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .fidelity import FixtureRenderPlan, RenderPlan  # no cycle: fidelity imports serde only
from .serde import ValidationError, join

if TYPE_CHECKING:  # annotations only — avoids a fixtures <-> contention import cycle
    from .fixtures import Fixture, FixtureRegistry, Target

__all__ = [
    "CONTENTION_DEFAULT_POLICY",
    "CONTENTION_POLICIES",
    "CONTENTION_OVERRIDE_VALUES",
    "FixtureContention",
    "HyperHdrInstance",
    "HyperHdrView",
    "hue_active_entertainment_light_ids",
    "parse_contention_override",
    "parse_contention_policy",
    "map_fixture_contention",
    "resolve_contention_policy",
    "split_plan_by_contention",
    "wled_held_device_ids",
]

#: Engine-wide default when neither the fixture nor any of its targets
#: carries an explicit ``contention_policy`` (user decision: hyperHDR wins;
#: manual scenes yield).
CONTENTION_DEFAULT_POLICY = "yield"

#: Durable per-fixture/per-target registry policies. ``takeover`` means
#: "stop the overlapping hyperHDR instance instead of yielding" — a
#: standing instruction, applied at every decision (the ONE-SHOT command
#: override is a separate envelope parameter, not a policy value).
CONTENTION_POLICIES = ("yield", "takeover", "ignore")

#: One-shot envelope ``contention_override`` values (scene.apply /
#: playback.start / playback.resume). ``takeover`` = suspend the
#: overlapping hyperHDR instance(s) for this action only; the next
#: external grab wins again.
CONTENTION_OVERRIDE_VALUES = ("takeover",)

#: Priority components that count as "hyperHDR is actively rendering an
#: input" when ``active`` and ``visible`` (idle grabbers sit at low
#: priority with ``active: false`` — observed live on v21).
_STREAMING_COMPONENTS = frozenset({
    "FLATBUFSERVER", "PROTOCOLBUFFER", "VIDEOGRABBER", "SYSTEMGRABBER",
    "EFFECT", "COLOR", "BOBLIGHTSERVER",
})


def parse_contention_policy(value: Any, path: str) -> str | None:
    """Validate a stored ``contention_policy`` (None/absent = inherit)."""
    if value is None:
        return None
    if value in CONTENTION_POLICIES:
        return value
    allowed = ", ".join(CONTENTION_POLICIES)
    raise ValidationError(join(path, "contention_policy"), f"must be one of: {allowed}")


def parse_contention_override(value: Any, path: str) -> str | None:
    """Validate the one-shot envelope ``contention_override`` parameter."""
    if value is None:
        return None
    if value in CONTENTION_OVERRIDE_VALUES:
        return value
    allowed = ", ".join(CONTENTION_OVERRIDE_VALUES)
    raise ValidationError(join(path, "contention_override"), f"must be one of: {allowed}")


# ---------------------------------------------------------------------------
# hyperHDR probe view (pure parse of a serverinfo ``info`` payload)
# ---------------------------------------------------------------------------


@dataclass
class HyperHdrInstance:
    """One hyperHDR LED instance from ``serverinfo.info.instance``."""

    instance: int
    name: str
    running: bool

    def to_dict(self) -> dict:
        return {"instance": self.instance, "name": self.name, "running": self.running}


@dataclass
class HyperHdrView:
    """Normalized snapshot of the hyperHDR server (probe result)."""

    available: bool
    checked_at: str
    detail: str = ""
    instances: list[HyperHdrInstance] = field(default_factory=list)
    streaming: bool = False
    streaming_sources: list[str] = field(default_factory=list)

    def running_instances_named(self, *needles: str) -> list[HyperHdrInstance]:
        """Running instances whose friendly name case-folds any needle."""
        lowered = tuple(needle.casefold() for needle in needles)
        return [
            entry for entry in self.instances
            if entry.running and any(needle in entry.name.casefold() for needle in lowered)
        ]

    def instance(self, instance_id: int) -> HyperHdrInstance | None:
        return next((entry for entry in self.instances if entry.instance == instance_id), None)

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "checked_at": self.checked_at,
            "detail": self.detail,
            "instances": [entry.to_dict() for entry in self.instances],
            "streaming": self.streaming,
            "streaming_sources": list(self.streaming_sources),
        }

    @classmethod
    def unavailable(cls, checked_at: str, detail: str) -> "HyperHdrView":
        return cls(available=False, checked_at=checked_at, detail=detail)

    @classmethod
    def from_serverinfo(cls, info: Any, checked_at: str) -> "HyperHdrView":
        """Parse a ``serverinfo`` ``info`` payload. Never raises — a
        malformed payload degrades to an unavailable view with the reason
        in ``detail`` (the probe port hands back raw provider data)."""
        if not isinstance(info, dict):
            return cls.unavailable(checked_at, "serverinfo payload was not an object")
        raw_instances = info.get("instance")
        instances: list[HyperHdrInstance] = []
        if isinstance(raw_instances, list):
            for entry in raw_instances:
                if not isinstance(entry, dict):
                    continue
                raw_id = entry.get("instance")
                if not isinstance(raw_id, int) or isinstance(raw_id, bool):
                    continue
                instances.append(
                    HyperHdrInstance(
                        instance=raw_id,
                        name=str(entry.get("friendly_name") or f"Instance {raw_id}"),
                        running=bool(entry.get("running")),
                    )
                )
        if not instances:
            return cls.unavailable(checked_at, "serverinfo carried no instance list")
        priorities = info.get("priorities")
        sources: list[str] = []
        streaming = False
        if isinstance(priorities, list):
            for priority in priorities:
                if not isinstance(priority, dict):
                    continue
                if not (priority.get("active") is True and priority.get("visible") is True):
                    continue
                component = str(priority.get("componentId") or "")
                if component not in _STREAMING_COMPONENTS:
                    continue
                streaming = True
                origin = str(priority.get("origin") or "")
                sources.append(f"{component}@{priority.get('priority')} {origin}".strip())
        return cls(
            available=True,
            checked_at=checked_at,
            instances=instances,
            streaming=streaming,
            streaming_sources=sorted(sources),
        )


# ---------------------------------------------------------------------------
# fixture mapping + policy resolution
# ---------------------------------------------------------------------------


@dataclass
class FixtureContention:
    """Per-fixture contention view (derived, never persisted)."""

    fixture_id: str
    provider: str
    held: bool
    owner: str | None = None

    def to_dict(self) -> dict:
        out = {"fixture_id": self.fixture_id, "provider": self.provider, "held": self.held}
        if self.owner:
            out["owner"] = self.owner
        return out


def wled_held_device_ids(wled_states: Any) -> set[str]:
    """WLED device ids currently under a realtime owner (``lor != 0``).

    ``wled_states`` maps device_id -> fetched ``/json/state`` payload
    (root carries ``lor``/``mainseg``). A malformed payload or absent
    ``lor`` simply doesn't hold anything (fail-open; WLED truth only).
    """
    held: set[str] = set()
    if not isinstance(wled_states, dict):
        return held
    for device_id, state in wled_states.items():
        if not isinstance(state, dict):
            continue
        lor = state.get("lor")
        if isinstance(lor, int) and not isinstance(lor, bool) and lor != 0:
            held.add(str(device_id))
    return held


def _data_list(payload: Any) -> list:
    """``{"data": [...]}`` -> the list, or ``[]`` for anything malformed."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return data
    return []


def hue_active_entertainment_light_ids(
    configurations: Any,
    services: Any,
    devices: Any,
) -> set[str]:
    """Light resource ids currently wired into an ACTIVE Hue Entertainment
    Configuration — the precise per-light Hue signal (mirrors WLED's
    per-device ``lor``).

    ``configurations``: ``GET /clip/v2/resource/entertainment_configuration``
    ``services``: ``GET /clip/v2/resource/entertainment`` (service -> owning device)
    ``devices``: ``GET /clip/v2/resource/device`` (device -> its light service)

    Only a configuration whose own ``status`` is ``"active"`` contributes —
    that is the bridge's own record of "hyperHDR (or anything else) is
    streaming to this configuration's lights right now", independent of
    hyperHDR's server-wide "an input is active" flag. Any malformed or
    missing payload contributes nothing (fail-open, same philosophy as the
    WLED/hyperHDR probe paths) rather than falling back to holding
    everything.
    """
    service_owner: dict[str, str] = {}
    for service in _data_list(services):
        if not isinstance(service, dict):
            continue
        service_id = service.get("id")
        owner = service.get("owner")
        owner_id = owner.get("rid") if isinstance(owner, dict) else None
        if isinstance(service_id, str) and isinstance(owner_id, str):
            service_owner[service_id] = owner_id

    device_light: dict[str, str] = {}
    for device in _data_list(devices):
        if not isinstance(device, dict):
            continue
        device_id = device.get("id")
        if not isinstance(device_id, str):
            continue
        for device_service in device.get("services") or []:
            if (
                isinstance(device_service, dict)
                and device_service.get("rtype") == "light"
                and isinstance(device_service.get("rid"), str)
            ):
                device_light[device_id] = device_service["rid"]
                break

    held: set[str] = set()
    for config in _data_list(configurations):
        if not isinstance(config, dict) or config.get("status") != "active":
            continue
        for channel in config.get("channels") or []:
            if not isinstance(channel, dict):
                continue
            for member in channel.get("members") or []:
                service = member.get("service") if isinstance(member, dict) else None
                service_id = service.get("rid") if isinstance(service, dict) else None
                if not isinstance(service_id, str):
                    continue
                device_id = service_owner.get(service_id)
                light_id = device_light.get(device_id) if device_id else None
                if light_id:
                    held.add(light_id)
    return held


def map_fixture_contention(
    registry: FixtureRegistry,
    view: HyperHdrView | None,
    *,
    held_wled_device_ids: set[str] | frozenset[str] = frozenset(),
    held_hue_light_resource_ids: set[str] | frozenset[str] = frozenset(),
    wled_instance_ids: list[int] | None = None,
    hue_instance_ids: list[int] | None = None,
) -> dict[str, FixtureContention]:
    """Compose the per-fixture contention view (see module docstring).

    ``held_hue_light_resource_ids`` is the precise per-light Hue hold set
    (:func:`hue_active_entertainment_light_ids`) — a ``hue_v2`` fixture is
    held only when its OWN bound light resource is a member, never by
    virtue of "some hue_v2 fixture somewhere is held". ``wled_instance_ids``
    / ``hue_instance_ids`` pin hyperHDR instance ids to providers explicitly
    (apps.yaml, used only to label the owner and for the WLED instance
    match); auto-matching by friendly name (``wled`` / ``hue`` needle) is
    the fallback.
    """
    view = view or HyperHdrView.unavailable("", "no probe result yet")

    hue_instances = (
        [entry for entry in (view.instance(i) for i in hue_instance_ids) if entry]
        if hue_instance_ids is not None
        else view.running_instances_named("hue")
    )
    hue_owner = "hyperHDR sync active"
    if hue_instances:
        names = ", ".join(f"'{entry.name}'" for entry in hue_instances)
        sources = ", ".join(view.streaming_sources) or "active input"
        hue_owner = f"hyperHDR instance {names}: {sources}"

    out: dict[str, FixtureContention] = {}
    for fixture in registry.fixtures:
        provider = getattr(fixture.binding, "provider", None) if fixture.binding else None
        if provider == "wled":
            device_id = str(getattr(fixture.binding, "device_id", ""))
            if device_id in held_wled_device_ids:
                out[fixture.id] = FixtureContention(
                    fixture.id, provider, True,
                    f"WLED live override active (device {device_id}, lor != 0)",
                )
            else:
                out[fixture.id] = FixtureContention(fixture.id, provider, False)
        elif provider == "hue_v2":
            resource_id = getattr(fixture.binding, "resource_id", None)
            held = bool(resource_id) and resource_id in held_hue_light_resource_ids
            out[fixture.id] = FixtureContention(fixture.id, provider, held, hue_owner if held else None)
        else:
            out[fixture.id] = FixtureContention(fixture.id, provider or "unknown", False)
    return out


def resolve_contention_policy(
    fixture: Fixture,
    targets: list[Target],
    *,
    engine_default: str = CONTENTION_DEFAULT_POLICY,
) -> str:
    """Effective policy: fixture > its targets (deterministic id order) >
    engine default."""
    if fixture.contention_policy is not None:
        return fixture.contention_policy
    groups = set(fixture.groups)
    for target in sorted(targets, key=lambda item: item.id):
        if target.id in groups and target.contention_policy is not None:
            return target.contention_policy
    return engine_default


def split_plan_by_contention(
    plan: RenderPlan,
    held_fixture_ids: set[str] | frozenset[str],
    owners: dict[str, str],
    *,
    takeover_fixture_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[RenderPlan, list[str], dict[str, str]]:
    """Partition a plan into (executable_plan, yielded_ids, owners_by_id).

    Fixtures that are held AND not taken over are removed from the
    executable plan and reported as yielded; takeover fixtures stay in the
    plan (the caller is responsible for having suspended hyperHDR first).
    The executable plan keeps the original scene/target identity and grows
    an honest note per yield.
    """
    yielded: list[str] = []
    yielded_owners: dict[str, str] = {}
    executable: list[FixtureRenderPlan] = []
    for fixture_plan in plan.fixture_plans:
        if fixture_plan.fixture_id in held_fixture_ids and fixture_plan.fixture_id not in takeover_fixture_ids:
            yielded.append(fixture_plan.fixture_id)
            yielded_owners[fixture_plan.fixture_id] = owners.get(fixture_plan.fixture_id, "external sync owner")
            continue
        executable.append(fixture_plan)
    if not yielded:
        return plan, [], {}
    filtered = RenderPlan(
        scene_id=plan.scene_id,
        target_ids=list(plan.target_ids),
        fixture_plans=executable,
        skipped_fixture_ids=list(plan.skipped_fixture_ids),
        notes=list(plan.notes)
        + [
            f"yielded fixture '{fixture_id}' to {yielded_owners[fixture_id]}"
            for fixture_id in sorted(yielded)
        ],
    )
    return filtered, yielded, yielded_owners
