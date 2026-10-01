# Scene Studio — Builder Expansion, Product Polish & Rollout (live record)

Status: **executed 2026-09-15 (America/Chicago)** — Builder expansion is
deployed and live-accepted in production `normal` mode.

Plan: [.cursor/plans/scene_studio_builder_expansion_polish_rollout.plan.md](../../.cursor/plans/scene_studio_builder_expansion_polish_rollout.plan.md)
Predecessor (live cutover): [PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md)

---

## 1. Implementation commits

| Commit | Scope |
| --- | --- |
| `aff2c00` | Backend: `scene.create` accepts optional `duplicate_of`; server/history metadata stripped from create payloads; engine records `metadata.duplicated_from`; `scene.update` re-attaches it. Focused engine tests. |
| `75f7196` | Workbench + contracts: Builder targets/defaults/overrides/duplicate/motion/palette work, honest preview quality, backend-authoritative Scenes-row fidelity, UX/responsive polish, mock parity, smoke + rendered browser coverage. |
| `393a429` | Tooling/docs: read-only production preflight script, legacy retirement readiness audit, repaired ledger header. |
| (script-only follow-up) | Preflight archived-catalog fix: `/scenes` returns active only, so the preflight now also reads `/scenes?archived=true` to report both counts. Not an application change — scripts are local tooling. |
| `6a0ab94` | Review round: nine defects found and fixed across the Builder, the store, the mock, and the preflight (see §12). This is the **currently deployed application revision**. |
| `fda489c` | Documentation only: the review round, the post-review redeploy, and updated validation totals. Not part of the deployed application. |

At commit `393a429` the deployed application content equalled that working
tree and the preflight fix afterwards touched only `scripts/`. That was
superseded by the review round: the **deployed application revision is
`6a0ab94`**, and `fda489c` is documentation only — so application drift and
repo-history drift now differ by one documentation commit (see §12).

## 2. Repository/production baseline reconciled (prework)

- Live application code was `4e75f0c` with `runtime.mode = normal`,
  `read_only = false`, `provider_writes_blocked = false`,
  `legacy_events_enabled = false`, full 25-command catalog, 22 ready / 3
  disabled / 0 degraded of 25 fixtures, 7 active production scenes.
- Both deployers' `-VerifyOnly` passed under PowerShell 7 (`pwsh`); the
  backend deployer preserved `read_only → read_only` on the previous run and
  `normal → normal` on this one.
- `IMPLEMENTATION_STATUS.md` no longer claims production is read-only /
  deployment-pending; pre-cutover sections are labelled historical.
- `custom_gradient` is recorded as a **known pre-existing provider caveat**
  (see §7), not a regression.

## 3. Deployed revisions

> Superseded by §12 "Redeployed revisions (post-review)" after the review
> round; the values below are the original rollout and remain the first
> rollback point.

- **Backend**: AppDaemon package `HA:/addon_configs/a0d7b954_appdaemon/apps/scene_studio`,
  deployed from the tree above; pre-deploy mode `normal`, post-restart mode
  `normal` (deployer-enforced).
  - Backup: `backups/scene-studio-backend-20260915T223040Z/apps-scene_studio-before.tar.gz`
    (SHA-256 `48d83416fdd63ce8ecb6f78066f4f665d33da8f2d3738dc87bccfd2d25f3b12b`)
    + `live-before/`.
- **Workbench**: static assets at `HA:.../www/scene_studio/`, 3 files,
  index SHA-256 `20ca66e56c8e952e3b52932cd506497fcb2ab00a8c472f603fdeb03d461c0f71`.
  - Backup: `backups/scene-studio-workbench-20260915T223209Z/www-scene_studio-before.tar.gz`
    (SHA-256 `4d42f3e6a8a1cfb35a8af96ac9c439f59ada6a904405eecf8bf336ba54fe3ae1`)
    + `live-before/`.
- Only the AppDaemon add-on was restarted (backend pass). The Workbench pass
  is asset-only and required no restart; HA Core was never restarted.

## 4. Runtime flags after rollout

```text
runtime.mode: normal
read_only: false
provider_writes_blocked: false
legacy_events_enabled: false
allowed_commands: full canonical catalog (25)
no validation/admin keys in the scene_studio apps.yaml block
```

## 5. Validation totals

| Gate | Result |
| --- | --- |
| `python -m pytest services/scene_studio/tests` | **720 passed, 1 skipped** |
| `npm run smoke` | **651 passed, 0 failed** |
| `npm run build` | green (3 assets) |
| `npm run browser` | **66 passed, 0 failed** |
| `python scripts/security/scan_secrets.py` | exit 0 |

New focused coverage (no duplicated backend matrix): target helpers and
readiness aggregation; preview-quality matrix (clean / approximate /
unsupported / skipped / notes-only / cannot-render); default-state
set/unset semantics; override add/edit/remove and advanced-field
preservation; duplicate draft construction + `duplicate_of` provenance,
absence of a false migration claim, source untouched, unknown source
`not_found`, derived-id collision; catalog-fidelity cache refresh rules
(initial load, revision change, no re-preview on unchanged revision,
honest `unavailable` on failure); rendered-path Duplicate / defaults /
override / honest-preview-headline journeys; responsive checks at
1440/700/390.

## 6. Builder results

- **Targets**: declared Rooms/Groups and individual fixtures are both
  selectable with canonical ids; readiness is aggregated from existing
  fixture health only (live example: `studio 12/13 ready`).
- **Defaults**: `default_state` is first-class (On/Off + explicit off,
  brightness, color, color temperature). Unset properties are absent from
  the canonical document; capability support is summarized, never hidden.
- **Overrides**: per-fixture `fixture_states` authoring with progressive
  disclosure; editing a supported field preserves that fixture's
  gradient/effect/transition/`provider_ext`; removing an override is
  explicit.
- **Duplicate / Save-as-New**: opens the same Builder in duplicate mode with
  the source intent and no source identity, shows the server-derived
  candidate id, and saves through `scene.create` + `duplicate_of`.
- **Motion**: static / palette-cycle + speed + palette brightness are
  editable; `effect` mode and non-auto strategy round-trip and are warned
  about instead of being faked into a universal effect picker.
- **Palette**: ordered authoring with add / remove / reorder / duplicate /
  hex / native picker, a compact order preview, and a confirmed clear.
- **Preview**: honest quality headline derived from the canonical render
  plan (`ready` / `ready with reductions` / `partially unsupported` /
  `cannot render`); candidate id plus a non-authoritative collision warning.
- **Scenes rows**: backend-authoritative fidelity via a bounded
  `scene.preview` cache (catalog load, engine revision change,
  create/update, explicit `Fidelity` refresh). Goldens remain mock/test
  fixtures only; a failed preview renders "fidelity unavailable".

## 7. Known remaining product limitations (recorded, not blockers)

1. **`custom_gradient` (GLEDOPTO GL-C-103P)** — the Hue device rejects the
   static PUT at device level (HTTP 207). Scene Studio surfaces it honestly
   as a per-fixture error. Pre-existing; not touched by this pass. Live
   production `scene.preview` reports 7/7 scenes preview OK with 0
   unsupported, because the rejection is a device-execution fact, not a
   renderer-fidelity fact.
2. **`motion.mode = effect`** — effect names are per-fixture provider
   effects (e.g. WLED `fx` ids, Hue `effects`), so this pass deliberately
   does **not** offer a universal effect picker. Existing effect scenes
   round-trip unchanged and the Builder warns that the mode is preserved.
3. **Motion strategy** (`native_preferred` / `static`) — preserved and
   warned about rather than exposed; switching explicitly to Static or
   Dynamic intentionally replaces advanced strategy intent.
4. **Managed Hue resource retention** — archived scenes keep their reusable
   `SS-Z-*` zone / `SS-<scene-id>-*` managed scene as intentional R5B
   provider state. Archive is not destructive provider cleanup, and this
   pass adds no deletion affordance. The two acceptance probe scenes were
   archived **without ever being applied or played**, so they created no
   provider resources.
5. **No delete command** — cleanup uses `scene.archive`; the two disposable
   acceptance scenes remain in the archived catalog (3 archived total).
6. **Catalog fidelity event noise** — each refresh issues one
   `scene.preview` per active scene, and the engine emits one INFO event per
   preview. The refresh is revision-gated (never a timer), so the cost is
   bounded to catalog changes plus the explicit refresh action.
7. **Provider-write acceptance was intentionally not repeated** per plan
   §14: this pass changed authoring/validation code only — no backend
   execution, playback, or renderer semantics — and the immediately
   preceding deployment already validated unchanged execution/playback. The
   authored probe scene compiled through the real render path
   (`scene.preview_draft`, 12 planned / 1 skipped, native 5 / approximate 7)
   and the Apply/Play affordances were policy-enabled, but no device write
   was issued for the probe.

## 8. Legacy retirement readiness

[LEGACY_RETIREMENT_READINESS.md](LEGACY_RETIREMENT_READINESS.md) — audit
only. Ready to retire (after consumer migration): `apply_scene`,
`delete_scene`, `generate_office_scene`, the four legacy scripts/emitters.
Retain temporarily: `save_light_states` (capture semantics differ),
`scene_manager` + `input_select.saved_scenes`, `generate_crosswalk`, v1
files/crosswalk, rollback archives. Still required: the Test Bench
scene-controls card and two lovelace dashboards that read
`input_select.saved_scenes`. No deletion or disabling was performed.

## 9. Live acceptance result (production Workbench, real browser)

| Step | Result |
| --- | --- |
| New Scene → mixed targets (`studio` group + `lamp` fixture) | accepted; canonical ids only |
| Scene defaults (brightness 42, color `#ff7a45`) | stored in `default_state` |
| One override (`g_strip` brightness 70) | stored in `fixture_states` |
| Palette (2 colors) + dynamic motion (speed 0.4, palette brightness 55) | stored |
| Preview | 12 fixtures planned / 1 skipped (`double_strip`); native 5 / approx 7 / unsup 0; headline **"Preview ready with reductions"** (warn) — severity matches render quality |
| Save | persisted; catalog row appears with **backend-derived** fidelity `5 nat · 7 approx` |
| Edit → Preview → Save Changes | prepopulated correctly; default brightness → 61; id stable; override survived (70) |
| Duplicate (via the overflow item) → rename → Preview → Save New | Builder opened in duplicate mode with source intent, no source id, no migration claim; candidate id `acceptance_probe_copy`; saved with `metadata.duplicated_from = "acceptance_probe"`; **original untouched** |
| Rename via overflow | inline editor persisted the new name with the id stable |
| Dirty-draft guard | exit confirmation shown, stayed in the Builder, Leave discarded, no stray scene created |
| Cleanup | both probes archived through `scene.archive`; active catalog back to the exact 7 production scenes |

No provider write was issued during acceptance (see §7.7).

## 10. Production preflight (post-acceptance, read-only)

```text
HA Core:   2026.9.2
API:       healthy
Runtime:   mode=normal read_only=False provider_writes_blocked=False allowed_commands=25 live_sessions=0
Fixtures:  25 total / 22 ready / 3 disabled / 0 degraded / 0 missing
Providers: ha_light 1/1 ready; hue_v2 15/18 ready; wled 6/6 ready
Scenes:    7 active / 3 archived
Fidelity:  7/7 scenes preview OK; 0 unsupported; 0 approximate
Events:    0 error / 0 warning
Workbench: served index SHA-256 matches the local build
Preflight PASSED — no mutation was performed.
```

## 11. Rollback

1. **Workbench assets**: restore
   `HA:.../backups/scene-studio-workbench-20260915T223209Z/live-before` to
   `.../www/scene_studio/` (or untar `www-scene_studio-before.tar.gz`).
   Asset-only; no restart needed.
2. **Backend package**: restore
   `HA:.../backups/scene-studio-backend-20260915T223040Z/live-before` to
   `.../apps/scene_studio/` and restart the AppDaemon add-on
   (`hassio.addon_restart`). Runtime mode and `apps.yaml` were unchanged by
   this pass, so no `apps.yaml` rollback is required.
3. Re-run `pwsh ./scripts/scene_studio/preflight_scene_studio.ps1` to
   confirm the restored state (expected mode `normal`, 7 active scenes,
   7/7 previews OK).

---

## 12. Review round — defects found and fixed (2026-09-15, post-rollout)

A bug-hunt pass over the work above (`6a0ab94`) found and fixed nine defects.
Two of them were user-visible correctness problems in the new authoring
surfaces; the rest were robustness/parity gaps.

| # | Defect | Fix |
| --- | --- | --- |
| 1 | A **failed preview left the previous server render plan in builder state**, so a stale "preview ready" panel could render alongside the failure. | A failed `scene.preview_draft` clears the stale plan; the error headline is the only preview presentation. |
| 2 | The "preserved unchanged" notice and the preserved-advanced-motion hint were computed **once from the frozen original document**, so they kept claiming content was present after the user removed an override or switched to Static. | Both now derive from the live draft (one source of truth; `builder.advanced` removed). |
| 3 | **Overrides stranded by a target change** (fixture_states entries outside the selected targets) were listed but had **no removal affordance**, so a scene could be permanently stuck in a reduced preview. | Each orphan override now renders an explicit per-override **Remove** action. |
| 4 | A **forced catalog-fidelity refresh was silently dropped** when it arrived mid-flight — the common case right after a save. | The forced intent is remembered and re-run once the in-flight pass settles. |
| 5 | Error-to-field mapping read **only save errors**, so preview-time canonical validation (the path most authoring mistakes take) never highlighted the offending control — the palette and motion inline errors were effectively dead code. | Both error surfaces are consulted; state editors render the message inline and mark the field invalid. |
| 6 | **Correcting** a field an error pointed at cleared the message but gave no prompt to re-preview. | Any draft change now invalidates the last preview *attempt*, so a correction asks for a fresh preview. |
| 7 | The **mock accepted** `default_state`/`fixture_states` shapes the engine rejects (bad hex, out-of-range values, unknown/empty states, bad `provider_ext` namespace), so those mistakes only surfaced live. | The mock enforces the same canonical rules at the same dotted paths. |
| 8 | `resolveTargetFixtures` **fell back to a same-named fixture** for a declared target with no members; the engine treats a declared target as authoritative, so Scenes row counts and the Builder could disagree with the backend plan. | The helper takes the declared target ids (canonical precedence) at every call site. |
| 9 | Scene rows showed a single generic "fidelity unavailable" for both "not loaded yet" and "preview failed". | Rows distinguish **fidelity pending** from **fidelity unavailable** and expose the backend's own failure reason as a tooltip. |

Preflight script cleanups in the same round: removed a dead helper, report
unbound fixtures as a note, and read the archived catalog from
`/scenes?archived=true` (the first live run reported 0 archived because
`/scenes` returns active scenes only).

### Post-review validation

| Gate | Result |
| --- | --- |
| `python -m pytest services/scene_studio/tests` | 720 passed, 1 skipped |
| `npm run smoke` | **651 passed, 0 failed** |
| `npm run build` | green |
| `npm run browser` | **66 passed, 0 failed** |
| `python scripts/security/scan_secrets.py` | exit 0 |

New coverage added with the fixes: forced-refresh re-run, stale-preview
clearing, mock canonical-parity matrix (10 rejected cases + a valid rich
case), declared-target precedence, draft-derived preserved flags,
orphan-override listing/removal and honest fidelity-failure presentation in
the rendered browser path.

### Redeployed revisions (post-review)

Deployed **application revision: `6a0ab94`** (the review-fix commit). The
documentation commit `fda489c` is not part of the deployed application, so
`master` is ahead of live by that one commit only.

- **Backend**: redeployed (`normal → normal`), backup
  `HA:.../backups/scene-studio-backend-20260915T225423Z/apps-scene_studio-before.tar.gz`
  (SHA-256 `0c8145dd1930b3f5a8004f45be84e0e1decc979f8cc7ff17edeb346433060af6`).
- **Workbench**: redeployed, 3 files, index SHA-256
  `daff20e373f336cb329d9a2c89e925253f3800cff825d56fee75ff89dbac900c`;
  backup `HA:.../backups/scene-studio-workbench-20260915T225557Z/www-scene_studio-before.tar.gz`
  (SHA-256 `de1a06775a638ddb878f9061658240875c9697e1159f05ef82a23d6744de35b8`).
- Both deployers' `-VerifyOnly` pass; the preflight now reports the served
  Workbench index as matching the local build, `mode=normal`, 7 active / 3
  archived scenes, and 7/7 scenes preview OK with 0 unsupported.

### Live spot-check of the fixes (production Workbench, no writes)

Editing the live `twilight` scene: retargeting from the `studio` group to
individual fixtures produced **12 orphan overrides with Remove actions**; one
removal updated the draft (11 left) and the saved scene was untouched
(`savedStillIntact: true`, nothing persisted). The Scenes row for `twilight`
reported backend-derived fidelity of 13 planned / 1 skipped, quality
`reductions` — matching the R4 baseline (13 planned, `double_strip` skipped).
No provider write was issued.

