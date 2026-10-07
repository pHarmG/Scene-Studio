# Scene Studio — Architecture Contracts (Phase 1)

Status: **current** — R5 model, HA unification canonical bridge (`sensor.scene_studio_ui` schema v3), and HA-native scene routines (§11)
Created: 2026-09-10
Code: `services/scene_studio/` (pure Python, stdlib only, pytest)
Sample data: `services/scene_studio/fixtures/*.sample.json`

This document is the handoff contract for every Wave A/B sub-agent (store,
discovery, migration, renderers, Workbench). If a module needs semantics not
covered here, stop and raise it to the parent agent — **do not invent them**.

---

## 1. Identity rules

- All IDs (fixture/scene/target) share one charset: `^[a-z][a-z0-9_]{0,63}$`.
  Enforced by `scene_studio.domain.identities.validate_id`.
- IDs are immutable. Renames change `name` only. There is no rename of IDs.
- Display names are labels; `normalize_name_to_id` (seed tooling only) may
  derive an ID candidate from a name, never at runtime.
- Targets are logical groupings; `fixture.groups` is the authoritative
  membership side. A target resolves to every fixture whose `groups`
  contains the target id. Commands accept a target_id that is either a
  declared `Target.id` or a single `fixture.id`.
- `derive_health(fixture)` computes operational status: explicit `health`
  override wins, else `disabled` when `enabled == false`, else `unbound`
  when no binding, else `ready`.
- Scene Studio fixtures represent independently addressable endpoints.
  Aggregate Home Assistant convenience groups whose children are already
  canonical Scene Studio fixtures are **not** canonical fixtures; Scene
  Studio `targets`/`groups` represent aggregation. Discovery must not
  silently adopt those HA helpers as ordinary `ha_light` bindings.
- `BROAD_GROUP_ID` (`whole_house`) is preserved by same-binding room
  reconcile unless the operator changes it explicitly. HA convenience
  groups never write it. Physical/provider/HA room evidence may propose a
  **primary room group** change; canonical groups feed the HA target
  projection, never the reverse through a second manual list.

## 2. Core models (`scene_studio.domain`)

| Model | File | Notes |
| --- | --- | --- |
| `Fixture`, `Target`, `FixtureRegistry` | `fixtures.py` | registry doc, `schema_version: 2`; loads v1 through a deterministic local migration; duplicate IDs rejected |
| `HueBinding`, `WledBinding`, `HaLightBinding` | `bindings.py` | tagged union on `provider` (`hue_v2`, `wled`, `ha_light`); endpoints are hints only; `WledBinding` with empty `segment_ids` = whole device |
| `Capabilities` | `capabilities.py` | normalized **effective capability of the current binding** and the only renderer authorization; `dynamic_native` marks provider-native dynamic execution; physical claims never widen it |
| `DeviceProfile`, `CapabilityAssessment`, `BindingRevision` | `fixtures.py` | descriptive physical evidence, explanatory provider limitation status, and bounded atomic-rebind rollback history |
| `Scene` | `scenes.py` | `schema_version: 2`; `fixture_states` keyed by fixture_id; `default_state` for the rest; `palette` = canonical color intent (static pointers + dynamic cycle) |
| `Motion` | `scenes.py` | `mode`: `static`/`palette_cycle`/`effect`; `strategy`: `auto`/`native_preferred`/`static`; `speed` normalized 0.0..1.0 |
| `FixtureState` | `scenes.py` | at least one field required; colors canonical `#rrggbb` lowercase; optional `palette_index` 0..23 pointer into `scene.palette` (mutually exclusive with `color`); brightness 0..100; provider escape hatch only under `provider_ext` (namespaced to `hue_v2`/`wled`/`ha_light`) |
| `DiscoveryObservation`, `BindingCandidate`, `DiscoveryEntry`, `DiscoveryReport` | `discovery.py` | observations never mutate bindings; candidates carry `confidence` 0..1 + `reasons[]`; `observation_id` = `provider:provider_resource_id` |
| `FidelityLevel`, `ProviderOperation`, `FixtureRenderPlan`, `RenderPlan` | `fidelity.py` | `native`/`equivalent`/`approximate`/`unsupported`; render plans are the dry-run contract |
| `CommandEnvelope`, `CommandResult`, `ErrorCode` | `commands.py` | single `POST command` API; catalog in `COMMAND_CATALOG` |
| `OperationalEvent` | `events.py` | structured diagnostics feed (Level-1/2 text; raw data sanitized) |
| `sanitize_tree` | `sanitize.py` | mandatory for any export/shared serialization |

## 3. Scene resolution semantics (binding for command service + renderers)

Applying `scene.apply {scene_id, target_id?}` resolves as:

1. Load scene; validate schema v2.
2. Resolve targets: if `target_id` given, restrict to fixtures of that
   target (or that single fixture); otherwise all scene `target_ids`.
3. For each resolved fixture: state = `fixture_states[fixture_id]` if
   present, else `default_state`, else skip fixture (record in plan notes).
   When `scene.palette` is non-empty and motion is `static` or
   `palette_cycle`, RGB is filled from `palette_index` (clamped) or
   auto-spread in target/registry order before renderer dispatch.
   Explicit override `color` and `gradient` win; `default_state.color` is
   ignored while a palette is active. Native Hue `dynamic_palette` still
   receives the full palette list.
4. Skip (never error) fixtures that are `disabled` or `missing`; list them
   in `RenderPlan.skipped_fixture_ids` with a note.
5. Hand per-fixture intent to the provider renderer for its binding;
   renderer returns a `FixtureRenderPlan` (fidelity + operations).
6. `dry_run: true` returns the `RenderPlan` without device contact.

## 4. Command API

- Envelope: `{"command": "<name>", "request_id"?: "...", ...params}` —
  params are flattened; unknown params rejected per-command.
- Result: `{"command", "ok", "request_id"?, "data"?, "error"?{code,message,details?}}`.
- Error codes: `validation_error`, `unknown_command`, `not_found`,
  `conflict`, `provider_unavailable`, `internal_error`.
- Catalog today: `scene.apply`, `scene.rename`, `scene.archive`,
  `scene.restore`, `scene.save`, `scene.preview`,
  `scene.preview_draft`, `scene.play_draft`, `scene.create`, `scene.update`,
  `playback.start`, `playback.pause`, `playback.resume`, `playback.stop`,
  `fixture.enable`, `fixture.disable`, `fixture.retry`, `fixture.identify`,
  `fixture.set_contention_policy`, `sync.suspend`, `sync.resume`,
  `fixture.rebind_preview`, `fixture.rebind`, `fixture.rebind_rollback`,
  `fixture.reconcile_preview`, `fixture.reconcile`,
  `fixture.adopt`, `target.create`, `target.update`,
  `registry.migration_preview`, `registry.migrate`,
  `discovery.run`, `diagnostics.export`, `routine.create`, `routine.update`,
  `routine.delete`, `routine.enable`, `routine.disable` (§11). Adding
  commands: extend
  `COMMAND_CATALOG` with a params dataclass; never overload existing param
  meanings.
- **First-run bootstrap (portability pass)** — `fixture.adopt
  {observation_id, fixture_id, name, groups?, enabled?}` creates the first
  canonical fixture FROM a selected latest-discovery observation: the SERVER
  derives binding, capabilities, device profile, assessment, and the safe
  endpoint hint; the client supplies identity/label/groups only (no provider
  payloads). Duplicate stable ids conflict; an observation absent from the
  latest report is `not_found`; HA aggregate observations are rejected.
  `target.create {name, target_id?, description?, fixture_ids?}` declares a
  semantic target (id derived server-side via `normalize_name_to_id` when
  absent) and assigns membership on the authoritative `fixture.groups` side;
  `target.update {target_id, name?, add_fixture_ids?, remove_fixture_ids?}`
  renames and applies membership deltas. No arbitrary registry JSON
  replacement exists. Adoption is always an explicit command — discovery
  never mutates the registry (§6).
- **Runtime policy (backend-owned)**: `status().runtime = {mode:
  "normal"|"read_only"|"registry_admin"|"r2_validation"|"r5_validation",
  read_only, provider_writes_blocked, allowed_commands}` is the
  single source of command permissions. The engine enforces it before
  dispatch (conflict on rejection; a `scene.apply:dry_run` sentinel grants
  dry-run apply without real execution). Frontends only read `runtime` to
  disable unavailable actions — they never duplicate command rules.
  `read_only` is discovery/preview/diagnostics only — it includes the
  observational `scene.preview_draft` (an unsaved-draft render persists
  nothing and writes no provider) but never `scene.create`/`scene.update`.
  `registry_admin`
  permits registry/configuration mutations (`fixture.enable`/`disable`/
  `retry`/`reconcile`/`rebind`/`rebind_rollback`, `fixture.adopt`,
  `target.create`/`target.update`, `registry.migrate`,
  scene rename/archive/restore/save) plus Scene Builder authoring
  (`scene.create`/`scene.update` — catalog mutations, provider writes stay
  blocked) while provider execution, real
  `scene.apply`, and playback stay blocked; the provider executor is
  independently write-disabled in this mode.
  Legacy HA event compatibility (`legacy_events_enabled` on the AppDaemon
  app) is independent of runtime mode: `normal` does not register those
  listeners unless the flag is explicitly true. Restricted modes never
  register them.
- **Scene authoring (Builder Pass 2)** — explicit authoring seams, distinct
  from the legacy `scene.save` (empty-draft capture, v1 parity) and
  `upsert_scene` (legacy generation; never a Builder API):
  - `scene.preview_draft {scene}` validates an **unsaved** Scene v2 draft
    through the canonical `Scene.from_dict` and compiles it with the exact
    stored-scene render-plan path (`build_render_plan`). It never persists,
    never contacts a provider, never emits an event, and never bumps the
    revision (debounced auto-preview must not flood the bounded event log).
    A missing/empty `scene.id` is derived server-side from `scene.name` via
    `normalize_name_to_id` — the one canonical identity rule; frontends
    never re-implement slug logic. Unknown target ids surface as render-plan
    notes here; save-time validation (below) is stricter.
  - `scene.create {scene, duplicate_of?}` atomically persists a new canonical document
    through `SceneStore.add_scene`: id derived/validated server-side,
    collisions against active AND archived ids are `conflict`, every
    declared target id must resolve (declared target or fixture id —
    otherwise `validation_error` at `scene.target_ids.<n>`), an operational
    event is emitted, and the scene is never applied/played as a side
    effect. Server/history metadata (`archived_at`, `migrated_from_v1`,
    `duplicated_from`) is **stripped from the payload**: a brand-new scene
    cannot claim to be archived, v1-migrated, or a duplicate.
    `duplicate_of` (optional, an existing active or archived scene id) is the
    Duplicate / Save-as-New seam — the ENGINE records
    `metadata.duplicated_from` on the new document, so a copy of a migrated
    scene never falsely claims to be the migrated artifact. Unknown
    `duplicate_of` is `not_found`.
  - `scene.update {scene_id, scene}` atomically replaces the whole editable
    intent of an **active** scene through the store's `replace_scene`:
    `scene_id` is authoritative and immutable (a conflicting payload id is
    `validation_error`; archived scenes must be restored first and report
    `conflict`); the replacement document is strictly re-validated; and
    server/history metadata the Builder does not own
    (`metadata.archived_at`, `metadata.migrated_from_v1`,
    `metadata.duplicated_from`) is re-attached
    from the stored document so an editor payload cannot erase migration or
    duplicate provenance. Live playback is deliberately left alone: the
    running session keeps its already-realized provider state, and the next
    Apply/Play uses the new definition (lifecycle operations re-read the
    stored document).
  - Store semantics (`SceneStore.replace_scene`): active scenes only, the
    stable id (and filename) never moves, `atomic_write_json` replacement,
    detached-copy returns, archived-space uniqueness unaffected. The
    targeted `update_fixture_states()` helper remains for the legacy
    generation flow only; the Builder uses the whole-document operation.
- Transports: the devserver exposes `route()` directly at
  `/api/scene_studio/*` (Workbench transport `direct`). AppDaemon exposes
  ONE RPC-style named endpoint, `scene_studio_api`
  (`/api/appdaemon/scene_studio_api`): the callback receives AppDaemon's
  decoded args object, standardizes on a POST envelope
  `{method, path, query, body}`, and always returns HTTP 200 wrapping
  `{"status", "body"}` — AppDaemon replaces 404/500 bodies with HTML, so
  JSON error payloads can only survive inside a 200 envelope (verified
  against AppDaemon 4.5.0 sources). Workbench transport `appdaemon`
  targets that endpoint; live validation pending R1.

## 5. Migration semantics (Workstream A3)

- v1 scene = `{hue: {ip, lights: {uuid: rawClipPayload}}, wled: {host, state}}`.
- Identity mapping must go through a seeded fixture registry (Phase 1
  sample `registry.sample.json` mirrors the live crosswalk; A3 may refine
  the seed but IDs must stay stable once committed).
- Apply-scope authority = the v1 crosswalk (the v1 apply path filtered
  saved states to Studio; the crosswalk records that effective scope).
  Only crosswalk-listed resources map into `fixture_states` (14 per scene:
  8 crosswalk hue incl. disabled `double_strip` + 6 WLED). Everything else
  the v1 saver captured is out of scope: states are NOT migrated; they are
  preserved as provenance only
  (`metadata.migrated_from_v1.out_of_scope_lights`: resource id, captured
  name, registry fixture id when known, status
  `registry_known_out_of_scope` | `not_in_use`). The registry remains the
  v2 identity/control authority — knowing about all bridge lights is
  correct; migrating their accidentally captured states is not.
- Disabled fixtures are excluded from migrated-scene target coverage and
  from overcover/uncovered accounting: apply skips them regardless
  (contracts §3). Their migrated states remain in `fixture_states` and the
  report surfaces them via `fixture_disabled` concerns.
- Dry-run mutates nothing and is deterministic: same input bytes → same
  report bytes.
- Migrated scenes carry
  `metadata.migrated_from_v1 = {filename, ...}` provenance.
- swatch/palette inference: v1 stores per-fixture colors, not palettes;
  migrated `palette` prefers the first mapped Hue gradient with two or more
  distinct colors; a uniform/degenerate gradient falls through to unique
  mapped fixture colors (Hue + WLED) in fixture-id order. Matching solid
  hexes become `palette_index` pins so the scene is a static palette
  assignment (auto-spread remains available for unpinned RGB fixtures).
  Always labeled `approximate` in the report.

## 6. Discovery semantics (Workstream A2)

- Discovery returns a `DiscoveryReport`; it **never** writes bindings or
  registry metadata in any runtime mode. Hue room/zone RIDs are retained in
  observation provider metadata; persisting them is an explicit
  `fixture.rebind`/reconciliation action against a selected observation.
- Required per-fixture statuses: `bound_ready`, `bound_reconcile_available`,
  `bound_degraded`,
  `missing`, `available_unbound`, `disabled`, `candidate_replacement`,
  `conflict`.
  `bound_ready` is the exact current binding with agreeing effective
  capabilities and no actionable registry drift. `bound_reconcile_available`
  is the same exact resource with safer/newer registry, profile, or
  assessment evidence — not a capability regression. `bound_degraded` is a
  genuine provider-effective capability regression or other real
  degradation. Capability-limited assessments on an otherwise `ready`
  fixture are informational, not a discovery failure. Intentionally
  disabled fixtures are skipped and are not Needs Attention issues.
- Candidate scoring inputs may include: previous binding match (resource
  id / device id), friendly-name similarity, location hints, capability
  compatibility, endpoint hints (weak signal only). Every candidate lists
  its reasons; confidence is 0..1.
- WLED `device_id` must come from a provider-stable identifier (e.g.
  MAC); the endpoint/IP is only `endpoint_hint`. Segment observations use
  real segment indexes from the device, not entity-name parsing.
- Hue discovery must not filter by room; capture room as `location_hint` and
  its authoritative room/zone RID/type as observation provider metadata.
- WLED discovery captures the device's `/json/effects` and `/json/palettes`
  catalogs (index order preserved — list position IS the fx/pal id) and the
  `info.leds.cct` white-channel flag into observation capabilities; the
  renderer resolves effect/palette names through them.
- Device identity/profile evidence is distinct from the observation's
  effective `Capabilities`. Friendly names and hardware marketing never grant
  a provider capability. A capability assessment (`nominal`, `limited`, or
  `unknown`) explains that gap while operational health remains independently
  `ready`, `missing`, etc.
- `fixture.rebind_preview` is a read-only latest-discovery comparison. It
  reports binding/capability deltas and active-scene render-fidelity impact.
  `fixture.rebind` requires a candidate effective capability set and commits
  binding, capabilities, assessment, health reset, and rollback revision in
  one registry write. Silent rebinding is prohibited.
- `fixture.reconcile_preview` / `fixture.reconcile` are the same-binding
  counterpart: valid only when the selected latest Discovery observation
  is the fixture's existing provider/resource. They never write a provider
  and never change the binding. Preview returns a deterministic registry
  delta (`binding_unchanged: true`, current/proposed health, capabilities,
  profile, assessment, scene fidelity, warnings). Apply uses the existing
  bounded binding-revision architecture so rollback restores the
  pre-reconcile revision. A different resource/provider must use rebind.
  Stronger verified physical evidence is preserved; conflicting physical
  claims warn rather than silently overwrite; names/models never invent
  gradient or dynamic capability. `Fixture.capabilities` remains renderer
  authorization.
- A legacy registry is parsed compatibly but remains persistence-locked:
  ordinary fixture mutations fail until an explicit `registry.migrate`
  performs inspect/dry-run (`registry.migration_preview`, allowed
  read-only), v1 backup, deterministic transform, atomic write, and
  validation/read-back. Already-current migrate is a no-op success.
  This boundary is repo/runtime logic only; it does not
  authorize a live registry migration.
- Rebind parity is distinct from technical validity. Preview returns
  `preserved`, `changed`, `reduced`, or `unknown` plus an explicit
  confirmation requirement. It compares semantic control capability (ranges,
  gradients, catalogs, dynamics, CCT) and separately reports render-plan
  impact for active scenes.

## 7. Workbench contract (Workstream A4)

- Plain modern JS/HTML/CSS (Lit allowed, **no TypeScript**), Vite dev
  server, source under `packages/scene_studio_workbench/`.
- Mock client implements the same client surface as the HTTP client:
  `getStatus()`, `getFixtures()`, `getScenes()`, `getDiscovery()`,
  `getRecentEvents()`, `sendCommand(envelope)` returning `CommandResult`
  shapes from `commands.py` — the mocks must consume the **same JSON
  shapes** as `services/scene_studio/fixtures/*.sample.json`.
- **No JS rendering logic.** Dry-run plans in the mock come from
  Python-generated golden fixtures (`services/scene_studio/src/
  scene_studio/devmock.py` → `src/mocks/render_plans/<scene_id>.json`,
  regenerate via `npm run goldens`). Goldens are scenario-independent
  static demo data; anything interactive uses the devserver, which runs
  the real Python engine. Do not reimplement renderer behavior in JS.
- Required views: Overview, Fixtures, Scenes, Discovery, Diagnostics.
  The Scene Builder is a mode of the Scenes workflow — reached
  from `New Scene` / scene-row `Edit` / scene-row `Duplicate`, holding a
  disposable local deep-copy
  draft, previewing through `scene.preview_draft`, and persisting through
  `scene.create`/`scene.update`; it is never a separate top-level nav
  destination and never introduces a second scene store, frontend schema,
  or execution path.
- Builder authoring scope (Builder-expansion pass): targets are offered as
  declared Rooms/Groups AND individual fixtures (canonical ids only, with
  readiness aggregated from existing fixture health); scene `default_state`
  is first-class (On/Off, brightness, color, color temperature) and
  per-fixture `fixture_states` overrides are authorable with progressive
  disclosure, where editing a supported field never deletes that fixture's
  advanced content (gradient/effect/transition/`provider_ext`); Duplicate
  opens the same Builder with the source intent and no source identity and
  saves through `scene.create` + `duplicate_of`; static/palette-cycle motion
  and speed stay editable while advanced motion (effect mode, non-auto
  strategy) is preserved and warned about; the palette stays ordered and
  canonical (add/remove/reorder/duplicate/hex/native picker) for both
  static fixture pointers (`palette_index` pins, auto-spread, or custom
  hex/gradient) and dynamic cycle, with no
  keyframe/timeline/provider-editor surface.
- No JS rendering logic: Preview presentation is a UI SUMMARY over the
  canonical render plan — `ready` / `ready with reductions` /
  `partially unsupported` / `cannot render`, derived from the existing
  `native`/`equivalent`/`approximate`/`unsupported` fidelity levels plus
  `skipped_fixture_ids`/`notes`. Approximate, unsupported, or skipped
  results must never be presented as an unqualified green "valid" preview.
- Production Scenes-row fidelity comes from the backend `scene.preview`
  path (a bounded store-side cache refreshed on catalog load, engine
  revision change, create/update, and explicit refresh; a forced refresh
  that lands mid-flight is re-run rather than dropped). Static goldens stay
  for the mock client, deterministic tests, and renderer regression fixtures
  — they are **not** production row truth; a row whose preview has not
  succeeded renders **fidelity pending**, and a failed preview renders
  **fidelity unavailable** with the backend's own reason as a tooltip —
  never zeros.
- Preview-time canonical validation is field-addressed: the error path is
  mapped back onto the offending control (inline message + invalid marker)
  for both save and preview failures, any draft change invalidates the last
  preview *attempt*, and a failed preview clears the previous server plan so
  a stale "ready" panel can never sit next to a failure.
- Required mock states: all healthy; one missing fixture; disabled fixture
  (`Double Strip`); provider offline; replacement candidate; mixed scene
  fidelity; dynamic scene active; migration warning.
- Density rules (master plan §2.10): exception-driven, inspector on
  selection, raw IDs/UUIDs only at Level 3.

## 8. Renderer payload semantics (Wave B.5 review)

Established after a coverage review against the Hue CLIP v2 spec and the
WLED 0.14 JSON API (the user's device firmware). Renderers emit dry-run
plans; fidelity + reasons document every reduction.

### Hue (`hue_v2`)

- Static: `on`/`dimming`/`color.xy`/`color_temperature.mirek`/`gradient`
  (mode default `interpolated_palette`; overridable, see ext) /`effects.status`.
- `state.transition_ms` → `dynamics.duration` (ms, clamped 0..60000).
- Static Hue intent uses direct CLIP v2 light PUT. Dynamic Hue intent uses a
  provider-native managed Hue scene and an execution group: the authoritative
  room/zone when membership matches exactly, otherwise a deterministic,
  Scene-Studio-owned reusable Hue zone for the exact participants. The
  managed scene receives palette/speed and is recalled with
  `dynamic_palette`.
- Scene Studio does **not** write `dynamics.status=dynamic_palette` directly
  to Hue lights. Managed Hue scenes/zones are provider resources tracked in
  deterministic Scene Studio state, never fixture identity. WLED remains
  direct provider-native JSON execution.
- `provider_ext["hue_v2"]`: `gradient.mode` (interpolated_palette,
  interpolated_palette_mirrored, repeated_linear, randomized),
  `dynamics` merge (ext wins per key), `raw` top-level merge; other
  top-level keys → approximate + ignored.
- `grouped_light` bindings support on/dimming/color_temperature only;
  color/gradient/effects → approximate with an explanatory note.

### WLED (`wled`)

- Segment-scoped bindings write per-segment `bri` (prevents cross-fixture
  dim bleed on multi-segment devices); top-level `bri` only whole-device.
- Effect/palette names resolve via `capabilities.effects` / `.palettes`
  (discovery captures the device's `/json/effects` + `/json/palettes`
  arrays in index order). Precedence: ext numeric `fx` > resolved name >
  unsupported. `provider_ext.wled.pal` accepts a catalog name.
- `capabilities.cct` + `color_temp_mirek` → native segment `cct`
  (Kelvin = 1e6/mirek mapped 1900..10091K → 0..255; e.g. mirek 200 → cct 97).
- `transition_ms` → top-level `transition` (100 ms units, 0..65535).
- `provider_ext["wled"]` merges into the segment payload (ext wins);
  a `top` sub-object merges into top-level state with allowlist
  on/bri/transition/ps/pl/nl/lor/mainseg/tt — enabling native preset
  recall (`ps`) and playlists (`pl`).

### Known follow-ups

- `Motion.intensity` (WLED `ix`) remains ext-only; promote to a canonical
  Motion field if authored scenes need it.
- Playback pause → WLED `frz` mapping belongs to the dynamic-playback wave.

## 9. Validation gates

- `python -m pytest services/scene_studio/tests` must pass (720 passed + 1 POSIX-only skip after the Builder-expansion pass).
- `cd packages/scene_studio_workbench && npm run smoke && npm run build && npm run browser` must pass (651 smoke checks, 66 rendered browser checks after the Builder-expansion review round).
-   `pwsh ./scripts/scene_studio/preflight_scene_studio.ps1` is the compact
  read-only production preflight (runtime policy, providers, fixture health,
  per-scene render plans, recent ERROR events, membership drift flags,
  served Workbench revision); it mutates nothing.
- `python scripts/security/scan_secrets.py` must exit 0 before any commit.
- Renderer/migration/discovery additions must ship deterministic tests
  using recorded/mock payloads, plus render-plan (dry-run) coverage — no
  live-device tests in CI.

## 10. Canonical Home Assistant bridge

Home Assistant decides **when** something happens and presents compact
daily controls. Scene Studio decides **what** scene/fixture/target
identity means. There is one HA integration seam:

- Projection entity: `sensor.scene_studio_ui` (`bridge_schema_version: 3`)
- Command event: `scene_studio_ui_command`

The implementation file remains `appdaemon_adapter/ui_bridge.py`. HA
scripts (`script.scene_studio_apply`, `script.scene_studio_target_power`)
and the compact scene-control card fire the same event. Do not add a
second permanent `rest_command` architecture beside this seam.

Projection attributes stay compact: scenes (each with its declared
`target_ids` — authoring data controllers need to resolve a scene's
lights without depending on runtime state), live sessions, `current`,
canonical `targets` membership (from `project_target_membership` /
`fixture.groups`, excluding render-skipped health), `last_command`, and
the schema version. Never mirror full Scene v2 documents or diagnostics
into HA entities.

`targets[]` coverage is honest: a fixture with no HA entity still counts
toward `enabled_fixture_count`, so `complete_ha_coverage` cannot claim
parity by dropping it. Room on/off/toggle in HA reads `ha_entity_ids` at
execution time and refuses incomplete coverage rather than silently
omitting a fixture. Toggle is a **room** decision (`resolve_target_power_action`):
if any member is currently on, every member is turned off; otherwise
every member is turned on. Per-entity `light.toggle` is not used.

The bridge allowlist stays the existing scene/playback subset. Builder,
discovery mutation, registry-admin, and arbitrary engine RPC stay
unreachable through HA.

Read-only membership drift lives on `diagnostics.export` as
`membership_drift` (and the production preflight). Flags: room mismatch,
aggregate helper modeled as fixture, incomplete HA coverage, duplicate HA
entity, empty/stale Scene Studio target.

## 11. HA-native scene routines (routines pass)

**Canonical rule: Home Assistant is the source of truth for automation
definitions. Scene Studio maintains a DERIVED routine projection and an
opinionated editor over its supported subset — never a second automation
database, never direct `automations.yaml` editing.** A HA-created compatible
routine appears in Scene Studio; a Scene Studio-created routine is an
ordinary HA automation; HA-side rename/edit/delete is detected; supported
edits round-trip; unsupported structural edits move the routine to
`recognized_advanced` (visible, readable, read-only) instead of being lost.

- **Domain projection** (`domain/routines.py`, transport-free): classifies
  each HA automation into `native_routine` (fully inside the supported
  grammar), `recognized_advanced` (references Scene Studio but exceeds the
  grammar — still projected with what is safely readable, plus
  `unsupported_reasons`), or nothing (no Scene Studio bridge reference —
  not a routine, ignored). Recognition is STRUCTURAL (it matches the
  canonical `scene_studio_ui_command` bridge event shape), never
  provenance-based; the versioned description marker
  (`Scene Studio routine (schema N)`) is informative only. BOTH HA config
  storage eras are recognized — the legacy `platform:` trigger key with
  singular `trigger/condition/action` lists, and the HA >= 2024.8
  modernized shape (`trigger:` type key, plural `triggers/conditions/
  actions` aliases; carrying both forms at once is advanced). Scene Studio
  still GENERATES the legacy shape (universally readable); HA normalizes
  on save and the read-back classifies native.
- **Supported grammar**: exactly one `time` trigger (literal whole-minute
  `at`; `sun.*`, templates, multiple triggers → advanced), no conditions or
  one `time` condition with an optional weekday selection (mon..sun;
  `holiday` → advanced), exactly one action firing the canonical bridge
  event (`event_data` = exactly `command` ∈ {`scene.apply`,
  `playback.start`} + `scene_id`; extra keys → advanced), benign
  automation-level keys only (id/alias/description/mode/initial_state/icon;
  `variables`, blueprints, `choose`/`repeat`, scripts → advanced). One
  Scene Studio scene action per routine; behavior = Apply or Play
  dynamically (Play requires a dynamic scene and is refused for static
  scenes at the command gate).
- **Gateway port** (`service/ports.HaAutomationGateway`): the ONLY HA
  contact for routines — HA's supported REST surface (config
  read/upsert/delete, `automation.*` entity states, `automation/reload`,
  `automation/turn_on`/`turn_off`). Implemented by
  `appdaemon_adapter/ha_automation.py` (credentials from the hass plugin
  config, else the add-on `SUPERVISOR_TOKEN`; tokens stay in process
  memory). Without resolvable credentials the capability reports
  `available: false` honestly — there is deliberately NO YAML fallback.
- **Routine service** (`service/routines.py`): TTL-bounded (30 s) derived
  projection cache — explicitly NOT a second persistence layer. Invalidation
  is layered: HA `automation_reloaded` / `automation.*` state-change
  signals (adapter listeners), TTL expiry, and explicit
  `GET /routines?refresh=true`; no single HA event is assumed reliable.
- **CRUD commands**: `routine.create`, `routine.update`, `routine.delete`,
  `routine.enable`, `routine.disable` (normal mode only — restricted modes
  reject external-system writes exactly like provider writes; the HA card
  bridge allowlist excludes them forever). Generated automations are
  ordinary HA automations: stable unique `ssr_*` config id, human alias
  (`Scene Studio · <scene> · <recurrence> <time>`), the versioned
  provenance description, and the existing bridge event as the sole
  execution path. **Verify-after-write**: every mutation re-reads HA's
  canonical config/state and verifies the expected outcome before
  reporting success (a create that fails verification is rolled back).
- **Optimistic concurrency**: the projection carries a normalized
  `source_digest` of the raw HA config (volatile `trace`/`source` keys
  excluded). Every update/delete/enable/disable re-fetches immediately
  before writing and compares digests; a mismatch is a structured conflict
  (`error.details.kind = "routine_source_changed"` with the current
  digest) — never an overwrite. Advanced automations are refused with
  `kind = "routine_advanced"`.
- **HTTP surface**: `GET /routines` (sanitize_tree'd, TTL-cached;
  `?refresh=true` forces a HA re-read) returns
  `{available, unavailable_reason?, routines: [RoutineProjection],
  refreshed_at, stale}`.
- **Workbench**: no new navigation section. The scene row carries a
  compact temporal chip (`<ss-routine-popover>` in the row name cell):
  clock affordance when unscheduled, `Weekdays 7:30 PM` for one routine,
  `N routines` for several; the anchored top-layer popover edits only the
  supported grammar (time, days, Apply/Play offered dynamically by scene
  motion, enable/disable, delete with two-step confirm) and shows advanced
  automations read-only, labeled Home-Assistant-managed. Archived rows
  render no routine affordance.
- **Explicitly deferred** (do not invent silently): generic HA automation
  authoring, arbitrary triggers/conditions, sunrise/sunset grammar,
  interpreting arbitrary light-service actions as scenes, scheduled
  dynamic stop (needs new semantics such as
  `playback.stop_matching {scene_id, target_id?}` — follow-up work), HA
  card routine authoring, playback lifecycle redesign.
