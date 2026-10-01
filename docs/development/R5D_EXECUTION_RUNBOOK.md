# R5D Execution Runbook & Rollback Package (prepared 2026-09-13, pre-approval)

Status: **prepared, NOT executed** — every command below requires explicit
"R5D LIVE" approval before running.

Working directory on workstation: `D:\Home Tech`
Live addon config: `HA:/addon_configs/a0d7b954_appdaemon` (= `/config` inside addon)

Durable repo assets: this runbook (`docs/scene_studio/`), the validation
scene documents (`services/scene_studio/fixtures/r5d/`), and the no-I/O
rehearsal harness (`services/scene_studio/r5d_rehearsal.py`). Preflight
evidence (live provider snapshots, baselines, rehearsal output) is generated
working material only — kept in an untracked local directory
(`.tmp_r5d_preflight/`, gitignored) and never committed.

---

## 0. Runtime mode decision (requires adapter fix already in working tree)

`apps.yaml` `scene_studio` entry must be patched for the validation window to:

```yaml
scene_studio:
  module: scene_studio.appdaemon_adapter.adapter
  class: SceneStudioApp
  read_only: false            # CHANGED: executor write gate opens (allowlist still binds)
  r5_validation: true         # NEW: command policy = read-only base + playback.* + scene.apply
  r5_fixture_allowlist: ["g_strip", "wled_seg_0"]   # NEW: executor write boundary
  store_root: /config/scene_studio_store
  hue_ip: hue-bridge.local
  hue_username: !secret hue_app_key
  wled_host: wled.local
```

Rationale: with `read_only: true` the RequestsProviderExecutor refuses ALL
provider writes before the allowlist is even consulted, so R5D provider
validation would be impossible. `r5_validation: true` keeps legacy listeners
OFF and the command policy narrow; the executor allowlist remains the physical
write boundary. Phase 10 restores `read_only: true` and removes both r5 keys.

Patch method: append the two new keys and flip read_only in the existing
`scene_studio:` block ONLY — every other byte of `apps.yaml` preserved.
Byte-guard: hash `apps.yaml` before/after; only the scene_studio block may differ.

---

## 1. Pre-deployment backup (execute first after approval)

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
ssh HA "cd /addon_configs/a0d7b954_appdaemon && \
  sudo tar czf backups/pre-R5D-$TS.tar.gz \
    apps/scene_studio apps/apps.yaml appdaemon.yaml \
    www/scene_studio scene_studio_workbench \
    scene_studio_store/registry scene_studio_store/scenes && \
  sudo sha256sum backups/pre-R5D-$TS.tar.gz"
```

R4 immutable backup discrepancy — audit-trail rules (binding):

- `backups/v1_immutable_R4_20260912T045209Z/` on HA was found **EMPTY** during
  the R5D preflight (2026-09-13; no files, no manifest). It is preserved
  **exactly as found** — never backfilled, never renamed, never deleted. It
  stands as evidence of the discrepancy against the R4 ledger record.
- No newly created copy may ever be described, labeled, or referenced as the
  historical R4 backup.
- During the approved live phase, create a **separately named**
  reconstructed/reference backup from the still-intact v1 originals in
  `custom_scenes/`, BEFORE any store/app writes:
  `backups/v1_reference_reconstructed_R5D_<utc-timestamp>/`.
- Its `manifest.json` MUST contain, per file: the source path and SHA-256,
  plus the manifest creation timestamp and explicit provenance stating it was
  **reconstructed during R5D after the intended R4 immutable backup was found
  empty**.

```bash
TSR=$(date -u +%Y%m%dT%H%M%SZ)
ssh HA "D=/addon_configs/a0d7b954_appdaemon/backups/v1_reference_reconstructed_R5D_$TSR; \
  sudo mkdir -p \"\$D\" && \
  sudo cp -p /addon_configs/a0d7b954_appdaemon/custom_scenes/*.json \"\$D\"/ && \
  sudo python3 -c \"
import json, hashlib, pathlib, datetime
d = pathlib.Path('\$D')
entries = []
for p in sorted(d.glob('*.json')):
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    entries.append({'source_path': str(p), 'sha256': h})
manifest = {
  'backup_id': 'v1_reference_reconstructed_R5D_$TSR',
  'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'provenance': ('Reconstructed during R5D (2026-09-13) from the still-intact '
                 'v1 originals in custom_scenes/ after the intended R4 '
                 'immutable backup (v1_immutable_R4_20260912T045209Z) was '
                 'found empty; this is NOT the historical R4 backup.'),
  'files': entries,
}
(d / 'manifest.json').write_text(json.dumps(manifest, indent=1))
print('manifest files:', len(entries))
\""
```

Baselines already captured (read-only, pre-approval):
- WLED canonical state sha256: `6c7673d5f92f1a93d37146a674e5e886259076e21d91eb98a580184bb84c5ba8`
  (`.tmp_r5d_preflight/wled/state_canonical.json`)
- Hue light baselines: g_strip `793685e8…`, lamp `5d353eb2…`, middle_bar `9aae1673…`
  (`.tmp_r5d_preflight/hue_light_*_baseline.json`)
- Hue scene-list id-set (108 scenes): `5f4f8692e779363fa8b1780c0a04a87a901834ada048ff8845942358bf6d76b2`
- Store scene + registry hashes: see preflight report §A.

## 2. Deployment sequence (after approval)

1. Commit repo: adapter fix + derivation tests + R5D ledger/docs (one boundary
   commit, secret scan exit 0).
2. Run §1 backups.
3. Push backend files to `HA:/tmp/r5d/` via `ssh HA "cat > /tmp/r5d/<path>"`,
   then install with ownership matching existing files (uid/gid 197609):

   ```bash
   ssh HA "sudo install -o 197609 -g 197609 -m 644 \
     /tmp/r5d/<relpath> /addon_configs/a0d7b954_appdaemon/apps/scene_studio/<relpath>"
   ```

   Files (12): 3 new — `domain/playback.py`, `service/playback_realization.py`,
   `migration/activation.py`; 9 replaced — `appdaemon_adapter/adapter.py`,
   `discovery/hue.py`, `domain/bindings.py`, `domain/commands.py`,
   `renderers/hue.py`, `renderers/wled.py`, `service/engine.py`,
   `service/policy.py`, `service/ports.py`.
   Verify all 49 files post-install against the locally generated repo hash
   manifest (untracked evidence; regenerate from the repo tree if needed).

4. Replace Workbench build in `www/scene_studio/`:
   `index.html` (`42257e98…`), `assets/index-2cPLqGcp.js` (`33a389f7…`),
   `assets/index-BiL1UtWK.css` (`1df8df84…`, byte-identical to live — reinstall
   anyway for coherence). Remove stale `assets/index-lZ-yxfEu.js`.
   Keep `scene_studio_workbench/dist` (vestigial static_dirs target) untouched.
5. Patch `apps.yaml` per §0 (targeted block edit + byte-guard).
6. Install temporary scenes through the store path: run `SceneStore.add_scene`
   locally (workstation) against a temp store seeded with the live registry —
   or rehearse first with `python services/scene_studio/r5d_rehearsal.py
   --registry <local-untracked-live-registry.json>` — then place ONLY
   `services/scene_studio/fixtures/r5d/r5_dynamic_validation.json` and
   `services/scene_studio/fixtures/r5d/r5_static_validation.json` (the
   store-validated documents) into `scene_studio_store/scenes/`
   (sudo install, uid 197609).
   The seven production scene files are NOT rewritten.
7. Restart ONLY the AppDaemon addon (narrow procedure):

   ```bash
   ssh HA "ha addons restart a0d7b954_appdaemon"
   ```

   (fallback: HA REST `hassio/addon_restart`).
8. Health gate before any provider write:
   - `POST /api/appdaemon/scene_studio_api {"method":"GET","path":"/status"}`
     → `runtime.mode == "r5_validation"`, allowed_commands include
     playback.start/pause/resume/stop + scene.apply; fixtures 25 (21 ready);
     no errors in log tail; Workbench loads at
     `http://homeassistant.local:5050/local/scene_studio/index.html` and connects.
   - If unhealthy → restore per §4 immediately, no provider testing.

## 3. Validation execution order (after health gate)

Capture → act → read-back → compare at every step; abort + restore on any
isolation failure.

1. **6.1 Start** `playback.start {scene_id: r5_dynamic_validation}` →
   require real `session_id`.
   - Expect g_strip: gradient points + `dynamics.status=dynamic_palette`,
     speed 0.5, visibly animating (read-back `GET /light/2a2c45a9…`).
   - Expect seg0: `fx=9, pal=6, sx=128, bri=153, on=true, frz=false`,
     animating (read-back `GET /json/state`).
   - Isolation: segments 1–5 byte-identical to canonical baseline;
     `lamp`/`middle_bar` light resources unchanged vs baselines.
2. **6.2 Pause** `playback.pause {session_id}` → seg0 `frz=true`.
   Hue open question §8.1: read back `dynamics` + observe ~10 s; classify
   `native` (frozen) vs `approximate` (speed=0 but animating / no-op) with
   the observed behavior recorded. No provider behavior changes.
3. **6.3 Resume** `playback.resume {session_id}` → seg0 `frz=false` + motion
   resumes; Hue animation resumes (speed restored 0.5).
4. **6.4 Wrong session** `playback.pause {session_id: "r5d-bogus"}` →
   `conflict`; prove zero provider deltas (hash re-compare).
5. **6.5 Stop** `playback.stop {session_id}` → session `stopped`; seg0 left
   `frz=true` final frame (R5B Option A) classified `approximate_static`;
   g_strip `dynamics.status=none`. Record live decision evidence.
6. **Phase 7 static apply** start again → `scene.apply {r5_static_validation}`
   → session `stopped` reason `superseded by scene.apply`; seg0 ends
   `frz=false` at static color/bri; g_strip static gradient; siblings
   untouched (stale-freeze protection proven live).
7. **Phase 8 restart/orphan** start → capture session → `ha addons restart`
   → addon back → session shows `orphaned`, providers still animating, no
   auto-resume; Workbench surfaces orphan; `playback.stop {session_id}` reclaims.
8. **Phase 9 sub-gate (only if 6–8 pass)** expand allowlist to
   `["g_strip","wled_seg_0","lamp","middle_bar"]` (apps.yaml edit + restart),
   install `r5_hue_managed_validation` scene (prepared separately during
   execution), verify ONE managed scene resource for Studio
   (`b2abe49f-3a67-40e4-bf5e-29d58c0fbca2`), aggregated actions, recall,
   reuse-on-replay (no duplicate scene resources), honest pause/resume,
   stop retains the resource, PUT semantics observed (replace vs merge),
   user scenes untouched. Bridge scene count must return to 108 + exactly the
   managed resource(s) tracked in `provider_state/hue_scenes.json`.

## 4. Rollback / abort (any failure, or completion)

```bash
# files
ssh HA "cd /addon_configs/a0d7b954_appdaemon && \
  sudo tar xzf backups/pre-R5D-$TS.tar.gz && \
  sudo chown -R 197609:197609 apps/scene_studio www/scene_studio"
# remove temp scenes + runtime artifacts
ssh HA "cd /addon_configs/a0d7b954_appdaemon/scene_studio_store && \
  sudo rm -f scenes/r5_dynamic_validation.json scenes/r5_static_validation.json \
    playback_state.json provider_state/hue_scenes.json"
# restart + verify read_only gate re-armed
ssh HA "ha addons restart a0d7b954_appdaemon"
# then: /status shows mode read_only, playback.start -> 403 conflict
```

Provider restoration (Phase 10): PUT captured baseline payloads back
(seg-level col/fx/pal/sx/ix/bri + frz:false for all 6 segments; Hue light
PUT of baseline on/dimming/gradient/dynamics per captured resources), then
read-back verify against `state_canonical.json` / `hue_light_*_baseline.json`
hashes. Managed Hue scene resource deleted unless positively identified as
planned reusable provider state; user-authored scenes never touched.

## 5. Verification hashes (proposed replacements)

Backend (12 files, sha256 after adapter fix):
```
c75d5b537e5234a642c1768b723815bb396f0d037d6d0fa21e14ab144c8f90ca  appdaemon_adapter/adapter.py
45af7f919dc4543e315021a8fd5c3e845537c97678601b5c361a72e312d24732  discovery/hue.py
5406b8f23ddd83f161d018202df375ec7d0384780dd7820b4533af8c9bd605b4  domain/bindings.py
cd50dbb6efd0a0ee685ced718ec6e07ae28d08ccdc45f54ecfee9846a8ba6bb8  domain/commands.py
5390b5da9ecb5b4097db3665394bc55924b71d747997cf1b28b4df5c3bf939fd  domain/playback.py (new)
257e8f0455020eb5fc7572a6ea5007f60f3e205792366f475d00bff57c3f241f  migration/activation.py (new)
e02ef690306e8d3d4c2576bd0765820cfabc8547dfc655e6f3874702fbcd3f13  renderers/hue.py
015166c0332db8482f7b14bcc28393f02a51483fa619b436028c9528685007cf  renderers/wled.py
4699694fbd6d3f3a3f8a0e49143d5698eb3f78fcd25a5b1bb1c1ace60564586e  service/engine.py
8dcadce63789bb0ef91f81f05b9275baed113676a679a1c129722fa773b395b8  service/playback_realization.py (new)
3e9caaddc75cd4b68be99950c5f622b6c24371c451235f25d2c4f1cec2740903  service/policy.py
8dc22e66c094ab06222a75102a4d1dcbfb10716306dcb8866f5364ea6ccd4902  service/ports.py
```

Workbench:
```
42257e9844842d60928de179d9f9d759235c6b357bc37758797ebdbe6483e872  www/scene_studio/index.html
33a389f7ef56ab99f2cda70d04932a020141c59548e5c51f0b87a7c6fe0503b7  www/scene_studio/assets/index-2cPLqGcp.js
1df8df84e833e39a973a8c363d1e2883fdb1220a77a4e84aa5b6eba235067be9  www/scene_studio/assets/index-BiL1UtWK.css
```
