/**
 * <ss-playback-session> — one live playback session row (R5C plan §4).
 *
 * Makes five things readable at a glance: WHAT (scene name), WHERE (targets
 * + fixture count), STATE (Playing/Paused/Orphaned — text plus glyph, never
 * color alone), HOW WELL (per-provider execution + backend fidelity counts,
 * with degraded fixtures in a collapsed disclosure), and WHAT CAN I DO (the
 * correct session-addressed lifecycle controls, policy-gated by the parent
 * via playback.js `controlsForSession`).
 *
 * Pure presentation + control resolution: it never calls backend clients.
 * Actions are emitted upward as bubbling `playback-action` CustomEvents
 * carrying `{action, session_id, scene_id, scene_name}`; the parent view
 * routes them through the store command layer (state.sendCommand refreshes
 * status authoritatively — no optimistic session mutation anywhere).
 *
 * Healthy sessions stay compact (no fixture wall). Degraded detail expands
 * only on request via a keyboard-operable native <details> disclosure.
 *
 * @prop {object} session normalized PlaybackSession (playback.js)
 * @prop {object} summary `summarizeSession(session)` view model
 * @prop {string[]} [allowedCommands] status().runtime.allowed_commands
 * @prop {Record<string,string>} [fixtureNames] fixture_id -> display name
 * @prop {boolean} highlighted focus highlight (cross-navigation target)
 * @prop {"row"|"hero"} [variant] Stage 3+ (facelift plan §4.2): "hero"
 *   renders the SAME control resolution/event contract as "row" (the
 *   default) inside a larger, palette-tinted single-session card for the
 *   Overview cockpit. `#controlItems()`/`#emit()`/`summarizeSession` are
 *   untouched — only the template/CSS branch on this prop, so
 *   `playback-action` events and the Pause/Resume/Stop glyph buttons'
 *   accessible names (asserted via aria-label by
 *   scripts/browser_regression.mjs) are identical in both variants.
 */
import { LitElement, html, css } from "lit";
import { controlsForSession, providerLabel, sessionTone } from "../playback.js";
import { iconPlay, iconPause, iconStop } from "./icons.js";
import "./ss-status-glyph.js";
import "./ss-action-menu.js";

const STATE_WORDS = { active: "Playing", paused: "Paused", held: "Held by sync", orphaned: "Orphaned", stopped: "Stopped" };

/** Compact abbreviations for backend fidelity levels (same convention as
 *  ss-scene-row's fidelity column). Counts stay exact; only display is
 *  abbreviated, with full words in the line title. */
const FIDELITY_ABBREV = { native: "nat", equivalent: "eq", approximate: "approx", unsupported: "unsup" };

/** "office_strip" -> "Office strip" (display only; ids stay untouched). */
const prettyTarget = (id) => {
  const words = String(id).replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : words;
};

const FIDELITY_LEVELS = ["native", "equivalent", "approximate", "unsupported"];
const FIDELITY_LABELS = { native: "Native", equivalent: "Equivalent", approximate: "Approximate", unsupported: "Unsupported" };

export class SsPlaybackSession extends LitElement {
  static properties = {
    session: { attribute: false },
    summary: { attribute: false },
    allowedCommands: { type: Array },
    fixtureNames: { attribute: false },
    highlighted: { type: Boolean },
    variant: { type: String },
    // Hero variant only (Overview cockpit): this session's scene palette,
    // for the gradient wash — resolved by the parent from the scene
    // catalog (playback sessions don't carry palette). Purely decorative;
    // never touches the ok/warn/err/idle status tokens.
    heroPalette: { attribute: false },
  };

  static styles = css`
    :host {
      display: block;
    }
    .session {
      padding: 8px 10px;
      border-bottom: 1px solid var(--ss-border-soft);
    }
    .session:last-child {
      border-bottom: none;
    }
    .session.highlight {
      background: var(--ss-surface-2);
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .main {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      flex-wrap: wrap;
    }
    .name {
      font-size: var(--ss-size-15);
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      max-width: 260px;
    }
    .where {
      color: var(--ss-text-faint);
      font-size: 15px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .state {
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid var(--ss-border);
      border-radius: 3px;
      padding: 1px 6px;
      flex: none;
    }
    .state.active {
      color: var(--ss-ok);
      border-color: var(--ss-ok);
    }
    .state.paused {
      color: var(--ss-warn);
      border-color: var(--ss-warn);
    }
    .state.orphaned {
      color: var(--ss-warn);
      border-color: var(--ss-warn);
    }
    .spacer {
      flex: 1;
    }
    .actions-cell {
      display: flex;
      justify-content: flex-end;
    }
    .detail {
      display: flex;
      align-items: baseline;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 1px;
      padding-left: 16px; /* align under the name (glyph dot + gap) */
    }
    .exec {
      font-family: var(--ss-font-mono);
      font-size: 12px;
      color: var(--ss-text-faint);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 100%;
    }
    .note {
      font-size: 15px;
      padding-left: 16px;
      margin-top: 2px;
    }
    .note.warn {
      color: var(--ss-warn);
    }
    .note.legacy {
      color: var(--ss-text-faint);
    }
    /* Native <details>: keyboard-operable disclosure by default. */
    details.issues {
      display: inline-block;
      font-size: 13px;
      color: var(--ss-warn);
    }
    details.issues summary {
      cursor: pointer;
      user-select: none;
    }
    details.issues summary:focus-visible {
      outline: 1px solid var(--ss-accent);
      outline-offset: 2px;
    }
    details.issues ul {
      margin: 4px 0 2px;
      padding: 0;
      list-style: none;
      color: var(--ss-text-dim);
    }
    details.issues li {
      padding: 1px 0;
    }
    details.issues .iname {
      color: var(--ss-text);
    }
    details.issues .idetail {
      color: var(--ss-text-faint);
    }

    /* ---- hero variant (Stage 3+, facelift plan §4.2) ---- */
    .hero {
      position: relative;
      padding: 22px 24px;
      border-radius: var(--ss-radius-lg);
      border: 1px solid var(--ss-border-soft);
      background: var(--ss-surface);
      overflow: hidden;
    }
    /* Touch/mobile: the hero is the Overview cockpit's dominant card — keep
       its presence but reclaim edge padding for phone widths. */
    @media (max-width: 640px), (pointer: coarse) {
      .hero {
        padding: 18px 16px;
      }
    }
    .hero::before {
      content: "";
      position: absolute;
      inset: 0;
      background: var(--hero-wash, none);
      pointer-events: none;
    }
    .hero-inner {
      position: relative;
      display: flex;
      justify-content: space-between;
      gap: 24px;
      flex-wrap: wrap;
    }
    /* Sole content column of .hero-inner: grow to the card edge so the
       right-aligned chips row inside can actually reach the right side. */
    .hero-content {
      flex: 1;
      min-width: 0;
    }
    .hero-inner > div {
      flex: 1 1 auto;
      min-width: 0;
    }
    .hero-eyebrow {
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
    .hero-eyebrow.paused,
    .hero-eyebrow.orphaned,
    .hero-eyebrow.held {
      color: var(--ss-warn);
    }
    .hero-eyebrow .where {
      color: var(--ss-text-dim);
      font-weight: 600;
    }
    .hero-live-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
    }
    .hero-live-dot.pulse {
      animation: ss-hero-pulse 2.4s ease-in-out infinite;
    }
    @media (prefers-reduced-motion: reduce) {
      .hero-live-dot.pulse {
        animation: none;
      }
    }
    @keyframes ss-hero-pulse {
      0%,
      100% {
        opacity: 1;
      }
      50% {
        opacity: 0.3;
      }
    }
    .hero-title {
      font-size: var(--ss-size-28);
      font-weight: 700;
      letter-spacing: -0.01em;
      margin: 8px 0 10px;
    }
    .hero-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .hero-chip {
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
    .hero-chip b {
      color: var(--ss-text);
      font-weight: 600;
    }
    .hero-chip.warn {
      border-color: var(--ss-warn);
      color: var(--ss-warn);
    }
    .hero-chip.warn b {
      color: var(--ss-warn);
    }
    .hero-band {
      position: relative;
      height: 8px;
      width: 100%;
      border-radius: 999px;
      margin-top: 20px;
    }
    .hero-actions {
      margin-top: 16px;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 14px;
    }
    /* Chips share the actions row, pushed to the trailing edge; they wrap
       below when the details disclosure takes its full row. */
    .hero-actions .hero-chips {
      margin-left: auto;
      order: 1;
    }
    details.hero-details[open] {
      display: block;
      flex: 1 1 100%;
      order: 2;
    }
    details.hero-details summary.plain {
      color: var(--ss-text-faint);
    }
    .detail-body {
      display: flex;
      flex-wrap: wrap;
      gap: 14px 40px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px solid var(--ss-border-soft);
    }
    .detail-body section {
      min-width: 220px;
      flex: 1 1 260px;
    }
    .detail-body h4 {
      margin: 0 0 6px;
      font-size: var(--ss-size-11);
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
    }
    .detail-body ul {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .detail-body li {
      padding: 2px 0;
      font-size: var(--ss-size-13);
    }
    .fid-bar {
      display: flex;
      width: 100%;
      height: 6px;
      gap: 2px;
      border-radius: 999px;
      overflow: hidden;
      margin-bottom: 8px;
    }
    .fid-bar span {
      display: block;
      min-width: 3px;
    }
    .fid-bar .native,
    .swatch.native {
      background: var(--ss-ok);
    }
    .fid-bar .equivalent,
    .swatch.equivalent {
      background: var(--ss-accent);
    }
    .fid-bar .approximate,
    .swatch.approximate {
      background: var(--ss-warn);
    }
    .fid-bar .unsupported,
    .swatch.unsupported {
      background: var(--ss-err);
    }
    .fid-list li {
      cursor: help;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ss-text-dim);
    }
    .fid-list .swatch {
      width: 8px;
      height: 8px;
      border-radius: 2px;
      flex: none;
    }
    .fid-list .n {
      margin-left: auto;
      color: var(--ss-text);
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }
    .hero-issues {
      margin-top: 10px;
    }
  `;

  constructor() {
    super();
    this.session = null;
    this.summary = null;
    this.allowedCommands = null;
    this.fixtureNames = {};
    this.highlighted = false;
    this.variant = "row";
    this.heroPalette = null;
  }

  #emit(action) {
    this.dispatchEvent(
      new CustomEvent("playback-action", {
        detail: {
          action,
          session_id: this.session.session_id,
          scene_id: this.session.scene_id,
          scene_name: this.summary ? this.summary.scene_name : this.session.scene_id,
        },
        bubbles: true,
        composed: true,
      })
    );
  }

  /** Resolved <ss-action-menu> items — offered actions only, each gated by
   *  controlsForSession (sendable id AND runtime policy). Glyph buttons: the
   *  icon carries no text, so `name` (not `label`) supplies the accessible
   *  name/tooltip — never rely on `label`'s stringified form for those. */
  #controlItems() {
    const { controls } = controlsForSession(this.session, this.allowedCommands);
    const sceneLabel = this.summary ? this.summary.scene_name : this.session.session_id;
    const defs = [
      { action: "pause", icon: iconPause(16), name: "Pause" },
      { action: "resume", icon: iconPlay(16), name: "Resume" },
      { action: "stop", icon: iconStop(16), name: "Stop" },
    ];
    return defs
      .filter((d) => controls[d.action].offered)
      .map((d) => {
        const control = controls[d.action];
        return {
          key: d.action,
          label: d.icon,
          disabled: !control.enabled,
          title: control.title || d.name,
          ariaLabel: `${d.name} playback: ${sceneLabel}`,
          onClick: (e) => {
            e.stopPropagation();
            this.#emit(d.action);
          },
        };
      });
  }

  /** Compact per-provider execution text, e.g.
   *  "Hue 6/6 ok · 6 native_scene · 1 native_effect". */
  #providerText() {
    return (this.summary ? this.summary.providers : [])
      .map((p) => {
        const kinds = Object.entries(p.kinds)
          .filter(([, n]) => n > 0)
          .map(([kind, n]) => `${n} ${kind}`)
          .join(" · ");
        return `${providerLabel(p.provider)} ${p.ok}/${p.total} ok${kinds ? ` · ${kinds}` : ""}`;
      })
      .join("  |  ");
  }

  /** Exact backend fidelity counts, non-zero only ("nat 5 · approx 1"). */
  #fidelityText() {
    const fidelity = this.summary ? this.summary.fidelity : null;
    if (!fidelity) return "";
    return Object.entries(FIDELITY_ABBREV)
      .filter(([level]) => fidelity[level] > 0)
      .map(([level, abbrev]) => `${fidelity[level]} ${abbrev}`)
      .join(" · ");
  }

  /** Hover text for one fidelity level: which fixtures landed there and why. */
  #fidelityTitle(level) {
    const execs = ((this.session && this.session.fixture_executions) || []).filter((e) => e.fidelity === level);
    if (!execs.length) return "";
    const lines = execs.map((e) => {
      // Reasons only matter where fidelity is degraded; native/equivalent
      // details are provider boilerplate.
      const why = level === "approximate" || level === "unsupported" ? e.detail || e.execution || "" : "";
      return `• ${this.#fixtureName(e.fixture_id)}${why ? ` — ${why}` : ""}`;
    });
    return `${FIDELITY_LABELS[level]} (${execs.length}):\n${lines.join("\n")}`;
  }

  #fixtureName(fixtureId) {
    return (this.fixtureNames && this.fixtureNames[fixtureId]) || fixtureId;
  }

  render() {
    if (!this.summary) return html``;
    return this.variant === "hero" ? this.#renderHero() : this.#renderRow();
  }

  #renderRow() {
    const summary = this.summary;
    const tone = sessionTone(summary);
    const stateWord = STATE_WORDS[summary.state] || summary.state;
    const whereParts = [];
    if (summary.target_ids.length) whereParts.push(summary.target_ids.join(" · "));
    if (summary.fixture_count) whereParts.push(`${summary.fixture_count} fixture${summary.fixture_count === 1 ? "" : "s"}`);
    const providerText = this.#providerText();
    const fidelityText = this.#fidelityText();
    const degraded = summary.degraded || [];
    const ariaLabel = `${stateWord}: ${summary.scene_name}`;
    return html`
      <div class="session ${this.highlighted ? "highlight" : ""}" role="group" aria-label=${ariaLabel}>
        <div class="main">
          <ss-status-glyph tone=${tone} label=""></ss-status-glyph>
          <span class="name" title=${summary.scene_name || ""}>${summary.scene_name}</span>
          ${whereParts.length ? html`<span class="where">${whereParts.join(" · ")}</span>` : ""}
          <span class="state ${summary.state}">${stateWord}</span>
          <span class="spacer"></span>
          <span class="actions-cell">
            <ss-action-menu dense .items=${this.#controlItems()}></ss-action-menu>
          </span>
        </div>
        ${providerText
          ? html`<div class="detail">
              <span
                class="exec"
                title=${`Provider executions: ${providerText}${fidelityText ? ` — fidelity (backend-reported): ${fidelityText}` : ""}`}
              >
                ${providerText}${fidelityText ? ` — ${fidelityText}` : ""}
              </span>
              ${degraded.length
                ? html`
                    <details class="issues">
                      <summary>${degraded.length} issue${degraded.length === 1 ? "" : "s"}</summary>
                      <ul>
                        ${degraded.map(
                          (exec) => html`
                            <li title=${`${exec.fixture_id} — ${exec.detail || exec.fidelity}`}>
                              <span class="iname">${this.#fixtureName(exec.fixture_id)}</span>
                              <span class="idetail">— ${exec.detail || exec.fidelity}</span>
                            </li>
                          `
                        )}
                      </ul>
                    </details>
                  `
                : ""}
            </div>`
          : ""}
        ${summary.state === "orphaned"
          ? html`<div class="note warn">Engine restarted; provider playback may still be running.</div>`
          : ""}
        ${summary.legacy ? html`<div class="note legacy">Session controls require the R5 backend upgrade.</div>` : ""}
      </div>
    `;
  }

  /**
   * Overview cockpit hero (Stage 3+, facelift plan §4.2). Reuses
   * `#controlItems()` (same policy-gated Pause/Resume/Stop, same
   * `playback-action` payload) and `summarizeSession`'s output verbatim —
   * only the layout/type/wash differ from `#renderRow()`.
   */
  #renderHero() {
    const summary = this.summary;
    const tone = sessionTone(summary);
    const stateWord = STATE_WORDS[summary.state] || summary.state;
    const degraded = summary.degraded || [];
    const ariaLabel = `${stateWord}: ${summary.scene_name}`;
    const palette = Array.isArray(this.heroPalette) && this.heroPalette.length ? this.heroPalette : null;
    const wash = palette
      ? `linear-gradient(120deg, ${palette.map((hex) => `${hex}29`).join(", ")})`
      : "none";
    // Hard color stops, not a smooth blend: a plain multi-stop gradient
    // bleeds adjacent hues together (see ss-swatch-band's `band` variant,
    // which fixes the same problem for the Scenes list) and reads as one
    // flat wash instead of the scene's distinct, ordered palette.
    const band =
      palette && palette.length > 1
        ? `linear-gradient(90deg, ${palette
            .map((hex, i) => {
              const start = (i / palette.length) * 100;
              const end = ((i + 1) / palette.length) * 100;
              return `${hex} ${start}%, ${hex} ${end}%`;
            })
            .join(", ")})`
        : palette
          ? palette[0]
          : "var(--ss-border)";
    const eyebrowClass = summary.state === "active" ? "" : summary.state;
    const headline =
      summary.state === "held"
        ? `${summary.scene_name}: held by external light sync`
        : summary.scene_name;
    const fid = summary.fidelity;
    const providerChips = summary.providers.map((p) => {
      const kinds = Object.entries(p.kinds)
        .filter(([, n]) => n > 0)
        .map(([kind, n]) => `${n} ${kind}`)
        .join(" · ");
      return html`<span class="hero-chip ${p.failed > 0 ? "warn" : ""}" title=${kinds}>
        <b>${providerLabel(p.provider)}</b> ${p.ok}/${p.total} ok
      </span>`;
    });
    return html`
      <div
        class="hero"
        role="group"
        aria-label=${ariaLabel}
        style="--hero-wash: ${wash}"
      >
        <div class="hero-inner">
          <div class="hero-content">
            <div class="hero-eyebrow ${eyebrowClass}">
              <span class="hero-live-dot ${summary.state === "active" ? "pulse" : ""}"></span>
              ${stateWord}${tone === "err" ? " · attention" : ""}
              ${summary.target_ids.length
                ? html`<span class="where">· ${summary.target_ids.map(prettyTarget).join(" · ")}</span>`
                : ""}
            </div>
            <div class="hero-title">${headline}</div>
            <div class="hero-actions">
              <ss-action-menu .items=${this.#controlItems()}></ss-action-menu>
              ${summary.fixture_count
                ? html`
                    <details class="issues hero-issues hero-details">
                      <summary class=${degraded.length ? "" : "plain"}>
                        ${degraded.length ? `${degraded.length} issue${degraded.length === 1 ? "" : "s"}` : "Details"}
                      </summary>
                      <div class="detail-body">
                        ${degraded.length
                          ? html`<section>
                              <h4>Issues</h4>
                              <ul>
                                ${degraded.map(
                                  (exec) => html`
                                    <li title=${`${exec.fixture_id} — ${exec.detail || exec.fidelity}`}>
                                      <span class="iname">${this.#fixtureName(exec.fixture_id)}</span>
                                      <span class="idetail">— ${exec.detail || exec.fidelity}</span>
                                    </li>
                                  `
                                )}
                              </ul>
                            </section>`
                          : ""}
                        <section>
                          <h4>Fidelity</h4>
                          <div class="fid-bar" aria-hidden="true">
                            ${FIDELITY_LEVELS.filter((level) => fid[level] > 0).map(
                              (level) => html`<span class=${level} style="flex:${fid[level]}" title=${this.#fidelityTitle(level)}></span>`
                            )}
                          </div>
                          <ul class="fid-list">
                            ${FIDELITY_LEVELS.filter((level) => fid[level] > 0).map(
                              (level) => html`<li title=${this.#fidelityTitle(level)}>
                                <span class="swatch ${level}"></span>${FIDELITY_LABELS[level]}
                                <span class="n">${fid[level]}</span>
                              </li>`
                            )}
                          </ul>
                        </section>
                      </div>
                    </details>
                  `
                : ""}
              <div class="hero-chips">
                ${providerChips.length
                  ? providerChips
                  : html`<span class="hero-chip">No fixture executions reported yet</span>`}
                ${summary.fixture_count
                  ? html`<span class="hero-chip"><b>${summary.fixture_count}</b> fixture${summary.fixture_count === 1 ? "" : "s"}</span>`
                  : ""}
              </div>
            </div>
            ${summary.state === "orphaned"
              ? html`<div class="note warn">Engine restarted; provider playback may still be running.</div>`
              : ""}
            ${summary.state === "held"
              ? html`<div class="note warn">hyperHDR is syncing these fixtures — the session waits (no re-assert). It becomes resumable when the sync surrenders.</div>`
              : ""}
            ${summary.legacy ? html`<div class="note legacy">Session controls require the R5 backend upgrade.</div>` : ""}
          </div>
        </div>
        <div class="hero-band" style="background:${band}" title="Scene palette"></div>
      </div>
    `;
  }
}

customElements.define("ss-playback-session", SsPlaybackSession);
