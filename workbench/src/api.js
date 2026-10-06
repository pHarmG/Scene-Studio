/**
 * Scene Studio client interface + mock and HTTP implementations (§7, §9.3).
 *
 * `SceneStudioClient` is the surface both clients implement:
 *
 * @typedef {object} SceneStudioClient
 * @property {() => object|Promise<object>} getStatus
 *   Engine-shaped status doc — the STABLE shape of
 *   `SceneStudioEngine.status()` (backend/src/scene_studio/.../service/engine.py):
 *   {
 *     engine:   {ok, revision, event_capacity, events, ...clientExtras},
 *     fixtures: {total, ready, missing, disabled, unbound, degraded, conflicting},
 *     providers:{<provider>: {total, ready, missing, degraded, other}},
 *     provider_links: [{provider, label, connected, detail}],  // Workbench-side
 *     current:  {scene_id, target_id, name?, target_ids?} | null,
 *     playback: {sessions: PlaybackSession[],
 *                counts: {active, paused, held, orphaned, stopped},
 *                owned_fixture_count},                     // R5A collection
 *     contention: {configured, default_policy, checked_at?, available,
 *                  detail?, streaming, streaming_sources[], instances[],
 *                  instance_mapping, held_fixture_ids[], held_owners{},
 *                  wled_probe_ok, pending_restore?, handback?}, // hyperHDR pass
 *     last_discovery: {run_id, started_at, finished_at, summary} | null,
 *     warnings: [{code, message, details?}],
 *   }
 *   Playback is the R5A session collection (see src/playback.js); an empty
 *   collection means nothing is playing. The display name is resolved from
 *   the scenes catalog by the views (sessions carry scene_name from R5A on).
 * @property {() => Promise<object>} getFixtures
 *   Registry doc {schema_version?, updated?, targets[], fixtures[]} with each
 *   fixture carrying derived `health` + `health_reason` (both clients derive
 *   via contracts §1 so views never branch on client type).
 * @property {() => Promise<object>} getScenes
 *   Scenes doc {scenes:[Scene v2 JSON]} — active + archived merged; archived
 *   scenes carry `metadata.archived_at`.
 * @property {() => Promise<object>} getFixtureState
 *   Normalized live color-state read model (the backend's
 *   `/fixture-state`; see domain/live_state.py): {observed_at,
 *   fixtures: {[fixture_id]: {fixture_id, provider, available, on,
 *   brightness, color_mode, display_colors, color_temp_kelvin, dynamic,
 *   state_kind, detail?}}, providers: {[provider]: {ok, detail}}}.
 *   Read-only and disposable — never touches `engine.revision`. The mock
 *   fabricates the same normalized shape directly (never reimplements
 *   provider normalization in JS).
 * @property {() => Promise<object|null>} getDiscovery
 *   DiscoveryReport JSON (same shape as discovery.sample.json), or null when
 *   the engine has no discovery report on record yet.
 * @property {(limit?: number) => Promise<object>} getRecentEvents
 *   {events: OperationalEventJSON[]} newest-first.
 * @property {({refresh?: boolean}?) => Promise<object>} getRoutines
 *   Derived HA routine projection (routines pass, GET /routines):
 *   {available, unavailable_reason?, routines: [RoutineProjection],
 *   refreshed_at?, stale}. A RoutineProjection mirrors domain/routines.py:
 *   {automation_id, entity_id, alias, enabled, classification
 *   ("native_routine"|"recognized_advanced"), scene_id, behavior
 *   ("apply"|"play"), schedule {time "HH:MM", weekdays|null},
 *   provenance?, source_digest, unsupported_reasons?}. Advanced routines
 *   are read-only through Scene Studio.
 * @property {(envelope: object) => Promise<object>} sendCommand
 *   CommandEnvelope -> CommandResult: {command, ok, request_id?, data?,
 *   error?{code,message,details?}} (mirrors commands.py). Throws ONLY on
 *   transport/network failure; command failures resolve to {ok:false,...}.
 *
 * The mock consumes the SAME JSON shapes as
 * `backend/fixtures/*.sample.json` (see mocks/base_data.js);
 * its dry-run render plans are Python-generated golden fixtures
 * (`mocks/render_plans/*.json`, regenerated via `npm run goldens`) — Python
 * is the canonical renderer and no JS rendering logic lives here. The
 * HTTP client speaks `service/api.py` route() over a selectable wire
 * transport — direct HTTP to the devserver, or the AppDaemon 4.5
 * named-endpoint JSON envelope — and is the production Workbench remote
 * (plan §10.1). See `createHttpSceneStudioClient`.
 *
 * This module is DOM-free so scripts/smoke.mjs can import it under Node.
 * Node callers must load the sample JSON themselves (fs) and pass them to
 * {@link createMockSceneStudioClient}; browser callers use mocks/base_data.js.
 */

import {
  SCENARIOS,
  buildScenarioData,
  buildDiscoveryReport,
  clone,
  deriveHealth,
  observationMatchesBinding,
} from "./mocks/scenarios.js";
// Golden dry-run plans, generated from the base samples by the REAL Python
// renderers (`python -m scene_studio.devmock`, or `npm run goldens`).
// SCENARIO-INDEPENDENT by decision: the mock serves the same base-sample
// plan for every scenario and for every target_id (goldens are built for
// each scene's own targets) — the devserver is the interactive truth.
// Static imports (not import.meta.glob) so this DOM-free module stays
// importable under plain Node (scripts/smoke.mjs).
import { buildMockLiveFixtureState } from "./mocks/live_state.js";
import { localBuild } from "./product.js";
import { ROUTINE_WEEKDAYS, formatRoutineTime12h, routineAlias, describeRoutineWeekdays } from "./routines.js";
import goldenAuroraFlow from "./mocks/render_plans/aurora_flow.json" with { type: "json" };
import goldenMeetingBlue from "./mocks/render_plans/meeting_blue.json" with { type: "json" };
import goldenTwilight from "./mocks/render_plans/twilight.json" with { type: "json" };

/** scene_id -> golden ({scene_id, target_ids, registry_updated, render_plan}). */
const GOLDEN_RENDER_PLANS = new Map(
  [
    goldenAuroraFlow,
    goldenMeetingBlue,
    goldenTwilight,
    // Mock-only alias: the "Paused Tropical Smoothie" scenario reuses Aurora
    // Flow's golden plan under the live scene's id.
    { ...goldenAuroraFlow, scene_id: "tropical_smoothie", target_ids: ["office", "office_strip"] },
  ].map((golden) => [golden.scene_id, golden])
);

/** Count fidelity levels across (already Python-rendered) fixture plans. */
function fidelitySummary(fixturePlans) {
  const summary = { native: 0, equivalent: 0, approximate: 0, unsupported: 0 };
  for (const p of fixturePlans) {
    if (summary[p.fidelity] !== undefined) summary[p.fidelity] += 1;
  }
  return summary;
}

/** Error codes mirrored from commands.py ErrorCode. */
export const ERROR_CODES = [
  "validation_error",
  "unknown_command",
  "not_found",
  "conflict",
  "provider_unavailable",
  "internal_error",
  "contended",
];

/**
 * JS mirror of COMMAND_CATALOG param schemas (commands.py).
 * required/optional: name -> spec; "str" (max 64), {str,max}, "bool",
 * {int,min,max}, "str[]".
 */
const PARAM_SPECS = {
  "scene.apply": {
    required: { scene_id: "str" },
    optional: {
      target_id: "str",
      dry_run: "bool",
      transition_ms: { int: true, min: 0, max: 60000 },
      contention_override: "str",
    },
  },
  "scene.preview": {
    required: { scene_id: "str" },
    optional: { target_id: "str", dry_run: "bool", transition_ms: { int: true, min: 0, max: 60000 } },
  },
  "scene.rename": { required: { scene_id: "str", name: { str: true, max: 128 } }, optional: {} },
  "scene.archive": { required: { scene_id: "str" }, optional: {} },
  "scene.restore": { required: { scene_id: "str" }, optional: {} },
  "scene.save": { required: { name: { str: true, max: 128 } }, optional: { target_id: "str" } },
  "scene.preview_draft": { required: { scene: { obj: true } }, optional: {} },
  "scene.play_draft": { required: { scene: { obj: true } }, optional: {} },
  "scene.create": { required: { scene: { obj: true } }, optional: { duplicate_of: "str" } },
  "scene.update": { required: { scene_id: "str", scene: { obj: true } }, optional: {} },
  "playback.start": { required: { scene_id: "str" }, optional: { target_id: "str", contention_override: "str" } },
  "playback.pause": { required: { session_id: "str" }, optional: {} },
  "playback.resume": { required: { session_id: "str" }, optional: { contention_override: "str" } },
  "playback.stop": { required: { session_id: "str" }, optional: {} },
  "fixture.enable": { required: { fixture_id: "str" }, optional: {} },
  "fixture.disable": { required: { fixture_id: "str" }, optional: {} },
  "fixture.set_contention_policy": {
    required: { fixture_id: "str", policy: "str" },
    optional: {},
  },
  "sync.suspend": { required: {}, optional: { target_id: "str", fixture_id: "str" } },
  "sync.resume": { required: {}, optional: { reassert_scene: "bool" } },
  "fixture.retry": { required: { fixture_id: "str" }, optional: {} },
  "fixture.identify": { required: { fixture_id: "str" }, optional: {} },
  "fixture.rebind": {
    required: { fixture_id: "str", observation_id: { str: true, max: 300 } },
    optional: {},
  },
  "fixture.rebind_preview": {
    required: { fixture_id: "str", observation_id: { str: true, max: 300 } }, optional: {},
  },
  "fixture.rebind_rollback": { required: { fixture_id: "str" }, optional: {} },
  "fixture.reconcile": {
    required: { fixture_id: "str", observation_id: { str: true, max: 300 } },
    optional: {},
  },
  "fixture.reconcile_preview": {
    required: { fixture_id: "str", observation_id: { str: true, max: 300 } },
    optional: {},
  },
  "fixture.adopt": {
    required: { observation_id: { str: true, max: 300 }, fixture_id: "str", name: { str: true, max: 128 } },
    optional: { groups: "str[]", enabled: "bool" },
  },
  "target.create": {
    required: { name: { str: true, max: 128 } },
    optional: { target_id: "str", description: { str: true, max: 512 }, fixture_ids: "str[]" },
  },
  "target.update": {
    required: { target_id: "str" },
    optional: { name: { str: true, max: 128 }, add_fixture_ids: "str[]", remove_fixture_ids: "str[]" },
  },
  "registry.migration_preview": { required: {}, optional: {} },
  "registry.migrate": { required: {}, optional: {} },
  "discovery.run": { required: {}, optional: { providers: "str[]" } },
  "diagnostics.export": {
    required: {},
    optional: { redact: "bool", recent_events: { int: true, min: 1, max: 1000 } },
  },
  // HA-native routine CRUD (routines pass). weekdays null = every day.
  "routine.create": {
    required: { scene_id: "str", behavior: "str", time: { str: true, max: 5 } },
    optional: { weekdays: "strListOrNull" },
  },
  "routine.update": {
    required: { automation_id: "str", source_digest: "str" },
    optional: { time: "str", weekdays: "strListOrNull", behavior: "str", scene_id: "str" },
  },
  "routine.delete": { required: { automation_id: "str", source_digest: "str" }, optional: {} },
  "routine.enable": { required: { automation_id: "str", source_digest: "str" }, optional: {} },
  "routine.disable": { required: { automation_id: "str", source_digest: "str" }, optional: {} },
};

/** Commands that mutate state and therefore bump the revision. */
const MUTATING = new Set([
  "scene.apply", "scene.rename", "scene.archive", "scene.restore", "scene.save",
  "scene.create", "scene.update", "scene.play_draft",
  "playback.start", "playback.pause", "playback.resume", "playback.stop",
  "fixture.enable", "fixture.disable", "fixture.retry", "fixture.rebind", "fixture.rebind_rollback",
  "fixture.reconcile", "registry.migrate",
  "fixture.set_contention_policy", "sync.suspend", "sync.resume",
  "discovery.run",
  "fixture.adopt", "target.create", "target.update",
  "routine.create", "routine.update", "routine.delete", "routine.enable", "routine.disable",
]);

/**
 * Mirror of service/policy.py (backend-owned runtime policy). The mock
 * engine itself stays unrestricted; the runtime block below is what views
 * read to gate actions. The first-run scenario reports registry_admin so
 * the UI gates provider writes exactly like the real backend would.
 */
const REGISTRY_ADMIN_COMMANDS = [
  // read-only base
  "scene.preview", "scene.preview_draft", "discovery.run", "fixture.retry",
  "fixture.rebind_preview", "fixture.reconcile_preview", "registry.migration_preview",
  "diagnostics.export",
  // registry/catalog mutations (provider writes stay blocked)
  "fixture.enable", "fixture.disable", "fixture.set_contention_policy",
  "fixture.reconcile", "fixture.rebind", "fixture.rebind_rollback",
  "fixture.adopt", "target.create", "target.update",
  "registry.migrate",
  "scene.rename", "scene.archive", "scene.restore", "scene.save", "scene.create", "scene.update",
];

const NORMAL_COMMANDS = [
  "scene.apply", "scene.archive", "scene.preview", "scene.rename",
  "scene.restore", "scene.save", "scene.preview_draft", "scene.play_draft", "scene.create",
  "scene.update", "playback.start", "playback.pause",
  "playback.resume", "playback.stop", "fixture.enable", "fixture.disable",
  "fixture.retry", "fixture.identify", "fixture.rebind", "fixture.rebind_preview", "fixture.rebind_rollback",
  "fixture.reconcile", "fixture.reconcile_preview", "fixture.adopt", "target.create", "target.update",
  "registry.migration_preview", "registry.migrate",
  "discovery.run", "diagnostics.export",
  // HA-native routine CRUD (routines pass): normal mode only, mirroring
  // service/policy.py (restricted modes reject external-system writes).
  "routine.create", "routine.update", "routine.delete", "routine.enable", "routine.disable",
];

function mockRuntime(scenarioId) {
  if (scenarioId === "empty-registry") {
    return {
      mode: "registry_admin",
      read_only: false,
      allowed_commands: [...REGISTRY_ADMIN_COMMANDS],
      provider_writes_blocked: true,
    };
  }
  return {
    mode: "normal",
    read_only: false,
    allowed_commands: [...NORMAL_COMMANDS],
    provider_writes_blocked: false,
  };
}

/** Internal command-service failure (mirrors commands.py failure()). */
class CommandFailure extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function typeLabel(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return "list";
  return typeof value;
}

/**
 * Validate flattened envelope params against a PARAM_SPECS entry.
 * Mirrors reject_unknown_keys/require_* from serde.py.
 */
function validateParams(spec, data) {
  const out = {};
  for (const [key, value] of Object.entries(data)) {
    const isRequired = key in spec.required;
    const rawSpec = isRequired ? spec.required[key] : spec.optional[key];
    if (!rawSpec) {
      throw new CommandFailure("validation_error", `unknown param '${key}'`, { path: `params.${key}` });
    }
    const specObj = rawSpec === "str" ? { str: true, max: 64 } : rawSpec === "bool" ? { bool: true } : rawSpec === "str[]" ? { strList: true } : rawSpec === "strListOrNull" ? { strListOrNull: true } : rawSpec;
    if (specObj.str) {
      if (typeof value !== "string") {
        throw new CommandFailure("validation_error", `expected a string`, { path: `params.${key}`, got: typeLabel(value) });
      }
      const max = specObj.max || 64;
      if (value.length > max) {
        throw new CommandFailure("validation_error", `exceeds max length ${max}`, { path: `params.${key}` });
      }
      out[key] = value;
    } else if (specObj.obj) {
      if (!isPlainObject(value)) {
        throw new CommandFailure("validation_error", "expected a JSON object", { path: `params.${key}` });
      }
      out[key] = value;
    } else if (specObj.bool) {
      if (typeof value !== "boolean") {
        throw new CommandFailure("validation_error", `expected a boolean`, { path: `params.${key}` });
      }
      out[key] = value;
    } else if (specObj.int) {
      if (!Number.isInteger(value) || value < specObj.min || value > specObj.max) {
        throw new CommandFailure("validation_error", `expected an integer between ${specObj.min} and ${specObj.max}`, { path: `params.${key}` });
      }
      out[key] = value;
    } else if (specObj.strList) {
      if (!Array.isArray(value) || value.some((v) => typeof v !== "string")) {
        throw new CommandFailure("validation_error", `expected a list of provider names`, { path: `params.${key}` });
      }
      out[key] = [...value];
    } else if (specObj.strListOrNull) {
      // weekdays-style params: a list of strings, or explicit null ("every day").
      if (value !== null && (!Array.isArray(value) || value.some((v) => typeof v !== "string"))) {
        throw new CommandFailure("validation_error", `expected a list of strings or null`, { path: `params.${key}` });
      }
      out[key] = value === null ? null : [...value];
    }
  }
  for (const key of Object.keys(spec.required)) {
    if (!(key in out)) {
      throw new CommandFailure("validation_error", `missing required param '${key}'`, { path: `params.${key}` });
    }
  }
  return out;
}

function slugifyId(name) {
  const slug = String(name).toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  const safe = /^[a-z]/.test(slug) ? slug : `scene_${slug}`;
  return safe.slice(0, 64);
}

// ---- Builder authoring mock support (Pass 2 plan §8) ----------------------
//
// The mock models the AUTHORING CONTRACT (validation + state transitions),
// not the renderers: preview draft plans reuse per-fixture fidelity buckets
// from the Python-generated goldens — no rendering logic lives in JS.

const MOTION_MODES = ["static", "palette_cycle", "effect"];
const MOTION_STRATEGIES = ["auto", "native_preferred", "static"];
const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;
/** FixtureState fields the canonical model accepts (scene_studio.domain.scenes). */
const FIXTURE_STATE_FIELDS = [
  "on",
  "brightness",
  "color",
  "palette_index",
  "color_temp_mirek",
  "gradient",
  "effect",
  "transition_ms",
  "provider_ext",
];
const PROVIDER_EXT_NAMESPACES = ["hue_v2", "wled", "ha_light"];
const SCENE_ID_RE = /^[a-z][a-z0-9_]{0,63}$/;

/** command failure shaped like the engine's validation errors (dotted path). */
function draftFailure(path, message) {
  return new CommandFailure("validation_error", `${path}: ${message}`, { path });
}

/**
 * Validate ONE FixtureState document (scene `default_state` or a
 * `fixture_states` value) against the canonical Scene v2 rules the engine
 * enforces — unknown fields, out-of-range values, and the "at least one
 * field" rule. Without this the mock would accept authoring mistakes the
 * live backend rejects, which is exactly the divergence these mocks exist to
 * prevent (Builder-expansion §2/§3 made these fields user-editable).
 */
function validateFixtureState(state, path) {
  if (!isPlainObject(state)) throw draftFailure(path, "expected a JSON object");
  for (const key of Object.keys(state)) {
    if (!FIXTURE_STATE_FIELDS.includes(key)) {
      throw draftFailure(`${path}.${key}`, "unknown field for a fixture state");
    }
  }
  if (state.on !== undefined && state.on !== null && typeof state.on !== "boolean") {
    throw draftFailure(`${path}.on`, "expected a boolean");
  }
  if (
    state.brightness !== undefined &&
    state.brightness !== null &&
    (typeof state.brightness !== "number" || state.brightness < 0 || state.brightness > 100)
  ) {
    throw draftFailure(`${path}.brightness`, "expected a number between 0 and 100");
  }
  if (
    state.color !== undefined &&
    state.color !== null &&
    (typeof state.color !== "string" || !HEX_COLOR_RE.test(state.color))
  ) {
    throw draftFailure(`${path}.color`, `expected #rrggbb hex color, got ${JSON.stringify(state.color)}`);
  }
  if (
    state.palette_index !== undefined &&
    state.palette_index !== null &&
    (!Number.isInteger(state.palette_index) || state.palette_index < 0 || state.palette_index > 23)
  ) {
    throw draftFailure(`${path}.palette_index`, "expected an integer between 0 and 23");
  }
  if (
    state.color !== undefined &&
    state.color !== null &&
    state.palette_index !== undefined &&
    state.palette_index !== null
  ) {
    throw draftFailure(path, "palette_index and color cannot both be set");
  }
  if (
    state.color_temp_mirek !== undefined &&
    state.color_temp_mirek !== null &&
    (!Number.isInteger(state.color_temp_mirek) || state.color_temp_mirek < 100 || state.color_temp_mirek > 1000)
  ) {
    throw draftFailure(`${path}.color_temp_mirek`, "expected an integer between 100 and 1000");
  }
  if (state.gradient !== undefined && state.gradient !== null) {
    if (!Array.isArray(state.gradient) || state.gradient.length > 64) {
      throw draftFailure(`${path}.gradient`, "must contain 0..64 colors");
    }
    state.gradient.forEach((color, index) => {
      if (typeof color !== "string" || !HEX_COLOR_RE.test(color)) {
        throw draftFailure(`${path}.gradient.${index}`, `expected #rrggbb hex color, got ${JSON.stringify(color)}`);
      }
    });
  }
  if (
    state.effect !== undefined &&
    state.effect !== null &&
    (typeof state.effect !== "string" || state.effect.length > 64)
  ) {
    throw draftFailure(`${path}.effect`, "expected a string of at most 64 characters");
  }
  if (
    state.transition_ms !== undefined &&
    state.transition_ms !== null &&
    (!Number.isInteger(state.transition_ms) || state.transition_ms < 0 || state.transition_ms > 60000)
  ) {
    throw draftFailure(`${path}.transition_ms`, "expected an integer between 0 and 60000");
  }
  if (state.provider_ext !== undefined && state.provider_ext !== null) {
    if (!isPlainObject(state.provider_ext)) throw draftFailure(`${path}.provider_ext`, "expected an object");
    for (const [provider, payload] of Object.entries(state.provider_ext)) {
      if (!PROVIDER_EXT_NAMESPACES.includes(provider)) {
        throw draftFailure(
          `${path}.provider_ext.${provider}`,
          `provider extension must be namespaced to one of: ${PROVIDER_EXT_NAMESPACES.join(", ")}`
        );
      }
      if (!isPlainObject(payload)) throw draftFailure(`${path}.provider_ext.${provider}`, "expected an object");
    }
  }
  const setsField =
    ["on", "brightness", "color", "palette_index", "color_temp_mirek", "gradient", "effect"].some(
      (key) => state[key] !== undefined && state[key] !== null
    ) || (isPlainObject(state.provider_ext) && Object.keys(state.provider_ext).length > 0);
  if (!setsField) throw draftFailure(path, "fixture state must set at least one field");
}

/** Validate the scene-level default state and every per-fixture override. */
function validateDraftStates(scene) {
  if (scene.default_state !== undefined && scene.default_state !== null) {
    validateFixtureState(scene.default_state, "scene.default_state");
  }
  if (scene.fixture_states !== undefined && scene.fixture_states !== null) {
    if (!isPlainObject(scene.fixture_states)) {
      throw draftFailure("scene.fixture_states", "expected an object keyed by fixture_id");
    }
    for (const [fixtureId, state] of Object.entries(scene.fixture_states)) {
      if (!SCENE_ID_RE.test(fixtureId)) {
        throw draftFailure(`scene.fixture_states.${fixtureId}`, "invalid fixture id");
      }
      validateFixtureState(state, `scene.fixture_states.${fixtureId}`);
    }
  }
}

/**
 * Light mock-side validation of a Builder scene payload — mirrors the
 * canonical Scene v2 domain rules the backend enforces, so mock and live
 * transports surface the same authoring mistakes at the same paths.
 */
function validateDraftScene(scene) {
  if (!isPlainObject(scene)) throw draftFailure("params.scene", "expected a JSON object");
  if (scene.schema_version !== undefined && scene.schema_version !== 2) {
    throw draftFailure("scene.schema_version", `expected 2, got ${JSON.stringify(scene.schema_version)}`);
  }
  if (typeof scene.name !== "string" || !scene.name.trim()) {
    throw draftFailure("scene.name", "expected a non-empty string");
  }
  if (scene.name.length > 128) throw draftFailure("scene.name", "exceeds 128 characters");
  if (!Array.isArray(scene.target_ids) || scene.target_ids.length === 0) {
    throw draftFailure("scene.target_ids", "must contain at least 1 item(s)");
  }
  if (!scene.target_ids.every((t) => typeof t === "string" && t.trim())) {
    throw draftFailure("scene.target_ids", "expected a list of strings");
  }
  if (scene.palette !== undefined && scene.palette !== null) {
    if (!Array.isArray(scene.palette) || scene.palette.length > 24) {
      throw draftFailure("scene.palette", "must contain 0..24 colors");
    }
    scene.palette.forEach((color, i) => {
      if (typeof color !== "string" || !HEX_COLOR_RE.test(color)) {
        throw draftFailure(`scene.palette.${i}`, `expected #rrggbb hex color, got ${JSON.stringify(color)}`);
      }
    });
  }
  if (scene.brightness !== undefined && scene.brightness !== null) {
    if (typeof scene.brightness !== "number" || scene.brightness < 0 || scene.brightness > 100) {
      throw draftFailure("scene.brightness", "expected a number between 0 and 100");
    }
  }
  if (scene.motion !== undefined && scene.motion !== null) {
    const motion = scene.motion;
    if (!isPlainObject(motion)) throw draftFailure("scene.motion", "expected an object");
    if (motion.mode !== undefined && motion.mode !== null && !MOTION_MODES.includes(motion.mode)) {
      throw draftFailure("scene.motion", `mode must be one of: ${MOTION_MODES.join(", ")}`);
    }
    if (motion.strategy !== undefined && motion.strategy !== null && !MOTION_STRATEGIES.includes(motion.strategy)) {
      throw draftFailure("scene.motion", `strategy must be one of: ${MOTION_STRATEGIES.join(", ")}`);
    }
    if (motion.speed !== undefined && (typeof motion.speed !== "number" || motion.speed < 0 || motion.speed > 1)) {
      throw draftFailure("scene.motion.speed", "expected a number between 0 and 1");
    }
  }
  validateDraftStates(scene);
}

/** Save-time target gate (mirrors the engine): declared target or fixture id. */
function assertDraftTargets(doc, registry) {
  const targetIds = new Set(registry.targets.map((t) => t.id));
  const fixtureIds = new Set(registry.fixtures.map((f) => f.id));
  (doc.target_ids || []).forEach((targetId, i) => {
    if (!targetIds.has(targetId) && !fixtureIds.has(targetId)) {
      throw draftFailure(`scene.target_ids.${i}`, `unknown target id '${targetId}': not a declared target or fixture`);
    }
  });
}

/**
 * Canonical identity rule (mirrors the engine's `_authoring_document`): a
 * missing/empty id is derived from the display name; an authoritative
 * update id wins. Palette hexes are normalized to lowercase.
 */
function normalizeDraftScene(scene, forcedId = null) {
  const doc = structuredClone(scene);
  if (!forcedId && (typeof doc.id !== "string" || !doc.id.trim())) {
    doc.id = slugifyId(doc.name);
  }
  if (forcedId) doc.id = forcedId;
  if (Array.isArray(doc.palette)) doc.palette = doc.palette.map((c) => c.toLowerCase());
  return doc;
}

/** Per-fidelity golden fixture plans (fixture_id -> first golden plan). */
const GOLDEN_FIXTURE_PLANS = new Map();
for (const golden of GOLDEN_RENDER_PLANS.values()) {
  for (const plan of golden.render_plan.fixture_plans) {
    if (!GOLDEN_FIXTURE_PLANS.has(plan.fixture_id)) GOLDEN_FIXTURE_PLANS.set(plan.fixture_id, plan);
  }
}

/**
 * Compose a preview RenderPlan for an unsaved draft: canonical target
 * resolution + health-based skipping + the canonical no-state skip over the
 * mock registry, per-fixture fidelity buckets reused from the Python
 * goldens. Operations are omitted ([]) — the mock preview is a
 * validation/fidelity surface, not an execution plan.
 */
function buildMockPreviewPlan(doc, registry, offline) {
  const resolved = new Map();
  const notes = [];
  for (const targetId of doc.target_ids || []) {
    const declared = registry.targets.some((t) => t.id === targetId);
    if (declared) {
      const members = registry.fixtures.filter((f) => Array.isArray(f.groups) && f.groups.includes(targetId));
      if (!members.length) notes.push(`target '${targetId}' has no member fixtures`);
      for (const f of members) resolved.set(f.id, f);
    } else {
      const byId = registry.fixtures.find((f) => f.id === targetId);
      if (byId) resolved.set(byId.id, byId);
      else notes.push(`target '${targetId}' did not resolve to any fixture (unknown target id)`);
    }
  }
  const fixtureStates = doc.fixture_states && typeof doc.fixture_states === "object" ? doc.fixture_states : {};
  const defaultState = doc.default_state === undefined ? null : doc.default_state;
  const fixturePlans = [];
  const skipped = [];
  for (const fixtureId of [...resolved.keys()].sort()) {
    const fixture = resolved.get(fixtureId);
    const health = deriveHealth(fixture, { offlineProviders: offline }).status;
    if (health === "disabled" || health === "missing" || health === "unbound") {
      skipped.push(fixtureId);
      const reason =
        health === "disabled" ? "disabled" : health === "missing" ? "missing (provider resource not observed)" : "unbound (no binding)";
      notes.push(`skipped fixture '${fixtureId}': ${reason}`);
      continue;
    }
    // Canonical resolution rule (contract §3): explicit fixture_states entry,
    // else default_state, else skip with a note — never an implicit state.
    if (!fixtureStates[fixtureId] && defaultState === null) {
      notes.push(`skipped fixture '${fixtureId}': no fixture_states entry and scene has no default_state`);
      continue;
    }
    const golden = GOLDEN_FIXTURE_PLANS.get(fixtureId);
    fixturePlans.push({
      fixture_id: fixtureId,
      provider: fixture.binding ? fixture.binding.provider : "unknown",
      fidelity: golden ? golden.fidelity : "native",
      reason: golden ? golden.reason : "mock preview plan (demo fidelity)",
      operations: [],
    });
  }
  return {
    scene_id: doc.id,
    target_ids: [...(doc.target_ids || [])],
    fixture_plans: fixturePlans,
    skipped_fixture_ids: skipped,
    notes,
  };
}

/**
 * Create a MockSceneStudioClient.
 * @param {{registry: object, scenes: object, discovery: object}} baseData
 *        verbatim sample JSON documents
 * @param {{scenarioId?: string=}} [options]
 * @returns {SceneStudioClient & {setScenario(id: string): void,
 *          getScenario(): string, listScenarios(): object[]}}
 */
export function createMockSceneStudioClient(baseData, options = {}) {
  let scenarioId = options.scenarioId || SCENARIOS[0].id;
  let data = buildScenarioData(baseData, scenarioId);
  let revision = 1;
  let mockSessionSeq = 0;
  let runSeq = 0;
  let requestSeq = 0;
  // Deterministic mock clock for events generated after scenario load.
  let clockMs = Date.parse("2026-09-10T21:05:00Z");
  const EVENT_TICK_MS = 60000;
  // Session-scope observation pool + candidates, mutated only via rebind.
  let observations = clone(data.observations);
  let candidates = clone(data.candidates);
  let discoveryRunMeta = clone(data.discoveryRunMeta);
  /** @type {object[]} OperationalEvent JSON, newest first */
  let events = clone(data.events);
  /** @type {object[]} live playback session collection (engine-shaped dicts) */
  let playbackSessions = [];
  const rebindReviews = new Map();
  const reconcileReviews = new Map();

  const nextIso = () => {
    clockMs += EVENT_TICK_MS;
    return new Date(clockMs).toISOString();
  };

  const pushEvent = (event) => {
    events.unshift({ timestamp: nextIso(), ...event });
  };

  const offlineProviders = () =>
    new Set(data.providers.filter((p) => !p.connected).map((p) => p.provider));

  /**
   * Render-plan bundle for a scene at command time: the Python golden for
   * migrated/sampled scenes (byte-identical demo data), or a preview-style
   * composed plan for Builder-authored scenes (Pass 2) so the full
   * save -> Apply/Play journey works in mock without inventing a second
   * JS renderer — composition reuses the canonical resolution/skip rules
   * and golden fidelity buckets, never per-provider payload generation.
   */
  const planBundleForScene = (scene, targetId = null) => {
    const golden = GOLDEN_RENDER_PLANS.get(scene.id);
    if (golden) {
      const plan = structuredClone(golden.render_plan);
      if (targetId) {
        const members = fixturesForTarget(data.registry, targetId);
        plan.fixture_plans = plan.fixture_plans.filter((p) => members.has(p.fixture_id));
      }
      plan.target_ids = targetId ? [targetId] : [...golden.target_ids];
      return plan;
    }
    return buildMockPreviewPlan(
      { ...scene, target_ids: targetId ? [targetId] : scene.target_ids || [] },
      data.registry,
      offlineProviders()
    );
  };

  const refreshDiscovery = () => {
    return buildDiscoveryReport(discoveryRunMeta, data.registry, observations, candidates);
  };

  const currentDiscovery = () => refreshDiscovery();

  const fixturesWithHealth = () =>
    data.registry.fixtures.map((f) => {
      const health = deriveHealth(f, { offlineProviders: offlineProviders() });
      return { ...f, health: health.status, health_reason: health.reason };
    });

  const fixtureCounts = () => {
    const counts = { total: 0, ready: 0, missing: 0, disabled: 0, degraded: 0, unbound: 0 };
    for (const f of fixturesWithHealth()) {
      counts.total += 1;
      counts[f.health] = (counts[f.health] || 0) + 1;
    }
    return counts;
  };

  // ---- playback session collection (R5C mock model) --------------------
  //
  // The mock keeps a REAL session collection (engine.py R5A shape), not a
  // single-slot playback record: multiple simultaneous sessions (including
  // two for the same scene with disjoint fixture sets), targeted
  // session-addressed pause/resume/stop, per-fixture execution records,
  // degraded sessions, orphans, and a bounded stopped-history retention.
  // Seeded sessions are realized from scenario playbackSpecs; fixture ids
  // and executions derive from the Python golden render plan so mock
  // execution data always resembles backend results already covered by the
  // Python tests. The mock never becomes a second engine: lifecycle rules
  // here are just enough fidelity to exercise the frontend contract.

  /** Retained stopped sessions before the oldest is dropped (mirrors the
   *  backend's bounded trim_stopped retention). */
  const MAX_STOPPED_RETAINED = 5;

  /** Contract §1 target membership: group members first, then a fixture id. */
  const fixturesForTarget = (registry, targetId) => {
    const byGroup = registry.fixtures.filter(
      (f) => Array.isArray(f.groups) && f.groups.includes(targetId)
    );
    if (byGroup.length > 0) return new Set(byGroup.map((f) => f.id));
    const byId = registry.fixtures.find((f) => f.id === targetId);
    return byId ? new Set([byId.id]) : new Set();
  };

  /**
   * Mock execution kind for one golden fixture plan — mirrors the corrected
   * R5D realizations (ALL dynamic Hue through the managed scene resource =
   * native_scene; native WLED effects; documented approximate static
   * fallbacks). Hue lights never realize via a dynamics.status light PUT.
   */
  const executionKindFor = (fixturePlan) => {
    const ops = fixturePlan.operations || [];
    const managedScene = ops.some((o) => o.op === "hue.put_scene_dynamic");
    if (managedScene) return "native_scene";
    if (fixturePlan.fidelity === "approximate" || fixturePlan.fidelity === "unsupported") {
      return "approximate_static";
    }
    if (fixturePlan.provider === "wled") return "native_effect";
    return "native_scene";
  };

  /**
   * Realize one mock PlaybackSession (engine-shaped dict) from a scene's
   * golden plan, restricted to a target subset and/or provider slice, with
   * optional per-fixture failure overrides for degraded coverage. When the
   * caller passes `planBundle` (Builder-authored scenes), the already
   * composed fixture plans are used verbatim.
   */
  const buildMockSession = ({ scene, state, session_id, target_id, providers, started_at, paused_at, stopped_at, stop_reason, failures, planBundle }) => {
    let plans;
    let planTargets;
    if (planBundle) {
      plans = planBundle.fixture_plans.filter((p) => !Array.isArray(providers) || providers.includes(p.provider));
      planTargets = [...(planBundle.target_ids || scene.target_ids || [])];
    } else {
      const golden = GOLDEN_RENDER_PLANS.get(scene.id);
      if (!golden) {
        throw new CommandFailure(
          "internal_error",
          `no mock render plan golden for scene '${scene.id}' (regenerate via npm run goldens)`
        );
      }
      plans = golden.render_plan.fixture_plans.filter((p) => {
        if (target_id) {
          const members = fixturesForTarget(data.registry, target_id);
          if (!members.has(p.fixture_id)) return false;
        }
        if (Array.isArray(providers) && !providers.includes(p.provider)) return false;
        return true;
      });
      planTargets = [...golden.target_ids];
    }
    const overrides = failures || {};
    return {
      session_id,
      scene_id: scene.id,
      scene_name: scene.name,
      target_ids: target_id ? [target_id] : planTargets,
      fixture_ids: plans.map((p) => p.fixture_id),
      state,
      started_at,
      ...(paused_at ? { paused_at } : {}),
      ...(stopped_at ? { stopped_at } : {}),
      ...(stop_reason ? { stop_reason } : {}),
      fixture_executions: plans.map((p) => {
        const override = overrides[p.fixture_id] || {};
        return {
          fixture_id: p.fixture_id,
          provider: p.provider,
          execution: override.execution || executionKindFor(p),
          fidelity: override.fidelity || p.fidelity,
          ok: override.ok !== undefined ? override.ok : true,
          detail: override.detail !== undefined ? override.detail : p.reason || "",
        };
      }),
    };
  };

  const nextSessionId = (sceneId) => {
    mockSessionSeq += 1;
    return `sess-mock-${mockSessionSeq}-${sceneId}`;
  };

  /** Realize the active scenario's seeded playback specs. */
  const seedPlaybackSessions = () => {
    playbackSessions = (data.playbackSpecs || []).map((spec) => {
      const scene = data.scenes.scenes.find((s) => s.id === spec.scene_id);
      if (!scene) throw new Error(`scenario playback spec references unknown scene '${spec.scene_id}'`);
      return buildMockSession({
        scene,
        state: spec.state,
        session_id: spec.session_id || nextSessionId(spec.scene_id),
        target_id: spec.target_id,
        providers: spec.providers,
        started_at: spec.started_at || nextIso(),
        paused_at: spec.paused_at,
        stopped_at: spec.stopped_at,
        stop_reason: spec.stop_reason,
        failures: spec.failures,
      });
    });
  };

  /** Engine-shaped `status().playback` view of the collection. */
  const playbackState = () => {
    const counts = { active: 0, paused: 0, held: 0, orphaned: 0, stopped: 0 };
    const owned = new Set();
    for (const session of playbackSessions) {
      counts[session.state] = (counts[session.state] || 0) + 1;
      if (session.state === "active" || session.state === "paused") {
        for (const id of session.fixture_ids) owned.add(id);
      }
    }
    const sessions = [...playbackSessions]
      .sort((a, b) => (a.started_at < b.started_at ? -1 : a.started_at > b.started_at ? 1 : a.session_id < b.session_id ? -1 : 1))
      .map((s) => structuredClone(s));
    return { sessions, counts, owned_fixture_count: owned.size };
  };

  /** Trim stopped-history retention, dropping the oldest stops first. */
  const trimStoppedSessions = () => {
    const stopped = playbackSessions
      .filter((s) => s.state === "stopped")
      .sort((a, b) => (a.stopped_at < b.stopped_at ? -1 : 1));
    for (const stale of stopped.slice(0, Math.max(0, stopped.length - MAX_STOPPED_RETAINED))) {
      playbackSessions = playbackSessions.filter((s) => s !== stale);
    }
  };

  /** Stop one live/orphaned session in place (targeted mutation only). */
  const stopSession = (session, reason, nowIso, preemptedBy) => {
    session.state = "stopped";
    session.stopped_at = nowIso;
    session.stop_reason = reason;
    if (preemptedBy) session.preempted_by = preemptedBy;
    trimStoppedSessions();
  };

  // Initial realization of the scenario's seeded sessions.
  seedPlaybackSessions();

  // ---- HA-native routine simulation (routines pass) ----------------------
  //
  // The mock holds routine PROJECTIONS (the /routines response shape),
  // seeded deterministically from the scenario's scenes — the same contract
  // domain/routines.py derives from real HA automations. CRUD handlers
  // exercise the backend's discipline: digest-checked mutations (structured
  // `routine_source_changed` conflicts), advanced routines read-only, and
  // `play` refused for static scenes. This is contract fidelity for the
  // frontend, never a second automation engine.

  let routineStore = [];
  let routineSeq = 0;

  /** Tiny deterministic content digest standing in for the backend's
   *  canonical-config sha256 (only equality behavior is contractual). */
  const routineDigest = (routine) => {
    const content = JSON.stringify([
      routine.automation_id, routine.alias, routine.scene_id, routine.behavior,
      routine.schedule && routine.schedule.time, routine.schedule && routine.schedule.weekdays,
      routine.enabled === false ? "off" : "on", routine.unsupported_reasons || [],
    ]);
    let hash = 5381;
    for (let i = 0; i < content.length; i += 1) hash = ((hash * 33) ^ content.charCodeAt(i)) >>> 0;
    return hash.toString(16).padStart(8, "0");
  };

  // `routineAlias`/`describeRoutineWeekdays`/`formatRoutineTime12h` come
  // from the shared DOM-free routines module (src/routines.js) so mock and
  // live UI text can never drift.

  const seededRoutine = (scene, { behavior, time, weekdays, classification = "native_routine", alias = null, unsupported_reasons = [] }) => {
    routineSeq += 1;
    const automationId = classification === "native_routine" ? `ssr_mock${String(routineSeq).padStart(8, "0")}` : `user_advanced_${routineSeq}`;
    const routine = {
      automation_id: automationId,
      entity_id: `automation.${automationId.replace(/-/g, "_")}`,
      alias: alias || (classification === "native_routine" ? routineAlias(scene.name, time, weekdays) : `${scene.name} occupancy automation`),
      enabled: true,
      classification,
      scene_id: scene.id,
      behavior,
      schedule: classification === "native_routine" ? { time, weekdays: weekdays ? [...weekdays] : null } : null,
      provenance: classification === "native_routine" ? { schema: 1, scene_id: scene.id, behavior } : null,
      unsupported_reasons,
    };
    routine.source_digest = routineDigest(routine);
    return routine;
  };

  const seedRoutines = () => {
    routineStore = [];
    routineSeq = 0;
    const scenes = data.scenes.scenes;
    const twilight = scenes.find((s) => s.id === "twilight");
    if (twilight) {
      routineStore.push(seededRoutine(twilight, { behavior: "apply", time: "19:30", weekdays: ["mon", "tue", "wed", "thu", "fri"] }));
    }
    const aurora = scenes.find((s) => s.id === "aurora_flow");
    if (aurora) {
      routineStore.push(seededRoutine(aurora, { behavior: "play", time: "22:00", weekdays: null }));
      routineStore.push(seededRoutine(aurora, {
        behavior: "apply",
        classification: "recognized_advanced",
        unsupported_reasons: ["automation has multiple actions", "unsupported trigger platform 'state'"],
      }));
    }
  };

  const findRoutine = (automationId) => routineStore.find((r) => r.automation_id === automationId);

  /** Shared CRUD gate: the routine must exist, be native, and still carry
   *  the caller's digest (structured conflict otherwise). */
  const requireEditableRoutine = (params) => {
    const routine = findRoutine(params.automation_id);
    if (!routine) {
      throw new CommandFailure("conflict", "Home Assistant changed this automation since it was loaded; refresh the routines and reapply your edit.", {
        kind: "routine_source_changed", automation_id: params.automation_id, current_digest: "",
      });
    }
    if (routine.classification !== "native_routine") {
      throw new CommandFailure("conflict", `automation '${routine.automation_id}' is advanced (Home Assistant managed); Scene Studio does not edit it`, {
        kind: "routine_advanced", automation_id: routine.automation_id, unsupported_reasons: routine.unsupported_reasons || [],
      });
    }
    if (params.source_digest !== routine.source_digest) {
      throw new CommandFailure("conflict", "Home Assistant changed this automation since it was loaded; refresh the routines and reapply your edit.", {
        kind: "routine_source_changed", automation_id: routine.automation_id, current_digest: routine.source_digest,
      });
    }
    return routine;
  };

  const sceneIsDynamic = (sceneId) => {
    const scene = data.scenes.scenes.find((s) => s.id === sceneId);
    return !!(scene && scene.motion && scene.motion.mode !== "static");
  };

  const validateRoutineSchedule = (time, weekdays) => {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(String(time || ""))) {
      throw new CommandFailure("validation_error", "expected a 'HH:MM' 24-hour time (e.g. '19:30')", { path: "params.time" });
    }
    if (weekdays !== null && weekdays !== undefined) {
      if (!Array.isArray(weekdays) || weekdays.length === 0 || weekdays.some((d) => !ROUTINE_WEEKDAYS.includes(d))) {
        throw new CommandFailure("validation_error", "expected weekday strings (mon..sun) or null", { path: "params.weekdays" });
      }
    }
  };

  // Initial routine projection seed (routines pass).
  seedRoutines();

  const currentDoc = () =>
    data.currentScene
      ? {
          // Engine current shape, enriched with display fields the mock knows.
          scene_id: data.currentScene.scene_id,
          target_id: null,
          name: data.currentScene.name,
          target_ids: [...(data.currentScene.target_ids || [])],
        }
      : null;

  /** Per-provider readiness buckets, mirroring engine.status()["providers"]. */
  const providerCatalog = () => {
    const doc = {};
    for (const f of fixturesWithHealth()) {
      if (!f.binding) continue; // engine counts only bound fixtures per provider
      const provider = f.binding.provider;
      const bucket = doc[provider] || (doc[provider] = { total: 0, ready: 0, missing: 0, degraded: 0, other: 0 });
      bucket.total += 1;
      if (f.health === "ready") bucket.ready += 1;
      else if (f.health === "missing") bucket.missing += 1;
      else if (f.health === "degraded") bucket.degraded += 1;
      else bucket.other += 1;
    }
    return doc;
  };

  const lastDiscoveryDoc = () => {
    const report = refreshDiscovery();
    return {
      run_id: report.run_id,
      started_at: report.started_at,
      finished_at: report.finished_at,
      summary: clone(report.summary),
    };
  };

  const getStatus = () => ({
    product: { name: "Scene Studio", build: localBuild, update: { state: "unchecked", message: "Updates have not been checked." } },
    // Engine-shaped status (SceneStudioEngine.status() stable keys, engine.py).
    engine: {
      ok: true,
      revision,
      event_capacity: 500,
      events: events.length,
      implementation: "mock", // mock-only extra
      scenario: scenarioId, // mock-only extra
    },
    // Backend-owned runtime policy mirror (service/policy.py): normal mode
    // for mature scenarios, registry_admin for the first-run scenario.
    runtime: mockRuntime(scenarioId),
    fixtures: { ...fixtureCounts(), conflicting: 0 },
    providers: providerCatalog(),
    provider_links: clone(data.providers), // connectivity/labels: Workbench-side doc
    current: currentDoc(),
    playback: playbackState(),
    // External light-sync contention (hyperHDR pass): idle/idle in the mock.
    contention: {
      configured: false,
      default_policy: "yield",
      available: false,
      detail: null,
      streaming: false,
      streaming_sources: [],
      instances: [],
      instance_mapping: { wled_instance_ids: null, hue_instance_ids: null },
      held_fixture_ids: [],
      held_owners: {},
      wled_probe_ok: null,
      pending_restore: null,
      handback: null,
    },
    last_discovery: lastDiscoveryDoc(),
    warnings: clone(data.warnings),
  });

  const result = (command, ok, requestId, payload) => {
    /** @returns {object} CommandResult JSON */
    const out = { command, ok };
    if (requestId) out.request_id = requestId;
    if (payload && payload.data && Object.keys(payload.data).length) out.data = payload.data;
    if (payload && payload.error) out.error = payload.error;
    return out;
  };

  const failureResult = (command, requestId, err) =>
    result(command, false, requestId, {
      error: { code: err.code, message: err.message, ...(Object.keys(err.details || {}).length ? { details: err.details } : {}) },
    });

  // ---- command handlers -------------------------------------------------
  const handlers = {
    "scene.apply": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      const plan = planBundleForScene(scene, params.target_id ?? null);
      if (params.dry_run) {
        return { data: { render_plan: plan, dry_run: true } };
      }
      // R5A: a static apply stops every live session owning an overlapping
      // fixture; disjoint sessions keep playing untouched.
      const appliedIds = new Set(plan.fixture_plans.map((p) => p.fixture_id));
      const nowIso = nextIso();
      const stoppedNames = [];
      for (const session of playbackSessions) {
        if (session.state !== "active" && session.state !== "paused") continue;
        if (session.fixture_ids.some((id) => appliedIds.has(id))) {
          stopSession(session, "scene applied", nowIso);
          stoppedNames.push(session.scene_name);
        }
      }
      data.currentScene = { scene_id: scene.id, name: scene.name, target_ids: [...plan.target_ids] };
      pushEvent({
        level: "info",
        category: "scene",
        summary: `${scene.name} started`,
        detail: `${plan.fixture_plans.length} fixtures • ${scene.motion && scene.motion.mode !== "static" ? "dynamic" : "static"}`,
        scene_id: scene.id,
      });
      for (const name of stoppedNames) {
        pushEvent({
          level: "info",
          category: "playback",
          summary: `${name} stopped (scene applied)`,
          scene_id: scene.id,
        });
      }
      return {
        data: {
          applied: true,
          scene_id: scene.id,
          target_ids: plan.target_ids,
          skipped_fixture_ids: plan.skipped_fixture_ids || [],
          fidelity_summary: fidelitySummary(plan.fixture_plans),
        },
      };
    },

    "scene.preview": (params) => {
      const dryParams = { ...params, dry_run: true };
      return handlers["scene.apply"](dryParams);
    },

    "scene.rename": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      const old = scene.name;
      scene.name = params.name;
      if (data.currentScene && data.currentScene.scene_id === scene.id) data.currentScene.name = params.name;
      // Sessions carry the scene name they started with — keep them in sync.
      for (const session of playbackSessions) {
        if (session.scene_id === scene.id) session.scene_name = params.name;
      }
      pushEvent({
        level: "info",
        category: "scene",
        summary: `Scene renamed: ${old} -> ${params.name}`,
        scene_id: scene.id,
      });
      return { data: { scene_id: scene.id, name: params.name } };
    },

    "scene.archive": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      if (scene.metadata && scene.metadata.archived_at) {
        throw new CommandFailure("conflict", `scene '${scene.id}' is already archived`);
      }
      // Archiving a playing scene stops its live sessions (engine parity).
      const nowIso = nextIso();
      for (const session of playbackSessions) {
        if (session.scene_id === scene.id && session.state !== "stopped") {
          stopSession(session, "scene archived", nowIso);
          pushEvent({
            level: "info",
            category: "playback",
            summary: `${session.scene_name} stopped (scene archived)`,
            scene_id: scene.id,
          });
        }
      }
      scene.metadata = { ...(scene.metadata || {}), archived_at: nextIso() };
      pushEvent({ level: "info", category: "scene", summary: `${scene.name} archived`, scene_id: scene.id });
      return { data: { scene_id: scene.id, archived_at: scene.metadata.archived_at } };
    },

    "scene.restore": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      if (!scene.metadata || !scene.metadata.archived_at) {
        throw new CommandFailure("conflict", `scene '${scene.id}' is not archived`);
      }
      delete scene.metadata.archived_at;
      pushEvent({ level: "info", category: "scene", summary: `${scene.name} restored`, scene_id: scene.id });
      return { data: { scene_id: scene.id, restored: true } };
    },

    "scene.save": (params) => {
      const id = slugifyId(params.name);
      if (data.scenes.scenes.some((s) => s.id === id)) {
        throw new CommandFailure("conflict", `scene id '${id}' already exists`);
      }
      const scene = {
        schema_version: 2,
        id,
        name: params.name,
        target_ids: params.target_id ? [params.target_id] : [],
        palette: [],
        motion: { mode: "static", speed: 0.0, strategy: "auto" },
        fixture_states: {},
        metadata: { origin: "saved_draft" },
      };
      data.scenes.scenes.push(scene);
      pushEvent({ level: "info", category: "scene", summary: `Scene saved: ${params.name}`, detail: "draft captured (mock; no provider read-back)", scene_id: id });
      return { data: { scene_id: id, name: params.name } };
    },

    // ---- Builder authoring (Pass 2 plan §4) -------------------------------

    "scene.preview_draft": (params) => {
      validateDraftScene(params.scene);
      const doc = normalizeDraftScene(params.scene);
      const plan = buildMockPreviewPlan(doc, data.registry, offlineProviders());
      return { data: { dry_run: true, scene: clone(doc), render_plan: plan } };
    },

    "scene.play_draft": (params) => {
      validateDraftScene(params.scene);
      const doc = normalizeDraftScene(params.scene);
      assertDraftTargets(doc, data.registry);
      const renderPlan = buildMockPreviewPlan(doc, data.registry, offlineProviders());
      const isDynamic = !!doc.motion && doc.motion.mode !== "static";
      if (isDynamic) {
        const plan = planBundleForScene(doc, null);
        const sessionId = nextSessionId(doc.id);
        const nowIso = nextIso();
        const requested = buildMockSession({
          scene: doc,
          state: "active",
          session_id: sessionId,
          target_id: null,
          started_at: nowIso,
          planBundle: plan,
        });
        for (const session of playbackSessions) {
          if (session.state !== "active" && session.state !== "paused") continue;
          if (session.fixture_ids.some((id) => requested.fixture_ids.includes(id))) {
            stopSession(session, "preempted", nowIso, sessionId);
            pushEvent({
              level: "info",
              category: "playback",
              summary: `${session.scene_name} stopped (preempted)`,
              detail: `superseded by session ${sessionId}`,
              scene_id: session.scene_id,
            });
          }
        }
        playbackSessions.push(requested);
        data.currentScene = { scene_id: doc.id, name: doc.name, target_ids: [...plan.target_ids] };
        const nativeCount = requested.fixture_executions.filter((e) => e.fidelity === "native").length;
        const skippedIds = plan.skipped_fixture_ids || [];
        pushEvent({
          level: "info",
          category: "playback",
          summary: `${doc.name} started`,
          detail: `${requested.fixture_ids.length} fixtures • dynamic • ${nativeCount} native` +
            (skippedIds.length ? ` • ${skippedIds.length} unavailable` : ""),
          scene_id: doc.id,
        });
        return {
          data: {
            dry_run: false,
            played: true,
            kind: "playback",
            scene: clone(doc),
            render_plan: renderPlan,
            session_id: sessionId,
            playback: playbackState(),
            skipped_fixture_ids: skippedIds,
          },
        };
      }
      const plan = planBundleForScene(doc, null);
      const appliedIds = new Set(plan.fixture_plans.map((p) => p.fixture_id));
      const nowIso = nextIso();
      const stoppedNames = [];
      for (const session of playbackSessions) {
        if (session.state !== "active" && session.state !== "paused") continue;
        if (session.fixture_ids.some((id) => appliedIds.has(id))) {
          stopSession(session, "scene applied", nowIso);
          stoppedNames.push(session.scene_name);
        }
      }
      data.currentScene = { scene_id: doc.id, name: doc.name, target_ids: [...plan.target_ids] };
      pushEvent({
        level: "info",
        category: "scene",
        summary: `${doc.name} started`,
        detail: `${plan.fixture_plans.length} fixtures • static`,
        scene_id: doc.id,
      });
      for (const name of stoppedNames) {
        pushEvent({
          level: "info",
          category: "playback",
          summary: `${name} stopped (scene applied)`,
          scene_id: doc.id,
        });
      }
      return {
        data: {
          dry_run: false,
          played: true,
          kind: "apply",
          scene: clone(doc),
          render_plan: renderPlan,
          applied: true,
          scene_id: doc.id,
          scene_name: doc.name,
          target_ids: plan.target_ids,
          skipped_fixture_ids: plan.skipped_fixture_ids || [],
          fidelity_summary: fidelitySummary(plan.fixture_plans),
        },
      };
    },

    "scene.create": (params) => {
      validateDraftScene(params.scene);
      const doc = normalizeDraftScene(params.scene);
      // Engine order: target validation gates BEFORE the id-collision check.
      assertDraftTargets(doc, data.registry);
      // Server/history provenance is never claimed by a create payload
      // (mirrors engine._CREATE_STRIPPED_METADATA_KEYS); only the engine
      // records a duplicate link, from the explicit `duplicate_of` param.
      const metadata = { ...(doc.metadata || {}) };
      for (const key of ["archived_at", "migrated_from_v1", "duplicated_from"]) delete metadata[key];
      if (params.duplicate_of) {
        const source = data.scenes.scenes.find((s) => s.id === params.duplicate_of);
        if (!source) throw new CommandFailure("not_found", `no such scene: '${params.duplicate_of}'`);
        metadata.duplicated_from = source.id;
      }
      if (Object.keys(metadata).length) doc.metadata = metadata;
      else delete doc.metadata;
      const existing = data.scenes.scenes.find((s) => s.id === doc.id);
      if (existing) {
        throw new CommandFailure(
          "conflict",
          existing.metadata && existing.metadata.archived_at
            ? `an archived scene already uses id '${doc.id}'; restore it instead of re-adding`
            : `active scene already exists: '${doc.id}'`
        );
      }
      data.scenes.scenes.push(doc);
      pushEvent({
        level: "info",
        category: "scene",
        summary: `${doc.name} created`,
        detail: params.duplicate_of
          ? `id ${doc.id}; duplicated from ${params.duplicate_of} (mock)`
          : `id ${doc.id}; authored in the Scene Builder (mock)`,
        scene_id: doc.id,
      });
      return { data: { scene: clone(doc) } };
    },

    "scene.update": (params) => {
      const existing = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!existing) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      if (existing.metadata && existing.metadata.archived_at) {
        throw new CommandFailure("conflict", `scene '${params.scene_id}' is archived; restore it before editing`);
      }
      const payload = params.scene;
      if (payload && typeof payload.id === "string" && payload.id.trim() && payload.id !== params.scene_id) {
        throw draftFailure(
          "params.scene.id",
          `scene id is immutable: payload carries '${payload.id}', but the update targets '${params.scene_id}'`
        );
      }
      validateDraftScene(payload);
      const doc = normalizeDraftScene(payload, params.scene_id);
      // Server/history metadata the Builder does not own survives the edit.
      for (const key of ["archived_at", "migrated_from_v1", "duplicated_from"]) {
        if (existing.metadata && existing.metadata[key] !== undefined) {
          doc.metadata = { ...(doc.metadata || {}), [key]: existing.metadata[key] };
        }
      }
      assertDraftTargets(doc, data.registry);
      const index = data.scenes.scenes.indexOf(existing);
      data.scenes.scenes[index] = doc;
      pushEvent({
        level: "info",
        category: "scene",
        summary: `${doc.name} updated`,
        detail: `id ${doc.id} (immutable); definition replaced (mock)`,
        scene_id: doc.id,
      });
      return { data: { scene: clone(doc) } };
    },

    "playback.start": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      const isDynamic = !!scene.motion && scene.motion.mode !== "static";
      if (!isDynamic) {
        throw new CommandFailure("conflict", `scene '${scene.id}' is static; use scene.apply`);
      }
      if (params.target_id && !(scene.target_ids || []).includes(params.target_id)) {
        throw new CommandFailure("validation_error", `scene '${scene.id}' does not cover target '${params.target_id}'`);
      }
      const plan = planBundleForScene(scene, params.target_id ?? null);
      // R5A preemption is per overlapping session: every live session
      // owning >=1 requested fixture stops in full; disjoint sessions
      // continue unaffected (this is what lets the same scene run twice
      // against disjoint target/provider subsets).
      const sessionId = nextSessionId(scene.id);
      const nowIso = nextIso();
      const requested = buildMockSession({
        scene,
        state: "active",
        session_id: sessionId,
        target_id: params.target_id ?? null,
        started_at: nowIso,
        planBundle: plan,
      });
      for (const session of playbackSessions) {
        if (session.state !== "active" && session.state !== "paused") continue;
        if (session.fixture_ids.some((id) => requested.fixture_ids.includes(id))) {
          stopSession(session, "preempted", nowIso, sessionId);
          pushEvent({
            level: "info",
            category: "playback",
            summary: `${session.scene_name} stopped (preempted)`,
            detail: `superseded by session ${sessionId}`,
            scene_id: session.scene_id,
          });
        }
      }
      playbackSessions.push(requested);
      data.currentScene = { scene_id: scene.id, name: scene.name, target_ids: [...plan.target_ids] };
      const nativeCount = requested.fixture_executions.filter((e) => e.fidelity === "native").length;
      const skippedIds = plan.skipped_fixture_ids || [];
      pushEvent({
        level: "info",
        category: "playback",
        summary: `${scene.name} started`,
        detail: `${requested.fixture_ids.length} fixtures • dynamic • ${nativeCount} native` +
          (skippedIds.length ? ` • ${skippedIds.length} unavailable` : ""),
        scene_id: scene.id,
      });
      return { data: { session_id: sessionId, playback: playbackState(), skipped_fixture_ids: skippedIds } };
    },

    "playback.pause": (params) => {
      const session = playbackSessions.find((s) => s.session_id === params.session_id);
      if (!session) {
        throw new CommandFailure("conflict", `unknown playback session '${params.session_id}'`);
      }
      // Backend pause is idempotent on paused sessions (R5A §1.3); anything
      // else that is not active cannot be paused.
      if (session.state === "paused") {
        return { data: { session_id: session.session_id, playback: playbackState() } };
      }
      if (session.state !== "active") {
        throw new CommandFailure("conflict", `playback session '${session.session_id}' is ${session.state}, not active`);
      }
      session.state = "paused";
      session.paused_at = nextIso();
      pushEvent({ level: "info", category: "playback", summary: `${session.scene_name} paused`, scene_id: session.scene_id });
      return { data: { session_id: session.session_id, playback: playbackState() } };
    },

    "playback.resume": (params) => {
      const session = playbackSessions.find((s) => s.session_id === params.session_id);
      if (!session) {
        throw new CommandFailure("conflict", `unknown playback session '${params.session_id}'`);
      }
      if (session.state !== "paused") {
        throw new CommandFailure("conflict", `playback session '${session.session_id}' is ${session.state}, not paused`);
      }
      session.state = "active";
      pushEvent({ level: "info", category: "playback", summary: `${session.scene_name} resumed`, scene_id: session.scene_id });
      return { data: { session_id: session.session_id, playback: playbackState() } };
    },

    "playback.stop": (params) => {
      const session = playbackSessions.find((s) => s.session_id === params.session_id);
      if (!session) {
        throw new CommandFailure("conflict", `unknown playback session '${params.session_id}'`);
      }
      if (session.state === "stopped") {
        throw new CommandFailure("conflict", `playback session '${session.session_id}' is already stopped`);
      }
      // Targeted stop: ONLY the addressed session terminates; sibling
      // sessions (even for the same scene) keep their state.
      stopSession(session, "stopped by user", nextIso());
      pushEvent({ level: "info", category: "playback", summary: `${session.scene_name} stopped`, scene_id: session.scene_id });
      return { data: { session_id: session.session_id, stopped: true } };
    },

    "fixture.enable": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      fixture.enabled = true;
      pushEvent({ level: "info", category: "fixture", summary: `${fixture.name} enabled`, fixture_id: fixture.id });
      return { data: { fixture_id: fixture.id, enabled: true, health: deriveHealth(fixture, { offlineProviders: offlineProviders() }).status } };
    },

    "fixture.disable": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      fixture.enabled = false;
      pushEvent({ level: "info", category: "fixture", summary: `${fixture.name} disabled`, fixture_id: fixture.id });
      return { data: { fixture_id: fixture.id, enabled: false, health: deriveHealth(fixture, { offlineProviders: offlineProviders() }).status } };
    },

    // ---- first-run registry bootstrap (mirrors engine._fixture_adopt /
    // _target_create / _target_update; the client never submits provider
    // payloads — binding/capabilities/assessment derive from the
    // observation server-side) ----

    "fixture.adopt": (params) => {
      const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
      if (!ID_PATTERN.test(params.fixture_id)) {
        throw new CommandFailure("validation_error", "fixture_id must match ^[a-z][a-z0-9_]{0,63}$", { path: "params.fixture_id" });
      }
      for (const group of params.groups || []) {
        if (!ID_PATTERN.test(group)) {
          throw new CommandFailure("validation_error", `group id '${group}' must match ^[a-z][a-z0-9_]{0,63}$`, { path: "params.groups" });
        }
      }
      const observationId = params.observation_id;
      const obs = observations.find((o) => `${o.provider}:${o.provider_resource_id}` === observationId);
      if (!obs) {
        throw new CommandFailure("not_found", `observation '${observationId}' not found in the latest discovery report`);
      }
      if (obs.metadata && obs.metadata.ha_aggregate) {
        throw new CommandFailure("conflict", `observation '${observationId}' is an HA aggregate/group helper, not an addressable fixture`);
      }
      if (data.registry.fixtures.some((f) => f.id === params.fixture_id)) {
        throw new CommandFailure("conflict", `fixture id already exists: '${params.fixture_id}'`);
      }
      let binding;
      if (obs.provider === "hue_v2") {
        const bridge = (obs.metadata && obs.metadata.bridge_id) || null;
        if (!bridge) {
          throw new CommandFailure(
            "conflict",
            `cannot determine bridge for hue observation '${observationId}': set the deployment's hue_bridge_id`
          );
        }
        binding = {
          provider: "hue_v2",
          bridge_id: bridge,
          resource_id: obs.provider_resource_id,
          resource_type: (obs.metadata && obs.metadata.resource_type) || "light",
        };
        if (obs.metadata && obs.metadata.hue_group_id) {
          binding.hue_group_id = obs.metadata.hue_group_id;
          binding.hue_group_type = obs.metadata.hue_group_type || "room";
        }
      } else if (obs.provider === "wled") {
        const resource = obs.provider_resource_id;
        let deviceId = resource;
        const segments = [];
        if (resource.includes(":seg:")) {
          const idx = resource.lastIndexOf(":seg:");
          deviceId = resource.slice(0, idx);
          const tail = resource.slice(idx + 5);
          if (/^\d+$/.test(tail)) segments.push(Number(tail));
        } else if (resource.endsWith(":dev")) {
          deviceId = resource.slice(0, -4);
        }
        binding = { provider: "wled", device_id: deviceId, segment_ids: segments };
        if (obs.endpoint_hint) binding.endpoint_hint = obs.endpoint_hint;
      } else {
        binding = { provider: "ha_light", ha_entity_id: obs.provider_resource_id };
      }
      const adoptedAt = nextIso();
      const fixture = {
        id: params.fixture_id,
        name: params.name,
        groups: [...(params.groups || [])],
        enabled: params.enabled !== false,
        binding,
        ...(obs.capabilities ? { capabilities: structuredClone(obs.capabilities) } : {}),
        ...(obs.device_profile ? { device_profile: structuredClone(obs.device_profile) } : {}),
        capability_assessment: {
          status: "unknown",
          reasons: ["Adopted from discovery; physical evidence not yet assessed"],
        },
        metadata: {
          adopted_from_observation: observationId,
          adopted_at: adoptedAt,
          adopted_run_id: discoveryRunMeta ? discoveryRunMeta.run_id : null,
        },
      };
      data.registry.fixtures.push(fixture);
      pushEvent({
        level: "info",
        category: "fixture",
        summary: `Adopted ${fixture.name}`,
        detail: `first-run bootstrap • ${binding.provider}`,
        fixture_id: fixture.id,
        provider: binding.provider,
      });
      return {
        data: {
          fixture: structuredClone(fixture),
          observation_id: observationId,
          run_id: discoveryRunMeta ? discoveryRunMeta.run_id : null,
        },
      };
    },

    "target.create": (params) => {
      const ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
      const slug = (name) =>
        name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 64);
      const targetId = params.target_id || slug(params.name);
      if (!ID_PATTERN.test(targetId)) {
        throw new CommandFailure("validation_error", "target_id must match ^[a-z][a-z0-9_]{0,63}$", { path: "params.target_id" });
      }
      if (data.registry.targets.some((t) => t.id === targetId)) {
        throw new CommandFailure("conflict", `target id already exists: '${targetId}'`);
      }
      for (const fid of params.fixture_ids || []) {
        if (!data.registry.fixtures.some((f) => f.id === fid)) {
          throw new CommandFailure("not_found", `fixture '${fid}' not found`);
        }
      }
      const target = { id: targetId, name: params.name };
      if (params.description) target.description = params.description;
      data.registry.targets.push(target);
      const assigned = [];
      for (const fid of params.fixture_ids || []) {
        const fixture = data.registry.fixtures.find((f) => f.id === fid);
        if (!Array.isArray(fixture.groups)) fixture.groups = [];
        if (!fixture.groups.includes(targetId)) {
          fixture.groups.push(targetId);
          assigned.push(fid);
        }
      }
      pushEvent({
        level: "info",
        category: "system",
        summary: `Target ${target.name} declared`,
        detail: `first-run bootstrap • ${assigned.length} member(s) assigned`,
      });
      return { data: { target: structuredClone(target), assigned_fixture_ids: assigned } };
    },

    "target.update": (params) => {
      const target = data.registry.targets.find((t) => t.id === params.target_id);
      if (!target) throw new CommandFailure("not_found", `target '${params.target_id}' not found`);
      for (const fid of [...(params.add_fixture_ids || []), ...(params.remove_fixture_ids || [])]) {
        if (!data.registry.fixtures.some((f) => f.id === fid)) {
          throw new CommandFailure("not_found", `fixture '${fid}' not found`);
        }
      }
      if (params.name) target.name = params.name;
      const assigned = [];
      for (const fid of params.add_fixture_ids || []) {
        const fixture = data.registry.fixtures.find((f) => f.id === fid);
        if (!Array.isArray(fixture.groups)) fixture.groups = [];
        if (!fixture.groups.includes(target.id)) {
          fixture.groups.push(target.id);
          assigned.push(fid);
        }
      }
      const removed = [];
      for (const fid of params.remove_fixture_ids || []) {
        const fixture = data.registry.fixtures.find((f) => f.id === fid);
        if (Array.isArray(fixture.groups) && fixture.groups.includes(target.id)) {
          fixture.groups = fixture.groups.filter((g) => g !== target.id);
          removed.push(fid);
        }
      }
      pushEvent({
        level: "info",
        category: "system",
        summary: `Target ${target.name} updated`,
        detail: `+${assigned.length}/-${removed.length} member(s)`,
      });
      return {
        data: {
          target: structuredClone(target),
          assigned_fixture_ids: assigned,
          removed_fixture_ids: removed,
        },
      };
    },

    "fixture.set_contention_policy": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      if (params.policy !== "yield" && params.policy !== "takeover" && params.policy !== "ignore" && params.policy !== "default") {
        throw new CommandFailure("validation_error", `policy must be one of: yield, takeover, ignore, default`, { path: "params.policy" });
      }
      fixture.contention_policy = params.policy === "default" ? null : params.policy;
      pushEvent({
        level: "info",
        category: "fixture",
        summary: `${fixture.name} contention policy set to ${params.policy}`,
        fixture_id: fixture.id,
      });
      return { data: { fixture: { id: fixture.id, contention_policy: fixture.contention_policy } } };
    },

    // External light-sync (hyperHDR) control: the mock has no hyperHDR to
    // talk to, so suspend reports an honest no-op with no instances and
    // resume refuses exactly like the engine when nothing was suspended.
    "sync.suspend": (params) => {
      pushEvent({ level: "info", category: "system", summary: "External light sync suspended (mock: no-op)" });
      return { data: { scope_fixture_ids: [], instances: [], handback_recorded: false } };
    },

    "sync.resume": (params) => {
      throw new CommandFailure("conflict", "no suspend record to resume (mock has no hyperHDR sync)");
    },

    "fixture.retry": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const health = deriveHealth(fixture, { offlineProviders: offlineProviders() });
      if (health.status === "missing" && fixture.binding && offlineProviders().has(fixture.binding.provider)) {
        throw new CommandFailure("provider_unavailable", `provider ${fixture.binding.provider} unreachable; cannot re-check ${fixture.name}`);
      }
      if (health.status === "missing") {
        throw new CommandFailure("provider_unavailable", `bound resource for ${fixture.name} still not observed`);
      }
      return { data: { fixture_id: fixture.id, health: health.status, checked_at: nextIso() } };
    },

    // Mirrors engine.py's _fixture_identify: a transient provider nudge
    // (never in MUTATING, never bumps revision). Hue per-light bindings and
    // HA lights get a real (mocked) native identify; grouped_light and WLED
    // report an honest ok:false receipt rather than fabricate one — same
    // truthful-to-provider-state rule the renderers follow.
    "fixture.identify": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      if (!fixture.binding) throw new CommandFailure("conflict", `fixture '${fixture.id}' has no provider binding to identify`);
      const provider = fixture.binding.provider;
      let receipt;
      if (provider === "hue_v2" && (fixture.binding.resource_type || "light") === "light") {
        receipt = { ok: true, provider, op: "hue.identify", detail: "identify sent (mock)" };
      } else if (provider === "ha_light") {
        receipt = { ok: true, provider, op: "ha.call_light_identify", detail: "identify sent (mock)" };
      } else {
        receipt = {
          ok: false,
          provider,
          op: "identify",
          detail:
            `identify is not supported yet for provider '${provider}'` +
            (provider === "hue_v2" ? " (grouped_light bindings expose no per-light identify)" : ""),
        };
      }
      pushEvent({
        level: receipt.ok ? "info" : "warning",
        category: "fixture",
        summary: `Identify ${fixture.name}: ${receipt.ok ? "sent" : "unavailable"}`,
        detail: receipt.detail,
        fixture_id: fixture.id,
        provider,
      });
      return { data: { fixture_id: fixture.id, provider, receipt } };
    },

    "fixture.rebind_preview": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const obs = observations.find((o) => `${o.provider}:${o.provider_resource_id}` === params.observation_id);
      if (!obs) throw new CommandFailure("not_found", `observation '${params.observation_id}' not found`);
      const prev = fixture.binding || {};
      let binding;
      if (obs.provider === "hue_v2") {
        binding = {
          provider: "hue_v2",
          bridge_id: prev.bridge_id || (obs.endpoint_hint || "").replace(/^bridge\s+/, "") || "unknown",
          resource_id: obs.provider_resource_id,
          resource_type: "light",
        };
        if (prev.provider === "hue_v2" && prev.ha_entity_id) binding.ha_entity_id = prev.ha_entity_id;
      } else if (obs.provider === "wled") {
        const segMatch = /:seg:(\d+)$/.exec(obs.provider_resource_id);
        binding = {
          provider: "wled",
          device_id: (obs.metadata && obs.metadata.device_id) || obs.provider_resource_id.split(":")[0],
          segment_ids: segMatch ? [parseInt(segMatch[1], 10)] : [],
        };
        if (obs.endpoint_hint) binding.endpoint_hint = obs.endpoint_hint;
      } else {
        binding = { provider: "ha_light", ha_entity_id: obs.provider_resource_id };
      }
      const current = fixture.capabilities || null;
      const candidate = obs.capabilities || null;
      const lost = current && candidate ? ["brightness", "color_xy", "dynamic_native", "cct"].filter((k) => current[k] && !candidate[k]) : [];
      const gained = current && candidate ? ["brightness", "color_xy", "dynamic_native", "cct"].filter((k) => !current[k] && candidate[k]) : [];
      const parity = !candidate ? "unknown" : lost.length ? "reduced" : gained.length ? "changed" : "preserved";
      const preview = { fixture_id: fixture.id, observation_id: params.observation_id, current_provider: prev.provider || null,
        candidate_provider: obs.provider, current_binding: clone(fixture.binding), candidate_binding: clone(binding),
        current_effective_capabilities: clone(current), candidate_effective_capabilities: clone(candidate),
        capabilities_gained: gained, capabilities_lost: lost, capabilities_changed: [],
        device_profile_delta: {}, capability_status_after: fixture.device_profile ? "unknown" : "unknown",
        affected_scene_ids: [], render_fidelity_impact: [], warnings: candidate ? [] : ["candidate observation has no effective capability set"],
        safe_to_apply: !!candidate, parity, requires_confirmation: parity !== "preserved" };
      rebindReviews.set(`${fixture.id}:${params.observation_id}`, { binding, preview, observation: clone(obs) });
      return { data: preview };
    },

    "fixture.rebind": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const review = rebindReviews.get(`${fixture.id}:${params.observation_id}`);
      if (!review || !review.preview.safe_to_apply) throw new CommandFailure("conflict", "review rebind before applying it");
      const obs = review.observation;
      const binding = review.binding;
      fixture.binding_history = [{ binding: clone(fixture.binding), capabilities: clone(fixture.capabilities), capability_assessment: clone(fixture.capability_assessment), device_profile: clone(fixture.device_profile), health: fixture.health || null, changed_at: nextIso() }, ...(fixture.binding_history || [])].slice(0, 8);
      fixture.binding = binding;
      fixture.capabilities = clone(obs.capabilities);
      fixture.capability_assessment = { status: fixture.device_profile ? "unknown" : "unknown", provider: obs.provider, observation_id: params.observation_id, reasons: ["Mock provider evidence" ] };
      delete fixture.health; // rebind clears scenario overrides -> derived healthy
      if (fixture.metadata) delete fixture.metadata.note;
      const stillCandidate = candidates.some((c) => c.fixture_id === fixture.id);
      candidates = candidates.filter((c) => c.fixture_id !== fixture.id);
      pushEvent({
        level: "info",
        category: "fixture",
        summary: `${fixture.name} rebound to ${obs.name || obs.provider_resource_id}`,
        detail: stillCandidate ? "explicit rebind via Workbench (candidate consumed)" : "explicit rebind via Workbench",
        fixture_id: fixture.id,
        provider: obs.provider,
      });
      return { data: { fixture: clone(fixture), preview: review.preview } };
    },

    "fixture.rebind_rollback": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const previous = (fixture.binding_history || []).shift();
      if (!previous) throw new CommandFailure("conflict", "fixture has no binding revision to roll back");
      fixture.binding = previous.binding; fixture.capabilities = previous.capabilities;
      fixture.capability_assessment = previous.capability_assessment; fixture.device_profile = previous.device_profile;
      if (previous.health) fixture.health = previous.health; else delete fixture.health;
      return { data: { fixture: clone(fixture) } };
    },

    "fixture.reconcile_preview": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const obs = observations.find((o) => `${o.provider}:${o.provider_resource_id}` === params.observation_id);
      if (!obs) throw new CommandFailure("not_found", `observation '${params.observation_id}' not found`);
      if (!observationMatchesBinding(obs, fixture.binding)) {
        throw new CommandFailure("conflict", `observation '${params.observation_id}' is a different provider/resource than the current binding; use fixture.rebind instead`);
      }
      const currentHealth = deriveHealth(fixture).status;
      const proposedHealth = fixture.enabled === false ? currentHealth : "ready";
      const currentProfile = fixture.device_profile || null;
      const proposedProfile = obs.device_profile
        ? { ...(currentProfile || {}), ...obs.device_profile, native_features: { ...((currentProfile && currentProfile.native_features) || {}), ...((obs.device_profile && obs.device_profile.native_features) || {}) } }
        : currentProfile;
      const currentCaps = fixture.capabilities || null;
      const proposedCaps = obs.capabilities || currentCaps;
      const pixels = proposedProfile && proposedProfile.native_features && proposedProfile.native_features.addressable_pixels;
      const proposedAssessment = {
        status: pixels && !(proposedCaps && proposedCaps.gradient) ? "limited" : ((fixture.capability_assessment && fixture.capability_assessment.status) || "unknown"),
        provider: obs.provider,
        observation_id: params.observation_id,
        reasons: pixels && !(proposedCaps && proposedCaps.gradient)
          ? ["Physical profile claims addressable_pixels, but selected provider exposes no gradient capability"]
          : ((fixture.capability_assessment && fixture.capability_assessment.reasons) || []),
      };
      const preview = {
        fixture_id: fixture.id,
        observation_id: params.observation_id,
        binding_unchanged: true,
        current_binding: clone(fixture.binding),
        proposed_binding: clone(fixture.binding),
        current: {
          operational_health: currentHealth,
          effective_capabilities: clone(currentCaps),
          device_profile: clone(currentProfile),
          capability_assessment: clone(fixture.capability_assessment || null),
        },
        proposed: {
          operational_health: proposedHealth,
          effective_capabilities: clone(proposedCaps),
          device_profile: clone(proposedProfile),
          capability_assessment: proposedAssessment,
        },
        capabilities_gained: [],
        capabilities_lost: [],
        capabilities_changed: [],
        device_profile_delta: {},
        assessment_changes: { before: clone(fixture.capability_assessment || null), after: proposedAssessment },
        affected_scene_ids: [],
        render_fidelity_impact: [],
        warnings: [],
        safe_to_apply: currentHealth !== proposedHealth || JSON.stringify(currentProfile) !== JSON.stringify(proposedProfile) || (fixture.capability_assessment && fixture.capability_assessment.status) !== proposedAssessment.status,
        requires_confirmation: false,
      };
      if (!preview.safe_to_apply) preview.warnings.push("no registry drift to apply");
      reconcileReviews.set(`${fixture.id}:${params.observation_id}`, { preview, observation: clone(obs) });
      return { data: preview };
    },

    "fixture.reconcile": (params) => {
      const fixture = data.registry.fixtures.find((f) => f.id === params.fixture_id);
      if (!fixture) throw new CommandFailure("not_found", `fixture '${params.fixture_id}' not found`);
      const review = reconcileReviews.get(`${fixture.id}:${params.observation_id}`);
      if (!review || !review.preview.safe_to_apply) throw new CommandFailure("conflict", "review reconcile before applying it");
      const obs = review.observation;
      if (!observationMatchesBinding(obs, fixture.binding)) {
        throw new CommandFailure("conflict", `observation '${params.observation_id}' is a different provider/resource than the current binding; use fixture.rebind instead`);
      }
      const priorBinding = clone(fixture.binding);
      fixture.binding_history = [{
        binding: clone(fixture.binding),
        capabilities: clone(fixture.capabilities),
        capability_assessment: clone(fixture.capability_assessment),
        device_profile: clone(fixture.device_profile),
        health: fixture.health || null,
        changed_at: nextIso(),
      }, ...(fixture.binding_history || [])].slice(0, 8);
      fixture.capabilities = clone(review.preview.proposed.effective_capabilities);
      fixture.device_profile = clone(review.preview.proposed.device_profile);
      fixture.capability_assessment = clone(review.preview.proposed.capability_assessment);
      delete fixture.health;
      if (JSON.stringify(fixture.binding) !== JSON.stringify(priorBinding)) {
        throw new CommandFailure("conflict", "reconcile mutated binding identity; rolled expectation violated");
      }
      refreshDiscovery();
      pushEvent({
        level: "info",
        category: "fixture",
        summary: `${fixture.name} registry reconciled from ${params.observation_id}`,
        detail: "same-binding reconcile; provider was not contacted",
        fixture_id: fixture.id,
        provider: obs.provider,
      });
      return { data: { fixture: clone(fixture), observation_id: params.observation_id, preview: review.preview } };
    },

    "registry.migration_preview": () => ({
      data: {
        required: false,
        from_schema_version: data.registry.schema_version || 2,
        to_schema_version: 2,
        registry: clone(data.registry),
      },
    }),

    "registry.migrate": () => ({
      data: {
        noop: true,
        reason: "already current",
        from_schema_version: data.registry.schema_version || 2,
        to_schema_version: 2,
        registry: clone(data.registry),
      },
    }),

    "discovery.run": (params) => {
      const requested = params.providers && params.providers.length ? params.providers : null;
      const offline = offlineProviders();
      const skippedOffline = requested
        ? [...offline].filter((p) => requested.includes(p))
        : [...offline];
      runSeq += 1;
      const startedIso = nextIso();
      discoveryRunMeta = {
        run_id: `run-20260910-${1906 + runSeq}`,
        started_at: startedIso,
        finished_at: new Date(Date.parse(startedIso) + 4000).toISOString(),
        providers: discoveryRunMeta.providers,
      };
      const report = refreshDiscovery();
      const changed = report.entries.filter(
        (e) => e.status !== "bound_ready" && e.detail !== "unchanged" && e.status !== "disabled"
      ).length;
      pushEvent({
        level: skippedOffline.length ? "warning" : "info",
        category: "discovery",
        summary: "Discovery completed",
        detail: `${changed} change${changed === 1 ? "" : "s"} detected` +
          (skippedOffline.length ? ` • providers skipped: ${skippedOffline.join(", ")} (offline)` : ""),
      });
      return { data: { report, skipped_providers: skippedOffline } };
    },

    "diagnostics.export": (params) => {
      const limit = params.recent_events ?? 100;
      return {
        data: {
          generated_at: new Date(clockMs).toISOString(),
          redacted: params.redact !== false,
          scenario: scenarioId,
          revision,
          fixture_summary: fixtureCounts(),
          events: clone(events.slice(0, limit)),
        },
      };
    },

    // ---- HA-native routine CRUD (routines pass) --------------------------

    "routine.create": (params) => {
      const scene = data.scenes.scenes.find((s) => s.id === params.scene_id);
      if (!scene) throw new CommandFailure("not_found", `scene '${params.scene_id}' not found`);
      if (!["apply", "play"].includes(params.behavior)) {
        throw new CommandFailure("validation_error", "must be 'apply' or 'play'", { path: "params.behavior" });
      }
      validateRoutineSchedule(params.time, params.weekdays);
      if (params.behavior === "play" && !sceneIsDynamic(scene.id)) {
        throw new CommandFailure("validation_error",
          `scene '${scene.id}' is static (motion.mode=static); scheduled behavior must be 'apply' — dynamic play requires a dynamic scene`,
          { path: "params.behavior" });
      }
      const weekdays = params.weekdays ? [...new Set(params.weekdays)].sort(
        (a, b) => ROUTINE_WEEKDAYS.indexOf(a) - ROUTINE_WEEKDAYS.indexOf(b)
      ) : null;
      const routine = seededRoutine(scene, { behavior: params.behavior, time: params.time, weekdays });
      routine.alias = routineAlias(scene.name, params.time, weekdays);
      routine.source_digest = routineDigest(routine);
      routineStore.push(routine);
      pushEvent({
        level: "info",
        category: "automation",
        summary: `Scheduled ${scene.name} · ${describeRoutineWeekdays(weekdays)} ${formatRoutineTime12h(params.time)}`,
        detail: `created HA automation ${routine.automation_id} (${params.behavior} ${scene.id})`,
        scene_id: scene.id,
      });
      return { data: { routine: clone(routine) } };
    },

    "routine.update": (params) => {
      const routine = requireEditableRoutine(params);
      const targetSceneId = params.scene_id ?? routine.scene_id;
      const behavior = params.behavior ?? routine.behavior;
      const time = params.time ?? routine.schedule.time;
      const weekdays = params.weekdays === undefined ? (routine.schedule.weekdays ? [...routine.schedule.weekdays] : null) : params.weekdays;
      const scene = data.scenes.scenes.find((s) => s.id === targetSceneId);
      if (!scene) throw new CommandFailure("not_found", `scene '${targetSceneId}' not found`);
      if (behavior === "play" && !sceneIsDynamic(targetSceneId)) {
        throw new CommandFailure("validation_error",
          `scene '${targetSceneId}' is static (motion.mode=static); scheduled behavior must be 'apply' — dynamic play requires a dynamic scene`,
          { path: "params.behavior" });
      }
      validateRoutineSchedule(time, weekdays);
      routine.scene_id = targetSceneId;
      routine.behavior = behavior;
      routine.schedule = { time, weekdays: weekdays ? [...new Set(weekdays)].sort(
        (a, b) => ROUTINE_WEEKDAYS.indexOf(a) - ROUTINE_WEEKDAYS.indexOf(b)
      ) : null };
      routine.alias = routineAlias(scene.name, time, routine.schedule.weekdays);
      routine.provenance = { schema: 1, scene_id: targetSceneId, behavior };
      routine.source_digest = routineDigest(routine);
      pushEvent({
        level: "info",
        category: "automation",
        summary: `Scheduled ${scene.name} · ${describeRoutineWeekdays(routine.schedule.weekdays)} ${formatRoutineTime12h(time)} updated`,
        detail: `updated HA automation ${routine.automation_id} (${behavior} ${targetSceneId})`,
        scene_id: targetSceneId,
      });
      return { data: { routine: clone(routine) } };
    },

    "routine.delete": (params) => {
      const routine = requireEditableRoutine(params);
      routineStore = routineStore.filter((r) => r !== routine);
      pushEvent({
        level: "info",
        category: "automation",
        summary: "Scheduled scene routine removed",
        detail: `deleted HA automation ${routine.automation_id}`,
        scene_id: routine.scene_id,
      });
      return { data: { removed: true } };
    },

    "routine.enable": (params) => {
      const routine = requireEditableRoutine(params);
      routine.enabled = true;
      routine.source_digest = routineDigest(routine);
      pushEvent({
        level: "info",
        category: "automation",
        summary: `Routine '${routine.alias}' enabled`,
        detail: `automation ${routine.automation_id} set to on`,
        scene_id: routine.scene_id,
      });
      return { data: { routine: clone(routine) } };
    },

    "routine.disable": (params) => {
      const routine = requireEditableRoutine(params);
      routine.enabled = false;
      routine.source_digest = routineDigest(routine);
      pushEvent({
        level: "info",
        category: "automation",
        summary: `Routine '${routine.alias}' disabled`,
        detail: `automation ${routine.automation_id} set to off`,
        scene_id: routine.scene_id,
      });
      return { data: { routine: clone(routine) } };
    },
  };

  // ---- public client ----------------------------------------------------
  const client = {
    /** Client kind marker: "mock" | "live". */
    mode: "mock",

    /** @returns {object} see SceneStudioClient#getStatus */
    getStatus,
    async checkUpdates() {
      return { state: "unavailable", message: "Update checks are available on an installed backend." };
    },

    async getFixtures() {
      const doc = clone(data.registry);
      doc.fixtures = fixturesWithHealth().map(({ health, health_reason, ...fixture }) => ({
        ...fixture,
        health,
        ...(health_reason ? { health_reason } : {}),
        // External light-sync contention (hyperHDR pass): the mock models
        // an idle external owner; the live client passes the engine's
        // derived per-fixture contention through untouched.
        contention: fixture.contention || { held: false, owner: null },
      }));
      return doc;
    },

    async getScenes() {
      return clone(data.scenes);
    },

    /** Derived HA routine projection (routines pass). */
    async getRoutines() {
      return {
        available: true,
        unavailable_reason: null,
        routines: clone(routineStore),
        refreshed_at: new Date(clockMs).toISOString(),
        stale: false,
      };
    },

    /** @returns {Promise<object>} see SceneStudioClient#getFixtureState */
    async getFixtureState() {
      return buildMockLiveFixtureState(fixturesWithHealth());
    },

    async getDiscovery() {
      return currentDiscovery();
    },

    async getRecentEvents(limit = 100) {
      return { events: clone(events.slice(0, limit)) };
    },

    /**
     * Validate a CommandEnvelope against the catalog and execute it.
     * @param {object} envelope flattened envelope {command, request_id?, ...params}
     * @returns {Promise<object>} CommandResult JSON
     */
    async sendCommand(envelope) {
      let command = null;
      let requestId = null;
      try {
        if (!isPlainObject(envelope)) {
          throw new CommandFailure("validation_error", "expected a command object", { path: "command" });
        }
        command = envelope.command;
        requestId = typeof envelope.request_id === "string" ? envelope.request_id : null;
        if (typeof command !== "string" || !command) {
          throw new CommandFailure("validation_error", "'command' must be a non-empty string", { path: "command.command" });
        }
        if (requestId && requestId.length > 64) {
          throw new CommandFailure("validation_error", "exceeds max length 64", { path: "command.request_id" });
        }
        if (!PARAM_SPECS[command]) {
          const known = Object.keys(PARAM_SPECS).sort().join(", ");
          throw new CommandFailure("unknown_command", `unknown command '${command}'; known commands: ${known}`, { path: "command.command" });
        }
        const reserved = ["command", "request_id"];
        const rawParams = Object.fromEntries(Object.entries(envelope).filter(([k]) => !reserved.includes(k)));
        const params = validateParams(PARAM_SPECS[command], rawParams);
        const dataOut = handlers[command](params);
        if (MUTATING.has(command)) revision += 1;
        return result(command, true, requestId, { data: (dataOut && dataOut.data) || {} });
      } catch (err) {
        if (err instanceof CommandFailure) {
          return failureResult(command || "unknown", requestId, err);
        }
        return failureResult(command || "unknown", requestId, new CommandFailure("internal_error", String((err && err.message) || err)));
      }
    },

    // ---- mock-only controls ----
    /** Switch the mock scenario; resets all mutated state deterministically. */
    setScenario(id) {
      if (!SCENARIOS.some((s) => s.id === id)) {
        throw new Error(`unknown scenario id: ${id}`);
      }
      scenarioId = id;
      data = buildScenarioData(baseData, id);
      observations = clone(data.observations);
      candidates = clone(data.candidates);
      discoveryRunMeta = clone(data.discoveryRunMeta);
      events = clone(data.events);
      seedPlaybackSessions();
      seedRoutines();
      revision += 1;
    },

    getScenario() {
      return scenarioId;
    },

    listScenarios() {
      return clone(SCENARIOS);
    },
  };

  return client;
}

// ---------------------------------------------------------------------------
// HTTP client (plan §9.3/§10.1) — speaks service/api.py route() over fetch
// ---------------------------------------------------------------------------

/** Display labels for the known providers (mirrors inspector.js). */
const PROVIDER_LABELS = { hue_v2: "Hue", wled: "WLED", ha_light: "HA" };

/**
 * Normalize a raw engine.status() payload into the canonical Workbench
 * status shape so views never branch on client type: fills conservative
 * defaults for every documented key and derives `provider_links`
 * (connectivity/labels are a Workbench-side concern; a live no-op executor
 * means every configured provider is local and reachable).
 */
export function normalizeEngineStatus(status) {
  const src = status && typeof status === "object" ? status : {};
  const providers =
    src.providers && typeof src.providers === "object" && !Array.isArray(src.providers) ? src.providers : {};
  return {
    ...src,
    engine: { ok: true, revision: 0, event_capacity: null, events: 0, ...(src.engine || {}) },
    fixtures: {
      total: 0, ready: 0, missing: 0, disabled: 0, unbound: 0, degraded: 0, conflicting: 0,
      ...(src.fixtures || {}),
    },
    providers,
    provider_links: Object.entries(providers).map(([provider, counts]) => ({
      provider,
      label: PROVIDER_LABELS[provider] || provider,
      connected: true,
      detail: "local engine",
      counts,
    })),
    current: src.current ?? null,
    playback: src.playback ?? null,
    last_discovery: src.last_discovery ?? null,
    warnings: [],
  };
}

/**
 * Default transport options applied when `createHttpSceneStudioClient` is
 * called without explicit options (e.g. by the state store, which always
 * constructs the live client with just the URL). The app shell layers its
 * persisted transport choice through `setDefaultHttpSceneStudioClientOptions`
 * BEFORE triggering a (re)connect, so the store needs no transport knowledge.
 */
let defaultHttpOptions = {};

export function setDefaultHttpSceneStudioClientOptions(options) {
  defaultHttpOptions = options && typeof options === "object" ? { ...options } : {};
}

/**
 * Create a live HTTP client against a Scene Studio dev/production server.
 * Same route surface as service/api.py (all paths under `/api/scene_studio`);
 * the wire transport is selectable via `options.transport`:
 *
 * - `"direct"` (default): GET/POST straight to `/api/scene_studio/*` — used
 *   by the devserver and any plain HTTP deployment.
 * - `"appdaemon"`: every call is POSTed as one JSON envelope to the single
 *   named AppDaemon endpoint `${base}/api/appdaemon/${options.endpointName}`
 *   (default "scene_studio_api"; the app name is NOT part of the URL — the
 *   AD 4.5 router is `/api/appdaemon/{endpoint}`, one segment). The envelope
 *   is `{method, path, query?, body?}`; the adapter always answers HTTP 200
 *   with `{status, body}` (AD 4.5 replaces non-200 bodies with HTML error
 *   pages), which is unwrapped here into the same (status, payload) shape
 *   the direct transport works with.
 *
 * Both transports implement the same SceneStudioClient interface. Throws
 * ONLY on network/transport failure — command failures arrive as
 * `{ok: false, error}` results, per the documented status-code policy (a
 * delivered command is always HTTP 200).
 *
 * @param {string} baseUrl e.g. "http://127.0.0.1:8765" or the AppDaemon host
 * @param {{transport?: "direct"|"appdaemon", endpointName?: string=}} [options]
 * @returns {SceneStudioClient & {mode: "live", baseUrl: string,
 *          transport: "direct"|"appdaemon", endpointName: string}}
 */
/** Default AppDaemon endpoint name registered by the Scene Studio adapter. */
export const DEFAULT_ENDPOINT_NAME = "scene_studio_api";

/**
 * Detect the AppDaemon-hosted production connection from the serving path.
 * AD natively serves the addon `www/` directory at `/local`, so the
 * deployed SPA lives at `/local/scene_studio/...` (a `static_dirs` entry
 * would serve `/scene_studio/...` equivalently). Either way the API is
 * same-origin — return appdaemon-transport defaults with an empty base URL.
 * Returns `null` everywhere else (dev server / file: keeps the mock-first
 * defaults).
 *
 * @param {string=} pathname window.location.pathname
 * @returns {{mode: "live", url: string, transport: "appdaemon", endpointName: string}|null}
 */
export function detectAppDaemonHosting(pathname = "") {
  return pathname.includes("/scene_studio")
    ? { mode: "live", url: "", transport: "appdaemon", endpointName: DEFAULT_ENDPOINT_NAME }
    : null;
}

export function createHttpSceneStudioClient(baseUrl, options = {}) {
  const merged = { ...defaultHttpOptions, ...options };
  const base = String(baseUrl || "").trim().replace(/\/+$/, "");
  const transport = merged.transport === "appdaemon" ? "appdaemon" : "direct";
  // AppDaemon-hosted production serves the SPA same-origin with the API at
  // /api/appdaemon/<endpoint>, so an empty baseUrl is valid for that
  // transport. The direct transport still requires an explicit base.
  if (!base && transport !== "appdaemon") {
    throw new Error("createHttpSceneStudioClient: baseUrl is required for the direct transport");
  }
  const endpointName = String(merged.endpointName || "scene_studio_api").trim().replace(/^\/+|\/+$/g, "");
  if (!endpointName) throw new Error("createHttpSceneStudioClient: endpointName is required in appdaemon transport");
  const adUrl = `${base}/api/appdaemon/${endpointName}`;

  const updateRequest = async (method, path, body) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch(transport === "appdaemon" ? `${base}/api/appdaemon/scene_studio_update_api` : `${base}/api/scene_studio${path}`, {
        method: transport === "appdaemon" ? "POST" : method,
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        ...(transport === "appdaemon" ? { body: JSON.stringify({ method, path, ...(body ? { body } : {}) }) }
          : body ? { body: JSON.stringify(body) } : {}),
      });
      if (!res.ok) {
        const error = new Error("Update executor unavailable. Provision the independent companion through the guided installer before applying updates.");
        error.rejected = true;
        throw error;
      }
      const envelope = await res.json();
      const code = transport === "appdaemon" ? envelope.status : res.status;
      const payload = transport === "appdaemon" ? envelope.body : envelope;
      if (!res.ok || ![200, 202].includes(code)) {
        const error = new Error("Update request rejected. Verify the update companion is installed and inspect System status.");
        error.rejected = true;
        throw error;
      }
      return payload;
    } finally { clearTimeout(timer); }
  };

  const fail = (what, err) =>
    new Error(`live connection failed (${what}): ${err && err.message ? err.message : String(err)}`);

  const nonEmpty = (query) =>
    query && typeof query === "object" && Object.keys(query).length ? query : null;

  /**
   * One wire request; resolves {status, payload} for any successfully
   * delivered route() outcome, throws on network/transport failure.
   * HTTP-level problems (404/400/500 pages, unexpected envelopes) also throw
   * — mirroring the direct transport's behavior for non-2xx responses.
   */
  const request = async (method, path, { query, body } = {}) => {
    let res;
    let text;
    if (transport === "appdaemon") {
      const envelope = { method, path };
      const cleanQuery = nonEmpty(query);
      if (cleanQuery) envelope.query = cleanQuery;
      if (body !== undefined) envelope.body = body;
      try {
        res = await fetch(adUrl, {
          method: "POST",
          signal: AbortSignal.timeout(10000),
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(envelope),
        });
      } catch (err) {
        throw fail(`POST ${adUrl}`, err);
      }
      text = await res.text();
      let envelopeOut = null;
      if (text) {
        try {
          envelopeOut = JSON.parse(text);
        } catch {
          envelopeOut = null;
        }
      }
      if (
        !res.ok ||
        envelopeOut === null ||
        typeof envelopeOut !== "object" ||
        typeof envelopeOut.status !== "number" ||
        !("body" in envelopeOut)
      ) {
        const detail =
          envelopeOut && envelopeOut.error ? envelopeOut.error.message : `${res.status} ${res.statusText}`;
        throw new Error(`POST ${adUrl} failed: ${detail}`);
      }
      return { status: envelopeOut.status, payload: envelopeOut.body };
    }
    // direct transport
    let url = base + path;
    const cleanQuery = nonEmpty(query);
    if (cleanQuery) url += `?${new URLSearchParams(cleanQuery).toString()}`;
    try {
      res = await fetch(url, body !== undefined
        ? {
            method: "POST",
            signal: AbortSignal.timeout(10000),
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        : { method, signal: AbortSignal.timeout(10000) });
    } catch (err) {
      throw fail(`${method} ${url}`, err);
    }
    text = await res.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }
    if (!res.ok) {
      const detail = payload && payload.error ? payload.error.message : `${res.status} ${res.statusText}`;
      throw new Error(`${method} ${path} failed: ${detail}`);
    }
    if (payload === null) {
      throw new Error(`${method} ${path} failed: unparseable response body`);
    }
    return { status: res.status, payload };
  };

  /** GET one JSON document; non-200 route status or transport failure -> throw. */
  const readJson = async (path, query) => {
    const { status, payload } = await request("GET", path, { query });
    if (status !== 200) {
      const detail = payload && payload.error ? payload.error.message : `route status ${status}`;
      throw new Error(`GET ${path} failed: ${detail}`);
    }
    return payload;
  };

  /** Derive contracts §1 health for every fixture in the registry doc. */
  const withDerivedHealth = (doc) => ({
    ...doc,
    fixtures: (doc.fixtures || []).map((f) => {
      const health = deriveHealth(f);
      return { ...f, health: health.status, ...(health.reason ? { health_reason: health.reason } : {}) };
    }),
  });

  return {
    mode: "live",
    baseUrl: base,
    transport,
    endpointName,

    async getStatus() {
      return normalizeEngineStatus(await readJson("/api/scene_studio/status"));
    },
    async checkUpdates() {
      const status = await readJson("/api/scene_studio/status", { check_updates: "true" });
      return status.product?.update || { state: "unavailable", message: "This backend does not support update checks." };
    },
    startUpdate(target_version) { return updateRequest("POST", "/update", { target_version }); },
    getUpdateStatus() { return updateRequest("GET", "/update/status"); },
    getUpdateBuild() { return readJson("/api/scene_studio/status"); },

    async getFixtures() {
      return withDerivedHealth(await readJson("/api/scene_studio/fixtures"));
    },

    async getScenes() {
      // Active + archived merged; the store stamps `metadata.archived_at`
      // on archived documents, matching the mock's single mixed doc.
      const [active, archived] = await Promise.all([
        readJson("/api/scene_studio/scenes"),
        readJson("/api/scene_studio/scenes", { archived: "true" }),
      ]);
      return { scenes: [...((active && active.scenes) || []), ...((archived && archived.scenes) || [])] };
    },

    /** Derived HA routine projection (routines pass); `refresh` forces a
     *  HA re-read (`?refresh=true`) instead of the bounded TTL cache. */
    async getRoutines(options = {}) {
      return readJson("/api/scene_studio/routines", options.refresh ? { refresh: "true" } : undefined);
    },

    /** @returns {Promise<object>} see SceneStudioClient#getFixtureState */
    async getFixtureState() {
      return readJson("/api/scene_studio/fixture-state");
    },

    async getDiscovery() {
      const payload = await readJson("/api/scene_studio/discovery");
      return payload ? payload.report : null;
    },

    async getRecentEvents(limit = 100) {
      const payload = await readJson("/api/scene_studio/diagnostics/recent", {
        limit: String(limit),
      });
      return { events: (payload && payload.events) || [] };
    },

    async sendCommand(envelope) {
      const { status, payload } = await request("POST", "/api/scene_studio/command", { body: envelope });
      // Delivered command (even a failed one) carries the result envelope.
      if (payload && typeof payload === "object" && typeof payload.ok === "boolean") {
        return payload;
      }
      if (status !== 200) {
        const detail = payload && payload.error ? payload.error.message : `route status ${status}`;
        throw new Error(`POST /command failed: ${detail}`);
      }
      return {
        command: envelope && typeof envelope.command === "string" ? envelope.command : null,
        ok: false,
        error: { code: "internal_error", message: "server returned an empty or malformed response body" },
      };
    },
  };
}
