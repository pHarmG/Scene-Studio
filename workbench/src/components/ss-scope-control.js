/**
 * <ss-scope-control> — condenses canonical target/group memberships
 * (`"office · whole_house"`) into one compact tap/click control (live
 * fixture color-state pass, plan §8). Used on both fixture rows
 * (`fixture.groups`) and scene rows (`scene.target_ids`).
 *
 * These are canonical target/group memberships, not necessarily literal
 * physical locations — this never claims to be "Location". The primary
 * label is the first meaningful group excluding the broad `whole_house`
 * catch-all (falling back to the first group, then "Ungrouped"); any
 * additional memberships collapse into a `+N` suffix instead of
 * concatenating every name, freeing the horizontal space the plan wants
 * back for the name, swatch, and (on fixtures) the state aura.
 *
 * Same native Popover API pattern as <ss-capability-indicators> (top-layer,
 * so it's never clipped by row/list `overflow: hidden`); the button stops
 * click propagation so opening it never also selects the row.
 *
 * @prop {string[]} groups canonical ids (fixture.groups or scene.target_ids), unhumanized
 */
import { LitElement, html, css } from "lit";

const BROAD_GROUP = "whole_house";
let seq = 0;

/** `whole_house` -> "Whole House"; canonical ids are never mutated, only displayed. */
export function humanizeGroupId(id) {
  return String(id || "")
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/**
 * @param {string[]} groups
 * @returns {{primary: string, rest: string[]}} `primary` humanized; `rest`
 *   is every OTHER membership (also humanized), in original order.
 */
export function resolveScope(groups) {
  const list = (groups || []).filter(Boolean);
  if (!list.length) return { primary: "Ungrouped", rest: [] };
  const primaryId = list.find((g) => g !== BROAD_GROUP) || list[0];
  const rest = list.filter((g) => g !== primaryId).map(humanizeGroupId);
  return { primary: humanizeGroupId(primaryId), rest };
}

export class SsScopeControl extends LitElement {
  static properties = {
    groups: { attribute: false },
  };

  static styles = css`
    :host {
      display: inline-flex;
      min-width: 0;
    }
    .scope {
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      max-width: 100%;
      min-width: 0;
      padding: 3px 8px;
      min-height: 26px;
      box-sizing: border-box;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface-2);
      color: var(--ss-text-dim);
      font: inherit;
      font-size: 13px;
      cursor: pointer;
    }
    .scope:hover {
      background: color-mix(in srgb, var(--ss-surface-2) 70%, var(--ss-text) 12%);
      color: var(--ss-text);
    }
    .scope:focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 1px;
    }
    .label {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .count {
      flex: none;
      color: var(--ss-text-faint);
      font-weight: 600;
    }
    /* Touch/mobile: the pill is a real tap control (opens the scope
       popover), so it grows with the touch tokens instead of staying a
       desktop-sized chip. Same query as the token block in tokens.css. */
    @media (max-width: 640px), (pointer: coarse) {
      .scope {
        padding: 6px 10px;
        min-height: 36px;
        font-size: 15px;
        gap: 6px;
      }
    }
    .tip {
      margin: 0;
      padding: 8px 10px;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface-2);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      max-width: 220px;
      font-size: 13px;
      line-height: 1.5;
      color: var(--ss-text-dim);
    }
    .tip:popover-open {
      display: block;
    }
    .tip strong {
      display: block;
      color: var(--ss-text);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.03em;
      margin-bottom: 4px;
    }
    .tip ul {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .tip li {
      padding: 1px 0;
    }
  `;

  constructor() {
    super();
    this.groups = [];
    this._uid = `ss-scope-${++seq}`;
  }

  #onBeforeToggle(e) {
    if (e.newState !== "open") return;
    const popover = e.target;
    const trigger = popover.previousElementSibling;
    if (!trigger) return;
    const r = trigger.getBoundingClientRect();
    const maxLeft = Math.max(4, window.innerWidth - 232);
    popover.style.position = "fixed";
    popover.style.top = `${r.bottom + 6}px`;
    popover.style.left = `${Math.max(4, Math.min(r.left, maxLeft))}px`;
  }

  render() {
    const { primary, rest } = resolveScope(this.groups);
    const all = [primary, ...rest];
    const accessibleName = `Scope: ${all.join(", ")}`;
    const popId = `${this._uid}-tip`;
    return html`
      <button
        type="button"
        class="scope"
        popovertarget=${popId}
        title=${accessibleName}
        aria-label=${accessibleName}
        @click=${(e) => e.stopPropagation()}
      >
        <span class="label">${primary}</span>
        ${rest.length ? html`<span class="count">+${rest.length}</span>` : ""}
      </button>
      <div id=${popId} popover class="tip" @beforetoggle=${this.#onBeforeToggle} role="status">
        <strong>Scope</strong>
        <ul>
          ${all.map((name) => html`<li>${name}</li>`)}
        </ul>
      </div>
    `;
  }
}

customElements.define("ss-scope-control", SsScopeControl);
