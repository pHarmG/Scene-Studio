/**
 * <ss-fixture-cluster> — a condensed "one physical controller, many
 * segments" presentation (post-R5 Workbench visual polish, completing the
 * WLED item deferred from Stage 3+ — see
 * docs/scene_studio/WORKBENCH_STAGE3_DESIGN_HANDOFF.md).
 *
 * WLED segments are the driving case (confirmed on the live Workbench: all
 * `wled_seg_*` fixtures share one `fixture.binding.device_id`, the physical
 * controller's MAC, distinguished only by `binding.segment_ids`), but this
 * component is provider-agnostic — it clusters whatever fixture list its
 * caller hands it under one summary row with a per-segment disclosure.
 *
 * This is a PRESENTATION-ONLY grouping: every segment renders as the exact
 * same <ss-fixture-row> used everywhere else, with its real fixture id,
 * its own health/capabilities, and the same bubbling `select-fixture`
 * event — nothing here touches the fixture model, discovery, or binding
 * data. Collapsing the cluster hides nothing that differs: it starts
 * expanded whenever any segment's health differs from the rest (a mixed
 * cluster is itself a "needs a look" signal), and only collapses by
 * default when every segment reports the identical health.
 *
 * @prop {string} label cluster name, e.g. "WLED Segment 0–5"
 * @prop {string} providerLabel short provider badge text, e.g. "WLED"
 * @prop {object[]} fixtures the member fixtures (each a full fixture doc)
 * @prop {string|null} selectedId currently-selected fixture id, if any
 * @prop {object} liveStateById live_state.js FixtureLiveState by fixture id
 *   (live fixture color-state pass, plan §9) — passed straight through to
 *   each member <ss-fixture-row>. The collapsed summary derives its OWN
 *   aggregate aura from these same already-loaded observations; it never
 *   triggers another provider read.
 * @prop {string} [freshness] resolved once per render by the view, same as
 *   <ss-fixture-row>
 * @fires select-fixture bubbling, `{id}` — identical to <ss-fixture-row>
 */
import { LitElement, html, css } from "lit";
import "./ss-fixture-row.js";
import "./ss-status-glyph.js";
import "./ss-capability-indicators.js";
import { iconChevronRight } from "./icons.js";
import { statusTone } from "../inspector.js";
import { auraBackground } from "./aura.js";

const HEALTH_ORDER = ["missing", "conflicting", "degraded", "disabled", "unbound", "ready"];

/** Worst-first health, matching the same severity convention as statusTone. */
function worstHealth(fixtures) {
  let worst = "ready";
  for (const f of fixtures) {
    if (HEALTH_ORDER.indexOf(f.health) < HEALTH_ORDER.indexOf(worst)) worst = f.health;
  }
  return worst;
}

function healthSummary(fixtures) {
  const counts = new Map();
  for (const f of fixtures) counts.set(f.health, (counts.get(f.health) || 0) + 1);
  if (counts.size === 1 && counts.has("ready")) return `${fixtures.length} ready`;
  return [...counts.entries()]
    .sort((a, b) => HEALTH_ORDER.indexOf(a[0]) - HEALTH_ORDER.indexOf(b[0]))
    .map(([health, n]) => `${n} ${health}`)
    .join(" · ");
}

/**
 * Collapsed-cluster aggregate aura input: up to 3 REPRESENTATIVE distinct
 * child colors (never an average of every segment — that produces one
 * muddy fake color, explicitly ruled out by the plan), at the brightest
 * active member's brightness. No aura at all when every member is
 * off/unavailable/stale.
 * @param {object[]} fixtures member fixture docs (for `.enabled`)
 * @param {object} liveStateById
 * @param {string} freshness
 * @returns {string|null} CSS `background`, or null
 */
export function clusterAuraBackground(fixtures, liveStateById, freshness) {
  const activeColors = [];
  let maxBrightness = null;
  for (const fixture of fixtures || []) {
    if (fixture.enabled === false) continue;
    const live = (liveStateById || {})[fixture.id];
    if (!live || live.available === false || live.on !== true) continue;
    const primary = (live.display_colors || [])[0];
    if (primary && !activeColors.includes(primary)) activeColors.push(primary);
    if (typeof live.brightness === "number") {
      maxBrightness = maxBrightness === null ? live.brightness : Math.max(maxBrightness, live.brightness);
    }
  }
  if (!activeColors.length) return null;
  return auraBackground({
    enabled: true,
    available: true,
    on: true,
    brightness: maxBrightness,
    displayColors: activeColors.slice(0, 3),
    freshness,
  });
}

export class SsFixtureCluster extends LitElement {
  static properties = {
    label: { type: String },
    providerLabel: { type: String },
    fixtures: { attribute: false },
    selectedId: { type: String },
    liveStateById: { attribute: false },
    freshness: { type: String },
    sampledAt: { type: String },
  };

  static styles = css`
    :host {
      display: block;
      container-type: inline-size;
      container-name: ss-fixture-row;
      margin-bottom: 1px;
    }
    details {
      background: var(--ss-surface);
    }
    /* Same column grammar as <ss-fixture-row> (Task 1): one shared grid
       template, so the summary's name/groups/provider/caps line up exactly
       with every plain row, above and inside the disclosure. The health
       rollup that used to be its own 5th column now rides inside the name
       cell as a compact tag, the same pattern <ss-scene-row> already uses
       for its runtime tag. */
    summary {
      position: relative;
      display: grid;
      grid-template-columns: var(--ss-fixture-cols);
      gap: var(--ss-gutter);
      align-items: center;
      min-height: var(--ss-row-h);
      padding: 4px 10px;
      border-bottom: 1px solid var(--ss-border-soft);
      cursor: pointer;
      list-style: none;
      font-size: 15px;
    }
    /* Same aura layer/stacking approach as <ss-fixture-row> (plan §9: the
       collapsed cluster gets its own aggregate glow from already-loaded
       child observations, never a new provider read). */
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
    summary > *:not(.aura) {
      position: relative;
      z-index: 1;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    summary:hover {
      background: var(--ss-surface-2);
    }
    .name {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      overflow: hidden;
    }
    .name .text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .chev {
      display: inline-flex;
      color: var(--ss-text-faint);
      flex: none;
      transition: transform 120ms ease;
    }
    details[open] .chev {
      transform: rotate(90deg);
    }
    /* Was missing entirely — the "N segments" text inherited the summary's
       bright 15px instead of the muted secondary-text style every plain
       <ss-fixture-row> uses for its own groups column. Mirrors that rule
       exactly (shadow DOM styles don't cross component boundaries, so this
       has to be declared here too, not just in ss-fixture-row.js). */
    .dim {
      color: var(--ss-text-dim);
      font-size: 14px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    /* Plain text, not a pill — this is catalog health (the same kind of
       information a plain <ss-fixture-row> shows via its status glyph's
       own text label), not a transient runtime/session state, so it reads
       at the SAME size as that label (ss-status-glyph's .label is 14px)
       instead of the smaller bordered-pill treatment scene rows use for
       actual live-playback state. Severity still reads through color,
       matching <ss-scene-row>'s plain-text readiness breakdown. */
    .health-tag {
      font-size: 14px;
      color: var(--ss-text-dim);
      flex: none;
      white-space: nowrap;
    }
    .health-tag.warn {
      color: var(--ss-warn);
    }
    .health-tag.err {
      color: var(--ss-err);
    }
    .provider {
      font-size: 13px;
      color: var(--ss-text-dim);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface-2);
      padding: 0 5px;
      text-align: center;
      width: fit-content;
    }
    /* Members render as identical <ss-fixture-row>s with no wrapper indent
       — an outer padding-left used to shift columns 2-4 out of alignment
       with the summary and with every ungrouped row in the same list
       (Task 1). Nesting is now a rail inside each row's own name cell. */
    .members {
      border-bottom: 1px solid var(--ss-border-soft);
    }
    ss-capability-indicators {
      justify-self: end;
    }
    @container ss-fixture-row (max-width: 560px) {
      summary {
        grid-template-columns: var(--ss-fixture-cols-compact);
      }
      .dim,
      ss-capability-indicators {
        display: none;
      }
    }
  `;

  constructor() {
    super();
    this.label = "";
    this.providerLabel = "";
    this.fixtures = [];
    this.selectedId = null;
    this.liveStateById = {};
    this.freshness = "unavailable";
    this.sampledAt = null;
  }

  render() {
    const fixtures = this.fixtures || [];
    const worst = worstHealth(fixtures);
    const tone = statusTone(worst);
    const mixed = new Set(fixtures.map((f) => f.health)).size > 1;
    const containsSelected = this.selectedId && fixtures.some((f) => f.id === this.selectedId);
    const allDisabled = fixtures.length > 0 && fixtures.every((f) => f.enabled === false);
    const tagClass = tone === "warn" || tone === "err" ? tone : "";
    const aura = clusterAuraBackground(fixtures, this.liveStateById, this.freshness);
    return html`
      <details ?open=${mixed || containsSelected}>
        <summary>
          <div class="aura" style=${aura ? `background:${aura};opacity:1` : "opacity:0"} aria-hidden="true"></div>
          <div class="name">
            <span class="chev">${iconChevronRight(14)}</span>
            <ss-status-glyph tone=${tone} label=""></ss-status-glyph>
            <span class="text">${this.label}</span>
            <span class="health-tag ${tagClass}">${healthSummary(fixtures)}</span>
          </div>
          <div class="dim groups">${fixtures.length} segments</div>
          <span class="provider">${this.providerLabel}</span>
          <ss-capability-indicators
            .capabilities=${(fixtures[0] && fixtures[0].capabilities) || null}
            .disabled=${allDisabled}
          ></ss-capability-indicators>
        </summary>
        <div class="members">
          ${fixtures.map(
            (f) => html`
              <ss-fixture-row
                role="listitem"
                nested
                .fixture=${f}
                .selected=${this.selectedId === f.id}
                .liveState=${(this.liveStateById || {})[f.id] || null}
                .freshness=${this.freshness}
                .sampledAt=${this.sampledAt}
              ></ss-fixture-row>
            `
          )}
        </div>
      </details>
    `;
  }
}

customElements.define("ss-fixture-cluster", SsFixtureCluster);
