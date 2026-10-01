# Scene Studio Pass 1 — production unlock cutover

Status: **executed 2026-09-15 (America/Chicago)** — production unlock is live; see "Live result" at the bottom.

Plan: `.cursor/plans/scene_studio_product_completion_pass1_production_unlock.plan.md`

## Pre-cutover live snapshot (read-only, 2026-09-14 evening, America/Chicago)

Captured from `HA` without writes.

- **Addon:** `a0d7b954_appdaemon`
- **Runtime:** `mode=read_only`, `read_only=true`, `provider_writes_blocked=true`
- **Allowed commands:** `diagnostics.export`, `discovery.run`, `fixture.rebind_preview`, `fixture.reconcile_preview`, `fixture.retry`, `registry.migration_preview`, `scene.apply:dry_run`, `scene.preview`
- **Fixtures:** 22 ready / 3 disabled / 0 degraded / 0 missing of 25
- **Providers:** Hue 15/18 ready (3 other), WLED 6/6 ready, HA light 1/1 ready
- **Playback:** idle (`sessions=[]`, all counts 0); **no** `playback_state.json` on disk
- **Scenes (7):** `banana_strawberry`, `code_focus`, `meeting_blue`, `pink_and_orange_dream`, `tropical_smoothie`, `twilight`, `winter_beauty`
- **Provider state present:** `scene_studio_store/provider_state/hue_zones.json` (R5 retained SS zones)
- **Workbench index SHA-256:** `558599c5b89e121806d53ad256b8ff8164d68838df82f45d85b1b48d89006ba9`
- **`apps.yaml` SHA-256:** `18d9a99e0feee2071b0b03bdc33cabd304db85582839e526b5737b62be67163c`
- **Live `scene_studio` block (secrets by reference only):**

```yaml
scene_studio:
  module: scene_studio.appdaemon_adapter.adapter
  class: SceneStudioApp
  read_only: true
  store_root: /config/scene_studio_store
  hue_ip: hue-bridge.local
  hue_username: !secret hue_app_key
  wled_host: wled.local
```

- **Also running (unchanged this pass):** `generate_crosswalk`, `scene_manager`, `delete_scene`, `save_light_states`, `apply_scene`, `generate_office_scene`, `voice_stack_control`
- **Last recorded live backend deploy:** `fef39aa` (reconcile rollout). Repo now carries the Pass 1 adapter seam; live package is behind until the backend deployer runs.

## Intended production configuration

```yaml
scene_studio:
  module: scene_studio.appdaemon_adapter.adapter
  class: SceneStudioApp
  read_only: false
  legacy_events_enabled: false
  store_root: /config/scene_studio_store
  hue_ip: hue-bridge.local
  hue_username: !secret hue_app_key
  wled_host: wled.local
```

Must remain **absent:** `registry_admin`, `r2_validation`, `r5_validation`, and any fixture allowlist. `legacy_events_enabled: false` while the legacy AppDaemon apps above are still active.

Workbench static assets do **not** need a redeploy for this pass (no shipped SPA change).

## Order (safety)

Do **not** flip `read_only: false` on the currently deployed backend. That older package registers legacy listeners whenever it is not read-only / validation / admin, which would double-consume events with the still-running legacy apps.

1. Timestamped backup of store + `apps.yaml` + live backend package.
2. Deploy the Pass 1 backend **while still `read_only: true`**. The deployer
   itself fails and rolls back if post-restart `status().runtime.mode` is not
   the captured pre-activation mode (`read_only → read_only` at this step).
3. Patch only the `scene_studio:` block as shown above.
4. Restart AppDaemon.
5. Live acceptance (section below). Abort and roll back on unexplained failure.

## Apply commands (after explicit HA-write approval)

Working directory: the Scene Studio workspace. Provide `HA_TOKEN` (or `SCENE_STUDIO_HA_TOKEN`) through the environment, or the gitignored local operator profile for non-secret topology.

### 1. Backup

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
ssh HA "cd /addon_configs/a0d7b954_appdaemon && \
  sudo tar czf backups/pre-pass1-normal-\$TS.tar.gz \
    apps/scene_studio apps/apps.yaml scene_studio_store && \
  sudo sha256sum backups/pre-pass1-normal-\$TS.tar.gz && \
  sudo cp -p apps/apps.yaml apps/apps.yaml.bak-pass1-\$TS"
```

Record the archive path, SHA-256, and `apps.yaml.bak-pass1-*` name in this file after the run. The backend deployer also writes `backups/scene-studio-backend-<stamp>/` (`apps-scene_studio-before.tar.gz` + `live-before/`).

### 2. Backend deploy (still read-only)

The backend deployer is package-only: it does not patch `apps.yaml`. It
captures `status().runtime.mode` immediately before activation and treats a
post-restart mismatch as deployment failure (existing rollback path). The
pre-cutover package deploy is therefore guaranteed `read_only → read_only`
by the deployer itself, not by a hardcoded read-only health check.

```powershell
.\scripts\deploy\ha\deploy_scene_studio_backend.ps1 -Apply
```

If this step succeeds, live mode is still `read_only`. `-VerifyOnly` remains
useful after or before apply: it checks package equivalence and API health
for whatever mode is live and does not assume `read_only`.

Do not patch `apps.yaml` until the package deploy has preserved `read_only`.

### 3. Patch `apps.yaml`

In `/addon_configs/a0d7b954_appdaemon/apps/apps.yaml`, change **only** the `scene_studio:` block: set `read_only: false` and add `legacy_events_enabled: false`. Every other byte of the file must stay the same. Re-hash; the delta must be confined to that block.

### 4. Restart AppDaemon

Restart add-on `a0d7b954_appdaemon` through the authenticated HA API (`hassio.addon_restart`). Wait until `scene_studio_api` returns HTTP 200.

## Live acceptance (abort and roll back on unexplained failure)

### A. Runtime gate

```text
runtime.mode == normal
runtime.read_only == false
runtime.provider_writes_blocked == false
allowed_commands includes scene.apply, playback.start/pause/resume/stop,
  scene.rename, scene.archive, scene.restore
```

AppDaemon log: `SceneStudioApp initialized NORMAL: ... legacy listeners off`. No ERRORs attributable to this change. Legacy apps still started. Fixture counts match the pre-cutover baseline except for explainable live device availability.

### B. Non-writing render check

`scene.preview` or `scene.apply` with `dry_run: true` on a known migrated scene (`twilight` or `meeting_blue`). Confirm the resolved fixture set before touching devices.

### C. Small canary writes

Use previously proven canaries (`g_strip`, `wled_seg_0` / `lamp` as in R2/R5). Capture pre-test state, apply a narrowed target, verify readback, restore baselines, confirm unrelated WLED segments untouched.

### D. Normal scene apply

Apply one existing migrated scene from the **production Workbench**. Confirm the button is enabled by backend policy, the API succeeds, skipped/disabled fixtures are reported, and current-scene state updates.

### E. Dynamic playback lifecycle

Start one existing dynamic scene from the Workbench, then Pause / Resume / Stop using the returned `session_id` only. No sessionless shortcuts.

### F. Catalog mutation sanity

Rename and restore the original name, or archive/restore a suitable test scene, through the Workbench.

## Rollback

1. Restore `apps.yaml` from `apps.yaml.bak-pass1-<stamp>` (or the tarball). That returns `read_only: true` and removes the production flags.
2. If the new backend must also be reverted: restore `apps/scene_studio` from `backups/scene-studio-backend-<stamp>/live-before` (or the Pass 1 tarball).
3. Restart AppDaemon.
4. Confirm `mode=read_only`, `provider_writes_blocked=true`, `scene.apply` / `playback.start` conflict, and fixture/provider counts are coherent.

Keep this rollback path even after acceptance.

## Live result

Executed 2026-09-15 (America/Chicago) with explicit user approval. Implementation
commit deployed: `4e75f0c` (includes Pass 1 unlock seam, Pass 2 authoring commands,
and the corrective repo pass). Deployed SHA-256s below were verified by the
deployers' hash manifests.

### Backups (kept; also listed in IMPLEMENTATION_STATUS.md)

- Pre-cutover full snapshot: `HA:/addon_configs/a0d7b954_appdaemon/backups/pre-pass1-normal-20260915T150502Z.tar.gz`
  (SHA-256 `25cd3cadfdcfb22de88664b7651bd411c5462406bbbf723538b01cd8655b3441`;
  covers `apps/scene_studio`, `apps/apps.yaml`, `scene_studio_store`)
- `apps.yaml` copy: `HA:.../apps/apps.yaml.bak-pass1-20260915T150502Z` (pre-patch SHA-256 `18d9a99e...` — matches the pre-cutover record above)
- Backend deployer backup: `HA:.../backups/scene-studio-backend-20260915T150943Z/` (`apps-scene_studio-before.tar.gz` SHA-256 `6d9db6b9...` + `live-before/`)
- Workbench deployer backup: `HA:.../backups/scene-studio-workbench-20260915T152127Z/` (`www-scene_studio-before.tar.gz` SHA-256 `078c0115...` + `live-before/`)

### Execution record

1. **Backend deploy (`deploy_scene_studio_backend.ps1 -Apply`)**: succeeded.
   Deployer-captured transition `read_only → read_only` (pre-deploy mode read,
   post-restart mode verified identical, remote package hash-verified against
   the repo). First attempt aborted safely BEFORE activation because Windows
   PowerShell 5.1 stripped embedded `"` while marshalling the deployer's
   remote `python3 -c` snippet to ssh; live was untouched (no `live-before/`
   move, staging cleaned). Fix: the snippet now passes glob/encoding as argv
   (quote-free remote command), and the deployer runs under PowerShell 7
   (`pwsh`), which marshals the curl-status quoting correctly.
2. **`apps.yaml` patch**: only the `scene_studio:` block changed —
   `read_only: true` → `read_only: false` + `legacy_events_enabled: false`
   (diff verified against the `.bak-pass1-*` copy; no
   `registry_admin`/`r2_validation`/`r5_validation`/allowlist keys).
   Post-patch SHA-256 `f0e873faedcb2390d56dc9237a491ce195b5a56b39ae7fbf1a4e7cc9ecd58262`.
3. **AppDaemon restart** via authenticated `hassio.addon_restart`; all 8 apps
   restarted (7 legacy + `scene_studio`).

### Runtime after unlock

- `runtime.mode == normal`, `read_only == false`, `provider_writes_blocked == false`
- `allowed_commands` equals the full canonical `COMMAND_CATALOG` (25 commands)
- Log: `SceneStudioApp initialized NORMAL: command policy normal; provider writes enabled; legacy listeners off`
  and `mode=normal, read_only=False, legacy_events=False` — exactly one
  SceneStudioApp instance; the 7 legacy apps started untouched.
- Fixtures/providers match the pre-cutover baseline (25 total, 22 ready,
  3 disabled; Hue 15/18 ready, WLED 6/6, HA light 1/1).

### Acceptance outcomes

- **Non-writing render check** (`scene.preview` twilight): 13 planned,
  `double_strip` skipped, native 7 / equivalent 6 — matches the R4 baseline.
- **Static apply** (`scene.apply` twilight): ok, 13 planned / 12 ops executed /
  1 failed receipt — the single failure is the live Hue device `custom_gradient`
  rejecting the PUT (HTTP 207 device error), surfaced honestly as a per-fixture
  error event. In-scope canaries (`light.lamp`, `light.lg_wled_segment_0`)
  changed to twilight values; out-of-scope `light.food_light` untouched;
  canaries restored to pre-test state afterwards.
- **Catalog mutation**: `winter_beauty` renamed and restored (id stable,
  `migrated_from_v1` provenance intact through both renames).
- **Dynamic playback lifecycle** (Builder-created `cutover_test_scene`, since
  no migrated scene is dynamic): `playback.start` created session
  `sess-20260915T152406-eb4e44037f6f` with a NATIVE Hue managed-scene +
  `dynamic_palette` realization on `office_strip` and an honest
  `approximate_static` on `office_lights`; Pause/Resume/Stop all performed
  with the returned `session_id` only. Stop trimmed history sanely.
- **Builder journey (live production API path)**: `scene.preview_draft`
  derived id `cutover_test_scene` server-side (no id sent) and persisted
  nothing; `scene.create` stored the canonical document; `scene.update`
  edited name/brightness/palette-order/speed with the id stable; a
  Builder-style whole-document update on migrated `winter_beauty` (metadata
  omitted from the payload) kept all 14 `fixture_states` and re-attached
  `migrated_from_v1` server-side; replay after edit used the updated intent;
  `scene.archive` removed the test scene from the active catalog (7 remain).
- **Errors**: zero errors attributable to Scene Studio. The only AppDaemon
  ERROR/500 entries in the window trace to three malformed probe requests
  sent by the acceptance harness itself (broken shell quoting / empty body
  routed to AppDaemon's `system` endpoint); every well-formed
  `scene_studio_api` call returned 200.

### Notes / follow-ups (from real use)

- The Builder test scene leaves its reusable managed Hue zone + managed scene
  (`SS-Z-*`, `SS-cutover_test_scene-*`) on the bridge after archive — by-design
  R5B reusable provider state; a cleanup affordance is a refinement
  candidate, not a defect.
- `light.food_light` was `unavailable` during the canary window (pre-existing
  device state, unrelated to this rollout).
- `custom_gradient`'s live device-level rejection predates this pass and is
  the known GL-C-103P surface; surfaced correctly, no action taken.
