"""Protocol-1 release trust, also installed as the independent scene_studio_release.

Keep this module stdlib-only and free of relative/backend imports. A supervisor
keeps its installed copy throughout an update, including backend import failure.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

REPOSITORY = "pHarmG/Scene-Studio"
API = f"https://api.github.com/repos/{REPOSITORY}"
STABLE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXTRACTED = 512 * 1024 * 1024


class TrustError(ValueError):
    pass


def stable_key(version):
    if not isinstance(version, str) or not STABLE.fullmatch(version):
        raise TrustError("A stable target_version is required.")
    return tuple(map(int, version.split(".")))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_bytes(url, accept="application/vnd.github+json", limit=1024 * 1024):
    """Bounded downloads; credentials only on the initial GitHub API request."""
    opener = build_opener(NoRedirect())
    for hop in range(5):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443) or parts.hostname not in {
                    "api.github.com", "github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com"}):
            raise TrustError("Untrusted release download URL.")
        headers = {"Accept": accept, "User-Agent": "Scene-Studio",
                   "X-GitHub-Api-Version": "2022-11-28"}
        token = os.environ.get("SCENE_STUDIO_GITHUB_TOKEN")
        if hop == 0 and parts.hostname == "api.github.com" and token:
            headers["Authorization"] = "Bearer " + token
        try:
            with opener.open(Request(url, headers=headers), timeout=30) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise TrustError("Release download exceeds size limit.")
                return data
        except HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                url = urljoin(url, exc.headers.get("Location", ""))
                continue
            raise TrustError("Release access unavailable.") from None
        except TrustError:
            raise
        except Exception:
            raise TrustError("Release access unavailable.") from None
    raise TrustError("Too many release download redirects.")


def fetch_release(version=None):
    suffix = "latest" if version is None else "tags/v" + version
    if version is not None:
        stable_key(version)
    return json.loads(github_bytes(f"{API}/releases/{suffix}"))


def release_assets(release, requested=None):
    tag = release.get("tag_name", "")
    version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else ""
    stable_key(version)
    if release.get("draft") or release.get("prerelease") or (requested and version != requested):
        raise TrustError("No compatible stable release.")
    names = (f"Scene-Studio-v{version}.zip", "SHA256SUMS.txt")
    selected = []
    for name in names:
        matches = [a for a in release.get("assets", []) if a.get("name") == name]
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
            raise TrustError("Release must contain exactly one product ZIP and checksum asset.")
        selected.append(matches[0]["id"])
    return version, selected


def asset_bytes(asset_id, limit):
    return github_bytes(f"{API}/releases/assets/{asset_id}", "application/octet-stream", limit)


def safe_relative(name):
    # Reject ambiguous paths on POSIX AND Windows, links, collisions and ADS.
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or name.startswith("/") or any(p in ("", ".", "..") or p.endswith((".", " "))
                                         for p in name.split("/"))):
        raise TrustError("Unsafe archive or manifest path.")
    return name


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_manifest(root):
    root = Path(root)
    expected = {}
    folded = set()
    for line in (root / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (\d+)  (.+)", line)
        if not match:
            raise TrustError("Release manifest verification failed.")
        sha, size, name = match.groups()
        safe_relative(name)
        if name.casefold() in folded or name == "MANIFEST.sha256":
            raise TrustError("Duplicate manifest entry.")
        folded.add(name.casefold())
        expected[name] = (sha, int(size))
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
              and p.relative_to(root).as_posix() != "MANIFEST.sha256"}
    if actual != set(expected):
        raise TrustError("Release manifest file set mismatch.")
    for name, (sha, size) in expected.items():
        path = root / name
        if path.is_symlink() or path.stat().st_size != size or digest(path) != sha:
            raise TrustError("Release manifest verification failed.")


def verify_build(root, version):
    build = json.loads((root / "BUILD.json").read_text(encoding="utf-8"))
    if (build.get("version") != version or build.get("tag") != "v" + version
            or build.get("channel") != "release" or build.get("dirty") is not False
            or not re.fullmatch(r"[0-9a-f]{40}", build.get("source_sha") or "")
            or not re.fullmatch(r"[0-9a-f]{64}", build.get("source_tree_sha256") or "")
            or build.get("update_protocol") != 1):
        raise TrustError("Release build identity or update protocol mismatch.")
    for relative in ("backend/src/scene_studio/build-info.json", "workbench/dist/build-info.json"):
        if json.loads((root / relative).read_text(encoding="utf-8")) != build:
            raise TrustError("Product build identities do not match.")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise TrustError("Release VERSION mismatch.")
    if not (root / "backend/src/scene_studio/appdaemon_adapter/adapter.py").is_file() or not (root / "workbench/dist/index.html").is_file():
        raise TrustError("Release product trees missing.")
    return build


def retrieve_verified(version, destination, fetch=fetch_release, download=asset_bytes, progress=lambda _: None):
    stable_key(version)
    _, ids = release_assets(fetch(version), version)
    progress("downloading")
    sums = download(ids[1], 1024 * 1024).decode("utf-8")
    archive = download(ids[0], MAX_ARCHIVE)
    progress("verifying")
    name = f"Scene-Studio-v{version}.zip"
    checks = [m.group(1).lower() for line in sums.splitlines()
              if (m := re.fullmatch(r"([0-9a-fA-F]{64})  " + re.escape(name), line))]
    if len(checks) != 1 or hashlib.sha256(archive).hexdigest() != checks[0]:
        raise TrustError("Release ZIP checksum verification failed.")
    import io
    destination = Path(destination)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        entries = zipped.infolist()
        names = set()
        if len(entries) > 20000 or sum(e.file_size for e in entries) > MAX_EXTRACTED:
            raise TrustError("Release archive exceeds size limit.")
        for entry in entries:
            path = entry.filename.rstrip("/") if entry.is_dir() else entry.filename
            safe_relative(path)
            if (not path.startswith("scene-studio-release/") and path != "scene-studio-release") or path.casefold() in names:
                raise TrustError("Unsafe or duplicate ZIP entry.")
            if stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise TrustError("Archive links and special files are forbidden.")
            names.add(path.casefold())
        destination.mkdir(parents=True, exist_ok=False)
        zipped.extractall(destination)
    root = destination / "scene-studio-release"
    verify_manifest(root)
    return root, verify_build(root, version)
