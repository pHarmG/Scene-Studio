"""Real filesystem transactions, controlled release/network/runtime boundaries."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import sys
import threading
import zipfile

import pytest

from scene_studio import release_trust as trust

ROOT = Path(__file__).resolve().parents[2]
sys.modules["scene_studio_release"] = trust
spec = importlib.util.spec_from_file_location("update_supervisor", ROOT / "installer/runtime/scene_studio_update_supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


def build(version, sha="b"):
    return {"version": version, "source_sha": sha * 40, "source_tree_sha256": sha * 64,
            "channel": "release", "dirty": False, "tag": "v" + version, "update_protocol": 1}


def release(version="0.1.1", **extra):
    return {"tag_name": "v" + version, "assets": [
        {"name": f"Scene-Studio-v{version}.zip", "id": 21, "browser_download_url": "injected"},
        {"name": "SHA256SUMS.txt", "id": 22}], **extra}


def archive(failure=None):
    identity = build("0.1.1")
    files = {
        "VERSION": b"0.1.1\n", "BUILD.json": json.dumps(identity).encode(),
        "backend/src/scene_studio/build-info.json": json.dumps(identity).encode(),
        "backend/src/scene_studio/appdaemon_adapter/adapter.py": b"# new code\n",
        "workbench/dist/build-info.json": json.dumps(identity).encode(),
        "workbench/dist/index.html": b"new workbench",
    }
    if failure in ("version", "tag", "channel", "dirty", "source_sha", "update_protocol"):
        identity[failure] = {"version": "0.1.2", "tag": "v0.1.2", "channel": "local",
                             "dirty": True, "source_sha": "x", "update_protocol": 2}[failure]
        files["BUILD.json"] = json.dumps(identity).encode()
    if failure == "metadata": files["workbench/dist/build-info.json"] = b"{}"
    files["MANIFEST.sha256"] = ("\n".join(f"{hashlib.sha256(data).hexdigest()}  {len(data)}  {name}"
                                               for name, data in sorted(files.items())) + "\n").encode()
    if failure == "manifest": files["VERSION"] = b"wrong"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zipped:
        for name, data in files.items(): zipped.writestr("scene-studio-release/" + name, data)
        if failure in ("traversal", "absolute", "backslash", "duplicate", "symlink"):
            names = {"traversal": "scene-studio-release/../../escape", "absolute": "/escape",
                     "backslash": "scene-studio-release/..\\escape", "duplicate": "scene-studio-release/version",
                     "symlink": "scene-studio-release/link"}
            entry = zipfile.ZipInfo(names[failure])
            if failure == "symlink": entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(entry, b"escape")
    data = stream.getvalue()
    sums = (hashlib.sha256(data).hexdigest() + "  Scene-Studio-v0.1.1.zip\n").encode()
    if failure == "checksum": sums = b"0" * 64 + b"  Scene-Studio-v0.1.1.zip\n"
    return data, sums


def retrieval(failure=None, fetched=None):
    data, sums = archive(failure)
    def retrieve(version, dest, progress):
        return trust.retrieve_verified(version, dest,
            fetch=lambda v: (fetched if fetched is not None else release()),
            download=lambda ident, limit: data if ident == 21 else sums, progress=progress)
    return retrieve


class FastEvent(threading.Event):
    def wait(self, timeout=None):
        return super().wait(min(timeout or 0, .001))


@pytest.fixture
def runtime(tmp_path):
    old = build("0.1.0", "a")
    for product in ("apps/scene_studio", "www/scene_studio"):
        path = tmp_path / product
        path.mkdir(parents=True)
        (path / "build-info.json").write_text(json.dumps(old))
        (path / "old.txt").write_text("old product")
    for name in ("apps/apps.yaml", "secrets.yaml", "scene_studio_store/scenes.json", "apps/unrelated.py"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic-private-data-marker")
    calls = []
    def probe():
        current = json.loads((tmp_path / "apps/scene_studio/build-info.json").read_text())
        return {"engine": {"ok": True}, "product": {"build": current}, "runtime": {"mode": "registry_admin"}}
    def runner(**kwargs):
        return supervisor.Runner(tmp_path, kwargs.pop("probe", probe),
                                 kwargs.pop("restart", lambda: calls.append("restart")),
                                 retrieve=kwargs.pop("retrieve", retrieval()), timeout=.025,
                                 cancelled=FastEvent(), **kwargs)
    return tmp_path, old, probe, runner, calls


@pytest.mark.parametrize("body", [None, {}, {"version": "0.1.1"}, {"target_version": "v0.1.1"},
    {"target_version": "0.1.1", "url": "injected"}, {"target_version": "0.1.1", "checksum": "injected"},
    {"target_version": "0.1.1", "tag": "v0.1.1"}, {"target_version": "0.1.1", "token": "injected"},
    {"target_version": "0.1.0"}, {"target_version": "0.0.9"}, {"target_version": "0.1.1-rc.1"}])
def test_explicit_newer_version_only(runtime, body):
    root, _, _, factory, calls = runtime
    runner = factory()
    before = [supervisor.tree_hashes(p) for p in runner.products]
    with pytest.raises(trust.TrustError): runner.start(body, background=False)
    assert before == [supervisor.tree_hashes(p) for p in runner.products]
    assert not calls


@pytest.mark.parametrize("failure", ["checksum", "traversal", "absolute", "backslash", "duplicate", "symlink",
    "manifest", "version", "tag", "channel", "dirty", "source_sha", "update_protocol", "metadata"])
def test_verification_failure_zero_activation(runtime, failure, monkeypatch):
    root, _, _, factory, calls = runtime
    runner = factory(retrieve=retrieval(failure))
    before = [supervisor.tree_hashes(p) for p in runner.products]
    mutations = []
    replace = supervisor.os.replace
    def spy(source, dest):
        if Path(source) in runner.products or Path(dest) in runner.products: mutations.append((source, dest))
        return replace(source, dest)
    monkeypatch.setattr(supervisor.os, "replace", spy)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["state"] == "failed" and not mutations and not calls
    assert before == [supervisor.tree_hashes(p) for p in runner.products]


@pytest.mark.parametrize("upstream", [release("0.1.2"), release(draft=True), release(prerelease=True), release(assets=[]), release(assets=[{"name": "SHA256SUMS.txt", "id": 3}])])
def test_incompatible_exact_release(runtime, upstream):
    _, _, _, factory, calls = runtime
    runner = factory(retrieve=retrieval(fetched=upstream))
    assert runner.start({"target_version": "0.1.1"}, background=False)["state"] == "failed"
    assert not calls


def test_asset_ids_resolved_server_side(tmp_path):
    data, sums = archive()
    requested, downloaded = [], []
    def fetch(version): requested.append(version); return release()
    def download(ident, limit): downloaded.append(ident); return data if ident == 21 else sums
    root, identity = trust.retrieve_verified("0.1.1", tmp_path / "stage", fetch=fetch, download=download)
    assert requested == ["0.1.1"] and downloaded == [22, 21]
    assert identity == build("0.1.1") and root.is_dir()


def test_backups_before_activation_and_data_preserved(runtime, monkeypatch):
    root, old, _, factory, calls = runtime
    runner = factory()
    preserved = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and "old.txt" not in p.name and p.name != "build-info.json"}
    replace = supervisor.os.replace
    seen = []
    def spy(source, dest):
        if Path(source) in runner.products:
            work = runner.transaction_root()
            assert all((work / f"backup-{i}/old.txt").is_file() for i in range(2))
            assert runner.read_record()["activation_intent"]
            seen.append(source)
        return replace(source, dest)
    monkeypatch.setattr(supervisor.os, "replace", spy)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["state"] == "succeeded" and len(seen) == 2 and len(calls) == 1
    assert all(path.read_bytes() == data for path, data in preserved.items())
    assert not any("private-data-marker" in v for v in [json.dumps(result), runner.journal.read_text()])


def test_activation_failure_restores_both_trees(runtime, monkeypatch):
    _, _, _, factory, calls = runtime
    runner = factory()
    before = [supervisor.tree_hashes(p) for p in runner.products]
    replace = supervisor.os.replace
    def fail(source, dest):
        if Path(source).name == "stage-1": raise OSError("synthetic-secret-marker")
        return replace(source, dest)
    monkeypatch.setattr(supervisor.os, "replace", fail)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["state"] == "failed" and result["rolled_back"]
    assert before == [supervisor.tree_hashes(p) for p in runner.products]
    assert "synthetic-secret-marker" not in json.dumps(result) + runner.journal.read_text()


def test_new_import_failure_restores_healthy_old_build(runtime):
    _, _, probe, factory, calls = runtime
    def broken_new():
        status = probe()
        if status["product"]["build"]["version"] == "0.1.1": raise ImportError("new backend broken")
        return status
    runner = factory(probe=broken_new)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["rolled_back"] and len(calls) == 2
    assert probe()["product"]["build"]["version"] == "0.1.0"


def test_disconnect_then_exact_new_identity(runtime):
    _, _, probe, factory, calls = runtime
    missed = []
    def reconnect():
        status = probe()
        if calls and not missed: missed.append(True); raise ConnectionError()
        return status
    runner = factory(probe=reconnect)
    assert runner.start({"target_version": "0.1.1"}, background=False)["state"] == "succeeded"
    assert missed


@pytest.mark.parametrize("crash_point", ["stage-0", "stage-1", "restarting"])
def test_new_supervisor_recovers_persisted_intent(runtime, monkeypatch, crash_point):
    _, _, probe, factory, _ = runtime
    class PowerLoss(BaseException): pass
    runner = factory()
    replace = supervisor.os.replace
    def crash(source, dest):
        if Path(source).name == crash_point: raise PowerLoss()
        return replace(source, dest)
    if crash_point == "restarting": runner.restart = lambda: (_ for _ in ()).throw(PowerLoss())
    else: monkeypatch.setattr(supervisor.os, "replace", crash)
    with pytest.raises(PowerLoss): runner.start({"target_version": "0.1.1"}, background=False)
    monkeypatch.setattr(supervisor.os, "replace", replace)
    replacement = factory()
    replacement.recover()
    status = replacement.status()
    if crash_point == "restarting": assert status["state"] == "succeeded"
    else: assert status["rolled_back"] and probe()["product"]["build"]["version"] == "0.1.0"


def test_failed_recovery_is_explicit_and_blocks_reapply(runtime):
    _, _, probe, factory, calls = runtime
    def probe_until_restart():
        if calls: raise ConnectionError("secret")
        return probe()
    runner = factory(probe=probe_until_restart)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["recovery_required"] and result["state"] == "failed"
    with pytest.raises(trust.TrustError, match="recovery"): runner.start({"target_version": "0.1.1"})


def test_os_lock_and_repeated_clicks_exclude_workers(runtime):
    _, _, _, factory, _ = runtime
    entered, release_worker = threading.Event(), threading.Event()
    retrieve = retrieval()
    def blocked(*args, **kwargs):
        entered.set(); release_worker.wait(5); return retrieve(*args, **kwargs)
    first, second = factory(retrieve=blocked), factory()
    first.start({"target_version": "0.1.1"})
    assert entered.wait(1)
    try:
        for runner in (first, second):
            with pytest.raises(trust.TrustError, match="already running"): runner.start({"target_version": "0.1.1"})
    finally:
        release_worker.set(); first.worker.join(3)
    assert first.status()["state"] == "succeeded"


def test_store_inside_product_rejected(runtime):
    root, _, _, factory, calls = runtime
    runner = factory(preserved_paths=[root / "apps/scene_studio/store"])
    with pytest.raises(trust.TrustError, match="overlaps"): runner.start({"target_version": "0.1.1"})
    assert not calls


def test_private_redirect_does_not_forward_credential(monkeypatch, caplog):
    from urllib.error import HTTPError
    token = "synthetic-private-access-marker"
    monkeypatch.setenv("SCENE_STUDIO_GITHUB_TOKEN", token)
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b"verified bytes"
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise HTTPError(request.full_url, 302, token, {"Location": "https://release-assets.githubusercontent.com/asset?signature=opaque"}, None)
            return Response()
    monkeypatch.setattr(trust, "build_opener", lambda *args: Opener())
    assert trust.asset_bytes(21, 1024) == b"verified bytes"
    assert requests[0].get_header("Authorization") == "Bearer " + token
    assert requests[1].get_header("Authorization") is None
    assert not caplog.text


@pytest.mark.parametrize("fault", ["old_version", "wrong_sha", "changed_mode", "unhealthy"])
def test_reconnect_cannot_misidentify_success(runtime, fault):
    _, old, probe, factory, calls = runtime
    def mismatch():
        status = probe()
        if status["product"]["build"]["version"] == "0.1.1":
            if fault == "old_version": status["product"]["build"] = old
            elif fault == "wrong_sha": status["product"]["build"]["source_sha"] = "c" * 40
            elif fault == "changed_mode": status["runtime"]["mode"] = "normal"
            else: status["engine"]["ok"] = False
        return status
    runner = factory(probe=mismatch)
    result = runner.start({"target_version": "0.1.1"}, background=False)
    assert result["rolled_back"] and result["state"] == "failed" and len(calls) == 2


def test_supervisor_rpc_contract_and_no_exception_leaks(runtime):
    _, _, _, factory, _ = runtime
    app = supervisor.SceneStudioUpdateSupervisor()
    app.runner = factory()
    response, http = app.endpoint({"method": "GET", "path": "/update/status"})
    assert http == 200 and response["status"] == 200 and response["body"]["state"] == "idle"
    for body in ({}, {"target_version": "0.1.1", "asset_url": "private"}):
        response, http = app.endpoint({"method": "POST", "path": "/update", "body": body})
        assert response["status"] == 409 and "private" not in json.dumps(response)
    class PlainRequest: content_type = "text/plain"
    response, _ = app.endpoint({"method": "POST", "path": "/update", "body": {"target_version": "0.1.1"}}, request=PlainRequest())
    assert response["status"] == 409
    app.runner.ready = lambda: (_ for _ in ()).throw(RuntimeError("synthetic-private-access-marker"))
    response, _ = app.endpoint({"method": "POST", "path": "/update", "body": {"target_version": "0.1.1"}})
    assert response["status"] == 503 and "private-access-marker" not in json.dumps(response)


def test_production_commits_only_in_new_interpreter(runtime):
    _, _, _, factory, _ = runtime
    runner = factory(require_restart=True, process_token="old-process")
    runner.restart = runner.cancelled.set
    status = runner.start({"target_version": "0.1.1"}, background=False)
    assert status["state"] == "restarting", "same-process API health must not commit"
    replacement = factory(require_restart=True, process_token="new-process")
    replacement.recover()
    assert replacement.status()["state"] == "succeeded"


def test_failed_restart_cannot_claim_update_success(runtime):
    _, _, _, factory, _ = runtime
    runner = factory(require_restart=True, process_token="old-process")
    runner.restart = lambda: (_ for _ in ()).throw(ConnectionError("opaque-secret"))
    status = runner.start({"target_version": "0.1.1"}, background=False)
    assert status["state"] == "failed" and status["recovery_required"]
    assert "opaque-secret" not in json.dumps(status)


def test_expected_restart_disconnect_recovers_in_new_process(runtime):
    _, _, _, factory, _ = runtime
    runner = factory(require_restart=True, process_token="old-process")
    def disconnected():
        runner.cancelled.set()
        raise ConnectionError("expected service disconnect")
    runner.restart = disconnected
    assert runner.start({"target_version": "0.1.1"}, background=False)["state"] == "restarting"
    replacement = factory(require_restart=True, process_token="new-process")
    replacement.recover()
    assert replacement.status()["state"] == "succeeded"


def test_import_failure_rollback_survives_both_production_restarts(runtime):
    _, _, probe, factory, _ = runtime
    first = factory(require_restart=True, process_token="old-process")
    first.restart = first.cancelled.set
    first.start({"target_version": "0.1.1"}, background=False)
    def new_import_failure():
        status = probe()
        if status["product"]["build"]["version"] == "0.1.1": raise ImportError()
        return status
    second = factory(require_restart=True, process_token="new-process", probe=new_import_failure)
    second.restart = second.cancelled.set
    second.recover()
    assert second.status()["state"] == "rollback"
    third = factory(require_restart=True, process_token="restored-process")
    third.recover()
    assert third.status()["rolled_back"] and third.status()["state"] == "failed"
    assert probe()["product"]["build"]["version"] == "0.1.0"
