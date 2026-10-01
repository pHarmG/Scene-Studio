/**
 * <ss-view-fixtures> — dense fixture list with capability legend (§10.4).
 * Row selection updates the shell inspector; no downward expansion.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-fixture-row.js";
import "../components/ss-fixture-cluster.js";
import "../components/ss-panel.js";
import "../components/ss-empty-state.js";
import { iconChevronRight } from "../components/icons.js";
import { liveStateFreshness } from "../state.js";
import { clusterLabel, groupByRoom, roomLabel, withControllerClusters } from "../grouping.js";

const PROVIDER_SHORT = { hue_v2: "Hue", wled: "WLED", ha_light: "HA" };

export class SsViewFixtures extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-fixtures) {
    :scope {
      display: block;
    }
    .legend {
      color: var(--ss-text-faint);
      font-size: 14px;
      font-family: var(--ss-font-mono);
      margin: 0 0 6px 8px;
    }
    .group {
      margin-bottom: 10px;
    }
    .group > summary {
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 9px 10px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      font-size: 15px;
      font-weight: 700;
      color: var(--ss-text-dim);
    }
    .group > summary::-webkit-details-marker {
      display: none;
    }
    .group > summary .count {
      font-weight: 500;
      color: var(--ss-text-faint);
    }
    .group .chev {
      display: inline-flex;
      transition: transform 120ms ease;
    }
    .group[open] .chev {
      transform: rotate(90deg);
    }
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._unsub = null;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.store) this._unsub = this.store.subscribe(() => this.requestUpdate());
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
    super.disconnectedCallback();
  }

  #onSelect(e) {
    this.store.select({ type: "fixture", id: e.detail.id });
  }

  render() {
    const s = this.store.state;
    if (!s.fixtures) return html`<ss-empty-state>Loading…</ss-empty-state>`;
    const sel = s.selection;
    const groups = groupByRoom(s.fixtures.fixtures);
    const live = s.fixtureLiveState || { byId: {}, sampledAt: null };
    const freshness = liveStateFreshness(live.sampledAt);
    return html`
      <div class="legend">
        Capabilities (filled = supported, dashed = not): droplet RGB · thermometer temp ·
        layers gradient · waveform dynamic/effects. Disabled fixtures show all glyphs
        dimmed — not driven while disabled, whatever they support.
      </div>
      ${groups.map(
        (g) => html`
          <details class="group" open>
            <summary>
              <span class="chev">${iconChevronRight(14)}</span>
              ${roomLabel(g.room)} <span class="count">· ${g.fixtures.length} fixture${g.fixtures.length === 1 ? "" : "s"}</span>
            </summary>
            <ss-panel variant="list" role="list">
              ${withControllerClusters(g.fixtures).map((item) =>
                item.type === "cluster"
                  ? html`
                      <ss-fixture-cluster
                        .label=${clusterLabel(item.fixtures)}
                        .providerLabel=${PROVIDER_SHORT[item.provider] || item.provider}
                        .fixtures=${item.fixtures}
                        .selectedId=${sel && sel.type === "fixture" ? sel.id : null}
                        .liveStateById=${live.byId}
                        .freshness=${freshness}
                        .sampledAt=${live.sampledAt}
                        @select-fixture=${this.#onSelect}
                      ></ss-fixture-cluster>
                    `
                  : html`
                      <ss-fixture-row
                        role="listitem"
                        .fixture=${item.fixture}
                        .selected=${sel && sel.type === "fixture" && sel.id === item.fixture.id}
                        .liveState=${live.byId[item.fixture.id] || null}
                        .freshness=${freshness}
                        .sampledAt=${live.sampledAt}
                        @select-fixture=${this.#onSelect}
                      ></ss-fixture-row>
                    `
              )}
            </ss-panel>
          </details>
        `
      )}
    `;
  }
}

customElements.define("ss-view-fixtures", SsViewFixtures);
