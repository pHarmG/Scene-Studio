# Scene Studio — R5C Workbench Playback Integration Plan

Status: **implemented (repo-only; 2026-09-12) — R5D live-write validation remains the next explicit approval gate**  
Created: 2026-09-12  
Scope: `packages/scene_studio_workbench` frontend integration only, plus mock/smoke coverage.  
Prerequisites: R5A multi-session lifecycle contract complete; R5B provider-native realization complete and hardened; Workbench facelift **Stage 2 complete and intentionally paused**.

Implementation record (2026-09-12): all of §2–§10 landed. `playback.js` is the
canonical plural session model (scene-singleton helper removed); the reusable
`ss-playback-panel` / `ss-playback-session` components own session-addressed
Pause/Resume/Stop behind `controlsForSession` policy gating; Overview renders
every live session (no more "primary +N more"); Scenes carries the Live
playback panel, plural scene-row summaries, and the "Playback…/N sessions"
focus affordance; the mock realizes multi-session/degraded/orphaned scenarios
from the Python goldens; the smoke suite grew to 492 checks (R5C helper matrix
+ the §11 twelve-case workbench matrix). Render sweep validated at
1440×900 / 1920×1080 / ~700×900 with the Stage-2 shell intact and zero console
errors. No backend, provider, or live-system change was made.

Corrective pass (2026-09-13, post-acceptance review): Overview's rendered
panel is wired to the shared session-addressed routing seam
(`playbackActionEnvelope`, also adopted by Scenes); the scene-row status
glyph no longer claims "playing" for live scenes (the runtime tag owns
playback-state wording — see the reconciled Session tone above); and a
dependency-free rendered-path regression (`scripts/browser_regression.mjs`,
`npm run browser`) drives real clicks on both views' panels plus the
label matrix. Smoke is now 500 checks; the tone wording in §3 above was
reconciled to match the implemented `sessionTone` decision.

This document is the implementation handoff for the next fresh GLM cycle. It is deliberately narrower than the broader Workbench facelift plan. R5C makes the existing R5 playback contract fully operable and legible in the Workbench **without beginning the Stage 3+ visual redesign**.

The design principle is:

> **R5C supplies the correct session model, controls, status summaries, and reusable playback components. Claude's later facelift stages may restyle/recompose those components, but must not need to rediscover or rewrite the playback behavior.**

No live deployment or provider write belongs to R5C. R5D remains the explicit live-write validation gate.

---

## 0. Start-of-cycle reconciliation gate

Before changing code, read the latest versions of:

- `docs/scene_studio/IMPLEMENTATION_STATUS.md`
- `docs/scene_studio/R5_DYNAMIC_PLAYBACK_PLAN.md`
- `docs/scene_studio/WORKBENCH_FACELIFT_PLAN.md`
- `packages/scene_studio_workbench/src/playback.js`
- `packages/scene_studio_workbench/src/state.js`
- `packages/scene_studio_workbench/src/views/overview.js`
- `packages/scene_studio_workbench/src/views/scenes.js`
- `packages/scene_studio_workbench/src/components/ss-scene-row.js`
- current mock client + smoke suite

Also inspect the final R5B-hardening commit that immediately precedes this phase.

R5C assumes the following public backend contract remains stable after final R5B hardening:

```text
status().playback = {
  sessions: PlaybackSession[],
  counts: {active, paused, orphaned, stopped},
  owned_fixture_count: number
}

PlaybackSession = {
  session_id,
  scene_id,
  scene_name,
  target_ids,
  fixture_ids,
  state: active | paused | stopped | orphaned,
  started_at,
  paused_at?,
  stopped_at?,
  stop_reason?,
  preempted_by?,
  fixture_executions: FixtureExecution[]
}

FixtureExecution = {
  fixture_id,
  provider,
  execution,
  fidelity,
  ok,
  detail
}
```

Canonical lifecycle commands:

```text
playback.start  {scene_id, target_id?} -> returns session_id
playback.pause  {session_id}
playback.resume {session_id}
playback.stop   {session_id}
```

If final R5B hardening changes any of those **public** contracts, amend this plan before implementation rather than adding frontend workarounds. Internal Hue topology/resource changes should not affect R5C.

---

## 1. Scope boundaries

### R5C owns

- canonical frontend selectors/helpers for the playback session collection;
- multi-session presentation;
- per-session lifecycle controls;
- per-provider execution/fidelity summaries;
- degraded and orphaned playback visibility;
- policy gating from `status().runtime.allowed_commands`;
- Workbench mock data/behavior needed to exercise the real contract;
- smoke/browser coverage for all playback UI states;
- minimal functional integration into Overview and Scenes.

### R5C does **not** own

- provider implementation or backend lifecycle semantics;
- Hue/WLED behavior changes;
- R5D deployment or live lighting writes;
- the generic fallback animator;
- HyperHDR ownership/arbitration;
- Workbench facelift Stage 3/4 visual redesign;
- a new navigation shell;
- a broad inspector redesign;
- backend policy duplication in JS.

The Stage-2 shell (`Overview · Scenes · Fixtures`, Inbox, System drawer, authoritative header health pill) is the baseline and should remain structurally stable during R5C.

---

## 2. Critical model correction: sessions are fixture-owned, not scene-singleton

The current Stage-2 compatibility helper `sessionForScene()` carries an unsafe assumption: it states that the engine ownership model guarantees at most one live session per scene.

That is **not** an R5A invariant.

The backend guarantees:

```text
one active/paused owner per fixture
```

not:

```text
one live session per scene_id
```

The same Scene Studio scene may legitimately run as two disjoint sessions against different target/fixture subsets.

Example:

```text
scene: aurora
session A -> office fixtures
session B -> bedroom fixtures
```

Both can coexist if the fixture ownership sets do not overlap.

R5C must therefore remove scene-singleton assumptions from the frontend.

### Required canonical helpers

Evolve `playback.js` into the single read-side session model. At minimum provide pure helpers equivalent to:

```text
normalizePlayback(raw)
liveSessions(playback)                 // active + paused + orphaned
activeSessions(playback)
pausedSessions(playback)
orphanedSessions(playback)
stoppedSessions(playback)
sessionsForScene(playback, scene_id)   // ZERO, ONE, OR MANY
sessionById(playback, session_id)
hasSendableSessionId(session)

summarizeSession(session)
summarizePlayback(playback)
controlsForSession(session, allowed_commands)
```

`sessionForScene()` may remain temporarily only as a deprecated convenience for call sites that explicitly require `0..1`; it must not be used for lifecycle control routing.

### Legacy compatibility

Keep the existing pre-R5A singular-status compatibility until the live backend has actually been upgraded in R5D.

A synthesized legacy session:

- remains clearly marked `legacy: true`;
- is displayable;
- never yields a sendable `session_id`;
- never causes the UI to send a fabricated session-addressed lifecycle command.

Do not proliferate legacy-shape awareness outside `playback.js`.

---

## 3. Session summary model — aggregate without hiding failure

The Workbench must not recreate provider logic. It only summarizes backend-issued `FixtureExecution` records.

For each session derive a compact view model:

```text
state
scene_id / scene_name
target_ids
fixture_count
ok_count
failed_count
provider summaries
fidelity summaries
degraded_fixture_ids
```

### Provider summary

Group executions by `provider` and report counts, for example:

```text
Hue   4/4 ok   · 3 native_scene · 1 native_dynamic_palette
WLED  2/3 ok   · 2 native_effect · 1 failed
```

Do not promote every execution kind into a permanent chip. Compact text/glyph treatment is preferred; deeper detail can expand only when something is degraded or the user asks for it.

### Fidelity summary

Count the backend values exactly:

```text
native
equivalent
approximate
unsupported
```

Never infer `native` because an operation "looks right". `FixtureExecution.fidelity` is authoritative.

### Session tone

Use existing status semantics. Implemented decision (reconciled 2026-09-13
after review — see playback.js `sessionTone`): the health tone and the
session state are two separate signals; a paused session is not degraded
health.

- **ok (green)**: healthy active **or paused** session with all reported
  executions `ok=true` and no unsupported realization. The paused state
  stays explicitly visible through the Playing/Paused/Orphaned state
  text/badge, not through the health tone;
- **warn (yellow)**: partial failure, an explicitly unsupported
  realization, orphaned sessions (always — the provider may still be
  animating after engine restart), or unverifiable health when no execution
  records exist (transitional ambiguity, e.g. legacy sessions);
- **err (red)**: every reported execution failed / no useful provider
  realization;
- **idle (gray)**: stopped/history-only.

Do not introduce a new status color system.

---

## 4. Reusable UI components

Implement the R5C behavior in reusable components rather than embedding session logic independently in Overview and Scenes.

Recommended boundary:

```text
components/ss-playback-panel.js
components/ss-playback-session.js
```

Exact filenames may vary if the existing component structure suggests a cleaner fit.

### `ss-playback-panel`

Responsibilities:

- receives normalized playback collection + scene/fixture catalogs + runtime policy;
- renders **live sessions only by default** (`active`, `paused`, `orphaned`);
- shows aggregate playback counts in one compact header;
- renders one `ss-playback-session` per live session;
- optionally reports `N recent stopped` as subdued history text, but does not dump the bounded stopped-session retention list into the daily cockpit;
- emits session lifecycle events upward; never calls backend clients directly.

Empty state should be compact:

```text
Playback idle
```

not a giant empty card.

### `ss-playback-session`

Each session row/section should make the following understandable at a glance:

1. **What:** scene name.
2. **Where:** target(s), with fixture count as secondary information.
3. **State:** Playing / Paused / Orphaned.
4. **How well:** provider/fidelity summary and visible degradation count.
5. **What can I do:** the correct session-addressed controls.

Do not render a wall of fixture rows when everything is healthy.

When degraded, expose failed fixtures with useful detail through a compact disclosure or secondary line:

```text
2 issues
  wled_seg_0 — provider unavailable
  lamp — managed scene recall failed
```

Use fixture display names when the catalog can resolve them; retain fixture IDs in technical detail/tooltips.

### Accessibility

- buttons have explicit `aria-label` / tooltip text;
- state is conveyed by text/glyph as well as color;
- keyboard focus remains visible;
- disclosures are keyboard-operable;
- respect existing reduced-motion policy; R5C should not add continuous decorative animation.

---

## 5. Canonical lifecycle control behavior

The panel is the authoritative place for session-addressed lifecycle controls.

### Active session

Available actions:

```text
Pause  -> playback.pause {session_id}
Stop   -> playback.stop  {session_id}
```

### Paused session

Available actions:

```text
Resume -> playback.resume {session_id}
Stop   -> playback.stop   {session_id}
```

Do **not** continue showing `Pause` on a paused session merely because backend pause is idempotent. The UI should express intent accurately.

### Orphaned session

An orphan means Scene Studio restarted while the provider may still be animating.

Show explicit copy such as:

```text
Engine restarted; provider playback may still be running.
```

Available lifecycle action:

```text
Stop -> playback.stop {session_id}
```

Do not offer Resume or Pause. Starting/applying another scene remains a scene-level operation and may supersede the orphan through backend rules.

### Legacy synthesized session

Show the current state, but session-addressed controls are disabled with a specific explanation:

```text
Session controls require the R5 backend upgrade.
```

Never fall back to pre-R5 sessionless pause/stop calls.

### Policy gating

Backend-owned runtime policy is authoritative.

For each button require BOTH:

```text
real/sendable session_id
AND
runtime.allowed_commands includes the command
```

Disabled controls should explain whether the cause is:

- old backend / no real session ID;
- current runtime mode (`read_only`);
- session state incompatibility.

No frontend-maintained shadow permission table.

---

## 6. Overview integration

R5C should improve the existing Overview playback section functionally without beginning the facelift's Stage-3 hero/cockpit redesign.

### Replace singular "primary session" presentation

The current Overview chooses the first active/paused session and renders `+N more`. That is insufficient for R5.

Replace that behavior with the reusable playback panel or a compact variant of it.

Overview should show:

```text
Playback
2 playing · 1 paused · 0 orphaned
[session A]
[session B]
[session C]
```

All live sessions are individually addressable.

If there are many sessions, preserve one-viewport discipline through compact rows and internal disclosure rather than giant cards.

### Needs-attention integration

The existing Stage-2 shared health summary already surfaces orphaned playback. Extend the authoritative exception builder so that **degraded live sessions** are also visible when any `FixtureExecution.ok == false` or fidelity is explicitly unsupported.

Do not duplicate this logic independently in the header and Overview. The Stage-2 `buildOverviewExceptions()` single-source rule remains.

Examples:

```text
Aurora: 1 playback fixture failed
Office Glow: playback orphaned after engine restart
```

A View action should land on the relevant session presentation or the Scenes view with the correct scene/session highlighted where practical.

---

## 7. Scenes integration

Scenes remains the catalog; playback sessions are runtime instances. Do not collapse those concepts together.

### Scene → sessions relationship

A scene row must be able to represent:

```text
0 sessions
1 session
N sessions
```

Use `sessionsForScene()`.

Do not route controls by arbitrarily choosing "the first" session.

### Recommended R5C layout

At the top of Scenes, render the reusable **Live playback** panel when at least one live/orphaned session exists. Below it, retain the Stage-2 scene catalog/list.

Scene rows should receive a compact runtime summary such as:

```text
Playing
Paused
2 sessions
1 issue
```

rather than one opaque `playbackSession` object.

### Scene-row actions

Keep scene-level actions (`Apply`, `Dry`, `Rename`, `Archive`, `Restore`) on the scene row according to existing Stage-2 behavior and policy gating.

For dynamic playback:

- if the scene has **zero** live sessions, `Play` remains valid (`playback.start {scene_id}`);
- if the scene has one or more live sessions, do not expose ambiguous row-level Pause/Stop that select one session implicitly;
- instead provide a lightweight local action/affordance such as `Playback…` / `N sessions` that focuses or scrolls to the corresponding session(s) in the Live playback panel.

This is intentionally a functional R5C correction, not the Stage-4 scene-row visual redesign.

If implementation evidence strongly favors keeping direct row controls for the exactly-one-session case, they must disappear or become unambiguous as soon as `sessionsForScene().length > 1`. The reusable panel remains the canonical control surface.

### Resume support

Current Stage-2 row handling has Pause and Stop but no canonical Resume path. R5C must ensure paused sessions expose `playback.resume` somewhere operationally obvious—preferably the session component rather than expanding scene-row action clutter.

---

## 8. Selection/focus behavior

Avoid inventing a broad new inspector architecture in R5C.

A lightweight session focus model is enough:

```text
selectedSessionId: string | null
```

or an existing selection shape extended to:

```text
{type: "playback", id: session_id}
```

Use it only if it materially improves cross-navigation between:

- Overview exception → playback session;
- scene row `N sessions` → session panel;
- playback row → corresponding scene.

Do not turn R5C into the facelift's contextual inspector redesign. If a full playback inspector would duplicate what the session component already displays, skip it and leave that to Claude Stage 3+.

---

## 9. Mock-client and scenario requirements

The mock is a behavioral test harness, not a second engine. It only needs enough fidelity to exercise the frontend contract deterministically.

Ensure mock support for:

- collection-shaped playback status;
- stable real-looking `session_id` values;
- session-addressed pause;
- session-addressed resume;
- session-addressed stop;
- targeted mutation of the requested session only;
- multiple simultaneous sessions;
- same-scene/disjoint-target multiple sessions;
- per-fixture provider execution/fidelity records;
- partial/degraded execution;
- orphaned sessions;
- retained stopped session(s) without showing them as live.

Add focused visual scenarios if useful. Preferred additions are small and intentional, for example:

```text
multi-session
playback-degraded
playback-orphaned
```

Do not turn the scenario picker into a combinatorial catalog. Existing scenarios may be extended where that produces cleaner coverage.

The mock must never become the source of provider semantics; fixture execution values should resemble real backend results already covered by Python tests.

---

## 10. Command feedback and refresh

`state.sendCommand()` already refreshes Workbench state after command success/failure. Preserve that architecture.

Do not manually mutate session arrays in view components after Pause/Resume/Stop.

Improve command notices enough to be useful:

```text
Playback started: Aurora
Playback paused: Aurora
Playback resumed: Aurora
Playback stopped: Aurora
```

When a command fails, preserve the backend error and leave the refreshed session state authoritative.

Avoid optimistic UI transitions that can lie about provider realization.

---

## 11. Tests and validation matrix

R5C is not complete on visual inspection alone.

### Pure helper coverage

Cover at minimum:

- empty collection;
- active/paused/orphaned/stopped classification;
- legacy singular normalization;
- real session ID vs synthesized legacy ID;
- `sessionsForScene()` returns multiple sessions for the same scene;
- execution summary by provider;
- fidelity counts;
- degraded fixture detection;
- control availability by session state + runtime policy.

### Workbench smoke coverage

Add/assert:

1. **Zero sessions** — compact idle state; no lifecycle buttons.
2. **One active session** — Pause + Stop target the exact `session_id`.
3. **One paused session** — Resume + Stop; no Pause button.
4. **Two disjoint sessions** — both render; controlling one does not mutate the other.
5. **Two disjoint sessions for the same scene** — both render independently; scene row does not arbitrarily control only one.
6. **Partial provider failure** — session remains visible; failed fixture/provider summary is obvious.
7. **Orphaned session** — warning copy visible; Stop available if policy allows; no Resume.
8. **Stopped retained session** — excluded from live panel; aggregate/history treatment remains quiet.
9. **Read-only policy** — all playback mutation controls disabled despite visible sessions.
10. **R5 validation/normal policy** — appropriate lifecycle controls enabled.
11. **Legacy live-backend shape** — display remains correct; session-addressed controls disabled rather than fabricated.
12. **Command refresh** — pause/resume/stop round trip updates rendered state from refreshed status.

### Existing Stage-2 regression sweep

Re-run and preserve:

- Overview / Scenes / Fixtures primary nav;
- Inbox / Discovery;
- System drawer;
- Diagnostics routing;
- connection mode and mock-scenario controls;
- inspector docked + narrow overlay;
- authoritative header health pill;
- zero console errors.

### Render validation

Validate at:

- 1440×900;
- 1920×1080;
- narrow ~700×900.

Check specifically that multiple session rows do not create horizontal overflow or push core controls outside the visible content area.

### Build/gates

Run:

- full Workbench smoke suite;
- production Vite build;
- JS parse/static checks used by the repo;
- Python suite only if shared fixtures/goldens/contracts were touched (frontend-only R5C should normally not require backend edits);
- secret scan if the standard project gate runs it.

No live HA/AppDaemon deployment in this phase.

---

## 12. Suggested implementation sequence

### R5C-1 — normalize/selectors

- fix scene-singleton assumption in `playback.js`;
- add plural selectors + summaries + control-state helpers;
- add helper tests/smoke assertions;
- no view redesign yet.

### R5C-2 — reusable playback components

- build compact `ss-playback-panel` + session row/section;
- lifecycle event contract is session-addressed;
- policy-gated active/paused/orphaned controls;
- degraded execution disclosure.

### R5C-3 — Overview + Scenes integration

- Overview renders all live sessions instead of one primary session;
- shared health exceptions include degraded playback;
- Scenes gets the Live playback panel;
- scene rows use plural session summaries and eliminate ambiguous session control routing;
- canonical Resume exposed.

### R5C-4 — mocks + validation + cleanup

- multi-session/degraded/orphaned mock coverage;
- exact smoke matrix above;
- browser render sweep;
- remove obsolete singular playback helpers/call sites where safe;
- update docs/ledger.

Prefer one coherent R5C boundary commit after all gates are green rather than leaving half-integrated UI commits on main.

---

## 13. Exit gate

R5C is accepted only when all are true:

- Workbench consumes the R5 collection model without singular-session assumptions.
- Two disjoint sessions, including **two sessions from the same scene**, are independently visible and controllable.
- Pause / Resume / Stop always send the correct backend-issued `session_id`.
- Runtime policy is authoritative; read-only backend cannot appear operable.
- Overview exposes every live session rather than `primary + N more`.
- Scenes distinguishes catalog identity from runtime session instances.
- Per-provider execution/fidelity and degraded fixtures are visible without overwhelming healthy sessions.
- Orphaned playback is unmistakably a needs-attention state.
- Stopped-history retention does not clutter the operational cockpit.
- Legacy pre-R5 backend shape remains display-compatible until R5D deployment.
- Stage-2 shell behavior remains intact.
- Workbench smoke/build/render gates are green.
- No backend/provider/live-system changes occurred.

After acceptance, update `IMPLEMENTATION_STATUS.md` to mark R5C complete and keep R5D as the next explicit approval gate.

---

## 14. Handoff to facelift Stage 3+

Once R5C is accepted, Claude may resume the Workbench facelift from Stage 3 against the now-stable playback components and selectors.

Claude should be free to:

- make active playback the Overview visual focal point;
- redesign scene rows;
- add richer palette/mood treatment;
- recompose the session panel visually;
- improve contextual inspector behavior.

But Stage 3+ should **reuse**, not replace, the R5C behavioral seams:

```text
playback.js canonical selectors/summaries
ss-playback-panel / session component event contract
session-addressed lifecycle commands
runtime-policy gating
backend-issued execution/fidelity records
```

That separation is intentional: R5C proves the controls and runtime truth; the facelift then makes them beautiful.

---

## 15. Safety statement

R5C is repository/frontend work only.

**Do not:**

- deploy R5A/R5B/R5C to live HA;
- enable `r5_validation` live;
- create validation scenes on the live store;
- contact Hue/WLED providers from tests;
- perform any R5D write;
- resume facelift Stage 3 inside the R5C implementation cycle.

Stop after the R5C commit/report and wait for review before R5D or further visual work.
