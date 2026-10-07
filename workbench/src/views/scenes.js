/**
 * <ss-view-scenes> — compact scene rows (§10.6): name, targets, palette
 * swatches, static/dynamic, readiness/fidelity summary, and actions
 * (apply / dry run / play-or-playback-focus / rename / archive / restore).
 * All actions go through the store command layer.
 *
 * R5C: the catalog stays a catalog; runtime playback instances live in the
 * reusable <ss-playback-panel> rendered above it whenever at least one
 * live (active/paused/orphaned) session exists. Scene rows carry a compact
 * plural session summary and never route lifecycle controls by implicitly
 * picking "the first" session — the panel is the canonical control surface
 * (pause/resume/stop are session-addressed there).
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-scene-row.js";
import "../components/ss-panel.js";
import "../components/ss-empty-state.js";
import "../components/ss-playback-panel.js";
import { iconChevronRight, iconRefresh } from "../components/icons.js";
import { sceneLookSwatches } from "../scene_look.js";
import { normalizePlayback, sessionsForScene, summarizeSession } from "../playback.js";
import { playbackActionEnvelope, resolveTargetFixtures, currentSceneView } from "../state.js";

export class SsViewScenes extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-scenes) {
    :scope {
      display: block;
    }
    .heading {
      color: var(--ss-text-faint);
      font-size: 14px;
      margin: 0 0 6px 8px;
    }
    .heading-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 6px;
    }
    .heading-row .heading {
      margin: 0 0 0 8px;
      margin-right: auto;
    }
    .heading-row #scenes-refresh-fidelity {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    ss-playback-panel {
      margin: 0 0 10px;
    }
    /* Stage 3+ (facelift plan §4.3): archived scenes move out of the daily
       flat list into a collapsed disclosure — restoring one is still a
       single click after expanding, but the primary list is no longer
       padded with reference material. */
    .archived-disclosure {
      margin-top: 12px;
    }
    .archived-disclosure > summary {
      cursor: pointer;
      list-style: none;
      color: var(--ss-text-faint);
      font-size: 15px;
      font-weight: 600;
      padding: 10px 10px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .archived-disclosure > summary::-webkit-details-marker {
      display: none;
    }
    .archived-disclosure > summary:hover {
      color: var(--ss-text-dim);
    }
    .archived-disclosure .chev {
      display: inline-flex;
      transition: transform 120ms ease;
    }
    .archived-disclosure[open] .chev {
      transform: rotate(90deg);
    }
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._unsub = null;
    // Cross-navigation focus (R5C §8): which playback selection was already
    // scrolled to, so a highlight doesn't re-scroll on every unrelated
    // re-render. Cleared when the selection changes to a different session.
    this._scrolledSelectionId = null;
    // Scene-row "N sessions" focus: transient scene-scoped highlight +
    // one-shot panel scroll (not a store selection — multiple sessions of
    // one scene highlight together).
    this._panelFocusSceneId = null;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.store) this._unsub = this.store.subscribe(() => this.requestUpdate());
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
    super.disconnectedCallback();
  }

  #summary(scene) {
    const s = this.store.state;
    const fixtures = (s.fixtures && s.fixtures.fixtures) || [];
    const declaredTargetIds = ((s.fixtures && s.fixtures.targets) || []).map((t) => t.id);
    const counts = { total: 0, ready: 0, missing: 0, disabled: 0, degraded: 0 };
    const dynamicEligible = { capable: 0, total: 0 };
    const seen = new Set();
    for (const targetId of scene.target_ids || []) {
      for (const fixture of resolveTargetFixtures(fixtures, targetId, { declaredTargetIds })) {
        if (seen.has(fixture.id)) continue;
        seen.add(fixture.id);
        counts.total += 1;
        // Both clients attach derived health (contract §1) to every fixture.
        if (counts[fixture.health] !== undefined) counts[fixture.health] += 1;
        // "Upgrade to dynamic" gate: same capability field the Builder
        // already reads for its color-support ratio (capabilitySummary),
        // counted here instead for dynamic_native.
        dynamicEligible.total += 1;
        if (fixture.capabilities && fixture.capabilities.dynamic_native) dynamicEligible.capable += 1;
      }
    }
    counts.skipped = counts.missing + counts.disabled;
    // Backend-authoritative fidelity from the store's `scene.preview` cache
    // (Builder-expansion §8). No golden/sample data is production truth: a
    // scene without a successful preview reports `null` and the row says so,
    // with the backend's own failure reason available as a tooltip.
    const cached = this.store.sceneFidelityFor(scene.id);
    const fidelity = cached && cached.status === "ok" ? cached.fidelity : null;
    return {
      counts,
      fidelity,
      fidelityUnavailable: !!(cached && cached.status === "unavailable"),
      fidelityReason: cached && cached.status === "unavailable" ? cached.reason : null,
      dynamic: !!scene.motion && scene.motion.mode !== "static",
      mode: scene.motion ? scene.motion.mode : "static",
      dynamicEligible,
    };
  }

  /**
   * Compact plural runtime summary for a scene row (R5C §7): the row speaks
   * in states/counts, never in a single opaque session object.
   * @returns {object|null} {count, states, issueCount, legacy} or null
   */
  #sessionSummary(playback, sceneId) {
    const sessions = sessionsForScene(playback, sceneId);
    if (!sessions.length) return null;
    const issueCount = sessions.reduce((n, s) => n + summarizeSession(s).degraded.length, 0);
    return {
      count: sessions.length,
      states: [...new Set(sessions.map((s) => s.state))],
      issueCount,
      legacy: sessions.every((s) => s.legacy),
    };
  }

  #onSelect(e) {
    this.store.select({ type: "scene", id: e.detail.id });
  }

  /**
   * HA-native routine commands (routines pass) from a row's
   * <ss-routine-popover>, forwarded through the ONE store command seam.
   * The backend owns every write; a `routine_source_changed` conflict is
   * already surfaced by the store as a warn notice telling the user to
   * refresh and reapply.
   */
  #onRoutineAction(e) {
    const { action, scene_id, automation_id, source_digest, time, weekdays, behavior } = e.detail;
    const store = this.store;
    switch (action) {
      case "create":
        return store.sendCommand({ command: "routine.create", scene_id, behavior, time, weekdays });
      case "update":
        return store.sendCommand({
          command: "routine.update", automation_id, source_digest, time, weekdays, behavior,
        });
      case "delete":
        return store.sendCommand({ command: "routine.delete", automation_id, source_digest });
      case "enable":
        return store.sendCommand({ command: "routine.enable", automation_id, source_digest });
      case "disable":
        return store.sendCommand({ command: "routine.disable", automation_id, source_digest });
      default:
        return Promise.resolve();
    }
  }

  #onAction(e) {
    const { action, scene_id, name, session_id } = e.detail;
    const store = this.store;
    switch (action) {
      case "apply":
        return store.sendCommand({ command: "scene.apply", scene_id });
      case "takeover-apply":
        // External light sync (hyperHDR) is holding fixtures: explicit
        // one-shot takeover — suspend the overlapping sync instance(s),
        // then apply. Default Apply above stays yield-first.
        return store.sendCommand({
          command: "scene.apply",
          scene_id,
          contention_override: "takeover",
        });
      case "preview":
        return store.sendCommand({ command: "scene.preview", scene_id });
      case "play":
        return store.sendCommand({ command: "playback.start", scene_id });
      case "edit":
        // Builder Pass 2: edit converges into the Scenes workflow via the
        // canonical authoring commands — never a second product surface.
        return store.openBuilder({ sceneId: scene_id });
      case "duplicate":
        // Builder-expansion §4: Duplicate / Save-as-New opens the SAME
        // Builder with the source intent and no source identity; the
        // backend derives the new id and records the provenance.
        return store.openBuilder({ sceneId: scene_id, mode: "duplicate" });
      case "upgrade-dynamic":
        // Converges into the same "never a second product surface" rule as
        // edit/duplicate: open the existing Builder pre-switched to Dynamic
        // motion instead of collecting a speed in a bespoke row dialog.
        return store.openBuilder({ sceneId: scene_id, mode: "upgrade" });
      case "playback-focus":
        // R5C §7: a scene with live sessions gets an unambiguous "focus the
        // session panel" affordance instead of row-level Pause/Stop that
        // would implicitly pick one session of possibly many.
        this._panelFocusSceneId = scene_id;
        this.requestUpdate();
        return Promise.resolve();
      case "archive":
        return store.sendCommand({ command: "scene.archive", scene_id });
      case "restore":
        return store.sendCommand({ command: "scene.restore", scene_id });
      case "rename":
        return store.sendCommand({ command: "scene.rename", scene_id, name });
      default:
        return Promise.resolve();
    }
  }

  /**
   * Session-addressed lifecycle commands from the Live playback panel,
   * routed through the ONE shared seam (playbackActionEnvelope -> store
   * sendCommand) — the same routing Overview uses.
   */
  #onPlaybackAction(e) {
    const { action, session_id } = e.detail;
    const envelope = playbackActionEnvelope(action, session_id);
    return envelope ? this.store.sendCommand(envelope) : Promise.resolve();
  }

  updated() {
    // Scenario switches reset the fixture/session world — a stale focus
    // highlight must not light the wrong sessions afterwards.
    if (this._lastScenarioId !== undefined && this._lastScenarioId !== this.store.state.scenarioId) {
      this._panelHighlightSceneId = null;
      this._scrolledSelectionId = null;
    }
    this._lastScenarioId = this.store.state.scenarioId;
    // Cross-navigation scroll-into-view (R5C §6/§8): an exception "View"
    // (store selection {type:"playback"}) or a scene-row "N sessions"
    // focus lands on the Live playback panel once per focus change.
    const sel = this.store.state.selection;
    if (sel && sel.type === "playback" && this._scrolledSelectionId !== sel.id) {
      this._scrolledSelectionId = sel.id;
      this._panelHighlightSceneId = null;
      this.#scrollPanelIntoView();
    } else if (this._panelFocusSceneId) {
      // Apply the scene-scoped highlight, then re-render so the panel
      // receives it (updated() runs after render — a plain field write
      // here would only show up on the NEXT unrelated render).
      this._panelHighlightSceneId = this._panelFocusSceneId;
      this._panelFocusSceneId = null;
      this.requestUpdate();
      this.#scrollPanelIntoView();
    }
  }

  #scrollPanelIntoView() {
    const panel = this.renderRoot.querySelector("ss-playback-panel");
    if (panel) panel.scrollIntoView({ block: "nearest" });
  }

  #renderRow(scene, { archived, st, current, playback, sel, routineDoc }) {
    const contention = st ? st.contention : null;
    return html`
      <ss-scene-row
        .allowedCommands=${st && st.runtime ? st.runtime.allowed_commands : null}
        role="listitem"
        .scene=${scene}
        .summary=${this.#summary(scene)}
        .archived=${archived}
        .active=${!!current && current.scene_id === scene.id}
        .sessionSummary=${this.#sessionSummary(playback, scene.id)}
        .selected=${!!sel && sel.type === "scene" && sel.id === scene.id}
        .contentionActive=${!!contention && (contention.held_fixture_ids || []).length > 0}
        .routines=${this.store.routinesForScene(scene.id)}
        .routinesAvailable=${routineDoc ? !!routineDoc.available : false}
        .routinesReason=${routineDoc && routineDoc.unavailable_reason ? routineDoc.unavailable_reason : null}
        @select-scene=${this.#onSelect}
        @scene-action=${this.#onAction}
        @routine-action=${this.#onRoutineAction}
      ></ss-scene-row>
    `;
  }

  render() {
    const s = this.store.state;
    if (!s.scenes) return html`<ss-empty-state>Loading…</ss-empty-state>`;
    const st = s.status;
    // R5A collection shape (R5C playback.js) — never read st.playback
    // directly, an empty {sessions:[],...} is truthy.
    const playback = normalizePlayback(st ? st.playback : null);
    const current = st ? st.current : null;
    const isArchived = (x) => !!(x.metadata && x.metadata.archived_at);
    const activeScenes = s.scenes.scenes.filter((x) => !isArchived(x));
    const archivedScenes = s.scenes.scenes.filter(isArchived);
    const sel = s.selection;
    const scenePalettes = Object.fromEntries(s.scenes.scenes.map((sc) => [sc.id, sceneLookSwatches(sc)]));
    // Applied static scene (status `current`) for the live panel's idle row.
    const currentRow = currentSceneView(
      current,
      {
        scenes: s.scenes.scenes,
        fixtures: (s.fixtures && s.fixtures.fixtures) || [],
        targets: (s.fixtures && s.fixtures.targets) || [],
      },
      scenePalettes
    );
    const fixtureNames = Object.fromEntries(
      (s.fixtures ? s.fixtures.fixtures : []).map((f) => [f.id, f.name])
    );
    // Highlight resolution: exception-driven session focus wins; otherwise
    // a transient scene-row focus highlights that scene's sessions.
    const highlightSessionId = sel && sel.type === "playback" ? sel.id : "";
    const highlightSceneId =
      !highlightSessionId && this._panelHighlightSceneId ? this._panelHighlightSceneId : "";
    // Derived HA routine projection (routines pass) — read-only view data
    // passed through to each row's schedule popover.
    const routineDoc = s.routines;
    return html`
      <!-- Same cockpit header as Overview, always present (idle included)
           so the page layout does not jump when playback starts/stops. -->
      <ss-playback-panel
        heading="Live playback"
        heroFirst
        .playback=${playback}
        .allowedCommands=${st && st.runtime ? st.runtime.allowed_commands : null}
        .fixtureNames=${fixtureNames}
        .scenePalettes=${scenePalettes}
        .current=${currentRow}
        .highlightSessionId=${highlightSessionId}
        .highlightSceneId=${highlightSceneId}
        .stoppedHistory=${true}
        @playback-action=${this.#onPlaybackAction}
      ></ss-playback-panel>
      <div class="heading-row">
        <div class="heading">Row actions apply immediately.</div>
        <button
          id="scenes-refresh-fidelity"
          class="ss-btn"
          title="Re-read every active scene's render plan from the backend"
          ?disabled=${!this.store.commandAllowed("scene.preview")}
          @click=${() => this.store.refreshSceneFidelity({ force: true })}
        >
          ${iconRefresh(14)} Refresh
        </button>
        <button
          id="new-scene"
          class="ss-btn"
          title=${this.store.commandAllowed("scene.preview_draft") || this.store.commandAllowed("scene.create")
            ? "Create a new scene in the Builder"
            : "Creating scenes is blocked in the current backend runtime mode"}
          ?disabled=${!this.store.commandAllowed("scene.preview_draft") && !this.store.commandAllowed("scene.create")}
          @click=${() => this.store.openBuilder({})}
        >
          + New Scene
        </button>
      </div>
      <ss-panel variant="list" role="list">
        ${activeScenes.map((scene) => this.#renderRow(scene, { archived: false, st, current, playback, sel, routineDoc }))}
      </ss-panel>
      ${archivedScenes.length
        ? html`
            <details class="archived-disclosure">
              <summary>
                <span class="chev">${iconChevronRight(14)}</span>
                Archived (${archivedScenes.length})
              </summary>
              <ss-panel variant="list" role="list">
                ${archivedScenes.map((scene) => this.#renderRow(scene, { archived: true, st, current, playback, sel, routineDoc }))}
              </ss-panel>
            </details>
          `
        : ""}
    `;
  }
}

customElements.define("ss-view-scenes", SsViewScenes);
