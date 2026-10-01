/**
 * <ss-inspector> — generic right-side inspector / narrow-layout drawer
 * (master plan §10.2, §10.4).
 *
 * Renders an inspector *descriptor* built by src/inspector.js:
 *  - Level 1/2: status glyph, facts, explanation text, action buttons.
 *  - Level 3: `technical` JSON inside a collapsed "Technical details"
 *    disclosure (raw UUIDs, endpoints, capability payloads, event data).
 *
 * Actions emit a bubbling `inspector-action` CustomEvent with the action
 * object; the shell routes them to the store/command layer.
 *
 * Visual pass (2026-09-16): this panel had never received the app-wide
 * facelift — its action buttons used a `ss-btn` class that only exists in
 * the document-level `base.css`, which never reaches a shadow-DOM
 * component, so every action (Disable, Apply registry update, ...)
 * rendered as an unstyled native button. Buttons are now styled locally;
 * facts/the registry-review comparison read as a bordered card list
 * instead of a loose two-column grid; the explanation reads as a tone-
 * colored callout instead of plain paragraphs; Technical details gets the
 * same chevron-disclosure treatment used elsewhere (Fixtures room groups,
 * Scenes archived section); and Close matches the header's icon-button
 * language. Content/structure (facts, explanation, actions, technical)
 * is unchanged — only presentation.
 *
 * @prop {{kind,title,subtitle,tone,status,facts,explanation,actions,technical}|null} descriptor
 */
import { LitElement, html, css } from "lit";
import "./ss-status-glyph.js";
import "./ss-capability-indicators.js";
import "./ss-swatch-band.js";
import { iconChevronRight } from "./icons.js";

const TONE_VAR = { ok: "--ss-ok", warn: "--ss-warn", err: "--ss-err", idle: "--ss-idle" };

export class SsInspector extends LitElement {
  static properties = {
    descriptor: { attribute: false },
   allowedCommands: { type: Array }, review: { attribute: false },};

  static styles = css`
    :host {
      display: block;
    }
    .panel {
      border-left: 1px solid var(--ss-border);
      background: var(--ss-surface);
      padding: 16px;
      height: 100%;
      overflow-y: auto;
    }
    header {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding-bottom: 12px;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--ss-border-soft);
    }
    .title {
      flex: 1;
      min-width: 0;
    }
    h2 {
      margin: 2px 0 0;
      font-size: var(--ss-size-20);
      font-weight: 700;
      letter-spacing: -0.01em;
      overflow-wrap: anywhere;
    }
    .sub {
      color: var(--ss-text-faint);
      font-size: var(--ss-size-11);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .close {
      appearance: none;
      flex: none;
      width: var(--ss-control-h);
      height: var(--ss-control-h);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      background: transparent;
      border: 1px solid transparent;
      color: var(--ss-text-dim);
      border-radius: var(--ss-radius-sm);
      cursor: pointer;
      font-size: 16px;
    }
    .close:hover {
      color: var(--ss-text);
      background: var(--ss-surface-2);
      border-color: var(--ss-border-soft);
    }
    .close:focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 1px;
    }
    .status-row {
      margin: 0 0 12px;
    }
    /* Card-list treatment for key/value facts (shared with the registry
       review's before/after comparison below) — a bordered, subtly-filled
       block with divided rows, matching the visual weight of list rows
       elsewhere in the app instead of a loose unstyled grid. */
    .facts,
    .shift-grid {
      margin: 0 0 12px;
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
      background: var(--ss-surface-2);
      overflow: hidden;
    }
    .fact-row {
      display: grid;
      grid-template-columns: 130px 1fr;
      gap: 10px;
      padding: 8px 12px;
      border-bottom: 1px solid var(--ss-border-soft);
    }
    .fact-row:last-child {
      border-bottom: none;
    }
    .facts dt,
    .shift-grid dt {
      color: var(--ss-text-faint);
      font-size: var(--ss-size-12);
    }
    .facts dd,
    .shift-grid dd {
      margin: 0;
      color: var(--ss-text);
      font-size: 15px;
      overflow-wrap: anywhere;
    }
    /* Explanation as a tone-colored callout (left accent bar matches the
       descriptor's own status tone) instead of plain paragraphs under a
       thin rule — the text here is operational guidance ("intentionally
       disabled", "scenes skip this fixture until...") and deserves to
       read as a callout, not body copy. */
    .explain {
      margin: 0 0 12px;
      padding: 10px 12px;
      border-radius: var(--ss-radius);
      background: var(--ss-surface-2);
      border-left: 3px solid var(--ss-border);
    }
    .explain p {
      margin: 0 0 6px;
      font-size: 15px;
      line-height: 1.45;
      color: var(--ss-text-dim);
    }
    .explain p:last-child {
      margin-bottom: 0;
    }
    .explain p b {
      color: var(--ss-text);
    }
    /* Live-playback issues (R5C): what the row's issue tag counts, spelled
       out per fixture. Same callout language as .explain, warn-toned. */
    .issues {
      margin: 0 0 12px;
      padding: 10px 12px;
      border-radius: var(--ss-radius);
      background: var(--ss-warn-dim, var(--ss-surface-2));
      border-left: 3px solid var(--ss-warn);
    }
    .issues-head {
      font-size: var(--ss-size-11);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--ss-warn);
    }
    .issues ul {
      margin: 6px 0 0;
      padding: 0;
      list-style: none;
    }
    .issues li {
      padding: 3px 0;
      font-size: 15px;
      line-height: 1.45;
      color: var(--ss-text-dim);
    }
    .issues .iname {
      color: var(--ss-text);
      font-weight: 600;
    }
    .policy-note {
      margin: 8px 0 0;
      font-size: var(--ss-size-12);
      color: var(--ss-text-faint);
      font-style: italic;
    }
    .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 4px;
      align-items: center;
    }
    /* External light-sync policy: one labeled select instead of a spread
       of "Sync: …" buttons — the current value stays visible in place, and
       each option's next-apply behavior is in its tooltip. */
    .policy-control {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius-sm);
      padding: 5px 10px;
      background: var(--ss-surface-2);
    }
    .policy-label {
      font-size: var(--ss-size-11);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--ss-text-faint);
    }
    .policy-select {
      appearance: none;
      background: var(--ss-bg);
      color: var(--ss-text);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      padding: 6px 10px;
      min-height: var(--ss-control-h);
      font-size: 15px;
      font-family: inherit;
      cursor: pointer;
    }
    .policy-select:disabled {
      opacity: 0.45;
      cursor: default;
    }
    /* Locally-scoped action button — base.css's document-level .ss-btn
       never reaches this shadow root, so this panel's actions previously
       rendered as unstyled native buttons. */
    .btn {
      appearance: none;
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text);
      border-radius: var(--ss-radius-sm);
      padding: 8px 14px;
      min-height: var(--ss-control-h);
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
    }
    .btn:hover {
      border-color: var(--ss-accent);
    }
    .btn:active {
      transform: translateY(1px);
    }
    .btn:disabled {
      opacity: 0.45;
      cursor: default;
      transform: none;
    }
    .btn:focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 1px;
    }
    .btn.is-ok {
      border-color: var(--ss-ok);
      color: var(--ss-ok);
    }
    .btn.is-ok:hover:not(:disabled) {
      background: var(--ss-ok-dim);
    }
    .btn.is-warn {
      border-color: var(--ss-warn);
      color: var(--ss-warn);
    }
    .btn.is-warn:hover:not(:disabled) {
      background: var(--ss-warn-dim);
    }
    details.tech {
      margin-top: 14px;
      border-top: 1px solid var(--ss-border-soft);
      padding-top: 10px;
    }
    summary {
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 8px 0;
      color: var(--ss-text-faint);
      font-size: var(--ss-size-11);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      list-style: none;
    }
    summary::-webkit-details-marker {
      display: none;
    }
    summary:hover {
      color: var(--ss-text-dim);
    }
    summary:focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 2px;
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
    @media (prefers-reduced-motion: reduce) {
      .chev,
      .btn {
        transition: none;
      }
    }
    pre {
      margin: 10px 0 0;
      padding: 10px;
      background: var(--ss-surface-3);
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius-sm);
      font: var(--ss-font-mono);
      color: var(--ss-text-dim);
      overflow: auto;
      max-height: 280px;
    }
    .empty {
      color: var(--ss-text-faint);
      font-size: 15px;
      padding: 12px;
    }
    /* Touch/mobile (same query as the token block in tokens.css): the
       inspector becomes a full-width drawer on phones — labels need less
       reserved width and every row more air. */
    @media (max-width: 640px), (pointer: coarse) {
      .fact-row {
        grid-template-columns: 108px minmax(0, 1fr);
        padding: 10px 12px;
      }
    }
  `;

  constructor() {
    super();
    this.descriptor = null;
    this.review = null;
  }

  #onAction(action, e) {
    e.stopPropagation();
    this.dispatchEvent(
      new CustomEvent("inspector-action", { detail: action, bubbles: true, composed: true })
    );
  }

  #close(e) {
    e.stopPropagation();
    this.dispatchEvent(new CustomEvent("inspector-close", { bubbles: true, composed: true }));
  }

  #commandAllowed(command) {
    return !Array.isArray(this.allowedCommands) || this.allowedCommands.includes(command);
  }

  #shift(before, after) {
    const left = before || "unknown";
    const right = after || "unknown";
    return left === right ? "unchanged" : `${left} → ${right}`;
  }

  #device(profile) {
    if (!profile) return "unknown";
    const parts = [profile.manufacturer, profile.model].filter(Boolean);
    return parts.join(" ") || profile.product_name || "profiled";
  }

  #controls(caps) {
    if (!caps) return "none";
    const chips = [];
    if (caps.on_off) chips.push("ON/OFF");
    if (caps.brightness) chips.push("BRI");
    if (caps.color_xy) chips.push("RGB");
    if (caps.color_temp) chips.push("TEMP");
    if (caps.cct) chips.push("CCT");
    if (caps.gradient) chips.push(`GRAD×${caps.gradient.max_points}`);
    if (caps.dynamic_native) chips.push("DYN");
    return chips.join(" · ") || "none";
  }

  #reviewBlock(d) {
    const review = this.review;
    if (!review || !d.technical || review.fixture_id !== d.technical.fixture_id) return "";
    if (review.binding_unchanged) {
      const current = review.current || {};
      const proposed = review.proposed || {};
      const applyAllowed = this.#commandAllowed("fixture.reconcile");
      const fidelity = (review.render_fidelity_impact || []).length
        ? `${review.render_fidelity_impact.length} scene${review.render_fidelity_impact.length === 1 ? "" : "s"}`
        : "unchanged";
      return html`
        <div class="explain">
          <p><b>Registry update review</b></p>
          <dl class="shift-grid">
            <div class="fact-row"><dt>Binding</dt><dd>unchanged</dd></div>
            <div class="fact-row"><dt>Health</dt><dd>${this.#shift(current.operational_health, proposed.operational_health)}</dd></div>
            <div class="fact-row"><dt>Device</dt><dd>${this.#shift(this.#device(current.device_profile), this.#device(proposed.device_profile))}</dd></div>
            <div class="fact-row"><dt>Effective controls</dt><dd>${this.#shift(this.#controls(current.effective_capabilities), this.#controls(proposed.effective_capabilities))}</dd></div>
            <div class="fact-row"><dt>Assessment</dt><dd>${this.#shift((current.capability_assessment && current.capability_assessment.status) || "unknown", (proposed.capability_assessment && proposed.capability_assessment.status) || "unknown")}</dd></div>
            ${review.groups_changed
              ? html`<div class="fact-row"><dt>Groups</dt><dd>${this.#shift((current.groups || []).join(" · "), (proposed.groups || []).join(" · "))}</dd></div>`
              : ""}
            <div class="fact-row"><dt>Scene fidelity</dt><dd>${fidelity}</dd></div>
          </dl>
          <p>Warnings: ${(review.warnings || []).join(" ") || "none"}.</p>
          ${applyAllowed
            ? html`<button class="btn is-ok" ?disabled=${!review.safe_to_apply} @click=${(e) => this.#onAction({ id: "fixture.reconcile", label: "Apply registry update", args: review.action }, e)}>Apply registry update</button>`
            : html`<p class="policy-note">Apply is unavailable in the current backend mode. Registry updates require registry_admin; provider writes stay blocked.</p>`}
        </div>
      `;
    }
    const applyAllowed = this.#commandAllowed("fixture.rebind");
    return html`
      <div class="explain">
        <p>Rebind review: ${review.current_provider || "unbound"} → ${review.candidate_provider}; parity ${review.parity}${review.requires_confirmation ? " (confirmation required)" : ""}.</p>
        <p>Gained: ${(review.capabilities_gained || []).join(", ") || "none"}. Lost: ${(review.capabilities_lost || []).join(", ") || "none"}. Changed: ${(review.capabilities_changed || []).join(", ") || "none"}. Scenes affected: ${(review.affected_scene_ids || []).join(", ") || "none"}; fidelity impacts: ${(review.render_fidelity_impact || []).length}. Warnings: ${(review.warnings || []).join(" ") || "none"}.</p>
        ${applyAllowed
          ? html`<button class="btn is-warn" ?disabled=${!review.safe_to_apply} @click=${(e) => this.#onAction({ id: "fixture.rebind", label: "Apply rebind", args: review.action }, e)}>Apply rebind</button>`
          : html`<p class="policy-note">Apply is unavailable in the current backend mode.</p>`}
      </div>
    `;
  }

  /**
   * Facts, with "Capabilities" rendered as the same capability badges
   * (<ss-capability-indicators>) the Fixtures list uses — truthful
   * fill/border state per capability, not a plain text chip string — for a
   * fixture descriptor (identified by `technical.capabilities` being
   * present; scene/discovery-entry/event descriptors carry no such field
   * and fall through to plain text facts unchanged). Inserted where the
   * text "Capabilities" fact used to sit (right after Provider) rather
   * than appended, so the reading order — identity, then what it can do,
   * then deeper diagnostic facts — is unchanged.
   */
  #factRows(d) {
    const facts = d.facts || [];
    const hasCapabilityBadges = d.technical && d.technical.capabilities !== undefined;
    const rows = facts.map(
      (f) => html`
        <div class="fact-row">
          <dt>${f.label}</dt>
          <dd>
            ${f.swatches && f.swatches.length
              ? html`<ss-swatch-band variant="squares" .palette=${f.swatches} .max=${10}></ss-swatch-band>`
              : f.value || "—"}
          </dd>
        </div>
      `
    );
    if (hasCapabilityBadges) {
      const badgeRow = html`
        <div class="fact-row">
          <dt>Capabilities</dt>
          <dd>
            <ss-capability-indicators
              .capabilities=${d.technical.capabilities}
              .disabled=${d.tone === "idle"}
            ></ss-capability-indicators>
          </dd>
        </div>
      `;
      rows.splice(Math.min(2, rows.length), 0, badgeRow);
    }
    return rows;
  }

  render() {
    const d = this.descriptor;
    if (!d) {
      return html`<aside class="panel"><div class="empty">Select a row to inspect it.</div></aside>`;
    }
    const toneVar = TONE_VAR[d.tone] || "--ss-border";
    return html`
      <aside class="panel">
        <header>
          <div class="title">
            ${d.subtitle ? html`<div class="sub">${d.subtitle}</div>` : ""}
            <h2>${d.title}</h2>
          </div>
          <button class="close" @click=${this.#close} aria-label="Close inspector">✕</button>
        </header>
        <div class="status-row">
          <ss-status-glyph tone=${d.tone} label=${d.status}></ss-status-glyph>
        </div>
        <dl class="facts">
          ${this.#factRows(d)}
        </dl>
        ${(d.issues || []).length
          ? html`
              <div class="issues" role="note">
                <div class="issues-head">Playback issues (${d.issues.length})</div>
                <ul>
                  ${(d.issues || []).map(
                    (issue) => html`<li><span class="iname">${issue.fixture}</span> — ${issue.detail}</li>`
                  )}
                </ul>
              </div>
            `
          : ""}
        ${(d.explanation || []).length
          ? html`
              <div class="explain" style="border-left-color: var(${toneVar})">
                ${(d.explanation || []).map((text) => html`<p>${text}</p>`)}
              </div>
            `
          : ""}
        ${this.#reviewBlock(d)}
        ${(d.actions || []).length
          ? html`
              <div class="actions">
                ${d.actions.map((a) => {
                  if (a.id === "__policy_note") {
                    return html`<p class="policy-note">${a.note}</p>`;
                  }
                  if (a.id === "__contention_policy") {
                    const allowed = this.#commandAllowed("fixture.set_contention_policy");
                    return html`
                      <div
                        class="policy-control"
                        title=${allowed
                          ? "What a scene apply does to this fixture while hyperHDR is syncing it"
                          : "Unavailable in the current backend mode"}
                      >
                        <label class="policy-label" for="sync-policy-select">External sync</label>
                        <select
                          id="sync-policy-select"
                          class="policy-select"
                          .value=${a.current}
                          ?disabled=${!allowed}
                          @change=${(e) => {
                            const policy = e.target.value;
                            e.target.value = a.current;
                            this.#onAction(
                              { id: "fixture.set_contention_policy", args: { fixture_id: a.fixture_id, policy } },
                              e
                            );
                          }}
                        >
                          ${a.options.map(
                            (o) => html`<option value=${o.value} title=${o.description} ?selected=${o.value === a.current}>
                              ${o.label}
                            </option>`
                          )}
                        </select>
                      </div>
                    `;
                  }
                  const available =
                    !Array.isArray(this.allowedCommands) || this.allowedCommands.includes(a.id);
                  if (!available) {
                    return html`<p class="policy-note">Unavailable in the current backend mode</p>`;
                  }
                  return html`
                    <button
                      class="btn ${a.tone === "ok" ? "is-ok" : a.tone === "warn" ? "is-warn" : ""}"
                      aria-label=${a.ariaLabel || (typeof a.label === "string" ? a.label : "")}
                      title=${a.ariaLabel && typeof a.label !== "string" ? a.ariaLabel : ""}
                      @click=${(e) => this.#onAction(a, e)}
                    >
                      ${a.label}
                    </button>
                  `;
                })}
              </div>
            `
          : ""}
        <details class="tech">
          <summary><span class="chev">${iconChevronRight(12)}</span>Technical details</summary>
          <pre>${JSON.stringify(d.technical, null, 2)}</pre>
        </details>
      </aside>
    `;
  }
}

customElements.define("ss-inspector", SsInspector);
