"""Release contract and standalone bootstrap, using the real manifest verifier."""
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_installer_acceptance import release_dir, REPO_ROOT, HAS_PWSH  # noqa: F401


def ps_quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def test_payload_identity_and_secret_exclusion(release_dir, monkeypatch):
    identity = json.loads((release_dir / "BUILD.json").read_text())
    assert identity == json.loads((release_dir / "backend/src/scene_studio/build-info.json").read_text())
    assert identity == json.loads((release_dir / "workbench/dist/build-info.json").read_text())
    assert identity["version"] == (REPO_ROOT / "VERSION").read_text().strip()
    assert len(identity["source_sha"]) == 40
    assert identity["channel"] == os.environ.get("SCENE_STUDIO_BUILD_CHANNEL", "local")
    assert (release_dir / "Install-SceneStudio.ps1").read_bytes() == (REPO_ROOT / "installer/Install-SceneStudio.ps1").read_bytes()
    zip_path = release_dir.parent / f'Scene-Studio-v{identity["version"]}.zip'
    checksum = (release_dir.parent / "SHA256SUMS.txt").read_text().strip()
    assert checksum == hashlib.sha256(zip_path.read_bytes()).hexdigest() + "  " + zip_path.name
    sentinel = "synthetic-private-access-marker"
    monkeypatch.setenv("SCENE_STUDIO_GITHUB_TOKEN", sentinel)
    # Run the actual packaging path with a credential in its environment.
    out = release_dir.parent / "credential-check"
    result = subprocess.run(["python", str(REPO_ROOT / "scripts/build_release.py"), "--out", str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel not in result.stdout + result.stderr
    for path in out.rglob("*"):
        if path.is_file(): assert sentinel.encode() not in path.read_bytes()


@pytest.mark.skipif(not HAS_PWSH, reason="requires PowerShell 7")
@pytest.mark.parametrize("failure", [None, "checksum", "manifest", "identity", "traversal"])
def test_bootstrap_verifies_before_delegation(release_dir, tmp_path, failure):
    version = (release_dir / "VERSION").read_text().strip()
    archive_path = tmp_path / f"Scene-Studio-v{version}.zip"
    # Use a disposable official identity fixture; never mutate the shared tree.
    identity = json.loads((release_dir / "BUILD.json").read_text())
    identity.update(channel="release", dirty=False, tag="v" + version)
    if failure == "identity": identity["version"] = "99.0.0"
    payloads = {p.relative_to(release_dir).as_posix(): p.read_bytes() for p in release_dir.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256"}
    payloads["BUILD.json"] = json.dumps(identity).encode()
    manifest = "\n".join(f"{hashlib.sha256(data).hexdigest()}  {len(data)}  {name}" for name, data in sorted(payloads.items())) + "\n"
    payloads["MANIFEST.sha256"] = manifest.encode()
    if failure == "manifest": payloads["VERSION"] = b"tampered"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in payloads.items(): archive.writestr("scene-studio-release/" + name, data)
        if failure == "traversal": archive.writestr("scene-studio-release/../../escape.txt", "bad")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sums = tmp_path / "SHA256SUMS.txt"
    sums.write_text(("0" * 64 if failure == "checksum" else digest) + "  " + archive_path.name + "\n")
    release = {"tag_name": "v" + version, "draft": False, "prerelease": False, "assets": [{"id": 1, "name": archive_path.name}, {"id": 2, "name": "SHA256SUMS.txt"}]}
    release_json = tmp_path / "release.json"
    release_json.write_text(json.dumps(release))
    marker = tmp_path / "delegated.txt"
    harness = tmp_path / "bootstrap-test.ps1"
    harness.write_text(f"""
$ErrorActionPreference = 'Stop'
. {ps_quote(REPO_ROOT / 'installer/Get-SceneStudio.ps1')}
function Get-GithubRelease {{ param([string]$Tag); return (Get-Content -Raw -LiteralPath {ps_quote(release_json)} | ConvertFrom-Json) }}
function Save-GithubAsset {{ param($Asset, [string]$Path); if ($Asset.id -eq 1) {{ Copy-Item -LiteralPath {ps_quote(archive_path)} -Destination $Path }} else {{ Copy-Item -LiteralPath {ps_quote(sums)} -Destination $Path }} }}
function Start-SceneStudioInstaller {{ param([string]$Root); Set-Content -LiteralPath {ps_quote(marker)} -Value $Root }}
Invoke-SceneStudioBootstrap -RequestedVersion '{version}'
""", encoding="utf-8")
    result = subprocess.run(["pwsh", "-NoProfile", "-File", str(harness)], capture_output=True, text=True)
    if failure:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not marker.exists(), "bootstrap continued after failed verification"
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        root = Path(marker.read_text().strip())
        assert (root / "Install-SceneStudio.ps1").is_file()


def test_release_workflow_uses_real_gates():
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text()
    for gate in ("scripts/version.py", "pytest backend/tests", "npm run smoke", "npm run browser", "npm run typecheck", "npm test", "pytest tests/installer", "scan_secrets.py", "scripts/build_release.py", "verify_release_manifest.ps1", "--verify-tag"):
        assert gate in workflow
    assert "Get-SceneStudio.ps1" in workflow and "SHA256SUMS.txt" in workflow
