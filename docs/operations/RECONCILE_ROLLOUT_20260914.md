# Scene Studio same-binding reconcile rollout (2026-09-14)

Live registry-admin rollout of commit `fef39aa3fc1c78a62c889cd272ac461dcf17fe37`
(`scene-studio: add same-binding reconcile and registry_admin`). Scope was backend +
Workbench deploy, read-only previews, temporary `registry_admin`, v1→v2 registry
migration, same-binding reconcile of `custom_gradient` only, then restore
`read_only`. No rebind, provider mutation, scene execution, playback, fixture
enable/disable, or unrelated registry cleanup.

## Deployed commit

`fef39aa3fc1c78a62c889cd272ac461dcf17fe37`

HA Core at deploy: 2026.9.2. AppDaemon add-on `a0d7b954_appdaemon` restarted for
the backend; Workbench was static assets only.

## Backup

- Path: `/addon_configs/a0d7b954_appdaemon/backups/scene-studio-reconcile-20260914T191935Z/`
- Archive: `store-and-apps.tar.gz`
- SHA-256: `163855361b59171390a99b16034b6f4abd0af8792bf81a5ed00997377c35d522`
- Payload: `scene_studio_store`, live `apps/apps.yaml`, `apps/scene_studio`, `www/scene_studio`
- Registry v1 sibling written by migrate: `/config/scene_studio_store/registry/registry.v1.backup.json`

Working captures from the run remain on HA under that backup’s `evidence/` directory
and `/tmp/reconcile_rollout/`. They are not in git.

## Registry migration

- Preview: `required: true`, `from_schema_version: 1` → `to_schema_version: 2`
- Result: `ok: true`, `noop: false`

## Custom Gradient (final)

- Binding unchanged: Hue `hue_v2` / `f54ec7de-77b1-4bdf-b12f-f5c083733006`
- Health: **ready** (stored degraded override cleared)
- Capabilities: ON/OFF, brightness, RGB, CCT (`mirek` 158–495); no gradient; `dynamic_native: false`
- Device profile: GLEDOPTO / GL-C-103P (`source: hue_device`)
- `capability_assessment`: **unknown** (“Physical feature evidence is insufficient to classify provider parity”)
- Discovery after reconcile: `bound_ready` on the same resource
- Prior degraded revision retained in `binding_history`

Ready + unknown is the expected live outcome: Hue’s control surface is used as
effective capabilities; model/name strings were not used to invent
`addressable_pixels` / `limited`.

## Double Strip (final)

- Still `enabled: false`
- Historical Hue binding retained: `b55f34cf-ce41-476b-a30e-44ab4972f0c1`
- Discovery: `disabled` (provider lookup skipped)
- Workbench: intentionally disabled / excluded from scenes
- Not a Needs Attention item

## Provider-state integrity

Complete WLED `/json/state` and all 18 Hue CLIP v2 lights were hashed before
`registry_admin` and again after migrate/reconcile, before restoring `read_only`.
Control-surface hashes were **byte-identical** (raw payloads also matched):

| Surface | Before = after |
| --- | --- |
| WLED control surface | `c0f0b4c309e7af76ebb839af553cd6d6de87a46fb1429d5ceefa1ef599825fa5` |
| Hue control surface (18 lights) | `5c8f91d7c2663f1c1598fe0ae9d781f5d706d644405d9b0657940c9c8e9e6ef9` |

## `registry_admin` write boundary

Status while in `registry_admin`:

- `mode=registry_admin`
- `read_only=false`
- **`provider_writes_blocked=true`**
- `fixture.reconcile` and `registry.migrate` allowed
- `scene.apply` and `playback.*` not in `allowed_commands`

Probes (scene `twilight`, no provider contact):

- `scene.apply` → `ok: false`, `conflict` (“not permitted”)
- `playback.start` → `ok: false`, `conflict` (“not permitted”)

Diagnostics recorded those conflicts, the migrate, Discovery, and the Custom
Gradient reconcile. No Hue PUT / WLED POST receipts.

## Restored `read_only`

After `apps.yaml` restore (`read_only: true`, `registry_admin` removed) and
AppDaemon restart:

- `mode=read_only`
- `read_only=true`
- `provider_writes_blocked=true`
- `fixture.reconcile` / `registry.migrate` → `conflict`
- Previews remain allowed: `fixture.reconcile_preview`, `registry.migration_preview`, `discovery.run`

Read-only re-check 2026-09-14 (evening, America/Chicago): same runtime and
fixture counts (`ready 22` / `disabled 3` / `degraded 0` of 25; Hue 15/18 ready).

## Workbench

Live page `http://homeassistant.local:5050/local/scene_studio/index.html`:

- **Needs Attention: 0**
- Header: All systems normal
- Custom Gradient: ready, Hue, ON/OFF · BRI · RGB · TEMP, GLEDOPTO GL-C-103P, assessment unknown
- Double Strip: disabled / intentionally disabled
