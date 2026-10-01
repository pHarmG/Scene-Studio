/**
 * <ss-action-menu> — a row of action buttons, given fully-resolved items
 * (facelift plan §6, Stage 1).
 *
 * Deliberately dumb: it knows nothing about commands, policy gating, or
 * `allowedCommands` — the caller resolves each item's `disabled`/`title`
 * (e.g. against backend-declared `runtime.allowed_commands`, per
 * `ss-scene-row`'s existing `#available()`/`#commandFor()` mapping) and
 * passes plain `{key, label, disabled?, title?, onClick}` objects. This
 * keeps the primitive reusable across call sites whose action-to-command
 * mapping differs, without duplicating that mapping logic here.
 *
 * Stage-1 scope: wired into <ss-scene-row> only, using the `dense` variant
 * that reproduces its previous inline `.actions button` CSS exactly (no
 * visual change). <ss-inspector>'s action buttons are NOT migrated in this
 * stage — see the facelift Stage-1 notes: they currently render as
 * unstyled native buttons (the shared `.ss-btn` class in base.css is a
 * document-level stylesheet and never penetrates Lit shadow DOM), a
 * pre-existing bug independent of this change, left for a deliberate
 * follow-up rather than silently fixed as a side effect here.
 *
 * @prop {{key:string, label:string, disabled?:boolean, title?:string,
 *         ariaLabel?:string, onClick:(e:Event)=>void}[]} items
 * @prop {boolean} dense compact sizing matching <ss-scene-row>'s prior
 *   inline button CSS (11px text, 1px/6px padding, 3px radius). Omit for
 *   the (currently unused) default/larger sizing reserved for future
 *   non-row call sites.
 */
import { LitElement, html, css } from "lit";

export class SsActionMenu extends LitElement {
  static properties = {
    items: { attribute: false },
    dense: { type: Boolean, reflect: true },
  };

  static styles = css`
    :host {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    :host([dense]) {
      flex-wrap: nowrap;
    }
    button {
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text);
      border-radius: var(--ss-radius-sm);
      padding: 8px 14px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
    }
    button:hover {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    button:disabled {
      opacity: 0.45;
      cursor: default;
    }
    /* Square in-row glyph buttons (apply/play/overflow on list rows). Size
       rides the --ss-row-btn touch token: 32px desktop, 40px on
       coarse-pointer/mobile, so the scene row's fixed --ss-actions-col
       (108px desktop / 136px touch) always fits all three. */
    :host([dense]) button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: var(--ss-row-btn);
      height: var(--ss-row-btn);
      min-height: 0;
      background: transparent;
      color: var(--ss-text-dim);
      font-weight: 400;
      font-size: 13px;
      border-radius: var(--ss-radius-sm);
      padding: 0;
    }
    :host([dense]) button:hover {
      color: var(--ss-text);
    }
  `;

  constructor() {
    super();
    this.items = [];
    this.dense = false;
  }

  render() {
    return html`
      ${(this.items || []).map(
        (item) => html`
          <button
            ?disabled=${!!item.disabled}
            title=${item.title || ""}
            aria-label=${item.ariaLabel || item.label || ""}
            @click=${item.onClick}
          >
            ${item.label}
          </button>
        `
      )}
    `;
  }
}

customElements.define("ss-action-menu", SsActionMenu);
