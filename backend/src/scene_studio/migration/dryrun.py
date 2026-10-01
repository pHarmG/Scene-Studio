"""Dry-run migration report writer (Workstream A3, deterministic).

Produces two review artifacts for all v1 scenes in a directory:

- ``<stem>.json`` — machine-readable report (analysis + converted Scene v2
  candidate per scene), sorted keys, stable ordering;
- ``<stem>.md``   — human-readable summary table + per-scene sections.

The report header states the migration scope rule: the v1 apply path filtered
saved Hue states to the crosswalk's migration room, so migration reproduces
that effective scope and preserves out-of-scope captured lights as provenance only.

Determinism contract (§5): same input bytes + same ``generated_at`` ->
identical output bytes. ``generated_at`` defaults to ``None`` and must be
injected explicitly when a wall-clock stamp is wanted. The report passes
through ``sanitize_tree`` before writing (Phase 0 standing contract).

Usage::

    python -m scene_studio.migration.dryrun            # regenerate committed artifacts
    python -m scene_studio.migration.dryrun --help     # override paths
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scene_studio.domain.fixtures import FixtureRegistry
from scene_studio.domain.sanitize import sanitize_tree

from .analyzer import (
    AnalysisReport,
    analyze_scene,
    build_registry_indexes,
)
from .backup import plan_backup
from .converter import convert_scene

TOOL_NAME = "scene_studio.migration"
TOOL_VERSION = "1"

_REPO_ROOT = Path(__file__).resolve().parents[5]
# Legacy v1 scene sources live outside the shareable tree; override via --scene-dir.
DEFAULT_SCENE_DIR = _REPO_ROOT / "fixtures" / "recorded" / "v1_scenes"
DEFAULT_REGISTRY = _REPO_ROOT / "services" / "scene_studio" / "fixtures" / "registry.sample.json"
DEFAULT_CROSSWALK = _REPO_ROOT / "fixtures" / "recorded" / "crosswalk.json"
DEFAULT_OUT_DIR = _REPO_ROOT / "services" / "scene_studio" / "reports"
DEFAULT_STEM = "migration_dryrun_20260910"
DEFAULT_GENERATED_AT = "2026-09-10T00:00:00Z"


def load_v1_scenes(scene_dir: str | Path) -> list[tuple[str, dict]]:
    """Load every ``*.json`` scene in ``scene_dir`` (sorted by filename)."""
    directory = Path(scene_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"scene directory not found: {directory}")
    scenes: list[tuple[str, dict]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        scenes.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    return scenes


def build_report(
    scene_dir: str | Path,
    registry_path: str | Path,
    crosswalk_path: str | Path,
    generated_at: str | None = None,
) -> dict:
    """Analyze + convert all v1 scenes into the machine-readable report dict."""
    registry = FixtureRegistry.from_dict(json.loads(Path(registry_path).read_text(encoding="utf-8")))
    crosswalk = json.loads(Path(crosswalk_path).read_text(encoding="utf-8"))
    indexes = build_registry_indexes(registry)

    per_scene: list[dict[str, Any]] = []
    for filename, v1_scene in load_v1_scenes(scene_dir):
        analysis: AnalysisReport = analyze_scene(v1_scene, registry, crosswalk, filename=filename)
        scene = convert_scene(v1_scene, registry, crosswalk, filename=filename)
        per_scene.append(
            {
                "filename": filename,
                "analysis": analysis.to_dict(),
                "scene": scene.to_dict(),
            }
        )
    per_scene.sort(key=lambda item: item["filename"])

    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated_at": generated_at,
        "inputs": {
            "scene_dir": str(Path(scene_dir)),
            "registry": str(Path(registry_path)),
            "crosswalk": str(Path(crosswalk_path)),
        },
        "registry_summary": {
            "fixture_count": len(registry.fixtures),
            "target_count": len(registry.targets),
            "wled_device_ids": list(indexes.wled_device_ids),
        },
        "crosswalk_summary": {
            "hue_entries": len(crosswalk.get("hue") or {}),
            "wled_entries": len(crosswalk.get("wled") or {}),
        },
        "scene_count": len(per_scene),
        "scenes": per_scene,
    }


def render_markdown(report: dict) -> str:
    """Render the human-readable companion document (deterministic)."""
    lines: list[str] = []
    generated_at = report.get("generated_at")
    lines.append("# Scene Studio v1 -> v2 Migration Dry Run")
    lines.append("")
    lines.append(f"Generated at: {generated_at if generated_at else 'not recorded (deterministic mode)'}")
    lines.append("")
    inputs = report["inputs"]
    lines.append("Inputs:")
    lines.append("")
    lines.append(f"- Scene dir: `{inputs['scene_dir']}`")
    lines.append(f"- Registry: `{inputs['registry']}` ({report['registry_summary']['fixture_count']} fixtures, "
                 f"{report['registry_summary']['target_count']} targets)")
    lines.append(f"- Crosswalk: `{inputs['crosswalk']}` ({report['crosswalk_summary']['hue_entries']} hue entries, "
                 f"{report['crosswalk_summary']['wled_entries']} wled entries)")
    lines.append("")
    lines.append(f"{report['scene_count']} scenes analyzed; converted v2 candidates are candidates only — "
                 "nothing here is activated (master plan §11).")
    lines.append("")
    lines.append("Scope rule: the v1 scene saver captured every Hue light on the bridge, but the v1 apply")
    lines.append("path filtered saved Hue states to the crosswalk's migration room before writing them. This")
    lines.append("migration reproduces that effective apply scope: only crosswalk-listed resources map into")
    lines.append("scene states. Lights captured outside the crosswalk are not migrated — they are preserved")
    lines.append("as provenance only (`metadata.migrated_from_v1.out_of_scope_lights`); the original v1")
    lines.append("files remain the archive.")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Scene file | v2 id | Mapped fixtures | Discarded lights | Unresolved | Targets | Palette source |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entry in report["scenes"]:
        analysis = entry["analysis"]
        lines.append(
            "| {file} | `{sid}` | {mapped} | {discarded} | {unresolved} | {targets} | {palette} |".format(
                file=entry["filename"],
                sid=analysis["scene_id"],
                mapped=len(analysis["mapped_fixture_ids"]),
                discarded=len(analysis["discarded_lights"]),
                unresolved=len(analysis["unresolved_resources"]),
                targets=", ".join(f"`{t}`" for t in analysis["target_selection"]["target_ids"]),
                palette=analysis["palette_inference"]["source"],
            )
        )
    lines.append("")

    lines.append("## Per-scene detail")
    lines.append("")
    for entry in report["scenes"]:
        analysis = entry["analysis"]
        lines.append(f"### {entry['filename']} -> `{analysis['scene_id']}`")
        lines.append("")

        mapped = analysis["mapped_fixture_ids"]
        lines.append(f"Mapped fixtures ({len(mapped)}): " + (", ".join(f"`{f}`" for f in mapped) if mapped else "none"))
        lines.append("")

        discarded = analysis["discarded_lights"]
        lines.append(f"Discarded out-of-scope lights ({len(discarded)}) — not migrated, provenance only:")
        if discarded:
            for mapping in discarded:
                scope = mapping.get("scope_status")
                suffix = f" [{scope}]" if scope else ""
                fixture = (
                    f" (registry fixture: `{mapping['fixture_id']}`)"
                    if mapping.get("fixture_id")
                    else ""
                )
                lines.append(f"- {mapping['name']} (`{mapping['resource_id']}`){suffix}{fixture} — {mapping['reason']}")
        else:
            lines.append("- none")
        lines.append("")

        unresolved = analysis["unresolved_resources"]
        lines.append(f"Unresolved resources ({len(unresolved)}):")
        if unresolved:
            for mapping in unresolved:
                lines.append(f"- {mapping['name']} (`{mapping['resource_id']}`) — {mapping['reason']}")
        else:
            lines.append("- none")
        lines.append("")

        segments = analysis["wled_segments"]
        mapped_segments = [s for s in segments if s["status"] == "mapped"]
        unmapped_segments = [s for s in segments if s["status"] != "mapped"]
        if mapped_segments:
            joined = ", ".join(f"{s['segment_index']}->`{s['fixture_id']}`" for s in mapped_segments)
            suffix = f", {len(unmapped_segments)} unmapped" if unmapped_segments else ""
            lines.append(f"WLED segments ({len(mapped_segments)} mapped{suffix}): {joined}")
        else:
            lines.append("WLED segments (0 mapped): none")
        for segment in unmapped_segments:
            lines.append(f"- segment {segment['segment_index']}: {segment['reason']}")
        lines.append("")

        selection = analysis["target_selection"]
        overcover = (
            " (over-covers: " + ", ".join(f"`{f}`" for f in selection["overcovered_fixture_ids"]) + ")"
            if selection["overcovered_fixture_ids"]
            else ""
        )
        lines.append(
            f"Targets: {', '.join(f'`{t}`' for t in selection['target_ids'])}{overcover} "
            f"— rule: {selection['rule']}"
        )
        lines.append("")

        palette = analysis["palette_inference"]
        colors = ", ".join(f"`{c}`" for c in palette["colors"]) if palette["colors"] else "none"
        lines.append(f"Palette (approximate={str(palette['approximate']).lower()}, source {palette['source']}): {colors}")
        lines.append("")
        lines.append(f"Palette note: {palette['note']}")
        lines.append("")

        motion = analysis["motion_inference"]
        lines.append(f"Motion: mode={motion['mode']}, strategy={motion['strategy']} — {motion['note']}")
        lines.append("")

        concerns = analysis["fidelity_concerns"]
        lines.append(f"Fidelity concerns ({len(concerns)}):")
        if concerns:
            for concern in concerns:
                fixtures = (
                    " fixtures " + ", ".join(f"`{f}`" for f in concern["fixture_ids"])
                    if concern["fixture_ids"]
                    else ""
                )
                lines.append(f"- [{concern['level']}] {concern['code']}:{fixtures} {concern['message']}")
        else:
            lines.append("- none")
        lines.append("")

        lines.append("Converted v2 candidate:")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(entry["scene"], indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")

    lines.append("## Backup plan (procedure only; not executed)")
    lines.append("")
    lines.append("A timestamped immutable copy of the v1 scene directory must be created before any")
    lines.append("v2 write (master plan §11 step 5). See `scene_studio.migration.backup.plan_backup`")
    lines.append("for the manifest (per-file sha256) and ordered procedure; it is pure data and")
    lines.append("performs no writes.")
    lines.append("")
    return "\n".join(lines)


def write_dry_run_report(
    scene_dir: str | Path,
    registry_path: str | Path,
    crosswalk_path: str | Path,
    out_dir: str | Path,
    generated_at: str | None = None,
    stem: str = DEFAULT_STEM,
) -> dict[str, str]:
    """Write the JSON + markdown dry-run artifacts; returns their paths.

    Byte-deterministic given identical inputs and ``generated_at``.
    """
    report = build_report(scene_dir, registry_path, crosswalk_path, generated_at=generated_at)
    report = sanitize_tree(report)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scene_studio.migration.dryrun",
        description="Generate the deterministic v1->v2 migration dry-run report (no activation).",
    )
    parser.add_argument("--scene-dir", default=str(DEFAULT_SCENE_DIR), type=str)
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY), type=str)
    parser.add_argument("--crosswalk", default=str(DEFAULT_CROSSWALK), type=str)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), type=str)
    parser.add_argument("--stem", default=DEFAULT_STEM, type=str)
    parser.add_argument("--generated-at", default=DEFAULT_GENERATED_AT, type=str)
    args = parser.parse_args(argv)
    paths = write_dry_run_report(
        scene_dir=args.scene_dir,
        registry_path=args.registry,
        crosswalk_path=args.crosswalk,
        out_dir=args.out_dir,
        generated_at=args.generated_at,
        stem=args.stem,
    )
    print(f"wrote {paths['json']}")
    print(f"wrote {paths['markdown']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
