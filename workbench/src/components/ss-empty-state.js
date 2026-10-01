/**
 * <ss-empty-state> — a standalone placeholder message block ("Loading…",
 * "No exceptions…") (facelift plan §6, Stage 1).
 *
 * Stage-1 scope: wired only where the replaced markup was byte-identical
 * CSS (`color: var(--ss-text-faint); padding: 12px;`) — currently the
 * Scenes and Fixtures "Loading…" states. Discovery's empty-state divs use
 * slightly different padding/font-size today (a pre-existing minor
 * inconsistency, not introduced by this component) and are left as a
 * follow-up rather than forced into a slightly-off shared look.
 *
 * @slot default message content
 */
import { LitElement, html, css } from "lit";

export class SsEmptyState extends LitElement {
  static styles = css`
    :host {
      display: block;
      color: var(--ss-text-faint);
      padding: 12px;
    }
  `;

  render() {
    return html`<slot></slot>`;
  }
}

customElements.define("ss-empty-state", SsEmptyState);
