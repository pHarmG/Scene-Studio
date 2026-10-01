/**
 * <ss-fixture-row> — one dense fixtures-list row (master plan §10.4).
 *
 * Columns: status+name | location/groups | provider binding glyph |
 * capability glyphs. The status glyph and its label live with the name, so
 * no trailing health-text column repeats the same signal. Selection emits a bubbling `select-fixture`
 * CustomEvent; this component never mutates state.
 *
 * @prop {object} fixture fixture JSON with derived `health` + `health_reason`
 * @prop {boolean} selected
 * @prop {boolean} nested true when rendered as a cluster member (product-
 *   polish pass, Task 1): shows a small grouping rail inside the name cell
 *   instead of an outer wrapper indent, so this row's column boundaries
 *   stay pixel-identical to every plain row and to the cluster summary
 *   above it — nesting is a hint inside column 1, never a shift of columns
 *   2-4.
 * @prop {object|null} liveState this fixture's live_state.js FixtureLiveState
 *   JSON (or null before the first sample / for a fixture with no binding) —
 *   drives the right-side radiant state aura (live fixture color-state
 *   pass, plan §6). Purely decorative: never gates row selection/health.
 * @prop {"fresh"|"aging"|"stale"|"unavailable"} [freshness] resolved once
 *   per render pass by the view (state.js's `liveStateFreshness`), not
 *   recomputed per row.
 */
import { LitElement, html, css } from "lit";
import "./ss-status-glyph.js";
import "./ss-capability-indicators.js";
import "./ss-scope-control.js";
import { statusTone } from "../inspector.js";
import { auraBackground } from "./aura.js";

const PROVIDER_SHORT = { hue_v2: "Hue", wled: "WLED", ha_light: "HA" };

export class SsFixtureRow extends LitElement {
  static properties = {
    fixture: { attribute: false },
    selected: { type: Boolean },
    nested: { type: Boolean },
    liveState: { attribute: false },
    freshness: { type: String },
    sampledAt: { type: String },
  };

  static styles = css`
    :host {
      display: block;
      container-type: inline-size;
      container-name: ss-fixture-row;
    }
    .row {
      position: relative;
      display: grid;
      grid-template-columns: var(--ss-fixture-cols);
      gap: var(--ss-gutter);
      align-items: center;
      min-height: var(--ss-row-h);
      padding: 4px 10px;
      border-bottom: 1px solid var(--ss-border-soft);
      cursor: pointer;
    }
    .row:hover {
      background: var(--ss-surface-2);
    }
    .row.selected {
      background: var(--ss-surface-2);
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .row.disabled .name .text {
      color: var(--ss-text-dim);
    }
    /* Right-side radiant state aura (plan §6): an absolutely-positioned
       layer behind the row's real content, never part of grid flow, never
       intercepting clicks. z-index math: grid items compare z-index even
       with position:static, so giving every OTHER child z-index:1 is
       enough to keep them painted above this z-index:0 layer — see
       aura.js for the actual gradient math. */
    .aura {
      position: absolute;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      border-radius: inherit;
      transition: opacity 220ms ease;
    }
    @media (prefers-reduced-motion: reduce) {
      .aura {
        transition: none;
      }
    }
    .row > *:not(.aura) {
      position: relative;
      z-index: 1;
    }
    .name {
      display: flex;
      align-items: center;
      gap: 8px;
      overflow: hidden;
    }
    .name .text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }
    /* External light-sync hold badge (hyperHDR pass): a fixed semantic
       signal — the fixture is currently owned by an external sync stream
       and scene applies will yield it until the stream surrenders. */
    .sync-held {
      flex: none;
      font-size: 11px;
      letter-spacing: 0.04em;
      padding: 1px 6px;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, var(--ss-warn, #e6a23c) 55%, transparent);
      color: var(--ss-warn, #e6a23c);
      background: color-mix(in srgb, var(--ss-warn, #e6a23c) 12%, transparent);
      cursor: help;
    }
    /* Cluster-member grouping cue (Task 1): a short rail inside the name
       cell's own flex content — it never touches the grid track edges, so
       columns 2-4 line up exactly with unclustered rows and the cluster
       summary. */
    .rail {
      width: 2px;
      height: 16px;
      border-radius: 1px;
      background: var(--ss-border);
      flex: none;
    }
    .dim {
      color: var(--ss-text-dim);
      font-size: 14px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .provider {
      font-size: 13px;
      color: var(--ss-text-dim);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface-2);
      padding: 0 5px;
      text-align: center;
    }
    ss-capability-indicators {
      justify-self: end;
    }
    @container ss-fixture-row (max-width: 560px) {
      .row {
        grid-template-columns: var(--ss-fixture-cols-compact);
      }
      ss-scope-control,
      ss-capability-indicators {
        display: none;
      }
    }
  `;

  constructor() {
    super();
    this.fixture = {};
    this.selected = false;
    this.nested = false;
    this.liveState = null;
    this.freshness = "unavailable";
    this.sampledAt = null;
  }

  #onClick() {
    this.dispatchEvent(
      new CustomEvent("select-fixture", { detail: { id: this.fixture.id }, bubbles: true, composed: true })
    );
  }

  render() {
    const f = this.fixture;
    const tone = statusTone(f.health);
    const disabled = f.enabled === false;
    const live = this.liveState || {};
    const aura = auraBackground({
      enabled: !disabled,
      available: live.available,
      on: live.on,
      brightness: live.brightness,
      displayColors: live.display_colors,
      freshness: this.freshness,
    });
    return html`
      <div
        class="row ${this.selected ? "selected" : ""} ${disabled ? "disabled" : ""}"
        role="button"
        tabindex="0"
        @click=${this.#onClick}
        @keydown=${(e) => (e.key === "Enter" || e.key === " ") && this.#onClick()}
        aria-pressed=${this.selected ? "true" : "false"}
      >
        <div class="aura" style=${aura ? `background:${aura};opacity:1` : "opacity:0"} aria-hidden="true"></div>
        <div class="name">
          ${this.nested ? html`<span class="rail" aria-hidden="true"></span>` : ""}
          <ss-status-glyph tone=${tone} label=${f.health === "ready" ? "" : f.health}></ss-status-glyph>
          <span class="text">${f.name}</span>
          ${f.contention && f.contention.held
            ? html`<span class="sync-held" title=${`Held by external light sync — ${f.contention.owner || "hyperHDR"}. Scene applies yield this fixture until the sync surrenders.`}>SYNC</span>`
            : ""}
        </div>
        <ss-scope-control .groups=${f.groups || []}></ss-scope-control>
        <div>
          <span class="provider">${f.binding ? PROVIDER_SHORT[f.binding.provider] || f.binding.provider : "—"}</span>
        </div>
        <ss-capability-indicators
          .capabilities=${f.capabilities}
          .disabled=${disabled}
          .liveState=${this.liveState}
          .freshness=${this.freshness}
          .sampledAt=${this.sampledAt}
        ></ss-capability-indicators>
      </div>
    `;
  }
}

customElements.define("ss-fixture-row", SsFixtureRow);
