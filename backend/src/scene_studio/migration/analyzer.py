"""v1 scene analysis (Workstream A3, dry-run).

Scope authority is the v1 crosswalk. Verified id-space fact (2026-09-10 live
pull, all 7 scenes): v1 scene ``hue.lights`` keys are Hue CLIP v2 light
resource ids; the crosswalk maps HA entity ids to the *same*
``hue_resource_id`` space; the seeded registry binds fixtures by those same
resource ids (e.g. ``2a2c45a9-...`` = Hue G Strip in all three documents).

The v1 scene saver captured every Hue light on the bridge into each scene
file, but the v1 APPLY path explicitly filtered the saved Hue states to
one crosswalk room before writing them. The crosswalk (8 hue entries +
WLED segments 0-5) is therefore the record of the *effective old apply
scope*, and migration reproduces that effective scope — not the oversized
save snapshot:

    scene key (resource id) -> must be in the crosswalk -> crosswalk entry
    (ha_entity_id) -> registry fixture

Captured lights outside the crosswalk are out of scope and never map into
``fixture_states``, even when the registry knows a fixture for them. The
registry is still consulted, for provenance labeling only: out-of-scope
lights are tagged ``registry_known_out_of_scope`` (a seeded, enabled fixture
binds the resource) or ``not_in_use`` (the bound registry fixture is
disabled), and the converter preserves them as migration provenance — the
original v1 files remain the archive. Anything crosswalk-listed that cannot
be resolved to exactly one registry fixture is reported unresolved, never
guessed.

v1 WLED is a single device ``state`` blob with segments in ``state.seg[]`` by
index; segment ``i`` maps to the registry fixture whose WLED binding includes
segment id ``i`` (only unambiguous when the registry has exactly one WLED
device, because the v1 blob carries no device identity). Segments outside the
crosswalk's WLED scope are not migrated either.

All outputs are deterministic: same input dicts -> same report dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

from scene_studio.domain.capabilities import Capabilities
from scene_studio.domain.fixtures import Fixture, FixtureRegistry
from scene_studio.domain.identities import normalize_name_to_id

from .colors import wled_col_to_hex, xy_to_hex

MAX_PALETTE_COLORS = 24  # domain cap for scene palette

STATUS_MAPPED = "mapped"
STATUS_DISCARDED = "discarded"
STATUS_UNRESOLVED = "unresolved"
STATUS_UNMAPPED = "unmapped"

# Provenance statuses for captured lights OUTSIDE the v1 apply scope
# (absent from the crosswalk). They never map into fixture_states; the
# converter preserves them as migration provenance only.
SCOPE_REGISTRY_KNOWN_OUT_OF_SCOPE = "registry_known_out_of_scope"
SCOPE_NOT_IN_USE = "not_in_use"

_TRAILING_INT_RE = re.compile(r"(\d+)\s*$")


def scene_id_from_filename(filename: str) -> str:
    """Stable scene id: normalized filename stem (e.g. ``twilight.json`` -> ``twilight``)."""
    return normalize_name_to_id(PurePath(filename).stem)


def scene_name_from_filename(filename: str) -> str:
    """Display name using the v1 tooling rule (``friendly_scene_name``): underscores to spaces, title-cased."""
    return PurePath(filename).stem.replace("_", " ").title()


# ---------------------------------------------------------------------------
# registry indexes
# ---------------------------------------------------------------------------


@dataclass
class RegistryIndexes:
    """Deterministic lookup indexes over the seeded fixture registry."""

    fixtures_by_id: dict[str, Fixture]
    hue_by_resource: dict[str, list[Fixture]]
    hue_by_entity: dict[str, list[Fixture]]
    wled_by_segment: dict[int, list[Fixture]]
    wled_device_ids: list[str]

    @property
    def has_single_wled_device(self) -> bool:
        return len(self.wled_device_ids) == 1


def build_registry_indexes(registry: FixtureRegistry) -> RegistryIndexes:
    fixtures_by_id: dict[str, Fixture] = {}
    hue_by_resource: dict[str, list[Fixture]] = {}
    hue_by_entity: dict[str, list[Fixture]] = {}
    wled_by_segment: dict[int, list[Fixture]] = {}
    wled_devices: set[str] = set()
    for fixture in registry.fixtures:
        fixtures_by_id[fixture.id] = fixture
        binding = fixture.binding
        if binding is None:
            continue
        provider = getattr(binding, "provider", None)
        if provider == "hue_v2":
            hue_by_resource.setdefault(binding.resource_id, []).append(fixture)
            if binding.ha_entity_id:
                hue_by_entity.setdefault(binding.ha_entity_id, []).append(fixture)
        elif provider == "wled":
            wled_devices.add(binding.device_id)
            for segment_id in binding.segment_ids:
                wled_by_segment.setdefault(segment_id, []).append(fixture)
    return RegistryIndexes(
        fixtures_by_id=fixtures_by_id,
        hue_by_resource={key: sorted(items, key=lambda f: f.id) for key, items in hue_by_resource.items()},
        hue_by_entity={key: sorted(items, key=lambda f: f.id) for key, items in hue_by_entity.items()},
        wled_by_segment={key: sorted(items, key=lambda f: f.id) for key, items in wled_by_segment.items()},
        wled_device_ids=sorted(wled_devices),
    )


def build_crosswalk_index(crosswalk: dict) -> dict[str, dict]:
    """Index crosswalk hue entries by ``hue_resource_id`` (the v1 scene key space)."""
    index: dict[str, dict] = {}
    for entity_id, entry in sorted((crosswalk.get("hue") or {}).items()):
        resource_id = entry.get("hue_resource_id")
        if isinstance(resource_id, str):
            index[resource_id] = {"ha_entity_id": entity_id, **entry}
    return index


def build_crosswalk_wled_scope(crosswalk: dict) -> set[int] | None:
    """Derive the in-scope WLED segment indices from the crosswalk.

    Crosswalk WLED entries carry no v1 segment id field, so the index is read
    from the trailing integer of ``segment_label`` (e.g. ``"Segment 3"``),
    falling back to the trailing integer of the entry key (e.g.
    ``"light.lg_wled_segment_3"``). Returns ``None`` when the crosswalk has no
    parseable WLED segment identity, which leaves WLED scope unconstrained.
    """
    scope: set[int] = set()
    for entity_id, entry in sorted((crosswalk.get("wled") or {}).items()):
        label = (entry or {}).get("segment_label") if isinstance(entry, dict) else None
        for candidate in (label, entity_id):
            match = _TRAILING_INT_RE.search(candidate) if isinstance(candidate, str) else None
            if match:
                scope.add(int(match.group(1)))
                break
    return scope or None


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


@dataclass
class ResourceMapping:
    """One v1 Hue light resource and what became of it.

    ``scope_status`` is set only for out-of-scope lights (absent from the
    crosswalk, ``status`` discarded): ``registry_known_out_of_scope`` when a
    seeded enabled registry fixture binds the resource (``fixture_id`` carries
    that fixture), ``not_in_use`` when the bound registry fixture is disabled
    (no ``fixture_id``). Out-of-scope lights are provenance only and never map
    into ``fixture_states``.
    """

    resource_id: str
    name: str
    status: str  # mapped | discarded | unresolved
    fixture_id: str | None = None
    ha_entity_id: str | None = None
    reason: str | None = None
    scope_status: str | None = None  # out-of-scope provenance: registry_known_out_of_scope | not_in_use

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "resource_id": self.resource_id,
            "name": self.name,
            "status": self.status,
        }
        if self.fixture_id is not None:
            out["fixture_id"] = self.fixture_id
        if self.ha_entity_id is not None:
            out["ha_entity_id"] = self.ha_entity_id
        if self.reason is not None:
            out["reason"] = self.reason
        if self.scope_status is not None:
            out["scope_status"] = self.scope_status
        return out


@dataclass
class SegmentMapping:
    """One v1 WLED segment and what became of it."""

    segment_index: int
    status: str  # mapped | unmapped
    fixture_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"segment_index": self.segment_index, "status": self.status}
        if self.fixture_id is not None:
            out["fixture_id"] = self.fixture_id
        if self.reason is not None:
            out["reason"] = self.reason
        return out


def _out_of_scope_mapping(
    resource_id: str, name: str, indexes: RegistryIndexes
) -> ResourceMapping:
    """Classify a captured light absent from the crosswalk (out of apply scope).

    The v1 apply path wrote only crosswalk (migration-room) states, so a light the
    crosswalk does not list was never applied by v1 and is out of migration
    scope regardless of what the registry knows. The registry is consulted for
    provenance labeling only: an enabled bound fixture yields
    ``registry_known_out_of_scope`` with its fixture id; a disabled bound
    fixture yields ``not_in_use``. Edge cases with no single unambiguous
    binding stay plain discarded (no ``scope_status``) and are reported, never
    guessed.
    """
    candidates = indexes.hue_by_resource.get(resource_id, [])
    if len(candidates) == 1 and candidates[0].enabled:
        return ResourceMapping(
            resource_id=resource_id,
            name=name,
            status=STATUS_DISCARDED,
            fixture_id=candidates[0].id,
            scope_status=SCOPE_REGISTRY_KNOWN_OUT_OF_SCOPE,
            reason="known to the registry but outside the v1 crosswalk apply scope",
        )
    if len(candidates) == 1:
        return ResourceMapping(
            resource_id=resource_id,
            name=name,
            status=STATUS_DISCARDED,
            scope_status=SCOPE_NOT_IN_USE,
            reason="confirmed not in use; registry fixture disabled",
        )
    if len(candidates) > 1:
        joined = ", ".join(sorted(fixture.id for fixture in candidates))
        return ResourceMapping(
            resource_id=resource_id,
            name=name,
            status=STATUS_DISCARDED,
            reason=(
                "outside the v1 crosswalk apply scope; ambiguous registry bindings "
                f"({joined}); provenance withheld"
            ),
        )
    return ResourceMapping(
        resource_id=resource_id,
        name=name,
        status=STATUS_DISCARDED,
        reason=(
            "resource id absent from the crosswalk and from the registry; never "
            "applied by the v1 crosswalk filter"
        ),
    )


def map_hue_resources(
    hue_lights: dict, crosswalk: dict, indexes: RegistryIndexes
) -> list[ResourceMapping]:
    """Classify every v1 scene light key: mapped / discarded / unresolved.

    Scope authority is the crosswalk: only crosswalk-listed resources can map.
    Everything else the v1 saver captured is out of the effective old apply
    scope and is discarded (with provenance labeling via the registry).
    """
    cw_by_resource = build_crosswalk_index(crosswalk)
    results: list[ResourceMapping] = []
    for key in sorted(hue_lights):
        payload = hue_lights.get(key) or {}
        name = (payload.get("metadata") or {}).get("name") or key
        crosswalk_entry = cw_by_resource.get(key)
        if crosswalk_entry is None:
            results.append(_out_of_scope_mapping(key, name, indexes))
            continue
        entity_id = crosswalk_entry.get("ha_entity_id")
        candidates: dict[str, Fixture] = {}
        for fixture in indexes.hue_by_entity.get(entity_id, []):
            candidates[fixture.id] = fixture
        for fixture in indexes.hue_by_resource.get(key, []):
            candidates[fixture.id] = fixture
        if len(candidates) == 1:
            fixture = next(iter(candidates.values()))
            results.append(
                ResourceMapping(
                    resource_id=key,
                    name=name,
                    status=STATUS_MAPPED,
                    fixture_id=fixture.id,
                    ha_entity_id=entity_id,
                )
            )
        elif len(candidates) == 0:
            results.append(
                ResourceMapping(
                    resource_id=key,
                    name=name,
                    status=STATUS_UNRESOLVED,
                    ha_entity_id=entity_id,
                    reason=(
                        f"crosswalk maps this resource to HA entity {entity_id!r}, "
                        "but no seeded registry fixture binds that entity or resource id"
                    ),
                )
            )
        else:
            joined = ", ".join(sorted(candidates))
            results.append(
                ResourceMapping(
                    resource_id=key,
                    name=name,
                    status=STATUS_UNRESOLVED,
                    ha_entity_id=entity_id,
                    reason=f"ambiguous: multiple registry fixtures match ({joined}); refusing to guess",
                )
            )
    return results


def map_wled_segments(
    wled_state: dict, indexes: RegistryIndexes, crosswalk: dict | None = None
) -> list[SegmentMapping]:
    """Map v1 WLED ``state.seg[]`` entries to per-segment registry fixtures.

    The crosswalk is the scope authority here too: when it yields a parseable
    WLED segment scope (see ``build_crosswalk_wled_scope``), only in-scope
    segments map; out-of-scope segments are reported unmapped, never emitted.
    """
    segments = wled_state.get("seg") or []
    wled_scope = build_crosswalk_wled_scope(crosswalk) if crosswalk is not None else None
    results: list[SegmentMapping] = []
    if not indexes.wled_device_ids:
        for position, _segment in enumerate(segments):
            results.append(
                SegmentMapping(
                    segment_index=_segment_index(_segment, position),
                    status=STATUS_UNMAPPED,
                    reason="registry seeds no WLED fixtures",
                )
            )
        return results
    if not indexes.has_single_wled_device:
        joined = ", ".join(indexes.wled_device_ids)
        for position, _segment in enumerate(segments):
            results.append(
                SegmentMapping(
                    segment_index=_segment_index(_segment, position),
                    status=STATUS_UNMAPPED,
                    reason=(
                        "v1 WLED blob carries no device identity and the registry seeds "
                        f"multiple WLED devices ({joined}); device disambiguation needs discovery"
                    ),
                )
            )
        return results
    for position, segment in enumerate(segments):
        segment_index = _segment_index(segment, position)
        if wled_scope is not None and segment_index not in wled_scope:
            results.append(
                SegmentMapping(
                    segment_index=segment_index,
                    status=STATUS_UNMAPPED,
                    reason=(
                        f"segment {segment_index} is outside the v1 crosswalk apply scope "
                        f"(in-scope segments: {', '.join(str(i) for i in sorted(wled_scope))})"
                    ),
                )
            )
            continue
        candidates = indexes.wled_by_segment.get(segment_index, [])
        if len(candidates) == 1:
            results.append(
                SegmentMapping(
                    segment_index=segment_index,
                    status=STATUS_MAPPED,
                    fixture_id=candidates[0].id,
                )
            )
        elif len(candidates) == 0:
            results.append(
                SegmentMapping(
                    segment_index=segment_index,
                    status=STATUS_UNMAPPED,
                    reason=(
                        f"no registry fixture binds segment {segment_index} on WLED device "
                        f"{indexes.wled_device_ids[0]!r}"
                    ),
                )
            )
        else:
            joined = ", ".join(fixture.id for fixture in candidates)
            results.append(
                SegmentMapping(
                    segment_index=segment_index,
                    status=STATUS_UNMAPPED,
                    reason=f"ambiguous: multiple registry fixtures bind segment {segment_index} ({joined})",
                )
            )
    return results


def _segment_index(segment: dict, position: int) -> int:
    value = (segment or {}).get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return position


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------


@dataclass
class TargetSelection:
    """Result of the documented deterministic target-selection rule.

    ``rule`` names the exact semantic cover strategy (see
    ``TARGET_SELECTION_RULE``): declared targets are chosen only when their
    members stay inside the scene's mapped enabled fixtures; everything else
    falls back to individual fixture ids.
    """

    rule: str
    target_ids: list[str]
    overcovered_fixture_ids: list[str]  # chosen-target members not present in the scene (empty under the exact-cover rule)
    uncovered_fixture_ids: list[str]    # mapped fixtures no declared target covers (emitted as their own fixture ids)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "target_ids": list(self.target_ids),
            "overcovered_fixture_ids": list(self.overcovered_fixture_ids),
            "uncovered_fixture_ids": list(self.uncovered_fixture_ids),
        }


TARGET_SELECTION_RULE = (
    "smallest exact semantic cover of the scene's mapped enabled fixtures: "
    "greedily add declared targets that introduce no over-coverage (target "
    "members beyond the scene's mapped fixtures, disabled excluded), most "
    "newly covered fixtures first, ties by target id; stop when no such "
    "target adds coverage; mapped fixtures still uncovered fall back to "
    "their own fixture id (a fixture id resolves to that single fixture, "
    "contracts §1). Over-coverage minimization ranks above target count: "
    "two tight targets beat one loose covering target"
)


def select_targets(mapped_fixture_ids: list[str], registry: FixtureRegistry) -> TargetSelection:
    """Deterministic target selection (rule documented in TARGET_SELECTION_RULE).

    The goal is the smallest exact semantic cover of the scene's mapped
    enabled fixtures — never merely "a covering target". A single declared
    target that happens to cover every mapped fixture (``whole_house``)
    would land every scene on it while dragging in unrelated rooms'
    fixtures, so only targets whose members stay inside the scene's mapped
    set are eligible; an uncovered remainder is emitted as individual
    fixture ids (fixture ids are valid targets per contracts §1).

    Disabled fixtures are excluded from coverage and over-coverage
    accounting: the apply pipeline skips them regardless (contracts §3), so
    scenes must not target them just to satisfy coverage. Their migrated
    states remain in ``fixture_states`` and the analyzer surfaces them via
    ``fixture_disabled`` concerns.
    """
    disabled = {fixture.id for fixture in registry.fixtures if not fixture.enabled}
    mapped = set(mapped_fixture_ids) - disabled
    if not mapped:
        return TargetSelection(rule=TARGET_SELECTION_RULE, target_ids=[], overcovered_fixture_ids=[], uncovered_fixture_ids=[])
    membership: dict[str, set[str]] = {
        target.id: {fixture.id for fixture in registry.fixtures if target.id in fixture.groups}
        for target in registry.targets
    }
    # Exact targets only: a candidate must reach into the mapped set and must
    # not reach beyond it (disabled members are excluded from the accounting).
    candidates = {
        target_id: members & mapped
        for target_id, members in membership.items()
        if members & mapped and not ((members - mapped) - disabled)
    }

    chosen: list[str] = []
    uncovered = set(mapped)
    while uncovered:
        scored = sorted(
            (
                (-len(members & uncovered), target_id)
                for target_id, members in candidates.items()
                if members & uncovered
            )
        )
        if not scored:
            break
        target_id = scored[0][1]
        chosen.append(target_id)
        uncovered -= candidates[target_id]
    fallback = sorted(uncovered)  # no exact declared target covers these -> own fixture id
    return TargetSelection(
        rule=TARGET_SELECTION_RULE,
        target_ids=sorted(chosen) + fallback,
        overcovered_fixture_ids=sorted(
            # Exact-target eligibility keeps chosen members inside the mapped
            # set; the disabled subtraction keeps the accounting honest if the
            # eligibility ever loosens (always empty under the current rule).
            ({fixture_id for target_id in chosen for fixture_id in membership[target_id]} - mapped) - disabled
        ),
        uncovered_fixture_ids=fallback,
    )


# ---------------------------------------------------------------------------
# palette + fidelity
# ---------------------------------------------------------------------------


@dataclass
class PaletteInference:
    source: str  # gradient:<fixture_id> | wled_segments | fixture_colors | none
    colors: list[str]
    approximate: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "colors": list(self.colors),
            "approximate": self.approximate,
            "note": self.note,
        }


@dataclass
class FidelityConcern:
    code: str
    level: str  # info | warning
    message: str
    fixture_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "fixture_ids": list(self.fixture_ids),
        }


def _gradient_points(payload: dict) -> list[dict]:
    points = ((payload.get("gradient") or {}).get("points")) or []
    return [point for point in points if isinstance(point, dict)]


def _payload_hex_color(payload: dict) -> str | None:
    xy = (payload.get("color") or {}).get("xy") or {}
    if isinstance(xy.get("x"), (int, float)) and isinstance(xy.get("y"), (int, float)):
        return xy_to_hex(xy["x"], xy["y"])
    return None


def _unique_preserve(colors: list[str | None], *, limit: int = MAX_PALETTE_COLORS) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for color in colors:
        if not color or color in seen:
            continue
        seen.add(color)
        out.append(color)
        if len(out) >= limit:
            break
    return out


def _hue_point_colors(points: list[dict]) -> list[str]:
    colors: list[str] = []
    for point in points:
        xy = (point.get("color") or {}).get("xy") or {}
        x, y = xy.get("x"), xy.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            colors.append(xy_to_hex(x, y))
    return colors


def _mapped_fixture_colors(
    hue_lights: dict,
    hue_mappings: list[ResourceMapping],
    wled_state: dict,
    segment_mappings: list[SegmentMapping],
) -> dict[str, list[str]]:
    """Representative hexes per mapped fixture id (unique within the fixture)."""
    colors_by_id: dict[str, list[str]] = {}
    by_resource = {mapping.resource_id: mapping for mapping in hue_mappings}
    for resource_id in sorted(hue_lights):
        mapping = by_resource.get(resource_id)
        if mapping is None or mapping.status != STATUS_MAPPED or not mapping.fixture_id:
            continue
        payload = hue_lights.get(resource_id) or {}
        point_colors = _hue_point_colors(_gradient_points(payload))
        distinct_points = _unique_preserve(point_colors)
        if distinct_points:
            colors_by_id[mapping.fixture_id] = distinct_points
            continue
        solid = _payload_hex_color(payload)
        if solid:
            colors_by_id[mapping.fixture_id] = [solid]
    segments = wled_state.get("seg") or []
    mapped_segments = sorted(
        (mapping for mapping in segment_mappings if mapping.status == STATUS_MAPPED and mapping.fixture_id),
        key=lambda mapping: mapping.segment_index,
    )
    for mapping in mapped_segments:
        if mapping.segment_index >= len(segments):
            continue
        hex_color = wled_col_to_hex((segments[mapping.segment_index] or {}).get("col"))
        if hex_color:
            colors_by_id[mapping.fixture_id] = [hex_color]
    return colors_by_id


def infer_palette(
    hue_lights: dict,
    hue_mappings: list[ResourceMapping],
    wled_state: dict,
    segment_mappings: list[SegmentMapping],
    indexes: RegistryIndexes,
) -> PaletteInference:
    """Reconstruct an ordered palette from v1 data; always labeled approximate.

    Priority: (1) first mapped Hue fixture whose gradient has two or more
    distinct colors — point order is the only ordering v1 preserves;
    (2) unique mapped fixture colors (Hue + WLED) in fixture-id order.
    A uniform gradient (Meeting Blue's G Strip is five copies of one cyan)
    is not the scene's look; apply still uses per-fixture hexes. v1 stores
    per-fixture colors, not palettes, so any reconstruction is approximate.
    """
    by_resource = {mapping.resource_id: mapping for mapping in hue_mappings}
    for resource_id in sorted(hue_lights):
        mapping = by_resource.get(resource_id)
        if mapping is None or mapping.status != STATUS_MAPPED:
            continue
        point_colors = _hue_point_colors(_gradient_points(hue_lights.get(resource_id) or {}))
        if len(_unique_preserve(point_colors)) < 2:
            continue
        return PaletteInference(
            source=f"gradient:{mapping.fixture_id}",
            colors=point_colors[:MAX_PALETTE_COLORS],
            approximate=True,
            note=(
                "reconstructed from the ordered gradient points of the first mapped "
                "gradient fixture; v1 stores per-fixture colors, not palettes"
            ),
        )

    colors_by_id = _mapped_fixture_colors(hue_lights, hue_mappings, wled_state, segment_mappings)
    ordered: list[str] = []
    for fixture_id in sorted(colors_by_id):
        ordered.extend(colors_by_id[fixture_id])
    colors = _unique_preserve(ordered)
    if colors:
        return PaletteInference(
            source="fixture_colors",
            colors=colors,
            approximate=True,
            note="reconstructed from per-fixture colors in fixture-id order; v1 has no palette ordering",
        )
    return PaletteInference(
        source="none",
        colors=[],
        approximate=True,
        note="no mapped color data available; palette left empty",
    )


def fidelity_concerns(
    hue_lights: dict,
    hue_mappings: list[ResourceMapping],
    segment_mappings: list[SegmentMapping],
    indexes: RegistryIndexes,
) -> list[FidelityConcern]:
    concerns: list[FidelityConcern] = []

    disabled: list[str] = []
    degraded: list[str] = []
    xy_color_fixtures: list[str] = []
    gradient_mismatch: list[str] = []
    gradient_overflow: list[str] = []
    empty_state: list[str] = []
    for mapping in hue_mappings:
        if mapping.status != STATUS_MAPPED or mapping.fixture_id is None:
            continue
        fixture = indexes.fixtures_by_id[mapping.fixture_id]
        if not fixture.enabled:
            disabled.append(fixture.id)
        if fixture.capability_assessment is not None and fixture.capability_assessment.status.value == "limited":
            degraded.append(fixture.id)
        payload = hue_lights.get(mapping.resource_id) or {}
        if (payload.get("color") or {}).get("xy"):
            xy_color_fixtures.append(fixture.id)
        points = _gradient_points(payload)
        if points:
            gradient_cap: Capabilities | None = fixture.capabilities
            gradient_capability = gradient_cap.gradient if gradient_cap is not None else None
            if gradient_capability is None:
                gradient_mismatch.append(fixture.id)
            elif gradient_capability.max_points < len(points):
                gradient_overflow.append(fixture.id)
        has_any = any(
            (
                (payload.get("on") or {}).get("on") is not None,
                (payload.get("dimming") or {}).get("brightness") is not None,
                (payload.get("color") or {}).get("xy"),
                points,
            )
        )
        if not has_any:
            empty_state.append(fixture.id)

    if disabled:
        concerns.append(
            FidelityConcern(
                code="fixture_disabled",
                level="warning",
                message=(
                    "scene carries state for fixture(s) disabled in the registry "
                    "(house-move leave-behind); scene apply will skip them"
                ),
                fixture_ids=sorted(disabled),
            )
        )
    if degraded:
        concerns.append(
            FidelityConcern(
                code="fixture_capability_limited",
                level="info",
                message="fixture capability is limited through its current provider; verify expected behavior at activation",
                fixture_ids=sorted(degraded),
            )
        )
    if xy_color_fixtures:
        concerns.append(
            FidelityConcern(
                code="xy_color_conversion_approximate",
                level="info",
                message=(
                    "colors converted from Hue CIE xy to sRGB hex using the v1 tooling "
                    "algorithm; values are approximate, not gamut-exact"
                ),
                fixture_ids=sorted(xy_color_fixtures),
            )
        )
    if gradient_mismatch:
        concerns.append(
            FidelityConcern(
                code="capability_mismatch_gradient",
                level="warning",
                message="v1 gradient state targets fixture(s) without gradient capability; renderer will approximate or drop",
                fixture_ids=sorted(gradient_mismatch),
            )
        )
    if gradient_overflow:
        concerns.append(
            FidelityConcern(
                code="gradient_points_exceed_capability",
                level="warning",
                message="v1 gradient has more points than the fixture capability allows; excess points will be dropped",
                fixture_ids=sorted(gradient_overflow),
            )
        )
    if empty_state:
        concerns.append(
            FidelityConcern(
                code="empty_state_omitted",
                level="warning",
                message="mapped fixture payload carried no on/brightness/color/gradient data; no fixture state emitted",
                fixture_ids=sorted(empty_state),
            )
        )

    quantized = [
        mapping.fixture_id
        for mapping in segment_mappings
        if mapping.status == STATUS_MAPPED
    ]
    if quantized:
        concerns.append(
            FidelityConcern(
                code="wled_brightness_scale",
                level="info",
                message=(
                    "segment brightness converted from WLED 0-255 scale to 0-100 "
                    "(rounded to one decimal)"
                ),
                fixture_ids=sorted(quantized),
            )
        )
    return concerns


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class AnalysisReport:
    """Per-scene dry-run analysis (JSON-serializable, deterministic)."""

    scene_file: str | None
    scene_id: str
    scene_name: str
    hue_resources: list[ResourceMapping]
    wled_segments: list[SegmentMapping]
    mapped_fixture_ids: list[str]
    target_selection: TargetSelection
    palette_inference: PaletteInference
    fidelity_concerns: list[FidelityConcern]
    motion_note: str = (
        "v1 scenes store static per-fixture colors only; migrated motion is "
        "mode=static, strategy=auto with no animation intent inferred"
    )

    @property
    def discarded_lights(self) -> list[ResourceMapping]:
        return [mapping for mapping in self.hue_resources if mapping.status == STATUS_DISCARDED]

    @property
    def unresolved_resources(self) -> list[ResourceMapping]:
        return [mapping for mapping in self.hue_resources if mapping.status == STATUS_UNRESOLVED]

    def to_dict(self) -> dict:
        return {
            "scene_file": self.scene_file,
            "scene_id": self.scene_id,
            "scene_name": self.scene_name,
            "hue_resources": [mapping.to_dict() for mapping in self.hue_resources],
            "wled_segments": [mapping.to_dict() for mapping in self.wled_segments],
            "mapped_fixture_ids": list(self.mapped_fixture_ids),
            "discarded_lights": [mapping.to_dict() for mapping in self.discarded_lights],
            "unresolved_resources": [mapping.to_dict() for mapping in self.unresolved_resources],
            "target_selection": self.target_selection.to_dict(),
            "palette_inference": self.palette_inference.to_dict(),
            "motion_inference": {"mode": "static", "strategy": "auto", "note": self.motion_note},
            "fidelity_concerns": [concern.to_dict() for concern in self.fidelity_concerns],
        }


def analyze_scene(
    v1_scene: dict,
    registry: FixtureRegistry,
    crosswalk: dict,
    filename: str | None = None,
) -> AnalysisReport:
    """Analyze one v1 scene dict against the seeded registry + crosswalk.

    Pure function: no I/O, no mutation, deterministic output.
    """
    hue_lights = (v1_scene.get("hue") or {}).get("lights") or {}
    wled_state = (v1_scene.get("wled") or {}).get("state") or {}
    indexes = build_registry_indexes(registry)

    hue_mappings = map_hue_resources(hue_lights, crosswalk, indexes)
    segment_mappings = map_wled_segments(wled_state, indexes, crosswalk)
    mapped_fixture_ids = sorted(
        {mapping.fixture_id for mapping in hue_mappings if mapping.status == STATUS_MAPPED and mapping.fixture_id}
        | {mapping.fixture_id for mapping in segment_mappings if mapping.status == STATUS_MAPPED and mapping.fixture_id}
    )
    target_selection = select_targets(mapped_fixture_ids, registry)
    palette = infer_palette(hue_lights, hue_mappings, wled_state, segment_mappings, indexes)
    concerns = fidelity_concerns(hue_lights, hue_mappings, segment_mappings, indexes)

    filename = filename or "(unknown).json"
    return AnalysisReport(
        scene_file=filename,
        scene_id=scene_id_from_filename(filename),
        scene_name=scene_name_from_filename(filename),
        hue_resources=hue_mappings,
        wled_segments=segment_mappings,
        mapped_fixture_ids=mapped_fixture_ids,
        target_selection=target_selection,
        palette_inference=palette,
        fidelity_concerns=concerns,
    )
