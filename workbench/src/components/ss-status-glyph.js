/**
 * <ss-status-glyph> — fixed semantic status indicator (dot + optional label).
 *
 * Tones follow the fixed mapping (AGENTS.md styling mandate):
 *   ok = green (healthy/ready), warn = yellow (degraded/warning),
 *   err = red (missing/error), idle = gray (disabled/idle).
 *
 * @prop {"ok"|"warn"|"err"|"idle"} tone semantic tone
 * @prop {string} [label] optional visible label next to the dot
 */
import { LitElement, html, css } from "lit";

export class SsStatusGlyph extends LitElement {
  static properties = {
    tone: { type: String },
    label: { type: String },
  };

  static styles = css`
    :host {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      white-space: nowrap;
    }
    .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      flex: none;
    }
    .dot.ok {
      background: var(--ss-ok);
    }
    .dot.warn {
      background: var(--ss-warn);
    }
    .dot.err {
      background: var(--ss-err);
    }
    .dot.idle {
      background: var(--ss-idle);
    }
    .label {
      color: var(--ss-text-dim);
      font-size: 14px;
    }
    :host(.loud) .label {
      color: var(--ss-text);
    }
  `;

  constructor() {
    super();
    this.tone = "idle";
    this.label = "";
  }

  render() {
    return html`<span
        class="dot ${this.tone}"
        role="img"
        aria-label=${this.label || this.tone}
      ></span>${this.label ? html`<span class="label">${this.label}</span>` : ""}`;
  }
}

customElements.define("ss-status-glyph", SsStatusGlyph);
