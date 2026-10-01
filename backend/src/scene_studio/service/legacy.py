"""Legacy AppDaemon event -> command envelope mappers (plan §9.5).

Pure functions: no engine, no store, no I/O. They resolve old helper/name
semantics into the new stable-ID command layer so the historic entry points
(`apply_scene_event`, `save_light_states_event`, `delete_scene_event`,
`generate_office_scene`) keep working through the single command service.

Legacy name semantics (from the legacy AppDaemon scene tools this pass replaced):

- Scene *names* were the storage key (slugified with ``\\W+ -> _``); v2 keys
  scenes by stable id. Mappers therefore slugify names with
  ``domain.identities.normalize_name_to_id`` — the same charset rules — and
  an unknown name simply becomes an unknown scene id: the mapper returns the
  envelope with the slugified id as given and the ENGINE reports
  ``not_found`` (no registry access here).
- ``delete_scene_event`` moved ``custom_scenes/<slug>.json`` into
  ``custom_scenes_archive/``: v2 ``scene.archive`` is the exact analog
  (archive-to-folder, restore supported), so delete maps to archive.
- ``save_light_states_event`` captured live provider state into a new scene;
  v2 ``scene.save`` persists an EMPTY draft (live provider capture is a
  later wave — documented engine gap). The mapper passes the raw name; the
  engine derives the draft targets (all declared registry targets).
- ``generate_office_scene`` used random.choice/shuffle over a 14-step
  interpolated palette. The v2 mapping is DETERMINISTIC: a fixed 14-sample
  even interpolation of the requested colors, ``g_strip`` receiving the even
  gradient samples, every other mapped fixture a cycled palette entry at the
  default brightness (50), motion static, scene id ``generated_office``.
  The result is represented as ``{"legacy", "scene", "commands": [save,
  apply]}`` and executed IN ORDER by ``SceneStudioEngine.handle_legacy``
  (the leading ``scene.save`` is fulfilled by the engine's ``upsert_scene``
  because the frozen catalog has no full-content save command — reported
  gap; see engine module docstring).
"""

from __future__ import annotations

import json
from typing import Any

from ..domain.identities import normalize_name_to_id
from ..domain.serde import normalize_hex_color

__all__ = [
    "GENERATED_SCENE_ID",
    "LEGACY_EVENT_MAPPERS",
    "map_apply_scene_event",
    "map_delete_scene_event",
    "map_generate_office_scene",
    "map_save_light_states_event",
]

#: Stable id of the auto-generated office scene (generate flow upserts it).
GENERATED_SCENE_ID = "generated_office"


def map_apply_scene_event(data: dict | None) -> dict:
    """``apply_scene_event {scene_name}`` -> ``scene.apply`` envelope."""
    name = str((data or {}).get("scene_name", "scene"))
    return {"command": "scene.apply", "scene_id": normalize_name_to_id(name)}


def map_save_light_states_event(data: dict | None) -> dict:
    """``save_light_states_event {scene_name}`` -> ``scene.save`` envelope.

    v1 captured live provider state; v2 persists an empty draft (documented
    wave-1 gap in the engine). The display name passes through as given.
    """
    name = str((data or {}).get("scene_name", "scene")).strip() or "scene"
    return {"command": "scene.save", "name": name}


def map_delete_scene_event(data: dict | None) -> dict:
    """``delete_scene_event {scene_name}`` -> ``scene.archive`` envelope.

    v1 "delete" archived the scene file into ``custom_scenes_archive``; v2
    keeps that semantic as ``scene.archive`` (recoverable, id-stable).
    """
    name = str((data or {}).get("scene_name", ""))
    return {"command": "scene.archive", "scene_id": normalize_name_to_id(name)}


def map_generate_office_scene(
    data: dict | None,
    *,
    fixture_ids: list[str] | None = None,
    target_id: str = "office",
    gradient_fixture_id: str = "g_strip",
    gradient_points: int = 5,
    brightness: float = 50.0,
) -> dict:
    """``generate_office_scene {colors: [hex...]}`` -> deterministic scene + sequence.

    Returns ``{"legacy": "generate_office_scene", "scene": <scene dict>,
    "commands": [<scene.save envelope>, <scene.apply envelope>]}``. The
    adapter executes the mapping through ``SceneStudioEngine.handle_legacy``
    which upserts the scene document, then applies it.

    Determinism contract: same ``colors`` (and same fixture id list) always
    produce byte-identical scene dicts — no randomness anywhere, unlike the
    v1 app (random.choice/random.shuffle). Raises ``ValueError`` when fewer
    than two valid ``#rrggbb`` colors are supplied (v1 parity: it refused to
    generate from <2 colors too); invalid entries are skipped, mirroring the
    v1 per-entry validation.
    """
    colors = _extract_colors((data or {}).get("colors"))
    if len(colors) < 2:
        raise ValueError("generate_office_scene requires at least two valid #rrggbb colors")

    gradient = _interpolate_gradient(colors, total=14)
    ids = list(fixture_ids) if fixture_ids else [gradient_fixture_id]

    fixture_states: dict[str, dict] = {}
    for fixture_id in ids:
        if fixture_id == gradient_fixture_id:
            fixture_states[fixture_id] = {
                "on": True,
                "brightness": brightness,
                "gradient": _even_samples(gradient, max(2, gradient_points)),
            }
        else:
            fixture_states[fixture_id] = {
                "on": True,
                "brightness": brightness,
                "color": gradient[len(fixture_states) % len(gradient)],
            }

    scene = {
        "schema_version": 2,
        "id": GENERATED_SCENE_ID,
        "name": "Generated Office",
        "target_ids": [target_id],
        "palette": list(gradient),
        "brightness": brightness,
        "motion": {"mode": "static", "speed": 0.0, "strategy": "static"},
        "fixture_states": fixture_states,
        "metadata": {"origin": "generated", "generated_from_colors": len(colors)},
    }
    return {
        "legacy": "generate_office_scene",
        "scene": scene,
        "commands": [
            {"command": "scene.save", "name": scene["name"], "target_id": target_id},
            {"command": "scene.apply", "scene_id": GENERATED_SCENE_ID},
        ],
    }


#: Event name -> mapper for the simple (envelope-only) legacy events.
#: ``generate_office_scene`` needs registry context (fixture ids) and is
#: dispatched separately by the adapter via :func:`map_generate_office_scene`.
LEGACY_EVENT_MAPPERS = {
    "apply_scene_event": map_apply_scene_event,
    "save_light_states_event": map_save_light_states_event,
    "delete_scene_event": map_delete_scene_event,
}


# ---------------------------------------------------------------------------
# deterministic color helpers (no randomness — replaces the v1 shuffler)
# ---------------------------------------------------------------------------


def _extract_colors(payload: Any) -> list[str]:
    """Normalize a legacy color payload into canonical ``#rrggbb`` entries.

    Accepts a list (or a JSON-encoded list string, as the v1 event could
    carry either) of hex strings or ``{"hex": ...}``/``{"color": ...}`` maps.
    Invalid entries are skipped; colors are canonicalized to lowercase.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = [payload]
    if not isinstance(payload, list):
        payload = [payload]

    colors: list[str] = []
    for entry in payload:
        if isinstance(entry, dict):
            entry = entry.get("hex") or entry.get("color")
        if not isinstance(entry, str):
            continue
        candidate = entry if entry.startswith("#") else f"#{entry}"
        try:
            colors.append(normalize_hex_color(candidate, "generate.colors"))
        except ValueError:
            continue
    return colors


def _interpolate_gradient(colors: list[str], total: int) -> list[str]:
    """Evenly sample a multi-stop RGB gradient (endpoints included)."""
    stops = [_rgb_of(color) for color in colors]
    if total < 2:
        total = 2
    samples: list[str] = []
    last = len(stops) - 1
    for index in range(total):
        position = (index / (total - 1)) * last
        lower = int(position)
        upper = min(lower + 1, last)
        ratio = position - lower
        blended = tuple(
            round(stops[lower][channel] + (stops[upper][channel] - stops[lower][channel]) * ratio)
            for channel in range(3)
        )
        samples.append("#" + "".join(f"{value:02x}" for value in blended))
    return samples


def _even_samples(gradient: list[str], count: int) -> list[str]:
    """Pick ``count`` evenly spaced samples from the gradient (no repeats)."""
    count = max(2, min(count, len(gradient)))
    indexes = {round(index * (len(gradient) - 1) / (count - 1)) for index in range(count)}
    return [gradient[index] for index in sorted(indexes)]


def _rgb_of(hex_color: str) -> tuple[int, int, int]:
    return (int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16))
