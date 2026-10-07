/**
 * <ss-scene-row> — one compact scene row (master plan §10.6).
 *
 * Shows: name, scope pill (same <ss-scope-control> as Fixtures), look
 * swatches (authored palette, or unique applied fixture colors when the
 * stored palette collapses to a single hue), exception notes (missing/degraded/disabled — healthy rows are
 * a status dot only), and row actions (apply / dry run / play /
 * edit-in-Builder / archive-restore / rename). Fidelity bucket counts
 * (nat/eq/approx) stay off the row; they belong to Preview / the
 * Fidelity refresh, not the catalog list. Emits bubbling
 * `scene-action` CustomEvents with `{action, scene_id, name?}`; never
 * mutates state itself.
 *
 * @prop {object} scene Scene v2 JSON
 * @prop {{counts:{total,ready,missing,disabled,degraded},
 *         fidelity:{native,equivalent,approximate,unsupported},
 *         dynamic:boolean, mode:string,
 *         dynamicEligible:{capable:number,total:number}}} summary assembled
 *   from fixture readiness + golden render-plan fidelity buckets.
 *   `dynamicEligible` counts how many of the scene's resolved fixtures
 *   report `capabilities.dynamic_native`, used to gate the "Upgrade to
 *   dynamic" overflow action on static rows.
 * @prop {boolean} archived
 * @prop {boolean} active true when this scene is current/playing
 * @prop {object|null} sessionSummary compact plural runtime summary for
 *   this scene's live playback sessions (R5C plan §7), resolved by the
 *   parent view via playback.js `sessionsForScene` + summaries:
 *   `{count, states, issueCount, legacy}` or null when the scene has no
 *   live sessions. A scene may run as SEVERAL disjoint sessions (the
 *   backend's ownership invariant is per fixture, not per scene), so rows
 *   never receive "the one" session and never route lifecycle controls by
 *   implicitly picking one — dynamic playback actions are Play (zero live
 *   sessions) or an unambiguous "Playback…"/"N sessions" focus affordance
 *   pointing at the Live playback panel, the canonical control surface.
 * @prop {boolean} selected
 */
import { LitElement, html, css } from "lit";
import "./ss-status-glyph.js";
import {
  iconAlertTriangle,
  iconApply,
  iconArchive,
  iconClock,
  iconDryRun,
  iconDuplicate,
  iconPencil,
  iconPlay,
  iconRestore,
  iconSparkle,
  iconWaveform,
} from "./icons.js";
import "./ss-swatch-band.js";
import { bandGradient } from "./ss-swatch-band.js";
import "./ss-scope-control.js";
import "./ss-action-menu.js";
import "./ss-overflow-menu.js";
import "./ss-routine-popover.js";
import { sceneLookSwatches } from "../scene_look.js";

export class SsSceneRow extends LitElement {
  static properties = {
    scene: { attribute: false },
    summary: { attribute: false },
    archived: { type: Boolean },
    active: { type: Boolean },
    sessionSummary: { attribute: false },
    selected: { type: Boolean },
    allowedCommands: { type: Array },
    // External light-sync (hyperHDR) contention is active somewhere in the
    // registry — surfaces the one-shot "Take over & apply" affordance.
    contentionActive: { type: Boolean },
    // HA-native routine awareness (routines pass): this scene's routine
    // projections (null = not loaded) + the /routines capability doc.
    // Rendered through <ss-routine-popover> in the name cell; the row only
    // forwards the bubbling `routine-action` events to the view.
    routines: { attribute: false },
    routinesAvailable: { type: Boolean },
    routinesReason: { type: String },
  };

  static styles = css`
    :host {
      display: block;
      container-type: inline-size;
      container-name: ss-scene-row;
    }
    /* Independent per-row grids (hosts cannot subgrid through shadow), so
       tracks size the SAME way for every sibling (fr resolves off the
       shared container width). Name and swatch share leftover width —
       they are the row's identity. Scope uses the same compact pill
       track as Fixtures (--ss-scope-col); status notes collapse to
       content; actions stay a fixed-width token (--ss-actions-col: 108px
       desktop / 136px touch, sized to its row-btn contents) so
       apply+play+overflow never wrap onto a second line. Compact collapse
       is a container query on the row, not the viewport. */
    .row {
      position: relative;
      display: grid;
      grid-template-columns:
        minmax(0, 1.5fr)
        var(--ss-scope-col)
        minmax(5.5rem, 1.2fr)
        auto
        var(--ss-actions-col);
      gap: var(--ss-gutter);
      align-items: center;
      min-height: var(--ss-row-h);
      padding: 6px 10px;
      border-bottom: 1px solid var(--ss-border-soft);
      cursor: pointer;
    }
    /* A faint cast of the scene's own palette on the row surface — z-index
       -1 keeps it behind every normal-flow child without touching each
       child's own stacking, and above the row's own (transparent-until-
       hover) background, so it still reads under --ss-surface-2. Same
       restrained "hint of hue, not a fill" language as the HA-native card. */
    .row::before {
      content: "";
      position: absolute;
      inset: 0;
      z-index: -1;
      opacity: 0;
      pointer-events: none;
      transition: opacity 160ms ease;
      background: var(--ss-row-tint, transparent);
    }
    .row.has-tint::before {
      opacity: 0.07;
    }
    .row.selected.has-tint::before,
    .row:hover.has-tint::before {
      opacity: 0.13;
    }
    .row:hover {
      background: var(--ss-surface-2);
    }
    .row.selected {
      background: var(--ss-surface-2);
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .row.archived .name {
      color: var(--ss-text-faint);
    }
    .name {
      display: flex;
      align-items: center;
      gap: 8px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .name .text {
      flex: 1 1 auto;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }
    ss-swatch-band {
      min-width: 0;
      width: 100%;
    }
    ss-scope-control {
      min-width: 0;
    }
    /* Grid-cell alignment shell for the actions column — kept as its own
       rule (rather than folded into ss-action-menu) because it also wraps
       the single-child rename span, which needs the identical flex-end
       justification the button list gets. */
    .actions-cell {
      display: flex;
      align-items: center;
      gap: 6px;
      justify-content: flex-end;
    }
    .rename {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .rename input {
      background: var(--ss-bg);
      color: var(--ss-text);
      border: 1px solid var(--ss-accent);
      border-radius: var(--ss-radius-sm);
      font-size: 15px;
      padding: 5px 8px;
      min-height: 34px;
      width: 130px;
    }
    .rename button {
      appearance: none;
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text);
      border-radius: var(--ss-radius-sm);
      font-size: 14px;
      font-weight: 600;
      padding: 5px 12px;
      min-height: 34px;
      cursor: pointer;
    }
    .rename button:hover {
      border-color: var(--ss-accent);
    }
    .ready {
      font-size: 15px;
      display: flex;
      flex-wrap: wrap;
      gap: 2px 8px;
      align-items: center;
      min-width: 0;
    }
    .ready .muted {
      color: var(--ss-text-faint);
    }
    .ready .err {
      color: var(--ss-err);
    }
    .ready .warn {
      color: var(--ss-warn);
    }
    .active-tag {
      font-size: 12px;
      color: var(--ss-ok);
      border: 1px solid var(--ss-ok);
      border-radius: 3px;
      padding: 1px 5px;
    }
    /* Live-session runtime tag (R5C §7): text state — Playing/Paused/
       Orphaned/N sessions — never color alone. Paused/orphaned read as
       attention tones, plain playing stays green. The text stays in the
       DOM at every width (browser_regression.mjs asserts textContent
       verbatim); the compact breakpoint below swaps the visible text for
       the alert glyph / pulse dot so the name gets the space back. */
    .runtime-tag {
      display: inline-flex;
      align-items: center;
      font-size: 12px;
      border: 1px solid var(--ss-ok);
      color: var(--ss-ok);
      border-radius: 3px;
      padding: 1px 5px;
      flex: none;
    }
    .runtime-tag.warn {
      border-color: var(--ss-warn);
      color: var(--ss-warn);
    }
    .tag-glyph {
      display: none;
      align-items: center;
    }
    .tag-glyph svg {
      display: block;
    }
    /* Stage 3+: the one continuous animation in the product (facelift plan
       §3) — a live row's runtime tag gets a small pulsing dot ahead of its
       state text. Purely decorative: the dot contributes no characters, so
       .runtime-tag's textContent (asserted verbatim by
       scripts/browser_regression.mjs) is unchanged. */
    .pulse-dot {
      display: inline-block;
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: currentColor;
      margin-right: 4px;
      animation: ss-row-pulse 2.4s ease-in-out infinite;
    }
    @media (prefers-reduced-motion: reduce) {
      .pulse-dot {
        animation: none;
      }
    }
    @keyframes ss-row-pulse {
      0%,
      100% {
        opacity: 1;
      }
      50% {
        opacity: 0.3;
      }
    }
    .count-badge {
      font-size: 10px;
      font-weight: 700;
      margin-left: 2px;
    }
    /* Compact rows (the scene list carries live-state chips, so it
       collapses earlier than Fixtures' 560px): drop the scope pill and
       status notes before the swatch — name and swatch share leftover
       width (no empty gutter, no 200px name cap). The live-state chip
       condenses to a symbol so the name keeps its space: attention tags
       collapse to the alert-triangle badge, healthy live tags to the
       pulse dot alone. The state text stays in the DOM (hidden) and in
       the hover tooltip. "current" loses its text chip here and reads as
       a soft green ring instead — same semantics, near-zero width. */
    @container ss-scene-row (max-width: 700px) {
      .row {
        grid-template-columns: minmax(0, 1.25fr) minmax(4.75rem, 1.15fr) var(--ss-actions-col);
      }
      ss-scope-control,
      .ready {
        display: none;
      }
      /* The live-state tag adopts the row's icon-button language
         (ss-action-menu dense), tinted by state: warn = alert triangle,
         ok = pulse dot. Text stays in the DOM (hidden) and in the
         tooltip. */
      .runtime-tag {
        width: var(--ss-row-btn);
        height: var(--ss-row-btn);
        justify-content: center;
        padding: 0;
        border-color: color-mix(in srgb, currentColor 50%, transparent);
        background: color-mix(in srgb, currentColor 10%, transparent);
      }
      .runtime-tag .tag-text {
        display: none;
      }
      .runtime-tag.warn .tag-glyph {
        display: inline-flex;
      }
      .runtime-tag.warn .pulse-dot {
        display: none;
      }
      .active-tag {
        display: none;
      }
      /* Narrow rows: the routine chip leaves the name cell — schedules open
         from the row's context menu ("Schedules…"). The chip element must
         STAY RENDERED (the schedule card lives in its tree and a display:none
         ancestor would zero-size even the top-layer card), so it is taken out
         of flow in a 0x0 clipped box instead: invisible, no row space, and
         the popover escapes the clip into the top layer when opened. */
      ss-routine-popover {
        position: absolute;
        width: 0;
        height: 0;
        overflow: hidden;
      }
      .row {
        --ss-actions-col: 88px;
      }
      /* Drawn by ::after (the row's ::before carries the palette tint),
         inset and rounded: a square full-bleed outline gets its corners
         clipped by the list panel's 12px rounded corners. */
      .row.active::after {
        content: "";
        position: absolute;
        inset: 2px;
        border: 1px solid color-mix(in srgb, var(--ss-ok) 55%, transparent);
        border-radius: 8px;
        pointer-events: none;
      }
    }
  `;

  constructor() {
    super();
    this.scene = {};
    this.summary = null;
    this.archived = false;
    this.active = false;
    this.sessionSummary = null;
    this.selected = false;
    this.routines = null;
    this.routinesAvailable = true;
    this.routinesReason = null;
    this._editing = false;
    // Narrow-row mode (mobile scene-row pass): mirrors the row's own
    // container-query breakpoint (700px). Below it the row is restructured —
    // the routine chip leaves the name cell for the row's context menu, and
    // Apply/Play condense into ONE menu control — so Name / Swatch /
    // Controls keep the space. ResizeObserver (not matchMedia) because the
    // breakpoint is the ROW's container width, not the viewport's.
    this._narrow = false;
    this._rowResizeObserver = null;
  }

  connectedCallback() {
    super.connectedCallback();
    this._rowResizeObserver = new ResizeObserver((entries) => {
      const narrow = entries[0].contentRect.width <= 700;
      if (narrow !== this._narrow) {
        this._narrow = narrow;
        this.requestUpdate();
      }
    });
    this._rowResizeObserver.observe(this);
  }

  disconnectedCallback() {
    if (this._rowResizeObserver) {
      this._rowResizeObserver.disconnect();
      this._rowResizeObserver = null;
    }
    super.disconnectedCallback();
  }


  /** This scene currently owns at least one non-"stopped" R5 session. */
  #isLive() {
    return !!this.sessionSummary && this.sessionSummary.count > 0;
  }

  /** Text for the name-cell runtime tag: state word for one session,
   *  plural count for several; issues appended when degraded. */
  #runtimeTag() {
    const sum = this.sessionSummary;
    if (!sum || !sum.count) return "";
    if (sum.issueCount > 0) {
      return sum.count > 1 ? `${sum.count} sessions · ${sum.issueCount} issue${sum.issueCount === 1 ? "" : "s"}` : `${sum.issueCount} issue${sum.issueCount === 1 ? "" : "s"}`;
    }
    if (sum.count > 1) return `${sum.count} sessions`;
    const words = { active: "Playing", paused: "Paused", held: "Held by sync", orphaned: "Orphaned" };
    return words[sum.states[0]] || "Playing";
  }

  #commandFor(action) {
    const map = {
      apply: "scene.apply",
      "takeover-apply": "scene.apply",
      preview: "scene.preview",
      play: "playback.start",
      // Builder ENTRY is observational (scene.preview_draft is allowed even
      // in read_only); persistence is gated per-command inside the Builder.
      edit: "scene.preview_draft",
      duplicate: "scene.preview_draft",
      archive: "scene.archive",
      restore: "scene.restore",
      rename: "scene.rename",
      "rename-start": "scene.rename",
      // Row-click only opens the Builder (same gate as edit/duplicate);
      // persistence stays gated by scene.update inside the Builder's Save.
      "upgrade-dynamic": "scene.preview_draft",
    };
    return map[action] || null;
  }

  #available(action) {
    // Backend-declared runtime policy: actions the live mode will reject are
    // rendered unavailable rather than silently failing.
    if (!Array.isArray(this.allowedCommands)) return true;
    const command = this.#commandFor(action);
    return command === null || this.allowedCommands.includes(command);
  }

  #emit(action, extra = {}) {
    this.dispatchEvent(
      new CustomEvent("scene-action", {
        detail: { action, scene_id: this.scene.id, ...extra },
        bubbles: true,
        composed: true,
      })
    );
  }

  #onSelect() {
    this.dispatchEvent(
      new CustomEvent("select-scene", { detail: { id: this.scene.id }, bubbles: true, composed: true })
    );
  }

  /** Narrow rows carry no routine chip; the context menu's "Schedules…"
   *  item opens the same schedule card, anchored to this row (mobile
   *  scene-row pass). */
  #openRoutines(e) {
    e.stopPropagation();
    const chip = this.renderRoot.querySelector("ss-routine-popover");
    if (chip) chip.open(this.getBoundingClientRect());
  }

  #startRename(e) {
    // The overflow-menu Rename item enters the row's OWN inline editor
    // directly (the menu closes itself via popovertargetaction="hide");
    // persistence still flows through the existing scene-action
    // "rename" -> store scene.rename seam on submit.
    if (e) e.stopPropagation();
    this._editing = true;
    this.requestUpdate();
  }

  #submitRename(e) {
    e.stopPropagation();
    const input = e.target.closest(".rename").querySelector("input");
    const name = input.value.trim();
    this._editing = false;
    if (name && name !== this.scene.name) this.#emit("rename", { name });
    this.requestUpdate();
  }

  #cancelRename(e) {
    e.stopPropagation();
    this._editing = false;
    this.requestUpdate();
  }

  /**
   * Post-R5 visual polish: the row's ONE primary, high-frequency action
   * (Apply, or Play/Playback-focus for a dynamic scene) — same keys, same
   * `#available()` policy gating, same `scene-action` payloads as the
   * original flat action row; only `label` is an icon (`ariaLabel`/`title`
   * carry the word). Archived rows have exactly one action (Restore) and
   * no overflow — there is nothing lower-frequency to hide there.
   */
  #primaryItems() {
    if (this.archived) {
      return [
        {
          key: "restore",
          label: iconRestore(17),
          ariaLabel: "Restore",
          disabled: !this.#available("restore"),
          title: this.#available("restore") ? "Restore" : "Unavailable in the current backend mode",
          onClick: (e) => { e.stopPropagation(); this.#emit("restore"); },
        },
      ];
    }
    const items = [
      {
        key: "apply",
        label: iconApply(17),
          ariaLabel: "Apply",
          disabled: !this.#available("apply"),
          title: this.#available("apply")
            ? "Apply this scene to its targets"
            : "Apply is blocked in the current backend mode",
        onClick: (e) => { e.stopPropagation(); this.#emit("apply"); },
      },
    ];
    const sum = this.summary;
    // R5C dynamic-playback actions (plan §7): with ZERO live sessions a
    // dynamic scene offers Play (playback.start). Once ANY live session
    // exists — one or several, and several can legitimately coexist for the
    // same scene against disjoint fixtures — the row no longer exposes
    // Pause/Stop: a row-level button that implicitly picks one session is
    // ambiguous, so the row offers an unambiguous "Playback…"/"N sessions"
    // focus affordance instead. Pause/Resume/Stop stay session-addressed in
    // the Live playback panel, the canonical control surface.
    if (sum && sum.dynamic) {
      if (this.#isLive()) {
        const count = this.sessionSummary.count;
        items.push({
          key: "playback-focus",
          label: html`${iconWaveform(17)}${count > 1 ? html`<span class="count-badge">${count}</span>` : ""}`,
          ariaLabel: count > 1 ? `${count} sessions` : "Playback…",
          title: "Show the live playback session(s) for this scene (pause, resume, or stop there)",
          onClick: (e) => { e.stopPropagation(); this.#emit("playback-focus"); },
        });
      } else {
        items.push({
          key: "play",
          label: iconPlay(17),
          ariaLabel: "Play",
          disabled: !this.#available("play"),
          title: this.#available("play") ? "Play" : "Unavailable in the current backend mode",
          onClick: (e) => { e.stopPropagation(); this.#emit("play"); },
        });
      }
    }
    return items;
  }

  /**
   * Post-R5 visual polish: lower-frequency actions (Dry run, Rename,
   * Archive) move out of the flat icon row into a real "…" overflow menu
   * (<ss-overflow-menu>) — same action keys, same `#available()` gating,
   * same `scene-action` payloads, same accessible names, unavailable for
   * archived rows (nothing to move there). Menu items carry visible text
   * (not just an icon) since a dropdown has room for it.
   */
  #secondaryItems() {
    if (this.archived) return [];
    const sum = this.summary;
    const eligible = sum && sum.dynamicEligible;
    const canAnimate = !!(eligible && eligible.capable > 0);
    const items = [];
    // Narrow rows carry no routine chip (mobile scene-row pass): schedules
    // live in the row's context menu instead, opening the same card
    // anchored to this row.
    if (this._narrow) {
      items.push({
        key: "schedules",
        icon: iconClock(16),
        label: "Schedules…",
        ariaLabel: "Schedules",
        title: "Time schedules for this scene",
        onClick: (e) => { this.#openRoutines(e); },
      });
    }
    // External light sync (hyperHDR) is holding fixtures: offer the one-shot
    // takeover apply here rather than as a second bolt icon next to the
    // primary Apply/Play button (indistinguishable at a glance — both used
    // iconApply). The overflow item's visible label carries the distinction
    // instead (default remains yield — hyperHDR holds until it surrenders).
    if (this.contentionActive) {
      items.push({
        key: "takeover-apply",
        icon: iconApply(16),
        label: "Take over & apply",
        ariaLabel: "Take over & apply",
        disabled: !this.#available("takeover-apply"),
        title: this.#available("takeover-apply")
          ? "Suspend hyperHDR sync for these fixtures, then apply"
          : "Apply is blocked in the current backend mode",
        onClick: (e) => { e.stopPropagation(); this.#emit("takeover-apply"); },
      });
    }
    items.push(
      {
        key: "edit",
        icon: iconPencil(16),
        label: "Edit",
        ariaLabel: "Edit scene in the Builder",
        title: this.#available("edit")
          ? "Open this scene in the Builder (saving follows backend policy)"
          : "Unavailable in the current backend mode",
        disabled: !this.#available("edit"),
        onClick: (e) => { e.stopPropagation(); this.#emit("edit"); },
      },
      {
        key: "duplicate",
        icon: iconDuplicate(16),
        label: "Duplicate",
        ariaLabel: "Duplicate scene",
        title: this.#available("duplicate")
          ? "Copy this scene's intent into a new backend-authoritative scene"
          : "Unavailable in the current backend mode",
        disabled: !this.#available("duplicate"),
        onClick: (e) => { e.stopPropagation(); this.#emit("duplicate"); },
      },
      {
        key: "preview",
        icon: iconDryRun(16),
        label: "Dry run",
        ariaLabel: "Dry run",
        title: "Simulate this scene without applying it",
        onClick: (e) => { e.stopPropagation(); this.#emit("preview"); },
      },
      {
        key: "rename-start",
        icon: iconPencil(16),
        label: "Rename",
        ariaLabel: "Rename",
        disabled: !this.#available("rename-start"),
        title: this.#available("rename-start") ? "Rename this scene" : "Unavailable in the current backend mode",
        // Enter the row's local inline rename editor; the parent's existing
        // "rename" -> scene.rename command path persists the submitted name.
        onClick: (e) => { this.#startRename(e); },
      },
      {
        key: "archive",
        icon: iconArchive(16),
        label: "Archive",
        ariaLabel: "Archive",
        disabled: !this.#available("archive"),
        title: this.#available("archive") ? "" : "Unavailable in the current backend mode",
        onClick: (e) => { e.stopPropagation(); this.#emit("archive"); },
      },
    );
    if (sum && !sum.dynamic) {
      items.splice(items.findIndex((item) => item.key === "preview"), 0, {
        key: "upgrade-dynamic",
        icon: iconSparkle(16),
        label: "Upgrade to dynamic",
        ariaLabel: "Upgrade to dynamic",
        disabled: !this.#available("upgrade-dynamic") || !canAnimate,
        title: !canAnimate
          ? "No targeted fixture supports native dynamic playback"
          : this.#available("upgrade-dynamic")
            ? `Open in Builder as a dynamic scene (${eligible.capable}/${eligible.total} fixtures support it natively)`
            : "Unavailable in the current backend mode",
        onClick: (e) => { e.stopPropagation(); this.#emit("upgrade-dynamic"); },
      });
    }
    return items;
  }

  #statusLabel(faults, ready) {
    if (this.archived) return "archived";
    if (this.#isLive() || faults === 0) return "";
    return ready === 0 ? "error" : "degraded";
  }

  #statusNotes(disabled, missing, degraded) {
    return { disabled, missing, degraded };
  }

  render() {
    const s = this.scene;
    const swatches = sceneLookSwatches(s);
    const sum = this.summary;
    const ready = sum ? sum.counts.ready : 0;
    const total = sum ? sum.counts.total : 0;
    const missing = sum ? sum.counts.missing : 0;
    const degraded = sum ? sum.counts.degraded : 0;
    const disabled = sum ? sum.counts.disabled : 0;
    // Intentionally disabled fixtures are skipped, not scene faults — same
    // rule as Overview Needs Attention / Fleet. A office scene that still
    // resolves Double Strip must not paint every row "degraded".
    const faults = missing + degraded;
    const tone = this.archived ? "idle" : faults === 0 ? "ok" : faults === total - disabled && ready === 0 ? "err" : "warn";
    const notes = this.#statusNotes(disabled, missing, degraded);
    const showNotes = !this.archived && (notes.disabled || notes.missing || notes.degraded);
    const runtimeTag = this.#runtimeTag();
    const runtimeWarn = !!this.sessionSummary &&
      (this.sessionSummary.states.includes("orphaned") ||
        this.sessionSummary.states.includes("paused") ||
        this.sessionSummary.issueCount > 0);

    const actions = this._editing
      ? html`
          <span class="rename">
            <input
              .value=${s.name}
              @click=${(e) => e.stopPropagation()}
              @keydown=${(e) => {
                if (e.key === "Enter") this.#submitRename(e);
                if (e.key === "Escape") this.#cancelRename(e);
              }}
            />
            <button @click=${this.#submitRename}>Save</button>
            <button @click=${this.#cancelRename}>✕</button>
          </span>
        `
      : this._narrow
        // Narrow rows (mobile scene-row pass): Apply/Play condense into ONE
        // menu control (⚡-triggered, offers both) so the row keeps two
        // compact controls total — actions + context — and Name / Swatch
        // keep their room.
        ? html`
            ${this.archived
              ? ""
              : html`<ss-overflow-menu
                  triggerLabel="Scene actions"
                  .triggerIcon=${iconApply(17)}
                  .items=${this.#primaryItems()}
                ></ss-overflow-menu>`}
            ${this.archived
              ? ""
              : html`<ss-overflow-menu triggerLabel="More scene actions" .items=${this.#secondaryItems()}></ss-overflow-menu>`}
          `
        : html`
            <ss-action-menu dense .items=${this.#primaryItems()}></ss-action-menu>
            ${this.archived
              ? ""
              : html`<ss-overflow-menu triggerLabel="More scene actions" .items=${this.#secondaryItems()}></ss-overflow-menu>`}
          `;

    return html`
      <div
        class="row ${this.selected ? "selected" : ""} ${this.active ? "active" : ""} ${this.archived ? "archived" : ""} ${swatches.length ? "has-tint" : ""}"
        style=${swatches.length ? `--ss-row-tint:${bandGradient(swatches)}` : ""}
        role="button"
        tabindex="0"
        @click=${this.#onSelect}
        @keydown=${(e) => (e.key === "Enter" || e.key === " ") && this.#onSelect()}
      >
        <div class="name">
          <!-- R5C corrective pass: the runtime tag (Playing/Paused/Orphaned/
               N sessions/N issues) owns live playback-state wording — a
               "playing" glyph label here would visibly contradict a Paused
               or Orphaned tag ("playing · Paused"). Live rows show the
               catalog-health dot alone; the tag next to the name carries
               the state text. -->
          <ss-status-glyph
            tone=${tone}
            label=${this.#statusLabel(faults, ready)}
          ></ss-status-glyph>
          <span class="text">${s.name}</span>
          <!-- HA-native routine affordance (routines pass): a compact
               temporal chip in the row chrome — clock-only when unscheduled,
               the concise recurrence + time for one routine, "N routines"
               for several. Anchored popover, top-layer (never clipped).
               Archived rows stay unschedulable; NARROW rows drop the chip
               entirely — schedules move into the row's context menu. -->
          ${!this.archived
            ? html`<ss-routine-popover
                .scene=${s}
                .routines=${this.routines}
                .routinesAvailable=${this.routinesAvailable}
                .routinesReason=${this.routinesReason}
                .allowedCommands=${this.allowedCommands}
                .triggerDisabled=${this._narrow}
              ></ss-routine-popover>`
            : ""}
          ${runtimeTag
            ? html`<span
                class="runtime-tag ${runtimeWarn ? "warn" : ""}"
                title=${this.#isLive()
                  ? `${runtimeTag} — opens scene details`
                  : ""}
                >${this.#isLive() ? html`<span class="pulse-dot"></span>` : ""}<span class="tag-text"
                    >${runtimeTag}</span
                  ><span class="tag-glyph" aria-hidden="true">${iconAlertTriangle(16)}</span></span
              >`
            : ""}
          ${this.active ? html`<span class="active-tag">current</span>` : ""}
        </div>
        <ss-scope-control .groups=${s.target_ids || []}></ss-scope-control>
        <ss-swatch-band variant="band" .palette=${swatches}></ss-swatch-band>
        <div class="ready">
          ${showNotes
            ? html`
                ${notes.disabled ? html`<span class="muted">${notes.disabled} disabled</span>` : ""}
                ${notes.missing ? html`<span class="err">${notes.missing} missing</span>` : ""}
                ${notes.degraded ? html`<span class="warn">${notes.degraded} degraded</span>` : ""}
              `
            : ""}
        </div>
        <div class="actions-cell">${actions}</div>
      </div>
    `;
  }
}

customElements.define("ss-scene-row", SsSceneRow);
