# Workbench Stage 3+ — Design Handoff & Implementation Mapping

Status: **implemented and current** — Stage 3+ shipped, and the two items it
deferred (WLED-segment condensation, the scene-row overflow menu) shipped in
the post-R5 visual polish pass; see "Post-R5 visual polish" below.
Source of truth: [Scene Studio Workbench Redesign](https://claude.ai/code/artifact/8028a35c-dbb2-47a6-9707-9e9fe7b96b47)
(Claude Design canvas, 6 artboards). This document plus the reference images
and `.dc.html` source under `redesign_reference/` are the durable, offline
copy — reconstructable without the canvas.

Read `WORKBENCH_FACELIFT_PLAN.md` first (it is still the design-language
rationale: status semantics, type scale, motion, layout-width reasoning).
This document covers only what that plan did not already specify concretely,
plus the concrete implementation mapping.

## Reference images

`redesign_reference/01-overview.png` … `06-mobile.png` — real headless-Chrome
renders of the six accepted artboards (not hand-drawn mockup screenshots).
Raw, re-seedable `.dc.html` source + `canvas.json` is under
`redesign_reference/source/`.

| # | File | Screen / state |
|---|------|-----------------|
| 01 | `01-overview.png` | Overview — one active session (Aurora Flow, hero) + one degraded condition (WLED offline) |
| 02 | `02-scenes.png` | Scenes — multi-session live tag, mixed fidelity, archived disclosure |
| 03 | `03-fixtures.png` | Fixtures — room-grouped |
| 04 | `04-discovery.png` | Discovery — grouped inbox (candidates / missing / new-unbound / disabled) |
| 05 | `05-system.png` | System drawer |
| 06 | `06-mobile.png` | 390px collapsed shell |

## Visual rules not already in WORKBENCH_FACELIFT_PLAN.md

- **Type scale mapped to concrete uses**: 11px micro/mono labels and status
  chips, 12px meta/dim text, 13px row/body text (existing base size,
  unchanged), 15px row-primary name emphasis, 20px section/drawer headline
  (e.g. "System"), 28px reserved for exactly one thing: the Overview hero
  headline.
- **Panel radius**: 14px (`--ss-radius-lg`, new token) on primary containers
  (hero, Fleet/Needs-attention panels, System drawer sections); list rows
  stay flat (existing `--ss-radius`/`--ss-radius-sm`, unchanged) — panels
  read as "panels," rows stay dense.
- **Hero gradient wash**: a `linear-gradient(120deg, …)` built from the
  active scene's own palette at ~12–16% alpha over `--ss-surface`, never
  touching the semantic status tokens. Derive the 4 gradient stops from
  `scene.palette` (already on every scene JSON) — no new data.
- **Left-edge severity bars**: 3px solid left border in `--ss-err` /
  `--ss-warn`, err before warn, on Needs-Attention items and Discovery
  entries (Discovery rows already have this via `statusTone`; Overview's
  Needs-Attention list did not).
- **LIVE pulse**: a single `~2.4s ease-in-out` opacity pulse (`0.3↔1`) on a
  small dot inside the runtime tag for any row with a live session —
  `prefers-reduced-motion` must disable it (`@media (prefers-reduced-motion:
  reduce) { animation: none; }`). This is the **only** continuous animation
  in the product.
- **Icon-first actions**: every cryptic text abbreviation (`Ren`, `Arch`,
  `STA`/`DYN`, capability initials) becomes a 13–16px stroke icon with a
  `title` + accessible name carrying the word the abbreviation used to show.
  Exception (load-bearing, see Deviations): `ss-playback-session`'s
  Pause/Resume/Stop controls **keep literal text** — `browser_regression.mjs`
  asserts `button.textContent.trim() === "Pause"` etc.
- **Fixture grouping key**: group by `fixture.groups[0]` when it isn't
  `"whole_house"` (the registry's own room/whole-house convention — no new
  field). A collapsed group shows a static `N fixtures` summary; expanding
  is per-group, independent of other groups.
- **Discovery bucket order**: Candidates to review → Missing to resolve →
  New/unbound → Disabled (worst-first, matching `statusTone` severity), each
  a native `<details>` with a badge count — same exact per-entry actions,
  just regrouped.
- **Mobile shell**: wordmark shortens to "Scene Studio" under ~420px; header
  icons never clip (verified render at 390px); nav/tab row and hero/panels
  stack to one column below the existing 900px breakpoint (no new
  breakpoint introduced).

## Component-by-component implementation mapping

```
target element                          -> existing component/view
                                            keep / restyle / restructure / replace
                                            shared primitive required
                                            behavioral contract that must remain unchanged
```

**Overview active-playback hero**
- `views/overview.js` — restructure (Engine+Fixtures panels merge into
  Fleet; playback gets top billing).
- `components/ss-playback-session.js` — **restyle via new opt-in
  `variant="hero"` prop** (bigger name/state text, gradient wash, inline
  Pause/Stop) — the SAME `#controlItems()`/`#emit()`/`summarizeSession`
  read path, so `playback-action` events are byte-identical.
- `components/ss-playback-panel.js` — restructure: new opt-in `heroFirst`
  prop picks the most-attention-needing/most-recent live session (orphaned
  > degraded > plain active) for the hero slot; every other live session
  renders as a normal, fully-controllable `<ss-playback-session>` row under
  an "Also playing" label (not a separate lighter-weight chip widget — the
  existing row treatment already reads as compact, and reusing it keeps
  every session individually pause/resume/stop-able with zero new markup).
  Only Overview passes `heroFirst`; Scenes' panel is visually untouched.
- Contract that MUST remain unchanged: `playback.js` selectors
  (`normalizePlayback`, `liveSessions`, `summarizeSession`,
  `controlsForSession`), `playbackActionEnvelope` → `store.sendCommand`
  routing, exact `session_id` per action, `Pause`/`Resume`/`Stop` button
  text (regression-asserted), the `.head` counts line text pattern
  (`"N playing"`, `"N recent stopped"` — regression-asserted).

**Needs Attention hierarchy**
- `views/overview.js` `.exceptions`/`.exception` — restyle: 3px left
  severity bar + icon (alert-circle err / alert-triangle warn) replacing
  the bare status dot; same `buildOverviewExceptions` array, same `#goto`
  click routing, same sort order (already worst-first in the builder).

**Condensed Fleet presentation**
- `views/overview.js` — restructure: merge the separate Engine panel and
  Fixtures panel into one `Fleet` panel (provider dots + ready/missing
  counts). Pure template change; `st.engine`, `st.provider_links`,
  `st.providers`, `st.fixtures` fields all already read exactly as today.

**Scenes identity rows + gradient swatch bands**
- `components/ss-swatch-band.js` — restyle: new opt-in `variant="band"`
  (single linear-gradient bar) alongside the existing default `"squares"`
  mode (zero change for any caller that doesn't pass `variant`).
- `components/ss-scene-row.js` — restyle: icon-based `#actionItems()`
  (Apply/Dry/Ren/Arch/Restore/Play/Playback-focus become icon + `title` +
  `ariaLabel`, same action keys, same `#available()` gating, same
  `scene-action` event payloads), swatch band switched to `variant="band"`,
  and the `STA`/`DYN` text chip replaced with a waveform/steady-dot icon
  (title-only label) — matching the accepted artboards exactly; not
  regression-asserted (no script reads `.mode`'s text).
- Contract that MUST remain unchanged: `scene-action` event `{action,
  scene_id, name?}`, `#commandFor`/`#available` policy-gating,
  `.runtime-tag` textContent (`"Playing"`/`"Paused"`/`"Orphaned"`/`"N
  sessions"` — regression-asserted verbatim), `.name ss-status-glyph`
  label never `"playing"` (regression-asserted).

**Live-session presentation (Scenes' own Live playback panel)**
- Visually untouched beyond shared token/type-scale updates — the
  artboards' "2 sessions" chip on Aurora Flow's *row* is
  `ss-scene-row`'s existing `#runtimeTag()` output restyled, not a change
  to the Live playback panel itself.

**Archived disclosure**
- `views/scenes.js` — restructure: partition `scenes` into active/archived
  before render, render active rows in the existing `<ss-panel
  variant="list">`, archived rows inside a native `<details>` with a
  `Archived (n)` `<summary>`. Same `<ss-scene-row>` instances, same
  `scene-action`/`select-scene` listeners.

**Fixtures room grouping + WLED condensation**
- `views/fixtures.js` — restructure: group `s.fixtures.fixtures` by the
  first `fixture.groups[]` entry that isn't `"whole_house"` (falling back
  to "Ungrouped"), each group a `<details open>` with a `N fixtures`
  summary — every group starts expanded, so nothing is hidden by default.
  Same `<ss-fixture-row>` instances, same `select-fixture` event, same
  inspector wiring.
- `components/ss-fixture-row.js` — restyle: the RGB/TEMP/GRADn/DYN text
  chips become small icon glyphs (droplet/thermometer/layers/waveform),
  same supported/not-supported signal (border color), label moved into
  `title`.
- **WLED-segment condensation shipped in the post-R5 pass** — see
  "Post-R5 visual polish" below for the `ss-fixture-cluster` design.

**Discovery workflow grouping**
- `views/discovery.js` — restructure: `#exceptions()` output regrouped by
  entry-status category into 4 `<details>` buckets (Candidates review /
  Missing / New-unbound / Disabled), badge-counted, worst-first. Same
  `#action()` switch, same command dispatch, same `entryKey`/dismiss
  semantics — purely a rendering regrouping of the identical array.

**System drawer**
- `app.js` `.system-panel`/`.system-section`/diagnostics — restyle only:
  new type scale, a darker diagnostics sub-surface
  (`--ss-surface-3`, new token) and monospace-leaning event rows. Same
  Connection/Scenario/Runtime-policy/Diagnostics sections, same
  `ss-view-diagnostics` embed, same `<ss-drawer floating backdrop>` usage
  (regression-asserted attribute), same `.system-head .close`
  structure (regression-asserted selector).

**390px mobile shell**
- `app.js` header CSS — restyle/fix: wordmark truncates/shortens, icon
  cluster never clips below ~390px (verified render), nav tabs remain the
  exact same 3 buttons/labels (regression-asserted array). No new
  breakpoint; extends the existing `900px` rule's intent downward.

## Icon vocabulary

One 13–18px inline-SVG stroke family (Feather-style: `stroke="currentColor"
stroke-width="2"`, 24 viewBox, rounded caps/joins), defined once in
`src/components/icons.js` and imported wherever needed. No emoji, no icon
font.

| Concept | Icon | Used in |
|---|---|---|
| Apply (static scene) | bolt/zap | Scenes row primary action |
| Play (start dynamic) | filled triangle | Scenes row |
| Pause / Stop | — kept as literal text, see Deviations | Live playback panel |
| Dry run | flask/beaker outline | Scenes row overflow menu |
| Rename | pencil | Scenes row overflow menu |
| Archive | archive-box | Scenes row overflow menu |
| Restore | counter-clockwise arrow | Scenes archived row (primary, no overflow) |
| Dynamic mode | small waveform | Scenes mode chip |
| Static mode | filled dot | Scenes mode chip |
| Missing / error | alert-circle | Needs Attention, Discovery, Fixtures |
| Degraded / warning | alert-triangle | Needs Attention, Discovery |
| Healthy / resolved | check-circle | (reserved; not required by any current row) |
| Disclosure collapsed/expanded | chevron-right / chevron-down | Fixtures groups, Discovery buckets, Scenes archived |
| Overflow menu ("...") | 3 dots (trigger) | Scenes row — real popover, see below |
| Retry | refresh arrows | Discovery, Fixtures |
| Run discovery | search/magnifier | Discovery header |
| Capability: color | droplet | Fixtures capability chip |
| Capability: color temp | thermometer | Fixtures capability chip |
| Capability: gradient | layers | Fixtures capability chip |
| Capability: dynamic | waveform (shared with mode chip) | Fixtures capability chip |

Inbox and gear/settings icons are **not** re-drawn — `app.js`'s existing
paths are reused byte-for-byte for shell consistency (the header ships
inside `app.js`, not `icons.js`).

## Responsive rules

- Existing `900px` breakpoint (drawer becomes overlay, scene/fixture row
  columns collapse) is unchanged — Stage 3 does not move it.
- New: header content must not clip down to ~360px. Verified by render at
  1440×900, 1920×1080, 700×900, and 390×844.
- Hero/Fleet/Needs-Attention stack to one column under the existing
  breakpoint rather than a new one.

## Deviations from the canvas / from WORKBENCH_FACELIFT_PLAN.md

1. **Pause/Resume/Stop stay plain text**, not icon+text or icon-only, in
   `ss-playback-session` — `scripts/browser_regression.mjs` asserts
   `button.textContent.trim() === "Pause"|"Resume"|"Stop"` on real rendered
   buttons; an icon-only button (empty textContent) or relabeled text
   would fail that regression. The hero's visual weight comes from
   layout/type/gradient, not from changing these three words.
2. **WLED-segment condensation** — shipped in the post-R5 pass (was
   deferred from Stage 3+). See "Post-R5 visual polish" below.
3. **Dry-run icon** (a flask/beaker outline) is a first-pass choice, kept
   after rendering it in the real app — reasonable but not precious; a
   better metaphor is a fine follow-up if one turns up.
4. **Capability chips move to a permanent small icon row**, not a
   hover/selection-only reveal as `WORKBENCH_FACELIFT_PLAN.md` §4.4
   originally proposed — CSS-only hover reveal would make the capability
   information undiscoverable on touch/no-hover devices without extra
   interaction work; showing them small and quiet by default (via the icon
   vocabulary above, not text pills) achieves the same "quiet until needed"
   goal without a hover-only accessibility gap. Flagged as a deliberate
   call, not an oversight.
5. **Scene-row overflow menu** — shipped in the post-R5 pass (was deferred
   from Stage 3+). See "Post-R5 visual polish" below.

## Post-R5 visual polish (this pass)

R5 is complete and treated as an API boundary — nothing here touches
`playback.js` semantics, `session_id` lifecycle, `allowed_commands`,
fixture identity/binding, or discovery/backend contracts. Reviewed against
the live Workbench (`http://homeassistant.local:5050/local/scene_studio/index.html`)
in read-only mode, not just the mock client.

**WLED-segment condensation (`components/ss-fixture-cluster.js`)** — the
deferred Stage 3+ item. Clustering key is `fixture.binding.device_id`,
confirmed against the LIVE registry (not just the mock sample): every
`wled_seg_*` fixture shares one `device_id` (the controller's MAC, e.g.
`aabbccddeeff`), distinguished only by `binding.segment_ids`; Hue/HA
bindings carry no `device_id` at all (Hue's shared identifier is
`bridge_id`, which every light on the bridge shares — clustering on that
would wrongly lump together unrelated fixtures, so only `device_id`
triggers clustering, and only when 2+ fixtures share one). `views/fixtures.js`
groups by room as before, then clusters within each room group via
`withControllerClusters()`. The cluster's summary row is a NEW,
non-selectable presentational row (worst-of health dot, "Segment 0–5"
label derived from the members' real `segment_ids`, provider badge, the
first member's capability icons, an aggregate health readout); expanding
it (a native `<details>`) reveals the exact same `<ss-fixture-row>` per
segment used everywhere else — same fixture id, same health/capabilities,
same bubbling `select-fixture` event, no change to the fixture model.
Starts expanded automatically when segments' health differs (a mixed
cluster is itself a "look at this" signal); collapsed, the aggregate
health text still surfaces a uniform failure (e.g. "6 missing" in red) —
collapsing never hides a problem, it just stops repeating six identical
"ready" rows.

**Scene-row overflow menu (`components/ss-overflow-menu.js`)** — the
deferred Stage 3+ item. Built on the native Popover API
(`popover`/`popovertarget`) specifically because `<ss-panel
variant="list">` sets `overflow: hidden` on every row list — a normal
absolutely-positioned dropdown would get clipped; a top-layer popover is
immune to that (verified: the popover renders and measures a real,
non-zero size from inside the clipped list). `ss-scene-row.js` now splits
`#primaryItems()` (Apply, or Play/Playback-focus — the one high-frequency
action, unchanged icon/gating/event) from `#secondaryItems()` (Dry run,
Rename, Archive — same keys/gating/`scene-action` payloads, now with
visible text since a dropdown has room for it) and renders
`<ss-action-menu dense>` for the former plus `<ss-overflow-menu>` for the
latter; archived rows keep their single Restore action with no overflow
(nothing lower-frequency to hide there). Keyboard: Tab/Shift+Tab and
Enter/Space via native `<button>` semantics; ArrowUp/ArrowDown/Home/End
roam focus among enabled items (`role="menu"`/`"menuitem"`); Escape and
outside-click close it via native `popover="auto"` light-dismiss; opening
focuses the first enabled item, closing returns focus to the trigger —
all verified with real (trusted) key events, since a script-dispatched
`KeyboardEvent` does not trigger native popover Escape-to-close. The
trigger button stops click propagation so opening the menu never also
selects the row (a real bug caught during this pass, fixed before commit).
`scripts/browser_regression.mjs` gained one check per component (cluster
member selection, overflow menu open/route/no-side-effect/close) —
targeted, not a wholesale test-count increase.

**Scene-row narrow-width fix** — a real, pre-existing gap found while
reviewing 390px: the existing `<900px` row breakpoint budgeted a 160px
actions column for up to five text buttons; that column, plus the row's
other fixed-width columns, added up to ~510px minimum — wider than a real
phone viewport, so the actions column was clipped off-screen entirely by
`ss-panel`'s `overflow: hidden` below ~420px (confirmed by direct
`getBoundingClientRect` measurement, not just visual inspection). Icon
actions only need ~70px now (one primary icon + the overflow trigger), so
the breakpoint's column budget was corrected to match; verified the row
now fits with both actions visible down to 390px.

**Overview boot-flash fix (`app.js`)** — a real, user-reported bug, root
cause confirmed by reading the boot sequence: a persisted Live URL used to
connect AFTER the mock client's first paint (`store.init()` with mock data
rendered a full — and wrong — mock dashboard, THEN `setConnection("live",
...)` replaced it once the real fetch resolved), so every load of a
live-configured deployment (the normal case for the real AppDaemon-hosted
Workbench) briefly showed the mock scenes/fixtures/counts before the real
ones arrived. Fixed by skipping the mock client's first paint entirely
when a live URL is already persisted, connecting live from the very first
render instead — `state.status` stays `null` (a single, already-handled
"Loading…" state) until the real fetch resolves, never a mock-then-live
content swap. Mock mode (no persisted live prefs) is unchanged. Not yet
deployed to the live device — this pass only changes frontend source; the
AppDaemon addon runs whatever was last deployed there, unaffected by this
commit until an explicit, separately-approved deploy step.

**Investigated and ruled out**: several additional "distorted layout"
renders observed while testing in this session's Browser pane (cramped
sub-width layouts, content appearing far below a blank gap) were
confirmed — by direct `getBoundingClientRect`/`window.innerWidth`
measurement, every time — to be a screenshot/compositor-capture and
viewport-emulation staleness artifact of this session's browser-automation
tooling (stale frames and stale `innerWidth` briefly following a resize or
a same-page state mutation), not a defect in the Workbench: the actual DOM
layout measured correct at every check. Noted here so a future session
doesn't rediscover the same dead end — trust `getBoundingClientRect`/
`innerWidth` over a screenshot taken immediately after a resize or a
scripted state change in that tooling; a fresh tab or a ~1s wait resolves
it.
