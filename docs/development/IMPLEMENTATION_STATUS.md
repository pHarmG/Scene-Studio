# Scene Studio — Implementation Status Ledger

Source plan: [.cursor/plans/scene_studio_overhaul.md](../../.cursor/plans/scene_studio_overhaul.md)
Last updated: 2026-09-30 (America/Chicago) — portability pass (portable friend install) in progress

This file is the restart point for any agent resuming the rebuild. Read the master plan first,
then this ledger, then the most recent commit. Update after every completed module/commit.

## Current baseline (2026-09-30, America/Chicago) — reconciliation before the portability pass

Recorded so portability work builds on the CURRENT implementation, not the 9/16 mental model.
Treat current code as authority wherever older status text below conflicts with it.

```text
master:                66dc086 ("scene-studio: plan portable friend installation") + portability pass
live deployed backend: 2026-09-30 backend -Apply (20260930T134759Z) per the block below;
                       master may be ahead via docs/tooling commits (application drift vs
                       repo-history drift — verify with the deployers' -VerifyOnly)
backend command catalog (master): 33 canonical commands — scene.* (apply/rename/archive/
                       restore/save/preview/preview_draft/play_draft/create/update),
                       playback.start/pause/resume/stop, fixture.enable/disable/
                       set_contention_policy/retry/identify/rebind/rebind_preview/
                       rebind_rollback/reconcile/reconcile_preview, sync.suspend/resume,
                       registry.migration_preview/migrate, discovery.run, diagnostics.export,
                       + first-run bootstrap fixture.adopt / target.create / target.update
runtime policy:        normal | read_only | registry_admin (+ r2/r5_validation); policy.py is
                       the single source; registry_admin permits registry/catalog mutations,
                       provider writes + real apply + playback stay blocked
Builder:               live and complete — create/update/duplicate, preview_draft/play_draft,
                       first-class default_state, ordered palettes (equal-weight blend
                       redesign + degenerate-palette healing, 2026-09-18/19), upgrade-to-
                       dynamic, dirty guard, backend-authoritative row fidelity
HA bridge:             sensor.scene_studio_ui schema v3 + scene_studio_ui_command; allowlist
                       = scene.apply, playback.*, scene.archive only; registry/builder/
                       discovery commands unreachable through HA in every mode
contention (hyperHDR): ownership pass live 2026-09-24, corrected 2026-09-29 (Hue holds are
                       per entertainment-configuration members, not server-wide); OPTIONAL —
                       with no hyperhdr_host the layer is configured:false and pass-through
Workbench:             Lit/Vite SPA, mock | direct (devserver) | appdaemon (same-origin)
                       transports; views Overview/Fixtures/Scenes(+Builder)/Discovery/
                       Diagnostics; playback panels; live same-origin auto-detect intact
WLED scope:            single-controller `wled_host` with discovery device_id map +
                       binding endpoint_hint resolution at execution time
fresh validation (pre-portability baseline): pytest 890 passed + 1 POSIX skip (run from
                       services/scene_studio); workbench smoke 784/0; build green; browser
                       regression 108/0; secret scan exit 0
```

Portability pass plan: `.cursor/plans/scene_studio_portable_friend_install.plan.md`
(clean allowlisted bundle, no inherited Git history; first-run bootstrap commands;
portable deployment profile). Portability history appends below; the block above is the
authority snapshot the pass started from.

## Portability pass — portable friend install (2026-09-30, repo-complete)

Implemented per [.cursor/plans/scene_studio_portable_friend_install.plan.md](../../.cursor/plans/scene_studio_portable_friend_install.plan.md).
Passes A (reconcile + portable configuration), B (first-run registry bootstrap), and
C (clean bundle + installer) are repo-complete; Pass D is the friend's real install.

- **Bootstrap commands (Pass B):** `fixture.adopt` / `target.create` / `target.update`
  in `COMMAND_CATALOG` (catalog now 33). Adoption derives binding/capabilities/
  profile/assessment/provenance server-side from the selected latest-discovery
  observation (never client provider payloads); duplicates conflict, stale
  observations are `not_found`, HA aggregates are rejected. Target commands declare
  semantic targets and assign membership on the authoritative `fixture.groups` side.
  `registry_admin` allows all three; `read_only` still blocks them; provider writes
  stay blocked in every bootstrap path (27-test `test_registry_bootstrap.py` includes
  the zero-provider-write proof). `DiscoveryFetchers.hue_bridge_id` (apps.yaml
  `hue_bridge_id`) stamps bridge identity into hue observations so a clean install
  can adopt Hue lights (previously impossible — no producer of that metadata existed).
- **Portable configuration (Pass A):** `packaging/scene_studio/` — profile example +
  stdlib validator/renderer (`scene-studio-profile.py`: validate / resolve /
  render-apps-yaml; unknown keys fail, secret fields hold secret NAMES only, WLED
  multi-host rejected with the single-`wled_host` rationale, fresh installs start
  `registry_admin`). Both Scene Studio deployers resolve the HA URL as
  `-HaUrl` > `SCENE_STUDIO_HA_URL` > gitignored local config > FAIL (no more silent
  `homeassistant.local` fallback); Vite live proxy requires `SCENE_STUDIO_LIVE_HOST` or runs
  mock.
- **Demo decoupling (Pass A §2.3):** `fixtures/demo/` is a generic structural clone
  of the canonical samples (19 real provider UUIDs → synthetic, WLED MAC → the
  documented placeholder, LAN hosts → `.local` names, personal lamp names → generic,
  studio → office), generated by the committed `packaging/scene_studio/
  generate_demo_fixtures.py`. The Workbench mocks/goldens/scenarios now run on the
  demo set (`npm run goldens` regenerates from it; `devserver --seed-demo` seeds it);
  the canonical samples stay internal regression data and are excluded from the
  bundle. Source-tree docstrings were genericized (`studio`/`ha_legacy_staging` prose);
  `migration/activation.py` (this house's R4 gate, imported by nothing at runtime)
  stays repo-only. HT topology in workbench scripts was remapped to the demo ids.
- **Clean bundle (Pass C):** `scripts/scene_studio/build_portable_bundle.py`
  builds `dist/scene-studio-portable/` (+ `--zip`) from an explicit allowlist:
  backend src, built Workbench, demo fixtures, deployers, profile tooling,
  installer, install guide, secrets template — 144 files, no `.git`, no
  `ha_legacy_staging`, no recorded payloads, no `migration/activation.py`. Gates: secret
  scan, portability scan (LAN addresses / device ids / bridge ids / personal or
  production room names), Workbench build presence, clean-environment Python
  imports, sha256 `MANIFEST.sha256`. Bundle installer
  `installer/deploy_scene_studio.ps1` (preflight read-only by
  default; `-Apply` drives the two deployers with backup/verify/rollback; never
  edits `apps.yaml`/`secrets.yaml`). Install/rollback guide:
  `packaging/scene_studio/PORTABLE_INSTALL.md`.
- **Workbench Setup flow (Pass B §2.2):** an empty registry now lands on a
  bounded Setup view (health → discovery → adopt → targets → summary → the exact
  `registry_admin: false` exit step), built on the normal store/command surfaces
  (no second API). Mock gains an `empty-registry` scenario whose runtime honestly
  reports `registry_admin` (`provider_writes_blocked: true`), plus mock/bootstrap
  handlers for the three commands.
- **Docs:** `ARCHITECTURE_CONTRACTS.md` §4 catalog + runtime-policy text now match
  `COMMAND_CATALOG` (33) and document the bootstrap seams.
- **Validation (final, this pass):** `pytest` **931 passed + 1 POSIX skip** (890
  baseline + 41 new: bootstrap 27, profile 9, demo 5) run from
  `services/scene_studio`; workbench `npm run smoke` **806/0**, build green,
  `npm run browser` **114/0** (+22 bootstrap smoke checks, +6 rendered setup-flow
  checks); `python scripts/security/scan_secrets.py` exit 0; bundle build green
  with all gates (bundle secret scan + portability scan clean); bundled devserver
  `--seed-demo` verified serving 24 generic fixtures + the Workbench.
- **Not done here (by design):** Pass D (friend's real install) and any
  apps.yaml auto-patching (`-Apply` YAML mutation stays a later milestone — the
  installer prints the block and instructions).

## Portability corrective pass — fresh-install hardening (2026-09-30, after review of 849c643)

Review-driven corrections; bootstrap/domain work preserved, distribution path
proven against a fake remote instead of assumed:

- **Fresh-install deployer path:** both deployers classify the remote state
  FIRST (proven `test -d`, never inferred from a caught failure). `-FreshInstall`
  requires the target ABSENT; ownership derives from the existing parent
  (apps/ or www/, else the add-on root); stage → hash-verify → syntax-check →
  activate → restart → first health check AFTER activation must land in
  `registry_admin`. Failed first-install rollback quarantines the new tree
  (`failed-new`) and proves the API ABSENT again (`Restart-AppDaemonAddon
  -AllowAbsent`) — no fabricated backup of a nonexistent previous package.
  `-VerifyOnly` on an absent target reports "not installed" clearly. The
  existing UPGRADE path (backup tar, mode preservation, verify, rollback) is
  byte-for-byte unchanged — and the acceptance harness now proves it. A real
  defect the harness flushed out: `@(... | Select-Object -Last 1)` owner
  extraction binds as Object[] and fails `[string]` parameter binding on some
  pwsh builds; both deployers now cast `[string]` (the upgrade path's bare
  pipeline assignment was already safe).
- **Prebuilt Workbench path:** `-Prebuilt` deploys the bundle's dist/ with no
  npm build/smoke (structurally unavailable in the bundle; dist integrity still
  verified + remote gates unchanged). Source-repo deployments keep the
  build+smoke gates by default; the installer passes `-Prebuilt` explicitly.
- **Installer contract:** `scene-studio-profile.py resolve` now emits ONE
  canonical nested shape (`providers.*`/`integrations.*`) — 849c643's installer
  read `$Settings.providers.*` against a flattened resolve and could never work;
  the acceptance harness runs the bundled installer against the bundled profile
  tooling, proving the seam. Installer preflight order: bundle manifest
  verification (byte-exact, no unlisted payload — `verify_release_manifest.ps1`)
  BEFORE any remote contact; add-on root existence check, then read-only remote
  state classification (a mistyped AddonConfigRoot fails read-only, never
  reads as a fresh install); provider
  preflight (TCP reachability for enabled hue/wled/hyperhdr, hue bridge_id
  adoption warning); a first-install invocation (either target absent) REQUIRES
  `runtime_mode: registry_admin` — normal/read_only profiles are upgrade-only;
  `-Apply` fails on unreachable enabled providers.
- **ha_light toggle wired:** `providers.ha_light.enabled: false` now renders
  `ha_light_enabled: false` and the adapter honors it (discovery skips the
  provider; hue enrichment omitted with it). Previously the option was ignored.
- **Atomic bootstrap mutations:** `FixtureStore.create_target_with_members` /
  `update_target_definition` validate the WHOLE request (duplicate target, every
  fixture id) and commit ONE canonical registry document; a failed command
  leaves the registry byte-for-byte unchanged (regressions assert the bytes).
  Receipts report only fixtures the command actually changed (idempotent
  re-add/remove is a no-op).
- **Resumable Setup:** the Workbench offers Setup while the runtime is
  `registry_admin` regardless of fixture count (mode is re-read on every
  reload/reconnect), auto-lands only for a genuinely empty install, and leaves
  ordinary navigation in normal mode. Browser regression: adopt exactly one
  fixture, navigate away, return through the tab, complete `target.create`.
- **Portable-distribution acceptance harness**
  (`tests/portable_install/test_portable_install_acceptance.py` +
  `fake_remote_ha.py`): operates on the GENERATED bundle through a fake ssh
  host (stateful fake FS + mini shell interpreter) and a fake HA REST API.
  Covers: bundle build/gates, manifest accept/tamper/missing/extra, read-only
  preflight (proves no remote writes), fresh install END TO END (staged,
  hash-checked, activated, restarted, `registry_admin` health, prebuilt
  workbench, no npm, no fabricated backup), upgrade backup/mode preservation,
  the registry_admin first-install rule, and provider reachability gating.
  Builder gained `--out` for harness use. 7/7 passing.
- **Validation:** backend `pytest` (incl. the acceptance harness) green;
  workbench smoke/build/browser green; secret scan exit 0; bundle gates clean;
  portable ZIP regenerated only after these distribution-level checks passed.

## Distribution UX pass — guided portable installer (2026-09-30/10-01, after 8634ad7)

Distribution-layer only; the backend, bootstrap commands, Workbench, profile
contract, deployer gates, fresh/upgrade semantics, manifest verification, and
acceptance harness are unchanged except where noted.

- **Explicit deployment model:** the workstation (PowerShell 7 + Python +
  ssh client; no local HA/AppDaemon) is now first-class versus the remote
  target (Home Assistant + AppDaemon). HA API URL, SSH host, SSH username,
  SSH port, and the AppDaemon config root are all independent — never assumed
  to share an address. Deployers and the internal installer gained an optional
  `-SshPort` (ssh args threaded through every remote call; the destination
  stays `-HaHost`, which may be `user@host`). No Home-Tech topology anywhere.
- **ZIP root entry points:** the bundle now reads like software —
  `START-HERE.md` (short task-oriented guide), `Install-SceneStudio.ps1`
  (the wizard), and everything repo-shaped moved under
  `support/scene-studio/` (deployer `$PSScriptRoot`-relative resolution
  preserved). The builder writes TWO manifests: a support-level one (used by
  the internal installer, unchanged semantics) and a root one covering the
  whole bundle (verified by the wizard before anything runs); the manifest
  verifier treats every `MANIFEST.sha256` as metadata, not payload.
- **Guided wizard (`packaging/scene_studio/Install-SceneStudio.ps1`):**
  9 steps — dependency check; HA API URL + long-lived token (SecureString,
  never echoed, never persisted; reachability even with 401, authenticated
  /api/config check); ssh host/port/user probe (BatchMode + accept-new);
  AppDaemon root AUTO-DETECTION under `/addon_configs` (appdaemon.yaml/apps
  markers; one candidate proposed, several numbered/prompted or answered,
  none -> manual entry), with a hard existence check; provider selection
  (HA lights default on; Hue with optional link-button AUTO-PAIRING via
  CLIP v1 and best-effort bridge-id detection; WLED; hyperHDR) with TCP
  reachability preflight; remote state read (fresh vs upgrade, apps.yaml/
  secrets.yaml presence, current runtime mode preserved on upgrade); a
  REVIEW showing the exact apps.yaml block before an explicit confirmation
  (type INSTALL / confirm_install / -Yes); and a finish step printing the
  Workbench URL. The wizard generates the canonical profile JSON into a
  workstation temp dir and drives the EXISTING internal installer — no
  second deployment engine. Advanced paths: `-ProfilePath` bypass and
  `-Unattended -Answers <json> [-Yes]` (the answers file is the tested
  noninteractive interface; secrets come from env only).
- **Bounded apps.yaml/secrets.yaml mutation:** marker-delimited managed
  block (`# >>> scene_studio managed block >>>` ... `<<<`). Replaces an
  existing scene_studio block (marker-delimited or bare-key bounded by the
  next top-level key) or appends without touching anything else; unrelated
  content is preserved byte-for-byte. Hue key goes to secrets.yaml under the
  canonical name `scene_studio_hue_app_key` (value never displayed; created
  file gets chmod 600). Backups: remote timestamped copy under
  `<root>/backups/scene-studio-config-<stamp>/` plus the original text in
  memory; on deployment failure the wizard restores the original bytes
  (or removes files it created), restarts AppDaemon via the authenticated
  REST path, and reports. No YAML parser — deterministic text surgery on the
  clearly owned block only.
- **Acceptance harness extended:** `test_wizard_acceptance.py` (10 tests,
  fake ssh + fake HA API) covers the wizard boundary: full fresh journey
  with SEPARATE HA API and SSH values and a non-default ssh port (2222);
  single-root auto-detection; multi-root numbered selection; invalid addon
  root; unreachable ssh; unreachable HA API failing before ANY remote
  contact; zero remote mutation before confirmation (call-log proven);
  upgrade preserving unrelated apps.yaml content byte-for-byte with the
  runtime mode preserved; provider choices -> profile/secret handling (key
  value in secrets.yaml only, never in console output or the profile; HA
  token persisted nowhere); config backup + automatic rollback on a forced
  deployment failure. The fake remote gained ssh option parsing (`-p`,
  `-o`), `ls -1`/`cat`/`cp -a`/`rm -f`/`chmod`/`base64 -w0`, an
  `ssh_unreachable` marker, a `fail_backend_staging` marker, and dynamic
  addon-root discovery for API/served-index probes. Harness found and fixed
  real wizard defects: a PowerShell scope gotcha where a script-scope
  variable sharing a typed parameter's name re-coerces assignments (the
  parsed answers JSON silently became a string), array-splat mis-binding of
  named tokens into scripts (replaced with hashtable splatting), PS7's
  removed `Get-Date -UtcNow`, and empty-array unrolling to `$null` in
  hashtable literals.
- **Validation:** wizard acceptance 10/10; existing portable acceptance
  8/8; backend `pytest` green; Workbench smoke/build/browser green (no
  frontend changes); secret scan exit 0; bundle gates clean; portable ZIP
  regenerated from the new layout only after all distribution tests passed.

## Historical: current state (2026-09-17 UTC) — HA unification live

Canonical HA seam is `sensor.scene_studio_ui` schema v2 + `scene_studio_ui_command`.
Live apply record: [HA_UNIFICATION_ROLLOUT.md](HA_UNIFICATION_ROLLOUT.md).

```text
live Scene Studio application code: this unification tree (backend -Apply 20260930T134759Z; prior 20260917T010417Z)
runtime.mode: normal
read_only: false
provider_writes_blocked: false
legacy_events_enabled: false
allowed_commands: full canonical catalog (26)
fixtures (live): 22 ready / 2 disabled of 24 (office_lights aggregate removed; office target retired)
active scenes: 8
bridge_schema_version: 3 (adds per-scene target_ids; compact card resolves the brightness trim from authoring data)
targets: bathroom, bedroom, living_room, studio, whole_house
legacy apply_scene / delete_scene apps: unregistered
Scene Studio legacy listeners: OFF
rest_command.scene_studio_apply_scene: deleted
script.scene_studio_apply / script.scene_studio_target_power: live
script.apply_scene_script: transitional wrapper still present
input_select.saved_scenes / scene_manager / light.office_lights helper: not retired yet
Hue-native Work lights: untouched (operator may retire manually)
```

The authoritative records for earlier live cutovers remain:

- [PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md)
- [BUILDER_EXPANSION_ROLLOUT.md](BUILDER_EXPANSION_ROLLOUT.md)
- [HA_UNIFICATION_ROLLOUT.md](HA_UNIFICATION_ROLLOUT.md) — this pass

Read-only verification of the live deployment is the two deployers'
`-VerifyOnly` modes (run them with PowerShell 7 / `pwsh`):

```powershell
pwsh ./scripts/deploy/ha/deploy_scene_studio_backend.ps1 -VerifyOnly
pwsh ./scripts/deploy/ha/deploy_scene_studio_workbench.ps1 -VerifyOnly
```

Compact read-only production preflight (backend health, runtime policy, provider
connectivity, fixture health, per-scene render plans, recent ERROR events,
served Workbench revision, no mutation):

```powershell
pwsh ./scripts/scene_studio/preflight_scene_studio.ps1
```

`master` may be newer than the deployed application because plan, documentation,
and deployer-only commits land after an application deployment. Distinguish
application drift from repo-history drift before deciding anything needs a
redeploy.

## Workbench product-polish pass (2026-09-15 — deployed live 2026-09-16)

Bounded UI polish over the Fixtures and Scenes views, no backend/scene-model/runtime
change:

- **Fixtures/grouped-row alignment:** `<ss-fixture-row>` and `<ss-fixture-cluster>` now
  share one grid template (`--ss-fixture-cols` in `tokens.css`) instead of two
  differently-shaped grids (the cluster summary previously carried an extra 5th
  column). The cluster's health rollup moved into a name-adjacent tag (same pattern as
  `<ss-scene-row>`'s runtime tag) instead of owning its own column. Nested member rows
  dropped the 20px wrapper indent that shifted their columns out of alignment with
  every other row in the list; nesting is now a small rail drawn inside the name
  cell's own flex content, so columns 2-4 stay pixel-identical across plain rows, the
  cluster summary, and nested members (verified in `scripts/browser_regression.mjs`).
- **Scene swatches:** `<ss-swatch-band>`'s `band` variant now renders one solid,
  equal-width segment per palette color (an ordered "flag" of chips) instead of a
  single blended `linear-gradient` — a 3+ stop palette used to blur into one smear;
  segments keep each color and its order individually legible. The chip also grew
  (64×20px → 100×26px) for more at-a-glance prominence, and an empty palette now
  renders an explicit dashed placeholder rather than a solid border-colored chip.
- **Fixture capability indicators:** the old per-row `#caps()` markup (border-color-only
  active/inactive, easy to misread as decorative) is replaced by a shared
  `<ss-capability-indicators>` component with real fill/border-style contrast between
  supported and unsupported. It still covers exactly the fixture's real
  `color_xy`/`color_temp`/`gradient`/`dynamic_native` — `on_off`/`brightness` are
  deliberately not given dedicated glyphs because every fixture in the fleet has them,
  so a dedicated icon would be constant-true decoration, not signal. A disabled
  fixture (`enabled: false`) now renders every glyph in one uniform dimmed state
  (title explains why) instead of showing capabilities as if they were actively
  drivable.
- **Validation:** `pytest` 720 passed + 1 skip (unchanged, no backend touched);
  `npm run smoke` 651/0 (unchanged, DOM-free layer); `npm run build` green;
  `npm run browser` **73/0** (+7 new focused checks: grid-column equality between a
  plain row and a cluster summary, x-alignment across plain/summary/nested rows,
  truthful on/off capability state for a Hue fixture and a WLED segment, a disabled
  fixture never showing an "on" glyph, and ordered/color-correct swatch segments for a
  4-color scene); `python scripts/security/scan_secrets.py` exit 0.
- **Deployed (2026-09-16, live):** `./scripts/deploy/ha/deploy_scene_studio_workbench.ps1 -Apply`
  after explicit user approval. Local build/smoke gates passed (651/0) immediately
  before deploy; served index SHA-256 `e99fcc8e58730fb8a32109b20c91aee508bcb3e213dc0a0600bd82f126e0966b`;
  backup `HA:/addon_configs/a0d7b954_appdaemon/backups/scene-studio-workbench-20260916T015605Z/`
  (archive + retained previous-live directory). Asset-only deploy — no AppDaemon/Core
  restart required, backend unchanged.

## Workbench scale-up + mobile swatch-priority pass (2026-09-16, deployed live 2026-09-16)

Direct user feedback after using the just-deployed polish pass on desktop and phone:
"we made the entire scale of the UI ridiculously tiny for no reason... I want to hit
zoom twice"; and on the Scenes view at mobile width, the palette swatch disappeared
before the location/ready-disabled text did, backwards from what's actually useful.

- **Scale-up (~15-18%) across the whole design-token system**, not a per-component
  patch: `tokens.css` — `--ss-font` 13px→15px, `--ss-font-mono` 11px→13px, the
  `--ss-size-*` scale (11/12/13/15/20/28 → 13/14/15/17/22/30), `--ss-row-h` 34px→40px,
  `--ss-gutter` 12px→14px, `--ss-radius*` bumped a notch, `--ss-inspector-w` 340→372px,
  `--ss-fixture-cols` tracks widened ~10%. Every `font-size: Npx` declaration across all
  18 component/view files (121 occurrences) remapped through the same table
  (9→10, 10→11, 11→13, 12→14, 13→15, 14→16, 15→17) so nothing was scaled twice or
  skipped; every `icon*(N)` call site and the `icons.js` defaults moved the same way.
  Header icon buttons, status/badge dots, the capability-indicator boxes, and the scene
  swatch chip (100×26px → 116×30px) grew to match. `.ss-btn`/`.ss-select` padding grew
  for touch comfort.
- **Scenes-row mobile collapse priority reversed:** the swatch is the single most useful
  way to recognize a scene at a glance, so it's now the LAST thing a narrow
  `<ss-scene-row>` hides — the `@container` breakpoint drops `.targets` (location) and
  `.ready` (the ready/disabled counts) first, keeping `ss-swatch-band` visible down to
  the row's own minimum width. Verified at a true 375px mobile viewport via
  `getComputedStyle` (not just a screenshot): swatch visible, targets/ready hidden.
- **Corrective same-day fix — `<ss-scene-row>` was silently clipping itself:** the
  scale-up above widened its fixed-px column budget (name/targets/ready) to a ~1024px
  minimum while the container-query breakpoint stayed at 560px, so at ordinary desktop
  widths (confirmed live at 636px) the row demanded more space than `<ss-panel>`'s
  `overflow: hidden` would show — the swatch, ready counts, and action buttons were
  rendered but entirely invisible, with no scrollbar to hint why (user-reported: "the
  swatch is off screen"). Fixed by making name/targets/ready flexible (`minmax(floor,
  fr)`) instead of rigid fixed-px, dropping the now-redundant dedicated gutter column,
  and raising the compact-mode breakpoint from 560px to 680px (the full layout's own
  flex-track floor) so there is no longer a width range where a track's minimum is
  demanded but not available. `<ss-fixture-row>`/`<ss-fixture-cluster>` already used
  flexible tracks from the earlier product-polish pass, so they were not affected.
  Verified programmatically (`scrollWidth` vs `clientWidth`, not just a screenshot)
  at 400/560/680/700/850/1100px — zero clipping at any of them — with a new permanent
  `browser_regression.mjs` check pinning this.
- **Fixture-cluster health text was two sizes too small:** the cluster summary's "N
  ready" rollup was a bespoke bordered pill at 11px, while every plain `<ss-fixture-row>`
  shows the identical kind of information ("ready"/"disabled") as a plain 14px label via
  the shared `<ss-status-glyph>`. Fixed by dropping the pill chrome and matching the
  plain-row size/color exactly; separately, the cluster's "N segments" text had NO
  `.dim` rule at all in its own shadow styles (a sibling component's CSS never crosses
  the shadow boundary) and was rendering at full brightness/15px instead of muted/14px —
  added the missing rule, copied verbatim from `<ss-fixture-row>`.
- **Validation:** `pytest` 758 passed + 1 skip (backend untouched); `npm run smoke`
  651/0; `npm run build` green; `npm run browser` **76/0** (+3 new checks: the two
  mobile swatch-priority checks at 390px, plus the no-clipping check across
  400-1100px); `python scripts/security/scan_secrets.py` exit 0.

### Second corrective round, same day: uneven row heights + dead middle-row space

User feedback on the just-fixed layout: "uneven rows and empty space[,] condense
controls on the right?" — two more real bugs, not a false impression:

- **Uneven row heights:** `<ss-action-menu>`'s own `:host` is `display:flex;
  flex-wrap: wrap`. The 76px actions column (from the scale-up) was too narrow to
  fit its 2 dense 30px buttons (apply+play, for a dynamic scene) plus the 30px
  overflow trigger — ~98px needed — so it silently wrapped to a second line for
  dynamic scenes while a static scene's single apply button fit on one line,
  making every dynamic scene's row visibly taller than every static scene's row in
  the same list. Fixed by widening the actions column to 108px (comfortably above
  the 3-button minimum) and raising the compact-mode breakpoint from 680px to
  712px to match the full layout's new (slightly larger) flex-track floor.
- **Swatch position, iterated live with the user (3 rounds):** in compact mode
  (targets/ready hidden), the name track was `minmax(90px, 1fr)` — with nothing
  else to fill the row, it grew to absorb ALL leftover width (measured: 392px
  for the word "Twilight"), pushing the swatch/actions far to the right with a
  large empty gap in between.
  1. First tried capping name to a plain `minmax(90px, 220px)` (a
     `min(220px, max-content)` content-hugging version was tried first but the
     whole declaration was silently dropped as unsupported by the rendering
     engine, confirmed by dumping the parsed CSSOM rule) so the swatch/actions
     packed to the left with dead space pushed to the trailing edge instead.
  2. User pushback: right-alignment of the swatch/actions at a consistent x
     across every row was correct and expected (standard list convention); the
     actual bug was the uneven row height, not the alignment. Reverted the cap
     back to `minmax(90px, 1fr)`.
  3. Follow-up: with the swatch sitting immediately before the tiny action
     buttons at the far right, it read as just another small control rather
     than a prominent identity marker. Final shape: name capped to
     `minmax(90px, 200px)`, then the swatch immediately after it (part of the
     scene's identity block), then a `.gutter` track — `display:none` in full
     mode (where `.ready`'s own `1fr` already provides the flexible track),
     `minmax(16px, 1fr)` in compact mode — as the ONLY flexible track, so it
     alone pushes just the actions to the row's right edge without dragging
     the swatch along with them.
- **Validation:** `pytest` 758/1-skip, `npm run smoke` 651/0, `npm run build`
  green, `npm run browser` **77/0** (+2 checks: no row ever overflows across
  400-1100px including the new breakpoint edges, and static/dynamic rows report
  identical heights at both a narrow and a wide width).
- **Deployed (2026-09-16, live)** — see the combined deployment record at the end
  of the "Capability badges: tap/click description" section below; this pass shipped
  in the same `-Apply` run as the two later passes.

## Inspector facelift + truthful capability badges (2026-09-16, deployed live 2026-09-16)

User: "This side panel could really use a face lift" (the `<ss-inspector>` fixture/
scene/discovery/event side panel, never touched by any earlier polish pass), then
"we have capability badges we can use" and "wtf is the point of Discovery
bound_ready being shown?"

- **Fixed a real, silent bug, not just a style upgrade:** every inspector action
  button (Disable, Apply registry update, Apply rebind, ...) used `class="ss-btn"`,
  but `.ss-btn` is defined only in the document-level `base.css`, which — per the
  component's own pre-existing docstring note — never reaches a shadow-DOM
  component. Every action button was rendering as a completely unstyled native
  button (visibly wrong: a bright white box against the dark theme). Fixed by
  defining `.btn`/`.is-ok`/`.is-warn` locally in `ss-inspector.js`'s own
  `static styles`, with hover/disabled/`:focus-visible` states matching the rest
  of the app. Pinned with a new check reading the button's actual computed style.
- **Visual pass:** facts (and the registry-review before/after comparison) now
  read as a bordered, divided-row card instead of a loose two-column grid;
  explanation text is a tone-colored callout (left accent bar matches the
  descriptor's own status tone) instead of plain paragraphs; "Technical details"
  gets the same chevron-disclosure treatment as the Fixtures room groups / Scenes
  archived section; Close matches the header icon-button language; title/type
  scale bumped to match the rest of the app's post-scale-up hierarchy.
- **Capability badges, not a text chip string:** "Capabilities" now renders the
  SAME `<ss-capability-indicators>` truthful icon badges the Fixtures list rows
  use (reusing `technical.capabilities`, already present on the fixture
  descriptor) instead of a plain "ON/OFF · BRI · RGB · TEMP · GRAD×5 · DYN" text
  fact — one visual language for capability truth across the whole Workbench.
  Ghosts identically for a disabled fixture (verified live: Double Strip).
- **Dropped the redundant "Discovery" fact:** it was the raw discovery-entry
  status string (`bound_ready`, `bound_reconcile_available`, ...) — always a
  duplicate of either the status glyph or the explanation callout ("REGISTRY
  UPDATE AVAILABLE" / "Discovery suggests a possible replacement...") already
  shown in plain English, in noisier snake_case. The full entries remain in
  Technical details (`technical.discovery_entries`) for anyone who wants the raw
  value — no information lost, just decluttered from the primary facts card.
- **Validation:** `pytest` 758/1-skip (backend untouched), `npm run smoke` 651/0,
  `npm run build` green, `npm run browser` **80/0** (+4 checks: button carries
  real app styling not the UA default, the 4 capability badges render and are
  truthfully "on" for a fully-capable fixture, and the Discovery fact is gone
  while Capabilities remains as a label). Live-verified across fixture/
  disabled/registry-review states via screenshots, not just the check suite.
- **Deployed (2026-09-16, live)** — see the combined deployment record at the end
  of the "Capability badges: tap/click description" section below; this pass shipped
  in the same `-Apply` run as the tap/click pass.

### Capability badges: tap/click description (2026-09-16, deployed live 2026-09-16)

User: "give capabilities description on icon tap / click" — the badges' only
description was a `title` hover tooltip, unreachable on touch/mobile and
dependent on hover the app's own accessibility guidance says not to rely on.

- Each `<ss-capability-indicators>` glyph is now a real `<button>`; tapping/
  clicking it (or Enter/Space while focused) opens a small description
  popover naming the capability, its truthful state, and any detail (mirek
  range, gradient points, native effect count) — the exact same text the
  `title` already carried, just reachable without a mouse. Built on the
  native Popover API (top-layer, immune to the row/list ancestors'
  `overflow: hidden`), the same technique `<ss-overflow-menu>` already uses;
  position computed from the tapped button's own rect in `beforetoggle`.
- Regression caught and fixed during this pass: since the badge lives inside
  a selectable `<ss-fixture-row>`, the click was bubbling up and ALSO
  selecting/inspecting the row as a side effect. Fixed with
  `e.stopPropagation()` on the button, matching the same pattern
  `<ss-overflow-menu>`'s own trigger already uses for the identical reason.
- Verified live in all three contexts: a supported capability (Hue G Strip's
  Gradient), an unsupported one (a WLED segment's RGB color), and a disabled
  fixture (Double Strip, every glyph reads "Unavailable — fixture disabled").
  Also verified directly on a Fixtures-list row (not just the inspector):
  the popover opens and the row does NOT get selected.
- **Validation:** `pytest` 758/1-skip (backend untouched), `npm run smoke`
  651/0, `npm run build` green, `npm run browser` **82/0** (+2 checks: tapping
  a glyph opens a non-empty description popover, and does not also select
  the row).
- **Deployed (2026-09-16, live):** `./scripts/deploy/ha/deploy_scene_studio_workbench.ps1 -Apply`
  after explicit user approval ("deploy"). This single `-Apply` run shipped all three
  repo-only passes above (scale-up + mobile swatch-priority, inspector facelift +
  capability badges, tap/click description) in one deploy. Local build/smoke gates
  passed (651/0) immediately before deploy; served index SHA-256
  `c0f99bfd46cd41a9e71e50460dba59745b4547f198f85e499752dc7e11026c8d`; backup
  `HA:/addon_configs/a0d7b954_appdaemon/backups/scene-studio-workbench-20260916T044301Z/`
  (archive + retained previous-live directory). Asset-only deploy — no AppDaemon/Core
  restart required, backend unchanged.

---

⚠️ **Historical records below.** Everything from "Live deployment status
(2026-09-14 evening check)" through "Current restart point (2026-09-14, Pass 1
repo-complete)" describes the **pre-cutover** state and is preserved as history.
It is not current status.

---

**Historical (pre-cutover) live deployment status (2026-09-14 evening check):** Scene Studio backend + Workbench
from `fef39aa` are deployed on AppDaemon add-on `a0d7b954_appdaemon` alongside the
untouched legacy apps. Registry is schema v2. Runtime is `read_only: true` /
`provider_writes_blocked: true`. Fixture counts: **22 ready / 3 disabled / 0 degraded**
of 25 (Hue 15/18 ready). Custom Gradient is **ready** on its existing Hue binding
(assessment **unknown**, GLEDOPTO GL-C-103P). Double Strip remains intentionally
disabled. Workbench Needs Attention is **0**. Full record:
[RECONCILE_ROLLOUT_20260914.md](RECONCILE_ROLLOUT_20260914.md). Pass 1
(`legacy_events_enabled` independent of `normal` writes) is implemented in
the repo; the live cutover is **not** applied. Record:
[PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md).

## R1 live validation record (2026-09-11, America/Chicago)

Deployed the Scene Studio sidecar to the AppDaemon addon (`a0d7b954_appdaemon`)
alongside the untouched legacy apps, in `read_only: true` mode.

- **Versions (live):** AppDaemon 4.5.13 (Python 3.12.13), HA 2026.8.0,
  WLED firmware **16.0.1** (ESP32, real MAC `aabbccddeeff` — note: the
  recorded sample claimed 14.4 + placeholder MAC), Hue bridge at
  `hue-bridge.local`, WLED at `wled.local`.
- **Deployment:** full backup at
  `backup-pre-R1-20260911.tar.gz` (apps, appdaemon.yaml, custom_scenes,
  Resources) taken before any change; `scene_studio` Python package copied
  to `apps/scene_studio/`; `secrets.yaml` created (chmod 600) extracting the
  existing Hue application key from `apps.yaml` — **no credentials rotated,
  no legacy app entries modified**; `scene_studio` app entry appended with
  `read_only: true`; file logging added to `appdaemon.yaml`
  (`logs/main_log` → `/config/logs/appdaemon.log`) because docker access is
  protection-mode-blocked over SSH.
- **Validation performed (all pass):**
  - initializes cleanly; log shows `SceneStudioApp initialized READ ONLY
    (R1)`; all 7 legacy apps + voice_stack_control started; 0 ERROR lines.
  - `/api/appdaemon/scene_studio_api` responds (POST envelope + GET-query
    fallback both verified).
  - `status`, `fixtures`, `scenes`, `diagnostics/recent` reads all 200
    (catalog intentionally empty — store unseeded in R1; **no store
    directory was created**, proving zero persistence).
  - provider reachability accurate via discovery: hue_v2 **18
    observations** (all 18 bridge lights, incl. the 10 out-of-scope ones),
    wled **6 observations** (device `aabbccddeeff:seg:0..5`, 220-effect
    catalog captured, `dynamic_native` true), ha_light **34 observations**;
    58/58 entries `available_unbound` (registry unseeded by design; **no
    binding mutations**).
  - read-only gates live: `scene.apply` (non-dry), `fixture.enable`,
    `playback.start` all rejected `403 conflict`; `scene.apply` with
    `dry_run: true` passes the gate (not_found on empty store, as expected).
  - **no-writes proof:** sha256 of WLED `/json/state`, the g_strip Hue light
    resource, and all 7 v1 scene files are byte-identical before vs after
    the entire validation run.
  - legacy unaffected: `input_select.saved_scenes` intact (7 options,
    "Meeting Blue" selected), scene files unchanged, legacy apps running.
- **Discrepancies / notes:** sample WLED MAC/version were placeholders
  (`aabbccddeeff` / 14.4) vs live (`aabbccddeeff` / 16.0.1); live HA has 34
  `light.*` entities while the seed registry models 25 fixtures — registry
  seeding from live discovery is an R2 decision. Personal lamps'
  locations still provisional (needs HA areas at binding review).

## R2 record (2026-09-11, America/Chicago)

Stage 1 (read-only maintained): reconciled the registry against live discovery +
HA `area_name()` data and seeded it into the live store
(`scene_studio_store/registry/registry.json`; no scenes seeded, `read_only: true`
throughout). Post-seed discovery: 21 bound_ready, 3 disabled, 1 degraded, 0
missing — sample-to-live discrepancies resolved (real WLED device
`aabbccddeeff`, live capability catalogs, authoritative areas; personal lamps
resolved to Bedroom from HA data, not guessed). Full report:
`reports/r2_registry_reconciliation_20260911.md`.

Stage 2 (temporary `r2_validation` mode — legacy listeners stayed OFF, command
gate locked to apply-only, executor limited to `["g_strip", "lamp",
"wled_seg_0"]`): tested ordinary Hue color apply on `lamp` (bri 40 + color +
400 ms transition), Hue gradient apply on `g_strip` (3-point
interpolated_palette), WLED seg0 apply (exact `#2222cc`, per-seg bri 102;
**segments 1–5 byte-identical**, device bri/on unchanged — per-segment routing
proven on live 16.0.1 firmware). Dry-run whole_house verification skipped
`double_strip` (disabled) with no-state notes. **Every fixture restored from
pre-test baselines and verified by readback** (WLED full-state JSON
byte-identical). Mode returned to `read_only: true` and the 403 gate was
re-verified live. Temp validation scenes and server-side helpers removed.
WLED 16.0.1 static JSON fields used by R2 (seg.col/bri/on, top-level on) all
validated against live behavior; 16.x dynamic behavior remains deferred.

## R3 record (2026-09-11, America/Chicago)

Workbench deployed through the AppDaemon sidecar and validated in a live
browser, `read_only: true` throughout:

- **Hosting:** AD 4.5 natively serves `{config_dir}/www` at `/local`
  (source-verified in `http.py`; a `static_dirs` entry also exists but the
  `/local` route needed no config). Built SPA (Vite `base: "./"`) deployed
  to `www/scene_studio/` → `http://homeassistant.local:5050/local/scene_studio/index.html`.
  Same origin as `scene_studio_api` — no second API, no HA frontend dependency.
- **Client:** production auto-detection (`detectAppDaemonHosting`) flips the
  Workbench to live same-origin appdaemon transport on first load; persisted
  prefs win for explicit user choices. Persist-race bug found live and fixed
  (prefs were saved before the async connect settled, restoring mock on
  reload).
- **Validated live (headless Chrome):** overview/status; 25 fixtures with
  correct ready/disabled/degraded states; provider chips (Hue 14/18, WLED
  6/6, HA 1/1); fixture inspector with Level-3 technical disclosure
  collapsed; diagnostics feed with sanitized export; read-only discovery
  refresh from the UI (21 unchanged / 36 new-unbound / 3 disabled);
  exception-driven needs-attention list (the 3 disabled + 1 degraded only);
  attempted `fixture.enable` on Double Strip through the page's own origin →
  server `403 conflict`, registry unchanged. No AppDaemon log inspection
  needed for any operational state.
- **Pre-R4 cleanup:** migration target rule replaced — smallest exact
  semantic cover with individual fixture-id fallback instead of the loose
  `whole_house` cover. All seven legacy scenes now produce
  `["studio", "office_strip"]` (historical 14-fixture scope);
  14 mapped / 10 discarded / 0 unresolved; deterministic report regenerated.
  Note: WLED-only synthetic scenes now emit per-fixture ids (declared
  targets cannot cover them exactly).

## R4 record (2026-09-11, America/Chicago)

Migration activation executed through a new explicit, all-or-nothing path
(`migration/activation.py` + `python -m` usage; 8 dedicated tests):

1. Conversion re-run from the immutable v1 sources against the reconciled
   registry + crosswalk with hard gates: 7 scenes; 14 mapped / 10
   provenance-only discarded / 0 unresolved each; targets exactly
   `["studio", "office_strip"]`; disabled `double_strip` state preserved in
   every document (runtime skip comes from the registry).
2. Immutable v1 backup: `scene_studio_store/backups/v1_immutable_R4_20260912T045209Z/`
   on `HA` — byte-for-byte copies of all 7 v1 files, SHA-256 verified against
   the source, manifest written.
3. Pre-R4 v2 store snapshot: `backups/store-pre-R4-20260912T045209Z.tar.gz`.
4. Seven scenes written through `SceneStore.add_scene` (atomic,
   domain-validated); store reloaded post-write and every document
   round-tripped through `Scene.from_dict`.
5. Activation is all-or-nothing: gate/write/verify failures restore the
   pre-activation store (covered by tests, incl. mid-write failure).
6. Live dry-run render plans for all seven scenes: 13 planned fixtures each,
   `double_strip` skipped, **zero bathroom/bedroom/living-room/personal-lamp
   fixtures in any plan**.
7. Workbench live: all 7 scenes listed with names/targets/palettes/summaries
   (12/14 ready); backend reports `read_only`; Apply/Archive/Rename buttons
   render disabled (backend policy); Dry Run functional (13 planned, 0
   out-of-scope). v1 files, legacy apps, and `input_select.saved_scenes`
   verified untouched.

Cleanup folded in: normal-mode policy now derives from the canonical
command catalog; `handle_legacy`'s upsert substitution obeys the runtime
policy (bypass closed, regression test added).

## R5C corrective closure (2026-09-13, America/Chicago)

Small post-acceptance corrective pass (review findings only; no model or
scope changes):

- **Overview playback controls wired:** the rendered `ss-playback-panel` in
  Overview now binds `@playback-action` and routes through a new shared seam
  (`playbackActionEnvelope` in state.js) — the exact same session-addressed
  routing Scenes uses (exact `session_id`, no direct client calls, no
  optimistic mutation; runtime policy stays enforced by the panel gating).
  Previously Overview's visible Pause/Resume/Stop buttons did nothing.
- **Scene-row label contradiction fixed:** the status glyph no longer claims
  "playing" for every live scene — the runtime tag (Playing/Paused/Orphaned/
  N sessions/N issues) owns playback-state wording; the glyph keeps only the
  catalog-health tone. No more "playing · Paused"/"playing · Orphaned".
- **Plan tone wording reconciled:** R5C plan §3 now documents the implemented
  decision — healthy active/paused = health-ok (paused carried by the state
  badge, not the health tone); orphaned/partial/unsupported = warning;
  all-failed = error; stopped = idle. Documentation only.
- **New rendered-path regression:** `scripts/browser_regression.mjs`
  (`npm run browser`, dependency-free headless-Chrome CDP harness over the
  production build) drives real clicks inside the shadow DOM: Overview
  Pause→Resume→Stop (notices, targeted mutation, counts header), Scenes
  panel controls, row labels across active/paused/orphaned/multi-session,
  Stage-2 basics, zero console errors. Smoke grew to 500 checks (seam
  mapping, altered-session_id rejection, view wiring guards, label
  contract). Final gates: smoke 500/0, Vite production build, secret scan
  exit 0, browser regression 17/17.

## R5C record (2026-09-12, America/Chicago)

Workbench playback integration landed as one coherent repo-only boundary
commit (per [R5C_WORKBENCH_INTEGRATION_PLAN.md](R5C_WORKBENCH_INTEGRATION_PLAN.md));
no backend/provider/live change, `read_only: true` untouched on `HA`:

- **Canonical session model (`playback.js`):** plural selectors
  (`liveSessions`/`activeSessions`/`pausedSessions`/`orphanedSessions`/
  `stoppedSessions`), `sessionsForScene` returning ZERO/ONE/MANY (the
  scene-singleton `sessionForScene` helper is removed — backend ownership is
  per fixture, so one scene can run several disjoint sessions),
  `summarizeSession`/`summarizePlayback` (per-provider execution groups,
  exact backend fidelity counts, degraded = failed OR unsupported),
  `sessionTone` (existing status semantics only), and `controlsForSession`
  (state-accurate Pause/Resume/Stop, each requiring BOTH a real
  backend-issued `session_id` AND `runtime.allowed_commands`, with cause-
  specific disabled reasons: legacy / policy / state). Pre-R5A singular
  normalization stays here and nowhere else; legacy sessions stay
  displayable and never sendable.
- **Reusable components:** `ss-playback-panel` (compact counts header +
  "N recent stopped" history note, live sessions only, compact "Playback
  idle" empty state, re-emits `playback-action`) and `ss-playback-session`
  (What/Where/State-text+glyph/How-well/Controls; orphan warning copy;
  legacy explanation; collapsed issues disclosure with fixture display
  names; aria-labeled controls). Panels never call backend clients.
- **Overview:** replaces "primary session +N more" with the panel (every
  live session individually addressable); `buildOverviewExceptions` now
  also surfaces degraded live sessions ("Aurora Flow: 2 playback fixtures
  degraded") single-sourced with the header pill; exception "View" lands on
  Scenes with `{type:"playback", id: session_id}` selection → session
  highlighted in the panel (no playback inspector by design — the session
  row already shows everything).
- **Scenes:** Live playback panel above the catalog; scene rows get a
  compact plural runtime tag ("Playing"/"Paused"/"Orphaned"/"2 sessions"/
  "1 issue"); row-level ambiguous Pause/Stop are GONE — dynamic scenes with
  live sessions offer an unambiguous "Playback…"/"N sessions" focus
  affordance; canonical Resume lives on the paused session row.
- **Mock:** real multi-session collection (targeted pause/resume/stop,
  per-overlap preemption on start/apply, bounded stopped retention ≤5,
  rename/archive session interplay) realized from scenario specs against
  the Python goldens; new focused scenarios `multi-session` (two same-scene
  disjoint sessions + retained stop), `playback-degraded` (provider failure
  + unsupported), `playback-orphaned`; mock runtime policy now includes
  `playback.resume` (was missing from the normal-mode list).
- **Validation:** smoke 492 checks / 0 failed (R5C helper matrix + the §11
  twelve-case matrix incl. store round-trip refresh semantics); production
  Vite build green; secret scan exit 0; headless-browser sweep at
  1440×900 / 1920×1080 / ~700×900 across the new scenarios — no session-
  driven overflow (the ~28px document-level narrow-viewport overflow was
  proven pre-existing on master via stash), Stage-2 regression sweep green
  (nav, header pill, Inbox, System drawer, Diagnostics, inspector, mock
  scenario picker), zero console errors.

## R5 plan record (2026-09-12)

R5 planned as a playback lifecycle/orchestration problem (not a renderer
rewrite). Plan **v2** (amended 2026-09-12 before R5A):
[R5_DYNAMIC_PLAYBACK_PLAN.md](R5_DYNAMIC_PLAYBACK_PLAN.md). Key contracts:

- **Multi-session cardinality**: a persisted session collection keyed by
  stable `session_id` + a fixture_id → session_id ownership index; disjoint
  sessions coexist; overlap preemption stops only the overlapping
  session(s); `status().playback` is the collection + aggregate counts.
- **Session-addressed commands**: `playback.start` returns `session_id`;
  canonical `playback.resume` added; pause/resume/stop act on `session_id`.
- **WLED stop corrected**: `frz:false` is resume (16.0.1 semantics), not
  stop; pause=`frz:true`, resume=`frz:false`; stop realization
  (remain-frozen vs Solid) is an R5B/R5D decision with honest loss
  classification, and start/static-apply ops always clear stale freeze.
- **Hue managed-scene model corrected**: CLIP v2 scenes belong to a group;
  managed resources keyed by `(scene_id, hue_group_id)` with persisted
  bridge resource ids as provider state (`Scene Studio · …` name is a
  label/recovery heuristic only); fixture rendering contributes per-fixture
  action payloads while a playback orchestration layer aggregates per Hue
  group and runs find→create/update→recall once per managed resource;
  per-light `dynamic_palette` (g_strip) preserved.

Stages: **R5A** lifecycle contracts + engine state machine (pure) →
**R5B** native provider implementation → **R5C** Workbench/status →
**R5D** narrowly allowlisted live validation (gate unchanged). Fallback
animator and HyperHDR ownership integration explicitly deferred with
preserved seams. (HyperHDR ownership has since landed: see `docs/scene_studio/CONTENTION_RUNBOOK.md` and `services/scene_studio/tests/test_contention.py`).

## R5D preflight record (2026-09-13, America/Chicago — read-only, pre-approval)

Phases 0–4 of the R5D preflight completed with **zero live writes**; the live
sidecar remains the R1–R4 generation in `read_only: true` (verified by
inspection, not the ledger): R4-era backend (46 files, no R5 modules),
R3-era Workbench build, 7 production scenes + registry hash-verified intact,
legacy apps/`input_select` untouched, provider reachability healthy.

- **Adapter defect found and fixed (repo-only, NOT deployed):** a stale line
  in `SceneStudioApp.initialize()` re-derived the runtime mode after the
  `r5_validation` branch, making `r5_validation` unreachable (read_only +
  r5 → executor refused all writes; without read_only → silent normal-mode
  policy). Escaped tests because `initialize()` is `pragma: no cover`. Fix:
  stale override removed, derivation extracted into testable
  `_derive_mode_and_allowlist()` + 2 regression tests; suite 631 passed +
  1 POSIX skip.
- **R4 immutable backup discrepancy (live, unresolved):**
  `backups/v1_immutable_R4_20260912T045209Z/` on HA was found **EMPTY**
  (no files, no manifest), contradicting the R4 record. The directory is
  preserved untouched as evidence. The v1 originals in `custom_scenes/` are
  verified intact (SHA-256 captured in preflight evidence) and the pre-R4
  store tarball is present, so no data is lost. Any reconstruction during the
  approved live phase must use a separately named
  `v1_reference_reconstructed_R5D_*` directory whose manifest carries source
  paths, SHA-256 values, creation timestamp, and explicit provenance — never
  presented as the historical R4 backup (runbook:
  [docs/scene_studio/R5D_EXECUTION_RUNBOOK.md](R5D_EXECUTION_RUNBOOK.md)).
- Preflight artifacts: provider baselines (WLED canonical sha256
  `6c7673d5…5ba8`; Hue g_strip/lamp/middle_bar light baselines; 108-scene
  id-set with 0 managed), live↔repo deployment delta (3 new + 9 changed
  backend files + R5C Workbench build), local rehearsal of the temporary
  validation scenes through the real store/engine path (16 ops, 0 allowlist
  offenders).
- **Pre-live corrective pass (this commit, repo-only):** (1) `_derive_mode_and_allowlist()`
  now fails closed — `r5_validation` without a non-empty `r5_fixture_allowlist`
  raises at initialization instead of degrading to an unrestricted (None)
  executor allowlist; regressions prove refusal + exact allowlist preservation
  and initialize-level logging ("R5 VALIDATION … allowlist […]", never
  claiming READ ONLY; R1/R2 messages preserved). (2) An accidental commit
  (`2d93837`) tracked the preflight evidence directory (live provider
  snapshots, generated hashes, rehearsal output); it is removed from Git
  tracking and gitignored here — the files remain reachable in Git history at
  that commit (no credentials were captured in them). Durable artifacts were
  promoted intentionally: runbook → `docs/scene_studio/R5D_EXECUTION_RUNBOOK.md`,
  validation scenes → `services/scene_studio/fixtures/r5d/`, rehearsal
  harness → `services/scene_studio/r5d_rehearsal.py` (sample-registry default,
  live-registry via `--registry`, no repo-tracked live data).

## R5D live-run record (2026-09-13, America/Chicago — aborted at 6.1, fully restored)

The approved R5D live validation deployed from exact HEAD `38681c3` (fresh
SHA-256 manifest; staged and installed files verified 12/12 + full 49-file
tree). Live entered `r5_validation` with allowlist `["g_strip", "wled_seg_0"]`
(truthful "R5 VALIDATION" log line confirmed; fail-closed config behavior
live). The run **aborted at Phase 6.1** on a material provider contradiction
and was fully restored per the abort mandate:

- **Proven live (read-back verified):** WLED seg0 start is native
  (`fx=9 Rainbow, pal=6 Party, sx=128, bri=153, frz=false`, animation
  running) with segments 1–5 **byte-identical** to the canonical baseline
  (`6c7673d5…5ba8`); `playback.start` returns a real `session_id`
  (`sess-20260913T202119-dcde2d85f4e2`) and the R5 status collection,
  execution records, and diagnostics events carry honest per-fixture
  results (the failed Hue op recorded `ok=false` with the bridge's 207
  detail — no silent success).
- **Contradiction (repo-side correction required before any R5D re-run):**
  this bridge rejects per-light dynamics activation outright —
  `PUT /clip/v2/resource/light/{id} {"dynamics": {"status":
  "dynamic_palette"}}` → **207** `attribute (.dynamics.status) cannot be
  written`, alone or combined; `dynamics.speed` alone IS accepted. The
  pre-R5B "live G Strip supports per-light dynamic_palette" claim came from
  the read-only `status_values` metadata, which does **not** imply
  writability. The R5B per-light start path is therefore refuted on this
  firmware; the likely correction routes Hue dynamics through the
  managed-scene recall path (recall PUT carries `dynamics`), to be confirmed
  by a read-only/recall probe before any code change. Gradient + dimming
  portions of the same PUT applied (207 is partial success) — read-back
  proved gradient points landed while `dynamics.status` stayed `none`.
- **Full restoration verified:** engine `playback.stop` released the
  session; WLED canonical hash back to `6c7673d5…5ba8` (all six segments);
  g_strip at baseline (bri 100, dynamics none/speed 0, gradient points
  cleared — only the bridge-side `gradient.mode` descriptor flip-flops
  normal/streaming); `lamp`/`middle_bar` byte-identical (never written);
  bridge scene list back to 108 with **0 managed resources** (the sub-gate
  never ran); temp scenes + `playback_state.json` removed (store = registry
  + 7 production scenes, hashes unchanged); no `provider_state/` was ever
  created. `apps.yaml` restored **byte-exact** (`f3ecef24…`, original
  mtime/preserve) and the addon restarted: `read_only` re-armed,
  `playback.start` → `conflict`, exactly 7 scenes, Workbench 200. v1
  originals, legacy apps, and `input_select.saved_scenes` verified
  untouched; the empty R4 directory remains untouched evidence.
- **Rollback materials on HA:** `backups/pre-R5D-20260913T201454Z.tar.gz`
  (sha256 `af05d2b9…`) and
  `backups/v1_reference_reconstructed_R5D_20260913T201454Z/` (7 v1 files +
  provenance manifest; NOT the historical R4 backup).
- **Live deployment state after the run:** the R5 backend (HEAD `38681c3`)
  and the R5C Workbench build **remain deployed but in `read_only: true`** —
  consistent with the R5 exit semantics; the repo-side correction redeploys
  on the next approved run. Phases 6.2–6.5, 7, 8, and 9 were not entered
  (locally rehearsed only).

## R5D Hue corrective pass (2026-09-13, repo-only — committed, NOT deployed)

Implements the corrected Hue dynamic model demanded by the first live run's
evidence (plan §2/§2.5/§8.1 rewritten accordingly):

- **Hue dynamic state is a scene-resource feature.** The per-light
  `dynamics.status` start path is removed from the renderer (and blocked
  through the `provider_ext` dynamics escape hatch); every dynamic-native
  Hue fixture — gradient-capable or not, any palette size — contributes its
  static action and realizes through the managed-scene path. Static Hue
  rendering (direct light PUTs) is unchanged; static `scene.apply` skips
  the `hue.put_scene_dynamic` realization marker instead of executing it.
- **Managed scenes are real Hue dynamic scenes:** create/update send a
  complete deterministic payload — actions (participants only), metadata,
  group, `palette` built from the canonical palette via the existing
  hex→XY conversion, and the scene-level `speed`. Identity
  `(scene_id, hue_group_id)` and the immutable-id label/recovery rules are
  unchanged.
- **Recall semantics:** start/resume recall `{"recall": {"action":
  "dynamic_palette"}}`; pause/stop recall `{"recall": {"action":
  "static"}}` — never a light-style `dynamics` member on the recall.
  Pause/resume/stop fidelity records **approximate** (with the reason);
  the second R5D live run must classify the lifecycle. A Hue scene recall
  is a provider mutation — explicitly NOT probeable read-only.
- **Execution contract:** the path reports `native_scene`;
  `native_dynamic_palette` stays accepted in `EXECUTION_KINDS` for
  serialization/legacy-session compatibility but is never emitted (legacy
  persisted sessions carrying it fail honestly at lifecycle commands).
- **Tests:** renderer realization regressions (no `dynamics.status` light
  PUT anywhere; g_strip routes managed; single-fixture scene in a shared
  group valid with participant-only actions; palette/speed on create and
  update; XY conversion; start/pause/resume/stop recall actions; replay
  reuse; mixed Hue+WLED cardinality; user-scene recovery isolation) —
  new `tests/test_playback_hue_corrective.py` plus reworked R5B/renderer/
  engine coverage; suite 643 passed + 1 POSIX skip. Workbench
  mock execution-kind mapping and smoke contract updated (g_strip expects
  the managed-scene marker); goldens regenerated; rehearsal updated to the
  corrected expectations (g_strip find/post/recall dynamic_palette, static
  recalls on pause/stop, no dynamics-status writes) and runs green on the
  sample registry.

## R5D second live run (2026-09-13, America/Chicago — corrected model PROVEN; isolation contradiction found; R5 NOT closed)

Deployed from HEAD `c457b49` (fresh blob manifest; full 49-file backend tree
verified byte-exact; Stage 3+ Workbench deployed and hash-verified). Hue
topology reconciled through the store seam (`fixtures.bind`) for g_strip /
lamp / middle_bar → Studio `b2abe49f…` (registry backup + hash verified;
registry integrity retained). r5_validation with allowlist
`[g_strip, wled_seg_0]` live-verified (truthful R5 log line).

- **The corrected managed-scene model WORKS end-to-end live.** With four
  bridge-schema rules discovered and implemented (below), `playback.start`
  created a real Hue dynamic scene (`SS-r5_dynamic_vali-b2abe49f`) with the
  canonical palette (XY + per-target dimming), scene-level speed 0.5,
  group-scoped actions, and recalled `{"recall": {"action":
  "dynamic_palette"}}` — **read-back proved g_strip
  `dynamics.status=dynamic_palette, speed 0.5, speed_valid=true`** — the
  recall path achieves what direct light PUTs cannot. WLED seg0 native
  start + segments 1–5 byte-identical re-proven in the same run.
- **Bridge rules discovered this run (all implemented + tested):**
  1. `PalettePost` requires palette-level `dimming` and
     `color_temperature` members (empty allowed) and per-target `dimming`;
  2. scene `metadata.name` maxLength 32, and non-ASCII separators (U+00B7)
     trip name-validity at ~31 chars → all-ASCII label scheme
     `SS-<scene_id[:18]>-<group_rid[:8]>`;
  3. scene create/update requires actions covering EVERY light in the
     referenced group (partial coverage → 400 "Light action targets not
     matching lights in referenced group"; empty anchor actions rejected)
     → new read-only `hue.get_group_lights` / `hue.get_light` ops;
     non-participants carry leave-unchanged on/dimming anchors built from
     their current state;
  4. **MATERIAL CONTRADICTION (R5 remains open): a dynamic_palette recall
     animates the ENTIRE group** — non-participating color lights (lamp,
     middle_bar) received palette colors and animated at the scene speed
     despite on/dimming-only anchors. Dynamic Hue scenes are inherently
     group-wide on this bridge; a room-scoped managed scene cannot animate
     a fixture subset, so the isolation gate fails for any group containing
     non-participants.
- **Per the standing contradiction rule the run was aborted and fully
  restored:** engine stop (static recall) returned non-participants to
  their anchors; WLED canonical hash `b694a8c9…` MATCH (note: the run-2
  baseline itself is the household's drifted state — seg0 Rainbow fx9/pal6
  — recorded before any run-2 write); g_strip/lamp/middle_bar restored
  **hash-exact**; managed scene resource deleted (bridge back to 108
  scenes, 0 managed); temp scenes + `playback_state.json` +
  `provider_state/hue_scenes.json` removed (store = registry + 7 scenes,
  combined hash unchanged); `apps.yaml` byte-exact `read_only: true`
  (`f3ecef24…`); mutation rejection re-verified; v1 originals, R4 empty
  directory, and legacy `input_select` untouched; Workbench 200.
  Rollback archive: `backups/pre-R5D2-20260913T215213Z.tar.gz`
  (sha256 `8e77bebc…`).
- **OPEN DECISION (user) before R5D-3:** the provider needs an isolation
  boundary for Hue dynamics. Candidate: a Scene-Studio-owned ZONE scoped to
  each scene's participating fixtures (zones may overlap rooms; the managed
  scene hangs on the zone) — visible in the Hue app as extra zones.
  Alternatives: accept whole-room animation for Hue dynamic scenes, or
  restrict Hue dynamic playback to dedicated rooms. Not implementable
  unilaterally because it changes user-visible Hue topology.
- Live deployment state: corrected R5 backend (through `c457b49`) + Stage 3+
  Workbench deployed, `read_only: true`.

## R5D third live run (2026-09-14, America/Chicago) — R5 COMPLETE ✅

Managed Hue ZONES shipped as the isolation boundary and the full R5D matrix
passed live. Deployed from HEAD `a06e8a9` (fresh blob manifest; 49/49
backend files byte-exact; Stage 3+ Workbench live). Topology for g_strip /
lamp / middle_bar reconciled through the store seam in the prior run
(retained). r5_validation allowlist `[g_strip, wled_seg_0]` then expanded
in-window to `+ lamp, middle_bar` for the sub-gate.

- **Isolation PROVEN by 18-light hash sweep:** `playback.start` of the
  g_strip-only validation scene resolved an SS zone
  (`SS-Z-9110400a29a9`, created with light-children — the bridge rejects
  device-rtype children with "Invalid children" — and requires zone
  `metadata.archetype`), bound the managed scene
  (`SS-r5_dynamic_validat-13836efb`, palette 3 colors, speed 0.5, actions =
  g_strip only) to the zone, and recalled dynamic_palette. Read-back: g_strip
  `dynamics.status=dynamic_palette, speed 0.5, speed_valid=true`; **every
  other Hue light byte-identical** (zone members are exactly the
  participants, so group-wide animation cannot leak). WLED seg0 native +
  segments 1–5 byte-identical.
- **Lifecycle matrix live:** pause → seg0 `frz:true` + static recall;
  resume → `frz:false` + dynamic_palette recall (g_strip animating again);
  unknown session → conflict with zero provider delta; stop → seg0 remains
  frozen (`approximate_static`); static apply superseded the overlapping
  session, cleared stale freeze (seg0 `frz:false` at the static color), and
  g_strip landed on its static state; addon restart mid-playback → session
  `orphaned`, ownership released, no auto-resume, orphan reclaimed via
  session-addressed stop; Workbench (Stage 3+) serves the live status the
  rendered panel consumes, orphan visible; rendered-path presentation
  covered by the browser regression (17/17).
- **Second participant set (lamp + middle_bar):** own SS zone
  (`SS-Z-67c78b157c5a`, exactly those two lights) + own managed scene
  (`SS-r5_hue_pair_valida-5edc8f2b`); actions exactly the two participants;
  18-light sweep showed only lamp/middle_bar changed. Replay **updated and
  reused** the same zone + scene (zero duplicates). g_strip's zone/scene
  untouched by the pair playback.
- **Resource close-out:** temp validation scenes/state removed (store back
  to registry + 7 production scenes, combined hash unchanged);
  the two SS-managed scenes of the removed temp scenes deleted (bridge at
  108 scenes, 0 SS scenes); **the two SS-Z zones RETAINED as canonical
  managed provider resources** (ownership + exact membership verified via
  provider_state/hue_zones.json: SS-Z-9110400a29a9 = g_strip;
  SS-Z-67c78b157c5a = lamp+middle_bar) — they are the reusable isolation
  boundary for future Hue dynamic playback. WLED canonical hash, all three
  Hue light baselines, v1 originals, R4 empty directory, legacy
  `input_select` (7 options), and mutation rejection all verified intact;
  Workbench 200. Rollback archive: `backups/pre-R5D3-20260914T000122Z.tar.gz`
  (sha256 `45025471…`).
- **Live deployment state:** R5 backend (through `c457b49` + zone model) +
  Stage 3+ Workbench deployed, `read_only: true`.

**R5 COMPLETE.** All acceptance lines proven by live read-back/hash
evidence. Enabling production `normal` writes is Pass 1; the code seam is
in the repo and the live cutover is documented, not yet applied
([PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md)).

## Pass 1 production unlock (2026-09-14, America/Chicago) — repo-complete / deployment-pending

Independent `legacy_events_enabled` flag (default **false**) so `normal`
mode can enable the canonical command catalog and provider writes without
registering the old HA event listeners while legacy AppDaemon apps are
still running. Restricted modes never register those listeners even if the
flag is true. Backend deployer health check now accepts any known runtime
mode (it previously required `read_only`, which would fail after unlock).

Local gates: pytest 680 passed + 1 POSIX skip; Workbench smoke 546/0;
`npm run browser` 29/0; secret scan exit 0. Live HA was inspected read-only
only (`mode=read_only`, 22/3/0 of 25, seven scenes, legacy apps present).
No AppDaemon/`apps.yaml` write this session.

## Current restart point (2026-09-14, Pass 1 repo-complete)

**Superseded 2026-09-15: production is LIVE in `normal` mode — see
[PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md) "Live result" and
the Pass 1 / Pass 2 rows in the phase table below. The read-only notes that
follow are the pre-cutover historical record.**

### Same-binding reconcile + `registry_admin` (live, 2026-09-14)

Approved live rollout of `fef39aa` completed the same day. Temporary
`registry_admin` (`provider_writes_blocked=true`) migrated the live registry
v1→v2 and reconciled **Custom Gradient only** onto its existing Hue resource.
`scene.apply` and `playback.start` were rejected in that mode. Hue and WLED
control-surface hashes were unchanged. `read_only` was restored. Record:
[RECONCILE_ROLLOUT_20260914.md](RECONCILE_ROLLOUT_20260914.md).

Live now (read-only re-check 2026-09-14 evening):

- Runtime `mode=read_only`, `provider_writes_blocked=true`
- Custom Gradient: **ready**, Hue, ON/OFF+brightness+RGB+CCT, no gradient,
  `dynamic_native: false`, GLEDOPTO GL-C-103P, assessment **unknown**
- Double Strip: **disabled**, historical Hue binding retained, excluded from scenes
- Workbench Needs Attention: **0**
- 15 other fixtures may still show `bound_reconcile_available` after a fresh
  Discovery; that is informational idle drift, not a Needs Attention failure,
  and was out of this rollout’s scope

### Backend capability/binding hardening (repo, then live, 2026-09-14)

- Registry schema v2 separates physical `DeviceProfile`, renderer-authoritative
  effective `Capabilities`, explanatory `CapabilityAssessment`, and
  operational health. Live v1→v2 migrate ran during the reconcile rollout
  (`noop: false`).
- Same-binding `fixture.reconcile` / `fixture.reconcile_preview` plus
  `registry_admin` (registry mutations, provider writes blocked) are live.
- Custom Gradient is no longer operationally degraded merely because Hue
  exposes a narrower control surface. Assessment is **unknown**, not
  `limited`, because live Discovery did not carry independently verified
  richer physical evidence; model/name strings were not used to invent
  `addressable_pixels`.

The remaining waves all involve live-system decisions:

1. **Completed 2026-09-15: Pass 1 live cutover + Pass 2 Builder are LIVE.**
   Backend package deployed with `read_only → read_only` preserved
   (deployer-enforced), `apps.yaml` `scene_studio:` block patched to
   `read_only: false` + `legacy_events_enabled: false`, AppDaemon restarted,
   runtime verified `normal` with the full 25-command catalog and legacy
   listeners off, and live acceptance passed (static apply, dynamic
   playback lifecycle, rename, Builder create/edit/preview/save/apply/play,
   archive cleanup). Record: [PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md)
   ("Live result"). Workbench assets deployed and hash-verified from the
   same tree.
2. Completed earlier: R1 (read-only sidecar), R2 (static-apply validation +
   registry seeding), R3 (Workbench live), R4 (7 migrated scenes activated;
   v1 originals preserved as rollback/reference), R5A–R5D, capability/binding
   hardening, live v2 migrate + Custom Gradient reconcile, Pass 1 code,
   Pass 2 Builder.
3. Next work should be selected from actual use of the live product (UX
   polish, missing Builder capabilities, provider-specific rough edges, HA
   distillation) — not another architecture phase.

Repo history through Phase 0 sanitization remains on `origin/master` (the
pre-sanitization secret values remain in git history — see `SECURITY.md`,
rotation still `deployment-pending`).

## Restart instructions (for the next agent)

1. Read `AGENTS.md`, this ledger, and the two live records:
   [PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md) and
   [BUILDER_EXPANSION_ROLLOUT.md](BUILDER_EXPANSION_ROLLOUT.md).
2. Verify the last commit below exists: `git log --oneline -5`.
3. Production is LIVE in `normal` mode at **application revision `6a0ab94`**
   (Pass 1 unlocked 2026-09-15, Pass 2 Builder deployed, Builder expansion
   deployed later the same day, review fixes redeployed after `6a0ab94`).
   `master` is ahead of live only by documentation commits. Live
   backend/Workbench package SHA is verified by the
   two deployers' `-VerifyOnly` mode:
   `pwsh ./scripts/deploy/ha/deploy_scene_studio_backend.ps1 -VerifyOnly` and
   `pwsh ./scripts/deploy/ha/deploy_scene_studio_workbench.ps1 -VerifyOnly`.
   Run deployers with **PowerShell 7 (`pwsh`)** — Windows PowerShell 5.1
   strips embedded double quotes when passing remote commands to ssh.exe.
4. For a compact read-only health check (runtime policy, providers, fixture
   health, per-scene render plans, recent ERROR events, served Workbench
   revision, zero mutation) run
   `pwsh ./scripts/scene_studio/preflight_scene_studio.ps1`.
5. Run `python scripts/security/scan_secrets.py` before every commit.

## Legacy retirement readiness

[LEGACY_RETIREMENT_READINESS.md](LEGACY_RETIREMENT_READINESS.md) maps every
legacy app, event, helper entity, consumer, storage artifact, and rollback
resource with its Scene Studio replacement, remaining dependency, retirement
prerequisite, and classification. Audit only — no legacy system may be
deleted or disabled from that document.

## Phase / module status

| Phase / module | Status | Commit | Validation performed | Unresolved risks | Next dependency |
| --- | --- | --- | --- | --- | --- |
| Phase 0 — repo-side security prep | **complete** | `scene-studio: phase 0 security prep` (2026-09-10, repo tip) | `python scripts/security/scan_secrets.py` → clean (exit 0) over all git-tracked files; sanitized files re-grepped for secret strings | Secret values remain in git history; live rotation pending | Live rotation gated on user approval (see `SECURITY.md`) |
| Phase 0 — live credential rotation | **deployment-pending** | n/a | n/a (live HA/bridge action) | Token A exp 2035; two distinct HA tokens + Hue app key exposed in history | User approval per `AGENTS.md` |
| Phase 1 — domain contracts | **complete** | `scene-studio: define domain and API contracts` (2026-09-10) | `python -m pytest services/scene_studio/tests` → 86 passed; secret scan exit 0; sample data validates through models | None known; contracts frozen per `ARCHITECTURE_CONTRACTS.md` | Unblocks Wave A1/A2/A3/A4 in parallel |
| Wave A1 — store + registry core | **complete** | `56e36da` | 56 new tests; full suite 271 passed; scan exit 0 | Referential guard blocks removing fixtures still referenced by active scenes (ConflictError) | Renderer wave + command service |
| Wave A2 — discovery service | **complete** | `50b6a98` | 44 new tests incl. all §16.2 scenarios; determinism + non-mutation asserted; scan exit 0 | Divergence from aspirational sample: disabled fixtures get no candidates (by design); candidates limited to fixture's current provider | Command service; candidate review UI |
| Wave A3 — migration analyzer | **complete** | `a53d3aa` | 129 new tests; dry-run report byte-deterministic (md5-verified); reports committed under `services/scene_studio/reports/` | Target rule superseded in R3 (exact semantic cover → `['studio', 'office_strip']`); migrated ids collide with sample scenes by design — activation must replace seeded samples | Migration activation (wave 2) after renderers + command service |
| Wave A4 — Workbench mock shell | **complete** | `498c98e` | 220-check node smoke suite; vite build green; headless browser pass | `getStatus()` shape mock-defined — confirm at integration wave 1; `scene.save` = empty draft in mocks | Live API connection (after integration wave 1) |
| Wave B — provider renderers | **complete** | `a498a97` | 85 new tests (356 total); render-plan exit gate demonstrated (dry-run ops + fidelity, no device contact); scan exit 0 | Executor-wave refinements recorded in §Refinements below | Command service (consumes `build_render_plan`); migration activation |
| Wave B.5 — renderer coverage review + upgrade | **complete** | `scene-studio: upgrade renderers to full CLIP v2 / WLED 0.14 coverage` | 491 tests (+38); spot-checked native dynamic_palette on G Strip, per-seg WLED bri, name→fx resolution | Superseded by the goldens unwinding (user audit round) | Wave 3 dynamic playback |
| Registry extension (full light capture) | **complete** | `ccf00f5` | 496 tests; report regenerated deterministically (24 mapped / 0 discarded per scene) | 8 new fixtures have provisional/null locations (set from HA areas at binding review); `bedroom_closet` resource name varies across bridge pulls (noted in metadata) | Binding review at R1; migration activation |
| Workbench mock parity resync | **complete** | `ca96b45` | 325 smoke checks; JS↔Python plan output verified byte-identical incl. 120 fuzzed scenes | Mock mirrors current renderer code — resync again if renderers change | Workbench fidelity UI |
| R1 — read-only live sidecar | **complete (live)** | `scene-studio: R1 read-only mode + live discovery fetchers` (2026-09-11) | Live validation on `HA` — see "R1 live validation record" below | Live validation was read-only; WLED live firmware 16.0.1 vs sample 14.4; renderer JSON-API assumptions to re-confirm when dynamic wave touches live WLED | R2 (static apply validation) — requires separate user approval |
| Stage 1 — registry reconciliation + live seeding | **complete (live)** | this commit (2026-09-11) | Live discovery after seeding: 21 bound_ready / 3 disabled / 1 degraded — matches model; report: `reports/r2_registry_reconciliation_20260911.md` | `bedroom_closet` + `living_room_lamp` HA entities flagged/ambiguous | R2 write validation |
| R2 — controlled static-apply validation | **complete (live)** | this commit (2026-09-11) | `r2_validation` mode (legacy listeners off, apply-only gate, fixture allowlist); live tests: Hue color apply (lamp, incl. 400 ms transition), Hue gradient apply (g_strip, 3 pts), WLED seg0 apply — siblings 1–5 byte-identical; dry-run whole_house skips `double_strip`; allowlist refusal unit-tested; all baselines restored + readback-verified; returned to `read_only: true` (403 gate re-verified) | none open | R3 (done) → R4 migration activation |
| R3 — Workbench live via AppDaemon | **complete (live)** | this commit (2026-09-11) | Live browser validation: auto-connect same-origin, overview/fixtures(25)/discovery refresh/diagnostics/inspector all working; mutation attempt → 403 conflict, registry unchanged; density rules hold | Vite `base: "./"` required for sub-path hosting; persist-race bug found+fixed (prefs saved before async connect settled); office_lights bound_degraded false-positive RESOLVED post-R4 (halight builder uses HA default mirek range when min/max absent) | R4 migration activation |
| R4 — migration activation | **complete (live)** | this commit (2026-09-11) | Explicit activation path (`migration/activation.py`): gates held (7 scenes, 14 mapped / 10 provenance-only / 0 unresolved each, targets `studio`+`office_strip`, disabled `double_strip` preserved); immutable v1 backup SHA-256-verified; pre-R4 store snapshot taken; 7 scenes written via SceneStore atomic path + round-trip verified; live dry-run × 7 = 13 planned each, `double_strip` skipped, **zero out-of-scope fixtures**; Workbench shows all 7 with Apply/Arch/Ren unavailable and Dry functional; `read_only: true` maintained throughout | v1 originals + legacy apps + `input_select.saved_scenes` untouched (verified by hash/state) | R5 dynamic playback |
| R5A — playback lifecycle contracts + engine state machine | **complete (repo-only, corrective pass applied)** | this commit (2026-09-12) | Corrective pass: real restart recovery fixed (load was silently failing — missing json import meant every restart returned an empty collection; regression test added), one-live-owner invariant + persistence-key identity enforced inside PlaybackState, resume-on-active now conflicts (pause stays idempotent), retention trimmed on every stop transition; 22 playback tests + full suite 586 passed + 1 POSIX skip | Provider ops are `pending` placeholders until R5B; Workbench panel is R5C | R5B native provider implementation |
| R5B — native provider playback realization | **complete (repo-only, final corrective gates passed)** | this final corrective commit (after `fa8a640`) | 629 Python tests + 1 skip; 344-check Workbench smoke; Workbench production build; secret scan clean. Final pass restores observational discovery (including read-only discovery/retry byte invariance), makes managed-scene labels immutable-id based across rename, exercises committed registry + recorded Hue discovery into explicit rebind and mixed Hue/WLED playback, and verifies archive stop realization. | R5D live validation pending; HA remains `read_only: true` | R5C Workbench integration |
| R5C — Workbench playback integration | **complete (repo-only; corrective closure applied 2026-09-13)** | corrective commit after `4ce3d4b` (2026-09-13) | Plural session model + reusable playback panel/session components + Overview/Scenes integration + multi-session mock; smoke 500 checks (helper matrix + §11 twelve-case matrix + corrective seam/wiring/label guards), Vite production build, secret scan exit 0, rendered-path browser regression 17/17 (`npm run browser`), Stage-2 shell intact. Details: "R5C record" + "R5C corrective closure" above. | R5D live validation is the next explicit user-approval gate; facelift Stage 3+ may resume against the R5C seams | R5D live validation |
| R5 planning | **complete (v2 amended)** | this commit (2026-09-12) | Plan grounded in live probes (WLED 16.0.1 per-seg `frz` present; bridge scene resource shape; g_strip dynamic_palette); staged R5A–R5D with test matrix + live exit gate | Hue speed-0 pause semantics + managed-scene PUT semantics verified during R5B/D | R5A implementation |
| Same-binding reconcile + `registry_admin` live rollout | **complete (live)** | `fef39aa` deployed 2026-09-14 | v1→v2 migrate (`noop: false`); Custom Gradient same-binding reconcile to **ready** / assessment **unknown**; Double Strip left disabled; Hue+WLED control-surface hashes identical; `registry_admin` rejected `scene.apply`/`playback.start` with `provider_writes_blocked=true`; `read_only` restored; Workbench Needs Attention 0. Record: [RECONCILE_ROLLOUT_20260914.md](RECONCILE_ROLLOUT_20260914.md) | 15 other fixtures may still be `bound_reconcile_available` after Discovery (out of scope) | Pass 1 live cutover |
| Pass 1 — production unlock | **complete (live)** | backend + Workbench deployed from `4e75f0c` (2026-09-15) | Backend deployer preserved `read_only → read_only`; `apps.yaml` `scene_studio:` block patched (`read_only: false`, `legacy_events_enabled: false`; no validation-mode keys); after restart `runtime.mode=normal`, `provider_writes_blocked=false`, `allowed_commands` = full 25-command catalog, log shows `legacy listeners off`; all 7 legacy apps restarted untouched; live acceptance passed (twilight dry-run 13 planned matching R4 baseline; static apply with one honestly-surfaced live device error on `custom_gradient`; rename round trip with provenance intact). Backups + hashes: [PASS1_PRODUCTION_CUTOVER.md](PASS1_PRODUCTION_CUTOVER.md) "Live result" | Legacy apps still on the bridge by design; `legacy_events_enabled: false` must stay until they are retired | Use the product; retire legacy apps when ready |
| Pass 2 — Scene Builder (authoring vertical slice) | **complete (live)** | backend + Workbench deployed from `4e75f0c` (2026-09-15) | Live Builder journey through the production command path: `preview_draft` derived `cutover_test_scene` server-side (no id sent, nothing persisted); `create` stored the canonical doc; `playback.start/pause/resume/stop` ran a real session (native Hue managed-scene `dynamic_palette` on `office_strip`, honest approximate static on `office_lights`); `update` changed name/brightness/palette-order/speed with id stable; Builder-style update on migrated `winter_beauty` kept 14 `fixture_states` and re-attached `migrated_from_v1`; archive cleaned the catalog back to 7. Workbench assets hash-verified at `/local/scene_studio/`. Corrective pass in the same tree: Rename action fixed, stale preview invalidation, read-only Builder entry (browser regression 41/0 incl. read-only journey) | Archived Builder scenes leave their reusable managed Hue zone/scene on the bridge (by-design R5B state; cleanup affordance is a refinement candidate) | Real-use-driven refinement (UX polish, more Builder capabilities, HA distillation) |
| Hard-blocker round (user audit) | **complete** | `migration apply-scope` + `appdaemon transport` + `workbench goldens` (2026-09-11) | 523 Python tests, 333 smoke checks, deterministic report (14 mapped / 10 discarded per scene), JS↔Python parity obligation removed | AD endpoint live validation pending R1 | R1 rollout |
| Builder expansion / polish / rollout | **complete (live)** | `aff2c00` (backend duplicate provenance) + `75f7196` (Builder expansion + backend-authoritative row fidelity) + `393a429` (preflight + legacy audit + ledger repair) + `6a0ab94` (review fixes); backend package + Workbench assets deployed 2026-09-15 and redeployed after the review round | `pytest` 720 passed + 1 skip; smoke 651/0; Vite build green; `npm run browser` 66/0; secret scan exit 0. Live: mixed group+fixture targets, first-class `default_state`, per-fixture overrides with advanced-field preservation (incl. an explicit Remove for overrides stranded by a retarget), Duplicate/Save-as-New with server-derived `duplicated_from`, honest preview quality, backend-derived row fidelity (13 planned / 1 skipped on `twilight`), Rename, dirty guard, cleanup back to 7 active scenes; preflight 7/7 scenes preview OK, Workbench index hash matches the local build. Record: [BUILDER_EXPANSION_ROLLOUT.md](BUILDER_EXPANSION_ROLLOUT.md) (incl. §12 review round) | Effect mode has no universal picker (provider-specific effect ids) — preserved + warned; archived scenes retain reusable managed Hue resources by design; no delete command (archive is cleanup); catalog fidelity emits one INFO event per previewed scene per refresh | Real-use-driven refinement from the expanded Builder; legacy retirement when the documented consumers are migrated |
| HA unification — canonical bridge, scene control, target membership | **complete (live)** | backend + HA patches applied 2026-09-17 UTC | Schema v2 + `targets`; canonical apply proven (`meeting_blue`/`twilight`); 7 AM + NFC + Office Lights Off migrated; rest_command deleted; aggregate `office_lights` removed; studio room-level toggle live-accepted; preflight PASSED. Record: [HA_UNIFICATION_ROLLOUT.md](HA_UNIFICATION_ROLLOUT.md) | Transitional `apply_scene_script` / `scene_manager` / `input_select.saved_scenes` still present; `light.office_lights` helper still in HA UI; `custom_gradient` unavailable; Workbench static index not redeployed; Hue "Work lights" left for manual retirement | Zero-consumer helper retirement; optional Workbench asset deploy |
| Integration wave 1 — command service + AppDaemon adapter + HTTP API | **complete** | `88fb988` | 80 new tests; full §9 exit gate covered against fakes (apply/rename/archive/restore/disable/rebind/legacy routing/HTTP shapes); no live HA needed | `require_int` domain bug found+fixed (`f5c187b`); adapter `register_endpoint` signature varies across AppDaemon versions — live validation pending R1 | Workbench live; migration activation |
| Workbench live API connection | **complete** | `2d504c1` | 453 Python tests + 256 smoke checks; headless-browser e2e against spawned devserver (live client, revision-gated polling, dry-run plan from UI) | `provider_links.connected` is hard-true under the no-op executor — wire real provider connectivity at R1 | Rollout R3 (serve through AppDaemon) |
| Integration wave 2 — migration activation | not-started | — | — | Blocked on user review of dry-run report + HA write approval | R1 deploy first |
| Integration wave 3 — native dynamic playback | not-started | — | — | Fallback animator intentionally unbuilt until unsupported cases demonstrated (§8-B4) | Wave 2 |
| Builder convergence / HA distilled UI / rollout R1–R6 | Builder: **complete (live)**; HA distilled UI / rollout waves: not-started | — | — | HA distilled UI + rollout waves should now be driven by demonstrated real-use need | — |

## Decisions and notes so far

- **Code home:** Scene Studio Python lives under `services/scene_studio/` following the
  `weather_risk_bridge_service` convention (`pyproject.toml`, `src/scene_studio/`, `tests/`).
  Pure domain/store/renderer modules must import without AppDaemon; the AppDaemon adapter is a
  thin subpackage that imports AppDaemon lazily (master plan §2.1).
- **Workbench home:** `packages/scene_studio_workbench/` — plain modern JS/HTML/CSS (Lit
  allowed, TypeScript not required), Vite dev server, `MockSceneStudioClient` first (master
  plan §2.9, §7-A4, §10).
- **Phase 1 contracts frozen** (`docs/scene_studio/ARCHITECTURE_CONTRACTS.md`): ID charset
  `^[a-z][a-z0-9_]{0,63}$`; registry schema v1; scene schema v2; provider-tagged bindings;
  `Motion.speed` normalized 0..1 with renderers owning native translation; fidelity levels
  `native/equivalent/approximate/unsupported`; single `POST command` envelope with catalog in
  `COMMAND_CATALOG`; scene resolution = explicit `fixture_states` → `default_state` → skip;
  disabled/missing fixtures are skipped-with-notes, never errors; discovery never mutates
  bindings; target membership resolves via `fixture.groups`.
- **Phase 0 findings beyond the plan snapshot:** a second, distinct HA long-lived token was
  found tracked in `archive/voice_capture/...` logs and a `card_workbench` sample HTML; both
  sanitized. Full inventory + rotation procedure: `docs/scene_studio/SECURITY.md`.
- **Secret scanner:** `scripts/security/scan_secrets.py` (git-tracked files by default,
  shape-based detectors, placeholder allowlist, inline `secrets-scan:allow` marker for known
  fake test fixtures). Treat exit != 0 as a commit blocker.
- Sanitized snapshot copies remain the current-state authority for migration analysis
  (master plan §3); `custom_scenes/*.json` and `Resources/crosswalk.json` contained no
  credentials and are unchanged apart from the two YAML/secret-bearing files listed in
  `SECURITY.md`.
- **Sample dataset** (`services/scene_studio/fixtures/*.sample.json`): mirrors the live
  crosswalk — 15 fixtures (8 Hue incl. disabled `double_strip` + degraded `custom_gradient`,
  6 WLED segments, 1 plain HA light), 3 targets, 3 scenes (migrated static `twilight`,
  dynamic `aurora_flow`, archived `meeting_blue`), and a fully self-consistent discovery
  report exercising every required status. These are the seed/mock data for Wave A sub-agents.

## Refinements queued for the executor / integration wave

Recorded from Wave B sub-agent review (parent adjudication, not blockers):

- `provider_ext` semantics are renderer-specific today: WLED merges its
  namespace into segment payloads (native effects), Hue and HA-light ignore
  theirs. Contracts rule: a renderer interprets only its own namespace and
  ignores others; revisit Hue/HA interpretation in the dynamic wave.
- Unsupported vs partial ops: Hue emits zero ops for wholly-unsupported
  states; WLED/HA-light emit representable ops with worst-field fidelity.
  Normalize when the executor decides op execution.
- `transition_ms`: unmapped for Hue (CLIP v2 light PUT has none) and WLED
  (legacy used deci-seconds `transition`); HA light maps to seconds.
  Decide per-provider in the executor.
- HA light: `transition` dropped on turn_off ops; declared effects could
  upgrade from `approximate` to `equivalent`.
- RESOLVED (R3): migration target rule now emits the smallest exact
  semantic cover — `["studio", "office_strip"]` for all 7 scenes
  (`whole_house` no longer chosen as a loose covering target).
- Registry extension (2026-09-11): per user, only Water Light + Food Light are
  confirmed not-in-use; the other 8 previously discarded bridge lights are now
  registry fixtures. Migration analyzer treats the registry (not the v1
  crosswalk) as the resource-id authority, and excludes disabled fixtures from
  migrated-scene target coverage (apply skips them regardless).
- User audit round (2026-09-11), four blockers fixed:
  (1) Migration scope regression — migration reproduces the v1 effective
  apply scope (crosswalk-authoritative, 14 fixtures/scene); out-of-scope
  captured states are provenance only; the registry extension stays for
  identity/control. (2) AppDaemon transport rebuilt to AD 4.5's real REST
  model (single `scene_studio_api` endpoint, POST envelope, 200-wrapped
  status/body — verified against AD 4.5.0 sources; Workbench transports
  `direct` | `appdaemon`). (3) `call_service` signature fixed to
  `call_service(service, **payload)` with a signature-exact regression
  test. (4) The 1,200-line JS renderer mirror was deleted; mock dry-run
  plans are Python-generated goldens (`npm run goldens` /
  `scene_studio.devmock`), devserver is the interactive truth.
- Wave B.5 (2026-09-11): parent review vs CLIP v2 + WLED 0.14 JSON API found
  and fixed 11 gaps — Hue `dynamics.duration` transitions, per-light native
  `dynamic_palette` (live G Strip supports it), hue ext honor + grouped_light
  gating; WLED per-seg `bri` bleed fix, effect/palette name resolution via
  discovery-captured device catalogs, native CCT, transition unit mapping,
  preset/playlist access via `provider_ext.wled.top`. WLED 0.14.4 device is
  rgbw=false (no white channel) so cct is dormant until new hardware.

## Environment notes

- Windows workstation; Python 3.13.9 and Node v24.13.0 available locally.
- Machine restart 2026-09-10/11 mid-run (after Phase 0) — resumed cleanly from this ledger.
- Run totals 2026-09-11/12: Phase 0/1 + Waves A/B + Integration 1–3 + R1–R4
  committed; 588 Python tests (+1 POSIX-only skip), 342 workbench smoke
  checks, secret scanner exit 0 at every commit.
