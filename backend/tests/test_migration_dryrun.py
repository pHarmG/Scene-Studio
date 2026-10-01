"""Dry-run report tests: deterministic artifact generation (JSON + markdown)."""

import json
from pathlib import Path

from scene_studio.migration.dryrun import (
    DEFAULT_GENERATED_AT,
    build_report,
    render_markdown,
    write_dry_run_report,
)

from test_migration_analyzer import CROSSWALK_PATH, REGISTRY_PATH, V1_DIR

FIXED_GENERATED_AT = DEFAULT_GENERATED_AT  # "2026-09-10T00:00:00Z"


def _write(tmp_path: Path, generated_at: str | None) -> dict[str, bytes]:
    paths = write_dry_run_report(
        scene_dir=V1_DIR,
        registry_path=REGISTRY_PATH,
        crosswalk_path=CROSSWALK_PATH,
        out_dir=tmp_path,
        generated_at=generated_at,
    )
    return {
        "json": Path(paths["json"]).read_bytes(),
        "md": Path(paths["markdown"]).read_bytes(),
    }


def test_report_generation_is_byte_deterministic(tmp_path):
    first = _write(tmp_path / "a", FIXED_GENERATED_AT)
    second = _write(tmp_path / "b", FIXED_GENERATED_AT)
    assert first == second


def test_generated_at_changes_bytes_but_nothing_else(tmp_path):
    stamped = _write(tmp_path / "stamped", FIXED_GENERATED_AT)
    unstamped = _write(tmp_path / "unstamped", None)
    assert stamped["json"] != unstamped["json"]
    report = json.loads(stamped["json"].decode("utf-8"))
    del report["generated_at"]
    unstamped_report = json.loads(unstamped["json"].decode("utf-8"))
    unstamped_report.pop("generated_at", None)
    assert report == unstamped_report
    assert b"not recorded (deterministic mode)" in unstamped["md"]


def test_report_structure_and_content(tmp_path):
    data = _write(tmp_path / "out", FIXED_GENERATED_AT)
    report = json.loads(data["json"].decode("utf-8"))

    assert report["tool"] == "scene_studio.migration"
    assert report["generated_at"] == FIXED_GENERATED_AT
    assert report["scene_count"] == 7
    assert report["registry_summary"]["fixture_count"] == 24
    assert report["registry_summary"]["target_count"] == 5
    assert [entry["filename"] for entry in report["scenes"]] == sorted(
        entry["filename"] for entry in report["scenes"]
    )
    for entry in report["scenes"]:
        assert entry["analysis"]["scene_id"] == entry["scene"]["id"]
        assert entry["scene"]["schema_version"] == 2
        assert entry["scene"]["metadata"]["migrated_from_v1"]["filename"] == entry["filename"]
        # Out-of-scope provenance: identification records for every captured
        # light outside the v1 apply scope.
        out_of_scope = entry["scene"]["metadata"]["migrated_from_v1"]["out_of_scope_lights"]
        assert len(out_of_scope) == 10
        assert {item["status"] for item in out_of_scope} == {
            "registry_known_out_of_scope",
            "not_in_use",
        }
    # Every converted fixture state key exists in the registry (spot-check via report).
    registry_fixture_ids = {
        fixture["id"] for fixture in json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["fixtures"]
    }
    for entry in report["scenes"]:
        assert set(entry["scene"]["fixture_states"]) <= registry_fixture_ids


def test_markdown_summary_and_sections(tmp_path):
    data = _write(tmp_path / "out", FIXED_GENERATED_AT)
    md = data["md"].decode("utf-8")
    assert md.startswith("# Scene Studio v1 -> v2 Migration Dry Run")
    assert md.count("| `") >= 7  # one summary row per scene
    assert "### twilight.json -> `twilight`" in md
    # Scope rule: migration reproduces the effective old apply scope —
    # 14 mapped fixtures (8 crosswalk hue + 6 wled), 10 discarded out-of-scope
    # captured lights per scene.
    assert "| 14 | 10 | 0 |" in md
    # Target selection: smallest exact semantic cover — every scene resolves
    # to studio once office_strip is a studio member, never a covering target.
    assert "`studio`" in md
    assert "office_strip`" not in md or "`studio`, `office_strip`" not in md
    assert "whole_house" not in md
    assert "Scope rule:" in md
    assert "provenance only" in md
    assert "Water Light" in md and "Food Light" in md  # discarded out-of-scope lights surfaced
    assert "registry_known_out_of_scope" in md and "not_in_use" in md
    assert "out_of_scope_lights" in md  # provenance present in the converted candidates
    assert "fixture_disabled" in md  # disabled-fixture states are flagged (double_strip)
    assert "gradient:g_strip" in md
    assert "nothing here is activated" in md  # explicit non-activation statement
    assert "## Backup plan (procedure only; not executed)" in md


def test_build_report_is_pure_and_repeatable():
    first = build_report(V1_DIR, REGISTRY_PATH, CROSSWALK_PATH, generated_at=FIXED_GENERATED_AT)
    second = build_report(V1_DIR, REGISTRY_PATH, CROSSWALK_PATH, generated_at=FIXED_GENERATED_AT)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_render_markdown_matches_report():
    report = build_report(V1_DIR, REGISTRY_PATH, CROSSWALK_PATH, generated_at=FIXED_GENERATED_AT)
    assert render_markdown(report) == render_markdown(report)
