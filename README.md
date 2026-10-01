# Scene Studio

Provider-neutral lighting scenes for Home Assistant. Author scenes in the
Workbench, apply and play them back on Hue / WLED / Home Assistant lights,
control playback from an optional Lovelace card — installed and upgraded by a
guided wizard that treats your Home Assistant host carefully (read-only
probes, explicit review, backups, and automatic rollback).

- **Backend** — Python command engine + domain core (AppDaemon-hosted), stdlib-only.
- **Workbench** — static authoring SPA (Vite + Lit), served by the AppDaemon add-on.
- **HA card** — `custom:scene-studio-card`, an optional daily controller.
- **Installer** — guided wizard; models Home Assistant API, AppDaemon ssh, and
  the Scene Studio HTTP endpoint as three independent addresses.

## Install from GitHub Releases

Open [GitHub Releases](https://github.com/pHarmG/Scene-Studio/releases) while
signed in if the repository is private. Download `Scene-Studio-v<version>.zip`
and `SHA256SUMS.txt`. Compare the ZIP's `Get-FileHash -Algorithm SHA256` output
with the published checksum, extract the ZIP, then
run `pwsh ./Install-SceneStudio.ps1` from its `scene-studio-release` folder.
For the browser wizard, run `python installer/ui/server.py` from that folder.

Alternatively, download the **Get-SceneStudio.ps1** Release asset and run
`pwsh ./Get-SceneStudio.ps1`. This small bootstrap downloads the complete ZIP,
verifies its checksum and manifest, then opens the same guided installer.
Use `-Version 0.1.0` to select a release, or `-DownloadOnly` to retrieve the
verified tree without opening the wizard. Private downloads optionally use
`SCENE_STUDIO_GITHUB_TOKEN` in the process environment with Contents read
permission; otherwise use the signed-in manual ZIP download. The standalone
canonical installer requires the adjacent support tree and is bundled in the
ZIP, rather than offered as an incomplete standalone asset.

## Quick start (developer/operator clone)

```powershell
git clone https://github.com/pHarmG/Scene-Studio.git
cd Scene-Studio

# backend tests
python -m pytest backend/tests -q

# workbench
cd workbench && npm install && npm run build && npm run smoke && cd ..

# guided install against your Home Assistant + AppDaemon host
pwsh ./installer/Install-SceneStudio.ps1
# ...or the same guided installer with a browser UI (Python 3.9+, any OS):
python installer/ui/server.py
```

## Install (non-developer)

Download the verified `Scene-Studio-v<version>.zip` from GitHub Releases as
described above, extract, and run from the product folder:

```powershell
pwsh ./Install-SceneStudio.ps1
```

or, for the same guided installer with a browser UI:

```bash
python installer/ui/server.py
```

The wizard asks plain questions, probes everything read-only first, shows the
exact `apps.yaml` change for review, installs with automatic rollback on
failure, and — if it fails — writes a sanitized support-report ZIP you can
send to the maintainer.

Workbench **System → Product** shows the installed semantic version, channel,
source SHA, local modifications, and build timestamp. **System → Update → Check
for updates** checks stable complete GitHub Releases on demand. No check runs
on page load. A check or download never installs anything; upgrades use the
same guided installer, review, backups, verification and rollback as installs.
Optional private update access reads `SCENE_STUDIO_GITHUB_TOKEN` only in the
AppDaemon server process environment. It is never required to run Scene Studio
and is never sent to the browser. Without access, the check is unavailable.

## Documentation

| Path | Contents |
| --- | --- |
| `AGENTS.md` | Agent/operator guide: architecture, tests, deployment safety. |
| `docs/architecture/` | Contracts between backend, Workbench, and the HA card. |
| `docs/installation/` | Install walkthrough, AppDaemon secrets template, profile example. |
| `docs/development/` | Implementation status, plans, runbooks. |
| `docs/operations/` | Contention, rollout history, security notes. |
| `docs/MIGRATION_FROM_HOME_TECH.md` | Provenance: where this project came from. |

## Security

Run `python scripts/security/scan_secrets.py` before committing. Release
artifacts are generated only by `python scripts/build_release.py`, which
enforces allowlist copying, a topology portability scan, a secret scan, and
clean-import gates, then writes a byte-exact `MANIFEST.sha256`.
