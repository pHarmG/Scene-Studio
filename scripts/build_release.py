#!/usr/bin/env python3
"""Build the clean, allowlisted Scene Studio release tree.

Produces ``dist/scene-studio-release/`` from an EXPLICIT allowlist — never
from a repo copy — so the shareable artifact carries no Git history, no
private topology, and no source-checkout noise. The release root reads like
the product, not a source checkout: user-facing entry points at the root,
product trees at their natural relative paths.

The build FAILS (non-zero exit, nothing shareable — the manifest is written
only after every gate passes) unless all gates pass, in order:

1. Workbench build present — ``workbench/dist/index.html`` + hashed assets;
2. Card build present — ``home-assistant/scene-studio-card/dist/scene-studio-card.js``;
3. Python imports — every bundled ``*.py`` parses, and the package imports in
   a clean interpreter with only the release tree on ``sys.path``
   (stdlib-only rule);
4. Portability scan — no private LAN addresses, device/bridge ids, or
   person/room names from the production sample set anywhere in the release;
5. Secret scan — ``scripts/security/scan_secrets.py <release>`` exits 0;
6. Prune — any ``__pycache__``/``*.pyc`` the gates dropped is removed before
   the manifest is written.

Usage::

    python scripts/build_release.py [--out DIR] [--refresh] [--zip]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "dist" / "scene-studio-release"

# ---------------------------------------------------------------------------
# allowlist: repo-relative path -> True (copy directory tree) / False (file).
# The release tree keeps the SAME relative paths as the repo — no nesting, no
# fake repo — so the release root reads like the product.
# ---------------------------------------------------------------------------

ALLOWLIST = {
    # Root entry points (user-facing). The guided wizard is canonical at
    # installer/Install-SceneStudio.ps1 in the repo; the release places a copy
    # at the root because that is the friend-facing entry point.
    "README.md": False,
    "VERSION": False,
    ".scene-studio.local.example.json": False,
    # Installer tooling: guided installer, profile example/validator,
    # manifest verifier, safe deployers, docs.
    "installer": True,
    # Backend Python package (runtime core; stdlib-only).
    "backend/src/scene_studio": True,
    # Generic demo topology only.
    "backend/fixtures/demo": True,
    # Built Workbench SPA (npm run build must run first).
    "workbench/dist": True,
    # Built Home Assistant card (npm run build must run first).
    "home-assistant/scene-studio-card/dist": True,
    # Operator documentation shipped with the release.
    "docs/installation": True,
    "docs/architecture/ARCHITECTURE_CONTRACTS.md": False,
    "docs/operations/SECURITY.md": False,
}

#: repo-relative source -> release-relative destination for the root entry
#: point (copied after the allowlist trees so the root copy wins).
ROOT_ENTRY_POINTS = {
    "installer/Install-SceneStudio.ps1": "Install-SceneStudio.ps1",
}

TREE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")

# ---------------------------------------------------------------------------
# portability scan (deny patterns inherited from the monorepo portable-bundle
# builder, plus voice_ecosystem; NO filename exceptions — the standalone
# deployers must not reference the operator's private local config at all).
# ---------------------------------------------------------------------------

DENY_PATTERNS = [
    (re.compile(r"10\.0\.0\.\d+"), "Home-Tech LAN address"),
    (re.compile(r"a0dd6c938664", re.IGNORECASE), "Home-Tech WLED device id"),
    (re.compile(r"001788ffffff"), "Home-Tech Hue bridge id"),
    (re.compile(r"gennady", re.IGNORECASE), "personal fixture name"),
    (re.compile(r"catherine", re.IGNORECASE), "personal fixture name"),
    (re.compile(r"sunroom", re.IGNORECASE), "production room name"),
    (re.compile(r"_ha_tmp"), "private staging path"),
    (re.compile(r"voice_ecosystem", re.IGNORECASE), "Home-Tech local config reference"),
]

TEXT_SUFFIXES = {
    ".py", ".json", ".md", ".yaml", ".yml", ".ps1", ".js", ".css", ".html",
    ".svg", ".txt", ".toml", ".mjs", ".map",
}


def _is_text(path: Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return False
    try:
        path.read_bytes()[:4096].decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def portability_scan(release_root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(release_root.rglob("*")):
        if not path.is_file() or not _is_text(path):
            continue
        relative = path.relative_to(release_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, label in DENY_PATTERNS:
            for match in pattern.finditer(text):
                problems.append(f"{relative}: {label} {match.group(0)!r}")
    return problems


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------


def copy_allowlist(release_root: Path) -> list[str]:
    """Copy the allowlist into the release tree; returns release-relative paths."""
    copied: list[str] = []
    for source_rel, is_tree in ALLOWLIST.items():
        source = REPO_ROOT / source_rel
        if not source.exists():
            hint = ""
            if source_rel == "workbench/dist":
                hint = " (run `npm run build` in workbench/ first)"
            elif source_rel == "home-assistant/scene-studio-card/dist":
                hint = " (run `npm run build` in home-assistant/scene-studio-card/ first)"
            raise SystemExit(f"allowlist source missing{hint}: {source_rel}")
        target = release_root / source_rel
        if is_tree:
            shutil.copytree(source, target, dirs_exist_ok=True, ignore=TREE_IGNORE)
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    copied.append(path.relative_to(release_root).as_posix())
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(source_rel)
    for source_rel, dest_rel in ROOT_ENTRY_POINTS.items():
        source = REPO_ROOT / source_rel
        if not source.is_file():
            raise SystemExit(f"root entry point missing in the repo: {source_rel}")
        target = release_root / dest_rel
        shutil.copy2(source, target)
        copied.append(dest_rel)
    return sorted(set(copied))


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def gate_workbench_dist(release_root: Path) -> None:
    index = release_root / "workbench/dist/index.html"
    assets = release_root / "workbench/dist/assets"
    if not index.is_file():
        raise SystemExit("Workbench build gate failed: workbench/dist/index.html missing (run npm run build)")
    if not assets.is_dir() or not any(assets.glob("*.js")):
        raise SystemExit("Workbench build gate failed: workbench/dist/assets missing")


def gate_card_dist(release_root: Path) -> None:
    card = release_root / "home-assistant/scene-studio-card/dist/scene-studio-card.js"
    if not card.is_file():
        raise SystemExit(
            "Card build gate failed: home-assistant/scene-studio-card/dist/scene-studio-card.js missing"
            " (run `npm run build` in home-assistant/scene-studio-card/ first)"
        )


def gate_python_imports(release_root: Path) -> None:
    src = release_root / "backend/src"
    for path in sorted(release_root.rglob("*.py")):
        # ast-parse instead of py_compile: compiling would drop __pycache__
        # into the release tree and invalidate the manifest.
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import scene_studio.service,"
        "scene_studio.appdaemon_adapter.adapter,"
        "scene_studio.stores,"
        "scene_studio.discovery,"
        "scene_studio.renderers.plan;"
        "print('imports ok')"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=env,
        cwd=str(release_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Python import gate failed:\n" + (result.stdout + result.stderr).strip()[:2000]
        )


def gate_secret_scan(release_root: Path) -> None:
    scanner = REPO_ROOT / "scripts" / "security" / "scan_secrets.py"
    if not scanner.is_file():
        raise SystemExit(f"Secret scan gate failed: scanner missing: {scanner}")
    result = subprocess.run(
        [sys.executable, str(scanner), str(release_root)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise SystemExit("Secret scan gate failed:\n" + output[:2000])


def prune_build_artifacts(release_root: Path) -> None:
    """Remove any __pycache__/.pyc that a gate may have dropped into the tree."""
    for cache_dir in list(release_root.rglob("__pycache__")):
        shutil.rmtree(cache_dir, ignore_errors=True)
    for pyc in release_root.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def write_manifest(release_root: Path, relatives: list[str]) -> Path:
    lines = ["# Scene Studio release manifest", "# sha256  size  path", ""]
    for relative in relatives:
        path = release_root / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.stat().st_size}  {relative}")
    manifest = release_root / "MANIFEST.sha256"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh", action="store_true", help="delete an existing output tree before building"
    )
    parser.add_argument(
        "--out",
        default=str(DIST_DIR),
        help=f"output directory (default: {DIST_DIR}; the acceptance harness uses a temp dir)",
    )
    parser.add_argument("--zip", action="store_true", help="also write <out-dir-name>.zip next to the output")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    if out_dir.exists():
        if not args.refresh:
            raise SystemExit(f"{out_dir} exists; pass --refresh to rebuild it")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    copied = copy_allowlist(out_dir)
    print(f"release: copied {len(copied)} files from the allowlist")

    gate_workbench_dist(out_dir)
    print("release: workbench build present")
    gate_card_dist(out_dir)
    print("release: card build present")
    gate_python_imports(out_dir)
    print("release: python syntax + clean imports ok")

    problems = portability_scan(out_dir)
    if problems:
        print("release: PORTABILITY SCAN FAILED:", file=sys.stderr)
        for problem in problems[:80]:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)
    print("release: portability scan clean")

    gate_secret_scan(out_dir)
    print("release: secret scan clean")

    prune_build_artifacts(out_dir)
    print("release: pruned __pycache__/.pyc")

    # Single manifest covering EVERY file in the release tree, written last so
    # it can exclude itself; a tree without it is not shareable/verifiable.
    all_files = sorted(
        path.relative_to(out_dir).as_posix()
        for path in out_dir.rglob("*")
        if path.is_file() and path.name != "MANIFEST.sha256"
    )
    manifest = write_manifest(out_dir, all_files)
    print(f"release: manifest written ({manifest})")

    if args.zip:
        import zipfile

        zip_path = out_dir.parent / (out_dir.name + ".zip")
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(out_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(out_dir.parent).as_posix())
        print(f"release: zip written ({zip_path})")

    summary = {
        "release_root": str(out_dir),
        "files": len(all_files),
        "gates": [
            "workbench-dist",
            "card-dist",
            "python-imports",
            "portability-scan",
            "secret-scan",
            "prune",
            "manifest",
        ],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
