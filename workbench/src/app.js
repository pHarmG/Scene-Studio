/**
 * Scene Studio Workbench app shell.
 *
 * Wires: SceneStudioClient (src/api.js — mock or live HTTP) ->
 * store (src/state.js) -> <ss-app> shell.
 *
 * Facelift plan Stage 2 (docs/scene_studio/WORKBENCH_FACELIFT_PLAN.md §4.1,
 * §4.6, §9): primary nav is Overview/Scenes/Fixtures only; Discovery moves
 * to a header Inbox icon+badge (still its own `state.view`, just reached
 * differently — see §7 row 18); Diagnostics, connection/transport/endpoint
 * config, and the mock-only scenario picker move out of primary chrome
 * into the System drawer (§4.1) opened from a header gear icon. A header
 * status pill synthesizes one severity/count from the same exceptions
 * computation Overview uses (inspector.js's `buildOverviewExceptions`) so
 * it can never read "healthy" while an underlying signal is not (the
 * critique's §1.2 finding). Every control still calls the exact same
 * store method it did before this stage — see the interaction-contract
 * table (§7) for the full mapping.
 *
 * Live mode polls store.poll() every 5s (revision-gated heavy refetches,
 * plan §9.4); mock mode never polls. The live URL persists in localStorage,
 * alongside the live transport choice ("direct" devserver HTTP vs the
 * AppDaemon 4.5 named-endpoint envelope) and, for appdaemon, the endpoint
 * name.
 *
 * DOM entry: mounts <ss-app> into #app of index.html.
 */
import { html, css, render } from "lit";
import {
  createMockSceneStudioClient,
  DEFAULT_ENDPOINT_NAME,
  detectAppDaemonHosting,
  setDefaultHttpSceneStudioClientOptions,
} from "./api.js";
import { baseData } from "./mocks/base_data.js";
import { createStore, DEFAULT_LIVE_URL, POLL_INTERVAL_MS } from "./state.js";
import { buildInspectorDescriptor, buildOverviewExceptions, discoveryOpenCount } from "./inspector.js";
import { SsLightElement } from "./components/ss-light-element.js";

import "./components/ss-status-glyph.js";
import "./components/ss-inspector.js";
import "./components/ss-drawer.js";
import "./views/overview.js";
import "./views/fixtures.js";
import "./views/scenes.js";
import "./views/scene_builder.js";
import "./views/discovery.js";
import "./views/setup.js";
import "./views/diagnostics.js";

/** Primary, persistent nav (facelift plan §2) — daily operation + catalog. */
const VIEWS = [
  { id: "overview", label: "Overview" },
  { id: "scenes", label: "Scenes" },
  { id: "fixtures", label: "Fixtures" },
];

/**
 * First-run bootstrap view (portable pass): offered INSTEAD of the mature
 * nav while the registry is empty — an empty install must not pretend to be
 * a mature one. The Setup tab appears first and the shell auto-lands on it
 * (once) when the first status with zero fixtures arrives.
 */
const SETUP_VIEW = { id: "setup", label: "Setup" };

const CONN_STORAGE_KEY = "scene_studio_workbench.conn";

/**
 * Live-mode transport preferences: "direct" (devserver-style plain HTTP) or
 * "appdaemon" (JSON envelope POSTed to the single named AD endpoint).
 * Transport is purely a client-construction concern — the store sees both as
 * its normal "live" mode (polling, status dot, notices are identical).
 */
function normalizeConnPrefs(parsed) {
  if (!parsed || (parsed.mode !== "mock" && parsed.mode !== "live") || typeof parsed.url !== "string") {
    return null;
  }
  return {
    mode: parsed.mode,
    url: parsed.url,
    transport: parsed.transport === "appdaemon" ? "appdaemon" : "direct",
    endpointName:
      typeof parsed.endpointName === "string" && parsed.endpointName.trim()
        ? parsed.endpointName.trim()
        : DEFAULT_ENDPOINT_NAME,
  };
}

/**
 * Local Vite (`npm run dev`) talks to HA AppDaemon through this origin's
 * proxy (`vite.config.js`). The stored default is still the Python
 * devserver (`DEFAULT_LIVE_URL` on :8765, direct transport) — picking
 * "Live URL" without changing those fields fails with Failed to fetch.
 * When the SPA is served from a different localhost origin, rewrite that
 * unused default to origin + AppDaemon so the first Live click works.
 */
function migrateViteLivePrefs(prefs) {
  if (!prefs || prefs.mode !== "live") return prefs;
  const origin = window.location.origin;
  const onLocalWorkbench = /^https?:\/\/(localhost|127\.0\.0\.1):\d+$/i.test(origin);
  const unusedDevserverDefault =
    prefs.transport === "direct" &&
    (prefs.url === DEFAULT_LIVE_URL || prefs.url === "http://localhost:8765");
  if (onLocalWorkbench && unusedDevserverDefault && origin !== prefs.url) {
    return {
      ...prefs,
      url: origin,
      transport: "appdaemon",
    };
  }
  return prefs;
}

function loadConnPrefs() {
  try {
    const raw = localStorage.getItem(CONN_STORAGE_KEY);
    if (!raw) return null;
    const parsed = normalizeConnPrefs(JSON.parse(raw));
    const migrated = migrateViteLivePrefs(parsed);
    if (
      migrated &&
      parsed &&
      (migrated.url !== parsed.url || migrated.transport !== parsed.transport)
    ) {
      localStorage.setItem(CONN_STORAGE_KEY, JSON.stringify(migrated));
    }
    return migrated;
  } catch {
    // corrupted or unavailable storage: fall back to defaults
  }
  return null;
}

const CONN_DOT_TITLES = {
  idle: "Mock mode — no live connection",
  connecting: "Connecting to live engine…",
  ok: "Connected to live engine",
  error: "Live engine unreachable (retrying every 5s)",
};

class SsApp extends SsLightElement {
  static properties = {
    store: { attribute: false },
    connTransport: { state: true },
    connEndpoint: { state: true },
    systemOpen: { state: true },
  };

  static styles = css`
    @scope (ss-app) {
    :scope {
      display: grid;
      /* The connection/command notice is conditionally rendered between the
         tabs and main.  It must have its own auto-sized track: with only
         three declared rows, a visible notice claimed the 1fr track and
         pushed main into an implicit row at the bottom of the viewport for
         its four-second lifetime. The Builder's unsaved-changes exit
         confirmation (Pass 2 plan §6) is the second conditional bar, so
         main always lives in row 5 and empty auto rows collapse to zero. */
      grid-template-rows: auto auto auto auto minmax(0, 1fr);
      height: 100vh;
      /* iOS Safari: 100vh includes the collapsed URL-bar area, so the bottom
         of the app hides behind it. dvh tracks the real visible viewport. */
      height: 100dvh;
    }
    /* header.top/nav.tabs/.notice are full-bleed bars (background/border
       span the whole viewport), but their CONTENT must line up with
       main's centered, max-width column below — otherwise the header
       icons sit near the raw viewport edge while the panels are inset by
       main's centering margin, and the two visibly disagree on wide
       screens. Each bar's *-inner wrapper carries the identical
       max-width/margin:auto/horizontal-padding as the main element below,
       so every row of chrome shares one left/right edge. */
    header.top {
      border-bottom: 1px solid var(--ss-border);
      background: var(--ss-surface);
    }
    .top-inner {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      max-width: var(--ss-content-max);
      width: 100%;
      margin: 0 auto;
    }
    h1 {
      font-size: 17px;
      margin: 0;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .tag {
      font-size: 12px;
      font-family: var(--ss-font-mono);
      color: var(--ss-text-faint);
      border: 1px solid var(--ss-border);
      border-radius: 3px;
      padding: 1px 6px;
    }
    .spacer {
      flex: 1;
    }
    /* Synthesized system status (facelift plan §2/§4.2) — worst tone across
       fixtures/providers/warnings, never a bare passthrough of engine.ok. */
    .status-pill {
      appearance: none;
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 4px 12px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      background: var(--ss-ok-dim);
      color: var(--ss-ok);
    }
    .status-pill.warn {
      background: var(--ss-warn-dim);
      color: var(--ss-warn);
    }
    .status-pill.err {
      background: var(--ss-err-dim);
      color: var(--ss-err);
    }
    .status-pill .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      flex: none;
    }
    .icon-btn {
      position: relative;
      appearance: none;
      width: var(--ss-icon-btn);
      height: var(--ss-icon-btn);
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--ss-text-dim);
      background: transparent;
      border: 1px solid transparent;
      cursor: pointer;
      flex: none;
    }
    .icon-btn:hover {
      color: var(--ss-text);
      background: var(--ss-surface-2);
      border-color: var(--ss-border-soft);
    }
    .icon-btn.active {
      color: var(--ss-text);
      background: var(--ss-surface-2);
      border-color: var(--ss-accent);
    }
    .icon-btn .badge {
      position: absolute;
      top: -3px;
      right: -3px;
      min-width: 18px;
      height: 18px;
      padding: 0 4px;
      border-radius: 9px;
      background: var(--ss-warn);
      color: #1a1408;
      font-size: 11px;
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .icon-btn .conn-dot {
      position: absolute;
      bottom: 2px;
      right: 2px;
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--ss-idle);
      border: 1.5px solid var(--ss-surface);
    }
    .icon-btn .conn-dot.ok {
      background: var(--ss-ok);
    }
    .icon-btn .conn-dot.error {
      background: var(--ss-err);
    }
    .icon-btn .conn-dot.connecting {
      background: var(--ss-warn);
    }
    nav.tabs {
      background: var(--ss-surface);
      border-bottom: 1px solid var(--ss-border-soft);
    }
    .tabs-inner {
      display: flex;
      gap: 2px;
      padding: 0 14px;
      max-width: var(--ss-content-max);
      width: 100%;
      margin: 0 auto;
    }
    nav.tabs button {
      appearance: none;
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--ss-text-dim);
      font-size: 15px;
      padding: 9px 12px;
      cursor: pointer;
    }
    nav.tabs button:hover {
      color: var(--ss-text);
    }
    nav.tabs button[aria-current="page"] {
      color: var(--ss-text);
      border-bottom-color: var(--ss-accent);
    }
    main {
      /* Keep the content in the flexible row even when the notice is absent
         (the empty auto notice rows simply resolve to zero height). */
      grid-row: 5;
      display: grid;
      grid-template-columns: minmax(0, 1fr) var(--ss-inspector-w);
      gap: 10px;
      padding: 10px 14px;
      overflow: hidden;
      min-height: 0;
      max-width: var(--ss-content-max);
      width: 100%;
      margin: 0 auto;
    }
    main.no-inspector {
      grid-template-columns: minmax(0, 1fr);
    }
    .content {
      overflow-y: auto;
      min-width: 0;
    }
    .notice {
      border-bottom: 1px solid var(--ss-border-soft);
      background: var(--ss-surface);
    }
    .notice-inner {
      font-size: 15px;
      padding: 6px 14px;
      max-width: var(--ss-content-max);
      width: 100%;
      margin: 0 auto;
    }
    .notice.ok { color: var(--ss-ok); }
    .notice.err { color: var(--ss-err); }
    .notice.warn { color: var(--ss-warn); }
    .notice.info { color: var(--ss-text-dim); }
    /* Builder unsaved-changes exit confirmation (Pass 2 plan §6): a
       lightweight in-app bar — native confirm() dialogs are untestable in
       the headless browser harness and silently discard nothing. */
    .exit-confirm {
      border-bottom: 1px solid var(--ss-warn);
      background: var(--ss-warn-dim);
      color: var(--ss-warn);
    }
    .exit-confirm-inner {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 15px;
      padding: 6px 14px;
      max-width: var(--ss-content-max);
      width: 100%;
      margin: 0 auto;
    }
    .exit-confirm-inner button {
      font-size: 13px;
      padding: 2px 10px;
    }
    .exit-confirm-inner .leave {
      border-color: var(--ss-warn);
      color: var(--ss-warn);
    }
    .exit-confirm-inner .spacer {
      flex: 1;
    }
    @media (max-width: 900px) {
      main,
      main.no-inspector {
        grid-template-columns: minmax(0, 1fr);
        padding: 8px;
      }
    }
    /* Touch/mobile header + tabs (touch-target pass 2026-09-29): chrome must
       not just survive phone widths, it must read BIGGER than the content
       rows below it (user feedback: on a phone "the header rows and the
       content all appear to be the same small size"). Same query as the
       token block in tokens.css. */
    @media (max-width: 640px), (pointer: coarse) {
      h1 {
        font-size: 18px;
      }
      nav.tabs button {
        font-size: 16px;
        padding: 12px 14px;
      }
      .notice-inner,
      .exit-confirm-inner {
        font-size: 16px;
      }
      .system-head h2 {
        font-size: 18px;
      }
      .policy-facts {
        font-size: 15px;
      }
    }
    /* Stage 3+ (facelift plan §4.1/§9): the header never clips its icon
       cluster off-screen at phone widths — verified down to 360px. The
       spacer forces a line break instead of squeezing the status pill and
       icon buttons past the viewport edge. */
    @media (max-width: 480px) {
      .top-inner {
        flex-wrap: wrap;
        row-gap: 8px;
        padding: 10px 14px;
      }
      .spacer {
        flex-basis: 100%;
        height: 0;
      }
    }

    /* ---- System drawer (facelift plan §4.1/§4.6) ---- */
    .system-panel {
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .system-head {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .system-head h2 {
      margin: 0;
      font-size: 16px;
      font-weight: 650;
    }
    .system-head .close {
      margin-left: auto;
      appearance: none;
      background: transparent;
      border: 1px solid var(--ss-border);
      color: var(--ss-text-dim);
      border-radius: var(--ss-radius-sm);
      cursor: pointer;
      padding: 0 10px;
      min-width: var(--ss-control-h);
      min-height: var(--ss-control-h);
      font-size: 15px;
    }
    .system-head .close:hover {
      color: var(--ss-text);
      border-color: var(--ss-accent);
    }
    .system-section h3 {
      margin: 0 0 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
    }
    .conn {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-size: 15px;
      color: var(--ss-text-dim);
    }
    .conn .dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      flex: none;
      background: var(--ss-idle);
    }
    .conn .dot.ok { background: var(--ss-ok); }
    .conn .dot.error { background: var(--ss-err); }
    .conn .dot.connecting { background: var(--ss-warn); }
    .conn input.url {
      width: 100%;
      font-size: 15px;
      font-family: var(--ss-font-mono);
      color: var(--ss-text);
      background: var(--ss-bg);
      border: 1px solid var(--ss-border);
      border-radius: 3px;
      padding: 5px 8px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
    }
    .conn input.url:focus {
      outline: none;
      border-color: var(--ss-accent);
    }
    .conn label {
      white-space: nowrap;
    }
    .scenario {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 15px;
      color: var(--ss-text-dim);
    }
    .policy-facts {
      margin: 0;
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 6px 10px;
      font-size: 15px;
    }
    .policy-facts dt {
      color: var(--ss-text-faint);
    }
    .policy-facts dd {
      margin: 0;
      color: var(--ss-text-dim);
      overflow-wrap: anywhere;
    }
    .diagnostics-section {
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .diagnostics-section ss-view-diagnostics {
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      display: block;
    }
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._unsub = null;
    this._noticeTimer = null;
    this._pollTimer = null;
    this.connTransport = "direct";
    this.connEndpoint = DEFAULT_ENDPOINT_NAME;
    this.systemOpen = false;
    this.inspectorReview = null;
    // Diagnostics-navigation shim (Stage 2 corrective pass §2): "diagnostics"
    // was a real main-content view pre-Stage-2; it no longer renders as one
    // (it lives inside the System drawer instead), but exception links built
    // before this stage still request store.setView("diagnostics"). Rather
    // than teach the store/backend about the System drawer, the shell alone
    // remembers the last REAL content view so it can restore it, and a
    // one-shot flag scrolls the Diagnostics section into view once the
    // drawer opens.
    this._lastContentView = "overview";
    this._focusDiagnostics = false;
    this._beforeUnload = null;
  }

  connectedCallback() {
    super.connectedCallback();
    const prefs = loadConnPrefs() || detectAppDaemonHosting(window.location.pathname);
    if (prefs) {
      this.connTransport = prefs.transport;
      this.connEndpoint = prefs.endpointName;
      // The store starts in mock; AppDaemon-hosted production flips it to
      // live same-origin on first load (explicit saved prefs win via
      // loadConnPrefs above). #applyLiveTransport layers the transport
      // defaults into the client factory BEFORE connecting.
      if (prefs.mode === "live" && this.store.state.conn.mode === "mock") {
        this.#applyLiveTransport({ url: prefs.url });
      }
    }
    this._unsub = this.store.subscribe(() => {
      this.#scheduleNoticeClear();
      this.#syncPolling();
      this.#syncDirtyGuard();
      this.#maybeLandOnSetup();
      this.requestUpdate();
    });
    this.#syncPolling();
    this.#syncDirtyGuard();
    this.#maybeLandOnSetup();
    // Live fixture color-state sampling (plan §4) suspends while the tab
    // is hidden and refreshes immediately when it becomes visible again;
    // setView already drives the Fixtures-view-entered/left transitions.
    this._onVisibilityChange = () => this.store.setDocumentVisible(!document.hidden);
    document.addEventListener("visibilitychange", this._onVisibilityChange);
    this.store.setDocumentVisible(!document.hidden);
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    if (this._noticeTimer) clearTimeout(this._noticeTimer);
    if (this._pollTimer) clearInterval(this._pollTimer);
    this._pollTimer = null;
    if (this._onVisibilityChange) {
      document.removeEventListener("visibilitychange", this._onVisibilityChange);
      this._onVisibilityChange = null;
    }
    this.store.stopLiveStateSampling();
    if (this._beforeUnload) {
      window.removeEventListener("beforeunload", this._beforeUnload);
      this._beforeUnload = null;
    }
    super.disconnectedCallback();
  }

  /**
   * Standard `beforeunload` protection while a Builder draft is dirty
   * (Builder-expansion §9). The in-app navigation guard stays authoritative
   * for in-app moves; this only covers a real page unload. No draft
   * persistence/recovery infrastructure is implied.
   */
  #syncDirtyGuard() {
    const dirty = !!(this.store && this.store.state.builder && this.store.state.builder.dirty);
    if (dirty && !this._beforeUnload) {
      this._beforeUnload = (event) => {
        event.preventDefault();
        event.returnValue = "";
        return "";
      };
      window.addEventListener("beforeunload", this._beforeUnload);
    } else if (!dirty && this._beforeUnload) {
      window.removeEventListener("beforeunload", this._beforeUnload);
      this._beforeUnload = null;
    }
  }

  /**
   * Live mode polls every POLL_INTERVAL_MS; mock mode never polls.
   * Called on every state change so mode switches start/stop the timer.
   */
  #syncPolling() {
    const live = !!this.store && this.store.state.conn.mode === "live";
    if (live && !this._pollTimer) {
      this._pollTimer = setInterval(() => {
        this.store.poll();
      }, POLL_INTERVAL_MS);
    } else if (!live && this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  /** True while the registry has no fixtures (first-run bootstrap). */
  #isEmptyInstall(state) {
    const status = state && state.status;
    return !!status && status.fixtures && status.fixtures.total === 0;
  }

  /**
   * Setup stays reachable for the WHOLE bootstrap phase, not only while the
   * registry is literally empty: an operator may adopt one fixture, refresh,
   * and need to come back to create targets. Setup is therefore offered
   * while the backend is still in `registry_admin` (the mode persists across
   * reloads/reconnects because it is re-read from the backend), or while the
   * install is empty. Once normal mode is on with fixtures present, Setup
   * leaves ordinary navigation.
   */
  #setupAvailable(state) {
    const status = state && state.status;
    if (!status || !status.fixtures) return false;
    // #isEmptyInstall takes application STATE (it reads .status itself);
    // passing `status` here silently disabled the empty-registry branch.
    if (this.#isEmptyInstall(state)) return true;
    return !!status.runtime && status.runtime.mode === "registry_admin";
  }

  /**
   * Land ONCE on the Setup view when an empty install's first status
   * arrives (mock or live). Never fights the operator: an explicit view
   * choice afterwards is respected (the flag only moves overview → setup).
   */
  #maybeLandOnSetup() {
    const s = this.store?.state;
    if (!s || !s.status) return;
    if (this._setupLanded) return;
    if (!this.#isEmptyInstall(s)) return;
    this._setupLanded = true;
    if (this.#resolveContentView(s.view) === "overview") {
      this.store.setView("setup");
    }
  }

  #persistConn(overrides = {}) {
    // Reads intended values (not store state): setConnection is async, so
    // persisting store state straight after calling it races and can save
    // the stale pre-connect mode/URL (found in R3 live validation).
    const merged = { ...this.store.state.conn, ...overrides };
    try {
      localStorage.setItem(
        CONN_STORAGE_KEY,
        JSON.stringify({
          mode: merged.mode,
          url: merged.url,
          transport: this.connTransport,
          endpointName: this.connEndpoint,
        })
      );
    } catch {
      // storage unavailable (private mode etc.): session-only persistence
    }
  }

  #onConnModeChange(e) {
    const mode = e.target.value;
    let url = this.store.state.conn.url;
    if (mode === "live") {
      const migrated = migrateViteLivePrefs({
        mode: "live",
        url,
        transport: this.connTransport,
        endpointName: this.connEndpoint,
      });
      url = migrated.url;
      this.connTransport = migrated.transport;
      this.connEndpoint = migrated.endpointName;
      this.#persistConn({ mode, url });
      this.#applyLiveTransport({ url });
      return;
    }
    this.#persistConn({ mode, url });
    this.store.setConnection({ mode, url });
  }

  #onConnUrlChange(e) {
    const url = e.target.value.trim(); // empty = same origin (appdaemon transport)
    this.#persistConn();
    if (this.store.state.conn.mode === "live") {
      this.#applyLiveTransport({ url });
      return;
    }
    this.store.setConnection({ mode: "live", url });
  }

  #onConnTransportChange(e) {
    this.connTransport = e.target.value === "appdaemon" ? "appdaemon" : "direct";
    this.#applyLiveTransport({ url: this.store.state.conn.url });
  }

  #onConnEndpointChange(e) {
    this.connEndpoint = e.target.value.trim() || DEFAULT_ENDPOINT_NAME;
    this.#applyLiveTransport({ url: this.store.state.conn.url });
  }

  /**
   * Reconnect in live mode with the current transport extras. The store only
   * knows "live"; the transport is layered into the client factory defaults
   * before the (re)connect so state.js stays transport-agnostic.
   */
  #applyLiveTransport({ url }) {
    setDefaultHttpSceneStudioClientOptions({
      transport: this.connTransport,
      endpointName: this.connEndpoint,
    });
    this.#persistConn({ mode: "live", url });
    this.store.setConnection({ mode: "live", url });
  }

  #scheduleNoticeClear() {
    if (this.store.state.notice) {
      if (this._noticeTimer) clearTimeout(this._noticeTimer);
      this._noticeTimer = setTimeout(() => {
        this.store.state.notice = null;
        this.requestUpdate();
      }, 4000);
    }
  }

  #onInspectorAction(e) {
    const action = e.detail;
    const store = this.store;
    if (action.id === "__leave_unbound") {
      store.dismissDiscoveryKey(action.args.key);
      return;
    }
    if (action.id === "__policy_note") return;
    store.sendCommand({ command: action.id, ...action.args }).then((result) => {
      if ((action.id === "fixture.rebind_preview" || action.id === "fixture.reconcile_preview") && result.ok) {
        this.inspectorReview = { ...result.data, action: action.args };
        this.requestUpdate();
      }
      if ((action.id === "fixture.rebind" || action.id === "fixture.reconcile") && result.ok) {
        this.inspectorReview = null;
        this.requestUpdate();
      }
    });
  }

  #onInspectorClose() {
    this.store.select(null);
  }

  #toggleSystem() {
    this.systemOpen = !this.systemOpen;
  }

  #closeSystem() {
    this.systemOpen = false;
  }

  /** Header connection controls — same markup/handlers as before Stage 2,
   * just relocated from the top bar into the System drawer's Connection
   * section (facelift plan §4.1, §7 row 15). */
  #renderConnectionSection(conn) {
    return html`
      <div class="conn">
        <span class="dot ${conn.status}" title=${CONN_DOT_TITLES[conn.status] || conn.status}></span>
        <label for="conn-select">Client</label>
        <select id="conn-select" class="ss-select" .value=${conn.mode} @change=${this.#onConnModeChange}>
          <option value="mock">Mock</option>
          <option value="live">Live URL</option>
        </select>
        ${conn.mode === "live"
          ? html`
              <label for="conn-transport">Via</label>
              <select
                id="conn-transport"
                class="ss-select"
                .value=${this.connTransport}
                @change=${this.#onConnTransportChange}
              >
                <option value="direct">Dev server direct</option>
                <option value="appdaemon">AppDaemon</option>
              </select>
              ${this.connTransport === "appdaemon"
                ? html`
                    <input
                      type="text"
                      class="url endpoint"
                      .value=${this.connEndpoint}
                      placeholder=${DEFAULT_ENDPOINT_NAME}
                      spellcheck="false"
                      title="AppDaemon endpoint name served at /api/appdaemon/<name> (Enter to reconnect)"
                      @change=${this.#onConnEndpointChange}
                    />
                  `
                : ""}
              <input
                type="text"
                class="url"
                .value=${conn.url}
                placeholder=${DEFAULT_LIVE_URL}
                spellcheck="false"
                title=${this.connTransport === "appdaemon"
                  ? "AppDaemon base URL (Enter to connect)"
                  : "Live Scene Studio base URL (Enter to connect)"}
                @change=${this.#onConnUrlChange}
              />
            `
          : ""}
      </div>
    `;
  }

  /**
   * "diagnostics" is a routing target (exceptions still say
   * `view: "diagnostics"`) but not a real main-content view anymore —
   * redirect it to the System drawer's Diagnostics section (Stage 2
   * corrective pass §2). Returns the view id the main content area should
   * actually render THIS frame (never "diagnostics"), and schedules the
   * store fixup + drawer focus as a side effect for after this render, so
   * render() itself stays a straightforward function of current state.
   */
  #resolveContentView(rawViewId) {
    if (rawViewId !== "diagnostics") {
      this._lastContentView = rawViewId;
      return rawViewId;
    }
    queueMicrotask(() => {
      this.systemOpen = true;
      this._focusDiagnostics = true;
      // The store/backend never learn "diagnostics" was ever a drawer
      // concept — from their side this just looks like a plain setView
      // back to whatever the operator was actually looking at.
      this.store.setView(this._lastContentView || "overview");
    });
    return this._lastContentView || "overview";
  }

  updated() {
    if (this.systemOpen && this._focusDiagnostics) {
      this._focusDiagnostics = false;
      const drawer = this.querySelector("ss-drawer[floating]");
      const section = this.querySelector(".diagnostics-section");
      if (drawer && section) {
        // The drawer custom element was just mounted THIS render — its own
        // shadow DOM (the actual scrollable .panel) may not have finished
        // its first Lit update yet even though ss-app's own `updated()` has
        // already fired, so scrollIntoView() called synchronously here can
        // silently no-op. Wait for the drawer's own render to settle first.
        Promise.resolve(drawer.updateComplete).then(() => {
          section.scrollIntoView({ block: "start" });
        });
      }
    }
  }

  render() {
    const s = this.store.state;
    const descriptor = buildInspectorDescriptor(s);
    const viewId = this.#resolveContentView(s.view);
    const conn = s.conn;

    const exceptions = buildOverviewExceptions(s);
    const issueCount = exceptions.filter((e) => e.tone === "err" || e.tone === "warn").length;
    const severity = exceptions.some((e) => e.tone === "err")
      ? "err"
      : exceptions.some((e) => e.tone === "warn")
        ? "warn"
        : "ok";
    const inboxCount = discoveryOpenCount(s);

    return html`
      <header class="top">
        <div class="top-inner">
          <h1>Scene Studio Workbench</h1>
          <span class="tag">${conn.mode === "live" ? "live" : "mock"}</span>
          <div class="spacer"></div>
          <button
            class="status-pill ${severity}"
            title="Open Overview"
            @click=${() => this.store.setView("overview")}
          >
            <span class="dot"></span>
            ${severity === "ok" ? "All systems normal" : `${issueCount} issue${issueCount === 1 ? "" : "s"}`}
          </button>
          <button
            class="icon-btn ${viewId === "discovery" ? "active" : ""}"
            title="Discovery inbox"
            @click=${() => this.store.setView("discovery")}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 4h16v11l-3 5H7l-3-5z" />
              <path d="M4 15h4l2 3h4l2-3h4" />
            </svg>
            ${inboxCount ? html`<span class="badge">${inboxCount}</span>` : ""}
          </button>
          <button
            class="icon-btn ${this.systemOpen ? "active" : ""}"
            title="System (connection, scenario, diagnostics)"
            @click=${this.#toggleSystem}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3" />
              <path
                d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.6-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.6 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1z"
              />
            </svg>
            <span class="conn-dot ${conn.status}" title=${CONN_DOT_TITLES[conn.status] || conn.status}></span>
          </button>
        </div>
      </header>
      <nav class="tabs" role="tablist">
        <div class="tabs-inner">
          ${(this.#setupAvailable(s) ? [SETUP_VIEW, ...VIEWS] : VIEWS).map(
            (v) => html`
              <button
                role="tab"
                aria-current=${viewId === v.id ? "page" : "false"}
                @click=${() => this.store.setView(v.id)}
              >
                ${v.label}
              </button>
            `
          )}
        </div>
      </nav>
      ${s.notice ? html`<div class="notice ${s.notice.level}"><div class="notice-inner">${s.notice.text}</div></div>` : ""}
      ${s.builderExit
        ? html`
            <div class="exit-confirm" role="alertdialog" aria-label="Unsaved scene changes">
              <div class="exit-confirm-inner">
                <span>Scene has unsaved changes — leave without saving?</span>
                <div class="spacer"></div>
                <button id="builder-exit-stay" class="ss-btn" @click=${() => this.store.cancelBuilderExit()}>Stay</button>
                <button id="builder-exit-leave" class="ss-btn leave" @click=${() => this.store.confirmBuilderExit()}>
                  Leave
                </button>
              </div>
            </div>
          `
        : ""}
      <main class=${descriptor ? "" : "no-inspector"}>
        <div class="content">
          ${viewId === "overview"
            ? html`<ss-view-overview .store=${this.store}></ss-view-overview>`
            : viewId === "setup"
              ? html`<ss-view-setup .store=${this.store}></ss-view-setup>`
              : viewId === "fixtures"
                ? html`<ss-view-fixtures .store=${this.store}></ss-view-fixtures>`
                : viewId === "scenes"
                  ? html`<ss-view-scenes .store=${this.store}></ss-view-scenes>`
                  : viewId === "scene_builder"
                    ? html`<ss-view-scene-builder .store=${this.store}></ss-view-scene-builder>`
                    : viewId === "discovery"
                      ? html`<ss-view-discovery .store=${this.store}></ss-view-discovery>`
                      : html`<ss-view-overview .store=${this.store}></ss-view-overview>`}
        </div>
        ${descriptor
          ? html`
              <ss-drawer docked>
                <ss-inspector
                  .allowedCommands=${this.store?.state?.status?.runtime?.allowed_commands}
                  .descriptor=${descriptor}
                  .review=${this.inspectorReview}
                  @inspector-action=${this.#onInspectorAction}
                  @inspector-close=${this.#onInspectorClose}
                ></ss-inspector>
              </ss-drawer>
            `
          : ""}
      </main>
      ${this.systemOpen
        ? html`
            <ss-drawer floating backdrop @drawer-dismiss=${this.#closeSystem}>
              <div class="system-panel">
                <div class="system-head">
                  <h2>System</h2>
                  <button class="close" @click=${this.#closeSystem} aria-label="Close System">✕</button>
                </div>

                <div class="system-section">
                  <h3>Connection</h3>
                  ${this.#renderConnectionSection(conn)}
                </div>

                ${conn.mode === "mock"
                  ? html`
                      <div class="system-section">
                        <h3>Scenario</h3>
                        <div class="scenario">
                          <label for="scenario-select">Mock scenario</label>
                          <select
                            id="scenario-select"
                            class="ss-select"
                            .value=${s.scenarioId}
                            @change=${(e) => this.store.setScenario(e.target.value)}
                          >
                            ${s.scenarios.map(
                              (sc) => html`<option value=${sc.id} ?selected=${sc.id === s.scenarioId}>${sc.label}</option>`
                            )}
                          </select>
                        </div>
                      </div>
                    `
                  : ""}

                <div class="system-section">
                  <h3>Runtime policy</h3>
                  <dl class="policy-facts">
                    <dt>Mode</dt>
                    <dd>${s.status?.runtime?.mode ?? "—"}</dd>
                    <dt>Read-only</dt>
                    <dd>${s.status?.runtime?.read_only ? "yes" : "no"}</dd>
                    <dt>Allowed commands</dt>
                    <dd>${(s.status?.runtime?.allowed_commands || []).join(", ") || "all (unrestricted)"}</dd>
                  </dl>
                </div>

                <div class="system-section diagnostics-section">
                  <h3>Diagnostics</h3>
                  <ss-view-diagnostics .store=${this.store}></ss-view-diagnostics>
                </div>
              </div>
            </ss-drawer>
          `
        : ""}
    `;
  }
}

customElements.define("ss-app", SsApp);

const client = createMockSceneStudioClient(baseData);
const store = createStore(client);

render(html`<ss-app .store=${store}></ss-app>`, document.getElementById("app"));

(async () => {
  // Post-R5 polish: a persisted Live URL used to be restored AFTER the mock
  // baseline rendered (store.init() with the mock client, THEN
  // setConnection("live", ...)) — on every load of a live-configured
  // deployment (the normal case for the real AppDaemon-hosted Workbench)
  // that painted the full mock dashboard (mock scenes/fixtures, wrong
  // counts) for one visible beat before the real live data replaced it, a
  // jarring "distorted on load" flash. When a live URL is already known,
  // skip the mock client's first paint entirely and connect live from the
  // start — state.status stays null (a single clean "Loading…", already
  // handled by every view) until the real fetch resolves, never a
  // mock-then-live content swap. Mock mode (no persisted live prefs) is
  // unchanged.
  const prefs = loadConnPrefs();
  if (prefs && prefs.mode === "live" && prefs.url) {
    setDefaultHttpSceneStudioClientOptions({
      transport: prefs.transport,
      endpointName: prefs.endpointName,
    });
    await store.setConnection({ mode: "live", url: prefs.url });
  } else {
    await store.init();
  }
})();
