"""Contract tests for the Workbench golden generator (``scene_studio.devmock``).

The goldens are the Workbench mock's static demo data: they must stay
deterministic (byte-identical across runs), track the base samples one scene
at a time, and embed a plan that validates against the domain model.
"""

import json
from pathlib import Path

import pytest

from scene_studio.devmock import DEFAULT_OUT_DIR, build_goldens, write_goldens
from scene_studio.domain.fidelity import RenderPlan

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def goldens() -> dict:
    return build_goldens(FIXTURES_DIR)


def test_one_golden_per_sample_scene(goldens):
    scenes_doc = json.loads((FIXTURES_DIR / "scenes.sample.json").read_text(encoding="utf-8"))
    assert sorted(goldens) == sorted(scene["id"] for scene in scenes_doc["scenes"])


def test_golden_shape_tracks_the_samples(goldens):
    registry_doc = json.loads((FIXTURES_DIR / "registry.sample.json").read_text(encoding="utf-8"))
    scenes_by_id = {
        scene["id"]: scene
        for scene in json.loads((FIXTURES_DIR / "scenes.sample.json").read_text(encoding="utf-8"))["scenes"]
    }
    for scene_id, golden in goldens.items():
        assert set(golden) == {"scene_id", "target_ids", "registry_updated", "render_plan"}
        assert golden["scene_id"] == scene_id
        assert golden["target_ids"] == scenes_by_id[scene_id]["target_ids"]
        # registry_updated mirrors the sample document (None when it has none).
        assert golden["registry_updated"] == registry_doc.get("updated")
        assert golden["render_plan"]["scene_id"] == scene_id


def test_golden_render_plan_validates_against_the_domain_model(goldens):
    for golden in goldens.values():
        plan = RenderPlan.from_dict(golden["render_plan"])
        assert plan.scene_id == golden["scene_id"]
        assert [p.fixture_id for p in plan.fixture_plans] == sorted(
            p.fixture_id for p in plan.fixture_plans
        )


def test_build_goldens_is_deterministic():
    assert json.dumps(build_goldens(FIXTURES_DIR), sort_keys=True) == json.dumps(
        build_goldens(FIXTURES_DIR), sort_keys=True
    )


def test_written_goldens_are_byte_identical_across_runs(tmp_path):
    goldens = build_goldens(FIXTURES_DIR)
    out_one, out_two = tmp_path / "one", tmp_path / "two"
    write_goldens(goldens, out_one)
    write_goldens(build_goldens(FIXTURES_DIR), out_two)
    for scene_id in goldens:
        left = (out_one / f"{scene_id}.json").read_bytes()
        right = (out_two / f"{scene_id}.json").read_bytes()
        assert left == right
        assert json.loads(left.decode("utf-8"))["scene_id"] == scene_id


def test_default_out_dir_is_the_workbench_mock_directory():
    assert DEFAULT_OUT_DIR.name == "render_plans"
    assert DEFAULT_OUT_DIR.parent.name == "mocks"
