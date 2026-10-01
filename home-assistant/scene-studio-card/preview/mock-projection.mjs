// Shared mock hass + scenario catalog for the Scene Studio compact card
// (preview/index.html + review/index.html). Mirrors the real projection
// contract published by appdaemon_adapter/ui_bridge.py — NOT the legacy
// input_select.saved_scenes / sensor.saved_scene_catalog shape.

export const PROJECTION_ENTITY = "sensor.scene_studio_ui";
export const UI_COMMAND_EVENT = "scene_studio_ui_command";

const FULL_ALLOWLIST = [
  "playback.pause",
  "playback.resume",
  "playback.start",
  "playback.stop",
  "scene.apply",
  "scene.archive",
];

function scene(id, name, palette, mode = "static") {
  // Bridge v3 projects each scene's declared target_ids (authoring data) so
  // the brightness trim resolves lights without live sessions or `current`.
  return { id, name, palette, motion_mode: mode, target_ids: ["studio"] };
}

function session(id, sceneId, state, targets) {
  return { session_id: id, scene_id: sceneId, state, target_ids: targets };
}

/**
 * @param {object} opts
 * @param {object[]} opts.scenes
 * @param {object[]} [opts.sessions]
 * @param {string} [opts.selected] scene id preselected via localStorage
 * @param {string} [opts.runtimeMode]
 * @param {boolean} [opts.providerWritesBlocked]
 * @param {string[]} [opts.allowedCommands]
 * @param {boolean} [opts.panelOpen]
 * @param {"scene"|"sessions"} [opts.panelKind]
 * @param {"ok"|"fail"|"hang"|"delay"} [opts.commandBehavior] how callApi resolves
 * @param {number} [opts.commandDelayMs] for "delay": ack latency
 * @param {object} [opts.cardConfig] extra card config (e.g. command_timeout_ms)
 */
export function buildScenario(opts) {
  return {
    scenes: [],
    sessions: [],
    runtimeMode: "normal",
    providerWritesBlocked: false,
    allowedCommands: FULL_ALLOWLIST,
    commandBehavior: "ok",
    ...opts,
  };
}

export const SCENARIOS = [
  buildScenario({
    id: "collapsed-idle",
    label: "Collapsed (idle) state",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    collapsed: true,
  }),
  buildScenario({
    id: "static-selected",
    label: "Static scene — Apply",
    widthPx: 462,
    scenes: [
      scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"]),
      scene("meeting_blue", "Meeting Blue", ["#0d47a1", "#2962ff"]),
    ],
    selected: "twilight",
  }),
  buildScenario({
    id: "static-no-runtime",
    label: "Static scene, runtime cleared — trim stays",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    // The overnight failure the trim used to vanish under: no live sessions
    // and `current` taken over/cleared by an automation (or a restart).
    currentPointer: "none",
  }),
  buildScenario({
    id: "dynamic-idle",
    label: "Dynamic, idle — Apply",
    widthPx: 462,
    scenes: [scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle")],
    selected: "aurora_flow",
  }),
  buildScenario({
    id: "dynamic-active",
    label: "Dynamic, active — Pause",
    widthPx: 462,
    scenes: [scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle")],
    sessions: [session("sess-1", "aurora_flow", "active", ["studio"])],
    selected: "aurora_flow",
  }),
  buildScenario({
    id: "dynamic-paused",
    label: "Dynamic, paused — Resume",
    widthPx: 462,
    scenes: [scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle")],
    sessions: [session("sess-1", "aurora_flow", "paused", ["studio"])],
    selected: "aurora_flow",
  }),
  buildScenario({
    id: "dynamic-orphaned",
    label: "Dynamic, orphaned — Stop only",
    widthPx: 462,
    scenes: [scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle")],
    sessions: [session("sess-1", "aurora_flow", "orphaned", ["studio"])],
    selected: "aurora_flow",
  }),
  buildScenario({
    id: "multi-session",
    label: "Two live sessions — ambiguous, drawer",
    widthPx: 462,
    scenes: [scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle")],
    sessions: [
      session("sess-1", "aurora_flow", "active", ["studio"]),
      session("sess-2", "aurora_flow", "paused", ["office"]),
    ],
    selected: "aurora_flow",
    panelOpen: true,
    panelKind: "sessions",
  }),
  buildScenario({
    id: "read-only",
    label: "Read-only backend — every action disabled",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    runtimeMode: "read_only",
    allowedCommands: [],
  }),
  buildScenario({
    id: "provider-writes-blocked",
    label: "registry_admin — provider writes blocked",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    runtimeMode: "registry_admin",
    providerWritesBlocked: true,
    allowedCommands: ["scene.archive"],
  }),
  buildScenario({
    id: "command-failure",
    label: "Command failure — error banner",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    commandBehavior: "fail",
    autoFire: { command: "scene.apply", args: { scene_id: "twilight" } },
  }),
  buildScenario({
    id: "command-delayed",
    label: "Delayed ack — banner reconciles",
    widthPx: 462,
    scenes: [scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"])],
    selected: "twilight",
    commandBehavior: "delay",
    commandDelayMs: 2500,
    cardConfig: { command_timeout_ms: 1000 },
  }),
  buildScenario({
    id: "one-color-palette",
    label: "Single-color palette",
    widthPx: 462,
    scenes: [scene("solid_amber", "Solid Amber", ["#e0a83a"])],
    selected: "solid_amber",
  }),
  buildScenario({
    id: "empty-catalog",
    label: "No scenes yet",
    widthPx: 462,
    scenes: [],
  }),
  buildScenario({
    id: "no-bridge",
    label: "Bridge entity not published",
    widthPx: 462,
    scenes: [],
    noEntity: true,
  }),
  buildScenario({
    id: "mobile-default",
    label: "Mobile default",
    widthPx: 376,
    scenes: [
      scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"]),
      scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle"),
    ],
    sessions: [session("sess-1", "aurora_flow", "active", ["studio"])],
    selected: "aurora_flow",
  }),
  buildScenario({
    id: "mobile-long-name",
    label: "Mobile — long scene name",
    widthPx: 376,
    scenes: [
      scene(
        "warm_sunset_focused_glow",
        "Warm sunset with focused side glow",
        ["#e5a76f", "#db674f", "#8459d8", "#f3db95"],
      ),
    ],
    selected: "warm_sunset_focused_glow",
  }),
  buildScenario({
    id: "scene-picker-open",
    label: "Scene picker open",
    widthPx: 462,
    scenes: [
      scene("twilight", "Twilight", ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"]),
      scene("aurora_flow", "Aurora Flow", ["#00e5ff", "#2979ff", "#7c4dff", "#00c853"], "palette_cycle"),
      scene("meeting_blue", "Meeting Blue", ["#0d47a1", "#2962ff"]),
      scene("solid_amber", "Solid Amber", ["#e0a83a"]),
    ],
    sessions: [session("sess-1", "aurora_flow", "active", ["studio"])],
    selected: "twilight",
    panelOpen: true,
    panelKind: "scene",
  }),
];

/**
 * Build a mock `hass` object for one scenario. `onRender` is called after
 * every simulated state change (command ack, or never for a hung command).
 */
export function createMockHass(scenario, { onRender }) {
  const scenes = scenario.scenes.map((s) => ({ ...s }));
  const sessions = scenario.sessions.map((s) => ({ ...s }));
  let lastCommand = null;
  let revision = 1;

  const buildAttributes = () => ({
    bridge_schema_version: 3,
    engine_revision: revision,
    runtime_mode: scenario.runtimeMode,
    provider_writes_blocked: scenario.providerWritesBlocked,
    allowed_commands: scenario.allowedCommands,
    // `current` is runtime state an overnight automation can legitimately
    // take over or clear; scenario.currentPointer: "none" simulates that.
    // Scenes carry target_ids since v3, so the trim resolves without it.
    current:
      scenario.currentPointer === "none"
        ? null
        : { scene_id: scenario.selected ?? null, target_id: scenario.selected ? "studio" : null },
    scenes,
    sessions: sessions.filter((s) => s.state !== "stopped"),
    targets: scenario.targets ?? [
      {
        id: "studio",
        name: "Studio",
        fixture_ids: ["g_strip", "lamp", "custom_gradient"],
        // The third entity mirrors the live 2026-09-30 failure: a dead light
        // in the target must not cap the trim below 100%.
        ha_entity_ids: ["light.hue_g_strip", "light.lamp", "light.custom_gradient"],
        enabled_fixture_count: 3,
        ha_covered_fixture_count: 3,
        complete_ha_coverage: true,
      },
    ],
    last_command: lastCommand,
  });

  const states = {};
  const lightStates = {
    "light.hue_g_strip": { state: "on", attributes: { friendly_name: "Studio G Strip", brightness: 178 } },
    "light.lamp": { state: "on", attributes: { friendly_name: "Studio Lamp", brightness: 130 } },
    // Mirrors the live dead template light: unreachable, so the trim's mean
    // excludes it instead of reading (2*100 + 0) / 3 forever.
    "light.custom_gradient": { state: "unavailable", attributes: { friendly_name: "Custom Gradient" } },
  };
  Object.assign(states, lightStates);
  if (!scenario.noEntity) {
    states[PROJECTION_ENTITY] = { state: scenario.runtimeMode, attributes: buildAttributes() };
  }

  const publish = () => {
    if (states[PROJECTION_ENTITY]) {
      states[PROJECTION_ENTITY] = { state: scenario.runtimeMode, attributes: buildAttributes() };
    }
    onRender();
  };

  const applyCommand = (payload) => {
    revision += 1;
    const { command, request_id: requestId, scene_id: sceneId, session_id: sessionId } = payload;
    if (command === "playback.start") {
      sessions.push({ session_id: `mock-${Date.now()}`, scene_id: sceneId, state: "active", target_ids: ["studio"] });
    } else if (command === "playback.pause") {
      const s = sessions.find((x) => x.session_id === sessionId);
      if (s) s.state = "paused";
    } else if (command === "playback.resume") {
      const s = sessions.find((x) => x.session_id === sessionId);
      if (s) s.state = "active";
    } else if (command === "playback.stop") {
      const idx = sessions.findIndex((x) => x.session_id === sessionId);
      if (idx >= 0) sessions.splice(idx, 1);
    } else if (command === "scene.archive") {
      const idx = scenes.findIndex((x) => x.id === sceneId);
      if (idx >= 0) scenes.splice(idx, 1);
    }
    // Mirrors the real AppDaemon->HA attribute transport, which is lossy for
    // booleans: success acks arrive as the string "true", and rejections can
    // lose `ok` entirely (only `error` survives). The card must parse both.
    lastCommand = { request_id: requestId, ok: "true", command };
    publish();
  };

  const failCommand = (payload) => {
    lastCommand = {
      request_id: payload.request_id,
      command: payload.command,
      error: "Simulated failure for review",
    };
    publish();
  };

  const hass = {
    states,
    async callApi(_method, path, payload) {
      if (!path.startsWith(`events/${UI_COMMAND_EVENT}`)) return {};
      if (scenario.commandBehavior === "hang") {
        return new Promise(() => {}); // never resolves -> exercises the card's own timeout
      }
      if (scenario.commandBehavior === "delay") {
        await new Promise((r) => setTimeout(r, scenario.commandDelayMs ?? 2500));
        applyCommand(payload);
        return {};
      }
      if (scenario.commandBehavior === "fail") {
        failCommand(payload);
        return {};
      }
      applyCommand(payload);
      return {};
    },
    // Direct HA light services (the card's live brightness trim), mirrored
    // on the shared light states so scrub demos behave like real lights.
    async callService(domain, service, data = {}) {
      if (domain !== "light") return {};
      const ids = Array.isArray(data.entity_id) ? data.entity_id : [data.entity_id];
      for (const entityId of ids) {
        const entity = states[entityId];
        // Real HA skips unreachable lights (warning in the log, no state
        // change) — the mock must not resurrect the dead entity on turn_on.
        if (!entity || entity.state === "unavailable" || entity.state === "unknown") continue;
        if (service === "turn_off") {
          entity.state = "off";
        } else if (service === "turn_on") {
          entity.state = "on";
          if (typeof data.brightness_pct === "number") {
            entity.attributes.brightness = Math.round((data.brightness_pct / 100) * 255);
          }
        }
      }
      publish();
      return {};
    },
  };

  return hass;
}
