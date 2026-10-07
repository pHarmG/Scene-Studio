/**
 * <ss-view-overview> — the operational cockpit (master plan §10.3; restyled
 * Stage 3+ per docs/scene_studio/WORKBENCH_STAGE3_DESIGN_HANDOFF.md).
 *
 * One dominant hero (active playback, via <ss-playback-panel heroFirst>),
 * a condensed Fleet panel and Needs Attention — both <ss-panel> so they
 * share Playback's surface, border, and corner radius. Needs Attention
 * keeps a severity-colored left edge on exception rows. All data reads
 * are UNCHANGED from the prior implementation — `st.engine`, `st.provider_links`, `st.providers`,
 * `st.fixtures`, `normalizePlayback`/`liveSessions`, and
 * `buildOverviewExceptions` are the same single sources of truth; only the
 * template/CSS differ.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-status-glyph.js";
import "../components/ss-panel.js";
import "../components/ss-playback-panel.js";
import { iconAlertCircle, iconAlertTriangle, iconChevronRight } from "../components/icons.js";
import { statusTone, buildOverviewExceptions } from "../inspector.js";
import { normalizePlayback } from "../playback.js";
import { playbackActionEnvelope, currentSceneView } from "../state.js";
import { sceneLookSwatches } from "../scene_look.js";

export class SsViewOverview extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-overview) {
    :scope {
      display: block;
    }
    .grid {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    ss-playback-panel,
    .row-2col ss-panel {
      display: block;
      min-width: 0;
    }
    .row-2col {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }
    @media (max-width: 900px) {
      .row-2col {
        grid-template-columns: minmax(0, 1fr);
      }
    }
    .fleet-line {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid var(--ss-border-soft);
      font-size: var(--ss-size-13);
    }
    .fleet-line:last-child {
      border-bottom: none;
      padding-bottom: 0;
    }
    .fleet-name {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--ss-text-dim);
      min-width: 0;
      flex-wrap: wrap;
    }
    .fleet-val {
      font-weight: 600;
      flex: none;
    }
    .fleet-val.ok, .ok { color: var(--ss-ok); }
    .fleet-val.err, .err { color: var(--ss-err); }
    .fleet-val.warn, .warn { color: var(--ss-warn); }
    .exceptions {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .exception {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-left: 3px solid var(--ss-err);
      background: var(--ss-err-dim);
      border-radius: 0 8px 8px 0;
    }
    .exception.warn {
      border-left-color: var(--ss-warn);
      background: var(--ss-warn-dim);
    }
    .exception.idle {
      border-left-color: var(--ss-idle);
      background: var(--ss-idle-dim);
    }
    .exception .icon {
      flex: none;
      display: flex;
      color: var(--ss-err);
    }
    .exception.warn .icon {
      color: var(--ss-warn);
    }
    .exception.idle .icon {
      color: var(--ss-idle);
    }
    .exception .text {
      flex: 1;
      min-width: 0;
      font-size: var(--ss-size-13);
      color: var(--ss-text);
    }
    .goto {
      appearance: none;
      background: transparent;
      border: none;
      display: flex;
      align-items: center;
      gap: 3px;
      color: var(--ss-text-dim);
      font-size: var(--ss-size-12);
      font-weight: 600;
      cursor: pointer;
      flex: none;
      padding: 6px 8px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      border-radius: var(--ss-radius-sm);
    }
    .goto:hover {
      color: var(--ss-text);
      background: var(--ss-surface-2);
    }
    .all-clear {
      color: var(--ss-text-faint);
      font-size: var(--ss-size-13);
    }
    .muted {
      color: var(--ss-text-faint);
      font-size: 14px;
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

  #goto(view, selection) {
    if (selection) this.store.select(selection);
    this.store.setView(view);
  }

  /**
   * Session-addressed lifecycle commands from this view's playback panel,
   * routed through the ONE shared seam (playbackActionEnvelope -> store
   * sendCommand) — the same routing Scenes uses. No direct backend-client
   * calls, exact session_id preserved, no optimistic mutation: the store
   * refreshes status authoritatively and runtime policy stays enforced by
   * the panel's control gating.
   */
  #onPlaybackAction(e) {
    const { action, session_id } = e.detail;
    const envelope = playbackActionEnvelope(action, session_id);
    return envelope ? this.store.sendCommand(envelope) : Promise.resolve();
  }

  render() {
    const s = this.store.state;
    if (!s.status) return html`<div class="muted">Loading…</div>`;
    const st = s.status;
    const fx = st.fixtures;
    // R5A collection shape (R5C playback.js) — never read st.playback
    // directly, an empty {sessions:[],...} object is truthy.
    const playback = normalizePlayback(st.playback);
    const current = st.current;
    const links = st.provider_links || [];
    const providerCounts = st.providers || {};
    const scenes = s.scenes ? s.scenes.scenes : [];
    const scenePalettes = Object.fromEntries(scenes.map((sc) => [sc.id, sceneLookSwatches(sc)]));
    // Applied static scene for the playback panel's idle row — replaces the
    // former bespoke "Current:" line in the Fleet panel (same visibility
    // rule: it yields while any live session owns the fixtures).
    const currentRow = currentSceneView(
      current,
      {
        scenes,
        fixtures: (s.fixtures && s.fixtures.fixtures) || [],
        targets: (s.fixtures && s.fixtures.targets) || [],
      },
      scenePalettes
    );
    // Shared with the header status pill (app.js, Stage 2) — see
    // inspector.js's buildOverviewExceptions for the one source of truth.
    const exceptions = buildOverviewExceptions(s);
    const issues = exceptions.filter((ex) => ex.tone === "err" || ex.tone === "warn");
    const notices = exceptions.filter((ex) => ex.tone === "idle");
    // fixture_id -> display name for degraded-fixture disclosures.
    const fixtureNames = Object.fromEntries(
      (s.fixtures ? s.fixtures.fixtures : []).map((f) => [f.id, f.name])
    );

    return html`
      <div class="grid">
        <!-- R5C: every live session, individually addressable. Stage 3+:
             the most-attention-needing/most-recent one gets hero billing,
             any others stay real, fully-controllable rows beneath it. -->
        <ss-playback-panel
          heading="Playback"
          heroFirst
          .playback=${playback}
          .allowedCommands=${st.runtime ? st.runtime.allowed_commands : null}
          .fixtureNames=${fixtureNames}
          .scenePalettes=${scenePalettes}
          .current=${currentRow}
          .stoppedHistory=${true}
          @playback-action=${this.#onPlaybackAction}
        ></ss-playback-panel>

        <div class="row-2col">
          <ss-panel id="ss-needs-attention" variant="padded" heading=${`Needs attention (${issues.length})`}>
            <div class="exceptions">
              ${issues.length
                ? issues.map(
                    (ex) => html`
                      <div class="exception ${ex.tone}" data-ss="needs-attention-item">
                        <span class="icon">${ex.tone === "warn" ? iconAlertTriangle(17) : iconAlertCircle(17)}</span>
                        <span class="text">${ex.text}</span>
                        <button class="goto" @click=${() => this.#goto(ex.view, ex.selection)}>
                          View ${iconChevronRight(14)}
                        </button>
                      </div>
                    `
                  )
                : html`<div class="all-clear">No exceptions — every fixture ready, all providers connected.</div>`}
              ${notices.length
                ? notices.map(
                    (ex) => html`
                      <div class="exception idle">
                        <span class="icon">${iconAlertCircle(17)}</span>
                        <span class="text">${ex.text}</span>
                        <button class="goto" @click=${() => this.#goto(ex.view, ex.selection)}>
                          View ${iconChevronRight(14)}
                        </button>
                      </div>
                    `
                  )
                : ""}
            </div>
          </ss-panel>

          <ss-panel variant="padded" heading="Fleet">
            <div class="fleet-line">
              <span class="fleet-name">
                <ss-status-glyph tone=${st.engine.ok ? "ok" : "err"} label=""></ss-status-glyph>
                Engine <span class="muted">rev ${st.engine.revision}</span>
              </span>
              <span class="fleet-val ${st.engine.ok ? "ok" : "err"}">${st.engine.ok ? "healthy" : "down"}</span>
            </div>
            ${links.map((p) => {
              const counts = providerCounts[p.provider];
              return html`
                <div class="fleet-line">
                  <span class="fleet-name">
                    <ss-status-glyph tone=${p.connected ? "ok" : "err"} label=""></ss-status-glyph>
                    ${p.label}
                  </span>
                  <span class="fleet-val ${p.connected ? "ok" : "err"}">
                    ${counts ? `${counts.ready}/${counts.total}` : p.connected ? "ok" : "offline"}
                  </span>
                </div>
              `;
            })}
            <div class="fleet-line">
              <span class="fleet-name">Fixtures ready</span>
              <span class="fleet-val ${fx.missing || fx.conflicting ? "err" : fx.degraded ? "warn" : "ok"}">
                ${fx.ready}/${fx.total}
                ${fx.missing ? html`· <span class="err">${fx.missing} missing</span>` : ""}
                ${fx.degraded ? html`· <span class="warn">${fx.degraded} degraded</span>` : ""}
                ${fx.disabled ? html`· <span class="muted">${fx.disabled} disabled</span>` : ""}
              </span>
            </div>
          </ss-panel>
        </div>
      </div>
    `;
  }
}

customElements.define("ss-view-overview", SsViewOverview);
