# Scene Studio — Security & Credentials (Phase 0)

Created: 2026-09-10 (America/Chicago)
Status: repo-side preparation **complete**; live rotation **deployment-pending** (requires user approval per `AGENTS.md`).

## Summary

During Phase 0 of the Scene Studio rebuild, live credentials were found **tracked in git** in
runtime snapshots and historical captures. All tracked working-tree copies have been sanitized
and a shape-based secret scanner (`scripts/security/scan_secrets.py`) is now the repo-side gate.
The secret **values remain in git history**, so live rotation is mandatory before these
credentials can be considered safe.

## Credential inventory requiring live rotation

| # | Credential | Where it was found (tracked) | Rotation status |
| --- | --- | --- | --- |
| 1 | HA long-lived token **A** (JWT, iss `b032e0bd…`, iat 2025-06, exp 2035) | `ha_legacy_staging/appdaemon_scene_tools/apps.yaml`, `live-pulled-20260910/appdaemon.yaml`, `live-pulled-20260910/apps/apps.yaml` | **deployment-pending** — rotate in HA (Profile → Security → Long-lived access tokens), then redeploy via AppDaemon secrets |
| 2 | HA long-lived token **B** (JWT, iss `54fb25cf…`) | `archive/voice_capture/log_captures_2026-03/pi3_lva_20260305-105723.txt`, `card_workbench/Sample Live HTML/Office Control – Home Assistant.html` | **deployment-pending** — same rotation procedure |
| 3 | Hue Bridge application key (`REDACTED-HUE-APPLICATION-KEY` in tracked copies) | `ha_legacy_staging/appdaemon_scene_tools/apps.yaml`, `live-pulled-20260910/apps/apps.yaml` | **deployment-pending** — delete/regenerate the application on the bridge (CLIP: delete `auth` resource or use bridge settings) |

Notes:

- Repo remote trust boundary: this repository must be treated as having crossed any private
  trust boundary for these values. If the repo is ever pushed anywhere new, rotation is a
  precondition, not an option.
- The source monorepo's gitignored local config also contained a live HA token (never
  committed). Rotation of tokens A/B above should consider whether that token is one of them.
  In the standalone repository, tokens are provided per-run through environment variables
  (`SCENE_STUDIO_HA_TOKEN`) and are never stored in tracked files or profiles.
- Internal IPs (Hue `hue-bridge.local`, WLED `wled.local`, HA `homeassistant.local`, snapshot latitude/longitude)
  remain in tracked files. They are LAN topology, not credentials; flagging them is out of scope
  for this phase. The snapshot's `latitude`/`longitude` in `appdaemon.yaml` is a real-world
  location — consider changing to an approximate location on the live system at rotation time.

## What was sanitized (2026-09-10)

| File | Change |
| --- | --- |
| `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/appdaemon.yaml` | HA token → `REDACTED-HA-LONGLIVED-TOKEN`, sanitization note added |
| `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/apps/apps.yaml` | HA token (2×) + Hue key (4×) → `REDACTED-*`, note added |
| `ha_legacy_staging/appdaemon_scene_tools/apps.yaml` | HA token (2×) + Hue key (4×) → `REDACTED-*`, note added |
| `archive/voice_capture/log_captures_2026-03/pi3_lva_20260305-105723.txt` | 3 JWT occurrences → redaction marker |
| `card_workbench/Sample Live HTML/Office Control – Home Assistant.html` | 1 embedded JWT → redaction marker |

## Secret scanner (repo-side gate)

```powershell
python scripts/security/scan_secrets.py          # git-tracked files; exit 1 on findings
python scripts/security/scan_secrets.py --json   # machine-readable
python scripts/security/scan_secrets.py --all    # include gitignored files
python scripts/security/scan_secrets.py <path>   # subtree scan
```

- Detectors are shape-based (JWT segments, credential-key assignments) with a placeholder
  allowlist — the scanner never contains real secret values.
- Validation rule for Scene Studio work: run the scanner before every commit; a failing scan
  blocks the commit (per the master plan §4 and §17).
- Live re-pulls (AppDaemon configs, HA `.storage`, device dumps) must be sanitized **before**
  they touch a tracked path.

## Runtime credential target state (deployment-pending)

AppDaemon supports `!secret` references resolved from a `secrets.yaml` that lives outside the
repo. The live cutover (master plan §15, rollout R1/R4) must switch `appdaemon.yaml` /
`apps.yaml` to secret references. Target layout:

- `/addon_configs/a0d7b954_appdaemon/secrets.yaml` (never tracked):

  ```yaml
  ha_token: "<live long-lived token>"
  hue_app_key: "<live Hue application key>"
  ```

- `appdaemon.yaml` / `apps.yaml` (tracked, safe):

  ```yaml
  token: !secret ha_token
  hue_username: !secret hue_app_key
  ```

Reference template: [APPDAEMON_SECRETS_EXAMPLE.yaml](APPDAEMON_SECRETS_EXAMPLE.yaml).

## Workbench diagnostic export rule (standing contract)

The future Workbench "export sanitized diagnostic snapshot" feature **must never emit**
tokens, application keys, or other credentials. Sanitization helpers for exports are part of
the Phase 1 domain contracts (`scene_studio.domain.sanitize`) and are covered by unit tests.
Any new code path that serializes engine/provider state for download or sharing must route
through them.

## Rotation procedure (when approved)

1. HA: create a new long-lived token; update `secrets.yaml` on the AppDaemon addon; restart
   AppDaemon; verify `scene_studio` API health. Then revoke the old token(s).
2. Hue: create a new application key via CLIP bootstrap (press bridge link button, `POST
   /clip/v2/registration`); update `secrets.yaml`; verify bridge calls; delete the old
   application key.
3. Update this file: move rows from "deployment-pending" to rotated with date.
4. Re-run `python scripts/security/scan_secrets.py` after any live pull lands in the repo.
