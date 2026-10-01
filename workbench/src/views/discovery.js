/**
 * <ss-view-discovery> — summary counts first, exception-focused list,
 * explicit actions (§10.5). Discovery never mutates bindings; bind/rebind is
 * always an explicit command, "leave unbound" is a session-local view choice.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-status-glyph.js";
import "../components/ss-panel.js";
import { iconChevronRight, iconSearch } from "../components/icons.js";
import { statusTone, entryKey } from "../inspector.js";

const FIXTURE_NAMES_FALLBACK = {};

/**
 * Stage 3+ (facelift plan §4.5): "Entries group into labeled sections
 * mirroring the actual decision types... worst-first" instead of one flat
 * list. Pure regrouping of the SAME `#exceptions()` array by
 * `entry.status` — no new data shape, same per-entry actions.
 */
const BUCKETS = [
  { id: "reconcile", label: "Registry updates", statuses: ["bound_reconcile_available"], tone: "idle" },
  { id: "review", label: "Candidates to review", statuses: ["candidate_replacement", "bound_degraded"], tone: "warn" },
  { id: "missing", label: "Missing to resolve", statuses: ["missing", "conflict"], tone: "err" },
  { id: "unbound", label: "New / unbound resources", statuses: ["available_unbound"], tone: "idle" },
  { id: "disabled", label: "Disabled", statuses: ["disabled"], tone: "idle" },
];

function bucketFor(status) {
  return BUCKETS.find((b) => b.statuses.includes(status)) || BUCKETS[BUCKETS.length - 1];
}

export class SsViewDiscovery extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-discovery) {
    :scope {
      display: block;
    }
    .bar {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      background: var(--ss-surface);
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
      padding: 10px 12px;
      margin-bottom: 10px;
      font-size: 15px;
    }
    .counts {
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
    }
    .count b {
      font-weight: 600;
    }
    .count.ok b { color: var(--ss-ok); }
    .count.warn b { color: var(--ss-warn); }
    .count.err b { color: var(--ss-err); }
    .count.idle b { color: var(--ss-idle); }
    .muted {
      color: var(--ss-text-faint);
    }
    .run {
      margin-left: auto;
    }
    .entry {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: var(--ss-row-h);
      padding: 4px 10px;
      border-top: 1px solid var(--ss-border-soft);
      font-size: 15px;
      cursor: pointer;
    }
    .entry:first-child {
      border-top: none;
    }
    .entry:hover {
      background: var(--ss-surface-2);
    }
    .entry.selected {
      background: var(--ss-surface-2);
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .name {
      min-width: 150px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .status {
      width: 150px;
      flex: none;
      font-family: var(--ss-font-mono);
      font-size: 12px;
      color: var(--ss-text-dim);
    }
    .detail {
      flex: 1;
      min-width: 0;
      color: var(--ss-text-faint);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .entry.warn .detail { color: var(--ss-warn); }
    .entry.err .detail { color: var(--ss-err); }
    .actions {
      display: flex;
      gap: 6px;
      flex: none;
    }
    .actions button {
      appearance: none;
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text-dim);
      font-size: 14px;
      border-radius: var(--ss-radius-sm);
      padding: 5px 12px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      cursor: pointer;
      white-space: nowrap;
    }
    .actions button:hover {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    .empty {
      color: var(--ss-text-faint);
      font-size: 15px;
      padding: 10px;
    }
    /* Discovery never got a narrow-width treatment when the row was first
       built (unlike ss-scene-row/ss-fixture-row's existing 900px rule) —
       the fixed 150px name + 150px status + flex detail + action buttons
       has no room to fit under ~500px and overflows the row horizontally.
       Stack the row instead: name+status+dot on the first line, the
       (now-wrapping, not truncated) detail text on its own line, actions
       on a third. */
    @media (max-width: 640px) {
      .entry {
        flex-wrap: wrap;
        row-gap: 4px;
        padding: 8px;
      }
      .name {
        min-width: 0;
        flex: 1 1 auto;
      }
      .status {
        width: auto;
        flex: none;
      }
      .detail {
        flex-basis: 100%;
        white-space: normal;
        overflow: visible;
        text-overflow: clip;
      }
      .actions {
        flex-basis: 100%;
        justify-content: flex-start;
        flex-wrap: wrap;
      }
    }
    .bucket {
      margin-bottom: 10px;
    }
    .bucket > summary {
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
    .bucket > summary::-webkit-details-marker {
      display: none;
    }
    .bucket .chev {
      display: inline-flex;
      transition: transform 120ms ease;
    }
    .bucket[open] .chev {
      transform: rotate(90deg);
    }
    .bucket-count {
      font-weight: 700;
      border-radius: 999px;
      padding: 1px 8px;
      font-size: 14px;
    }
    .bucket-count.err {
      background: var(--ss-err-dim);
      color: var(--ss-err);
    }
    .bucket-count.warn {
      background: var(--ss-warn-dim);
      color: var(--ss-warn);
    }
    .bucket-count.idle {
      background: var(--ss-idle-dim);
      color: var(--ss-idle);
    }
    .entry {
      border-left: 3px solid transparent;
    }
    .entry.err {
      border-left-color: var(--ss-err);
    }
    .entry.warn {
      border-left-color: var(--ss-warn);
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

  #fixtureName(id) {
    const s = this.store.state;
    const f = (s.fixtures ? s.fixtures.fixtures : []).find((x) => x.id === id);
    return f ? f.name : id;
  }

  #obsName(observationId) {
    const s = this.store.state;
    const obs = (s.discovery ? s.discovery.observations : []).find(
      (o) => `${o.provider}:${o.provider_resource_id}` === observationId
    );
    return obs ? obs.name || observationId : observationId || "";
  }

  #exceptions() {
    const s = this.store.state;
    const report = s.discovery;
    if (!report) return [];
    return report.entries
      .map((entry, i) => ({ entry, key: entryKey(entry, i) }))
      .filter(({ entry, key }) => entry.status !== "bound_ready" && !s.dismissed[key]);
  }

  #onSelect(key) {
    this.store.select({ type: "entry", id: key });
  }

  #action(e, { action, fixture_id, observation_id, key }) {
    e.stopPropagation();
    const store = this.store;
    switch (action) {
      case "rebind":
        return store.sendCommand({ command: "fixture.rebind_preview", fixture_id, observation_id }).then((result) => {
          if (result.ok) {
            this._rebindReviews = this._rebindReviews || new Map();
            this._rebindReviews.set(`${fixture_id}:${observation_id}`, result.data);
            this.requestUpdate();
          }
          return result;
        });
      case "reconcile":
        return store.sendCommand({ command: "fixture.reconcile_preview", fixture_id, observation_id }).then((result) => {
          if (result.ok) {
            this._reconcileReviews = this._reconcileReviews || new Map();
            this._reconcileReviews.set(`${fixture_id}:${observation_id}`, result.data);
            this.requestUpdate();
          }
          return result;
        });
      case "applyRebind":
        return store.sendCommand({ command: "fixture.rebind", fixture_id, observation_id });
      case "applyReconcile":
        return store.sendCommand({ command: "fixture.reconcile", fixture_id, observation_id });
      case "disable":
        return store.sendCommand({ command: "fixture.disable", fixture_id });
      case "retry":
        return store.sendCommand({ command: "fixture.retry", fixture_id });
      case "leave":
        return store.dismissDiscoveryKey(key);
      default:
        return Promise.resolve();
    }
  }

  render() {
    const s = this.store.state;
    const report = s.discovery;
    if (!report) {
      // Live engines start with no discovery report on record (a null report
      // is the documented empty state, not an error) — distinguish that from
      // the initial catalog load.
      return s.status
        ? html`<div class="empty">No discovery report yet — run discovery to observe providers.</div>`
        : html`<div class="muted">Loading…</div>`;
    }
    const sum = report.summary;
    const exceptions = this.#exceptions();
    const sel = s.selection;

    return html`
      <div class="bar">
        <span class="counts">
          <span class="count ok"><b>${sum.fixtures_bound_ready}</b> unchanged</span>
          <span class="count idle"><b>${sum.fixtures_reconcile_available || 0}</b> registry updates</span>
          <span class="count idle"><b>${sum.unbound_observations}</b> new/unbound</span>
          <span class="count err"><b>${sum.fixtures_missing}</b> missing</span>
          <span class="count warn"><b>${sum.candidate_replacements}</b> candidates</span>
          <span class="count err"><b>${sum.conflicts}</b> conflicts</span>
          <span class="count idle"><b>${sum.fixtures_disabled}</b> disabled</span>
        </span>
        <span class="muted">run ${report.run_id}</span>
        <button class="ss-btn run" @click=${() => this.store.sendCommand({ command: "discovery.run" })}>
          ${iconSearch(14)} Run discovery
        </button>
      </div>
      ${exceptions.length
        ? BUCKETS.map((bucket) => {
            const rows = exceptions.filter(({ entry }) => bucketFor(entry.status) === bucket);
            if (!rows.length) return "";
            return html`
              <details class="bucket" open>
                <summary>
                  <span class="chev">${iconChevronRight(14)}</span>
                  ${bucket.label} <span class="bucket-count ${bucket.tone}">${rows.length}</span>
                </summary>
                <ss-panel variant="list">
                  ${rows.map(({ entry, key }) => this.#renderEntry(entry, key, sel))}
                </ss-panel>
              </details>
            `;
          })
        : html`<div class="empty">No exceptions — every bound fixture is unchanged and observed.</div>`}
    `;
  }

  #renderEntry(entry, key, sel) {
    const tone = statusTone(entry.status);
    const name = entry.fixture_id
      ? this.#fixtureName(entry.fixture_id)
      : this.#obsName(entry.observation_id);
    const candidateObsId = entry.candidate_ids && entry.candidate_ids[0]
      ? entry.candidate_ids[0].replace(/^[^:]+:/, "")
      : null;
    const review = this._rebindReviews && candidateObsId ? this._rebindReviews.get(`${entry.fixture_id}:${candidateObsId}`) : null;
    const reconcileReview = this._reconcileReviews && entry.observation_id
      ? this._reconcileReviews.get(`${entry.fixture_id}:${entry.observation_id}`)
      : null;
    return html`
      <div
        class="entry ${tone} ${sel && sel.type === "entry" && sel.id === key ? "selected" : ""}"
        role="button"
        tabindex="0"
        @click=${() => this.#onSelect(key)}
        @keydown=${(e) => (e.key === "Enter" || e.key === " ") && this.#onSelect(key)}
      >
        <ss-status-glyph tone=${tone} label=${entry.status}></ss-status-glyph>
        <span class="name">${name}</span>
        <span class="status">${entry.status}</span>
        <span class="detail">${entry.detail || ""}</span>
        <span class="actions">
          ${entry.status === "candidate_replacement"
            ? html`
                <button @click=${(e) => this.#action(e, { action: "rebind", fixture_id: entry.fixture_id, observation_id: candidateObsId, key })}>
                  Review rebind
                </button>
                ${review ? html`<span class="detail">${review.current_provider || "unbound"} → ${review.candidate_provider}; parity: ${review.parity}${review.requires_confirmation ? " • confirmation required" : ""}. Gains: ${review.capabilities_gained?.join(", ") || "none"}; losses: ${review.capabilities_lost?.join(", ") || "none"}; changes: ${review.capabilities_changed?.join(", ") || "none"}; scenes: ${review.affected_scene_ids?.join(", ") || "none"}; fidelity impacts: ${review.render_fidelity_impact?.length || 0}; warnings: ${review.warnings?.join(" ") || "none"}</span>
                  <button ?disabled=${!review.safe_to_apply} @click=${(e) => this.#action(e, { action: "applyRebind", fixture_id: entry.fixture_id, observation_id: candidateObsId, key })}>Apply rebind</button>` : ""}
                <button @click=${(e) => this.#action(e, { action: "disable", fixture_id: entry.fixture_id, key })}>Disable</button>
                <button @click=${(e) => this.#action(e, { action: "leave", key })}>Leave unbound</button>
              `
            : entry.status === "missing"
              ? html`
                  <button @click=${(e) => this.#action(e, { action: "retry", fixture_id: entry.fixture_id, key })}>Retry</button>
                  <button @click=${(e) => this.#action(e, { action: "disable", fixture_id: entry.fixture_id, key })}>Disable</button>
                `
              : entry.status === "available_unbound"
                ? html`<button @click=${(e) => this.#action(e, { action: "leave", key })}>Leave unbound</button>`
                : entry.status === "bound_degraded"
                  ? html`<button @click=${(e) => this.#action(e, { action: "retry", fixture_id: entry.fixture_id, key })}>Retry</button>`
                  : entry.status === "bound_reconcile_available"
                    ? html`
                        <button @click=${(e) => this.#action(e, { action: "reconcile", fixture_id: entry.fixture_id, observation_id: entry.observation_id, key })}>Review update</button>
                        ${reconcileReview ? html`<span class="detail">Binding unchanged. Health ${reconcileReview.current?.operational_health} → ${reconcileReview.proposed?.operational_health}. Apply when policy allows.</span>
                          <button ?disabled=${!reconcileReview.safe_to_apply} @click=${(e) => this.#action(e, { action: "applyReconcile", fixture_id: entry.fixture_id, observation_id: entry.observation_id, key })}>Apply registry update</button>` : ""}
                      `
                  : ""}
        </span>
      </div>
    `;
  }
}

customElements.define("ss-view-discovery", SsViewDiscovery);
