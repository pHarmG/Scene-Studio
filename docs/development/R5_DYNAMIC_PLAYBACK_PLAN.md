# R5 — Native Dynamic Playback: Implementation Plan & Staged Rollout

Status: **plan v2 (amended before R5A; approved for staged implementation) —
amended 2026-09-14 with the managed-zone isolation model (§2.1.1) and
final R5D live results (§8.2); per-light Hue
`dynamics.status` writes are rejected by this bridge; see §8.1**
Created: 2026-09-11 · Amended: 2026-09-12 (session-cardinality, WLED stop
semantics, Hue managed-scene resource model, orchestration boundary)
Prerequisites met: R1–R4 complete (read-only sidecar, seeded reconciled registry,
activated 7-scene catalog, Workbench live, runtime policy backend-owned).
Live WLED **16.0.1** (per-segment `frz` confirmed present), bridge scene
resource shape confirmed read-only, g_strip reports
`dynamics.status_values: ["none", "dynamic_palette"]`.

Framing: **R5 is a playback lifecycle/orchestration problem, not a renderer
rewrite.** The Hue and WLED renderers already compile substantial
provider-native dynamic intent; R5 keeps that work, fixes the one broken
abstraction (`hue.put_scene_dynamic`), and builds the session/orchestration
layer the engine currently lacks.

---

## 0. Current-state facts (verified, not assumed)

| Fact | Source |
| --- | --- |
| Executor sends EVERY hue op as a light PUT to `/clip/v2/resource/light/{ref}` — `hue.put_scene_dynamic` is not a real bridge-scene implementation | `appdaemon_adapter/adapter.py` `_hue_put` (op-agnostic) |
| `playback.pause` is "a deterministic engine state transition only" — no provider contact | `service/engine.py` docstring |
| `_playback` is `{scene_id, target_id, started_at, revision, paused}` — singular, no per-provider execution info | `engine.status()` |
| Static `scene.apply` already calls `_cancel_playback("scene applied")` — whole-session granularity | `engine._apply` |
| g_strip supports per-light `dynamics.status: "dynamic_palette"` + `speed` (0..1) | live bridge payload |
| WLED 16.0.1 exposes **per-segment `frz`** (freeze/unfreeze current effect); root-level `frz` absent | live `/json/state` probe |
| WLED 16.0.1 root state carries `lor` (live override), `mainseg`, `ps`, `pl` | live probe |
| CLIP v2 scene resources carry `{id, actions: [{target: {rid, rtype}, action: {...}}], metadata, group, ...}` — **a scene belongs to a group (room/zone)** | live `/clip/v2/resource/scene` GET |
| WLED on this network: one controller (`aabbccddeeff`), 6 segments; HyperHDR entity present (potential external realtime owner) | registry + HA areas |

---

## 1. Playback session contract & state machine (R5A)

### 1.1 Cardinality: a persisted collection, not a singular runtime

**Multiple disjoint playback sessions coexist.** The engine persists a
session collection plus an ownership index — never a singular conceptual
runtime:

```text
store/playback_state.json (atomic write, schema_version 1)
{
  "sessions": {
    "<session_id>": PlaybackSession
  },
  "ownership": { "<fixture_id>": "<session_id>" }   // derived index, rebuilt
}                                                    // + validated on load
```

```text
PlaybackSession
  session_id: <utc-stamped unique>     // stable key
  scene_id, scene_name
  target_ids: [..]
  fixture_ids: [..]                    // resolved, enabled-only ownership set
  state: active | paused | stopped | orphaned
  started_at, paused_at?, stopped_at?, stop_reason?
  preempted_by?: session_id
  fixture_executions: [FixtureExecution]
FixtureExecution
  fixture_id, provider
  execution: native_dynamic_palette | native_scene | native_effect
           | native_preset | approximate_static
  fidelity: native | equivalent | approximate | unsupported
  ok: bool, detail: str
```

- The **ownership index** (`fixture_id → session_id`) is the authority for
  preemption and cancellation; it is rebuilt from `sessions` on load and
  validated for consistency (a fixture may appear in at most one
  `active`/`paused` session; `stopped`/`orphaned` sessions release their
  fixtures).
- Retention: `stopped` sessions are kept for diagnostics (bounded — oldest
  trimmed past a retention count) and hold no ownership.

### 1.2 Command contracts (amended)

| Command | Params | Behavior |
| --- | --- | --- |
| `playback.start` | `scene_id`, `target_id?` | Creates a NEW session; **returns `session_id`** (+ per-fixture executions) in `data`. Preemption per §1.3. |
| `playback.pause` | **`session_id`** | Native pause for the session's fixtures; idempotent; `active → paused`. |
| `playback.resume` | **`session_id`** | **New canonical command.** `paused → active` via re-issue of start ops / unfreeze. From `stopped`/unknown → `conflict` ("use playback.start"). |
| `playback.stop` | **`session_id`** | Terminates that session (§1.4 semantics); releases its fixtures. Unknown/stopped session → `conflict`. |

- The previous parameterless `playback.pause` / `playback.stop` are removed
  from the catalog (pre-R5, nothing depends on them). A future convenience
  layer (e.g. target-based or "the active session" selection) sits ABOVE
  session-addressed commands; the engine contract stays session-addressed.
- Policy: `r5_validation` allows `playback.start/pause/resume/stop` +
  `scene.apply`/`scene.preview` + read-only base; `read_only` allows only
  the read-only base (all playback mutations rejected). Normal mode derives
  from the canonical catalog (which now includes `playback.resume`).

### 1.3 Deterministic semantics

| Transition | Rules |
| --- | --- |
| **start** | scene must exist and `motion.mode != static`; resolve targets → fixture ownership set. **Preemption is per overlapping session**: every `active`/`paused` session owning ≥1 requested fixture is stopped in full (whole-session stop, `preempted_by` recorded, event emitted) — but **disjoint sessions continue unaffected**. Start ops execute per fixture; per-fixture results recorded; the new session becomes `active` even with partial failures (degraded execution list). Returns `session_id`. |
| **pause** | session-addressed; idempotent (`paused → paused` = no-op success). Issues provider-native pause ops where they exist (WLED seg `frz:true`; Hue per §2.3), else records the honest approximation. Session → `paused`. |
| **resume** | session-addressed; only from `paused`. Re-issues start ops (idempotent recall / `frz:false` / restore speed). Session → `active`. |
| **stop** | session-addressed; terminates the session and releases its fixtures. Stop realization is provider-specific (§3.2 — R5B/R5D decision); **stop does NOT restore pre-playback light state** — that would be hidden state mutation; an explicit `scene.apply` changes lights deterministically. |
| **static scene.apply** | computes its target fixture set; every `active`/`paused` session owning ≥1 overlapping fixture is stopped first (native stop ops + event); disjoint sessions keep playing untouched. |
| **archive/rename of a playing scene** | existing behavior preserved (archive stops the session). |

### 1.4 Restart & provider-offline behavior

- Every transition persists the collection; on **engine restart**, previously
  `active`/`paused` sessions are reloaded and marked **`orphaned`**
  (animation is actually still running on the bridge/controller — status and
  a needs-attention event say so explicitly). Orphaned sessions are never
  auto-resumed; session-addressed `playback.stop` (native stop ops) or a new
  `playback.start` clears them. Multi-session orphaning is fully supported
  (each session keeps its own fixture set).
- **Provider offline**: executor receipts `ok=false` → that fixture's
  execution flips to `ok:false` in its session; the session stays `active`
  with a degraded execution list; status + events surface it. **No automatic
  retries in R5.**

### 1.5 Status surface — the collection, with aggregates

```text
status().playback = {
  "sessions": [ {session_id, scene_id, scene_name, state, started_at,
                 paused_at?, stopped_at?, stop_reason?, preempted_by?,
                 fixture_executions: [{fixture_id, provider, execution,
                                       fidelity, ok, detail}]} , ... ],
  "counts": { "active": n, "paused": n, "orphaned": n, "stopped": n },
  "owned_fixture_count": n,
} | { "sessions": [], "counts": {...all zero}, "owned_fixture_count": 0 }
```

No singular-session pretense: clients enumerate sessions and aggregate
counts. The Workbench renders one row/section per live session.

### 1.6 Policy (R5 mode)

`RuntimePolicy.build("r5_validation")`: read-only base **plus**
`playback.start/pause/resume/stop` and `scene.apply`/`scene.preview` (apply
required for the cancel-on-apply check; the executor fixture allowlist
remains the write boundary). Legacy listeners stay disabled in every R5
mode. Normal mode derives from the canonical catalog.

---

## 2. Hue native lifecycle (R5B) — corrected by the R5D Hue corrective pass

**Corrected provider model (binding):** the live bridge REJECTS per-light
dynamic activation — `PUT /light/{id} {"dynamics": {"status":
"dynamic_palette"}}` → 207 `attribute (.dynamics.status) cannot be written`
(proven live, first R5D run). Hue dynamic state is therefore a
**scene-resource feature**:

- **static Hue state → direct light PUT** (unchanged; no dynamics.status
  anywhere in the payload, and the `provider_ext` dynamics escape hatch
  cannot inject it);
- **dynamic Hue state → managed scene resource + dynamic_palette recall**
  for EVERY dynamic-native fixture, gradient-capable or not, whatever the
  palette size. No Scene Studio render/playback path writes
  `dynamics.status` to a Hue light.

### 2.1 Managed scene resources carry the dynamic information

The managed scene resource is a real Hue dynamic scene. Creation and update
send a **complete deterministic Scene Studio-owned representation** (never
reliant on merge state of an older resource):

- `actions` — one per participating fixture (capability-gated static light
  state), aggregated per group;
- `metadata` — the deterministic `Scene Studio · <scene_id> · <group>` label
  (immutable `scene.id`; recovery heuristic only);
- `group` — the authoritative room/zone (create; immutable afterwards);
- `palette` — built from the canonical Scene Studio palette, every hex
  converted through the existing Hue XY conversion (never copied as hex).
  The shape is the real Hue CLIP v2 ScenePalette (verified against the live
  bridge's own scene resources): `{"palette": {"color": [{"color": {"xy":
  {"x": .., "y": ..}}, ...}]}}` — a `color` ARRAY of xy color targets, not
  a `color_targets` wrapper;
- `speed` — the scene-level animation speed (canonical `motion.speed`).

Identity stays `(scene_id, hue_group_id) → bridge scene resource id`
persisted in `provider_state/hue_scenes.json`. One managed scene per
(Scene Studio scene, Hue group); actions only for participating fixtures —
a single-fixture scene in a shared group is valid and touches nothing else.

### 2.2 Corrected resource model: managed scenes are per (Scene Studio scene, Hue group)

CLIP v2 scenes belong to a **group** (room or zone) — one bridge scene
cannot span groups. Therefore Scene Studio does **not** assume one managed
bridge scene per Scene Studio scene:

```text
HueManagedScene                      // provider-managed state, persisted
  key: (scene_id, hue_group_id)
  hue_scene_resource_id: <bridge scene id>   // canonical resource identity
  hue_group_id: <room/zone rid>
  label: "Scene Studio · <scene_id> · <group name>"   // readable label +
  last_updated_at, last_recalled_at?                  // recovery heuristic —
                                                      // NOT canonical identity
store/provider_state/hue_scenes.json (atomic write)
```

- Canonical identity is the **persisted `(scene_id, hue_group_id) →
  hue_scene_resource_id` mapping**. The `Scene Studio · …` name is a
  readable label and a recovery heuristic (name search when the mapping is
  missing), never identity.
- Discovery feeds this: hue room/zone payloads already fetched by discovery
  record each fixture's parent group(s); a Scene Studio scene spanning
  fixtures in **two Hue groups** produces **two managed scene resources**,
  each recalled — per-fixture executions record which managed resource
  served them.

### 2.3 Orchestration boundary: fixture rendering contributes actions; aggregation is playback-layer

- **Fixture rendering** (existing hue renderer, adjusted) produces the
  fixture's **Hue action contribution** — the `action` object (on/dimming/
  color/gradient/dynamics) a bridge scene would apply to that light — plus
  the fixture's `hue_group_id` (from binding/discovery state). It does NOT
  emit bridge-scene device operations.
- **Hue playback orchestration** (new: `service/playback_hue.py`, engine-
  called) then:
  1. groups per-fixture action contributions by `hue_group_id`;
  2. per group: find/resolve the managed scene (`provider_state` mapping,
     else name search; else create) → create or update ONCE per group
     (`POST`/`PUT /clip/v2/resource/scene` with the group's aggregated
     `actions`);
  3. recall once per managed resource
     (`PUT {recall: {action: "active"}, dynamics: {speed}}`);
  4. records per-fixture executions (`native_scene`) + per-resource
     bookkeeping.
- The existing **static per-light rendering is preserved** (scene.apply and
  static action contributions). There is no per-light dynamic path: fixture
  rendering for dynamic-native Hue intent emits the static action
  contribution plus the `hue.put_scene_dynamic` intent marker consumed by
  the playback orchestration.
- **Executor**: `_hue_put` splits into an op dispatch — `hue.put_light`
  unchanged; new scene-resource ops
  (`hue.find_scene`/`hue.post_scene`/`hue.put_scene`/`hue.recall_scene`/
  `hue.delete_scene`) with correct verbs and URLs; scene ops carry
  `resource_ref = <bridge scene resource id>`.

### 2.4 Managed-resource cleanup/reuse

- Reuse-by-key means re-playing a scene UPDATES its per-group managed
  scenes — nothing is created per play; the bridge does not accumulate
  transient scenes.
- Deletion is NOT part of stop (managed scenes are retained for reuse). An
  explicit housekeeping command (`playback.forget_resources`) may DELETE
  managed resources not recalled within a retention window — R5+ backlog.
  User-authored bridge scenes are never touched (managed resources are
  tracked in `provider_state`, and the label is advisory only).

### 2.1.1 Isolation boundary: Scene-Studio-managed Hue zones (R5D-3 correction)

A dynamic_palette recall animates EVERY color light in the scene's group
(live-proven in R5D-2), so a room-scoped managed scene cannot isolate a
fixture subset. The provider-native isolation boundary is a
**Scene-Studio-owned zone** resolved from the exact sorted set of
participating Hue light service RIDs:

- **membership fingerprint** = deterministic SHA-256 (first 12 hex chars)
  of the sorted participant RIDs — the stable logical key;
- exact membership of an **authoritative room** → use that room directly
  (no manufactured zone; everyone participates, no anchors);
- otherwise ensure/reuse the zone labeled `SS-Z-<fingerprint>`:
  persisted fingerprint→rid is preferred (verified against the live
  membership; drifted membership is repaired to the exact set), then
  recovery by the deterministic ownership label + exact membership
  (label alone NEVER adopts or modifies a possibly user-authored zone);
  otherwise create the zone (children = the participants' device rids);
- the managed scene binds to the resolved group
  (`(scene_id, group_rid)` identity unchanged) and its actions cover the
  zone membership exactly — which is the participant set, so the
  room-level anchor workaround is gone from the isolated path;
- zones are shared by scenes with identical participant sets, never
  created/deleted per playback start/stop; provider state
  (`provider_state/hue_zones.json`) persists fingerprint, member rids,
  zone rid, label, created_at/updated_at. Cleanup of orphaned SS zones is
  an explicit bounded housekeeping operation that can never touch
  user-created zones/scenes.

### 2.5 Pause/resume/stop — recall-based (corrected)

All Hue lifecycle transitions are scene recalls of the managed resource; the
scene-level `speed` property stays on the resource (never a light-style
`dynamics` member on the recall payload):

- **start/resume**: `{"recall": {"action": "dynamic_palette"}}`;
- **pause/stop**: `{"recall": {"action": "static"}}`.

Fidelity is recorded honestly: pause/stop via static recall and resume via
dynamic_palette recall are recorded **approximate** — the second R5D live
run must observe whether static→dynamic transitions preserve enough state to
classify the lifecycle native/equivalent/approximate. No AppDaemon ticker is
involved anywhere.

---

## 3. WLED native lifecycle (R5B) — validated against live 16.0.1

### 3.1 Play / pause / resume (confirmed live semantics)

- **Play**: per-segment `col/fx/pal/sx/ix` (+ `provider_ext.wled.top` for
  preset `ps` / playlist `pl` when a validation scene uses them) — existing
  renderer output, unchanged.
- **Pause**: per-segment `{"seg": [{"id": i, "frz": true}]}` — native
  freeze confirmed on the live device; per-segment granularity preserves
  sibling isolation.
- **Resume**: `frz: false` — per current WLED semantics, `frz` is
  freeze/unfreeze of the running effect, so **`frz:false` is resume, not
  stop**.

### 3.2 Stop — an R5B/R5D provider decision, honestly classified

`frz:false` alone is **resume**, so stop must be realized one of two ways,
decided during R5B (design) and confirmed during R5D (live):

| Option | Behavior | Honest classification |
| --- | --- | --- |
| **A. Remain frozen** | leave `frz: true` on stop; session ends; segments stay frozen at the exact last frame. Subsequent Scene Studio start/static-apply **must explicitly clear stale freeze** (`frz: false` in the emitted seg ops). | `approximate_static` — exact last frame preserved, but the segment is left in a frozen (non-idle) condition the user must know about. |
| **B. Static/Solid representation** | `frz: false` + solid color (last captured primary) at current brightness. | `approximate_static` — clean idle condition, but **exact last-frame state is lost** for animated effects (mid-animation gradient/progress cannot be represented as Solid); the loss is recorded in the fixture execution reason. |

Decision criteria (R5D): whether leaving frozen segments interferes with
later manual/automation use; both options record `execution:
approximate_static` + reason — never silently "stopped natively".

**Stale-freeze rule (mandatory, both options):** every subsequent Scene
Studio `playback.start` and static `scene.apply` targeting a WLED segment
**explicitly includes `frz: false`** in the emitted segment ops, so a stale
freeze from a previous session can never swallow a new scene. (R5B renderer
change: static + dynamic WLED seg ops always emit `frz: false` unless the
op itself is a pause.)

### 3.3 Isolation, routing, ownership

- **Per-segment isolation & per-device routing**: unchanged from R2
  (per-seg ops; endpoint from binding hint / device map).
- **UDP sync NOT foundational**: `udpn` untouched. **Ownership seam for
  external realtime (HyperHDR)**: playback ops read `lor` + `mainseg` from
  the fetched state and report `external_owner_possible: true` in the
  fixture execution detail when `lor != 0` — Scene Studio never silently
  fights another realtime owner. No HyperHDR integration in this pass. **[IMPLEMENTED 2026-09-24, hyperHDR ownership pass]**: the seam is now live as the external light-sync contention layer - see `docs/scene_studio/CONTENTION_RUNBOOK.md`. WLED truth is the device's own `lor` (`live_state/wled.py` surfaces it as `external_owner`; the apply/playback gates read a fresh state per decision); Hue truth is the hyperHDR serverinfo probe (active+visible input priority on a running hue instance). The design's `external_owner_possible` is realized as the contention `yielded_fixture_ids`/`owners` receipt plus the `contended` error code.
- **Degradation reporting**: anything 16.0.1 does not support natively is
  recorded `approximate`/`unsupported` with the reason — never silently
  simulated.

---

## 4. Fallback animation — last, with a clean seam

- No universal AppDaemon palette ticker in R5. The only sanctioned fallback
  today is the documented `approximate` static snapshot (already implemented
  in renderers).
- **Seam kept**: `FixtureExecution.execution` is the contract a future
  bounded `FallbackAnimator` would claim (`execution: "fallback_ticker"`),
  plus the fixture-ownership index in the engine. Building it requires a
  demonstrated provider gap from R5D results — not before.

---

## 5. Stage boundaries & commit plan

### R5A — lifecycle contracts + engine state machine (pure, no device)
- `domain/playback.py`: `PlaybackSession`, session collection +
  ownership index (persisted `playback_state.json`, schema v1).
- Command catalog: `playback.resume` added; `playback.pause/stop` take
  `session_id`; `playback.start` result carries `session_id`.
- `RuntimePolicy`: `r5_validation` mode incl. `playback.resume`.
- Engine: session orchestration refactor (collection + ownership index,
  per-overlapping-session preemption, apply-cancels-overlap-only, session
  persistence/reload → orphaned, playback status surface).
- **Tests**: policy r5 mode incl. resume; **two simultaneous disjoint
  sessions coexist** (ownership index correct, status shows both);
  **overlap preemption stops only the overlapping session** (disjoint
  session untouched); **session-addressed pause/resume/stop** (wrong/unknown
  session → conflict; idempotent pause; resume only from paused);
  **persisted multi-session restart → orphaned** (all sessions orphaned,
  ownership released-and-remembered, no auto-resume); apply-cancels-
  overlap-only; archive playing scene stops its session; status collection
  shape + aggregate counts.
- Commit: `scene-studio: R5A playback session collection and engine state machine`.

### R5B — native provider implementation
- Renderer boundary change: fixture-level Hue dynamic rendering emits
  **per-fixture action contributions + hue_group_id** (no bridge-scene
  ops from the renderer); per-light `dynamic_palette` path unchanged; WLED
  pause (`frz:true`)/resume (`frz:false`) ops and **static + dynamic seg ops
  that clear stale freeze**.
- New `service/playback_hue.py`: group aggregation, managed-scene
  find/create/update/recall, `(scene_id, hue_group_id)` provider state
  (`store/provider_state/hue_scenes.json`).
- Executor: Hue op dispatch (light vs scene verbs/URLs), WLED `frz`
  realization (per-device routing + allowlist), `lor` read.
- Engine: start/pause/resume/stop orchestration per provider capability,
  execution recording.
- **Tests**: fixture action-contribution shape; **Hue action aggregation
  into one managed scene per group** (find→create→recall sequence;
  find→update→recall reuse); **a Scene Studio scene spanning two Hue groups
  produces two managed resources, both recalled**; WLED pause/resume/stop
  payloads with per-seg isolation; **stale-freeze cleared by start and
  static apply**; executor fake-request tests for every new op (verbs/URLs/
  payloads); `lor != 0` → external-owner detail.
- Commit: `scene-studio: R5B native hue managed-scene + wled playback operations`.

### R5C — Workbench & status integration
- Overview/Scenes: playback panel driven by the `status().playback`
  **collection** (one section per session: state, per-provider
  execution/fidelity counts, degraded fixtures), aggregate counts,
  policy-gated session-addressed buttons via `runtime.allowed_commands`,
  orphaned-session needs-attention events surfaced.
- **Tests**: smoke — multi-session panel rendering, session-addressed
  controls, policy-gated buttons (disabled in read_only), status shape
  passthrough.
- Commit: `scene-studio: R5C workbench playback sessions status and controls`.

### R5D — narrowly allowlisted live validation (user approval gate; §8.1 correction applied)

- Deploy; set `r5_validation: true` +
  `r5_fixture_allowlist: ["g_strip", "wled_seg_0"]` (+ the ordinary Hue
  lights sub-gate set, e.g. `["lamp", "middle_bar"]`, per §5.1); write temp
  scene `r5_dynamic_validation` via the store path (the seven production
  scenes untouched). Hue dynamic playback REQUIRES binding topology
  (`hue_group_id`/`hue_group_type`): the live registry must be reconciled
  with discovered room/zone ids **through the store path during deployment**
  (the r5_validation command policy intentionally excludes
  `fixture.rebind`).
- **Probe first (read-only)**: WLED `/json` (confirm `frz`, `lor`,
  catalogs), Hue GET scene list + g_strip dynamics fields. Note: a Hue
  scene RECALL is a provider mutation — there is no read-only recall probe;
  recall semantics are validated only inside an approved R5D LIVE window.
- **Live test sequence** (capture → act → read-back → restore, R2 pattern;
  corrected after the first live run):
  1. `playback.start r5_dynamic_validation` → seg0 animates (fx running);
     g_strip realizes through the managed scene: find/create
     (`Scene Studio · <scene_id> · <group>` scoped to the room) → recall
     `{"recall": {"action": "dynamic_palette"}}` → the strip animates the
     scene palette (read-back: bridge scene resource exists with palette +
     speed; no `dynamics.status` write ever issued). Siblings 1–5 unchanged;
     `session_id` returned.
  2. `playback.pause {session_id}` → seg0 `frz: true`; g_strip static
     recall — read-back + observe whether animation state survives enough
     to classify fidelity (implemented `approximate` until proven).
  3. `playback.resume {session_id}` → seg0 `frz:false`; g_strip
     dynamic_palette recall resumes animation (read-back + classify).
  4. `scene.apply` (static validation scene, allowlisted fixtures only) →
     the session auto-cancels; stale freeze cleared; lights land on static
     state.
  5. Restore captured pre-test states; read-back verify (R2 pattern).
  6. Remove temp scene; set `read_only: true`; re-verify gate.
- **Managed-scene sub-gate** (separate, only after the minimal set passes):
  ordinary Hue lights (e.g. lamp + middle_bar, same Hue group) — verify
  aggregation into ONE managed scene resource for the group, recall, and
  the §2.5 pause/resume behavior; then a two-group scene if the user wants
  the cross-group case exercised live.
- **Live exit gate (all must hold; corrected)**:
  - g_strip animates through the managed dynamic scene (bridge scene
    resource carries the canonical palette + speed; recall
    `dynamic_palette` accepted; animation observable), verified by
    read-back — **not** via a per-light dynamics write (refuted, §8.1);
  - WLED seg0 animates natively with segments 1–5 byte-identical
    (**proven in the first live run** — do not re-modify WLED);
  - pause/resume/stop each verified by read-back with recorded per-fixture
    fidelity (managed-scene lifecycle recorded approximate until this run
    classifies it; WLED stop decision outcome recorded);
  - session-addressed pause/resume/stop proven (start returns session_id;
    acting on it works; acting on a wrong/unknown session conflicts);
  - static apply canceled the session and cleared stale freeze;
  - restart during playback → orphaned session surfaced, no auto-resume;
  - zero writes outside the allowlisted fixtures (hash baselines);
  - seven production scenes, v1 files, legacy apps, `input_select` untouched;
  - Workbench shows live sessions with per-provider fidelity.
- Commit: `scene-studio: R5D staged dynamic playback validation (read_only restored)`.

---

## 6. Test matrix summary

| Area | Cases |
| --- | --- |
| Policy | r5 mode allows playback.start/pause/resume/stop + apply/preview + read-only base; rejects save/rename/archive/enable/disable/rebind; `playback.resume` in catalog + normal-mode derivation; legacy listeners off in all R5 modes |
| Session collection | **two simultaneous disjoint sessions coexist** (status shows both; ownership index correct); **overlap preemption stops only the overlapping session** (disjoint session untouched and still owned); ownership released on stop; stopped-session retention/trim |
| Session addressing | pause/resume/stop act on `session_id`; unknown/stopped → conflict; resume from active → conflict semantics; start returns session_id |
| Persistence | every transition persists; **multi-session restart → all sessions orphaned** with ownership remembered; orphan stop clears one session without touching others; corrupt playback_state.json → treated as absent + warning event |
| Status | collection shape + aggregate counts; per-fixture fidelity; empty-collection zero shape |
| Hue renderer | per-light dynamic ops (existing) intact; **action contribution + hue_group_id** emitted (no bridge ops from fixture renderer) |
| Hue orchestration | **aggregation into one managed scene per group** (find→create→recall; find→update→recall reuse); **two-group scene → two managed resources, both recalled**; provider_state mapping persisted + recovered via name search; user scenes never touched |
| WLED renderer | pause (`frz:true`) / resume (`frz:false`) per-seg; sibling isolation preserved; **start + static ops clear stale freeze** |
| Executor | hue light PUT unchanged; hue scene create/update/recall/delete/find verbs+URLs; wled frz per-device routing; allowlist + read_only still bound every write; `lor != 0` → external-owner detail |
| Orchestration | offline receipt → degraded execution; no retries; sanitizer covers playback status/exports |

## 7. Live exit gate

See §5 R5D item 6 — that checklist is the R5 acceptance test; every line is
verified by read-back or hash, never by log inspection alone.

## 8. Open questions (resolved during R5B/R5D, not blockers)

1. Hue: does `dynamics.speed: 0` truly freeze a dynamic scene/palette?
   (R5D probe decides native vs approximate pause — both are planned.)
2. WLED 16.0.1 stop decision: remain-frozen vs Solid representation
   (§3.2) — decided with live evidence in R5D and recorded per fixture.
3. Hue managed-scene update semantics: full `actions` PUT replace vs merge
   (verify live; plan assumes replace).
4. Hue group coverage: fixtures whose group is not a declared Hue room/zone
   (or span multiple groups) — per-light dynamics first, managed-scene
   grouping only where a group is authoritatively known.

### 8.1 R5D live findings (2026-09-13, first live run — aborted at 6.1)

- **RESOLVED (new): the per-light `dynamics.status` write is NOT supported
  by this bridge/firmware.** `PUT /light/{id} {"dynamics": {"status":
  "dynamic_palette"}}` → 207 `"attribute (.dynamics.status) cannot be
  written"` — standalone or combined with gradient/dimming. `dynamics.speed`
  alone IS writable. The earlier "live G Strip supports per-light
  dynamic_palette" evidence (`status_values` includes `dynamic_palette`) is
  read-only capability metadata and does not imply writability. §2.1's
  per-light start path is refuted live; WLED seg0 start + sibling isolation
  were proven native in the same run. **Correction IMPLEMENTED (repo-side,
  this corrective pass, 2026-09-13):** all dynamic Hue state now routes
  through the managed-scene path (§2.1/§2.5): the managed scene resource
  carries the canonical palette (XY-converted) + scene-level speed; start/
  resume recall `{"recall": {"action": "dynamic_palette"}}`; pause/stop
  recall `{"recall": {"action": "static"}}`; execution kind `native_scene`;
  no render/playback path emits `dynamics.status` on a light. Recall-based
  dynamics semantics are provider MUTATIONS — they are validated only in the
  second R5D LIVE run (explicitly approved), never by any "read-only probe".
  Gradient and
  dimming portions of the PUT applied normally (207 = partial success), so
  static gradient rendering is unaffected.
- **WLED seg0 native start + isolation: PROVEN live** (fx/pal/sx/bri/frz
  all exact; segments 1–5 byte-identical to baseline) — §3.1 play confirmed.
- Questions 1–4 above remain open (the run aborted before the pause probe,
  managed-scene path, and stop-decision evidence could be gathered).

### 8.2 R5D second live run findings (2026-09-13 — corrected model proven; isolation contradiction)

- **The managed-scene model works end-to-end live:** a real Hue dynamic
  scene was created (canonical palette XY + per-target dimming, scene-level
  speed, group-scoped actions) and recalled
  `{"recall": {"action": "dynamic_palette"}}`; read-back proved the
  participant light reached `dynamics.status=dynamic_palette` at the scene
  speed (`speed_valid=true`) — the recall path achieves what light PUTs
  reject. WLED native start + sibling isolation re-proven in the same run.
- **Bridge schema/semantic rules (all implemented + regression-tested):**
  1. `PalettePost` requires palette-level `dimming` + `color_temperature`
     members (empty allowed) and per-target `dimming` on color entries;
  2. `metadata.name` maxLength 32; non-ASCII separators (U+00B7) trip
     name-validity at ~31 chars → all-ASCII label
     `SS-<scene_id[:18]>-<group_rid[:8]>`;
  3. create/update requires actions covering EVERY light of the referenced
     group; empty actions rejected → read-only `hue.get_group_lights` /
     `hue.get_light` ops; non-participants get leave-unchanged
     on/dimming anchors;
  4. **GROUP-WIDE ANIMATION (material contradiction, R5 open):** a
     dynamic_palette recall animates every color light in the scene's
     group — non-participants with anchors received palette colors and
     animated. Isolation for a fixture subset inside a shared room is
     impossible with room-scoped managed scenes.
- **Decision taken (user) & implemented (R5D-3):** Scene-Studio-managed
  ZONES keyed by the membership fingerprint of the exact sorted participant
  RID set — see §2.1.1. Exact full-room participant sets use the
  authoritative room directly. Two live SS zones now exist as canonical
  reusable provider resources (SS-Z-9110400a29a9 = g_strip;
  SS-Z-67c78b157c5a = lamp+middle_bar).
- **R5D-3 final live result (2026-09-14): full matrix PASSED.** Zone-bound
  managed scene + dynamic_palette recall isolated the participant
  completely (17-of-18 lights byte-identical during playback); lifecycle
  (pause/resume/wrong-session/stop/static-apply/stale-freeze/restart-
  orphan-reclaim) proven live; second participant set got its own zone with
  replay reuse (zero duplicate resources); WLED native start + isolation
  re-proven. Live zone-create rules discovered: children reference LIGHTS
  (rtype light; device children → "Invalid children") and zone metadata
  requires `archetype`.
