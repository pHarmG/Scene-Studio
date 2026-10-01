"""v1 -> v2 scene migration tooling (Workstream A3).

Pure-Python, stdlib-only, deterministic, dry-run-first: nothing here
activates scenes or contacts live systems (master plan §11). Identity
mapping always flows through the seeded fixture registry (§5 of the
architecture contracts); unresolvable resources are reported, never guessed.
"""

from .aggregate_fixture import apply_aggregate_fixture_plan, infer_child_fixture_ids, plan_aggregate_fixture_removal
from .analyzer import (
    AnalysisReport,
    FidelityConcern,
    PaletteInference,
    RegistryIndexes,
    ResourceMapping,
    SegmentMapping,
    TargetSelection,
    analyze_scene,
    build_crosswalk_index,
    build_registry_indexes,
    fidelity_concerns,
    infer_palette,
    map_hue_resources,
    map_wled_segments,
    scene_id_from_filename,
    scene_name_from_filename,
    select_targets,
)
from .backup import BackupPlan, plan_backup
from .colors import rgb_to_hex, wled_col_to_hex, xy_to_hex
from .converter import convert_scene

__all__ = [
    "AnalysisReport",
    "BackupPlan",
    "FidelityConcern",
    "PaletteInference",
    "RegistryIndexes",
    "ResourceMapping",
    "SegmentMapping",
    "TargetSelection",
    "analyze_scene",
    "apply_aggregate_fixture_plan",
    "infer_child_fixture_ids",
    "plan_aggregate_fixture_removal",
    "build_crosswalk_index",
    "build_registry_indexes",
    "convert_scene",
    "fidelity_concerns",
    "infer_palette",
    "map_hue_resources",
    "map_wled_segments",
    "plan_backup",
    "rgb_to_hex",
    "scene_id_from_filename",
    "scene_name_from_filename",
    "select_targets",
    "wled_col_to_hex",
    "xy_to_hex",
]