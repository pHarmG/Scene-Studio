/**
 * <ss-overflow-menu> — a real "…" action menu (post-R5 Workbench visual
 * polish, completing the scene-row overflow item deferred from Stage 3+ —
 * see docs/scene_studio/WORKBENCH_STAGE3_DESIGN_HANDOFF.md Deviations).
 *
 * Built on the native Popover API (`popover`/`popovertarget`) specifically
 * because every list this menu lives in (<ss-panel variant="list">) sets
 * `overflow: hidden` on its host — a normal absolutely-positioned dropdown
 * would get clipped by the row list. A top-layer popover is immune to
 * ancestor overflow/clipping, so it needs no changes to ss-panel or any
 * list container. Position is computed from the trigger's own bounding
 * rect on open (`beforetoggle`) — plain fixed-position math, no CSS anchor
 * positioning dependency.
 *
 * Deliberately dumb, same contract as <ss-action-menu>: the caller resolves
 * each item's `disabled`/`title`/`onClick` (policy gating, command mapping)
 * and hands over plain objects — this component only renders them and
 * manages the popover/keyboard/focus mechanics.
 *
 * Keyboard: Tab/Shift+Tab and Enter/Space work via native `<button>`
 * semantics; ArrowUp/ArrowDown/Home/End additionally roam focus among
 * enabled items (`role="menu"`/`"menuitem"`); Escape and outside-click
 * close it (native `popover="auto"` light-dismiss); opening focuses the
 * first enabled item, closing returns focus to the trigger.
 *
 * @prop {{key:string, label:string, icon?, disabled?:boolean, title?:string,
 *         ariaLabel?:string, onClick:(e:Event)=>void}[]} items
 * @prop {string} [triggerLabel] accessible name for the trigger (default
 *   "More actions")
 */
import { LitElement, html, css } from "lit";
import { iconMore } from "./icons.js";

let seq = 0;

export class SsOverflowMenu extends LitElement {
  static properties = {
    items: { attribute: false },
    triggerLabel: { type: String },
  };

  static styles = css`
    :host {
      display: inline-flex;
    }
    .trigger {
      appearance: none;
      width: var(--ss-row-btn);
      height: var(--ss-row-btn);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--ss-border);
      background: transparent;
      color: var(--ss-text-dim);
      border-radius: var(--ss-radius-sm);
      cursor: pointer;
    }
    .trigger:hover,
    .trigger:focus-visible {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    .trigger:popover-open {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    [popover] {
      margin: 0;
      padding: 6px;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius);
      background: var(--ss-surface-2);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      min-width: 200px;
      max-width: 280px;
    }
    [popover]:popover-open {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .item {
      appearance: none;
      border: none;
      background: none;
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      padding: 8px 10px;
      border-radius: 4px;
      font-size: 15px;
      font-family: inherit;
      color: var(--ss-text);
      text-align: left;
      cursor: pointer;
    }
    .item:hover:not(:disabled),
    .item:focus-visible {
      background: var(--ss-surface);
      outline: none;
    }
    .item:disabled {
      color: var(--ss-text-faint);
      cursor: default;
    }
    .item .ic {
      display: flex;
      color: var(--ss-text-dim);
      flex: none;
    }
  `;

  constructor() {
    super();
    this.items = [];
    this.triggerLabel = "More actions";
    this._id = `ss-om-${++seq}`;
  }

  #position(popover) {
    const trigger = this.renderRoot.querySelector(".trigger");
    if (!trigger) return;
    const r = trigger.getBoundingClientRect();
    popover.style.position = "fixed";
    popover.style.top = `${r.bottom + 4}px`;
    popover.style.right = `${Math.max(4, window.innerWidth - r.right)}px`;
    popover.style.left = "auto";
  }

  #onBeforeToggle(e) {
    if (e.newState === "open") {
      this.#position(e.target);
      requestAnimationFrame(() => {
        const first = this.renderRoot.querySelector(".item:not(:disabled)");
        if (first) first.focus();
      });
    } else {
      const trigger = this.renderRoot.querySelector(".trigger");
      if (trigger) trigger.focus();
    }
  }

  #onKeydown(e) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const items = [...this.renderRoot.querySelectorAll(".item:not(:disabled)")];
    if (!items.length) return;
    const active = this.renderRoot.activeElement;
    const idx = items.indexOf(active);
    e.preventDefault();
    if (e.key === "ArrowDown") (items[idx + 1] || items[0]).focus();
    else if (e.key === "ArrowUp") (items[idx - 1] || items[items.length - 1]).focus();
    else if (e.key === "Home") items[0].focus();
    else if (e.key === "End") items[items.length - 1].focus();
  }

  render() {
    return html`
      <button
        class="trigger"
        popovertarget=${this._id}
        title=${this.triggerLabel}
        aria-label=${this.triggerLabel}
        aria-haspopup="menu"
        @click=${(e) => e.stopPropagation()}
      >
        ${iconMore(17)}
      </button>
      <div
        id=${this._id}
        popover
        role="menu"
        @beforetoggle=${this.#onBeforeToggle}
        @keydown=${this.#onKeydown}
      >
        ${(this.items || []).map(
          (item) => html`
            <button
              class="item"
              role="menuitem"
              ?disabled=${!!item.disabled}
              title=${item.title || ""}
              aria-label=${item.ariaLabel || item.label || ""}
              popovertarget=${this._id}
              popovertargetaction="hide"
              @click=${item.onClick}
            >
              ${item.icon ? html`<span class="ic">${item.icon}</span>` : ""}
              <span>${item.label}</span>
            </button>
          `
        )}
      </div>
    `;
  }
}

customElements.define("ss-overflow-menu", SsOverflowMenu);
