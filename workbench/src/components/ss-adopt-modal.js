/**
 * <ss-adopt-modal> — the adoption surface for recognized legacy HA
 * automations (adopt pass).
 *
 * Rendered by the app shell ONLY while the routine projection carries
 * `adoptable` entries (the recognized `script.scene_studio_apply` wrapper
 * era): a header icon prompt opens this modal, so the feature is entirely
 * absent when there is nothing to adopt. Lists each candidate with its
 * discovered schedule and scene link, explains exactly what adoption
 * changes, and performs the conversion through the ONE store command seam
 * (`routine.adopt` — read-first/source_digest concurrency and
 * verify-after-write live in the backend; conflicts surface as store
 * notices). The list is derived live from store state, so a successful
 * adopt makes the entry disappear; adopting the last one closes the modal.
 *
 * Pure presentation + command routing: it never calls backend clients.
 *
 * @prop {object} store the shared store handle (state + sendCommand)
 * @fires adopt-close when the user dismisses the modal (or it empties out)
 */
import { LitElement, html, css } from "lit";
import { describeRoutineSchedule } from "../routines.js";

export class SsAdoptModal extends LitElement {
  static properties = {
    store: { attribute: false },
    // automation_id currently being adopted (button spin-lock, one at a time)
    _busyId: { state: true },
  };

  static styles = css`
    :host {
      position: fixed;
      inset: 0;
      z-index: 90;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 8vh 16px 16px;
      background: rgba(0, 0, 0, 0.55);
    }
    .card {
      width: min(560px, 100%);
      max-height: 80vh;
      overflow: auto;
      background: var(--ss-surface);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-lg);
      box-shadow: 0 18px 50px rgba(0, 0, 0, 0.45);
      padding: 18px 20px;
      box-sizing: border-box;
    }
    .head {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 4px;
    }
    h2 {
      margin: 0;
      font-size: 17px;
      font-weight: 700;
      flex: 1;
    }
    .close {
      appearance: none;
      background: transparent;
      border: none;
      color: var(--ss-text-faint);
      font-size: 15px;
      cursor: pointer;
      padding: 6px 8px;
      border-radius: var(--ss-radius-sm);
    }
    .close:hover {
      color: var(--ss-text);
      background: var(--ss-surface-2);
    }
    .intro {
      color: var(--ss-text-faint);
      font-size: 13px;
      margin: 0 0 12px;
    }
    .item {
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius-md, 10px);
      padding: 10px 12px;
      margin-bottom: 10px;
    }
    .item .when {
      font-size: var(--ss-size-13);
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--ss-accent);
    }
    .item .alias {
      font-size: var(--ss-size-15);
      font-weight: 600;
      margin: 3px 0 2px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .item .detail {
      color: var(--ss-text-faint);
      font-size: 13px;
    }
    .item .reasons {
      margin: 6px 0 0;
      padding: 0;
      list-style: none;
      color: var(--ss-text-dim);
      font-size: 12px;
    }
    .item .reasons li {
      padding: 1px 0;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
      flex-wrap: wrap;
    }
    .actions .spacer {
      flex: 1;
    }
    .what {
      color: var(--ss-text-faint);
      font-size: 12px;
      margin: 10px 0 0;
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._busyId = "";
  }

  #adoptables() {
    const s = this.store ? this.store.state : null;
    return ((s && s.routines && s.routines.routines) || []).filter((r) => r.adoptable === true);
  }

  #sceneName(sceneId) {
    const s = this.store ? this.store.state : null;
    const scene = s && s.scenes ? s.scenes.scenes.find((sc) => sc.id === sceneId) : null;
    return scene ? scene.name : sceneId;
  }

  #close() {
    this.dispatchEvent(new CustomEvent("adopt-close", { bubbles: true, composed: true }));
  }

  async #adopt(routine) {
    if (this._busyId) return;
    this._busyId = routine.automation_id;
    try {
      await this.store.sendCommand({
        command: "routine.adopt",
        automation_id: routine.automation_id,
        source_digest: routine.source_digest,
      });
    } finally {
      this._busyId = "";
      // The store refreshed its routine projection either way: a success
      // removed this entry (adoptable gone), a failure kept it with a
      // notice explaining why. An emptied list closes the modal.
      if (!this.#adoptables().length) this.#close();
    }
  }

  render() {
    const adoptables = this.#adoptables();
    if (!this.store || !adoptables.length) return html``;
    return html`
      <div
        class="backdrop"
        role="presentation"
        @click=${(e) => {
          if (e.target === e.currentTarget) this.#close();
        }}
      >
        <div class="card" role="dialog" aria-modal="true" aria-label="Adopt Home Assistant automations">
          <div class="head">
            <h2>Found in Home Assistant</h2>
            <button class="close" @click=${this.#close} aria-label="Close adoption dialog">✕</button>
          </div>
          <p class="intro">
            ${adoptables.length === 1
              ? "One Home Assistant automation schedules a Scene Studio scene through the legacy script wrapper."
              : `${adoptables.length} Home Assistant automations schedule Scene Studio scenes through the legacy script wrapper.`}
            They work as-is — adopt them to manage their schedules here.
          </p>
          ${adoptables.map(
            (routine) => html`
              <div class="item" data-ss="adopt-item">
                <div class="when">${routine.schedule ? describeRoutineSchedule(routine.schedule) : "Schedule unreadable"}</div>
                <div class="alias" title=${routine.alias}>${routine.alias || routine.automation_id}</div>
                <div class="detail">Applies ${this.#sceneName(routine.scene_id)}${routine.enabled ? "" : " · currently off"}</div>
                ${(routine.unsupported_reasons || []).length
                  ? html`
                      <ul class="reasons">
                        ${routine.unsupported_reasons.map((reason) => html`<li>${reason}</li>`)}
                      </ul>
                    `
                  : ""}
                <div class="actions">
                  <button
                    class="ss-btn"
                    data-ss="adopt-button"
                    ?disabled=${!!this._busyId}
                    title="Rewrite this automation into Scene Studio's canonical form (schedule is preserved)"
                    @click=${() => this.#adopt(routine)}
                  >
                    Adopt
                  </button>
                </div>
              </div>
            `
          )}
          <p class="what">
            Adopting keeps the automation's id and schedule and rewrites its action to
            Scene Studio's canonical bridge command — the same form the Workbench's own
            schedules use — so it becomes editable here and reads uniformly in Home Assistant.
          </p>
        </div>
      </div>
    `;
  }
}

customElements.define("ss-adopt-modal", SsAdoptModal);
