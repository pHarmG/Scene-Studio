"""Read-only release discovery. Credentials and upstream bodies never leave here.

Downloads are deliberate links to the authenticated GitHub Release page. Installing
still requires the verified guided installer; this module never writes a host.
"""
from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .build_info import version_key

REPOSITORY = "pHarmG/Scene-Studio"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward an Authorization header to another origin


def fetch_release():
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "Scene-Studio",
               "X-GitHub-Api-Version": "2022-11-28"}
    credential = os.environ.get("SCENE_STUDIO_GITHUB_TOKEN")
    if credential:
        headers["Authorization"] = "Bearer " + credential
    request = Request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest", headers=headers)
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        return json.loads(response.read(1024 * 1024))


def unchecked() -> dict:
    return {"state": "unchecked", "message": "Updates have not been checked.", "release_url": RELEASES_URL}


def check_updates(installed: str | None, fetch=None) -> dict:
    """Only stable, complete product releases qualify. Never compare main commits."""
    result = {"installed_version": installed, "release_url": RELEASES_URL}
    try:
        current = version_key(installed or "")
        release = (fetch or fetch_release)()
        tag = release["tag_name"]
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError()
        latest = tag[1:]
        latest_key = version_key(latest)
        names = {asset["name"] for asset in release.get("assets", [])}
        if (release.get("draft") or release.get("prerelease") or not latest_key[3]
                or not {f"Scene-Studio-v{latest}.zip", "SHA256SUMS.txt"}.issubset(names)):
            return {**result, "state": "unavailable", "message": "No compatible complete release is available."}
        # Construct safe URLs from the validated tag; never echo GitHub fields,
        # exceptions, headers, or release bodies (which can contain credentials).
        return {**result, "state": "available" if latest_key > current else "current",
                "latest_version": latest, "release_url": f"{RELEASES_URL}/tag/{tag}",
                "message": "Update available." if latest_key > current else "You are up to date."}
    except HTTPError as exc:
        if exc.code in (401, 403, 404, 429):
            return {**result, "state": "unavailable", "message": "Release access unavailable. For a private repository, configure SCENE_STUDIO_GITHUB_TOKEN on the server with Contents read access, or open GitHub Releases while signed in. No release may have been published yet."}
        return {**result, "state": "error", "message": "GitHub release check failed. Try again later."}
    except (URLError, TimeoutError, OSError):
        return {**result, "state": "unavailable", "message": "GitHub could not be reached. Try again later."}
    except Exception:
        return {**result, "state": "error", "message": "Release information could not be validated."}
