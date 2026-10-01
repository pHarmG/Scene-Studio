"""Validate canonical version declarations and emit safe build metadata."""
import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from scene_studio.build_info import checkout_build


def validate_versions(root=ROOT):
    version = (root / "VERSION").read_text().strip()
    project = tomllib.loads((root / "backend/pyproject.toml").read_text())["project"]
    package = json.loads((root / "workbench/package.json").read_text())
    lock = json.loads((root / "workbench/package-lock.json").read_text())
    for value in (project["version"], package["version"], lock["version"], lock["packages"][""]["version"]):
        if value != version:
            raise ValueError("Product version declarations must match root VERSION")
    return version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default=os.environ.get("SCENE_STUDIO_BUILD_CHANNEL", "local"))
    parser.add_argument("--tag", default=os.environ.get("SCENE_STUDIO_RELEASE_TAG"))
    args = parser.parse_args()
    validate_versions()
    print(json.dumps(checkout_build(ROOT, args.channel, args.tag)))


if __name__ == "__main__":
    main()
