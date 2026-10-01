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

## Quick start (developer)

```powershell
git clone https://github.com/pHarmG/Scene-Studio.git
cd Scene-Studio

# backend tests
python -m pytest backend/tests -q

# workbench
cd workbench && npm install && npm run build && npm run smoke && cd ..

# guided install against your Home Assistant + AppDaemon host
pwsh ./Install-SceneStudio.ps1
```

## Install (non-developer)

Download `Scene-Studio-<version>.zip` from GitHub Releases, extract, and run:

```powershell
pwsh ./Install-SceneStudio.ps1
```

The wizard asks plain questions, probes everything read-only first, shows the
exact `apps.yaml` change for review, installs with automatic rollback on
failure, and — if it fails — writes a sanitized support-report ZIP you can
send to the maintainer.

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
