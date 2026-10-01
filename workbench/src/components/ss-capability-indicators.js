/**
 * <ss-capability-indicators> — truthful, at-a-glance capability glyphs for a
 * fixture row (product-polish pass, Task 3; tap/click descriptions added
 * 2026-09-16 per user feedback — hover-only `title` tooltips never reach
 * touch/mobile users, and violate the app's own "not dependent on hover
 * alone" accessibility guidance).
 *
 * Replaces the old per-row `#caps()` markup that Fixtures rows and clusters
 * each hand-rolled (border-color-only active/inactive, indistinguishable at
 * a glance). Every glyph here is driven only by the fixture's real
 * `capabilities` — nothing is inferred, and an unsupported capability never
 * renders with the "supported" treatment. Support is carried on more than
 * color alone (filled chip + solid border vs. empty chip + dashed border +
 * dimmer glyph), so the signal survives reduced-contrast/colorblind
 * viewing. `on_off`/`brightness` are omitted deliberately: every fixture in
 * the fleet has them, so a dedicated glyph for either would be decorative,
 * not informative — see AGENTS.md "not a noisy dashboard" guidance. Power
 * state truthfully means "administratively enabled", already carried by
 * the row's <ss-status-glyph>; this component does not duplicate it.
 *
 * When `disabled` is true (fixture.enabled === false) the whole group is
 * rendered as a single dimmed, uniform "capabilities unavailable" state —
 * capability booleans stay physically true, but Scene Studio cannot drive
 * a disabled fixture, so showing them as actively usable would mislead.
 *
 * Each glyph is a real `<button>`: tapping/clicking (or Enter/Space while
 * focused) opens a small description popover naming the capability, its
 * supported/not-supported state, and any detail (mirek range, gradient
 * points, native effect count) — the exact same text the `title` attribute
 * already carries for a mouse-hover user, just reachable without a mouse.
 * Built on the native Popover API (top-layer, so it is never clipped by
 * the row/list ancestors' `overflow: hidden`) — same technique already
 * used by <ss-overflow-menu>; position is computed from the tapped
 * button's own rect in `beforetoggle`, not CSS anchoring.
 *
 * Extended (live fixture color-state pass, plan §7) with the CURRENT
 * observed value alongside the static capability, where the observation
 * actually supports it — e.g. "Current: #ff7a45, observed 2s ago" under
 * Color, or a note that WLED's dynamic colors are configured, not
 * per-frame-sampled, under the dynamic/effects glyph. Never adds a
 * current-value line the observation doesn't truthfully support.
 *
 * @prop {object|null} capabilities fixture.capabilities
 * @prop {boolean} disabled true when the fixture itself is administratively disabled
 * @prop {number} size icon pixel size (default 15)
 * @prop {object|null} [liveState] this fixture's live_state.js FixtureLiveState JSON
 * @prop {"fresh"|"aging"|"stale"|"unavailable"} [freshness]
 */
import { LitElement, html, css } from "lit";
import { iconDroplet, iconLayers, iconThermometer, iconWaveform } from "./icons.js";

let seq = 0;

/** "3s ago" / "just now" — small, deliberately coarse (not a live-ticking
 *  clock; this popover is opened on demand, not continuously rendered). */
function relativeAge(sampledAt) {
  if (!sampledAt) return null;
  const ms = Date.now() - Date.parse(sampledAt);
  if (Number.isNaN(ms) || ms < 0) return null;
  if (ms < 1000) return "just now";
  return `${Math.round(ms / 1000)}s ago`;
}

export class SsCapabilityIndicators extends LitElement {
  static properties = {
    capabilities: { attribute: false },
    disabled: { type: Boolean },
    size: { type: Number },
    liveState: { attribute: false },
    freshness: { type: String },
    sampledAt: { type: String },
  };

  static styles = css`
    :host {
      display: inline-flex;
    }
    .group {
      display: flex;
      gap: 4px;
      flex-wrap: nowrap;
    }
    .cap {
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: var(--ss-cap-btn);
      height: var(--ss-cap-btn);
      padding: 0;
      border-radius: 6px;
      flex: none;
      border: 1px dashed var(--ss-border-soft);
      background: transparent;
      color: var(--ss-text-faint);
      opacity: 0.55;
      font: inherit;
      cursor: pointer;
    }
    .cap.on {
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text-dim);
      opacity: 1;
    }
    .group.disabled .cap {
      border-style: dashed;
      border-color: var(--ss-border-soft);
      background: transparent;
      color: var(--ss-text-faint);
      opacity: 0.35;
    }
    .cap:hover {
      opacity: 1;
    }
    .cap:focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 1px;
    }
    .tip {
      margin: 0;
      padding: 8px 10px;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface-2);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      max-width: 220px;
      font-size: 13px;
      line-height: 1.4;
      color: var(--ss-text-dim);
    }
    .tip:popover-open {
      display: grid;
      gap: 2px;
    }
    .tip strong {
      color: var(--ss-text);
      font-size: 13px;
    }
    .tip .state {
      font-weight: 600;
    }
    .tip .state.on {
      color: var(--ss-ok);
    }
    .tip .state.off {
      color: var(--ss-text-faint);
      font-weight: 400;
    }
    .tip .current {
      display: block;
      margin-top: 4px;
      padding-top: 4px;
      border-top: 1px solid var(--ss-border-soft);
      color: var(--ss-text-faint);
      font-weight: 400;
    }
  `;

  constructor() {
    super();
    this.capabilities = null;
    this.disabled = false;
    this.size = 15;
    this.liveState = null;
    this.freshness = "unavailable";
    this.sampledAt = null;
    this._uid = `ss-cap-${++seq}`;
  }

  /** Current-value line for one capability, or "" when the observation
   *  doesn't actually support that capability right now (plan §7: "only
   *  add current state to a capability whose observation actually supports
   *  it" — never fabricated from what's merely capable). */
  #current(key) {
    const live = this.liveState;
    if (this.disabled || !live || live.available === false) return "";
    const stale = this.freshness === "stale" || this.freshness === "unavailable";
    const age = relativeAge(this.sampledAt) || (stale ? "stale" : null);
    if (key === "color" && (live.color_mode === "rgb" || live.color_mode === "gradient") && live.display_colors?.length) {
      const colors = live.display_colors.join(", ");
      return `Current: ${colors}${age ? ` — observed ${age}` : ""}`;
    }
    if (key === "temp" && live.color_mode === "cct" && live.color_temp_kelvin) {
      return `Current: ${live.color_temp_kelvin} K${age ? ` — observed ${age}` : ""}`;
    }
    if (key === "dynamic" && live.dynamic) {
      return live.state_kind === "configured_dynamic"
        ? "Active: configured effect colors shown in the row's aura; this provider does not report per-frame LED color here."
        : `Active${age ? ` — observed ${age}` : ""}.`;
    }
    return "";
  }

  #defs() {
    const c = this.capabilities || {};
    const mirek = c.color_temp
      ? ` (${c.color_temp.mirek_min}–${c.color_temp.mirek_max} mirek)`
      : "";
    const gradient = c.gradient ? ` (${c.gradient.max_points}-point gradient)` : "";
    const effectCount = Array.isArray(c.effects) ? c.effects.length : 0;
    const dynamicDetail = c.dynamic_native
      ? effectCount
        ? ` (${effectCount} native effect${effectCount === 1 ? "" : "s"})`
        : " (native)"
      : "";
    return [
      { key: "color", name: "RGB color", icon: iconDroplet(this.size), on: !!c.color_xy, detail: "", current: this.#current("color") },
      { key: "temp", name: "Color temperature", icon: iconThermometer(this.size), on: !!c.color_temp, detail: mirek, current: this.#current("temp") },
      { key: "gradient", name: "Gradient", icon: iconLayers(this.size), on: !!c.gradient, detail: gradient, current: "" },
      { key: "dynamic", name: "Provider-native dynamic/effects", icon: iconWaveform(this.size), on: !!c.dynamic_native, detail: dynamicDetail, current: this.#current("dynamic") },
    ];
  }

  #onBeforeToggle(e) {
    if (e.newState !== "open") return;
    const popover = e.target;
    const trigger = popover.previousElementSibling;
    if (!trigger) return;
    const r = trigger.getBoundingClientRect();
    const maxLeft = Math.max(4, window.innerWidth - 232);
    popover.style.position = "fixed";
    popover.style.top = `${r.bottom + 6}px`;
    popover.style.left = `${Math.max(4, Math.min(r.left, maxLeft))}px`;
  }

  render() {
    const defs = this.#defs();
    return html`
      <span class="group ${this.disabled ? "disabled" : ""}" role="group" aria-label="Capabilities">
        ${defs.map((d, i) => {
          const active = d.on && !this.disabled;
          const stateText = this.disabled ? "Unavailable — fixture disabled" : d.on ? "Supported" : "Not supported";
          const title = this.disabled
            ? `${d.name}: unavailable while this fixture is disabled`
            : `${d.name}${d.on ? " supported" : " not supported"}${d.detail}`;
          const popId = `${this._uid}-${i}`;
          return html`
            <button
              type="button"
              class="cap ${active ? "on" : ""}"
              popovertarget=${popId}
              title=${title}
              aria-label=${title}
              @click=${(e) => e.stopPropagation()}
            >
              ${d.icon}
            </button>
            <div id=${popId} popover class="tip" @beforetoggle=${this.#onBeforeToggle} role="status">
              <strong>${d.name}</strong>
              <span class="state ${active ? "on" : "off"}">${stateText}${!this.disabled ? d.detail : ""}</span>
              ${d.current ? html`<span class="current">${d.current}</span>` : ""}
            </div>
          `;
        })}
      </span>
    `;
  }
}

customElements.define("ss-capability-indicators", SsCapabilityIndicators);
