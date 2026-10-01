/**
 * <ss-view-diagnostics> — structured OperationalEvent feed (§10.7).
 * Concise summary + detail lines; raw payloads only through the disclosure
 * / inspector (Level 3). Includes the sanitized export action.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-status-glyph.js";
import "../components/ss-panel.js";
import { eventKey } from "../inspector.js";

export class SsViewDiagnostics extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-diagnostics) {
    :scope {
      display: block;
      /* Stage 3+ (facelift plan §4.6): diagnostics lives "below deck" —
         a darker sub-surface and smaller type than the rest of the shell,
         while staying fully functional. */
      background: var(--ss-surface-3);
      border-radius: var(--ss-radius);
      padding: 8px 4px;
    }
    .bar {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
    }
    .muted {
      color: var(--ss-text-faint);
      font-size: 14px;
    }
    .event {
      padding: 8px 10px;
      border-top: 1px solid var(--ss-border-soft);
      cursor: pointer;
    }
    .event:first-child {
      border-top: none;
    }
    .event:hover {
      background: var(--ss-surface-2);
    }
    .event.selected {
      background: var(--ss-surface-2);
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .head {
      display: flex;
      align-items: baseline;
      gap: 8px;
    }
    .time {
      flex: none;
      width: 40px;
      color: var(--ss-text-faint);
      font-family: var(--ss-font-mono);
      font-size: 13px;
    }
    .summary {
      font-size: 16px;
    }
    .event.error .summary { color: var(--ss-err); }
    .event.warning .summary { color: var(--ss-warn); }
    .detail {
      margin: 1px 0 0 48px;
      color: var(--ss-text-dim);
      font-size: 14px;
    }
    details {
      margin: 3px 0 0 48px;
    }
    summary {
      color: var(--ss-text-faint);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      cursor: pointer;
      padding: 6px 0;
    }
    pre {
      margin: 4px 0 0;
      padding: 6px 8px;
      background: var(--ss-bg);
      border: 1px solid var(--ss-border-soft);
      border-radius: 4px;
      font: var(--ss-font-mono);
      color: var(--ss-text-dim);
      overflow: auto;
      max-height: 200px;
    }
    .empty {
      color: var(--ss-text-faint);
      padding: 12px;
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

  #onSelect(key) {
    this.store.select({ type: "event", id: key });
  }

  async #export() {
    const res = await this.store.sendCommand({
      command: "diagnostics.export",
      redact: true,
      recent_events: 200,
    });
    if (res.ok) {
      const blob = new Blob([JSON.stringify(res.data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "scene_studio_diagnostics_sanitized.json";
      a.click();
      URL.revokeObjectURL(url);
    }
  }

  render() {
    const s = this.store.state;
    const events = s.events || [];
    const sel = s.selection;
    return html`
      <div class="bar">
        <span class="muted">Structured operational events — newest first, sanitized.</span>
        <button class="ss-btn" style="margin-left:auto" @click=${this.#export}>Export sanitized snapshot</button>
      </div>
      <ss-panel variant="list">
        ${events.length
          ? events.map((event, i) => {
              const key = eventKey(event, i);
              const selected = sel && sel.type === "event" && sel.id === key;
              return html`
                <div
                  class="event ${event.level} ${selected ? "selected" : ""}"
                  role="button"
                  tabindex="0"
                  @click=${() => this.#onSelect(key)}
                  @keydown=${(e) => (e.key === "Enter" || e.key === " ") && this.#onSelect(key)}
                >
                  <div class="head">
                    <span class="time">${event.timestamp.slice(11, 16)}</span>
                    <ss-status-glyph
                      tone=${event.level === "error" ? "err" : event.level === "warning" ? "warn" : "ok"}
                      label=""
                    ></ss-status-glyph>
                    <span class="summary">${event.summary}</span>
                  </div>
                  ${event.detail ? html`<p class="detail">${event.detail}</p>` : ""}
                  ${event.data && Object.keys(event.data).length
                    ? html`
                        <details @click=${(e) => e.stopPropagation()}>
                          <summary>technical</summary>
                          <pre>${JSON.stringify(event.data, null, 2)}</pre>
                        </details>
                      `
                    : ""}
                </div>
              `;
            })
          : html`<div class="empty">No events.</div>`}
      </ss-panel>
    `;
  }
}

customElements.define("ss-view-diagnostics", SsViewDiagnostics);
