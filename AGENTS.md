# Scene Studio — Agent Guide

Scene Studio is a provider-neutral lighting-scene product for Home Assistant:
a Python domain core + command engine (AppDaemon-hosted), a static Workbench
SPA for authoring, an optional Lovelace control card, and a guided installer
for fresh installs and upgrades.

Source of truth: **this repository** (`pHarmG/Scene-Studio`, local workspace
`D:\Scene-Studio`). The project was extracted from the `pHarmG/Home-Tech`
monorepo; see `docs/MIGRATION_FROM_HOME_TECH.md`. Do **not** recreate or edit
Scene Studio implementation in Home-Tech.

## Layout

| Path | What it is |
| --- | --- |
| `backend/` | Python package (`src/scene_studio`), fixtures, full test suite. Stdlib-only core; the AppDaemon adapter lazily imports `requests` + AppDaemon. |
| `workbench/` | Static authoring SPA (Vite + Lit). `npm run build|smoke|browser`. Mocks mirror `backend/fixtures/*.sample.json`. |
| `home-assistant/scene-studio-card/` | Optional Lovelace card (`custom:scene-studio-card`; `test-bench-scene-controls-card` is a compatibility alias). Build: `npm run build`, tests: `npm test`. |
| `installer/` | Guided wizard (`Install-SceneStudio.ps1`), browser wizard (`ui/server.py` + `ui/static/`), internal installer, deployers, profile tool, manifest verifier. |
| `scripts/` | `build_release.py` (release ZIP packager), `security/scan_secrets.py` (secret scanner — run before every commit). |
| `tests/` | Cross-cutting/installer acceptance support. |
| `docs/` | `architecture/`, `development/`, `installation/`, `operations/`, `design/`. |

## Ownership boundaries

- **Backend** (`backend/src/scene_studio`): domain, stores, discovery,
  renderers, command service, AppDaemon adapter. Scene authoring logic lives
  here — never in Lovelace.
- **Workbench** (`workbench/`): the ONLY authoring surface. Consumes the
  backend HTTP/AppDaemon API; its mock layer mirrors backend fixtures.
- **HA card** (`home-assistant/scene-studio-card/`): a daily CONTROLLER
  (select/apply/playback/brightness trim/archive) over the canonical
  projection + command bridge (`sensor.scene_studio_ui`,
  `scene_studio_ui_command`). It never authors scenes and never touches
  providers directly.
- **Installer** (`installer/`): the only code that writes to a target
  Home Assistant/AppDaemon host. Deployers keep backup → stage → hash-verify
  → activate → health-check → rollback semantics; do not bypass them.
- **Installer UI** (`installer/ui/`): a local, cross-platform browser front-end
  for the guided wizard (Python stdlib server + static SPA, loopback-only,
  session-token protected). It implements the wizard's read-only probes 1:1
  for structured display but performs NO installation logic itself: apply
  spawns `Install-SceneStudio.ps1 -Unattended -Answers` so review, backups,
  rollback, and support-report semantics stay in the wizard. Secrets (HA
  token, Hue key) live in the server process memory or environment
  variables only — never in files, logs, or API responses.

## Standard commands

Root `VERSION` is the canonical semantic product version. `scripts/version.py`
validates backend pyproject and Workbench package/lock declarations against it;
`__version__` derives from build metadata. Vite builds and release packaging run
this gate. Change those declarations together when changing VERSION.

`/status.product.build` contains `version`, `source_sha`, `short_sha`, `channel`
(`release` or `local`), `dirty`, `tag`, `source_tree_sha256`, and `built_at`.
The source fingerprint distinguishes different local patches at the same SHA.
Generated `BUILD.json`,
backend `build-info.json`, and Workbench `build-info.json` freeze the identity.
Deployers copy and hash-check that metadata with the payload. A local patch
retains the semantic version and identifies its source SHA/dirty state.

Create an official release by committing a version change, tagging that clean
commit `v<VERSION>`, and pushing the tag. `.github/workflows/release.yml` runs
the standard validations, builds with `SCENE_STUDIO_BUILD_CHANNEL=release` and
`SCENE_STUDIO_RELEASE_TAG=v<VERSION>`, then publishes only after packaging and
manifest verification. The tag must match VERSION and HEAD; dirty release
builds fail. Local `build_release.py` builds are labeled local.

GitHub Release assets: `Scene-Studio-v<version>.zip` (complete authoritative
payload with root wizard), `SHA256SUMS.txt`, and `Get-SceneStudio.ps1` (download
bootstrap only). Never upload the support-dependent wizard alone. Bootstrap
verifies checksum, archive paths, manifest, and release identity before delegation.

`GET /status?check_updates=true` explicitly checks stable complete GitHub Releases;
normal status polling never calls GitHub. `/status.product.update` carries the
result. GitHub access is isolated in `updates.py`. Optional server-only
`SCENE_STUDIO_GITHUB_TOKEN` uses Contents read permission. No credential is
required for Scene Studio; absent private access means unavailable, never current.
Tokens stay in process environments and must never enter assets, responses,
logs, profiles, diagnostics, or support ZIPs. Workbench links to the signed-in
Release page for intentional downloads; checking/downloading is distinct from
applying. Every live apply still requires the exact approved installer change.

```powershell
# backend tests (from backend/ or repo root)
python -m pytest backend/tests -q

# workbench (from workbench/)
npm install && npm run smoke && npm run build && npm run browser

# HA card (from home-assistant/scene-studio-card/)
npm install && npm run build && npm run typecheck && npm test

# installer UI acceptance tests (from repo root)
python -m pytest tests/installer/test_installer_ui.py -q

# installer UI (browser wizard; from repo root or release root)
python installer/ui/server.py

# release ZIP (from repo root)
python scripts/build_release.py --refresh --zip

# secret scan (required before every commit)
python scripts/security/scan_secrets.py
```

## Installer architecture

- `Install-SceneStudio.ps1` (wizard): asks plain questions, probes read-only,
  shows a review, then drives `installer/deploy_scene_studio.ps1` and the
  two deployers. Models **three independent endpoints**: HA API URL, AppDaemon
  ssh target, and the Scene Studio/AppDaemon HTTP endpoint (inferred from the
  ssh target + port 5050, tested, asked only when inference fails). HA Core
  and AppDaemon do not have to share a host.
- The wizard writes only: a marker-delimited `scene_studio` block in
  `apps.yaml`, optionally a named secret in `secrets.yaml` (values never
  displayed), the backend/Workbench trees via the deployers, and — if opted
  in — the prebuilt card into `/config/www/scene-studio-card/` (it never
  modifies a dashboard).
- Fresh installs must come up in `registry_admin` (provider writes blocked);
  upgrades preserve the live runtime mode.
- Deployment profiles are validated by `installer/scene-studio-profile.py`
  (validate / resolve / render-apps-yaml). Secrets stay NAMES there, never
  values.
- `installer/verify_release_manifest.ps1` gates release trees byte-exactly.

## Deployment safety rules

- **PowerShell 7 only** for every installer/deployer: PS 5.1 strips embedded
  quotes when marshalling ssh arguments and corrupts remote JSON payloads.
- All remote writes are opt-in (`-Apply`) and gated: read-only state
  classification first, backups before every replace, hash verification after
  staging AND activation, automatic rollback on failed health checks. Fresh
  installs quarantine to the absent state.
- Deployers never touch `apps.yaml`, stores, dashboards, or HA Core config.
- Never store HA tokens or provider keys in tracked files, profiles, or the
  answers file — environment variables only (`SCENE_STUDIO_HA_TOKEN`,
  `SCENE_STUDIO_HUE_APP_KEY`).

## Local environment discovery

**When `.scene-studio.local.json` exists in the repo root, treat it as the
authoritative local development/deployment topology.** It is machine-specific
and must never be committed (it is gitignored). `.scene-studio.local.example.json`
is the tracked placeholder template. The local profile may contain this
house's real hosts/aliases; generic code must never hardcode them. Tokens and
keys do NOT belong there — read them from the environment.

Read `.scene-studio.local.json` before working against the real environment;
if it is absent, ask the user or work in mock/dev mode only.

## HA write policy (mandatory)

1. **Read-only first**: inspect the live state (status APIs, `-VerifyOnly`
   deployer mode, profile preflight) before proposing any write.
2. **Explicit approval**: an agent may run any `-Apply` / wizard install
   against a live host only after the user has explicitly approved THAT write
   in the conversation. Present the exact intended change (files, paths,
   restart effect) as part of the approval request. A general "go ahead"
   covers only the change described when it was given.
3. **Minimal change set + immediate verification** after every approved write;
   the deployers' health checks are part of the write, not optional.

## Environment classes

- **Local development**: mock-mode Workbench (`npm run dev`), devserver
  (`python -m scene_studio.devserver --seed-demo`), unit/interaction tests.
  No HA contact.
- **Friend installation**: the release ZIP (GitHub Releases) or a clone +
  `pwsh ./installer/Install-SceneStudio.ps1`. The wizard's support-report ZIP (sanitized)
  is the failure channel back to maintainers.
- **Release/distribution**: `python scripts/build_release.py --refresh --zip`
  runs the allowlist copy + portability + secret + import gates and writes
  `MANIFEST.sha256`. Never hand-edit a release tree.

Generic product code must never hardcode any house's topology — that belongs
only in the gitignored local operator profile.
