/**
 * Minimal reactive store for the Workbench shell (framework-agnostic, DOM-free).
 *
 * Views receive the store as a Lit property and re-render on subscribe fires.
 * All data access goes through a SceneStudioClient — the in-page mock or the
 * live HTTP client (src/api.js), switchable at runtime via setConnection.
 *
 * Connection model (state.conn):
 *   {mode: "mock"|"live", url: string, status: "idle"|"connecting"|"ok"|"error",
 *    error: string|null, lastPoll: number|null}
 * Live mode polls getStatus() every POLL_INTERVAL_MS (app.js drives the timer
 * via store.poll(); the mock never polls). Polling is cheap: when the engine
 * `revision` is unchanged (see shouldRefetchCatalogs) the heavier
 * fixtures/scenes/discovery/events fetches are skipped entirely (plan §9.4).
 */

import { createHttpSceneStudioClient } from "./api.js";
import { canonicalizeStaticPalette } from "./palette_assign.js";
import { normalizePlayback, sessionById } from "./playback.js";

/** Default live base URL shown in the header connection control. */
export const DEFAULT_LIVE_URL = "http://127.0.0.1:8765";

/** Live-mode status poll cadence (plan §9.4: a few seconds). */
export const POLL_INTERVAL_MS = 5000;

/** Bounded concurrency for per-scene catalog fidelity previews. */
export const FIDELITY_CONCURRENCY = 4;

/**
 * Metadata keys the Builder does not own: scene.update re-attaches them
 * server-side, and the Workbench never offers them for editing. Mirrors
 * engine._SERVER_OWNED_METADATA_KEYS.
 */
export const SERVER_OWNED_METADATA_KEYS = ["archived_at", "migrated_from_v1", "duplicated_from"];

/**
 * Sensible starting draft for a NEW Builder draft (Builder-expansion §2):
 * an explicit `default_state` so a named + targeted scene actually renders
 * something (the canonical resolver skips fixtures with no state at all).
 * `brightness` is the scene-level palette dimming level used by dynamic
 * playback — NOT the per-fixture default (`default_state.brightness`).
 */
export function defaultBuilderDraft() {
  return {
    schema_version: 2,
    id: "",
    name: "",
    target_ids: [],
    palette: [],
    brightness: 60,
    motion: { mode: "static", speed: 0.0, strategy: "auto" },
    default_state: { on: true, brightness: 60 },
  };
}

/** Canonical first-pass default state emitted when the editor needs one. */
export function defaultFixtureState() {
  return { on: true };
}

/**
 * Duplicate / Save-as-New draft (Builder-expansion §4): carry the scene
 * INTENT, drop the source identity, and drop server/history provenance that
 * a brand-new document must not claim (it is neither archived nor the v1
 * migration artifact — the backend records the duplicate link itself when
 * the Save sends `duplicate_of`). Returns a plain Scene v2 payload.
 */
export function duplicateBuilderDraft(source, { nameSuffix = " Copy" } = {}) {
  const draft = structuredClone(source || {});
  delete draft.id;
  const metadata = { ...(draft.metadata || {}) };
  for (const key of SERVER_OWNED_METADATA_KEYS) delete metadata[key];
  if (Object.keys(metadata).length) draft.metadata = metadata;
  else delete draft.metadata;
  const baseName = typeof draft.name === "string" && draft.name.trim() ? draft.name : "Scene";
  draft.name = `${baseName.slice(0, 128 - nameSuffix.length)}${nameSuffix}`;
  return draft;
}

/**
 * UI summary over an existing render plan (Builder-expansion §7). NO new
 * backend fidelity enum: this reads the canonical `fixture_plans` fidelity
 * levels plus `skipped_fixture_ids`/`notes` and reports whether the preview
 * deserves an unqualified green "ready" presentation.
 *
 * quality:
 *   ready                  all plans native/equivalent, nothing skipped
 *   reductions             approximate results, skipped fixtures, or notes
 *   partially_unsupported  at least one fixture reports unsupported
 *   cannot_render          nothing at all would be rendered
 *
 * @param {object|null} renderPlan canonical RenderPlan JSON
 * @returns {{quality: string, tone: "ok"|"warn"|"bad", label: string, planned: number, skipped: number, fidelity: {native:number,equivalent:number,approximate:number,unsupported:number}, issues: {tone:string,text:string}[], providers: Record<string, number>}}
 */
export function summarizePreviewQuality(renderPlan) {
  const fidelity = { native: 0, equivalent: 0, approximate: 0, unsupported: 0 };
  const providers = {};
  const issues = [];
  const plans = (renderPlan && renderPlan.fixture_plans) || [];
  for (const plan of plans) {
    if (fidelity[plan.fidelity] !== undefined) fidelity[plan.fidelity] += 1;
    providers[plan.provider] = (providers[plan.provider] || 0) + 1;
    if (plan.fidelity === "approximate" || plan.fidelity === "unsupported") {
      issues.push({
        tone: plan.fidelity === "unsupported" ? "bad" : "warn",
        text: `${plan.fixture_id}: ${plan.fidelity}${plan.reason ? ` — ${plan.reason}` : ""}`,
      });
    }
  }
  const skipped = ((renderPlan && renderPlan.skipped_fixture_ids) || []).length;
  for (const id of (renderPlan && renderPlan.skipped_fixture_ids) || []) {
    issues.push({ tone: "warn", text: `${id}: skipped (disabled, missing, or unbound)` });
  }
  for (const note of (renderPlan && renderPlan.notes) || []) {
    issues.push({ tone: "warn", text: note });
  }
  const planned = plans.length;
  let quality;
  if (planned === 0) quality = "cannot_render";
  else if (fidelity.unsupported > 0) quality = "partially_unsupported";
  else if (fidelity.approximate > 0 || skipped > 0 || issues.length > 0) quality = "reductions";
  else quality = "ready";
  const meta = {
    ready: { tone: "ok", label: "Preview ready" },
    reductions: { tone: "warn", label: "Preview ready with reductions" },
    partially_unsupported: { tone: "bad", label: "Preview partially unsupported" },
    cannot_render: { tone: "bad", label: "Cannot render this draft" },
  }[quality];
  return { quality, tone: meta.tone, label: meta.label, planned, skipped, fidelity, issues, providers };
}

/**
 * Per-fixture override summary for one fixture state document: which
 * canonical fields the editor owns vs. advanced content it must preserve
 * (gradient/effect/transition/provider extensions).
 */
export function describeFixtureState(state) {
  const doc = state && typeof state === "object" ? state : {};
  const advanced = [];
  if (doc.gradient) advanced.push("gradient");
  if (doc.effect) advanced.push("effect");
  if (doc.transition_ms !== undefined && doc.transition_ms !== null) advanced.push("transition");
  if (doc.provider_ext && Object.keys(doc.provider_ext).length) advanced.push("provider_ext");
  return { advanced };
}

/**
 * Fixture ids a scene's target resolves to, using the canonical contract §1
 * rule. When the caller knows the registry's DECLARED target ids it must pass
 * them: the engine treats a declared target id as authoritative and never
 * falls back to a same-named fixture (`renderers/plan.py` records a note
 * instead), so the Workbench must not either. Without `declaredTargetIds`
 * the legacy "members first, then a single fixture id" behavior is kept.
 */
export function resolveTargetFixtures(fixtures, targetId, { declaredTargetIds = null } = {}) {
  const list = Array.isArray(fixtures) ? fixtures : [];
  const isDeclared = Array.isArray(declaredTargetIds) && declaredTargetIds.includes(targetId);
  if (isDeclared) {
    return list.filter((f) => Array.isArray(f.groups) && f.groups.includes(targetId));
  }
  const byGroup = list.filter((f) => Array.isArray(f.groups) && f.groups.includes(targetId));
  if (byGroup.length > 0) return byGroup;
  const byId = list.find((f) => f.id === targetId);
  return byId ? [byId] : [];
}

/**
 * Readiness string for a target selector option, aggregated from EXISTING
 * fixture health only (Builder-expansion §1): "4/4 ready" for a declared
 * target, the fixture's own health for a single fixture.
 */
export function targetReadiness(fixtures, targetId, { isGroup = false } = {}) {
  if (!isGroup) {
    const fixture = fixtures.find((f) => f.id === targetId);
    if (!fixture) return "";
    return fixture.health === "ready" ? "ready" : fixture.health || "unknown";
  }
  const members = fixtures.filter((f) => Array.isArray(f.groups) && f.groups.includes(targetId));
  if (!members.length) return "no fixtures";
  const ready = members.filter((f) => f.health === "ready").length;
  return `${ready}/${members.length} ready`;
}

/**
 * Describe canonical scene fields the Builder does not fully edit so the UI
 * can warn "preserved unchanged" instead of silently hiding them. Overrides
 * and defaults ARE editable now (Builder-expansion §2/§3) — what stays
 * advanced is their richer per-fixture content (gradient, effect,
 * transition, provider extensions), advanced motion modes/strategies, and
 * non-server metadata.
 */
export function describeAdvancedFields(doc) {
  const fixtureStates =
    doc && doc.fixture_states && typeof doc.fixture_states === "object" ? doc.fixture_states : {};
  const overrideFixtures = Object.keys(fixtureStates);
  const advancedOverrideFixtures = overrideFixtures.filter(
    (id) => describeFixtureState(fixtureStates[id]).advanced.length > 0
  );
  const advancedDefault = doc && doc.default_state ? describeFixtureState(doc.default_state).advanced : [];
  const motion = (doc && doc.motion) || {};
  const advancedMotionMode =
    motion.mode === "effect" ? "effect" : motion.strategy && motion.strategy !== "auto" ? `strategy "${motion.strategy}"` : null;
  const metadata = (doc && doc.metadata) || {};
  const customMetadata = Object.keys(metadata).filter((k) => !SERVER_OWNED_METADATA_KEYS.includes(k));
  const flags = {
    overrideFixtures,
    advancedOverrideFixtures,
    advancedDefault,
    advancedMotionMode,
    customMetadata,
  };
  flags.any =
    advancedOverrideFixtures.length > 0 ||
    advancedDefault.length > 0 ||
    !!flags.advancedMotionMode ||
    flags.customMetadata.length > 0;
  return flags;
}

/**
 * Pure revision-cache decision: should the heavy catalogs be refetched given
 * the previously seen engine revision and the freshly polled one?
 * Unknown/missing revisions always refetch (safe default).
 * @param {number|undefined} previousRevision
 * @param {number|undefined} nextRevision
 * @returns {boolean}
 */
export function shouldRefetchCatalogs(previousRevision, nextRevision) {
  if (typeof previousRevision !== "number" || typeof nextRevision !== "number") return true;
  return previousRevision !== nextRevision;
}

/** Freshness thresholds for a live-state sample (plan §5), tuned for the
 *  ~3s sampling cadence: fresh <= 7s, aging 7-15s, stale > 15s. */
export const LIVE_STATE_FRESH_MS = 7000;
export const LIVE_STATE_AGING_MS = 15000;

/**
 * Classify how trustworthy a live-state sample still is, purely from
 * elapsed time (plan §5: "the most dangerous failure mode is leaving an
 * old color aura displayed indefinitely as if it were still live" — this
 * is what stops that). Never returns anything but "unavailable" for a
 * missing/unparseable timestamp: an unknown age is never treated as fresh.
 * @param {string|null|undefined} sampledAt ISO-8601 timestamp
 * @param {number} [now] ms since epoch, injectable for tests
 * @returns {"fresh"|"aging"|"stale"|"unavailable"}
 */
export function liveStateFreshness(sampledAt, now = Date.now()) {
  if (!sampledAt) return "unavailable";
  const sampledMs = Date.parse(sampledAt);
  if (Number.isNaN(sampledMs)) return "unavailable";
  const age = now - sampledMs;
  if (age < 0) return "fresh"; // clock skew: treat as fresh rather than penalize
  if (age <= LIVE_STATE_FRESH_MS) return "fresh";
  if (age <= LIVE_STATE_AGING_MS) return "aging";
  return "stale";
}

/**
 * @typedef {object} WorkbenchSelection
 * @property {'fixture'|'scene'|'entry'|'event'|'observation'|'playback'} type
 * @property {string} id fixture id / scene id / entry key / event index key /
 *   playback session_id ({type:"playback"} is a focus/highlight target for
 *   the Scenes view's Live playback panel — it has no inspector descriptor).
 */

/**
 * @param {import('./api.js').SceneStudioClient} client initial client (mock)
 */
export function createStore(client) {
  const mockClient = client && client.mode === "mock" ? client : null;
  /** @type {import('./api.js').SceneStudioClient} the active client */
  let active = client;

  /** @type {object} */
  const state = {
    view: "overview",
    scenarioId: typeof client?.getScenario === "function" ? client.getScenario() : null,
    scenarios: typeof client?.listScenarios === "function" ? client.listScenarios() : [],
    selection: null, // WorkbenchSelection | null
    status: null,
    updateCheck: null,
    fixtures: null, // registry doc with derived health
    scenes: null, // scenes doc
    discovery: null, // discovery report | null (live: none until discovery.run)
    events: [], // newest-first OperationalEvent[]
    dismissed: {}, // discovery entry keys hidden via "leave unbound" (session)
    notice: null, // {text, level, at}
    // Builder draft (Pass 2): disposable LOCAL authoring state. `draft` is a
    // deep copy of the canonical scene document (edit) or a fresh sensible
    // draft (create); the canonical server document always wins after save.
    builder: null, // {mode, sceneId, duplicateOf, draft, original, dirty, busy, preview, previewError, error}
    builderExit: null, // {view} pending "leave with unsaved changes?" confirmation
    // Backend-authoritative scene-row fidelity (Builder-expansion §8):
    // scene_id -> {status:"ok"|"unavailable", planned, skipped, fidelity,
    // notes, revision, at} | {status:"loading"}. Goldens are mock/test
    // fixtures, never production row truth.
    sceneFidelity: {},
    sceneFidelityRevision: null,
    sceneFidelityBusy: false,
    conn: { mode: "mock", url: DEFAULT_LIVE_URL, status: "idle", error: null, lastPoll: null },
    // Live fixture color-state (plan: live fixture color-state pass) —
    // deliberately NOT merged into `state.fixtures.fixtures`: this is
    // disposable read-model data, independent of `engine.revision`, sampled
    // on its own view-aware cadence (see #syncLiveStateSampling below). One
    // atomic snapshot per sampling cycle, never a per-fixture trickle.
    fixtureLiveState: { byId: {}, sampledAt: null, providers: {}, loading: false },
  };

  const subscribers = new Set();
  const notify = () => subscribers.forEach((fn) => fn(state));
  let requestSeq = 0;
  /** A forced fidelity refresh that arrived while one was in flight. */
  let pendingFidelityForce = false;

  /**
   * Fetch status + catalogs WITHOUT touching state (fetch-only). Callers
   * commit all assignments in ONE synchronous block afterwards — a render
   * flushed in an await gap must never observe status and catalogs from
   * different generations (Lit schedules renders as microtasks).
   */
  const fetchAll = async () => {
    const [status, fixtures, scenes, discovery, eventsRes] = await Promise.all([
      Promise.resolve(active.getStatus()),
      active.getFixtures(),
      active.getScenes(),
      active.getDiscovery(),
      active.getRecentEvents(200),
    ]);
    return { status, fixtures, scenes, discovery, events: eventsRes.events };
  };

  const refresh = async () => {
    const fresh = await fetchAll();
    state.status = fresh.status;
    state.fixtures = fresh.fixtures;
    state.scenes = fresh.scenes;
    state.discovery = fresh.discovery;
    state.events = fresh.events;
    if (state.conn.mode === "live") {
      state.conn.status = "ok";
      state.conn.error = null;
      state.conn.lastPoll = Date.now();
    }
    notify();
    // Row fidelity (Builder-expansion §8) follows the catalog: recomputed
    // when the engine revision moved, fire-and-forget so catalogs never wait
    // on N preview round trips.
    void refreshSceneFidelity();
  };

  // -------------------------------------------------------------------
  // Live fixture color-state sampling (plan: live fixture color-state
  // pass, §4/§10). View-aware and visibility-aware: only runs while the
  // Fixtures view is the active view AND the document is visible. Fully
  // independent of the heavy catalog poll above.
  // -------------------------------------------------------------------
  const LIVE_STATE_INTERVAL_MS = 3000;
  let liveStateTimer = null;
  let liveStateInFlight = false;
  let documentVisible = true;

  /** One sampling cycle. Never queues behind an in-flight one (plan §3/§4:
   *  "the Workbench must not build up a queue of stale provider reads") —
   *  a call that arrives mid-sample is simply skipped; the next timer tick
   *  (or the next explicit trigger) will naturally pick it up once free. */
  const sampleLiveState = async () => {
    if (liveStateInFlight) return;
    if (typeof active.getFixtureState !== "function") return;
    liveStateInFlight = true;
    try {
      const snapshot = await active.getFixtureState();
      // Commit one atomic snapshot (never a per-fixture trickle) so this
      // never causes N cascading renders.
      state.fixtureLiveState = {
        byId: (snapshot && snapshot.fixtures) || {},
        sampledAt: (snapshot && snapshot.observed_at) || new Date().toISOString(),
        providers: (snapshot && snapshot.providers) || {},
        loading: false,
      };
      notify();
    } catch {
      // Transport failure: deliberately quiet (no notice banner spam at a
      // ~3s cadence). The last snapshot simply ages toward stale/unavailable
      // on its own via freshness computation (playback.js-style, at render
      // time) — never silently frozen as if it were still current.
    } finally {
      liveStateInFlight = false;
    }
  };

  const stopLiveStateTimer = () => {
    if (liveStateTimer) {
      clearInterval(liveStateTimer);
      liveStateTimer = null;
    }
  };

  /** Start/stop the periodic sampler to match "Fixtures view active AND
   *  document visible" (plan §4). Fires one immediate sample on the
   *  transition into that state — entering Fixtures, or the tab becoming
   *  visible again while Fixtures is already the active view — never makes
   *  the user wait for the next timer tick. */
  const syncLiveStateSampling = () => {
    const shouldRun = state.view === "fixtures" && documentVisible;
    if (shouldRun && !liveStateTimer) {
      void sampleLiveState();
      liveStateTimer = setInterval(() => void sampleLiveState(), LIVE_STATE_INTERVAL_MS);
      // Node (tests, SSR tooling) returns a Timeout with unref(): never let
      // a background sampler keep a process alive on its own. Browsers have
      // no unref() — this is a no-op there, which is exactly what's wanted.
      if (typeof liveStateTimer.unref === "function") liveStateTimer.unref();
    } else if (!shouldRun) {
      stopLiveStateTimer();
    }
  };

  /** Bounded post-command refresh (plan §4): after a Scene Studio command
   *  that can change light output (Apply/Play/Resume/Stop) succeeds while
   *  Fixtures is the active view, sample sooner than the normal cadence —
   *  once quickly, once to confirm — then fall back to the normal interval.
   *  Never creates a standing fast-poll state. */
  const LIVE_STATE_COMMANDS_THAT_CHANGE_LIGHT_OUTPUT = new Set([
    "scene.apply",
    "playback.start",
    "playback.resume",
    "playback.stop",
  ]);
  const scheduleLiveStateBurst = (command) => {
    if (!LIVE_STATE_COMMANDS_THAT_CHANGE_LIGHT_OUTPUT.has(command)) return;
    if (state.view !== "fixtures") return;
    setTimeout(() => void sampleLiveState(), 400);
    setTimeout(() => void sampleLiveState(), 1800);
  };

  /** Backend-declared command availability (status().runtime). */
  const commandAllowedIn = (command) => {
    const runtime = state.status && state.status.runtime;
    if (!runtime || !Array.isArray(runtime.allowed_commands)) return true;
    return runtime.allowed_commands.includes(command);
  };

  /**
   * Command WITHOUT the global refresh/notice side effects (Builder drafts,
   * diagnostics probes, catalog fidelity). The server remains authoritative.
   */
  const quietCommand = async (envelope) => {
    if (!commandAllowedIn(envelope.command)) {
      return {
        command: envelope.command,
        ok: false,
        error: {
          code: "conflict",
          message: `'${envelope.command}' is unavailable in the current backend runtime mode`,
        },
      };
    }
    try {
      return await active.sendCommand({ request_id: `req-${++requestSeq}`, ...envelope });
    } catch (err) {
      const message = String((err && err.message) || err);
      if (state.conn.mode === "live") {
        state.conn.status = "error";
        state.conn.error = message;
        notify();
      }
      return { command: envelope.command ?? null, ok: false, error: { code: "network_error", message } };
    }
  };

  /**
   * Bounded-concurrency refresh of per-scene row fidelity from the canonical
   * `scene.preview` path. Never previews on a timer: callers are catalog
   * load, engine-revision change, create/update, and explicit refresh.
   *
   * A `force` request that arrives while a refresh is in flight is NOT
   * dropped: the in-flight pass may have captured an older catalog, so the
   * forced intent is remembered and re-run once it settles.
   */
  const refreshSceneFidelity = async ({ force = false } = {}) => {
    if (state.sceneFidelityBusy) {
      if (force) pendingFidelityForce = true;
      return;
    }
    if (!commandAllowedIn("scene.preview")) return;
    const revision = state.status ? state.status.engine?.revision : undefined;
    const scenes = (state.scenes && state.scenes.scenes) || [];
    if (!force && state.sceneFidelityRevision === revision && Object.keys(state.sceneFidelity).length) return;
    state.sceneFidelityBusy = true;
    try {
      const active_scenes = scenes.filter((scene) => !(scene.metadata && scene.metadata.archived_at));
      const next = {};
      for (let index = 0; index < active_scenes.length; index += FIDELITY_CONCURRENCY) {
        const batch = active_scenes.slice(index, index + FIDELITY_CONCURRENCY);
        const settled = await Promise.allSettled(
          batch.map(async (scene) => ({ scene, res: await quietCommand({ command: "scene.preview", scene_id: scene.id }) }))
        );
        for (const item of settled) {
          if (item.status !== "fulfilled") continue;
          const { scene, res } = item.value;
          const plan = res && res.ok && res.data ? res.data.render_plan : null;
          if (!plan) {
            next[scene.id] = {
              status: "unavailable",
              reason: (res && res.error && res.error.message) || "preview failed",
            };
            continue;
          }
          const quality = summarizePreviewQuality(plan);
          next[scene.id] = {
            status: "ok",
            planned: quality.planned,
            skipped: quality.skipped,
            fidelity: quality.fidelity,
            quality: quality.quality,
            notes: (plan.notes || []).length,
          };
        }
      }
      state.sceneFidelity = next;
      state.sceneFidelityRevision = revision;
      notify();
    } finally {
      state.sceneFidelityBusy = false;
      if (pendingFidelityForce) {
        pendingFidelityForce = false;
        void refreshSceneFidelity({ force: true });
      }
    }
  };

  const notify_ = (text, level = "info") => {
    state.notice = { text, level, at: Date.now() };
    notify();
  };

  return {
    state,

    async checkUpdates() {
      if (state.updateCheck?.state === "checking") return;
      const source = active;
      state.updateCheck = { state: "checking", message: "Checking GitHub Releases…" };
      notify();
      let result;
      try { result = await source.checkUpdates(); }
      catch { result = { state: "error", message: "Update check failed. Try again later." }; }
      if (source === active) { state.updateCheck = result; notify(); }
    },

    /** @param {(state: object) => void} fn */
    subscribe(fn) {
      subscribers.add(fn);
      return () => subscribers.delete(fn);
    },

    async init() {
      await refresh();
    },

    async setView(view) {
      // Builder navigation guard (Pass 2 plan §6): a DIRTY draft is never
      // silently discarded — navigation parks on an in-app confirmation.
      // A clean draft is disposable and navigation closes it.
      if (state.builder && view !== "scene_builder") {
        if (state.builder.dirty) {
          state.builderExit = { view };
          notify();
          return;
        }
        state.builder = null;
        state.builderExit = null;
      }
      state.view = view;
      notify();
      syncLiveStateSampling();
    },

    /** Wired to the document's `visibilitychange` event by app.js (plan §4:
     *  suspend sampling while hidden, refresh immediately on return). */
    setDocumentVisible(visible) {
      documentVisible = !!visible;
      syncLiveStateSampling();
    },

    /** Test/teardown seam; the sampler otherwise self-manages via setView. */
    stopLiveStateSampling() {
      stopLiveStateTimer();
    },

    /**
     * Open the Builder.
     * - `{}` → create mode (fresh sensible draft)
     * - `{sceneId}` → edit mode (deep copy of the stored scene)
     * - `{sceneId, mode: "duplicate"}` → Duplicate / Save-as-New (deep copy
     *   of the scene INTENT, source id cleared, server/history metadata
     *   dropped, name suffixed so the derived id does not collide).
     * - `{sceneId, mode: "upgrade"}` → edit mode (same scene identity/save
     *   path as plain edit) whose initial draft has `motion` pre-switched
     *   to `palette_cycle` at a default speed, landing the user directly on
     *   the Builder's Dynamic radio instead of Static — the "Upgrade to
     *   dynamic" row action's entry point.
     */
    openBuilder({ sceneId = null, mode = null } = {}) {
      const scenes = (state.scenes && state.scenes.scenes) || [];
      const doc = sceneId ? scenes.find((sc) => sc.id === sceneId) : null;
      if (sceneId && !doc) {
        notify_(`Scene not found: ${sceneId}`, "err");
        return;
      }
      const duplicate = !!doc && mode === "duplicate";
      const source = !doc
        ? defaultBuilderDraft()
        : duplicate
          ? duplicateBuilderDraft(doc)
          : structuredClone(doc);
      const draft = canonicalizeStaticPalette(source);
      if (mode === "upgrade" && doc) {
        draft.motion = { mode: "palette_cycle", strategy: "auto", speed: 0.5 };
      }
      state.builder = {
        mode: !doc ? "create" : duplicate ? "duplicate" : "edit",
        sceneId: doc ? doc.id : null,
        duplicateOf: duplicate ? doc.id : null,
        draft,
        original: structuredClone(draft),
        dirty: false,
        busy: false,
        preview: null, // {scene, render_plan} — last server-authoritative preview
        previewStale: false, // a draft change since the last preview invalidated it
        previewError: null,
        error: null, // {message, path?} save/validation failure (draft stays intact)
      };
      state.builderExit = null;
      state.view = "scene_builder";
      notify();
    },
    
    /** Discard the draft (Cancel or post-save cleanup). */
    closeBuilder() {
      state.builder = null;
      state.builderExit = null;
      notify();
    },

    /**
     * Shallow-merge `patch` into the draft (e.g. {name}, {palette},
     * {motion}, {target_ids}, {brightness}); recomputes dirty state.
     *
     * A successful preview reflects the draft AS IT WAS at preview time:
     * any actual draft change invalidates it immediately, so the UI can
     * never present an older server render plan as current. Field-scoped
     * errors (save validation / preview validation) are cleared when the
     * field they attach to just changed, so corrected input never leaves
     * misleading stale validation state behind.
     */
    patchBuilderDraft(patch) {
      const b = state.builder;
      if (!b || !patch || typeof patch !== "object") return;
      const changedKeys = Object.keys(patch).filter(
        (key) => JSON.stringify(patch[key]) !== JSON.stringify(b.draft[key])
      );
      b.draft = { ...b.draft, ...patch };
      b.dirty = JSON.stringify(b.draft) !== JSON.stringify(b.original);
      // Any real draft change invalidates the last preview ATTEMPT — whether
      // it succeeded (panel) or failed (error). Correcting the field an error
      // points at therefore clears the error AND asks for a fresh preview.
      if (changedKeys.length > 0 && (b.preview || b.previewStale || b.previewError)) {
        b.preview = null;
        b.previewStale = true;
      }
      for (const key of changedKeys) {
        const prefix = `scene.${key}`;
        if (b.error && b.error.path && b.error.path.startsWith(prefix)) b.error = null;
        if (b.previewError && b.previewError.path && b.previewError.path.startsWith(prefix)) {
          b.previewError = null;
        }
      }
      notify();
    },

    /**
     * Merge a patch into the draft's scene-level `default_state`
     * (Builder-expansion §2). `null`/`undefined` values REMOVE that field so
     * the canonical "unset" state round-trips; a state that ends up empty is
     * dropped entirely. Advanced fields the editor does not own are kept.
     */
    patchBuilderDefaultState(patch) {
      const b = state.builder;
      if (!b) return;
      const current = b.draft.default_state || {};
      const next = { ...current };
      for (const [key, value] of Object.entries(patch || {})) {
        if (value === null || value === undefined) delete next[key];
        else next[key] = value;
      }
      if (patch && patch.palette_index !== undefined && patch.palette_index !== null) delete next.color;
      else if (patch && patch.color) delete next.palette_index;
      this.patchBuilderDraft({ default_state: Object.keys(next).length ? next : null });
    },

    /**
     * Merge a patch into ONE fixture's override (`fixture_states[id]`,
     * Builder-expansion §3). An existing advanced override document is
     * preserved field-by-field: only the patched keys change, and `null`
     * removes a single field. Editing a supported field can therefore never
     * delete gradient/effect/transition/provider_ext content.
     */
    patchBuilderOverride(fixtureId, patch) {
      const b = state.builder;
      if (!b) return;
      const states = { ...(b.draft.fixture_states || {}) };
      const current = states[fixtureId] || {};
      const next = { ...current };
      for (const [key, value] of Object.entries(patch || {})) {
        if (value === null || value === undefined) delete next[key];
        else next[key] = value;
      }
      if (patch && patch.palette_index !== undefined && patch.palette_index !== null) delete next.color;
      else if (patch && patch.color) delete next.palette_index;
      if (Object.keys(next).length === 0) delete states[fixtureId];
      else states[fixtureId] = next;
      this.patchBuilderDraft({ fixture_states: states });
    },

    /**
     * Remove an override ENTIRELY (explicit action only): the fixture falls
     * back to the scene default. Never implicit — clearing a single field
     * goes through patchBuilderOverride with null.
     */
    removeBuilderOverride(fixtureId) {
      const b = state.builder;
      if (!b) return;
      const states = { ...(b.draft.fixture_states || {}) };
      delete states[fixtureId];
      this.patchBuilderDraft({ fixture_states: states });
    },

    /** Create an override for a fixture (seeded from the scene default). */
    addBuilderOverride(fixtureId) {
      const b = state.builder;
      if (!b) return;
      const states = { ...(b.draft.fixture_states || {}) };
      if (states[fixtureId]) return;
      const seed = b.draft.default_state ? { ...b.draft.default_state } : { on: true };
      states[fixtureId] = seed;
      this.patchBuilderDraft({ fixture_states: states });
    },
    
    /** In-app confirmation: leave the Builder discarding unsaved changes. */
    confirmBuilderExit() {
      const target = state.builderExit ? state.builderExit.view : "scenes";
      state.builder = null;
      state.builderExit = null;
      state.view = target;
      notify();
    },

    /** Stay in the Builder (dismiss the exit confirmation). */
    cancelBuilderExit() {
      state.builderExit = null;
      notify();
    },

    /**
     * Send a command WITHOUT the global refresh/notice side effects. The
     * Builder owns its error surface (Pass 2 plan §6: network/validation
     * errors stay attached to the Builder and never destroy the draft) and
     * debounced preview must not spam notices or refetch catalogs.
     */
    async rawCommand(envelope) {
      return quietCommand(envelope);
    },

    /** Recompute per-scene row fidelity from the canonical preview path. */
    async refreshSceneFidelity(options) {
      return refreshSceneFidelity(options);
    },

    /** Cached backend-authoritative row fidelity for one scene, or null. */
    sceneFidelityFor(sceneId) {
      return state.sceneFidelity[sceneId] || null;
    },
    
    /**
     * Play the unsaved draft on fixtures (scene.play_draft). Falls back to
     * observational scene.preview_draft when the runtime forbids provider
     * writes (read-only). Never persists, never mutates the catalog, never
     * destroys the draft on failure. On success the derived candidate id
     * (create mode) is learned from the server-normalized scene document.
     */
    async previewBuilderDraft() {
      const b = state.builder;
      if (!b || b.busy) return { ok: false, error: { code: "internal_error", message: "no builder draft" } };
      const playAllowed = this.commandAllowed("scene.play_draft");
      if (playAllowed) {
        if (typeof b.draft.name !== "string" || !b.draft.name.trim()) {
          b.previewError = { message: "Name the scene before previewing it on lights.", path: "scene.name" };
          b.preview = null;
          notify();
          return {
            command: "scene.play_draft",
            ok: false,
            error: { code: "validation_error", message: b.previewError.message, details: { path: "scene.name" } },
          };
        }
        if (!Array.isArray(b.draft.target_ids) || b.draft.target_ids.length === 0) {
          b.previewError = { message: "Select at least one target before previewing.", path: "scene.target_ids" };
          b.preview = null;
          notify();
          return {
            command: "scene.play_draft",
            ok: false,
            error: { code: "validation_error", message: b.previewError.message, details: { path: "scene.target_ids" } },
          };
        }
      }
      b.busy = true;
      b.previewError = null;
      notify();
      const envelope = playAllowed
        ? { command: "scene.play_draft", scene: b.draft }
        : { command: "scene.preview_draft", scene: b.draft };
      const res = await this.rawCommand(envelope);
      b.busy = false;
      if (!res.ok) {
        b.previewError = {
          message: (res.error && res.error.message) || "Preview failed",
          path: (res.error && res.error.details && res.error.details.path) || null,
        };
        // A failed preview must not leave an older server plan on screen as
        // if it still described the current draft: drop it so the error is
        // the only preview presentation (the draft itself is untouched).
        b.preview = null;
      } else {
        b.preview = {
          scene: res.data.scene,
          render_plan: res.data.render_plan,
          played: !!res.data.played,
          kind: res.data.kind || null,
          session_id: res.data.session_id || null,
        };
        b.previewStale = false;
        if (playAllowed) {
          await refresh();
          notify_(commandNoticeText(envelope, res, state), "ok");
        }
      }
      notify();
      return res;
    },

    /** Stop a dynamic Builder preview session without leaving the draft. */
    async stopBuilderPreview() {
      const b = state.builder;
      const sessionId = b && b.preview && b.preview.session_id;
      if (!sessionId) {
        return { ok: false, error: { code: "internal_error", message: "no preview session" } };
      }
      const res = await this.sendCommand({ command: "playback.stop", session_id: sessionId });
      if (res.ok && state.builder && state.builder.preview) {
        state.builder.preview = { ...state.builder.preview, session_id: null };
        notify();
      }
      return res;
    },

    /**
     * Persist the draft: scene.create (new) or scene.update (edit). The
     * canonical server result replaces local assumptions: the catalog is
     * refreshed, the draft is dropped, the saved scene is selected, and the
     * app returns to Scenes. No optimistic success — a failed save keeps
     * the Builder open with the draft and the error attached.
     */
    async saveBuilderDraft() {
      const b = state.builder;
      if (!b || b.busy) return { ok: false, error: { code: "internal_error", message: "no builder draft" } };
      // Local minimums mirroring Scene v2 (the backend re-validates; these
      // just save a round trip and attach the error to the right control).
      if (typeof b.draft.name !== "string" || !b.draft.name.trim()) {
        return this.builderLocalFailure("Name is required.", "scene.name");
      }
      if (!Array.isArray(b.draft.target_ids) || b.draft.target_ids.length === 0) {
        return this.builderLocalFailure("Select at least one target.", "scene.target_ids");
      }
      b.busy = true;
      b.error = null;
      notify();
      const envelope =
        b.mode === "edit"
          ? { command: "scene.update", scene_id: b.sceneId, scene: b.draft }
          : b.mode === "duplicate"
            ? { command: "scene.create", duplicate_of: b.duplicateOf, scene: b.draft }
            : { command: "scene.create", scene: b.draft };
      const res = await this.rawCommand(envelope);
      b.busy = false;
      if (!res.ok) {
        b.error = {
          message: (res.error && res.error.message) || "Save failed",
          path: (res.error && res.error.details && res.error.details.path) || null,
        };
        notify();
        return res;
      }
      const saved = (res.data && res.data.scene) || null;
      const savedId = (saved && saved.id) || b.sceneId;
      const verb = b.mode === "edit" ? "Updated" : b.mode === "duplicate" ? "Duplicated" : "Saved";
      await refresh(); // canonical catalog from the backend, not the draft
      state.builder = null;
      state.builderExit = null;
      state.selection = { type: "scene", id: savedId };
      state.view = "scenes";
      notify_(`${verb} scene: ${(saved && saved.name) || savedId}`, "ok");
      // A newly authored scene gets backend-authoritative row fidelity
      // immediately (§8 acceptance), not only on the next revision change.
      await refreshSceneFidelity({ force: true });
      return res;
    },

    /** CommandResult-shaped local validation failure attached to the Builder. */
    builderLocalFailure(message, path) {
      const b = state.builder;
      if (b) {
        b.error = { message, path };
        notify();
      }
      return { command: "scene.create", ok: false, error: { code: "validation_error", message, details: { path } } };
    },

    /** @param {WorkbenchSelection|null} selection */
    select(selection) {
      state.selection = selection;
      notify();
    },

    async setScenario(scenarioId) {
      if (state.conn.mode !== "mock" || !mockClient) return;
      mockClient.setScenario(scenarioId);
      state.scenarioId = scenarioId;
      state.selection = null;
      state.dismissed = {};
      state.sceneFidelity = {};
      state.sceneFidelityRevision = null;
      pendingFidelityForce = false;
      state.builder = null;
      state.builderExit = null;
      await refresh();
      const meta = state.scenarios.find((s) => s.id === scenarioId);
      notify_(`Scenario: ${meta ? meta.label : scenarioId}`, "info");
    },

    /**
     * Switch the active client. Resolves true when the connection is up
     * (live mode keeps status "error" + retries via poll() when false).
     * @param {{mode: "mock"|"live", url?: string}} conn
     * @returns {Promise<boolean>}
     */
    async setConnection({ mode, url } = {}) {
      state.updateCheck = null;
      if (mode === "live") {
        // An explicit empty URL is valid (same-origin AppDaemon transport);
        // only fall back when the caller omitted url entirely.
        const provided = typeof url === "string" ? url.trim() : null;
        const target = provided !== null ? provided : state.conn.url || DEFAULT_LIVE_URL;
        state.conn = { mode: "live", url: target, status: "connecting", error: null, lastPoll: null };
        state.selection = null;
        state.dismissed = {};
        state.sceneFidelity = {};
        state.sceneFidelityRevision = null;
        pendingFidelityForce = false;
        state.builder = null;
        state.builderExit = null;
        notify();
        const live = createHttpSceneStudioClient(target);
        active = live;
        try {
          await live.getStatus();
        } catch (err) {
          state.conn.status = "error";
          state.conn.error = String((err && err.message) || err);
          notify_(`Live connect failed: ${state.conn.error}`, "err");
          return false;
        }
        state.scenarioId = null;
        await refresh();
        notify_(`Live: ${target}`, "ok");
        return true;
      }
      if (!mockClient) throw new Error("no mock client available");
      active = mockClient;
      state.conn = { mode: "mock", url: state.conn.url, status: "idle", error: null, lastPoll: null };
      state.selection = null;
      state.dismissed = {};
      state.sceneFidelity = {};
      state.sceneFidelityRevision = null;
      pendingFidelityForce = false;
      state.builder = null;
      state.builderExit = null;
      state.scenarioId = mockClient.getScenario();
      await refresh();
      notify_("Mock client", "info");
      return true;
    },

    /**
     * One live-mode status poll. Skips the heavy catalog fetches when the
     * engine revision is unchanged; updates conn status/dot either way.
     * No-op in mock mode (the mock never polls).
     */
    async poll() {
      if (state.conn.mode !== "live") return;
      let status;
      try {
        status = await active.getStatus();
      } catch (err) {
        state.conn.status = "error";
        state.conn.error = String((err && err.message) || err);
        notify();
        return;
      }
      const previousRevision = state.status ? state.status.engine?.revision : undefined;
      const changed = shouldRefetchCatalogs(previousRevision, status.engine?.revision);
      const fresh = changed ? await fetchAll() : null;
      // Commit in one synchronous block (see fetchAll note). In the skip
      // path only status changes; catalogs stay at the same revision.
      state.conn.status = "ok";
      state.conn.error = null;
      state.conn.lastPoll = Date.now();
      if (fresh) {
        state.status = fresh.status;
        state.fixtures = fresh.fixtures;
        state.scenes = fresh.scenes;
        state.discovery = fresh.discovery;
        state.events = fresh.events;
      } else {
        state.status = status;
      }
      notify();
      void refreshSceneFidelity();
    },

    /**
     * Send a command envelope and refresh state from the result.
     * @param {object} envelope
     * @returns {Promise<object>} CommandResult (network failures become
     *   {ok:false, error:{code:"network_error"}} results, never throws)
     */
    /**
     * Backend-declared command availability (status().runtime). Unknown
     * (mock mode / older backend) allows everything — the server remains
     * the enforcing authority; this only shapes the UI.
     * @param {string} command
     * @returns {boolean}
     */
    commandAllowed(command) {
      return commandAllowedIn(command);
    },

    async sendCommand(envelope) {
      // Backend-policy pre-check (live mode): surface mode rejections
      // without a round trip. The server still enforces authoritatively.
      if (state.conn.mode === "live" && !this.commandAllowed(envelope.command)) {
        return {
          command: envelope.command,
          ok: false,
          request_id: `req-${++requestSeq}`,
          error: {
            code: "conflict",
            message: `'${envelope.command}' is unavailable in the current backend runtime mode`,
          },
        };
      }
      let res;
      try {
        res = await active.sendCommand({ request_id: `req-${++requestSeq}`, ...envelope });
      } catch (err) {
        const message = String((err && err.message) || err);
        if (state.conn.mode === "live") {
          state.conn.status = "error";
          state.conn.error = message;
        }
        notify_(`Connection failed: ${message}`, "err");
        return { command: envelope.command ?? null, ok: false, error: { code: "network_error", message } };
      }
      if (res.ok) {
        await refresh();
        // Some commands succeed at the envelope level but carry per-op
        // provider receipts that may themselves be ok:false — a single
        // receipt (fixture.identify: e.g. the provider has no native
        // support) or a list of them (scene.apply: e.g. a fixture's
        // read-back confirmation never matched, plan: reliability hooks).
        // The notice tone/text should reflect that truthfully rather than
        // always reading as a blanket green "Applied".
        const receipts = res.data && Array.isArray(res.data.receipts) ? res.data.receipts : null;
        const singleReceiptOk = res.data && res.data.receipt ? !!res.data.receipt.ok : true;
        const allReceiptsOk = receipts ? receipts.every((r) => !r || r.ok !== false) : true;
        const tone = singleReceiptOk && allReceiptsOk ? "ok" : receipts ? "warn" : "info";
        notify_(commandNoticeText(envelope, res, state), tone);
        scheduleLiveStateBurst(envelope.command);
      } else {
        await refresh();
        if (res.error && res.error.code === "contended") {
          // External light sync (hyperHDR) holds every requested fixture.
          // The message already carries the yield/takeover guidance; keep
          // it readable instead of the generic failure echo.
          notify_(`${envelope.command}: ${res.error.message}`, "warn");
        } else {
          notify_(`${envelope.command} failed: ${res.error ? res.error.message : "unknown error"}`, "err");
        }
      }
      return res;
    },

    /** Discovery "leave unbound": a session-local view decision, no command. */
    dismissDiscoveryKey(key) {
      state.dismissed[key] = true;
      notify();
      notify_("Left unbound (no binding change)", "info");
    },

    isDismissed(key) {
      return !!state.dismissed[key];
    },

    notify: notify_,
  };
}

/**
 * Session-addressed command envelope for a `playback-action` event detail
 * (emitted by <ss-playback-session>, re-emitted by <ss-playback-panel>) —
 * the ONE routing seam shared by every view that renders the playback panel
 * (Overview and Scenes), so the two surfaces can never drift apart. Returns
 * null for unknown/non-lifecycle actions. The exact backend-issued
 * session_id is preserved verbatim; there is deliberately no sessionless
 * fallback shape and no optimistic mutation — the store refreshes status
 * authoritatively after the command, and runtime policy stays authoritative
 * through the components' control gating.
 *
 * @param {string} action "pause"|"resume"|"stop"
 * @param {string} sessionId
 * @returns {{command: "playback.pause"|"playback.resume"|"playback.stop", session_id: string}|null}
 */
export function playbackActionEnvelope(action, sessionId) {
  switch (action) {
    case "pause":
      return { command: "playback.pause", session_id: sessionId };
    case "resume":
      return { command: "playback.resume", session_id: sessionId };
    case "stop":
      return { command: "playback.stop", session_id: sessionId };
    default:
      return null;
  }
}

/**
 * Command success notices (R5C plan §10) — readable, scene-named playback
 * feedback. Resolves scene names from the REFRESHED state (post-command):
 * the session's backend-reported scene_name when the collection has it,
 * falling back to the scenes catalog / raw envelope ids.
 */
function commandNoticeText(envelope, res, state) {
  const d = res.data || {};
  // mock results carry `applied`; the engine carries `scene_name`
  if (envelope.command === "scene.apply" && (d.applied || d.scene_name)) {
    const failed = (d.receipts || []).filter((r) => r && r.ok === false);
    const yielded = (d.contention && d.contention.yielded_fixture_ids) || [];
    const yieldBit = yielded.length
      ? ` • ${yielded.length} fixture${yielded.length === 1 ? "" : "s"} yielded to hyperHDR (auto-restores when sync ends)`
      : "";
    if (failed.length) {
      const names = failed.map((r) => fixtureDisplayName(state, r.fixture_id)).join(", ");
      return `Applied ${d.scene_name || envelope.scene_id} — ${failed.length} fixture${failed.length === 1 ? "" : "s"} didn't confirm (${names})${yieldBit}`;
    }
    return `Applied ${d.scene_name || envelope.scene_id} • skipped ${(d.skipped_fixture_ids || []).length}${yieldBit}`;
  }
  if (envelope.command === "scene.apply" && d.contention && (d.contention.ignored_fixture_ids || []).length) {
    return `Applied • ${d.contention.ignored_fixture_ids.length} fixture(s) applied over active hyperHDR sync (policy: ignore)`;
  }
  if (envelope.command === "scene.preview") return `Preview plan ready (${(d.render_plan?.fixture_plans || []).length} fixtures)`;
  if (envelope.command === "scene.play_draft") {
    const name = (d.scene && d.scene.name) || d.scene_name || "draft";
    return d.kind === "playback" ? `Preview playing: ${name}` : `Preview applied: ${name}`;
  }
  if (envelope.command === "playback.start") return `Playback started: ${playbackSceneName(state, envelope.scene_id, d.session_id)}`;
  if (envelope.command === "playback.pause") return `Playback paused: ${playbackSceneName(state, null, envelope.session_id)}`;
  if (envelope.command === "playback.resume") return `Playback resumed: ${playbackSceneName(state, null, envelope.session_id)}`;
  if (envelope.command === "playback.stop") return `Playback stopped: ${playbackSceneName(state, null, envelope.session_id)}`;
  if (envelope.command === "fixture.identify") {
    const r = d.receipt || {};
    return r.ok ? "Identify sent" : `Identify unavailable: ${r.detail || "not supported for this provider"}`;
  }
  return `${envelope.command} ok`;
}

/** Scene display name for a playback notice, resolved from refreshed state. */
function playbackSceneName(state, sceneId, sessionId) {
  const st = state.status;
  if (st) {
    const playback = normalizePlayback(st.playback);
    const session = sessionId ? sessionById(playback, sessionId) : null;
    if (session && (session.scene_name || session.scene_id)) return session.scene_name || session.scene_id;
    if (sceneId) {
      const scene = (state.scenes ? state.scenes.scenes : []).find((sc) => sc.id === sceneId);
      if (scene) return scene.name;
    }
  }
  return sceneId || sessionId || "playback";
}

/** Fixture display name for a notice, resolved from the (refreshed)
 *  catalog; falls back to the raw id when the fixture can't be found. */
function fixtureDisplayName(state, fixtureId) {
  const fixtures = (state.fixtures && state.fixtures.fixtures) || [];
  const fixture = fixtures.find((f) => f.id === fixtureId);
  return (fixture && fixture.name) || fixtureId;
}
