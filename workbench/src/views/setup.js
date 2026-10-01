/**
 * <ss-view-setup> — first-run bootstrap surface (portable-install pass).
 *
 * Shown when the registry is EMPTY (status.fixtures.total === 0): a bounded
 * setup flow that turns a fresh discovery report into the first canonical
 * fixtures/targets through the narrow bootstrap commands
 * (fixture.adopt / target.create / target.update). No wizard framework —
 * each step is a plain section over the same store surfaces the other views
 * use, and every mutation goes through store.sendCommand so the BACKEND
 * policy (registry_admin blocks provider writes) stays the single
 * authority. The final step spells out the exact apps.yaml change needed to
 * leave registry_admin; the view never implies that adopting fixtures
 * authorized provider writes.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-panel.js";
import "../components/ss-empty-state.js";

const PROVIDER_SHORT = { hue_v2: "Hue", wled: "WLED", ha_light: "HA" };
const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;

function slugify(name) {
  return String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 64);
}

export class SsViewSetup extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-setup) {
    :scope {
      display: block;
    }
    .intro {
      margin: 0 8px 12px;
      color: var(--ss-text-dim);
      font-size: 15px;
      line-height: 1.45;
      max-width: 76ch;
    }
    .intro code {
      font-family: var(--ss-font-mono);
      font-size: 13px;
      color: var(--ss-text);
    }
    .step {
      margin: 0 8px 14px;
    }
    .step > h2 {
      font-size: 17px;
      margin: 0 0 8px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .step > h2 .n {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 22px;
      height: 22px;
      border-radius: 50%;
      border: 1px solid var(--ss-border);
      font-size: 13px;
      font-family: var(--ss-font-mono);
      color: var(--ss-text-dim);
    }
    .mode {
      font-family: var(--ss-font-mono);
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--ss-border);
      color: var(--ss-text-dim);
    }
    .providers {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      padding: 0 2px;
    }
    .provider {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--ss-border);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 14px;
    }
    .provider .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--ss-ok, #3f9b4f);
    }
    .provider.off .dot {
      background: var(--ss-err, #c0392b);
    }
    .provider .detail {
      color: var(--ss-text-faint);
      font-family: var(--ss-font-mono);
      font-size: 12px;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 6px 2px;
    }
    .obs-row {
      display: grid;
      grid-template-columns: minmax(140px, 1.2fr) minmax(110px, 1fr) minmax(110px, 1fr) minmax(90px, 0.9fr) auto;
      gap: 6px;
      align-items: center;
      padding: 7px 8px;
      border-bottom: 1px solid var(--ss-border-weak, var(--ss-border));
    }
    .obs-row:last-child {
      border-bottom: none;
    }
    .obs-row .obs-name {
      font-size: 15px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .obs-row .provider {
      font-size: 12px;
      font-family: var(--ss-font-mono);
      color: var(--ss-text-faint);
    }
    .obs-row input {
      min-width: 0;
      width: 100%;
      box-sizing: border-box;
    }
    .summary {
      padding: 8px 10px;
      color: var(--ss-text-dim);
      font-size: 14px;
    }
    .summary b {
      color: var(--ss-text);
      font-family: var(--ss-font-mono);
    }
    .target-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 7px 8px;
      border-bottom: 1px solid var(--ss-border-weak, var(--ss-border));
      font-size: 15px;
      flex-wrap: wrap;
    }
    .target-row:last-child {
      border-bottom: none;
    }
    .target-row .members {
      color: var(--ss-text-faint);
      font-size: 13px;
      font-family: var(--ss-font-mono);
    }
    .target-row select {
      max-width: 200px;
    }
    pre.yaml {
      font-family: var(--ss-font-mono);
      font-size: 13px;
      background: color-mix(in srgb, var(--ss-panel-bg, transparent) 82%, transparent);
      border: 1px solid var(--ss-border);
      border-radius: 8px;
      padding: 10px 12px;
      overflow-x: auto;
      margin: 8px 0 0;
    }
    .hint {
      color: var(--ss-text-faint);
      font-size: 13px;
      margin: 6px 2px 0;
    }
    .done {
      color: var(--ss-ok, #3f9b4f);
      font-size: 13px;
      font-family: var(--ss-font-mono);
    }
    @container (max-width: 760px) {
      .obs-row {
        grid-template-columns: 1fr 1fr;
      }
    }
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._unsub = null;
    /** per-observation adopt drafts: observation_id -> {fixture_id, name, groups} */
    this._drafts = {};
    /** target creation draft: {name, fixtureIds: Set} */
    this._targetDraft = { name: "", fixtureIds: new Set() };
    this._busy = false;
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

  #allowed(command) {
    const runtime = this.store?.state?.status?.runtime;
    if (!runtime || !Array.isArray(runtime.allowed_commands)) return true;
    return runtime.allowed_commands.includes(command);
  }

  #draftFor(observationId, fallbackName) {
    if (!this._drafts[observationId]) {
      this._drafts[observationId] = {
        fixture_id: slugify(fallbackName),
        name: fallbackName || "",
        groups: "",
      };
    }
    return this._drafts[observationId];
  }

  async #adopt(observationId) {
    const draft = this._drafts[observationId];
    if (!draft || this._busy) return;
    const groups = draft.groups
      .split(/[,\s]+/)
      .map((g) => g.trim())
      .filter(Boolean);
    this._busy = true;
    try {
      const res = await this.store.sendCommand({
        command: "fixture.adopt",
        observation_id: observationId,
        fixture_id: draft.fixture_id.trim(),
        name: draft.name.trim() || draft.fixture_id.trim(),
        groups,
      });
      if (res.ok) delete this._drafts[observationId];
    } finally {
      this._busy = false;
      this.requestUpdate();
    }
  }

  async #runDiscovery() {
    if (this._busy) return;
    this._busy = true;
    try {
      await this.store.sendCommand({ command: "discovery.run" });
    } finally {
      this._busy = false;
      this.requestUpdate();
    }
  }

  async #createTarget() {
    if (this._busy) return;
    const draft = this._targetDraft;
    if (!draft.name.trim()) return;
    this._busy = true;
    try {
      const res = await this.store.sendCommand({
        command: "target.create",
        name: draft.name.trim(),
        fixture_ids: [...draft.fixtureIds],
      });
      if (res.ok) this._targetDraft = { name: "", fixtureIds: new Set() };
    } finally {
      this._busy = false;
      this.requestUpdate();
    }
  }

  async #assign(targetId, fixtureId) {
    if (!fixtureId || this._busy) return;
    this._busy = true;
    try {
      await this.store.sendCommand({ command: "target.update", target_id: targetId, add_fixture_ids: [fixtureId] });
    } finally {
      this._busy = false;
      this.requestUpdate();
    }
  }

  async #unassign(targetId, fixtureId) {
    if (this._busy) return;
    this._busy = true;
    try {
      await this.store.sendCommand({ command: "target.update", target_id: targetId, remove_fixture_ids: [fixtureId] });
    } finally {
      this._busy = false;
      this.requestUpdate();
    }
  }

  #unbound(report) {
    if (!report || !Array.isArray(report.entries)) return [];
    const byId = new Map((report.observations || []).map((o) => [`${o.provider}:${o.provider_resource_id}`, o]));
    const seen = new Set();
    const rows = [];
    for (const entry of report.entries) {
      if (entry.status !== "available_unbound") continue;
      const observationId = entry.observation_id;
      if (!byId.has(observationId) || seen.has(observationId)) continue;
      seen.add(observationId);
      rows.push({ observationId, observation: byId.get(observationId) });
    }
    return rows;
  }

  #memberCounts(fixtures) {
    const counts = {};
    for (const fixture of fixtures || []) {
      for (const group of fixture.groups || []) {
        counts[group] = (counts[group] || 0) + 1;
      }
    }
    return counts;
  }

  render() {
    const s = this.store?.state;
    if (!s || !s.status) return html`<ss-empty-state>Loading…</ss-empty-state>`;
    const runtime = s.status.runtime || {};
    const registryEmpty = (s.fixtures?.fixtures || []).length === 0;
    const discoveryReport = s.discovery || null;
    const unbound = this.#unbound(discoveryReport);
    const fixtures = s.fixtures?.fixtures || [];
    const targets = s.fixtures?.targets || [];
    const memberCounts = this.#memberCounts(fixtures);
    const providers = s.status.provider_links || [];
    const summary = discoveryReport && discoveryReport.summary;

    return html`
      <p class="intro" data-ss="setup-intro">
        This Scene Studio install has an <b>empty registry</b> — nothing is adopted yet, so there is
        nothing to control. Work through the steps below to build your own registry from discovery.
        Runtime mode is <code>${runtime.mode || "unknown"}</code>:
        discovery and registry setup work here, but <b>every provider write is blocked</b> until you
        deliberately leave this mode (step 6). Nothing you do in this view touches a light.
      </p>

      <ss-panel class="step" data-ss="setup-health">
        <h2><span class="n">1</span> Runtime health <span class="mode">${runtime.mode || "?"}</span></h2>
        <div class="providers">
          ${providers.map(
            (p) => html`
              <span class="provider ${p.connected ? "" : "off"}">
                <span class="dot"></span> ${p.label || p.provider}
                <span class="detail">${p.detail || ""}</span>
              </span>
            `
          )}
        </div>
        <p class="hint">
          Providers are read during setup (discovery); writes stay blocked in
          <code>${runtime.mode || "?"}</code> regardless of connectivity.
        </p>
      </ss-panel>

      <ss-panel class="step" data-ss="setup-discovery">
        <h2><span class="n">2</span> Discovery</h2>
        <div class="row">
          <button
            class="ss-btn"
            data-ss="setup-run-discovery"
            ?disabled=${this._busy || !this.#allowed("discovery.run")}
            @click=${this.#runDiscovery}
          >
            Run discovery
          </button>
          ${summary
            ? html`<span class="done" data-ss="setup-discovery-summary">
                ${summary.observations_total} observation(s) • ${summary.unbound_observations ?? 0} unbound
              </span>`
            : html`<span class="hint">No discovery report yet — run discovery to find your devices.</span>`}
        </div>
      </ss-panel>

      <ss-panel class="step" data-ss="setup-adopt">
        <h2><span class="n">3</span> Adopt fixtures</h2>
        ${registryEmpty && unbound.length === 0
          ? html`<div class="summary">No unbound devices in the latest discovery report.</div>`
          : html`
              <div class="obs-row" role="row">
                <span class="obs-name"><b>Device</b></span>
                <span><b>Stable id</b></span>
                <span><b>Display name</b></span>
                <span><b>Groups</b></span>
                <span></span>
              </div>
            `}
        ${unbound.map(({ observationId, observation }) => {
          const draft = this.#draftFor(observationId, observation.name || "");
          return html`
            <div class="obs-row" role="row" data-ss="setup-observation">
              <span class="obs-name" title=${observationId}>
                ${observation.name || observationId}
                <span class="provider">${PROVIDER_SHORT[observation.provider] || observation.provider}</span>
              </span>
              <input
                class="ss-input"
                aria-label="Stable fixture id"
                data-ss="adopt-id"
                .value=${draft.fixture_id}
                @input=${(e) => (draft.fixture_id = e.target.value)}
              />
              <input
                class="ss-input"
                aria-label="Display name"
                data-ss="adopt-name"
                .value=${draft.name}
                @input=${(e) => (draft.name = e.target.value)}
              />
              <input
                class="ss-input"
                aria-label="Initial groups (comma separated)"
                data-ss="adopt-groups"
                placeholder="e.g. office"
                .value=${draft.groups}
                @input=${(e) => (draft.groups = e.target.value)}
              />
              <button
                class="ss-btn"
                data-ss="adopt-button"
                ?disabled=${this._busy || !this.#allowed("fixture.adopt") || !draft.fixture_id.trim()}
                @click=${() => this.#adopt(observationId)}
              >
                Adopt
              </button>
            </div>
          `;
        })}
        <p class="hint">
          The server derives the provider binding, capabilities, and endpoint hints from the
          discovery observation — you choose only the stable id, the label, and initial groups.
          Duplicate ids and stale observations are rejected.
        </p>
      </ss-panel>

      <ss-panel class="step" data-ss="setup-targets">
        <h2><span class="n">4</span> Targets (rooms / groups)</h2>
        ${targets.length
          ? html`
              <ss-panel variant="list">
                ${targets.map((target) => {
                  const candidates = fixtures.filter(
                    (f) => !(f.groups || []).includes(target.id)
                  );
                  return html`
                    <div class="target-row" data-ss="setup-target">
                      <b>${target.name}</b>
                      <span class="members">${target.id} · ${memberCounts[target.id] || 0} member(s)</span>
                      ${fixtures.length
                        ? html`
                            <select
                              class="ss-select"
                              aria-label="Add fixture to ${target.name}"
                              data-ss="target-add-select"
                              @change=${(e) => {
                                this.#assign(target.id, e.target.value);
                                e.target.value = "";
                              }}
                            >
                              <option value="">Add fixture…</option>
                              ${candidates.map(
                                (f) => html`<option value=${f.id}>${f.name} (${f.id})</option>`
                              )}
                            </select>
                          `
                        : ""}
                      ${(fixtures || [])
                        .filter((f) => (f.groups || []).includes(target.id))
                        .map(
                          (f) => html`
                            <button
                              class="ss-btn"
                              data-ss="target-remove-member"
                              title="Remove ${f.id} from ${target.id}"
                              ?disabled=${this._busy}
                              @click=${() => this.#unassign(target.id, f.id)}
                            >
                              ${f.id} ✕
                            </button>
                          `
                        )}
                    </div>
                  `;
                })}
              </ss-panel>
            `
          : html`<div class="summary">No targets declared yet.</div>`}
        <div class="row">
          <input
            class="ss-input"
            style="max-width: 240px"
            placeholder="New target name (e.g. Living Room)"
            aria-label="New target name"
            data-ss="target-name"
            .value=${this._targetDraft.name}
            @input=${(e) => (this._targetDraft.name = e.target.value)}
          />
          ${fixtures.map(
            (f) => html`
              <label class="row" style="padding:0">
                <input
                  type="checkbox"
                  data-ss="target-member-checkbox"
                  .checked=${this._targetDraft.fixtureIds.has(f.id)}
                  @change=${(e) => {
                    if (e.target.checked) this._targetDraft.fixtureIds.add(f.id);
                    else this._targetDraft.fixtureIds.delete(f.id);
                    this.requestUpdate();
                  }}
                />
                ${f.id}
              </label>
            `
          )}
          <button
            class="ss-btn"
            data-ss="target-create-button"
            ?disabled=${this._busy || !this.#allowed("target.create") || !this._targetDraft.name.trim()}
            @click=${this.#createTarget}
          >
            Create target
          </button>
        </div>
        <p class="hint">The id is derived from the name server-side; membership is assigned on the fixture side.</p>
      </ss-panel>

      <ss-panel class="step" data-ss="setup-summary">
        <h2><span class="n">5</span> Registry summary</h2>
        <div class="summary" data-ss="setup-registry-summary">
          <b>${fixtures.length}</b> fixture(s) · <b>${targets.length}</b> target(s) ·
          scenes become available once you leave setup and author them in the Builder.
        </div>
      </ss-panel>

      <ss-panel class="step" data-ss="setup-exit">
        <h2><span class="n">6</span> Leave <code>registry_admin</code> (deliberate, later)</h2>
        <p class="hint">
          Adopting fixtures never authorizes provider writes. When your registry is ready and
          you have previewed a scene, edit AppDaemon's <code>apps.yaml</code> Scene Studio block:
        </p>
        <pre class="yaml">scene_studio:
  registry_admin: false
  read_only: false</pre>
        <p class="hint">…then restart AppDaemon. Until then, scene apply and playback stay blocked.</p>
      </ss-panel>
    `;
  }
}

customElements.define("ss-view-setup", SsViewSetup);
