"""Deterministic golden render-plan generator for the Workbench mock (§10).

Policy
------
Python is the CANONICAL renderer. The Workbench mock used to carry a literal
JS mirror of the renderers (``src/mocks/render_plan.js``); that mirror is
unwound and replaced by the static goldens this module generates:

- Goldens are built from the VERBATIM base sample documents
  (``fixtures/registry.sample.json`` + ``fixtures/scenes.sample.json``)
  through the real domain models and the real ``build_render_plan``. No JS
  reimplementation is involved anywhere.
- One golden per sample scene: ``<scene_id>.json`` holding ``{scene_id,
  target_ids, registry_updated, render_plan}`` where ``render_plan`` is
  exactly ``RenderPlan.to_dict()``.
- Goldens are SCENARIO-INDEPENDENT demo data (Workbench rebuild decision):
  the mock serves the same base-sample plan for every mock scenario, and
  target-scoped dry-runs are not represented (goldens are built for each
  scene's own targets). They are refreshed only when the base samples or
  the renderers change — never per scenario.
- The devserver (``python -m scene_studio.devserver``, the real engine) is
  the interactive truth; anything needing a live computed plan uses it.

Output is deterministic: sorted keys, no timestamps beyond what the sample
documents themselves carry (``registry.updated``), byte-identical across
runs for the same input. Stale ``*.json`` files from removed scenes are not
deleted automatically; the Workbench smoke run asserts the golden set still
matches the sample scenes.

Usage (both forms the devserver supports)::

    python -m scene_studio.devmock [--fixtures-dir DIR] [--out DIR]

    python backend/src/scene_studio/devmock.py ...

``python -m scene_studio.devmock`` needs the ``src`` tree importable (run it
from ``backend/src``, with that directory on ``PYTHONPATH``,
or with the package installed); direct script execution always works — the
module bootstraps its own import path, mirroring ``devserver.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# import bootstrap (must run before the scene_studio imports below)
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVICE_ROOT.parents[1]
_SRC_PACKAGE = Path(__file__).resolve().parent

#: Sample documents the goldens are generated from (repo layout default).
DEFAULT_FIXTURES_DIR = _SERVICE_ROOT / "fixtures"

#: Golden output directory consumed by the Workbench mock client.
DEFAULT_OUT_DIR = (
    _REPO_ROOT / "packages" / "scene_studio_workbench" / "src" / "mocks" / "render_plans"
)


def _bootstrap_import_path() -> None:
    """Make ``scene_studio.*`` importable from this file's location.

    Two execution modes are supported (mirrors ``devserver.py``):

    - ``python -m scene_studio.devmock``: the ``-m`` machinery already
      imported a ``scene_studio`` package; make sure the ``src`` tree is
      part of its ``__path__``.
    - direct script execution: no ``scene_studio`` package exists yet, so
      the ``src`` root is added to ``sys.path`` and the regular package is
      imported normally.
    """
    package = sys.modules.get("scene_studio")
    src_package = str(_SRC_PACKAGE)
    if package is not None and hasattr(package, "__path__"):
        if src_package not in package.__path__:
            package.__path__.append(src_package)
    else:
        src_root = str(_SRC_PACKAGE.parent)
        if src_root not in sys.path:
            sys.path.insert(0, src_root)


_bootstrap_import_path()

from scene_studio.domain.fixtures import FixtureRegistry  # noqa: E402
from scene_studio.domain.scenes import Scene  # noqa: E402
from scene_studio.renderers.plan import build_render_plan  # noqa: E402

__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "DEFAULT_OUT_DIR",
    "build_goldens",
    "main",
    "write_goldens",
]


def build_goldens(fixtures_dir: Path | str | None = None) -> dict[str, dict]:
    """Build one golden per sample scene from the verbatim base samples.

    Loads both documents through the domain models (so the samples are
    validated the same way the engine validates them) and compiles each
    scene with the REAL ``build_render_plan``. Deterministic: scenes are
    processed in sorted id order and only document-carried data is used.
    """
    fixtures_dir = Path(fixtures_dir) if fixtures_dir is not None else DEFAULT_FIXTURES_DIR
    registry_doc = json.loads((fixtures_dir / "registry.sample.json").read_text(encoding="utf-8"))
    scenes_doc = json.loads((fixtures_dir / "scenes.sample.json").read_text(encoding="utf-8"))
    registry = FixtureRegistry.from_dict(registry_doc)

    goldens: dict[str, dict] = {}
    for raw_scene in sorted(scenes_doc.get("scenes", []), key=lambda scene: scene["id"]):
        scene = Scene.from_dict(raw_scene)
        plan = build_render_plan(scene, registry)
        goldens[scene.id] = {
            "scene_id": scene.id,
            "target_ids": list(scene.target_ids),
            "registry_updated": registry.updated,
            "render_plan": plan.to_dict(),
        }
    return goldens


def write_goldens(goldens: dict[str, dict], out_dir: Path | str | None = None) -> list[Path]:
    """Write one ``<scene_id>.json`` golden per scene; returns paths written.

    Bytes are deterministic: sorted keys, 2-space indent, LF newlines, one
    trailing newline — re-running on the same samples is byte-identical.
    """
    out_dir = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for scene_id in sorted(goldens):
        path = out_dir / f"{scene_id}.json"
        text = json.dumps(goldens[scene_id], indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scene_studio.devmock",
        description=(
            "Regenerate the Workbench mock's golden render plans from the base "
            "sample documents (Python is the canonical renderer; the goldens "
            "are scenario-independent static demo data)."
        ),
    )
    parser.add_argument(
        "--fixtures-dir",
        default=str(DEFAULT_FIXTURES_DIR),
        help=f"directory holding registry.sample.json + scenes.sample.json (default: {DEFAULT_FIXTURES_DIR})",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help=f"output directory for <scene_id>.json goldens (default: {DEFAULT_OUT_DIR})",
    )
    args = parser.parse_args(argv)

    fixtures_dir = Path(args.fixtures_dir)
    out_dir = Path(args.out)
    goldens = build_goldens(fixtures_dir)
    written = write_goldens(goldens, out_dir)
    print(
        f"scene_studio.devmock: wrote {len(written)} golden(s) "
        f"from {fixtures_dir} to {out_dir}"
    )
    for path in written:
        print(f"scene_studio.devmock: {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
