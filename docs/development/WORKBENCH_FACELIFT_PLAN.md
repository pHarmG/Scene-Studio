# Scene Studio Workbench — Major UX/UI Facelift Plan

Status: **assessment + design plan (no implementation in this pass)**
Created: 2026-09-12
Scope: `packages/scene_studio_workbench` (standalone Lit SPA) only. No backend,
domain, API, or live HA/AppDaemon behavior is touched or proposed to change.
Written in parallel with the R5A backend implementation
([R5_DYNAMIC_PLAYBACK_PLAN.md](./R5_DYNAMIC_PLAYBACK_PLAN.md)) — this plan is
designed to *not* collide with that work and to give the R5 multi-session
playback model a real home in the UI (§8).

North star: opening the Workbench should feel like stepping into an elegant
cockpit you inherently understand — not a page of admin widgets. Every screen
answers, in order: **what is the system doing, is anything wrong, what can I
do next.**

---

## 0. Method

This plan is based on running the real Workbench (`npm run dev` under
`packages/scene_studio_workbench`) and exercising it live in a browser, not
on reading the source in isolation:

- Rendered all five views (Overview, Fixtures, Scenes, Discovery,
  Diagnostics) against the mock client, cycling through all 8 mock
  scenarios (`all-healthy`, `missing-fixture`, `disabled-fixture`,
  `provider-offline`, `replacement-candidate`, `mixed-fidelity`,
  `dynamic-active`, `migration-warning`).
- Checked the inspector for fixture / scene / discovery-entry / event
  selections, including a live-playing scene and a provider-offline failure
  state.
- Checked the header's Live-URL / transport (`direct` vs `appdaemon`) /
  endpoint controls by switching the connection mode live in the running app.
- Rendered at 1440×900, 1920×1080, and a 700×900 narrow layout (the
  `max-width: 900px` breakpoint) to see the responsive drawer behavior and
  header overflow.
- Built and rendered (in-browser, iterated once against a screenshot) a
  throwaway static HTML comp of the proposed Overview + Scenes direction to
  pressure-test the hierarchy and swatch/row treatment described in §5
  before committing them to this plan. The comp was **not** committed and
  did not touch any production file (`mockup-facelift.html`, created and
  deleted in the workbench package root during this session).

No production file under `packages/scene_studio_workbench/src/` was
modified. `docs/scene_studio/WORKBENCH_FACELIFT_PLAN.md` (this file) is the
only durable output of this pass.

---

## 1. Rendered critique of the current Workbench

### 1.1 Shell / header

At 1440×900 in mock mode, the entire header is: a 14px wordmark, a `mock`
tag, and — pushed flush right — a connection-mode `<select>`, a status dot,
and (in mock) a scenario `<select>`. Switching to **Live URL** immediately
promotes a second `<select>` (transport: "Dev server direct" / "AppDaemon"),
a conditional endpoint-name `<input>`, and a base-URL `<input>` into the same
row — verified live in this session (`app.js:390-462`). This is the exact
weakness called out in the brief: **transport plumbing is first-class product
chrome**, sitting at the same visual weight as the product name, on every
screen, always rendered, whether or not the operator ever touches it.

At 700px wide, the header does not reflow — the transport controls run off
the right edge of the viewport with no scrolling affordance (checked in the
Live+AppDaemon state). The inspector drawer becomes a fixed-position overlay
per the `@media (max-width: 900px)` rule (`app.js:221-235`), but the header
itself has no matching narrow treatment.

### 1.2 Overview

Rendered at 1440×900 in `all-healthy`: three equal-weight boxes (`Engine`,
`Fixtures & playback`, `Needs attention`) stacked in a `max-width: 1180px`
column, followed by roughly 700px of empty dark canvas. All three sections
share identical surface color, border, radius, and type scale — there is no
visual signal for "this is the important one." The brief's three cockpit
questions map onto this screen unevenly:

- *"What is it doing now?"* — answered by a single inline text fragment,
  `"Aurora Flow · dynamic · playing"` (verified in the `dynamic-active`
  scenario), styled identically to the idle string `"playback idle"`. **The
  single most exciting fact the product can report — lights are animating
  right now — gets zero visual distinction.**
- *"Is anything wrong?"* — the `Needs attention` list is the best-designed
  part of the screen (exception-driven, click-through to the right view/
  selection) but is visually a peer of the two healthy-state boxes above it,
  not a dominant element.
- *"What can I do next?"* — not answered at all on this screen; every action
  lives one tab away.

A genuinely bad state exposes a hierarchy bug, not just a density one:
rendered in `provider-offline`, the `Engine` line still reads **"healthy"**
in green, one pixel away from `WLED 0/6` in red — because `engine.ok` and
provider connectivity are unrelated booleans, both surfaced at equal
top-level weight. An operator scanning quickly sees green first.

### 1.3 Scenes

Rendered row (`ss-scene-row.js`): status dot, name, target list, up to six
13×13px palette swatches, a `STA`/`DYN` text chip, a ready-count fragment,
a fidelity abbreviation string (`"7 nat · 4 approx"`), and — for an
unarchived scene — **up to six buttons**: `Apply`, `Play`/`Pause`+`Stop`,
`Dry`, `Ren`, `Arch` (verified in `mixed-fidelity`: Aurora Flow's row shows
`Apply Play Dry Ren Arch`, five buttons, simultaneously, permanently). Every
row, healthy or not, pays this full cost. `Ren` and `Arch` in particular are
exactly the cryptic-abbreviation problem the brief names directly.

Archived scenes sort to the bottom of the *same* flat list (`scenes.js:142-
145`) rather than being visually or structurally separated, so a 3-scene
catalog already mixes "things you do today" with "things you're keeping for
reference" in one undifferentiated list.

### 1.4 Fixtures

Rendered in `all-healthy`: 25 rows, every one showing all four capability
chips (`RGB`, `TEMP`, `GRADn`, `DYN`) with only a border-color difference
between "supported" and "not supported" — confirmed by screenshot: the
25-row list is a wall of near-identical gray pills that carries no
operational signal at all when everything is healthy (which is the common
case). The view's own legend line (`"RGB color · TEMP color temp · GRADn
gradient points · DYN provider-native dynamic"`) exists only because the
abbreviations aren't self-explanatory — a strong signal the row itself is
the problem, not the missing legend.

### 1.5 Discovery

Structurally this is the best-aimed view already (`discovery.js`): a summary
bar, an exception-only list (bound-and-unchanged entries are filtered out by
`#exceptions()`), and status-appropriate actions per entry. It still reads as
a data table, though — a flat list with a status-text column
(`available_unbound`, `candidate_replacement`, …) rather than a triage
workflow with distinct decision groups. One data-consistency point worth
flagging for later verification (not a UI defect to fix here): rendered in
`all-healthy`, the Discovery report still listed six `missing` WLED segments
and two unrecognized fixture names (`Water Light`, `Food Light`) not present
in that scenario's Fixtures list or Overview counts — discovery data is
scenario-independent in the mock by design (`mocks/scenarios.js`), but the
resulting cross-view inconsistency during this audit is itself an argument
for the "System" surface being clearly separated from "operational truth"
surfaces (§3, §4.6).

### 1.6 Diagnostics

A plain reverse-chronological event feed with an inline JSON disclosure per
event and an export button. Functionally fine, and appropriately plain — the
issue is purely architectural: it sits as a fifth co-equal tab, at the same
navigational weight as Scenes, for a feature an operator needs rarely.

### 1.7 Inspector

One fixed template — status glyph, `<dl>` facts grid, explanation
paragraphs, action buttons, collapsed technical JSON — applied uniformly to
fixtures, scenes, discovery entries, and events (`inspector.js`,
`ss-inspector.js`). The Level-1/2/3 information layering (facts vs.
explanation vs. raw JSON) is a good idea and should survive the facelift,
but "one card shape for every kind of thing" is why a scene's inspector and
a discovery entry's inspector look like clerical printouts of each other
when their underlying decisions (start something vs. resolve a binding
conflict) are completely different in shape.

### 1.8 Space, width, visual system

- `--ss-content-max: 1180px` (`tokens.css:49`) is conservative on a
  1920×1080 display — confirmed by screenshot: roughly 740px of dead margin
  on each side of centered content at 1920 width, more than the content
  column itself.
- The dark palette (`tokens.css`) is a reasonable base (matches the AGENTS.md
  styling mandate's fixed status-color semantics: green/yellow/red/gray for
  healthy/transitional/error/idle — **this mapping is correct today and must
  not change**), but every surface uses the same `--ss-surface` /
  `--ss-border-soft` pairing, so nothing in the visual system currently
  creates hierarchy — hierarchy is attempted entirely through spacing and
  copy, never through color, elevation, size, or motion.
- Zero motion anywhere. A live-playing dynamic scene — the one moment this
  is a *lighting* product and not a settings page — looks pixel-identical to
  every static, idle screen.

---

## 2. Proposed information architecture

**Primary, persistent nav (3 items — daily operation + catalog):**

```
Overview · Scenes · Fixtures
```

These are the surfaces used every session. They stay equal-weight tabs, but
Overview stops being "one of five" and becomes the load-bearing first
screen (§5.1).

**Secondary, header-anchored entry points (not tabs):**

- **Inbox** (Discovery) — a bell/inbox icon in the header with a badge count
  of open exceptions (candidates + missing + new-unbound). Zero badge → the
  icon is present but quiet. Clicking opens the Discovery workflow full-view
  (it's still a real view/route, just not competing for tab real estate with
  Scenes and Fixtures — it's *reactive* work, not routine navigation).
- **System** — a gear icon opening a drawer that bundles everything that is
  an engineering/admin concern today: connection mode (Mock/Live), transport
  (direct/AppDaemon) + endpoint name, Live URL, the mock scenario picker
  (dev-only — never present against a live backend), runtime policy/mode
  display (`read_only`, `allowed_commands`), Diagnostics (the full event
  feed + sanitized export), and API/version info. This is the direct answer
  to "connection transport, endpoint names, mock scenarios, raw IDs,
  provider capabilities... should not dominate primary chrome."

**Header status pill** — a single synthesized status readout
(`"All systems normal"` / `"3 issues"`, tone-colored) replaces reading three
different boxes to answer "is anything wrong." It is a *derived* summary
(worst tone across fixtures/providers/warnings/orphaned sessions), not a
raw passthrough of `engine.ok` — this directly fixes the §1.2
"healthy-while-a-provider-is-offline" bug by never showing green unless
every underlying signal actually is green.

This restructure is purely a navigation/placement change. No command, no
store method, and no backend contract changes — see §7 for the exact
mapping of every existing control to its new home.

---

## 3. Visual / design-system direction

Keep the dark, professional base — it's the right register for "mature
desktop control software" — and add the missing hierarchy tools: scale,
color-as-information, restrained motion, and one dominant object per screen.

**Status semantics (unchanged, load-bearing constraint):** green = healthy/
ready/ok, yellow = transitional/degraded/warning, red = failed/missing/
error, gray = idle/disabled/inactive, per the AGENTS.md styling mandate
(`AGENTS.md:143`) and `tokens.css`'s existing `--ss-ok/--ss-warn/--ss-err/
--ss-idle`. Every new component reuses these four tokens; no new status
color is introduced.

**Typography scale** — replace the current "13px everywhere, 10-11px for
labels" flat scale with a real scale: `11 / 12 / 13 / 15 / 20 / 28`. The two
new large sizes (20/28) are reserved for exactly one thing per screen: the
Overview hero headline and nothing else — scarcity is the point.

**Color as information, not decoration** — the one deliberately "premium
lighting app" move: a scene's own palette becomes a real visual object (a
gradient swatch band, §5.3) instead of a row of tiny squares, and the
Overview hero for an active session washes its background with a faint
(≤16% opacity) gradient derived from that scene's palette. This is the
"visual drama" the brief invites, kept strictly inside status-neutral
territory — it never touches the green/yellow/red/gray semantic tokens, so
it can't be confused with health signal.

**Radius / elevation** — slightly larger radius on primary containers
(10-14px vs. today's flat 6px) to read as "panels," not "table rows"; rows
inside a list stay flat/dense. No blur-glassmorphism; elevation comes from a
single subtle gradient wash + border, never a blur filter.

**Iconography** — a small inline-SVG icon set (16-20px stroke icons, no icon
font/external dependency, consistent with the package's zero-dependency
posture) replaces every cryptic text abbreviation: `Ren`→pencil, `Arch`→
archive-box, `STA`/`DYN`→steady-dot/waveform glyph, `RGB`/`TEMP`/`GRAD`/
`DYN` capability chips → small glyphs shown on hover/selection, not
permanently (§5.4). Icons always carry a `title`/`aria-label` — nothing
becomes *less* accessible than the current text labels.

**Motion** — restrained, purposeful, `prefers-reduced-motion`-aware:
120-160ms ease on hover/selection state changes (already implicitly present
via CSS, made consistent); a slow (\~3s) ambient pulse on the "live" tag /
session chip for actively-playing sessions — the only continuous animation
in the whole product, reserved for the one thing that's actually moving in
the real world.

**Layout width** — raise `--ss-content-max` from 1180px to roughly 1440px
on desktop, and let it grow further (\~1600-1680px) on displays ≥1920px
wide by using a `clamp()`-based max-width tied to viewport rather than a
fixed constant — validated visually against 1920×1080: this removes most of
the ~740px dead margin without pretending a control cockpit needs full
edge-to-edge 1920px content.

---

## 4. Screen-by-screen redesign intent

### 4.1 Shell

- Header: wordmark, primary nav (Overview/Scenes/Fixtures), status pill,
  Inbox icon+badge, System icon. Mock-mode `mock` tag stays (small, muted) —
  it's operationally important to always know you're not touching real
  lights, so it's the one piece of "engineering" signal that stays in
  primary chrome, just demoted to a quiet tag rather than a live control.
- System drawer: a right-side slide-over (reuses the same drawer mechanism
  as the narrow-layout inspector — see §6) with sections: **Connection**
  (mode/transport/endpoint/URL — today's header controls, verbatim
  behavior, relocated), **Scenario** (mock-only, hidden entirely outside
  mock mode), **Runtime policy** (read-only display of `runtime.mode` /
  `read_only` / `allowed_commands` — new, small, informational), **Recent
  events** (the current Diagnostics feed), **Export** (sanitized snapshot
  download).
- Narrow layout (≤900px): header collapses the primary nav into a compact
  icon/label row that wraps instead of overflowing; System/Inbox icons
  never get clipped off-screen (fixes the confirmed 700px overflow bug).

### 4.2 Overview — the cockpit

One dominant hero, one secondary row of two panels, then the scene/fixture
lists below the fold (not more boxes of the same weight):

1. **Hero** — "what's happening now." If any session is playing/paused:
   headline `"<Scene> is animating"` / `"<Scene> paused"` in the room it's
   targeting, palette-tinted background wash, elapsed time + fidelity
   summary, inline Pause/Resume/Stop for that session. Zero or one session:
   exactly this. Multiple sessions (R5, §8): the hero shows the most
   recently started/most attention-needing session expanded, the rest as
   compact chips beneath it — never a second hero-weight block. Fully idle:
   a calm centered state — `"Nothing playing"` + one-click shortcuts to
   apply the 2-3 most recently used scenes.
2. **Needs attention** + **Fleet** side-by-side panels below the hero —
   exceptions keep today's good click-through behavior, gain a colored left
   edge (red/yellow) so severity is scannable without reading, and sort
   errors before warnings. Fleet condenses the provider/fixture-count line
   from today's two separate Overview sections into one compact panel.
3. The synthesized header status pill (§2) is the *only* place "healthy" is
   asserted as a single word — and it is computed as `worst(engine.ok,
   provider connectivity, fixture counts, warnings, orphaned sessions)`,
   which structurally fixes the "healthy while WLED is offline" bug found
   in §1.2 (that scenario would now show "3 issues" in amber/red, never a
   green "healthy").

### 4.3 Scenes

- Row becomes a compact **identity row**: a 3px left accent bar (readiness
  tone), a single gradient swatch-band (the scene's whole palette as one
  chip, not up to six separate squares), name + room stacked, a mode glyph
  (steady dot = static, small waveform = dynamic) replacing `STA`/`DYN`
  text, a compact ready/fidelity readout, and **one primary icon action**
  (Apply, or Pause when that scene owns an active session) plus a `⋯`
  overflow menu (Dry run, Rename, Archive). Secondary icons (e.g. Stop, when
  playing) appear alongside the primary on hover/focus — never fewer
  affordances than today, just not all six visible permanently.
- A scene with a live session gets a small `LIVE` tag next to its name
  (pulse animation, §3) — this is the one place in the product where "this
  is actually happening in the real world right now" gets a distinct visual
  treatment.
- Archived scenes move out of the primary flat list into a collapsed
  **"Archived (n)"** disclosure at the bottom of the list — restoring a
  scene is still one click after expanding, but the daily list is no longer
  padded with reference material.
- Hover/hit-target and keyboard-focus reveal actions identically (no
  hover-only affordance — see §9 accessibility note).

### 4.4 Fixtures

- Default row: status glyph + name, group/room, provider badge, health text
  — capability chips (`RGB`/`TEMP`/`GRAD`/`DYN`) move to hover/selection
  reveal and to the inspector's facts list, where they already duplicate
  information (`inspector.js`'s `capabilityChips`). A healthy 25-row list
  becomes a fast, quiet scan instead of a wall of gray pills.
- Optional grouping (by room, collapsible) once fixture counts grow past
  what a flat list scans well — the same `<ss-row>` primitive (§6) supports
  a group header variant, so this can land later without a rewrite.

### 4.5 Discovery — inbox, not a table

- Header becomes a slim summary strip; the "Run discovery" action moves to
  a quiet ghost button (top-right, secondary weight) — it's maintenance, not
  the primary decision surface.
- Entries group into labeled sections mirroring the actual decision types:
  **Candidates to review**, **Missing to resolve**, **New/unbound
  resources** — each collapsible, badge-counted, worst-first. This is
  simply a `<ss-row>`-based regrouping of the exact same `#exceptions()`
  list (`discovery.js:156-163`) by `entry.status`; no new data shape.
- A `candidate_replacement` entry's inspector (§4.7) becomes a genuine
  compare view (current binding vs. candidate) instead of a facts list,
  since it's fundamentally an A/B decision.

### 4.6 Diagnostics

- Lives inside the System drawer (§4.1) as the default surface when the
  drawer opens with no other section selected — de-emphasized, monospace-
  leaning, "below deck" in tone (slightly darker surface, smaller type)
  while remaining fully functional and one click away, never buried behind
  more than the single System affordance.

### 4.7 Inspector — contextual, not templated

Keep the Level-1/Level-2/Level-3 (facts / explanation / raw JSON) layering —
it's a good, worth-preserving pattern — but vary the *template* by
`descriptor.kind`:

- **scene**: leads with the swatch-band + primary actions (Apply/Play) at
  the top, facts below — mirrors the Scenes row's visual language so
  selecting a row doesn't feel like switching applications.
- **fixture**: leads with capability glyphs (this is the one place they
  belong front-and-center) + binding/provider facts.
- **entry** (discovery): the compare-card described in §4.5 — current vs.
  candidate side by side — instead of a flat `<dl>`.
- **event**: unchanged in substance (already close to right), light restyle
  to match the new type scale.
- **session** (new, R5 — §8): playback-specific facts (scene, targets,
  started/paused timestamps, per-fixture execution/fidelity list) +
  session-addressed Pause/Resume/Stop.

`buildInspectorDescriptor` keeps returning one descriptor shape
(`{kind, title, subtitle, tone, status, facts, explanation, actions,
technical}`); `<ss-inspector>` grows a per-`kind` body template instead of
one fixed layout. No change to how descriptors are built from state beyond
adding the new `session` kind.

---

## 5. Visual reference: the validated prototype

To pressure-test §3-4 before writing them up, this session built and
rendered (via the running dev server, at 1440×900, iterated once) a static,
untracked HTML comp covering the Overview hero + exceptions/fleet panels +
new Scenes rows. It was deleted after review; the concrete decisions it
validated and that this plan carries forward:

- The hero's palette-tinted gradient wash reads as "premium lighting app"
  without at any point competing with or muddying the red/yellow/green
  status tokens — confirmed by rendering it stacked directly above a
  status-colored exceptions panel and checking for hue confusion (none).
- A single blended gradient swatch-band per scene row is legible at 30×20px
  and scans faster than six discrete 13px squares at the same information
  density.
- Session chips (`Aurora Flow · studio · LIVE`, `Meeting Blue · office ·
  PAUSED`) at 1440px width comfortably support 2 simultaneous sessions
  inline with room to wrap to a second line for a 3rd+ — validating that
  the hero can scale from 0 to N sessions (§8) without a structural
  redesign.
- Left-edge severity bars on the exceptions panel (red for missing/
  offline, amber for degraded/candidate) make error-vs-warning scannable
  without reading the row text, confirmed by squinting at a downscaled
  screenshot (a cheap but effective "does hierarchy survive at a glance"
  check).

---

## 6. Reusable component / primitive strategy (Lit)

Today, `scenes.js`, `fixtures.js`, and `discovery.js` each hand-roll their
own `.list` / row-grid / `.actions button` CSS that is 80-90% identical
(compare `fixtures.js:11-31`, `scenes.js:45-64`, `discovery.js:15-123`) —
this is exactly the kind of one-off-CSS-per-view the brief asks to move away
from. Proposed primitives (new files under `src/components/`), each a small
Lit element with no view-specific logic:

| Primitive | Replaces | Used by |
| --- | --- | --- |
| `<ss-icon>` | ad hoc inline SVGs per file | everywhere |
| `<ss-panel>` | every view's repeated `section{...}`/`.list{...}` card CSS | Overview, Scenes, Fixtures, Discovery |
| `<ss-row>` | `ss-scene-row`, `ss-fixture-row`, and discovery's `.entry` divs (three parallel implementations of "clickable row with status glyph + slots") | Scenes, Fixtures, Discovery |
| `<ss-swatch-band>` | the `.swatches`/`.swatch` loop in `ss-scene-row.js` | Scenes rows, scene inspector, Overview hero/chips |
| `<ss-action-menu>` | the always-visible button row in `ss-scene-row.js` | Scenes rows (overflow), Fixtures rows (future), Discovery entries |
| `<ss-drawer>` | the bespoke `position: fixed` inspector CSS in `app.js:227-234` | narrow-layout inspector (existing use, generalized) + new System drawer |
| `<ss-status-glyph>` | (already exists — kept as-is, extended with an optional icon variant) | everywhere |
| `<ss-empty-state>` | the ad hoc `.muted`/`.empty` divs repeated in every view | Overview idle hero, empty lists, no-discovery-report state |

`ss-scene-row`, `ss-fixture-row`, and the Discovery entry markup become thin
composers of `<ss-row>` + `<ss-swatch-band>` + `<ss-action-menu>` rather than
each owning a full grid layout — this is a genuine LOC reduction, not just a
rename, and it's what makes "contextual inspector" (§4.7) and "grouping in
Fixtures" (§4.4) cheap to add later instead of another parallel
implementation.

---

## 7. Interaction-contract mapping

Every redesigned control maps to the **exact same** store method / command
as today. This table is the acceptance checklist for "meaning of an
operation did not change."

| # | Operation | Today's trigger | Redesigned trigger | Store call (unchanged) |
| --- | --- | --- | --- | --- |
| 1 | Apply scene | Row `Apply` button | Row primary icon (▶) | `sendCommand({command:"scene.apply", scene_id})` |
| 2 | Dry-run preview | Row `Dry` button | Row overflow menu → "Preview render plan" | `sendCommand({command:"scene.preview", scene_id})` |
| 3 | Start dynamic playback | Row `Play` button | Row primary icon when `motion.mode != static` and no active session for this scene | `sendCommand({command:"playback.start", scene_id})` (see §8 for `session_id` in the result) |
| 4 | Pause playback | Row `Pause` button | Row primary icon (becomes ⏸ once the scene owns a session) / session chip pause | `sendCommand({command:"playback.pause"[, session_id]})` — session-addressed once R5C ships, see §8 |
| 5 | Stop playback | Row `Stop` button | Row secondary icon / session chip stop | `sendCommand({command:"playback.stop"[, session_id]})` |
| 6 | Resume playback (**new, R5**) | *not exposed today* | Session chip "Resume" when `state:"paused"` | `sendCommand({command:"playback.resume", session_id})` |
| 7 | Rename scene | Row `Ren` → inline input | Overflow menu → "Rename" → same inline input | `sendCommand({command:"scene.rename", scene_id, name})` |
| 8 | Archive scene | Row `Arch` button | Overflow menu → "Archive" | `sendCommand({command:"scene.archive", scene_id})` |
| 9 | Restore scene | Row `Restore` button (mixed into main list) | "Restore" inside the Archived disclosure row | `sendCommand({command:"scene.restore", scene_id})` |
| 10 | Enable/disable fixture | Inspector action | Inspector action (unchanged; still the only surface — no row-level quick action added) | `sendCommand({command:"fixture.enable"|"fixture.disable", fixture_id})` |
| 11 | Retry fixture | Inspector / Discovery entry action | Inspector / Discovery compare-card action (unchanged) | `sendCommand({command:"fixture.retry", fixture_id})` |
| 12 | Rebind fixture | Discovery entry `Rebind` / inspector action | Discovery compare-card primary action / inspector action | `sendCommand({command:"fixture.rebind", fixture_id, observation_id})` |
| 13 | Leave unbound | Discovery entry `Leave unbound` | Compare-card secondary action | `store.dismissDiscoveryKey(key)` (session-local view state, never a command — unchanged) |
| 14 | Run discovery | Discovery view primary button | Discovery view ghost button (demoted weight, same position) | `sendCommand({command:"discovery.run"[, providers]})` |
| 15 | Export diagnostics | Diagnostics view button | System drawer → Diagnostics section button | `sendCommand({command:"diagnostics.export", redact:true, recent_events:200})` |
| 16 | Switch connection mode/transport/URL | Header selects/inputs | System drawer → Connection section | `store.setConnection(...)` / `setDefaultHttpSceneStudioClientOptions(...)` — client construction only, never a backend command |
| 17 | Switch mock scenario | Header select (mock-only) | System drawer → Scenario section (mock-only) | `store.setScenario(id)` |
| 18 | Navigate views | Tab bar (5 equal tabs) | Primary nav (3 tabs) + Inbox icon + System icon | `store.setView(id)` |
| 19 | Select a row/entry/event | Click row | Click row (unchanged) | `store.select({type, id})` |
| 20 | Close inspector | `✕` button | `✕` button (unchanged) | `store.select(null)` |

Backend-owned policy gating (`runtime.allowed_commands`, `commandAllowed()`
in `state.js:235-239`, the `?disabled=` + `title="Unavailable in the current
backend mode"` pattern in `ss-scene-row.js`/`ss-inspector.js`) is preserved
verbatim: every redesigned icon/menu action keeps checking
`allowedCommands` before rendering as enabled, and the disabled state keeps
the same tooltip contract. No new action is ever rendered as usable if the
backend has not declared it in `runtime.allowed_commands`.

---

## 8. Where R5 multi-session playback lands

R5C ([R5_DYNAMIC_PLAYBACK_PLAN.md §5](./R5_DYNAMIC_PLAYBACK_PLAN.md)) will
change `status().playback` from a nullable singular object into a
collection: `{sessions: [...], counts: {...}, owned_fixture_count}`. This
plan is deliberately built around that shape rather than the current
singular one:

- The **hero's session-chip row** (§4.2, §5) *is* the collection renderer:
  0 sessions → idle empty state, 1 → hero expanded + no chip row, N → hero
  shows the top session expanded and the rest as chips, each independently
  clickable into an inspector `session` descriptor (§4.7).
- **Session addressing** is designed into the interaction-contract table
  (§7, rows 4-6) from the start — pause/resume/stop always carry a
  `session_id` once it exists in `data`/`status`, so the UI layer does not
  need a second redesign when R5C lands; it needs its data source switched
  from `status.current`/`status.playback` (singular) to
  `status.playback.sessions` (collection).
- **Preemption and orphaning** get first-class exception treatment: an
  `orphaned` session (post-restart, still animating on the bridge per
  §1.4 of the R5 plan) surfaces as a red `Needs attention` entry — "Aurora
  Flow may still be animating on the bridge — stop or reclaim" — linking
  into the session's inspector, never silently dropped from view.
- **Until R5C ships**, the hero/exception code path checks for
  `status.playback.sessions` and falls back to treating today's singular
  `status.playback`/`status.current` as a one-session collection — so the
  Overview/Scenes facelift (Stages 2-3, §9) can land *before* R5C without
  rework, and R5C's job becomes "swap the data source," not "redesign the
  screen."
- Scene rows already resolve "is *this* scene live" from `status.playback`
  today (`scenes.js:139-141`); under the collection shape this becomes "is
  there a session in `sessions` with `scene_id == this.id`," a small,
  contained change localized to the same spot.

---

## 9. Staged implementation strategy

Each stage is a frontend-only diff against `packages/scene_studio_workbench`
with no backend/API/domain dependency, sequenced so risk is isolated and
each stage is independently shippable/revertable. Stages 1-4 can start
immediately and do not depend on R5A/B/C landing first; Stage 2 is written
to tolerate both the pre-R5 and post-R5C playback shape (§8).

1. **Primitives, no visual change.** Land `<ss-icon>`, `<ss-panel>`,
   `<ss-row>`, `<ss-swatch-band>`, `<ss-action-menu>`, `<ss-drawer>`,
   `<ss-empty-state>` (§6) and re-point the *existing* views at them with
   equivalent visual output. Pure refactor — verify against `npm run smoke`
   and a manual pass through all 8 mock scenarios with no rendering
   difference. This de-risks every later stage.
2. **Shell rework.** New header (nav + status pill + Inbox/System icons),
   System drawer housing Connection/Scenario/Diagnostics/Export (§4.1, §4.6).
   Purely relocates existing controls onto `<ss-drawer>`; every `store.*`
   call they make is unchanged (§7 rows 15-18). Update `scripts/smoke.mjs`
   if it asserts on header DOM structure.
3. **Overview cockpit.** Hero + Needs-attention/Fleet panels (§4.2), built
   against the session-collection-or-fallback data source described in §8.
4. **Scenes redesign.** Icon actions + overflow menu + swatch-band + Archived
   disclosure (§4.3), checked row-by-row against the §7 table.
5. **Fixtures + Discovery.** Hover-reveal capability glyphs + optional
   grouping (§4.4); Discovery inbox regrouping + compare-card entries
   (§4.5).
6. **Contextual inspector.** Per-`kind` templates (§4.7), including the new
   `session` kind (wired to real data once R5C lands; buildable/testable
   against a hand-built fixture in the meantime).
7. **Polish / stretch.** Command palette (Cmd/Ctrl+K → jump to scene, run
   discovery, open diagnostics), fixture grouping by room, `scene.save`
   surfaced in the UI (currently a client/command capability with no view
   wired to it — a real gap, out of scope to fix here but worth flagging).

Recommended coordination with the parallel R5 backend work: Stages 1-2 are
safe to do concurrently with R5A (no shared files). Stage 3's session-chip
rendering is the natural place to pick up R5C's status shape the moment it
lands — land Stage 3 behind the fallback described in §8 first, then it's a
small follow-up diff, not a second design pass.

---

## 10. What's removed/demoted vs. what's added

**Removed or demoted:**

- Header Live-URL/transport/endpoint controls → System drawer (§4.1).
- Header mock-scenario picker → System drawer, still mock-only (§4.1).
- Diagnostics as an equal-weight primary tab → folded into System (§4.6).
- Discovery as an equal-weight primary tab → Inbox icon + badge (§2).
- Permanent `RGB`/`TEMP`/`GRAD`/`DYN` chips on every fixture row → hover/
  selection reveal + inspector (§4.4).
- `Ren`/`Arch`/`STA`/`DYN` text abbreviations → icons with tooltips (§3).
- Up to six always-visible row buttons on every scene → one primary icon +
  overflow menu (§4.3, §7).
- Archived scenes mixed permanently into the active list → collapsed
  disclosure (§4.3).
- Three equal-weight Overview boxes → one dominant hero + two secondary
  panels (§4.2).
- Fixed 1180px content cap → adaptive, wider on large displays (§3).

**Added:**

- Synthesized header status pill, computed as the worst of every underlying
  health signal (§2, fixes the "healthy while a provider is offline" bug).
- Overview hero with palette-tinted current-activity display, scaling from
  idle → one session → many sessions (§4.2, §8).
- `LIVE` tag + ambient pulse on actively-playing scenes (§3, §4.3) — the
  product's one deliberate piece of motion.
- Gradient swatch-band as a scene's compact visual identity (§4.3, §5).
- Grouped/triaged Discovery workflow (§4.5) and a compare-card entry
  inspector (§4.7).
- Contextual, per-kind inspector templates, including a new `session` kind
  for R5 (§4.7, §8).
- Shared Lit primitive library (§6) so future views/rows are composition,
  not new CSS.
- (Stretch, Stage 7) command palette; `scene.save` UI surface.

---

## 11. Summary

The current Workbench is functionally solid and already gets several things
right (exception-first Discovery filtering, backend-owned policy gating
rendered faithfully, a real Level-1/2/3 information-layering idea in the
inspector) — the facelift is not a rewrite of its logic, it's a rebalancing
of visual weight toward what an operator actually needs to see first, a
removal of engineering plumbing from primary chrome, and a small, reusable
component vocabulary so the next feature (R5 sessions, fixture grouping,
whatever comes after) is cheap to add correctly. Every control's underlying
command is pinned down in §7 before any pixel changes, so the facelift can
proceed stage-by-stage (§9) without ever being a "trust me" rewrite.
