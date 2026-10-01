"""Exercise published old/new ZIPs with the production transaction engine offline.

This validates artifact bytes and rollback in a temporary install. It does not
claim to exercise Home Assistant, restart authorization, or the live browser.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from scene_studio import release_trust as trust


def load_supervisor():
    sys.modules["scene_studio_release"] = trust
    spec = importlib.util.spec_from_file_location(
        "palette_pair_supervisor", ROOT / "installer/runtime/scene_studio_update_supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact_retrieval(path, checksum):
    data, sums = path.read_bytes(), checksum.read_bytes()
    with zipfile.ZipFile(path) as archive:
        version = json.loads(archive.read("scene-studio-release/BUILD.json"))["version"]
    def retrieve(target, destination, progress):
        assert target == version, "Wrong selected release"
        release = {"tag_name": "v" + version, "assets": [
            {"id": 21, "name": f"Scene-Studio-v{version}.zip"},
            {"id": 22, "name": "SHA256SUMS.txt"}]}
        return trust.retrieve_verified(target, destination, fetch=lambda _: release,
            download=lambda ident, _: data if ident == 21 else sums, progress=progress)
    return version, retrieve


def ui_bytes(product):
    return b"\n".join(p.read_bytes() for p in sorted(product.rglob("*.js")))


class FastEvent(threading.Event):
    def wait(self, timeout=None):
        return super().wait(min(timeout or 0, .001))


def verify_pair(old_zip, old_sums, new_zip, new_sums):
    old_version, retrieve_old = artifact_retrieval(old_zip, old_sums)
    new_version, retrieve_new = artifact_retrieval(new_zip, new_sums)
    assert trust.stable_key(new_version) > trust.stable_key(old_version)
    supervisor = load_supervisor()
    with tempfile.TemporaryDirectory(prefix="scene-studio-palette-pair-") as temporary:
        root = Path(temporary)
        old, old_identity = retrieve_old(old_version, root / "verified-old", lambda _: None)
        # Provision only this disposable fixture; the actual update below uses Runner.
        shutil.copytree(old / "backend/src/scene_studio", root / "apps/scene_studio")
        shutil.copytree(old / "workbench/dist", root / "www/scene_studio")
        assert b"builder-overrides" in ui_bytes(root / "www/scene_studio")
        preserved = {}
        for name in ("apps/apps.yaml", "secrets.yaml", "scene_studio_store/scenes.json",
                     "apps/unrelated.py", "www/scene-studio-card/card.js"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            preserved[path] = ("preserved-fixture:" + name).encode()
            path.write_bytes(preserved[path])
        restarts, disconnects = [], []
        def probe():
            if restarts and not disconnects:
                disconnects.append(True)
                raise ConnectionError("expected restart interruption")
            build = json.loads((root / "apps/scene_studio/build-info.json").read_text())
            return {"engine": {"ok": True}, "product": {"build": build},
                    "runtime": {"mode": "registry_admin"}}
        runner = supervisor.Runner(root, probe, lambda: restarts.append(True),
            retrieve=retrieve_new, cancelled=FastEvent(), timeout=1)
        before = [supervisor.tree_hashes(p) for p in runner.products]
        # A failed activation must restore actual old code, not just its version string.
        original_replace = supervisor.os.replace
        def fail_activation(source, destination):
            if Path(source).name == "stage-1":
                raise OSError("controlled activation failure")
            return original_replace(source, destination)
        supervisor.os.replace = fail_activation
        try:
            result = runner.start({"target_version": new_version}, background=False)
        finally:
            supervisor.os.replace = original_replace
        assert result["state"] == "failed" and result["rolled_back"], result
        assert before == [supervisor.tree_hashes(p) for p in runner.products]
        assert probe()["product"]["build"] == old_identity
        restarts.clear()
        disconnects.clear()
        result = runner.start({"target_version": new_version}, background=False)
        assert result["state"] == "succeeded" and restarts and disconnects, result
        installed = json.loads((root / "apps/scene_studio/build-info.json").read_text())
        assert installed["version"] == new_version and installed["tag"] == "v" + new_version
        assert json.loads((root / "www/scene_studio/build-info.json").read_text()) == installed
        bundle = ui_bytes(root / "www/scene_studio")
        assert b"builder-overrides" not in bundle and b"builder-override-add" not in bundle
        assert b"palette-slot" in bundle and b"data-fixture-details" in bundle
        assert all(path.read_bytes() == contents for path, contents in preserved.items())
        assert "preserved-fixture" not in runner.journal.read_text()
        return {"old_version": old_version, "new_version": new_version,
                "source_sha": installed["source_sha"], "ui_sentinel": "passed",
                "rollback": "passed", "data_preserved": "passed", "reconnect": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-zip", "old-sums", "new-zip", "new-sums"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify_pair(args.old_zip, args.old_sums, args.new_zip, args.new_sums), indent=2))
