/**
 * <ss-playback-panel> — the canonical live-playback surface (R5C plan §4).
 *
 * Receives the normalized playback collection + catalogs + runtime policy
 * and renders:
 *   - one compact aggregate counts header ("2 playing · 1 paused · 0
 *     orphaned", plus an optional subdued "N recent stopped" history note —
 *     never the stopped retention list itself);
 *   - one <ss-playback-session> per LIVE session (active/paused/orphaned),
 *     each individually session-addressable;
 *   - the APPLIED static scene (status `current`) as the hero card — the
 *     same presentation a playing session gets — while no session is live:
 *     the fixtures are holding that scene's look, and the panel is the one
 *     surface that answers "what are my lights doing right now". A running
 *     session owns the fixtures, so the static hero yields while any
 *     session is live (same rule Overview's old "Current:" fleet line used);
 *   - a compact "Playback idle" empty state (not a giant empty card).
 *
 * Emits bubbling `playback-action` CustomEvents upward (re-emitted from the
 * session rows) carrying `{action, session_id, scene_id, scene_name}` —
 * it never calls backend clients directly; the host view routes them
 * through the store command layer.
 *
 * @prop {object} playback normalized playback collection (playback.js
 *   normalizePlayback)
 * @prop {string[]} [allowedCommands] status().runtime.allowed_commands
 * @prop {Record<string,string>} [fixtureNames] fixture_id -> display name
 * @prop {string} [heading] panel heading (default "Playback")
 * @prop {string} [highlightSessionId] session to visually highlight
 *   (cross-navigation target from exceptions / scene rows)
 * @prop {string} [highlightSceneId] highlight ALL sessions of this scene
 * @prop {boolean} [stoppedHistory] show the subdued "N recent stopped" note
 * @prop {{scene_id: string, name: string, targets: string, fixtures: number,
 *   palette: string[]} | null} [current] applied static scene view model
 *   (state.js currentSceneView) — rendered as the hero card while no
 *   session is live; display-only, no controls: a static look has no
 *   session to pause/stop, re-apply lives on the scene rows
 */
import { LitElement, html, css } from "lit";
import { liveSessions, summarizeSession } from "../playback.js";
import "./ss-panel.js";
import "./ss-playback-session.js";

export class SsPlaybackPanel extends LitElement {
  static properties = {
    playback: { attribute: false },
    allowedCommands: { type: Array },
    fixtureNames: { attribute: false },
    heading: { type: String },
    highlightSessionId: { type: String },
    highlightSceneId: { type: String },
    stoppedHistory: { type: Boolean },
    // Stage 3+ (Overview cockpit, facelift plan §4.2): render the first
    // live session with the hero variant, all others unchanged (still real
    // <ss-playback-session> rows — same elements, same events, same
    // Pause/Resume/Stop text — browser_regression.mjs's per-row assertions
    // apply identically). Scenes' Live playback panel omits this prop and
    // is visually 100% unchanged.
    heroFirst: { type: Boolean },
    // scene_id -> palette hex[], for the hero session's gradient wash only.
    scenePalettes: { attribute: false },
    // Applied static scene view model (state.js currentSceneView); rendered
    // display-only while no session is live.
    current: { attribute: false },
  };

  static styles = css`
    :host {
      display: block;
    }
    .head {
      display: flex;
      align-items: baseline;
      gap: 8px;
      flex-wrap: wrap;
      padding: 6px 10px;
      border-bottom: 1px solid var(--ss-border-soft);
      font-size: 15px;
      min-height: 36px;
      box-sizing: border-box;
    }
    .head b {
      font-weight: 600;
    }
    .head span:not(.history) {
      color: var(--ss-text-faint);
    }
    .head span:not(.history) b {
      color: var(--ss-text-faint);
    }
    .head .active.on,
    .head .active.on b {
      color: var(--ss-ok);
    }
    .head .paused.on,
    .head .paused.on b,
    .head .orphaned.on,
    .head .orphaned.on b {
      color: var(--ss-warn);
    }
    .head .history {
      margin-left: auto;
    }
    .history {
      color: var(--ss-text-faint);
      font-size: 14px;
    }
    .idle {
      color: var(--ss-text-faint);
      font-size: 15px;
      padding: 6px 10px;
    }
    /* Applied static scene (status "current"), shown while nothing plays:
       the SAME hero card a playing session gets (same geometry, type scale,
       palette wash, and full-width band — see <ss-playback-session>'s hero
       variant), so the panel answers "what are my lights doing right now"
       with one consistent visual whether the answer is dynamic or static.
       No controls, no provider/fidelity body — a static look has no session
       to pause/stop; re-apply lives on the scene rows. */
    .current-hero {
      position: relative;
      padding: 22px 24px;
      border-radius: var(--ss-radius-lg);
      border: 1px solid var(--ss-border-soft);
      background: var(--ss-surface);
      overflow: hidden;
    }
    /* Touch/mobile: reclaim edge padding at phone widths, same as the
       session hero. */
    @media (max-width: 640px), (pointer: coarse) {
      .current-hero {
        padding: 18px 16px;
      }
    }
    .current-hero::before {
      content: "";
      position: absolute;
      inset: 0;
      background: var(--hero-wash, none);
      pointer-events: none;
    }
    .current-hero .hero-inner {
      position: relative;
      display: flex;
      justify-content: space-between;
      gap: 24px;
      flex-wrap: wrap;
    }
    .current-hero .hero-content {
      flex: 1;
      min-width: 0;
    }
    .current-hero .hero-eyebrow {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 7px;
      font-size: var(--ss-size-11);
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-ok);
    }
    .current-hero .hero-eyebrow .where {
      color: var(--ss-text-dim);
      font-weight: 600;
    }
    .current-hero .hero-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
    }
    .current-hero .hero-title {
      font-size: var(--ss-size-28);
      font-weight: 700;
      letter-spacing: -0.01em;
      margin: 8px 0 10px;
    }
    .current-hero .hero-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .current-hero .hero-chip {
      display: inline-flex;
      align-items: baseline;
      gap: 5px;
      padding: 2px 9px;
      border-radius: 999px;
      border: 1px solid var(--ss-border-soft);
      background: rgba(255, 255, 255, 0.04);
      font-size: var(--ss-size-12);
      color: var(--ss-text-dim);
    }
    .current-hero .hero-chip b {
      color: var(--ss-text);
      font-weight: 600;
    }
    .current-hero .hero-band {
      position: relative;
      height: 8px;
      width: 100%;
      border-radius: 999px;
      margin-top: 20px;
    }
    .hero-slot {
      padding: 10px;
    }
    .chips-label {
      padding: 0 10px 6px;
      font-size: var(--ss-size-11);
      color: var(--ss-text-faint);
    }
  `;

  constructor() {
    super();
    this.playback = null;
    this.allowedCommands = null;
    this.fixtureNames = {};
    this.heading = "Playback";
    this.highlightSessionId = "";
    this.highlightSceneId = "";
    this.stoppedHistory = false;
    this.heroFirst = false;
    this.scenePalettes = {};
    this.current = null;
  }

  #onAction(e) {
    // Re-emit the session row's event so consumers only ever listen on the
    // panel (the behavioral seam facelift Stage 3+ should reuse).
    e.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("playback-action", { detail: e.detail, bubbles: true, composed: true })
    );
  }

  #sessionRow(session, { hero } = {}) {
    const highlighted =
      (this.highlightSessionId && session.session_id === this.highlightSessionId) ||
      (this.highlightSceneId && session.scene_id === this.highlightSceneId);
    return html`
      <ss-playback-session
        variant=${hero ? "hero" : "row"}
        .session=${session}
        .summary=${summarizeSession(session)}
        .allowedCommands=${this.allowedCommands}
        .fixtureNames=${this.fixtureNames || {}}
        .heroPalette=${hero ? (this.scenePalettes || {})[session.scene_id] || null : null}
        .highlighted=${!!highlighted}
        @playback-action=${this.#onAction}
      ></ss-playback-session>
    `;
  }

  #currentHero() {
    const c = this.current;
    if (!c) return "";
    const palette = Array.isArray(c.palette) ? c.palette : [];
    const wash = palette.length
      ? `linear-gradient(120deg, ${palette.map((hex) => `${hex}29`).join(", ")})`
      : "none";
    // Hard color stops, not a smooth blend — same reason as the session
    // hero's band: a plain multi-stop gradient bleeds adjacent hues
    // together and reads as one flat wash instead of the scene's distinct,
    // ordered palette.
    const band =
      palette.length > 1
        ? `linear-gradient(90deg, ${palette
            .map((hex, i) => {
              const start = (i / palette.length) * 100;
              const end = ((i + 1) / palette.length) * 100;
              return `${hex} ${start}%, ${hex} ${end}%`;
            })
            .join(", ")})`
        : palette.length === 1
          ? palette[0]
          : "var(--ss-border)";
    return html`
      <div
        class="current-hero"
        role="group"
        aria-label="Current scene: ${c.name}"
        style="--hero-wash: ${wash}"
      >
        <div class="hero-inner">
          <div class="hero-content">
            <div class="hero-eyebrow">
              <span class="hero-dot"></span>
              Current
              ${c.targets ? html`<span class="where">· ${c.targets}</span>` : ""}
            </div>
            <div class="hero-title">${c.name}</div>
            <div class="hero-chips">
              ${c.fixtures
                ? html`<span class="hero-chip"><b>${c.fixtures}</b> fixture${c.fixtures === 1 ? "" : "s"}</span>`
                : html`<span class="hero-chip">No fixtures resolved</span>`}
            </div>
          </div>
        </div>
        <div class="hero-band" style="background:${band}" title="Scene palette"></div>
      </div>
    `;
  }

  render() {
    const playback = this.playback || { sessions: [], counts: {} };
    const live = liveSessions(playback);
    const active = live.filter((s) => s.state === "active").length;
    const paused = live.filter((s) => s.state === "paused").length;
    const orphaned = live.filter((s) => s.state === "orphaned").length;
    const stopped = (playback.counts && playback.counts.stopped) || 0;
    // Stage 3+ hero mode picks the most-attention-needing/most-recent live
    // session for the hero slot: an orphaned or degraded session outranks a
    // plain active one; ties keep the collection's own order. Everything
    // else in `live` still renders as a normal <ss-playback-session> row —
    // see the `heroFirst` prop doc above.
    const heroSession =
      this.heroFirst && live.length
        ? [...live].sort((a, b) => {
            const rank = (s) => (s.state === "orphaned" ? 0 : summarizeSession(s).degraded.length ? 1 : 2);
            return rank(a) - rank(b);
          })[0]
        : null;
    const rest = heroSession ? live.filter((s) => s !== heroSession) : live;
    // The applied static scene yields while a session is live: the session
    // owns those fixtures until it stops (the same visibility rule the
    // Overview cockpit's former "Current:" fleet line used).
    const showCurrent = !!this.current && !live.length;
    // Whichever hero is on stage — a playing session or the applied static
    // look — gets the same padded, heading-less panel billing.
    const heroCard = !!heroSession || showCurrent;
    return html`
      <ss-panel variant=${heroCard ? "padded" : "list"} heading=${heroCard ? "" : this.heading}>
        <div class="head" role="status">
          <span class="active ${active ? "on" : ""}"><b>${active}</b> playing</span>
          <span class="paused ${paused ? "on" : ""}"><b>${paused}</b> paused</span>
          <span class="orphaned ${orphaned ? "on" : ""}"><b>${orphaned}</b> orphaned</span>
          ${this.stoppedHistory && stopped
            ? html`<span class="history">${stopped} recent stopped</span>`
            : ""}
        </div>
        ${showCurrent ? html`<div class="hero-slot">${this.#currentHero()}</div>` : ""}
        ${heroSession ? html`<div class="hero-slot">${this.#sessionRow(heroSession, { hero: true })}</div>` : ""}
        ${rest.length
          ? html`
              ${heroSession ? html`<div class="chips-label">Also playing</div>` : ""}
              ${rest.map((session) => this.#sessionRow(session))}
            `
          : heroSession
            ? ""
            : showCurrent
              ? ""
              : html`<div class="idle">Playback idle</div>`}
      </ss-panel>
    `;
  }
}

customElements.define("ss-playback-panel", SsPlaybackPanel);
