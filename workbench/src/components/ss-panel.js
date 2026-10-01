/**
 * <ss-panel> — shared card/section container (facelift plan §6, Stage 1).
 *
 * Replaces the near-identical `section{...}`/`.list{...}` CSS that Overview,
 * Scenes, Fixtures, Discovery and Diagnostics each hand-rolled separately
 * (compare the pre-facelift `section` rules in views/overview.js with the
 * `.list` rules in views/scenes.js, views/fixtures.js, views/discovery.js
 * and the `.feed` rule in views/diagnostics.js — all four were the same
 * `background/border/border-radius/overflow` declaration). Purely a visual
 * no-op: every call site renders byte-identical CSS to what it replaced.
 *
 * @prop {string} heading optional uppercase section label (Overview-style
 *   `<h3>`); omit for list-style panels that don't have one.
 * @prop {"padded"|"list"} variant "padded": bordered card with internal
 *   padding (Overview sections). "list": edge-to-edge container for row
 *   children (Scenes/Fixtures/Discovery lists, Diagnostics feed) — no
 *   internal padding.
 * @slot default panel content
 */
import { LitElement, html, css } from "lit";

export class SsPanel extends LitElement {
  static properties = {
    heading: { type: String },
    // reflect: true so :host([variant="padded"]) matches whether the
    // consumer sets the attribute directly or binds the property.
    variant: { type: String, reflect: true },
  };

  static styles = css`
    :host {
      display: block;
      background: var(--ss-surface);
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
      overflow: hidden;
    }
    :host([variant="padded"]) {
      padding: 8px 12px;
    }
    h3 {
      margin: 0 0 4px;
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
    }
    /* Keep list rows edge-to-edge while aligning an optional label with
       their content (notably Overview's Playback panel). Horizontal inset
       matches the shared list-row padding (10px) so the label lines up
       with the row text (browser_regression.mjs pins this at <=1px). */
    :host([variant="list"]) h3 {
      padding: 6px 10px 0;
    }
  `;

  constructor() {
    super();
    this.heading = "";
    this.variant = "padded";
  }

  render() {
    return html`
      ${this.heading ? html`<h3>${this.heading}</h3>` : ""}
      <slot></slot>
    `;
  }
}

customElements.define("ss-panel", SsPanel);
