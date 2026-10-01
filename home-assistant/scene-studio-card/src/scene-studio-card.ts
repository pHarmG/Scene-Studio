import { LitElement, css, html, svg, nothing, type TemplateResult } from "lit";

/**
 * Scene Studio Home Assistant card (custom element `scene-studio-card`),
 * formerly the Test Bench scene-controls card — the old element name
 * `test-bench-scene-controls-card` stays registered as a compatibility alias
 * so unmigrated Lovelace configs keep working.
 *
 * A daily CONTROLLER, not a second Builder: it answers "which scene, what
 * palette, static or dynamic, can I Apply/Pause/Resume/Stop it, is
 * something live, how bright is it right now, how do I archive it" — nothing
 * more. Authoring stays in the Scene Studio Workbench. The primary button is
 * Apply (the bolt) for every idle scene — static or dynamic; putting the
 * scene's look on the lights is the default gesture. Dynamic playback is a
 * deliberate overflow choice ("Play dynamically" in the "…" menu) for idle
 * dynamic scenes. Once a scene has live sessions, the primary button is
 * session-aware again (Pause/Resume/Stop, or the sessions drawer when
 * several are live) — never an implicit pick among many.
 *
 * Brightness is a LIVE trim, not authoring: the UI bridge allowlist
 * deliberately excludes scene.update, so the card resolves the selected
 * scene's declared targets (scene.target_ids, projected since bridge v3) to
 * targets[].ha_entity_ids and scrubs those lights through HA light services
 * — the same class of direct control as any HA light card. Resolution is
 * authoring-based on purpose: live sessions and the applied `current`
 * pointer are runtime state that overnight automations and AppDaemon
 * restarts legitimately clear, and the trim used to vanish with them (a
 * scene applied without an explicit target never set current.target_id at
 * all). The trim stays visible whenever the scene declares lights; pre-v3
 * projections fall back to the old runtime resolution (live sessions, else
 * the applied `current` target). The trim is the light-group pill slider,
 * shrunk: glass track, palette-tinted fill, duotone % readout, icon toggle.
 * The palette swatch itself stays with the scene name as pure identity;
 * dynamic/static reads as a small note on the name line. When the scene
 * resolves to no HA light entities at all, the pill is simply absent.
 *
 * State comes from one compact HA-native projection entity
 * (`sensor.scene_studio_ui`, published by the AppDaemon adapter's
 * `appdaemon_adapter/ui_bridge.py`) — never `input_select.saved_scenes` /
 * `sensor.saved_scene_catalog`. Mutations fire one canonical HA event
 * (`scene_studio_ui_command`) through the authenticated frontend API; the
 * adapter validates against a small allowlist, routes through the real
 * Scene Studio command engine (same RuntimePolicy every other transport
 * obeys), and republishes the projection with the command's outcome. The
 * card never guesses success — it waits for the projection's `last_command`
 * to echo back the exact `request_id` it sent, or times out honestly.
 *
 * The overflow menu renders as a native top-layer popover (Popover API +
 * fixed positioning computed from the trigger, flipping above the trigger
 * near the embed bottom): ha-card's overflow:hidden and backdrop-filter
 * cannot clip it the way they clipped the old absolute dropdown.
 */

// ---------------------------------------------------------------------------
// projection + config types (mirrors appdaemon_adapter/ui_bridge.py exactly)
// ---------------------------------------------------------------------------

type HassEntity = {
  state: string;
  attributes: Record<string, any>;
};

type HomeAssistant = {
  states: Record<string, HassEntity>;
  callApi?: (method: string, path: string, parameters?: Record<string, any>) => Promise<any>;
  callService?: (domain: string, service: string, serviceData?: Record<string, any>) => Promise<any>;
};

type ProjectionScene = {
  id: string;
  name: string;
  palette: string[];
  motion_mode: string; // "static" | "palette_cycle" | "effect"
  target_ids: string[]; // since bridge v3; [] on older projections
};

type SessionState = "active" | "paused" | "orphaned" | "stopped";

type ProjectionSession = {
  session_id: string;
  scene_id: string;
  state: SessionState;
  target_ids: string[];
};

type LastCommand = {
  request_id: string | null;
  ok: boolean | string | null; // AppDaemon transport: true may arrive as "true", false may be dropped
  command: string | null;
  error?: string;
} | null;

type ProjectionTarget = {
  id: string;
  name: string;
  fixture_ids: string[];
  ha_entity_ids: string[];
  enabled_fixture_count: number;
  ha_covered_fixture_count: number;
  complete_ha_coverage: boolean;
};

type Projection = {
  bridge_schema_version: number | null;
  engine_revision: number | null;
  runtime_mode: string;
  provider_writes_blocked: boolean;
  allowed_commands: string[];
  current: { scene_id: string | null; target_id: string | null } | null;
  scenes: ProjectionScene[];
  sessions: ProjectionSession[];
  targets: ProjectionTarget[];
  last_command: LastCommand;
};

type CardConfig = {
  type?: string;
  projection_entity?: string;
  eyebrow?: string;
  title?: string;
  collapsed_by_default?: boolean;
  command_timeout_ms?: number;
  // Deprecated (pre-rework) fields kept only so an unmigrated Lovelace
  // config does not error; the canonical control path never reads them.
  scene_name_entity?: string;
  scene_select_entity?: string;
  scene_catalog_entity?: string;
  save_script?: string;
  apply_script?: string;
  delete_script?: string;
};

const DEFAULT_PROJECTION_ENTITY = "sensor.scene_studio_ui";
const UI_COMMAND_EVENT = "scene_studio_ui_command";
// Applying a whole room (render plan + provider writes + republish) can take
// well over a few seconds; the ack is authoritative, this is only a "the
// bridge is dead" backstop. A late ack still reconciles (see willUpdate).
const DEFAULT_COMMAND_TIMEOUT_MS = 15000;
const MIN_COMMAND_TIMEOUT_MS = 2000;
const MAX_COMMAND_TIMEOUT_MS = 60000;
const SELECTION_STORAGE_PREFIX = "scene-studio-ui:selected-scene:";

// ---------------------------------------------------------------------------
// icons (same filled-SVG family this card has always used)
// ---------------------------------------------------------------------------

const applyIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M13 2 3 14h7l-1 8 10-12h-7z" fill="currentColor"></path></svg>`;
const playIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.14v14l11-7z" fill="currentColor"></path></svg>`;
const pauseIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"></rect><rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"></rect></svg>`;
const stopIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"></rect></svg>`;
const archiveIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 2h5v2H3V5h5zm1 6h2v8h-2zm4 0h2v8h-2zM7 9h2v8H7zm-1 12a2 2 0 0 1-2-2V8h16v11a2 2 0 0 1-2 2z" fill="currentColor"></path></svg>`;
const stackIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 2 8 4-8 4-8-4zm0 8 8 4-8 4-8-4zm0 8 8-4v4l-8 4-8-4v-4z" fill="currentColor"></path></svg>`;
const chevronDownIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6z" fill="currentColor"></path></svg>`;
const moreIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.7" fill="currentColor"></circle><circle cx="12" cy="12" r="1.7" fill="currentColor"></circle><circle cx="19" cy="12" r="1.7" fill="currentColor"></circle></svg>`;
// Busy glyph: a rotation-neutral 270° arc, so tbsc-spin reads as "working"
// no matter which action is in flight (a spinning play triangle did not).
const spinnerIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"></path></svg>`;
const alertIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 1 21h22zm0 6.5 5.5 9.5h-11z" fill="currentColor"></path><rect x="11" y="10" width="2" height="5" fill="var(--tbsc-alert-fg,#1c1206)"></rect><rect x="11" y="16.4" width="2" height="2" fill="var(--tbsc-alert-fg,#1c1206)"></rect></svg>`;
const brightnessIcon = svg`<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2" fill="currentColor"></circle><g stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 2.6v2.5M12 18.9v2.5M2.6 12h2.5M18.9 12h2.5M5.35 5.35l1.77 1.77M16.88 16.88l1.77 1.77M18.65 5.35l-1.77 1.77M7.12 16.88l-1.77 1.77"></path></g></svg>`;

function makeRequestId(): string {
  const cryptoObj = (globalThis as any).crypto;
  if (cryptoObj && typeof cryptoObj.randomUUID === "function") {
    return cryptoObj.randomUUID();
  }
  return `card-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

function isDynamic(scene: ProjectionScene | null): boolean {
  return !!scene && scene.motion_mode !== "static";
}

/** Each color gets an equal 1/N share of the bar (a flat run at its own
 * hex), with the blend into its neighbor confined to a narrow band
 * straddling each 1/N boundary — not one continuous interpolation across
 * the whole width. A continuous N-stop blend gives an interior color a
 * transition on both sides and an edge color a transition on only one,
 * so the edge colors end up occupying roughly HALF the visual width of an
 * interior color even though every color is one equally-weighted list
 * entry — this is what that looked like ("are we underprioritizing the
 * end colors?"). Confining the blend to a fixed fraction of the segment
 * width around each boundary keeps every color's flat run exactly 1/N of
 * the bar, while still crossfading softly instead of a hard-edged seam. */
function blendGradient(colors: string[]): string {
  if (!colors.length) {
    return "linear-gradient(90deg, rgba(255,255,255,0.16), rgba(255,255,255,0.05))";
  }
  if (colors.length === 1) {
    return `linear-gradient(90deg, ${colors[0]}, ${colors[0]})`;
  }
  const n = colors.length;
  const segment = 100 / n;
  const half = segment * 0.3;
  const stops = [`${colors[0]} 0%`];
  for (let i = 0; i < n - 1; i += 1) {
    const boundary = (i + 1) * segment;
    stops.push(`${colors[i]} ${boundary - half}%`);
    stops.push(`${colors[i + 1]} ${boundary + half}%`);
  }
  stops.push(`${colors[n - 1]} 100%`);
  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

/** A very faint hairline at each 1/N boundary — the center of that
 * boundary's blend band (see blendGradient) and the edge between two
 * equal-width color shares, so it lines up with both the actual color
 * transition and the segment count a reader expects from N list entries. */
function blendDividers(colors: string[]): string {
  if (colors.length < 2) {
    return "none";
  }
  const lines: string[] = [];
  for (let i = 1; i < colors.length; i += 1) {
    const pct = (i / colors.length) * 100;
    lines.push(`linear-gradient(90deg, transparent calc(${pct}% - 1px), rgba(0, 0, 0, 0.22) ${pct}%, transparent calc(${pct}% + 1px))`);
  }
  return lines.join(", ");
}

function renderBlendStrip(colors: string[], size: "normal" | "compact" = "normal") {
  if (!colors.length) {
    return html`<span class="blendStrip empty ${size}" title="No palette set"></span>`;
  }
  return html`<span
    class="blendStrip ${size}"
    style=${`--strip-fill:${blendGradient(colors)};--strip-lines:${blendDividers(colors)}`}
    title=${`Palette, in order: ${colors.join(" → ")}`}
  ></span>`;
}

/** Shared mean of the palette (per-channel RGB average) — the one hue that
 *  represents the whole scene, used as the brightness pill's fill glow so
 *  the trim slider reads exactly like the light-group pills instead of
 *  cramming the whole gradient into a slim bar. */
function paletteMeanColor(colors: string[]): string {
  const hexes = (colors || []).filter((color) => /^#[0-9a-fA-F]{6}$/.test(color));
  if (!hexes.length) {
    return "#f6f2ea";
  }
  let r = 0;
  let g = 0;
  let b = 0;
  for (const hex of hexes) {
    r += parseInt(hex.slice(1, 3), 16);
    g += parseInt(hex.slice(3, 5), 16);
    b += parseInt(hex.slice(5, 7), 16);
  }
  const n = hexes.length;
  return `rgb(${Math.round(r / n)}, ${Math.round(g / n)}, ${Math.round(b / n)})`;
}

class SceneStudioCard extends LitElement {
  static properties = {
    hass: { attribute: false },
    _config: { state: true },
    _collapsed: { state: true },
    _selectedSceneId: { state: true },
    _scenePanelOpen: { state: true },
    _sessionsPanelOpen: { state: true },
    _overflowOpen: { state: true },
    _pendingRequestId: { state: true },
    _pendingCommand: { state: true },
    _errorMessage: { state: true },
    _brightnessDraft: { state: true },
    _brightnessDragging: { state: true },
  };

  private static _menuSeq = 0;

  hass?: HomeAssistant;
  private _config?: CardConfig;
  private _collapsed = false;
  private _selectedSceneId: string | null = null;
  private _scenePanelOpen = false;
  private _sessionsPanelOpen = false;
  private _overflowOpen = false;
  private _pendingRequestId: string | null = null;
  private _pendingCommand: string | null = null;
  private _errorMessage: string | null = null;
  private _pendingTimeout: number | null = null;
  // A timed-out command's request id, kept so a late ack can still reconcile
  // the banner honestly (success clears it, failure explains why). Buttons
  // are released at timeout; only the reconciliation memory remains.
  private _timedOutRequestId: string | null = null;
  private _commandTimeoutMs = DEFAULT_COMMAND_TIMEOUT_MS;
  // Live brightness trim on the palette strip: draft percent while scrubbing
  // (committed through HA light services on release), plus the drag gesture.
  private _brightnessDraft: number | null = null;
  private _brightnessDragging = false;
  private _brightnessDrag: {
    pointerId: number;
    startX: number;
    startPercent: number;
    width: number;
    moved: boolean;
    lastValue: number;
  } | null = null;
  private _brightnessDraftTimer: number | null = null;
  private _brightnessAdjustTimer: number | null = null;
  private _brightnessDraftFrame: number | null = null;
  private _suppressPillIconClick = false;
  private _menuId = `tbsc-overflow-${++SceneStudioCard._menuSeq}`;

  static styles = css`
    :host {
      display: block;
      container-type: inline-size;
      --glass-card: linear-gradient(
        180deg,
        color-mix(in srgb, var(--card-background-color, rgba(28, 31, 39, 0.76)) 32%, transparent),
        color-mix(in srgb, var(--card-background-color, rgba(28, 31, 39, 0.76)) 18%, transparent)
      );
      --glass-panel: linear-gradient(
        180deg,
        color-mix(in srgb, var(--card-background-color, rgba(34, 38, 48, 0.8)) 24%, transparent),
        color-mix(in srgb, var(--card-background-color, rgba(34, 38, 48, 0.8)) 12%, transparent)
      );
      --glass-line: color-mix(in srgb, var(--primary-text-color, #fff) 17%, transparent);
      --glass-line-soft: color-mix(in srgb, var(--primary-text-color, #fff) 10%, transparent);
      --glass-highlight: color-mix(in srgb, var(--primary-text-color, #fff) 20%, transparent);
      --glass-shadow:
        0 18px 42px rgba(0, 0, 0, 0.14),
        inset 0 1px 0 rgba(255, 255, 255, 0.12);
    }

    ha-card {
      display: block;
      width: 100%;
      min-width: 0;
      box-sizing: border-box;
      position: relative;
      overflow: hidden;
      border-radius: 24px;
      border: 1px solid var(--glass-line);
      background:
        radial-gradient(circle at top center, color-mix(in srgb, var(--primary-text-color, #fff) 8%, transparent), transparent 52%),
        var(--glass-card);
      backdrop-filter: blur(26px) saturate(1.24);
      -webkit-backdrop-filter: blur(26px) saturate(1.24);
      box-shadow: var(--glass-shadow);
      color: #f4f1ea;
      padding: 18px;
    }

    .shell {
      display: grid;
      gap: 14px;
    }

    .hero {
      display: grid;
      gap: 6px;
      padding: 2px 2px 0;
    }

    .heroButton {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: start;
      border: none;
      background: transparent;
      padding: 0;
      color: inherit;
      text-align: left;
      cursor: pointer;
    }

    .heroCopy {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .heroToggle {
      width: 40px;
      height: 40px;
      border-radius: 16px;
      border: 1px solid var(--glass-line-soft);
      background: var(--glass-panel);
      color: rgba(246, 242, 234, 0.82);
      display: grid;
      place-items: center;
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
    }

    .heroButton:hover .heroToggle {
      transform: translateY(-1px);
      border-color: var(--glass-line);
      background: linear-gradient(180deg, var(--glass-highlight), transparent), var(--glass-panel);
    }

    .heroToggle svg {
      width: 20px;
      height: 20px;
      transition: transform 160ms ease;
    }

    .heroToggle.open svg {
      transform: rotate(180deg);
    }

    .eyebrow {
      font-size: 12px;
      line-height: 1;
      font-weight: 700;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: rgba(223, 211, 194, 0.76);
    }

    .title {
      font-size: clamp(1.45rem, 4vw, 2.15rem);
      line-height: 1.02;
      font-weight: 800;
      letter-spacing: -0.04em;
      color: #f6f2ea;
      width: 100%;
    }

    .policyPill {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid color-mix(in srgb, #e0a83a 55%, transparent);
      color: #f3cf7f;
      background: color-mix(in srgb, #e0a83a 16%, transparent);
    }

    /* One glass control row IS the field — no bordered card nested inside a
       card inside a card (rework plan §2). Identity + palette sit directly
       on the row surface, separated by spacing/typography, not a second box. */
    .controlRow {
      position: relative;
      border-radius: 22px;
      border: 1px solid var(--glass-line-soft);
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--primary-text-color, #fff) 5%, transparent), transparent 26%),
        var(--glass-panel);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.09),
        0 10px 22px rgba(0, 0, 0, 0.08);
      padding: 10px 12px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      gap: 10px;
      align-items: center;
    }

    .controlRow::before {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: inherit;
      opacity: 0;
      pointer-events: none;
      background: var(--scene-blend, transparent);
    }

    .controlRow.hasBlend::before {
      opacity: 0.07;
    }

    .identity {
      position: relative;
      z-index: 1;
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 10px;
      border: none;
      background: transparent;
      padding: 4px;
      margin: -4px;
      color: inherit;
      text-align: left;
      cursor: pointer;
      border-radius: 14px;
    }

    .identity:hover,
    .identity:focus-visible {
      background: color-mix(in srgb, var(--primary-text-color, #fff) 6%, transparent);
    }

    .identityIcon {
      position: relative;
      z-index: 1;
      width: 20px;
      height: 20px;
      flex: none;
      color: rgba(234, 229, 221, 0.82);
    }

    .identityText {
      min-width: 0;
      display: grid;
      gap: 5px;
      flex: 1 1 auto;
    }

    .sceneName {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 15px;
      line-height: 1.2;
      font-weight: 700;
      color: #f6f2ea;
    }

    /* Name line: scene name with dynamic/static (and live state) as a small
       trailing note — the swatch owns the line below instead. */
    .nameLine {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }

    .sceneMeta {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-size: 11px;
      color: rgba(246, 242, 234, 0.62);
      flex: none;
      white-space: nowrap;
    }

    .sceneMeta .runtimeTag {
      color: #9be3ad;
      font-weight: 700;
    }

    .sceneMeta .runtimeTag.warn {
      color: #f3cf7f;
    }

    .identityChevron {
      width: 14px;
      height: 14px;
      flex: none;
      color: rgba(246, 242, 234, 0.55);
      transition: transform 160ms ease;
    }

    .identityChevron.open {
      transform: rotate(180deg);
    }

    .blendStrip {
      position: relative;
      height: 14px;
      width: 100%;
      flex: none;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.1);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.12),
        0 0 0 1px rgba(0, 0, 0, 0.16);
    }

    /* The fill paints on an inset pseudo-element, never the same box as the
       border-radius clip — putting a 90deg gradient directly on a fully
       rounded (999px) box causes the GPU to bleed the far-end stop into the
       rounded cap's antialiasing. A 1px inset breaks that. */
    .blendStrip::after {
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: inherit;
      background: var(--strip-lines, none), var(--strip-fill, transparent);
    }

    .blendStrip.compact {
      height: 10px;
    }

    .blendStrip.empty {
      border-style: dashed;
      border-color: rgba(255, 255, 255, 0.2);
    }

    .blendStrip.empty::after {
      background: repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.12) 0 4px, transparent 4px 8px);
    }

    .actionButton {
      width: 46px;
      min-width: 46px;
      height: 46px;
      border-radius: 16px;
      border: 1px solid var(--glass-line-soft);
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--primary-text-color, #fff) 5%, transparent), transparent 24%),
        var(--glass-panel);
      color: #f6f2ea;
      display: grid;
      place-items: center;
      cursor: pointer;
      position: relative;
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
      flex: none;
    }

    .actionButton:hover:not(:disabled) {
      transform: translateY(-1px);
      border-color: var(--glass-line);
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--primary-text-color, #fff) 8%, transparent), transparent 20%),
        var(--glass-panel);
    }

    .actionButton:disabled {
      opacity: 0.48;
      cursor: default;
    }

    .actionButton svg {
      width: 20px;
      height: 20px;
    }

    .actionButton.accent {
      color: #ffffff;
      border-color: color-mix(in srgb, #6fb2ff 45%, var(--glass-line-soft));
    }

    .actionButton .countBadge {
      position: absolute;
      top: -4px;
      right: -4px;
      min-width: 16px;
      height: 16px;
      padding: 0 3px;
      border-radius: 999px;
      background: #6fb2ff;
      color: #10131a;
      font-size: 10px;
      font-weight: 800;
      display: grid;
      place-items: center;
      line-height: 1;
    }

    .actionButton.busy svg {
      animation: tbsc-spin 900ms linear infinite;
    }

    @keyframes tbsc-spin {
      to {
        transform: rotate(360deg);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .actionButton.busy svg {
        animation: none;
      }
    }

    /* Attached picker/session panels — shared radius language, one subtle
       border, list rows divided by lines rather than nested cards (§2). */
    .panel {
      border-radius: 20px;
      border: 1px solid var(--glass-line-soft);
      background:
        linear-gradient(180deg, color-mix(in srgb, var(--primary-text-color, #fff) 4%, transparent), transparent 24%),
        var(--glass-panel);
      backdrop-filter: blur(18px) saturate(1.14);
      -webkit-backdrop-filter: blur(18px) saturate(1.14);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.05),
        0 14px 28px rgba(0, 0, 0, 0.12);
      overflow: hidden;
    }

    .panelHead {
      padding: 10px 14px 6px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: rgba(246, 242, 234, 0.55);
    }

    .panelList {
      max-height: 320px;
      overflow: auto;
      display: grid;
    }

    .panelRow {
      position: relative;
      background: transparent;
      border: none;
      border-top: 1px solid var(--glass-line-soft);
      color: inherit;
      text-align: left;
      padding: 11px 14px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      cursor: pointer;
      font: inherit;
      width: 100%;
    }

    .panelList .panelRow:first-child {
      border-top: none;
    }

    .panelRow:hover,
    .panelRow.selected {
      background: color-mix(in srgb, var(--primary-text-color, #fff) 5%, transparent);
    }

    .panelRow::before {
      content: "";
      position: absolute;
      inset: 0;
      opacity: 0;
      pointer-events: none;
      transition: opacity 160ms ease;
      background: var(--scene-blend, transparent);
    }

    .panelRow.hasBlend::before {
      opacity: 0.07;
    }

    .panelRow.selected.hasBlend::before,
    .panelRow:hover.hasBlend::before {
      opacity: 0.13;
    }

    .panelRowMeta {
      position: relative;
      z-index: 1;
      min-width: 0;
      display: grid;
      gap: 6px;
    }

    .panelRowName {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 14px;
      font-weight: 700;
      color: #f6f2ea;
    }

    .panelRowSub {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: rgba(246, 242, 234, 0.6);
    }

    .panelEmpty {
      padding: 14px;
      font-size: 14px;
      line-height: 1.35;
      color: rgba(246, 242, 234, 0.62);
    }

    .sessionRow {
      padding: 10px 14px;
      display: flex;
      align-items: center;
      gap: 10px;
      border-top: 1px solid var(--glass-line-soft);
    }

    .panelList .sessionRow:first-child {
      border-top: none;
    }

    .sessionMeta {
      flex: 1 1 auto;
      min-width: 0;
      display: grid;
      gap: 3px;
    }

    .sessionState {
      font-size: 11px;
      font-weight: 700;
      color: #9be3ad;
    }

    .sessionState.paused {
      color: #f3cf7f;
    }

    .sessionState.orphaned {
      color: #f3a37f;
    }

    .sessionTargets {
      font-size: 11px;
      color: rgba(246, 242, 234, 0.6);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .sessionActions {
      display: flex;
      gap: 6px;
      flex: none;
    }

    .sessionActions .actionButton {
      width: 36px;
      min-width: 36px;
      height: 36px;
      border-radius: 12px;
    }

    .sessionActions .actionButton svg {
      width: 16px;
      height: 16px;
    }

    /* Overflow menu: a small attached popover, not a permanent giant button
       row (§7). Rendered as a native top-layer popover (popover + fixed
       position computed from the trigger): ha-card's overflow:hidden and
       backdrop-filter clipping/containing-block can't cut it off in the
       compact card, and the positioner flips it above the trigger when the
       viewport (embed) bottom is closer than the menu height. */
    .overflowWrap {
      position: relative;
    }

    .overflowTrigger.open {
      border-color: color-mix(in srgb, #6fb2ff 45%, var(--glass-line-soft));
    }

    .overflowMenu {
      position: fixed;
      inset: auto;
      margin: 0;
      min-width: 190px;
      max-width: 280px;
      padding: 6px;
      display: none;
      gap: 2px;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.05),
        0 18px 42px rgba(0, 0, 0, 0.38);
    }

    .overflowMenu:popover-open {
      display: grid;
    }

    .overflowItem {
      display: flex;
      align-items: center;
      gap: 10px;
      border: none;
      background: transparent;
      color: inherit;
      font: inherit;
      text-align: left;
      padding: 9px 10px;
      border-radius: 12px;
      cursor: pointer;
    }

    .overflowItem:hover:not(:disabled) {
      background: color-mix(in srgb, var(--primary-text-color, #fff) 7%, transparent);
    }

    .overflowItem:disabled {
      opacity: 0.45;
      cursor: default;
    }

    .overflowItem.danger {
      color: rgba(255, 202, 202, 0.92);
    }

    .overflowItem svg {
      width: 16px;
      height: 16px;
      flex: none;
    }

    /* Live brightness trim (§5 daily-controller scope): the same pill slider
       implementation as the light-group card, shrunk — glass track, lit fill
       from the left in the scene's palette, duotone % readout that flips
       dark/white at the fill edge, knob while dragging. Relative drag (no
       jump), keyboard slider semantics, one service call per gesture. The
       palette swatch itself stays with the scene name as pure identity. */
    .brightnessRow {
      grid-column: 1 / -1;
      min-width: 0;
    }

    .brightnessPill {
      position: relative;
      display: block;
      height: 38px;
      border-radius: 999px;
      cursor: ew-resize;
      outline: none;
      isolation: isolate;
      touch-action: pan-y;
      user-select: none;
      -webkit-user-select: none;
      -webkit-tap-highlight-color: transparent;
    }

    .brightnessPill:focus-visible .bpVisual {
      outline: 2px solid color-mix(in srgb, #6fb2ff 68%, white 22%);
      outline-offset: 2px;
    }

    .bpVisual {
      position: absolute;
      inset: 0;
      border-radius: inherit;
      background:
        radial-gradient(circle at 18% 22%, rgba(255, 255, 255, 0.12), transparent 34%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.10), rgba(255, 255, 255, 0.04));
      border: 1px solid rgba(255, 255, 255, 0.10);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.14),
        inset 0 -8px 14px rgba(7, 10, 18, 0.12),
        0 6px 16px rgba(0, 0, 0, 0.08);
      overflow: hidden;
    }

    .bpFill {
      position: absolute;
      inset: 0 auto 0 0;
      width: var(--b-pct, 0%);
      border-radius: inherit;
      background: var(--b-glow, linear-gradient(90deg, rgba(255, 255, 255, 0.16), rgba(255, 255, 255, 0.05)));
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.2),
        inset 0 -6px 12px rgba(7, 10, 18, 0.14);
      opacity: 0.92;
      transition: width 280ms cubic-bezier(0.22, 1, 0.36, 1), opacity 200ms ease;
    }

    .brightnessPill.dragging .bpFill {
      transition: opacity 200ms ease;
    }

    /* Duotone % readout, same structure as the light-group pill: full-size
       absolute layers so the clip boundary lands at the fill edge across
       the whole pill, never inside the text box. */
    .bpPercentLayers {
      position: absolute;
      inset: 0;
      min-width: 0;
      padding: 0 14px 0 42px;
      pointer-events: none;
    }

    .bpPercentLayer {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      white-space: nowrap;
      padding: 0 14px 0 42px;
      pointer-events: none;
      font-size: 12.5px;
      line-height: 1;
      font-weight: 800;
      letter-spacing: 0.02em;
    }

    .bpPercentFill {
      color: rgba(20, 23, 31, 0.92);
      clip-path: inset(0 calc(100% - var(--b-pct, 0%)) 0 0 round 999px);
    }

    .bpPercentTrack {
      color: rgba(246, 242, 234, 0.94);
      clip-path: inset(0 0 0 var(--b-pct, 0%) round 999px);
    }

    .bpIcon {
      position: absolute;
      left: 5px;
      top: 50%;
      transform: translateY(-50%);
      z-index: 2;
      width: 28px;
      height: 28px;
      padding: 0;
      border-radius: 50%;
      border: none;
      appearance: none;
      -webkit-appearance: none;
      display: grid;
      place-items: center;
      color: color-mix(in srgb, var(--b-glow-color, #f6f2ea) 68%, #171a22 32%);
      background: rgba(27, 30, 39, 0.92);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
      cursor: pointer;
      touch-action: manipulation;
    }

    .bpIcon svg {
      width: 15px;
      height: 15px;
    }

    .bpKnob {
      position: absolute;
      top: 50%;
      left: clamp(8px, var(--b-pct, 0%), calc(100% - 8px));
      transform: translate(-50%, -50%);
      z-index: 3;
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background:
        radial-gradient(circle at 32% 28%, rgba(255, 255, 255, 0.95), rgba(255, 255, 255, 0.28) 48%),
        rgba(255, 255, 255, 0.5);
      box-shadow:
        0 2px 8px rgba(0, 0, 0, 0.42),
        0 0 0 1px rgba(255, 255, 255, 0.38);
      opacity: 0;
      pointer-events: none;
      transition: opacity 150ms ease, left 280ms cubic-bezier(0.22, 1, 0.36, 1);
    }

    .brightnessPill.dragging .bpKnob {
      opacity: 1;
      transition: opacity 110ms ease;
    }

    .errorBanner {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 14px;
      border: 1px solid color-mix(in srgb, #e05d5d 45%, transparent);
      background: color-mix(in srgb, #e05d5d 14%, transparent);
      color: #ffd6d6;
      font-size: 12px;
      line-height: 1.35;
    }

    .errorBanner svg {
      width: 16px;
      height: 16px;
      flex: none;
      color: #ffb4b4;
      --tbsc-alert-fg: #3a0d0d;
    }

    .emptyState {
      padding: 14px 4px;
      font-size: 13px;
      line-height: 1.4;
      color: rgba(246, 242, 234, 0.62);
    }

    @container (max-width: 400px) {
      ha-card {
        padding: 16px;
        border-radius: 20px;
      }

      .controlRow {
        padding: 10px;
        border-radius: 18px;
        gap: 8px;
      }

      .actionButton {
        width: 40px;
        min-width: 40px;
        height: 40px;
        border-radius: 12px;
      }

      .actionButton svg {
        width: 18px;
        height: 18px;
      }

      .panelRow,
      .sessionRow {
        padding: 10px 12px;
      }
    }
  `;

  setConfig(config: CardConfig) {
    this._config = {
      projection_entity: DEFAULT_PROJECTION_ENTITY,
      eyebrow: "Scene Studio",
      title: "Lighting Scenes",
      collapsed_by_default: true,
      ...config,
    };
    this._collapsed = Boolean(this._config.collapsed_by_default);
    this._scenePanelOpen = Boolean((config as any)?.preview_panel_open);
    this._selectedSceneId = this._loadSelectedId();
    const configuredTimeout = Number((config as any)?.command_timeout_ms);
    this._commandTimeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? Math.min(MAX_COMMAND_TIMEOUT_MS, Math.max(MIN_COMMAND_TIMEOUT_MS, Math.round(configuredTimeout)))
      : DEFAULT_COMMAND_TIMEOUT_MS;
  }

  getCardSize() {
    if (this._collapsed) return 2;
    if (this._scenePanelOpen) return 7;
    if (this._sessionsPanelOpen) return 6;
    return 4;
  }

  disconnectedCallback() {
    this._clearPendingTimeout();
    this._clearBrightnessTimers();
    window.removeEventListener("scroll", this._menuScrollClose, true);
    super.disconnectedCallback();
  }

  // -- projection reads ---------------------------------------------------

  private get projectionEntityId(): string {
    return this._config?.projection_entity ?? DEFAULT_PROJECTION_ENTITY;
  }

  private get projection(): Projection | null {
    const entity = this.hass?.states[this.projectionEntityId];
    if (!entity) return null;
    const attrs = entity.attributes || {};
    return {
      bridge_schema_version: attrs.bridge_schema_version ?? null,
      engine_revision: attrs.engine_revision ?? null,
      runtime_mode: attrs.runtime_mode ?? entity.state ?? "unknown",
      provider_writes_blocked: Boolean(attrs.provider_writes_blocked),
      allowed_commands: Array.isArray(attrs.allowed_commands) ? attrs.allowed_commands : [],
      current: attrs.current ?? null,
      scenes: Array.isArray(attrs.scenes) ? attrs.scenes : [],
      sessions: Array.isArray(attrs.sessions) ? attrs.sessions : [],
      targets: Array.isArray(attrs.targets) ? attrs.targets : [],
      last_command: attrs.last_command ?? null,
    };
  }

  private get scenes(): ProjectionScene[] {
    return this.projection?.scenes ?? [];
  }

  private get selectedScene(): ProjectionScene | null {
    const scenes = this.scenes;
    if (!scenes.length) return null;
    const found = this._selectedSceneId ? scenes.find((s) => s.id === this._selectedSceneId) : undefined;
    return found ?? scenes[0];
  }

  private liveSessionsFor(sceneId: string): ProjectionSession[] {
    return (this.projection?.sessions ?? []).filter((s) => s.scene_id === sceneId);
  }

  private get allowedCommands(): Set<string> {
    return new Set(this.projection?.allowed_commands ?? []);
  }

  private get isBusy(): boolean {
    return this._pendingRequestId !== null;
  }

  // -- local selection persistence (plan §8) -------------------------------

  private _storageKey(): string {
    return `${SELECTION_STORAGE_PREFIX}${this.projectionEntityId}`;
  }

  private _loadSelectedId(): string | null {
    try {
      return window.localStorage.getItem(this._storageKey());
    } catch {
      return null;
    }
  }

  private _saveSelectedId(id: string) {
    this._selectedSceneId = id;
    try {
      window.localStorage.setItem(this._storageKey(), id);
    } catch {
      // best-effort only; selection still works for this session
    }
  }

  // -- command lifecycle: busy -> ack from the projection, never optimistic --

  protected willUpdate() {
    const lastCommand = this.projection?.last_command;
    if (!lastCommand?.request_id) return;
    if (this._pendingRequestId && lastCommand.request_id === this._pendingRequestId) {
      this._clearPending();
      this._errorMessage = this.commandAckFailed(lastCommand)
        ? lastCommand.error ?? "Command failed"
        : null;
    } else if (this._timedOutRequestId && lastCommand.request_id === this._timedOutRequestId) {
      // Late ack after the card gave up waiting: reconcile honestly.
      this._timedOutRequestId = null;
      this._errorMessage = this.commandAckFailed(lastCommand)
        ? lastCommand.error ?? "Command failed"
        : null;
    }
    // The brightness draft is only honest while the live lights haven't
    // echoed it yet — drop it as soon as hass agrees (or the lights vanished
    // from the projection).
    if (this._brightnessDraft !== null && !this._brightnessDragging) {
      const scene = this.selectedScene;
      const entities = this.brightnessEntitiesFor(scene);
      if (!entities.length || this.brightnessPercentFor(scene) === this._brightnessDraft) {
        this._clearBrightnessDraft();
      }
    }
  }

  /** The AppDaemon->HA attribute transport is lossy for booleans (True can
   *  arrive as the string "true"; False can be dropped, leaving only the
   *  error), so ack truth is parsed defensively. */
  private commandAckFailed(ack: NonNullable<Projection["last_command"]>): boolean {
    if (ack.ok === false || ack.ok === "false") return true;
    if (ack.ok == null && !!ack.error) return true;
    return false;
  }

  private _clearPendingTimeout() {
    if (this._pendingTimeout !== null) {
      window.clearTimeout(this._pendingTimeout);
      this._pendingTimeout = null;
    }
  }

  private _clearPending() {
    this._clearPendingTimeout();
    this._pendingRequestId = null;
    this._pendingCommand = null;
  }

  private async fireCommand(command: string, extra: Record<string, unknown> = {}) {
    if (!this.hass?.callApi || this.isBusy) return;
    const requestId = makeRequestId();
    this._timedOutRequestId = null;
    this._pendingRequestId = requestId;
    this._pendingCommand = command;
    this._errorMessage = null;
    this._overflowOpen = false;
    this._clearPendingTimeout();
    // The timeout releases the buttons so the user can retry, but keeps the
    // request id around: when the ack lands late, willUpdate reconciles the
    // banner honestly (success clears it, failure says why) instead of
    // leaving a false "no response" over a command that actually worked.
    this._pendingTimeout = window.setTimeout(() => {
      if (this._pendingRequestId === requestId) {
        this._timedOutRequestId = requestId;
        this._clearPending();
        this._errorMessage = "No response from Scene Studio — check Home Assistant";
        this.requestUpdate();
      }
    }, this._commandTimeoutMs);
    try {
      await this.hass.callApi("POST", `events/${UI_COMMAND_EVENT}`, {
        request_id: requestId,
        command,
        ...extra,
      });
    } catch {
      if (this._pendingRequestId === requestId) {
        this._clearPending();
        this._errorMessage = "Could not send the command";
      }
    }
    this.requestUpdate();
  }

  // -- actions --------------------------------------------------------------

  private handleScenePick(sceneId: string) {
    this._saveSelectedId(sceneId);
    this._scenePanelOpen = false;
  }

  private handleArchive() {
    const scene = this.selectedScene;
    if (!scene) return;
    this._overflowOpen = false;
    const confirmed = window.confirm(`Archive "${scene.name}"? You can restore it later from the Workbench.`);
    if (!confirmed) return;
    void this.fireCommand("scene.archive", { scene_id: scene.id });
  }

  // -- live brightness trim (§5 controller scope) ----------------------------
  //
  // The UI bridge allowlist deliberately excludes scene.update (authoring
  // stays in the Workbench), so the card trims the scene's lights LIVE
  // through HA light services instead: the scene's declared target_ids
  // (bridge v3 projection) resolve to targets[].ha_entity_ids, and the
  // palette strip scrubs those entities' brightness. Relative drag (no
  // grab-point jump), keyboard slider semantics, one debounced service call
  // per gesture.

  /** HA light entities behind the scene's declared targets, or [] when the
   *  projection resolves the scene to no lights (never guess). Authoring
   *  data leads so the trim survives what runtime state can't: automations
   *  stopping playback, another scene taking over `current`, AppDaemon
   *  restarts, and applies without an explicit target (current.target_id
   *  stays null there, which is what hid the strip on static scenes).
   *  Pre-v3 projections carry no scene target_ids and keep the old runtime
   *  resolution: live sessions, else the applied `current` target. */
  private brightnessEntitiesFor(scene: ProjectionScene | null): string[] {
    const projection = this.projection;
    if (!projection || !scene) return [];
    const targetIds = new Set<string>((scene.target_ids || []).filter((id) => typeof id === "string" && id));
    if (!targetIds.size) {
      for (const session of projection.sessions) {
        if (session.scene_id !== scene.id || session.state === "stopped") continue;
        for (const targetId of session.target_ids || []) targetIds.add(targetId);
      }
      if (
        !targetIds.size &&
        projection.current?.scene_id === scene.id &&
        typeof projection.current.target_id === "string" &&
        projection.current.target_id
      ) {
        targetIds.add(projection.current.target_id);
      }
    }
    const entities: string[] = [];
    for (const target of projection.targets) {
      if (!targetIds.has(target.id)) continue;
      for (const entityId of target.ha_entity_ids || []) {
        if (entityId.startsWith("light.") && !entities.includes(entityId)) entities.push(entityId);
      }
    }
    return entities;
  }

  private lightPercent(entityId: string): number {
    const entity = this.hass?.states[entityId];
    if (!entity || entity.state !== "on") return 0;
    const brightness = entity.attributes?.brightness;
    if (typeof brightness !== "number") return 100;
    return Math.max(0, Math.min(100, Math.round((brightness / 255) * 100)));
  }

  /** Mean live brightness across the scene's lights. Unreachable entities
   *  (unavailable/unknown) are excluded rather than counted as 0: one dead
   *  light would otherwise cap the whole trim below 100% forever — observed
   *  live 2026-09-30: 13 lights at 100% + 1 unavailable entity read 93%
   *  (13*100/14). Off lights still count as 0 so the reading stays honest
   *  about the room, and commands keep addressing every resolved entity so
   *  a light that recovers rejoins the trim on its next write. */
  private brightnessPercentFor(scene: ProjectionScene | null): number {
    const reachable = this.brightnessEntitiesFor(scene).filter((entityId) => {
      const state = this.hass?.states[entityId]?.state;
      return state === "on" || state === "off";
    });
    if (!reachable.length) return 0;
    const total = reachable.reduce((sum, entityId) => sum + this.lightPercent(entityId), 0);
    return Math.round(total / reachable.length);
  }

  private displayBrightness(scene: ProjectionScene | null): number {
    return this._brightnessDraft ?? this.brightnessPercentFor(scene);
  }

  private _setBrightnessDraft(value: number) {
    this._brightnessDraft = value;
  }

  private _clearBrightnessDraft() {
    this._brightnessDraft = null;
    if (this._brightnessDraftTimer !== null) {
      window.clearTimeout(this._brightnessDraftTimer);
      this._brightnessDraftTimer = null;
    }
  }

  private _clearBrightnessTimers() {
    this._clearBrightnessDraft();
    if (this._brightnessAdjustTimer !== null) {
      window.clearTimeout(this._brightnessAdjustTimer);
      this._brightnessAdjustTimer = null;
    }
    if (this._brightnessDraftFrame !== null) {
      window.cancelAnimationFrame(this._brightnessDraftFrame);
      this._brightnessDraftFrame = null;
    }
  }

  private _scheduleBrightnessDraftClear(delayMs = 2200) {
    if (this._brightnessDraftTimer !== null) window.clearTimeout(this._brightnessDraftTimer);
    this._brightnessDraftTimer = window.setTimeout(() => {
      this._brightnessDraftTimer = null;
      if (!this._brightnessDragging) this._clearBrightnessDraft();
    }, delayMs);
  }

  private _queueBrightnessDraft(value: number) {
    if (this._brightnessDraftFrame !== null) window.cancelAnimationFrame(this._brightnessDraftFrame);
    this._brightnessDraftFrame = window.requestAnimationFrame(() => {
      this._brightnessDraftFrame = null;
      this._setBrightnessDraft(value);
    });
  }

  private _brightnessServiceError() {
    this._errorMessage = "Could not set brightness — check the scene's lights";
    this._clearBrightnessDraft();
    this.requestUpdate();
  }

  private _commitBrightness(scene: ProjectionScene, value: number) {
    const entities = this.brightnessEntitiesFor(scene);
    if (!entities.length) return;
    const callService = this.hass?.callService;
    if (!callService) {
      this._brightnessServiceError();
      return;
    }
    this._setBrightnessDraft(value);
    this._scheduleBrightnessDraftClear();
    const payload: Record<string, unknown> = { entity_id: entities };
    const call =
      value <= 0
        ? callService("light", "turn_off", payload)
        : callService("light", "turn_on", { ...payload, brightness_pct: value, transition: 0.4 });
    void Promise.resolve(call).catch(() => this._brightnessServiceError());
  }

  /** Enter/Space (or the pill's icon): off ↔ on, restoring each light's own
   *  last level (a bare turn_on, no brightness_pct) instead of forcing 100%.
   *  Drafts mirror the flip so the pill reads instantly before hass echoes. */
  private _toggleSceneLights(scene: ProjectionScene) {
    const entities = this.brightnessEntitiesFor(scene);
    const callService = this.hass?.callService;
    if (!entities.length || !callService) return;
    const anyOn = entities.some((entityId) => this.hass?.states[entityId]?.state === "on");
    if (anyOn) {
      this._setBrightnessDraft(0);
      this._scheduleBrightnessDraftClear(2200);
    } else {
      this._clearBrightnessDraft();
    }
    const call = anyOn
      ? callService("light", "turn_off", { entity_id: entities })
      : callService("light", "turn_on", { entity_id: entities });
    void Promise.resolve(call).catch(() => this._brightnessServiceError());
  }

  private handlePillIconPointerDown(scene: ProjectionScene, event: PointerEvent) {
    event.stopPropagation();
    event.preventDefault();
    this._suppressPillIconClick = true;
    this._toggleSceneLights(scene);
  }

  private handlePillIconClick(event: Event) {
    event.stopPropagation();
    event.preventDefault();
    if (this._suppressPillIconClick) {
      this._suppressPillIconClick = false;
      return;
    }
  }

  private _applyBrightnessAdjustment(scene: ProjectionScene, value: number) {
    const clamped = Math.max(0, Math.min(100, Math.round(value)));
    this._setBrightnessDraft(clamped);
    if (this._brightnessAdjustTimer !== null) window.clearTimeout(this._brightnessAdjustTimer);
    this._brightnessAdjustTimer = window.setTimeout(() => {
      this._brightnessAdjustTimer = null;
      if (this._brightnessDragging) return;
      this._commitBrightness(scene, clamped);
    }, 420);
  }

  private handleBrightnessPointerDown(scene: ProjectionScene, event: PointerEvent) {
    if (this._brightnessDrag) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const slider = event.currentTarget as HTMLElement;
    const rect = slider.getBoundingClientRect();
    const startPercent = this.displayBrightness(scene);
    this._brightnessDrag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startPercent,
      width: Math.max(1, rect.width),
      moved: false,
      lastValue: startPercent,
    };
    this._brightnessDragging = true;
    try {
      slider.setPointerCapture(event.pointerId);
    } catch {
      // best-effort; moves still work while the pointer stays inside
    }
  }

  private handleBrightnessPointerMove(scene: ProjectionScene, event: PointerEvent) {
    const drag = this._brightnessDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    if (!drag.moved && Math.abs(dx) > 5) drag.moved = true;
    if (!drag.moved) return;
    const next = Math.max(0, Math.min(100, Math.round(drag.startPercent + (dx / drag.width) * 100)));
    if (next === drag.lastValue) return;
    drag.lastValue = next;
    this._queueBrightnessDraft(next);
  }

  private handleBrightnessPointerUp(scene: ProjectionScene, event: PointerEvent) {
    const drag = this._brightnessDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    this._brightnessDrag = null;
    this._brightnessDragging = false;
    if (this._brightnessDraftFrame !== null) {
      window.cancelAnimationFrame(this._brightnessDraftFrame);
      this._brightnessDraftFrame = null;
    }
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      // pointer may already be released
    }
    if (!drag.moved) {
      // A plain tap keeps the strip's identity role; nothing to toggle here
      // (the primary action button owns on/off for the scene).
      if (this._brightnessDraft !== null) {
        this._clearBrightnessDraft();
        this.requestUpdate();
      }
      return;
    }
    this._commitBrightness(scene, drag.lastValue);
  }

  private handleBrightnessPointerCancel(event: PointerEvent) {
    const drag = this._brightnessDrag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    this._brightnessDrag = null;
    this._brightnessDragging = false;
    if (drag.moved) this._scheduleBrightnessDraftClear(900);
  }

  private handleBrightnessKeydown(scene: ProjectionScene, event: KeyboardEvent) {
    const current = this.displayBrightness(scene);
    let next: number | null = null;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowUp":
        next = current + (event.shiftKey ? 1 : 5);
        break;
      case "ArrowLeft":
      case "ArrowDown":
        next = current - (event.shiftKey ? 1 : 5);
        break;
      case "PageUp":
        next = current + 10;
        break;
      case "PageDown":
        next = current - 10;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = 100;
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        this._toggleSceneLights(scene);
        return;
      default:
        return;
    }
    event.preventDefault();
    if (Math.max(0, Math.min(100, next!)) === current) return;
    this._applyBrightnessAdjustment(scene, next!);
  }

  // -- overflow popover (top layer: immune to ha-card clipping) --------------

  private get _sr(): ShadowRoot {
    return this.renderRoot as ShadowRoot;
  }

  private _menuScrollClose = () => {
    const menu = this._sr.getElementById(this._menuId);
    if (menu?.matches?.(":popover-open")) {
      try {
        menu.hidePopover();
      } catch {
        // already closing
      }
    }
  };

  private _positionMenu(menu: HTMLElement) {
    const trigger = this.renderRoot.querySelector<HTMLElement>(".overflowTrigger");
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const gap = 6;
    menu.style.inset = "auto";
    menu.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
    const height = menu.offsetHeight || 100;
    const spaceBelow = window.innerHeight - rect.bottom - gap;
    const spaceAbove = rect.top - gap;
    if (spaceBelow >= height || spaceBelow >= spaceAbove) {
      menu.style.top = `${rect.bottom + gap}px`;
      menu.style.bottom = "auto";
    } else {
      // Compact card near the embed/viewport bottom: flip above the trigger
      // instead of letting the list run off the edge (the clipped
      // "Archive scene" failure this replaces).
      menu.style.top = "auto";
      menu.style.bottom = `${Math.max(8, window.innerHeight - rect.top + gap)}px`;
    }
    const maxHeight = Math.max(120, window.innerHeight - 16);
    if (height > maxHeight) {
      menu.style.maxHeight = `${maxHeight}px`;
      menu.style.overflowY = "auto";
    }
  }

  private handleMenuBeforeToggle(event: Event) {
    if ((event as ToggleEvent).newState === "open") {
      // Pre-layout estimate from the trigger rect; the toggle handler
      // re-positions with the real menu height right after it opens.
      this._positionMenu(event.target as HTMLElement);
    }
  }

  private handleMenuToggle(event: Event) {
    const open = (event as ToggleEvent).newState === "open";
    this._overflowOpen = open;
    if (open) {
      window.addEventListener("scroll", this._menuScrollClose, true);
      requestAnimationFrame(() => {
        const menu = this._sr.getElementById(this._menuId);
        if (menu?.matches?.(":popover-open")) this._positionMenu(menu);
      });
    } else {
      window.removeEventListener("scroll", this._menuScrollClose, true);
      // Restore focus only when focus was inside the closed menu (keyboard
      // Escape); outside light-dismiss already moved focus to the pointer.
      const menu = this._sr.getElementById(this._menuId);
      const active = this._sr.activeElement;
      if (active && menu && (active === menu || menu.contains(active))) {
        this.renderRoot.querySelector<HTMLElement>(".overflowTrigger")?.focus();
      }
    }
  }

  private handleMenuKeydown(event: KeyboardEvent) {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = [
      ...(this._sr.getElementById(this._menuId)?.querySelectorAll<HTMLButtonElement>(".overflowItem:not(:disabled)") ?? []),
    ];
    if (!items.length) return;
    const active = this._sr.activeElement;
    const index = items.indexOf(active as HTMLButtonElement);
    event.preventDefault();
    if (event.key === "ArrowDown") (items[index + 1] || items[0]).focus();
    else if (event.key === "ArrowUp") (items[index - 1] || items[items.length - 1]).focus();
    else if (event.key === "Home") items[0].focus();
    else items[items.length - 1].focus();
  }

  // -- render: primary action model (plan §5/§6) -----------------------------

  /** While THIS button's command is in flight, show the arc instead of the
   * action glyph; the button's aria-label/title still name the action. */
  private actionGlyph(command: string, icon: TemplateResult): TemplateResult {
    return this.isBusy && this._pendingCommand === command ? spinnerIcon : icon;
  }

  private renderPrimaryAction() {
    const scene = this.selectedScene;
    if (!scene) {
      return html`<button class="actionButton" disabled title="Select a scene first" aria-label="No scene selected">${applyIcon}</button>`;
    }
    const busyThis = this.isBusy;
    const sessions = this.liveSessionsFor(scene.id);
    if (sessions.length === 0) {
      // Idle (any scene, static or dynamic): the primary action is Apply —
      // the bolt reads as "put this scene's look on the lights". Dynamic
      // playback is a deliberate overflow choice, not the default button.
      const allowed = this.allowedCommands.has("scene.apply");
      return html`
        <button
          class="actionButton accent ${busyThis && this._pendingCommand === "scene.apply" ? "busy" : ""}"
          ?disabled=${busyThis || !allowed}
          title=${allowed ? `Apply ${scene.name}` : "Apply is unavailable in the current backend mode"}
          aria-label=${`Apply ${scene.name}`}
          @click=${() => this.fireCommand("scene.apply", { scene_id: scene.id })}
        >
          ${this.actionGlyph("scene.apply", applyIcon)}
        </button>
      `;
    }
    if (sessions.length === 1) {
      const session = sessions[0];
      if (session.state === "orphaned") {
        const allowed = this.allowedCommands.has("playback.stop");
        return html`
          <button
            class="actionButton ${busyThis && this._pendingCommand === "playback.stop" ? "busy" : ""}"
            ?disabled=${busyThis || !allowed}
            title="This session was interrupted (e.g. a restart) and can only be stopped"
            aria-label="Stop the interrupted session"
            @click=${() => this.fireCommand("playback.stop", { session_id: session.session_id })}
          >
            ${this.actionGlyph("playback.stop", stopIcon)}
          </button>
        `;
      }
      if (session.state === "paused") {
        const allowed = this.allowedCommands.has("playback.resume");
        return html`
          <button
            class="actionButton accent ${busyThis && this._pendingCommand === "playback.resume" ? "busy" : ""}"
            ?disabled=${busyThis || !allowed}
            title=${allowed ? `Resume ${scene.name}` : "Resume is unavailable in the current backend mode"}
            aria-label=${`Resume ${scene.name}`}
            @click=${() => this.fireCommand("playback.resume", { session_id: session.session_id })}
          >
            ${this.actionGlyph("playback.resume", playIcon)}
          </button>
        `;
      }
      const allowed = this.allowedCommands.has("playback.pause");
      return html`
        <button
          class="actionButton accent ${busyThis && this._pendingCommand === "playback.pause" ? "busy" : ""}"
          ?disabled=${busyThis || !allowed}
          title=${allowed ? `Pause ${scene.name}` : "Pause is unavailable in the current backend mode"}
          aria-label=${`Pause ${scene.name}`}
          @click=${() => this.fireCommand("playback.pause", { session_id: session.session_id })}
        >
          ${this.actionGlyph("playback.pause", pauseIcon)}
        </button>
      `;
    }
    // Plural-session correctness (plan §6): never implicitly pick one.
    return html`
      <button
        class="actionButton accent"
        title=${`${sessions.length} live sessions for ${scene.name} — manage them individually`}
        aria-label=${`${sessions.length} live sessions, manage`}
        @click=${() => {
          this._sessionsPanelOpen = !this._sessionsPanelOpen;
        }}
      >
        ${stackIcon}
        <span class="countBadge">${sessions.length}</span>
      </button>
    `;
  }

  private renderOverflow() {
    const scene = this.selectedScene;
    const sessions = scene ? this.liveSessionsFor(scene.id) : [];
    const singleStoppable = sessions.length === 1 && sessions[0].state !== "orphaned";
    const canArchive = this.allowedCommands.has("scene.archive");
    // Dynamic playback is the deliberate, lower-frequency choice: idle
    // dynamic scenes offer it here instead of owning the primary button.
    const canPlayDynamic =
      !!scene && isDynamic(scene) && sessions.length === 0 && this.allowedCommands.has("playback.start");
    return html`
      <div class="overflowWrap">
        <button
          class="actionButton overflowTrigger ${this._overflowOpen ? "open" : ""}"
          popovertarget=${this._menuId}
          aria-label="More scene actions"
          aria-expanded=${this._overflowOpen ? "true" : "false"}
          aria-haspopup="menu"
          title="More"
        >
          ${moreIcon}
        </button>
        <div
          id=${this._menuId}
          class="panel overflowMenu"
          popover="auto"
          role="menu"
          @beforetoggle=${this.handleMenuBeforeToggle}
          @toggle=${this.handleMenuToggle}
          @keydown=${this.handleMenuKeydown}
        >
          ${scene && isDynamic(scene) && sessions.length === 0
            ? html`
                <button
                  class="overflowItem"
                  role="menuitem"
                  popovertarget=${this._menuId}
                  popovertargetaction="hide"
                  ?disabled=${this.isBusy || !canPlayDynamic}
                  title=${canPlayDynamic
                    ? `Play ${scene.name} as a moving palette animation`
                    : "Dynamic playback is unavailable in the current backend mode"}
                  @click=${() => this.fireCommand("playback.start", { scene_id: scene.id })}
                >
                  ${playIcon} Play dynamically
                </button>
              `
            : nothing}
          ${singleStoppable
            ? html`
                <button
                  class="overflowItem"
                  role="menuitem"
                  popovertarget=${this._menuId}
                  popovertargetaction="hide"
                  ?disabled=${this.isBusy || !this.allowedCommands.has("playback.stop")}
                  @click=${() => this.fireCommand("playback.stop", { session_id: sessions[0].session_id })}
                >
                  ${stopIcon} Stop playback
                </button>
              `
            : nothing}
          <button
            class="overflowItem danger"
            role="menuitem"
            popovertarget=${this._menuId}
            popovertargetaction="hide"
            ?disabled=${!scene || this.isBusy || !canArchive}
            @click=${() => this.handleArchive()}
          >
            ${archiveIcon} Archive scene
          </button>
        </div>
      </div>
    `;
  }

  /** The palette swatch stays with the scene name (pure identity); brightness
   *  is the shrunken light-pill slider below, rendered whenever the scene
   *  declares lights the projection can resolve. Restricted runtime modes
   *  present the whole card as locked down, so the trim disables with them. */
  private renderBrightnessStrip(scene: ProjectionScene | null, restricted: boolean) {
    if (!scene) return nothing;
    const entities = restricted ? [] : this.brightnessEntitiesFor(scene);
    if (!entities.length) return nothing;
    const value = this.displayBrightness(scene);
    // Same fill treatment as the light-group pills: a solid shared glow
    // (the palette mean) mixed toward white across the fill, with the pill
    // icon tinted by the same hue.
    const glowColor = paletteMeanColor(scene.palette);
    const glow = `linear-gradient(90deg, color-mix(in srgb, ${glowColor} 58%, white 18%), color-mix(in srgb, ${glowColor} 76%, white 10%))`;
    return html`
      <div class="brightnessRow">
        <div
          class="brightnessPill ${this._brightnessDragging ? "dragging" : ""}"
          style=${`--b-pct:${value}%; --b-glow:${glow}; --b-glow-color:${glowColor};`}
          role="slider"
          tabindex="0"
          aria-label=${`${scene.name} brightness`}
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow=${value}
          aria-valuetext=${value > 0 ? `Brightness ${value}%` : "Lights off"}
          title="Drag to trim the scene's light brightness (${entities.length} light${entities.length === 1 ? "" : "s"}); the icon toggles them"
          @pointerdown=${(event: PointerEvent) => this.handleBrightnessPointerDown(scene, event)}
          @pointermove=${(event: PointerEvent) => this.handleBrightnessPointerMove(scene, event)}
          @pointerup=${(event: PointerEvent) => this.handleBrightnessPointerUp(scene, event)}
          @pointercancel=${(event: PointerEvent) => this.handleBrightnessPointerCancel(event)}
          @keydown=${(event: KeyboardEvent) => this.handleBrightnessKeydown(scene, event)}
        >
          <span class="bpVisual" aria-hidden="true">
            <span class="bpFill"></span>
          </span>
          <span class="bpPercentLayers" aria-hidden="true">
            <span class="bpPercentLayer bpPercentFill">${value}%</span>
            <span class="bpPercentLayer bpPercentTrack">${value}%</span>
          </span>
          <button
            class="bpIcon"
            aria-label="Toggle ${scene.name} lights"
            title="Toggle the scene's lights"
            @pointerdown=${(event: PointerEvent) => this.handlePillIconPointerDown(scene, event)}
            @click=${this.handlePillIconClick}
          >
            ${brightnessIcon}
          </button>
          <span class="bpKnob" aria-hidden="true"></span>
        </div>
      </div>
    `;
  }

  private renderScenePanel() {
    if (!this._scenePanelOpen) return nothing;
    const scenes = this.scenes;
    if (!scenes.length) {
      return html`<section class="panel"><div class="panelEmpty">No active scenes yet — create one in the Scene Studio Workbench.</div></section>`;
    }
    const liveSceneIds = new Set((this.projection?.sessions ?? []).map((s) => s.scene_id));
    return html`
      <section class="panel">
        <div class="panelHead">Scenes</div>
        <div class="panelList">
          ${scenes.map((scene) => {
            const dynamic = isDynamic(scene);
            return html`
              <button
                class="panelRow ${scene.id === this.selectedScene?.id ? "selected" : ""} ${scene.palette.length ? "hasBlend" : ""}"
                style=${scene.palette.length ? `--scene-blend:${blendGradient(scene.palette)};` : ""}
                @click=${() => this.handleScenePick(scene.id)}
              >
                <div class="panelRowMeta">
                  <div class="panelRowName">${scene.name}</div>
                  ${renderBlendStrip(scene.palette, "compact")}
                  <div class="panelRowSub">
                    <span>${dynamic ? "Dynamic" : "Static"}</span>
                    ${liveSceneIds.has(scene.id) ? html`<span class="runtimeTag">● live</span>` : nothing}
                  </div>
                </div>
                ${scene.id === this.selectedScene?.id ? html`<span class="identityIcon">${playIcon}</span>` : nothing}
              </button>
            `;
          })}
        </div>
      </section>
    `;
  }

  private renderSessionsPanel() {
    if (!this._sessionsPanelOpen) return nothing;
    const scene = this.selectedScene;
    const sessions = scene ? this.liveSessionsFor(scene.id) : [];
    return html`
      <section class="panel">
        <div class="panelHead">${scene ? `${scene.name} — live sessions` : "Live sessions"}</div>
        <div class="panelList">
          ${sessions.length === 0
            ? html`<div class="panelEmpty">No live sessions.</div>`
            : sessions.map((session) => this.renderSessionRow(session))}
        </div>
      </section>
    `;
  }

  private renderSessionRow(session: ProjectionSession) {
    const busyThis = this.isBusy;
    return html`
      <div class="sessionRow">
        <div class="sessionMeta">
          <div class="sessionState ${session.state}">${session.state === "active" ? "Playing" : session.state === "paused" ? "Paused" : "Orphaned"}</div>
          <div class="sessionTargets">${session.target_ids.join(" · ") || "no targets"}</div>
        </div>
        <div class="sessionActions">
          ${session.state === "active"
            ? html`
                <button
                  class="actionButton"
                  ?disabled=${busyThis || !this.allowedCommands.has("playback.pause")}
                  aria-label="Pause this session"
                  title="Pause"
                  @click=${() => this.fireCommand("playback.pause", { session_id: session.session_id })}
                >
                  ${pauseIcon}
                </button>
              `
            : nothing}
          ${session.state === "paused"
            ? html`
                <button
                  class="actionButton"
                  ?disabled=${busyThis || !this.allowedCommands.has("playback.resume")}
                  aria-label="Resume this session"
                  title="Resume"
                  @click=${() => this.fireCommand("playback.resume", { session_id: session.session_id })}
                >
                  ${playIcon}
                </button>
              `
            : nothing}
          <button
            class="actionButton"
            ?disabled=${busyThis || !this.allowedCommands.has("playback.stop")}
            aria-label="Stop this session"
            title="Stop"
            @click=${() => this.fireCommand("playback.stop", { session_id: session.session_id })}
          >
            ${stopIcon}
          </button>
        </div>
      </div>
    `;
  }

  private sceneMetaText(scene: ProjectionScene, sessions: ProjectionSession[]): { text: string; warn: boolean } | null {
    if (sessions.length === 0) return null;
    if (sessions.length > 1) return { text: `${sessions.length} sessions`, warn: false };
    const state = sessions[0].state;
    if (state === "active") return { text: "Playing", warn: false };
    if (state === "paused") return { text: "Paused", warn: true };
    return { text: "Orphaned", warn: true };
  }

  render() {
    if (!this._config) return nothing;
    const projection = this.projection;
    const scene = this.selectedScene;
    const sessions = scene ? this.liveSessionsFor(scene.id) : [];
    const runtimeTag = scene ? this.sceneMetaText(scene, sessions) : null;
    const restricted = !!projection && (projection.runtime_mode !== "normal" || projection.provider_writes_blocked);

    return html`
      <ha-card>
        <div class="shell">
          <section class="hero">
            <button
              class="heroButton"
              @click=${() => {
                this._collapsed = !this._collapsed;
              }}
            >
              <div class="heroCopy">
                <span class="eyebrow">${this._config.eyebrow}</span>
                ${restricted ? html`<span class="policyPill">${projection?.runtime_mode}</span>` : nothing}
                <span class="title">${this._config.title}</span>
              </div>
              <span class="heroToggle ${this._collapsed ? "" : "open"}">${chevronDownIcon}</span>
            </button>
          </section>

          ${this._collapsed
            ? nothing
            : !projection
              ? html`<div class="emptyState">
                  Scene Studio bridge not available yet (${this.projectionEntityId}). Check the AppDaemon
                  deployment.
                </div>`
              : html`
                  <section
                    class="controlRow ${scene?.palette.length ? "hasBlend" : ""}"
                    style=${scene?.palette.length ? `--scene-blend:${blendGradient(scene.palette)};` : ""}
                  >
                    <button class="identity" @click=${() => {
                      this._scenePanelOpen = !this._scenePanelOpen;
                      this._sessionsPanelOpen = false;
                    }}>
                      <span class="identityText">
                        <span class="nameLine">
                          <span class="sceneName">${scene ? scene.name : "Select Scene"}</span>
                          ${scene
                            ? html`<span class="sceneMeta">
                                <span>${isDynamic(scene) ? "Dynamic" : "Static"}</span>
                                ${runtimeTag ? html`<span class="runtimeTag ${runtimeTag.warn ? "warn" : ""}">· ${runtimeTag.text}</span>` : nothing}
                              </span>`
                            : html`<span class="sceneMeta">No scenes yet</span>`}
                        </span>
                        ${scene ? renderBlendStrip(scene.palette) : nothing}
                      </span>
                      <span class="identityChevron ${this._scenePanelOpen ? "open" : ""}">${chevronDownIcon}</span>
                    </button>
                    ${this.renderPrimaryAction()}
                    ${this.renderOverflow()}
                    ${this.renderBrightnessStrip(scene, restricted)}
                  </section>

                  ${this._errorMessage
                    ? html`<div class="errorBanner">${alertIcon}<span>${this._errorMessage}</span></div>`
                    : nothing}

                  ${this.renderScenePanel()}
                  ${this.renderSessionsPanel()}
                `}
        </div>
      </ha-card>
    `;
  }
}

customElements.define("scene-studio-card", SceneStudioCard);

// Compatibility alias for Lovelace configs authored before the Scene Studio
// rename. A CustomElementRegistry binds one constructor to exactly one name
// (defining the same class under a second name throws NotSupportedError), so
// the alias registers a trivial subclass: identical behavior and prototype
// chain, and alias-created elements still pass `instanceof SceneStudioCard`.
// The guard keeps a double bundle load from throwing on the duplicate name.
class SceneStudioCardCompatAlias extends SceneStudioCard {}
if (!customElements.get("test-bench-scene-controls-card")) {
  customElements.define("test-bench-scene-controls-card", SceneStudioCardCompatAlias);
}

declare global {
  interface HTMLElementTagNameMap {
    "scene-studio-card": SceneStudioCard;
    "test-bench-scene-controls-card": SceneStudioCard;
  }
}
