/**
 * <ss-drawer> — generic slide-over / dockable panel shell (facelift plan
 * §6, Stage 2). Deferred out of Stage 1 deliberately: it needed two real
 * call sites to validate against, not one risky migration in isolation
 * (see the Stage-1 handoff notes) — those two call sites land together
 * here: the narrow-layout inspector (generalized from its previous bespoke
 * `position: fixed` CSS in app.js) and the new System drawer (§4.1).
 *
 * Two independent modes, both driven by simple presence/absence in the DOM
 * (the consumer mounts/unmounts the element to open/close it — same
 * pattern the inspector already used via `${descriptor ? html\`...\` : ""}`;
 * no internal open/close animation state in Stage 2):
 *
 * @prop {boolean} docked normal in-flow block at desktop widths (sized by
 *   its parent — e.g. a CSS grid cell), auto-converting to a fixed
 *   right-edge overlay under the existing `max-width: 900px` breakpoint.
 *   This exactly reproduces the inspector's prior width-driven behavior.
 * @prop {boolean} floating ALWAYS a fixed right-edge overlay, at any
 *   viewport width (the System drawer — it's not part of the main content
 *   grid at any width).
 * @prop {boolean} backdrop only meaningful with `floating`: renders a
 *   click-to-dismiss scrim behind the panel and closes on Escape, firing
 *   `drawer-dismiss`. The docked/narrow inspector usage does not set this
 *   — it preserves its prior no-backdrop behavior exactly.
 * @slot default panel content
 */
import { LitElement, html, css } from "lit";

export class SsDrawer extends LitElement {
  static properties = {
    docked: { type: Boolean, reflect: true },
    floating: { type: Boolean, reflect: true },
    backdrop: { type: Boolean, reflect: true },
  };

  static styles = css`
    :host {
      display: block;
    }
    :host([docked]) {
      height: 100%;
      overflow: hidden;
      min-height: 0;
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
    }
    .backdrop {
      display: none;
    }
    .panel {
      display: block;
      height: 100%;
    }
    /* Whatever is slotted in (<ss-inspector>, or the System drawer's own
       content div) needs to actively fill the panel — a plain block child
       doesn't inherit height the way a direct CSS-grid item auto-stretches
       (which is how the inspector got its full-height sizing before this
       component existed and it WAS the grid item itself). */
    ::slotted(*) {
      display: block;
      height: 100%;
      box-sizing: border-box;
    }
    /* floating: always an overlay, any width */
    :host([floating]) {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: flex;
      justify-content: flex-end;
      pointer-events: none;
    }
    :host([floating][backdrop]) .backdrop {
      display: block;
      position: absolute;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
      pointer-events: auto;
    }
    :host([floating]) .panel {
      display: block;
      position: relative;
      width: min(var(--ss-inspector-w), 88vw);
      height: 100%;
      background: var(--ss-surface);
      border-left: 1px solid var(--ss-border);
      box-shadow: -8px 0 24px rgba(0, 0, 0, 0.45);
      overflow-y: auto;
      pointer-events: auto;
    }
    /* docked: overlay only under the narrow-layout breakpoint (verbatim
       values from the inspector's prior app.js media query) */
    @media (max-width: 900px) {
      :host([docked]) {
        position: fixed;
        inset: 0;
        z-index: 20;
        display: flex;
        justify-content: flex-end;
        pointer-events: none;
        border: none;
        border-radius: 0;
        height: auto;
        overflow: visible;
      }
      :host([docked]) .panel {
        display: block;
        position: relative;
        width: min(var(--ss-inspector-w), 88vw);
        height: 100%;
        background: var(--ss-surface);
        border-left: 1px solid var(--ss-border);
        box-shadow: -8px 0 24px rgba(0, 0, 0, 0.45);
        overflow-y: auto;
        pointer-events: auto;
      }
    }
  `;

  // Arrow-function class field, not a private method: a private method is
  // non-writable, so `this.#onKeydown = this.#onKeydown.bind(this)` in the
  // constructor (the first shape this took) throws "Private method
  // '#onKeydown' is not writable" — which aborts custom-element upgrade
  // entirely (the element silently never gets a shadow root, and every
  // :host() style rule then has no effect). A field avoids the rebind.
  #onKeydown = (e) => {
    if (e.key === "Escape") this.#dismiss();
  };

  constructor() {
    super();
    this.docked = false;
    this.floating = false;
    this.backdrop = false;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.floating && this.backdrop) {
      window.addEventListener("keydown", this.#onKeydown);
    }
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this.#onKeydown);
    super.disconnectedCallback();
  }

  #dismiss() {
    this.dispatchEvent(new CustomEvent("drawer-dismiss", { bubbles: true, composed: true }));
  }

  render() {
    return html`
      <div class="backdrop" @click=${this.#dismiss}></div>
      <div class="panel"><slot></slot></div>
    `;
  }
}

customElements.define("ss-drawer", SsDrawer);
