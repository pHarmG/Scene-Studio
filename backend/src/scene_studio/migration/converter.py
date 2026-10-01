"""Deterministic v1 -> v2 scene conversion (Workstream A3).

Pure function over ``(v1_scene, registry, crosswalk, filename)``. Produces a
validated domain ``Scene`` (schema v2). No I/O, no mutation, no activation —
activation is a later, explicit integration step (master plan §11).

Emission rules (also mirrored into the analysis report):

- identity: scene id = normalized filename stem; display name uses the v1
  ``friendly_scene_name`` rule (underscores to spaces, title-cased);
- scope: the crosswalk is the record of the effective old apply scope (the v1
  apply path filtered saved Hue states to one crosswalk room); only
  crosswalk-listed resources map into ``fixture_states``;
- ``target_ids``: documented deterministic coverage rule (``TARGET_SELECTION_RULE``);
- Hue lights: ``on`` / ``dimming.brightness`` kept, ``color.xy`` converted to
  canonical ``#rrggbb`` (brightness NOT folded into the color; the domain
  stores both separately), ``gradient.points`` converted in order,
  ``color_temperature`` kept when ``mirek_valid``;
- WLED segments: ``seg[i].col[0]`` (0-255 RGB) -> exact hex on
  ``wled_seg_i``-style fixtures resolved through registry bindings; segment
  ``bri`` (0-255) scaled to 0-100 rounded to one decimal;
- ``motion``: static/auto — v1 carries no animation intent;
- ``palette``: first multi-color Hue gradient, else unique fixture colors;
  matching solid hexes become ``palette_index`` pins (static assignment);
- ``metadata.migrated_from_v1``: provenance ``{"filename": ...}`` plus
  ``out_of_scope_lights`` — captured lights outside the apply scope, kept as
  identification only (no color/brightness payloads; the original v1 files
  remain the archive).
"""

from __future__ import annotations

from scene_studio.domain.fixtures import FixtureRegistry
from scene_studio.domain.palette_resolve import canonicalize_static_palette
from scene_studio.domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy, Scene

from .analyzer import (
    STATUS_DISCARDED,
    STATUS_MAPPED,
    RegistryIndexes,
    ResourceMapping,
    analyze_scene,
    build_registry_indexes,
    map_hue_resources,
    map_wled_segments,
    scene_id_from_filename,
    scene_name_from_filename,
    select_targets,
)
from .colors import wled_col_to_hex, xy_to_hex


def _hue_fixture_state(payload: dict) -> FixtureState | None:
    on_value = (payload.get("on") or {}).get("on")
    brightness = (payload.get("dimming") or {}).get("brightness")
    color_xy = (payload.get("color") or {}).get("xy") or {}
    color_temp = payload.get("color_temperature") or {}
    points = ((payload.get("gradient") or {}).get("points")) or []

    color = None
    if isinstance(color_xy.get("x"), (int, float)) and isinstance(color_xy.get("y"), (int, float)):
        color = xy_to_hex(color_xy["x"], color_xy["y"])

    mirek = color_temp.get("mirek") if color_temp.get("mirek_valid") else None
    gradient = None
    if points:
        gradient = [
            xy_to_hex(
                (point.get("color") or {}).get("xy", {}).get("x", 0.0),
                (point.get("color") or {}).get("xy", {}).get("y", 0.0),
            )
            for point in points
            if isinstance(point, dict)
        ]

    state = FixtureState(
        on=on_value if isinstance(on_value, bool) else None,
        brightness=float(brightness) if isinstance(brightness, (int, float)) and not isinstance(brightness, bool) else None,
        color=color,
        color_temp_mirek=int(mirek) if isinstance(mirek, int) and not isinstance(mirek, bool) else None,
        gradient=gradient,
    )
    if state.on is None and state.brightness is None and state.color is None and state.color_temp_mirek is None and state.gradient is None:
        return None
    return state


def _wled_fixture_state(segment: dict) -> FixtureState | None:
    segment = segment or {}
    on_value = segment.get("on")
    brightness_raw = segment.get("bri")
    brightness = None
    if isinstance(brightness_raw, (int, float)) and not isinstance(brightness_raw, bool):
        brightness = round(float(brightness_raw) / 255.0 * 100.0, 1)
    color = wled_col_to_hex(segment.get("col"))
    state = FixtureState(
        on=on_value if isinstance(on_value, bool) else None,
        brightness=brightness,
        color=color,
    )
    if state.on is None and state.brightness is None and state.color is None:
        return None
    return state


def _segment_payloads(wled_state: dict) -> dict[int, dict]:
    """Index v1 ``state.seg[]`` payloads by segment id (fallback: list position)."""
    payloads: dict[int, dict] = {}
    for position, segment in enumerate(wled_state.get("seg") or []):
        segment_id = (segment or {}).get("id")
        key = segment_id if isinstance(segment_id, int) and not isinstance(segment_id, bool) else position
        payloads[key] = segment or {}
    return payloads


def convert_scene(
    v1_scene: dict,
    registry: FixtureRegistry,
    crosswalk: dict,
    filename: str | None = None,
) -> Scene:
    """Convert one v1 scene dict into a validated domain Scene (schema v2).

    ``filename`` is required for stable identity/provenance (scene id derives
    from its stem). Raises ``ValueError`` when the scene maps no fixtures at
    all (a Scene needs at least one target) or when self-validation fails.

    Only crosswalk-listed resources enter ``fixture_states`` (the effective
    old apply scope); everything else the v1 saver captured is preserved as
    ``metadata.migrated_from_v1.out_of_scope_lights`` identification records.
    """
    if not filename:
        raise ValueError("filename is required to derive a stable scene id and provenance")

    hue_lights = (v1_scene.get("hue") or {}).get("lights") or {}
    wled_state = (v1_scene.get("wled") or {}).get("state") or {}
    indexes: RegistryIndexes = build_registry_indexes(registry)

    hue_mappings = map_hue_resources(hue_lights, crosswalk, indexes)
    segment_mappings = map_wled_segments(wled_state, indexes, crosswalk)
    segment_payloads = _segment_payloads(wled_state)

    fixture_states: dict[str, FixtureState] = {}
    for mapping in hue_mappings:
        if mapping.status == STATUS_MAPPED and mapping.fixture_id:
            state = _hue_fixture_state(hue_lights.get(mapping.resource_id) or {})
            if state is not None:
                fixture_states[mapping.fixture_id] = state
    for mapping in segment_mappings:
        if mapping.status == STATUS_MAPPED and mapping.fixture_id:
            state = _wled_fixture_state(segment_payloads.get(mapping.segment_index, {}))
            if state is not None:
                fixture_states[mapping.fixture_id] = state

    if not fixture_states:
        raise ValueError(f"scene {filename!r} maps no fixtures; refusing to emit an empty scene")

    target_selection = select_targets(sorted(fixture_states), registry)
    if not target_selection.target_ids:
        raise ValueError(f"scene {filename!r} has no resolvable targets")

    analysis = analyze_scene(v1_scene, registry, crosswalk, filename=filename)
    scene = Scene(
        schema_version=2,
        id=scene_id_from_filename(filename),
        name=scene_name_from_filename(filename),
        target_ids=target_selection.target_ids,
        palette=list(analysis.palette_inference.colors),
        motion=Motion(mode=MotionMode.STATIC, speed=0.5, strategy=MotionStrategy.AUTO),
        fixture_states=dict(sorted(fixture_states.items())),
        metadata={
            "migrated_from_v1": {
                "filename": filename,
                "out_of_scope_lights": _out_of_scope_provenance(hue_mappings),
            }
        },
    )
    scene = canonicalize_static_palette(scene)
    # Self-validation: the emitted document must survive a strict round-trip.
    Scene.from_dict(scene.to_dict())
    return scene


def _out_of_scope_provenance(hue_mappings: list[ResourceMapping]) -> list[dict]:
    """Identification records for captured lights outside the v1 apply scope.

    Ordered by resource id (mapping input order). Carries identity + status
    only — no captured color/brightness payloads; the original v1 files remain
    the archive.
    """
    return [
        {
            "resource_id": mapping.resource_id,
            "name": mapping.name,
            "fixture_id": mapping.fixture_id,
            "status": mapping.scope_status,
        }
        for mapping in hue_mappings
        if mapping.status == STATUS_DISCARDED and mapping.scope_status is not None
    ]
