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
    return html`
      <ss-panel variant=${heroSession ? "padded" : "list"} heading=${heroSession ? "" : this.heading}>
        <div class="head" role="status">
          <span class="active ${active ? "on" : ""}"><b>${active}</b> playing</span>
          <span class="paused ${paused ? "on" : ""}"><b>${paused}</b> paused</span>
          <span class="orphaned ${orphaned ? "on" : ""}"><b>${orphaned}</b> orphaned</span>
          ${this.stoppedHistory && stopped
            ? html`<span class="history">${stopped} recent stopped</span>`
            : ""}
        </div>
        ${heroSession ? html`<div class="hero-slot">${this.#sessionRow(heroSession, { hero: true })}</div>` : ""}
        ${rest.length
          ? html`
              ${heroSession ? html`<div class="chips-label">Also playing</div>` : ""}
              ${rest.map((session) => this.#sessionRow(session))}
            `
          : heroSession
            ? ""
            : html`<div class="idle">Playback idle</div>`}
      </ss-panel>
    `;
  }
}

customElements.define("ss-playback-panel", SsPlaybackPanel);
