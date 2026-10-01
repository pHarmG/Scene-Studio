"""Canonical version, immutable identity and read-only release discovery."""
import importlib.util
import json
import subprocess
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from scene_studio.build_info import checkout_build, get_build_info, version_key
from scene_studio.updates import check_updates, fetch_release
from scene_studio import updates

ROOT = Path(__file__).resolve().parents[2]


def release(version="0.2.0", **extra):
    return {"tag_name": "v" + version, "assets": [{"id": 1, "name": f"Scene-Studio-v{version}.zip"}, {"id": 2, "name": "SHA256SUMS.txt"}], **extra}


@pytest.mark.parametrize("installed,latest,state", [
    ("0.1.0", "0.1.0", "current"), ("0.2.0", "0.1.0", "current"),
    ("0.1.9", "0.1.10", "available"), ("0.1.0-rc.1", "0.1.0", "available"),
    ("0.9.0", "1.0.0", "available"),
])
def test_semantic_updates(installed, latest, state):
    assert check_updates(installed, lambda: release(latest))["state"] == state


@pytest.mark.parametrize("extra", [{"draft": True}, {"prerelease": True}, {"assets": []}])
def test_incomplete_releases_unavailable(extra):
    assert check_updates("0.1.0", lambda: release(**extra))["state"] == "unavailable"


def test_private_without_credential_never_claims_current(monkeypatch):
    monkeypatch.delenv("SCENE_STUDIO_GITHUB_TOKEN", raising=False)
    def denied():
        raise HTTPError("https://api.github.com", 404, "private", {}, None)
    result = check_updates("0.1.0", denied)
    assert result["state"] == "unavailable"
    assert "SCENE_STUDIO_GITHUB_TOKEN" in result["message"]


@pytest.mark.parametrize("error,state", [(HTTPError("", 500, "opaque", {}, None), "error"), (URLError("opaque"), "unavailable"), (ValueError("opaque"), "error")])
def test_errors_do_not_disclose_upstream(error, state, caplog):
    def failure():
        raise error
    result = check_updates("0.1.0", failure)
    assert result["state"] == state
    assert "opaque" not in json.dumps(result) + caplog.text


def test_credentials_used_only_on_server(monkeypatch, caplog):
    sentinel = "synthetic-private-access-marker"
    monkeypatch.setenv("SCENE_STUDIO_GITHUB_TOKEN", sentinel)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps(release(body=sentinel, name=sentinel, html_url="https://example.test/?" + sentinel)).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer " + sentinel
            assert request.full_url.startswith("https://api.github.com/")
            return Response()
    monkeypatch.setattr(updates, "build_opener", lambda *args: Opener())
    result = check_updates("0.1.0")
    assert result["state"] == "available"
    assert sentinel not in json.dumps(result) + caplog.text


def test_source_identity_and_tag_gates(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()
    git("init")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "Test")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    git("add", "VERSION")
    git("commit", "-m", "fixture")
    git("tag", "v0.1.0")
    info = checkout_build(tmp_path, "release", "v0.1.0")
    assert info["source_sha"] == git("rev-parse", "HEAD")
    assert not info["dirty"] and info["channel"] == "release"
    with pytest.raises(ValueError, match="match root VERSION"):
        checkout_build(tmp_path, "release", "v0.2.0")
    (tmp_path / "patch.py").write_text("# local patch\n")
    local = checkout_build(tmp_path)
    assert local["version"] == info["version"] and local["dirty"] and local["channel"] == "local"
    assert local["source_tree_sha256"] != info["source_tree_sha256"]
    with pytest.raises(ValueError, match="clean source"):
        checkout_build(tmp_path, "release", "v0.1.0")
    git("add", "patch.py")
    git("commit", "-m", "patched")
    with pytest.raises(ValueError, match="identify HEAD"):
        checkout_build(tmp_path, "release", "v0.1.0")


def test_canonical_version_validation(tmp_path):
    spec = importlib.util.spec_from_file_location("version_script", ROOT / "scripts/version.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.validate_versions() == (ROOT / "VERSION").read_text().strip()
    (tmp_path / "backend").mkdir()
    (tmp_path / "workbench").mkdir()
    (tmp_path / "VERSION").write_text("0.1.0")
    (tmp_path / "backend/pyproject.toml").write_text('[project]\nversion="0.1.1"\n')
    for name in ("package.json", "package-lock.json"):
        (tmp_path / "workbench" / name).write_text(json.dumps({"version": "0.1.0", "packages": {"": {"version": "0.1.0"}}}))
    with pytest.raises(ValueError, match="root VERSION"):
        module.validate_versions(tmp_path)


def test_installed_identity_without_checkout(tmp_path, monkeypatch):
    import scene_studio.build_info as module
    identity = {"version": "0.1.0", "channel": "release", "source_sha": "a" * 40, "dirty": False}
    (tmp_path / "build-info.json").write_text(json.dumps(identity))
    monkeypatch.setattr(module, "__file__", str(tmp_path / "build_info.py"))
    module.get_build_info.cache_clear()
    try:
        assert module.get_build_info() == identity
    finally:
        module.get_build_info.cache_clear()


def test_semver_prerelease_order():
    assert version_key("1.0.0-rc.2") < version_key("1.0.0-rc.10") < version_key("1.0.0")
    for invalid in ("1.0", "01.0.0", "1.0.0-rc..1", "1.0.0-01"):
        with pytest.raises(ValueError): version_key(invalid)


def test_github_credentials_scrubbed_from_diagnostics():
    from scene_studio.domain.sanitize import sanitize_tree
    for prefix in ("ghp_", "github_pat_"):
        credential = prefix + "A" * 40
        blob = json.dumps(sanitize_tree({"message": "failed " + credential, "github_token": credential}))
        assert credential not in blob and "[REDACTED]" in blob


def test_release_secret_scan_detects_github_credentials(tmp_path):
    spec = importlib.util.spec_from_file_location("secret_scanner", ROOT / "scripts/security/scan_secrets.py")
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    path = tmp_path / "accidental.txt"
    credential = "ghp_" + "A" * 40
    path.write_text("credential leaked: " + credential)
    findings = module.scan([path])
    assert findings and findings[0].detector == "github-credential"
    assert credential not in json.dumps([module.asdict(item) for item in findings])
