/**
 * <ss-routine-popover> — the scene row's schedule affordance (routines pass).
 *
 * A compact temporal chip in the row's name cell that opens an anchored,
 * top-layer popover integrated with the row list's visual language — the
 * same native Popover API mechanics as <ss-overflow-menu> (immune to the
 * list panel's `overflow: hidden`; position computed from the trigger's
 * bounding rect on open; Escape/outside-click light-dismiss; focus restore).
 *
 * Progressive disclosure, Home-App-style restraint:
 * - no routines: a subtle clock affordance alone;
 * - one routine: the concise recurrence + time text (`Weekdays 7:30 PM`);
 * - several: `3 routines`.
 *
 * The popover is the editor for the SUPPORTED routine grammar only — time,
 * recurrence, Apply vs Play (offered dynamically: Play exists only for
 * dynamic scenes), enabled/disabled, delete. `recognized_advanced` HA
 * automations are shown read-only, clearly labeled Home-Assistant-managed,
 * with what Scene Studio safely understands about them — they are never
 * flattened or rewritten, and the destructive controls simply do not exist
 * for them here.
 *
 * Emits bubbling `routine-action` CustomEvents
 * (`{action: "create"|"update"|"delete"|"enable"|"disable", scene_id, ...}`)
 * — persistence flows through the view's store command layer; this element
 * never mutates state itself.
 *
 * @prop {object} scene Scene v2 JSON (name + motion drive labels/behavior)
 * @prop {object[]|null} routines this scene's routine projections (null =
 *   routine awareness not loaded yet — the chip renders disabled)
 * @prop {boolean} routinesAvailable whether HA routine awareness is available
 * @prop {string|null} routinesReason why routine awareness is unavailable
 * @prop {string[]|null} allowedCommands backend runtime policy for gating
 */
import { LitElement, html, css } from "lit";
import { iconClock, iconPlus } from "./icons.js";
import {
  ROUTINE_WEEKDAYS,
  describeRoutineSchedule,
  describeRoutineWeekdays,
  formatRoutineTime12h,
  routineRowSummary,
} from "../routines.js";

let seq = 0;

const behaviorWord = (behavior) => (behavior === "play" ? "Plays" : "Applies");

export class SsRoutinePopover extends LitElement {
  static properties = {
    scene: { attribute: false },
    routines: { attribute: false },
    routinesAvailable: { type: Boolean },
    routinesReason: { type: String },
    allowedCommands: { type: Array },
  };

  static styles = css`
    :host {
      display: inline-flex;
      min-width: 0;
    }
    .trigger {
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      height: var(--ss-cap-btn, 24px);
      padding: 0 6px;
      border: 1px solid var(--ss-border-soft);
      background: transparent;
      color: var(--ss-text-faint);
      border-radius: 999px;
      font-family: inherit;
      font-size: 12px;
      line-height: 1;
      cursor: pointer;
      flex: none;
      max-width: 180px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .trigger:hover,
    .trigger:focus-visible {
      color: var(--ss-text-dim);
      border-color: var(--ss-accent);
    }
    .trigger:popover-open {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    .trigger.off {
      opacity: 0.72;
    }
    .trigger .ic {
      display: inline-flex;
      flex: none;
    }
    .trigger:disabled {
      cursor: default;
      opacity: 0.6;
    }
    [popover] {
      margin: 0;
      padding: 10px;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius);
      background: var(--ss-surface-2);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45);
      width: 264px;
      max-width: calc(100vw - 16px);
      box-sizing: border-box;
    }
    [popover]:popover-open {
      display: block;
    }
    .pop-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--ss-text-faint);
      margin: 2px 2px 8px;
      font-weight: 600;
    }
    .routine {
      padding: 6px 6px 8px;
      border-radius: var(--ss-radius-sm);
      background: var(--ss-surface);
      border: 1px solid var(--ss-border-soft);
      margin-bottom: 6px;
    }
    .routine .head {
      display: flex;
      align-items: baseline;
      gap: 6px;
      min-width: 0;
    }
    .routine .when {
      font-weight: 600;
      color: var(--ss-text);
      font-size: 14px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .routine .behavior {
      font-size: 12px;
      color: var(--ss-text-dim);
      flex: none;
    }
    .routine .state {
      margin-left: auto;
      flex: none;
      font-size: 12px;
    }
    .routine .state.on {
      color: var(--ss-ok);
    }
    .routine .state.off {
      color: var(--ss-text-faint);
    }
    .routine .detail {
      margin-top: 2px;
      font-size: 12px;
      color: var(--ss-text-faint);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .routine .controls {
      display: flex;
      gap: 6px;
      margin-top: 8px;
      align-items: center;
    }
    .routine.advanced {
      border-style: dashed;
    }
    .routine.advanced .head .when {
      color: var(--ss-text-dim);
    }
    .advanced-tag {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-warn);
      border: 1px solid var(--ss-warn);
      border-radius: 3px;
      padding: 1px 5px;
      flex: none;
    }
    .reasons {
      margin: 6px 0 0;
      padding-left: 16px;
      color: var(--ss-text-faint);
      font-size: 12px;
    }
    .reasons li {
      margin: 2px 0;
    }
    .ha-note {
      margin-top: 6px;
      font-size: 12px;
      color: var(--ss-text-faint);
    }
    .btn {
      appearance: none;
      border: 1px solid var(--ss-border);
      background: var(--ss-surface-2);
      color: var(--ss-text);
      border-radius: var(--ss-radius-sm);
      font-size: 13px;
      font-weight: 600;
      padding: 4px 10px;
      min-height: 30px;
      cursor: pointer;
      font-family: inherit;
    }
    .btn:hover:not(:disabled) {
      border-color: var(--ss-accent);
    }
    .btn:disabled {
      color: var(--ss-text-faint);
      cursor: default;
    }
    .btn.danger {
      color: var(--ss-err);
    }
    .btn.danger.confirm {
      background: var(--ss-err);
      border-color: var(--ss-err);
      color: #fff;
    }
    .btn.small {
      min-height: 26px;
      padding: 2px 8px;
      font-size: 12px;
    }
    .add-row {
      display: flex;
      justify-content: center;
      margin-top: 2px;
    }
    .add-row .btn {
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .unavailable {
      font-size: 13px;
      color: var(--ss-text-faint);
      padding: 4px 2px 8px;
    }
    /* ---- inline editor ---- */
    .editor {
      display: block;
    }
    .field {
      margin-bottom: 8px;
    }
    .field > label {
      display: block;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--ss-text-faint);
      margin: 0 2px 4px;
      font-weight: 600;
    }
    .time-row {
      display: flex;
      gap: 6px;
      align-items: center;
    }
    input[type="time"] {
      background: var(--ss-bg);
      color: var(--ss-text);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      font-size: 15px;
      font-family: inherit;
      padding: 4px 8px;
      min-height: 32px;
    }
    input[type="time"]:focus {
      outline: none;
      border-color: var(--ss-accent);
    }
    .segment {
      display: inline-flex;
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      overflow: hidden;
    }
    .segment button {
      appearance: none;
      border: none;
      background: transparent;
      color: var(--ss-text-dim);
      font-family: inherit;
      font-size: 13px;
      font-weight: 600;
      padding: 5px 12px;
      min-height: 30px;
      cursor: pointer;
    }
    .segment button.on {
      background: var(--ss-accent);
      color: #fff;
    }
    .segment button:disabled {
      color: var(--ss-text-faint);
      cursor: default;
    }
    .days {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .days button {
      appearance: none;
      border: 1px solid var(--ss-border);
      background: transparent;
      color: var(--ss-text-dim);
      border-radius: 999px;
      font-family: inherit;
      font-size: 12px;
      font-weight: 600;
      padding: 3px 9px;
      min-height: 26px;
      cursor: pointer;
    }
    .days button.on {
      border-color: var(--ss-accent);
      color: var(--ss-text);
      background: color-mix(in srgb, var(--ss-accent) 18%, transparent);
    }
    .days button.every.on {
      border-color: var(--ss-ok);
      color: var(--ss-ok);
      background: color-mix(in srgb, var(--ss-ok) 14%, transparent);
    }
    .summary-line {
      font-size: 13px;
      color: var(--ss-text-dim);
      margin: 2px 2px 8px;
    }
    .summary-line b {
      color: var(--ss-text);
    }
    .editor-actions {
      display: flex;
      justify-content: flex-end;
      gap: 6px;
    }
    .editor-actions .primary {
      background: var(--ss-accent);
      border-color: var(--ss-accent);
      color: #fff;
    }
    /* Compact rows: the chip collapses to the clock glyph alone. The row's
       container query cannot restyle inside this shadow tree (container
       queries do not cross shadow boundaries), so the row sets
       --ss-routine-chip-text-display: none on this element — a custom
       property, which DOES inherit across shadow roots (the same piercing
       strategy as the --ss-* tokens). */
    .trigger .text {
      display: var(--ss-routine-chip-text-display, inline);
    }
  `;

  constructor() {
    super();
    this.scene = {};
    this.routines = null;
    this.routinesAvailable = true;
    this.routinesReason = null;
    this.allowedCommands = null;
    this._id = `ss-rp-${++seq}`;
    this._editor = null; // null | {mode:"create"} | {mode:"edit", routine}
    this._form = null; // {time, weekdays: null|[...], behavior}
    this._confirmDelete = null; // automation_id armed for delete confirmation
  }

  #summary() {
    return routineRowSummary(this.routines);
  }

  #can(command) {
    if (!Array.isArray(this.allowedCommands)) return true;
    return this.allowedCommands.includes(command);
  }

  #emit(action, extra = {}) {
    this.dispatchEvent(
      new CustomEvent("routine-action", {
        detail: { action, scene_id: this.scene.id, ...extra },
        bubbles: true,
        composed: true,
      })
    );
  }

  #position(popover) {
    const trigger = this.renderRoot.querySelector(".trigger");
    if (!trigger) return;
    const r = trigger.getBoundingClientRect();
    popover.style.position = "fixed";
    popover.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 272))}px`;
    popover.style.top = `${r.bottom + 6}px`;
    popover.style.right = "auto";
  }

  #onBeforeToggle(e) {
    if (e.newState === "open") {
      this.#position(e.target);
    } else {
      // Reset transient editor state when the popover light-dismisses.
      this._editor = null;
      this._confirmDelete = null;
    }
  }

  #startCreate(e) {
    e.stopPropagation();
    this._editor = { mode: "create" };
    this._form = { time: "19:30", weekdays: null, behavior: "apply" };
    this.requestUpdate();
  }

  #startEdit(e, routine) {
    e.stopPropagation();
    this._editor = { mode: "edit", routine };
    this._form = {
      time: routine.schedule ? routine.schedule.time : "19:30",
      weekdays: routine.schedule && routine.schedule.weekdays ? [...routine.schedule.weekdays] : null,
      behavior: routine.behavior || "apply",
    };
    this._confirmDelete = null;
    this.requestUpdate();
  }

  #cancelEditor(e) {
    e.stopPropagation();
    this._editor = null;
    this._form = null;
    this.requestUpdate();
  }

  #saveEditor(e) {
    e.stopPropagation();
    if (!this._editor || !this._form) return;
    const mode = this._editor.mode;
    const routine = mode === "edit" ? this._editor.routine : null;
    const form = this._form;
    this._editor = null;
    this._form = null;
    if (mode === "edit" && routine) {
      this.#emit("update", {
        automation_id: routine.automation_id,
        source_digest: routine.source_digest,
        time: form.time,
        weekdays: form.weekdays,
        behavior: form.behavior,
      });
    } else {
      this.#emit("create", { time: form.time, weekdays: form.weekdays, behavior: form.behavior });
    }
    this.requestUpdate();
  }

  #setForm(patch, e) {
    if (e) e.stopPropagation();
    this._form = { ...this._form, ...patch };
    this.requestUpdate();
  }

  #toggleDay(day, e) {
    e.stopPropagation();
    const current = this._form.weekdays;
    if (current === null) {
      this.#setForm({ weekdays: [day] });
      return;
    }
    const next = current.includes(day) ? current.filter((d) => d !== day) : [...current, day];
    this.#setForm({ weekdays: next.length ? next : null });
  }

  #onDelete(e, routine) {
    e.stopPropagation();
    if (this._confirmDelete !== routine.automation_id) {
      this._confirmDelete = routine.automation_id;
      this.requestUpdate();
      return;
    }
    this._confirmDelete = null;
    this.#emit("delete", { automation_id: routine.automation_id, source_digest: routine.source_digest });
  }

  #onToggleEnabled(e, routine) {
    e.stopPropagation();
    this.#emit(routine.enabled ? "disable" : "enable", {
      automation_id: routine.automation_id,
      source_digest: routine.source_digest,
    });
  }

  #formSummary() {
    if (!this._form) return "";
    return `${describeRoutineWeekdays(this._form.weekdays)} ${formatRoutineTime12h(this._form.time)}`;
  }

  #renderEditor() {
    const mode = this._editor.mode;
    const dynamicScene = !!this.scene.motion && this.scene.motion.mode !== "static";
    const command = mode === "create" ? "routine.create" : "routine.update";
    const saveBlocked = !this.#can(command);
    const weekdays = this._form.weekdays;
    return html`
      <div class="editor" @click=${(e) => e.stopPropagation()}>
        <div class="field">
          <label>Time</label>
          <div class="time-row">
            <input
              type="time"
              .value=${this._form.time}
              @click=${(e) => e.stopPropagation()}
              @change=${(e) => this.#setForm({ time: e.target.value || "19:30" }, null)}
              @keydown=${(e) => e.stopPropagation()}
            />
            <span class="summary-line">${this.#formSummary()}</span>
          </div>
        </div>
        <div class="field">
          <label>Days</label>
          <div class="days">
            <button
              class="every ${weekdays === null ? "on" : ""}"
              title="Every day"
              @click=${(e) => this.#setForm({ weekdays: null }, e)}
            >Every day</button>
            ${ROUTINE_WEEKDAYS.map(
              (day) => html`
                <button
                  class="${weekdays !== null && weekdays.includes(day) ? "on" : ""}"
                  title=${day}
                  @click=${(e) => this.#toggleDay(day, e)}
                >${day[0].toUpperCase()}</button>
              `
            )}
          </div>
        </div>
        <div class="field">
          <label>Action</label>
          <div class="segment" role="group" aria-label="Scheduled action">
            <button
              class=${this._form.behavior === "apply" ? "on" : ""}
              title="Apply this scene statically at the scheduled time"
              @click=${(e) => this.#setForm({ behavior: "apply" }, e)}
            >Apply</button>
            <button
              class=${this._form.behavior === "play" ? "on" : ""}
              ?disabled=${!dynamicScene}
              title=${dynamicScene
                ? "Play this scene's motion dynamically at the scheduled time"
                : "Play requires a dynamic scene (this scene is static)"}
              @click=${(e) => this.#setForm({ behavior: "play" }, e)}
            >Play</button>
          </div>
        </div>
        <div class="editor-actions">
          <button class="btn" @click=${this.#cancelEditor}>Cancel</button>
          <button
            class="btn primary"
            ?disabled=${saveBlocked}
            title=${saveBlocked ? "Scheduling is blocked in the current backend mode" : ""}
            @click=${this.#saveEditor}
          >${mode === "create" ? "Add schedule" : "Save"}</button>
        </div>
      </div>
    `;
  }

  #renderRoutine(routine) {
    if (routine.classification === "recognized_advanced") {
      return html`
        <div class="routine advanced">
          <div class="head">
            <span class="advanced-tag">Advanced</span>
            <span class="when">${routine.alias || routine.automation_id}</span>
          </div>
          <div class="detail">
            ${routine.behavior === "play" ? "Plays" : "Applies"} ${routine.scene_id}
            ${routine.enabled ? "" : "· off"}
          </div>
          ${routine.unsupported_reasons && routine.unsupported_reasons.length
            ? html`
                <ul class="reasons">
                  ${routine.unsupported_reasons.map((reason) => html`<li>${reason}</li>`)}
                </ul>
              `
            : ""}
          <div class="ha-note">
            Managed in Home Assistant — Scene Studio reads it but never edits it.
          </div>
        </div>
      `;
    }
    const canMutate = this.#can("routine.update");
    const canDelete = this.#can("routine.delete");
    const canToggle = this.#can(routine.enabled ? "routine.disable" : "routine.enable");
    const confirming = this._confirmDelete === routine.automation_id;
    return html`
      <div class="routine">
        <div class="head">
          <span class="when">${routine.schedule ? describeRoutineSchedule(routine.schedule) : ""}</span>
          <span class="behavior">${behaviorWord(routine.behavior)}</span>
          <span class="state ${routine.enabled ? "on" : "off"}">${routine.enabled ? "On" : "Off"}</span>
        </div>
        <div class="detail">${routine.alias}</div>
        <div class="controls">
          <button
            class="btn small"
            ?disabled=${!canMutate}
            title=${canMutate ? "Edit this schedule" : "Scheduling is blocked in the current backend mode"}
            @click=${(e) => this.#startEdit(e, routine)}
          >Edit</button>
          <button
            class="btn small"
            ?disabled=${!canToggle}
            title=${canToggle ? (routine.enabled ? "Disable in Home Assistant" : "Enable in Home Assistant") : "Blocked in the current backend mode"}
            @click=${(e) => this.#onToggleEnabled(e, routine)}
          >${routine.enabled ? "Turn off" : "Turn on"}</button>
          <button
            class="btn small danger ${confirming ? "confirm" : ""}"
            ?disabled=${!canDelete}
            title=${canDelete ? (confirming ? "Confirm: remove this automation from Home Assistant" : "Delete this schedule") : "Blocked in the current backend mode"}
            @click=${(e) => this.#onDelete(e, routine)}
          >${confirming ? "Confirm delete" : "Delete"}</button>
        </div>
      </div>
    `;
  }

  #renderPopover() {
    if (!this.routinesAvailable) {
      return html`
        <div class="pop-title">Schedule</div>
        <div class="unavailable">
          ${this.routinesReason || "Home Assistant routine awareness is unavailable in this runtime."}
        </div>
      `;
    }
    const routines = Array.isArray(this.routines) ? [...this.routines] : [];
    const sorted = routines.sort((a, b) =>
      (a.schedule ? `0-${a.schedule.time}` : `1-${a.alias || ""}`).localeCompare(
        b.schedule ? `0-${b.schedule.time}` : `1-${b.alias || ""}`
      )
    );
    const canCreate = this.#can("routine.create");
    if (this._editor) {
      return html`
        <div class="pop-title">${this._editor.mode === "create" ? "Add schedule" : "Edit schedule"}</div>
        ${this.#renderEditor()}
      `;
    }
    return html`
      <div class="pop-title">Schedule</div>
      ${sorted.length
        ? sorted.map((routine) => this.#renderRoutine(routine))
        : html`<div class="unavailable">No schedules for this scene yet.</div>`}
      <div class="add-row">
        <button
          class="btn"
          ?disabled=${!canCreate}
          title=${canCreate ? "Add a time schedule for this scene" : "Scheduling is blocked in the current backend mode"}
          @click=${this.#startCreate}
        >${iconPlus(14)} Add schedule</button>
      </div>
    `;
  }

  render() {
    const sum = this.#summary();
    const loaded = sum !== null;
    const disabled = !loaded || !this.routinesAvailable || !this.#can("routine.create");
    const chipTitle = !loaded
      ? "Schedules loading…"
      : !this.routinesAvailable
        ? this.routinesReason || "Routine awareness unavailable"
        : sum.count === 0
          ? "Add a time schedule"
          : `${sum.text} — edit schedules`;
    return html`
      <button
        class="trigger ${loaded && sum.allDisabled ? "off" : ""}"
        popovertarget=${this._id}
        title=${chipTitle}
        aria-label=${`Schedules: ${loaded && this.routinesAvailable ? (sum.count ? sum.text : "none") : "unavailable"}`}
        aria-haspopup="dialog"
        ?disabled=${disabled}
        @click=${(e) => e.stopPropagation()}
      >
        <span class="ic">${iconClock(14)}</span>
        ${loaded && sum.count
          ? html`<span class="text">${sum.text}${sum.hasAdvanced ? "+" : ""}</span>`
          : ""}
      </button>
      <div
        id=${this._id}
        popover
        role="dialog"
        aria-label="Scene schedules"
        @beforetoggle=${this.#onBeforeToggle}
        @click=${(e) => e.stopPropagation()}
      >
        ${this.#renderPopover()}
      </div>
    `;
  }
}

customElements.define("ss-routine-popover", SsRoutinePopover);
