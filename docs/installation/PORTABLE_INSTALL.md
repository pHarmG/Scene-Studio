# Scene Studio — Portable Install & Rollback Guide (advanced reference)

This bundle is a clean, generated distribution of Scene Studio: the
AppDaemon-hosted lighting scene engine plus its Workbench web UI. It contains
no Git history, no one else's credentials, and no recorded private device
payloads — you configure your own Home Assistant, providers, and devices.

> **Most users do not need this file.** Run `Install-SceneStudio.ps1` from
> the ZIP root (see `START-HERE.md`): it asks the same questions, performs
> the same checks, backs up and mutates `apps.yaml`/`secrets.yaml` for you,
> and drives the installer documented below. This guide remains the
> reference for the advanced JSON profile, the noninteractive answers file,
> and the exact rollback anatomy.

Audience: a technically capable operator running **Home Assistant with the
AppDaemon add-on**, installing from a workstation with **PowerShell 7**,
**ssh access to the HA host**, and **Python 3.9+**.

## Deployment model

Two roles are independent and separately configured:

- **Deployment workstation** — the machine running the installer (this ZIP).
  Needs PowerShell 7, Python 3.9+, and an OpenSSH client. Does NOT need
  Home Assistant or AppDaemon locally. Holds the extracted bundle and the
  generated deployment configuration (temp dir).
- **Remote target** — the Home Assistant + AppDaemon host. Reached over SSH
  (AppDaemon filesystem deployment) and the Home Assistant HTTP API (health
  and AppDaemon restart). The API address and the SSH host/port/user are
  configured independently and may differ.

### Noninteractive wizard answers file

`Install-SceneStudio.ps1 -Unattended -Answers <file> [-Yes]` runs the guided
flow without prompts. Keys: `ha_url` (required), `ssh_host` (required),
`ssh_user`, `ssh_port`, `appdaemon_config_root` (null = auto-detect),
`addon_root_choice`, `store_root`, `providers` (`ha_light` bool; `hue`/`wled`/
`hyperhdr` objects with `enabled`/`host`, hue also `bridge_id`),
`confirm_install`. Secrets are NEVER in the file: the wizard reads
`SCENE_STUDIO_HA_TOKEN` (and `SCENE_STUDIO_HUE_APP_KEY` when Hue is enabled)
from the environment.

---

## 0. What you need before starting

| Requirement | Notes |
| --- | --- |
| Home Assistant + AppDaemon add-on | AppDaemon 4.5+ serving its HTTP port (default `5050`) |
| ssh access to the HA host | The installers run remote commands over ssh (key auth recommended) |
| AppDaemon add-on config directory | e.g. `/addon_configs/a0d7b954_appdaemon` — find your exact slug on the HA host under `/addon_configs` |
| A Home Assistant long-lived access token | Export as `SCENE_STUDIO_HA_TOKEN` for install health checks |
| Provider access (optional per provider) | Hue bridge CLIP v2 application key and/or a reachable WLED controller. HA lights need nothing extra. |
| PowerShell 7 (`pwsh`) on the workstation | Windows PowerShell 5.1 is NOT supported (it corrupts ssh payloads) |

Verify the bundle first (optional but recommended): `MANIFEST.sha256` lists
every file with its SHA-256.

---

## 1. Create your deployment profile

Copy `installer/scene-studio-profile.example.json` to e.g.
`my.profile.json` (keep it OUT of the bundle) and fill in your values:

- `ha_url` — your Home Assistant URL.
- `ha_ssh_host` — the ssh alias/host for the HA machine.
- `appdaemon_config_root` — the add-on config directory on the HA host.
- `store_root` — where the Scene Studio registry/scenes persist (a fresh
  path, e.g. `/config/scene_studio_store`).
- `runtime_mode` — keep `registry_admin` for the first install: discovery
  and registry setup work, but every provider write is blocked.
- `providers` — enable what you have. A Hue bridge needs its host, a secret
  NAME for the application key, and (recommended) the bridge hardware id
  (`bridge_id`) so first-run adoption of Hue lights can derive bindings.
  WLED takes one controller host. `ha_light` is enabled by default.
- `integrations.hyperhdr` — optional external light-sync contention support;
  leave disabled unless you run hyperHDR.

Validate it (fails loudly on unknown keys, placeholder values, or credential-
shaped values — the profile holds secret NAMES, never secret values):

```powershell
python installer/scene-studio-profile.py validate --profile my.profile.json
```

---

## 2. Preflight (read-only)

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File installer/deploy_scene_studio.ps1 -Profile my.profile.json
```

This validates the profile, prints the resolved deployment targets, prints
the generated `scene_studio:` apps.yaml block, and checks ssh reachability
and the AppDaemon config directory. Nothing is written.

---

## 3. Merge the AppDaemon configuration (manual, deliberate)

1. Edit the add-on's `secrets.yaml` (template:
   `docs/APPDAEMON_SECRETS_EXAMPLE.yaml`): add the secret names your profile
   references (e.g. `hue_app_key: "<your Hue CLIP v2 application key>"`).
2. Merge the printed `scene_studio:` block into the add-on's `apps.yaml`.
   Do not point `store_root` at an existing unrelated directory.
3. Keep `registry_admin: true` for now.

(The installer never edits `apps.yaml`/`secrets.yaml` — configuration stays
yours, in your hands, reviewable before any restart.)

---

## 4. Install

```powershell
$env:SCENE_STUDIO_HA_TOKEN = "<long-lived HA token>"
pwsh -NoProfile -ExecutionPolicy Bypass -File installer/deploy_scene_studio.ps1 -Profile my.profile.json -Apply
```

- The backend deployer backs up the previous Python package (if any), stages
  and hash-verifies the new one, restarts the AppDaemon add-on, health-checks
  the API, and **rolls back automatically** if the health check fails.
- The Workbench deployer does the same for the static SPA (asset-only; no
  restart).

---

## 5. First run: build your registry (Setup view)

Open `http://<ha-host>:5050/local/scene_studio/` — served by AppDaemon,
same origin as the API. With an empty registry the Workbench lands on the
**Setup** view:

1. Runtime health (providers read-only).
2. Run discovery — your devices appear as observations.
3. Adopt fixtures: choose the stable id, display name, and initial groups;
   the server derives bindings/capabilities from the discovery observation.
4. Create targets (rooms/groups) and assign members.
5. Author a scene in the Builder, then **preview/dry-run** it.
6. When your registry is deliberately ready: set `registry_admin: false` and
   `read_only: false` in `apps.yaml`, restart AppDaemon, and apply for real.

Nothing in Setup writes to any light, in any mode.

---

## 6. Rollback / removal

- **Reinstall safely**: re-running `-Apply` re-backs-up and re-verifies; the
  store (`store_root`) is never touched by deploys.
- **Backend rollback**: each deploy keeps
  `<appdaemon_config_root>/backups/scene-studio-backend-<stamp>/` with the
  previous package (`live-before/`). Restore it and restart AppDaemon.
- **Workbench rollback**: same pattern under
  `backups/scene-studio-workbench-<stamp>/` (asset-only; no restart needed).
- **Disable without deleting**: remove (or comment out) the `scene_studio:`
  block from `apps.yaml` and restart AppDaemon. Everything stops; your store
  and scenes remain on disk at `store_root` for a later re-install.
- **Full removal**: after disabling, delete `store_root`,
  `<appdaemon_config_root>/apps/scene_studio`, and
  `<appdaemon_config_root>/www/scene_studio`. No other Home Assistant
  configuration is created or modified by this bundle.

---

## 7. Upgrades

Replace the bundle with a freshly generated one and re-run the installer.
The backend deployer refuses to change your runtime mode: `apps.yaml`
(also `registry_admin`/`read_only`) is yours, and a post-restart mode change
fails the deploy and rolls back.
