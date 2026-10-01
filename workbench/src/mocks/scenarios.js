/**
 * Deterministic mock scenario transformations (master plan §7-A4, §10).
 *
 * Pure module: no DOM, no framework, no imports. Consumes the SAME JSON
 * shapes as `backend/fixtures/*.sample.json` and produces
 * scenario variants of `{registry, scenes, discovery inputs, events,
 * providers, warnings, playbackSpecs, currentScene}`.
 *
 * Health derivation follows ARCHITECTURE_CONTRACTS §1:
 *   explicit `health` override wins > `disabled` when enabled==false >
 *   `unbound` when no binding > `ready`.
 */

/** Allowed effective fixture health statuses (contract §1/§6). */
export const HEALTH_STATUSES = ["ready", "missing", "disabled", "degraded", "unbound"];

/** Scenario registry: order is the UI "Scenario" control order (default first). */
export const SCENARIOS = [
  {
    id: "all-healthy",
    label: "All healthy",
    description: "Every fixture bound and ready; clean discovery report.",
  },
  {
    id: "missing-fixture",
    label: "Missing fixture",
    description: "Double Strip is enabled but its bound resource is no longer observed (binding retained).",
  },
  {
    id: "disabled-fixture",
    label: "Disabled fixture",
    description: "Double Strip administratively disabled — the house-move leave-behind reference case (base sample verbatim).",
  },
  {
    id: "registry-stale",
    label: "Registry stale",
    description: "Custom Gradient is the same Hue light, but Scene Studio's registry knowledge is stale; Double Strip stays intentionally disabled.",
  },
  {
    id: "provider-offline",
    label: "Provider offline",
    description: "WLED provider unreachable; its six segments are treated as missing.",
  },
  {
    id: "replacement-candidate",
    label: "Replacement candidate",
    description: "Double Strip missing with a weak replacement candidate available in discovery.",
  },
  {
    id: "mixed-fidelity",
    label: "Mixed fidelity",
    description:
      "Twilight render plan mixes native, equivalent and approximate per-fixture fidelity (the degraded gradient falls back to its first color).",
  },
  {
    id: "dynamic-active",
    label: "Dynamic active",
    description:
      "Aurora Flow playing: native provider dynamics where supported (Hue dynamic_palette, WLED fx), approximate static snapshots elsewhere.",
  },
  {
    id: "multi-session",
    label: "Multi-session playback",
    description:
      "Two disjoint Aurora Flow sessions (Hue fixtures + WLED segments) for the SAME scene, independently controllable, plus a retained stopped session that is not live.",
  },
  {
    id: "playback-degraded",
    label: "Playback degraded",
    description:
      "Aurora Flow playing with partial provider failures: one WLED segment unreachable and one fixture with no representable realization (unsupported).",
  },
  {
    id: "playback-orphaned",
    label: "Playback orphaned",
    description:
      "Engine restart left Aurora Flow orphaned — the provider may still be animating; stop is the only lifecycle action. Includes a retained stopped session.",
  },
  {
    id: "playback-paused-tropical",
    label: "Paused Tropical Smoothie",
    description:
      "Tropical Smoothie (pink/yellow/orange/purple) paused across Office + Office strip with one degraded Hue fixture — mirrors the live header case.",
  },
  {
    id: "migration-warning",
    label: "Migration warning",
    description: "Legacy v1 scene files detected; warning banner + diagnostics event.",
  },
  {
    id: "empty-registry",
    label: "Empty install (first run)",
    description:
      "First-run bootstrap: no fixtures, no targets, no scenes — a fresh discovery report full of unbound observations, runtime registry_admin (provider writes blocked).",
  },
];

/** @returns {object} deep clone via structuredClone */
export function clone(value) {
  return structuredClone(value);
}

/**
 * Derive effective health for a fixture (contract §1).
 * @param {object} fixture registry fixture JSON
 * @param {{offlineProviders?: Set<string>=}} [ctx]
 * @returns {{status: string, reason: string}}
 */
export function deriveHealth(fixture, ctx = {}) {
  if (fixture.health) {
    return { status: fixture.health, reason: (fixture.metadata && fixture.metadata.note) || "" };
  }
  if (fixture.enabled === false) {
    return { status: "disabled", reason: "administratively disabled" };
  }
  if (!fixture.binding) {
    return { status: "unbound", reason: "no binding assigned" };
  }
  if (ctx.offlineProviders && ctx.offlineProviders.has(fixture.binding.provider)) {
    return { status: "missing", reason: `provider ${fixture.binding.provider} offline` };
  }
  return { status: "ready", reason: "" };
}

/**
 * Does a discovery observation correspond to a fixture binding?
 * @param {object} obs DiscoveryObservation JSON
 * @param {object} binding HueBinding|WledBinding|HaLightBinding JSON
 */
export function observationMatchesBinding(obs, binding) {
  if (!binding || obs.provider !== binding.provider) return false;
  if (binding.provider === "hue_v2") return obs.provider_resource_id === binding.resource_id;
  if (binding.provider === "wled") {
    const segs = Array.isArray(binding.segment_ids) ? binding.segment_ids : [];
    return segs.some((s) => obs.provider_resource_id === `${binding.device_id}:seg:${s}`);
  }
  if (binding.provider === "ha_light") return obs.provider_resource_id === binding.ha_entity_id;
  return false;
}

function observationId(obs) {
  return `${obs.provider}:${obs.provider_resource_id}`;
}

function capabilityDowngrades(stored, observed) {
  if (!stored || !observed) return [];
  const downs = [];
  if (stored.gradient && !observed.gradient) downs.push("gradient removed");
  if (stored.dynamic_native && !observed.dynamic_native) downs.push("dynamic_native lost");
  if (stored.color_xy && observed.color_xy === false) downs.push("color_xy lost");
  if (stored.brightness && observed.brightness === false) downs.push("brightness lost");
  return downs;
}

function knowledgeDriftReasons(fixture, observation) {
  const reasons = [];
  if (["degraded", "missing", "conflicting"].includes(fixture.health) && fixture.enabled !== false) {
    reasons.push("stale operational health override");
  }
  const currentProfile = fixture.device_profile || {};
  const observedProfile = observation.device_profile || {};
  const profileChanged =
    (observedProfile.manufacturer && observedProfile.manufacturer !== currentProfile.manufacturer) ||
    (observedProfile.model && observedProfile.model !== currentProfile.model) ||
    (!currentProfile.manufacturer && !currentProfile.model && (observedProfile.manufacturer || observedProfile.model));
  if (profileChanged) reasons.push("device profile can be improved");
  const currentStatus = fixture.capability_assessment && fixture.capability_assessment.status;
  const observedHasPixels = observedProfile.native_features && observedProfile.native_features.addressable_pixels;
  const storedHasPixels = currentProfile.native_features && currentProfile.native_features.addressable_pixels;
  const proposedLimited = !!(storedHasPixels || observedHasPixels) && !(observation.capabilities && observation.capabilities.gradient);
  const proposedStatus = proposedLimited ? "limited" : currentStatus || "unknown";
  if ((currentStatus || "unknown") !== proposedStatus) reasons.push("capability assessment can be updated");
  return reasons;
}

/**
 * Recompute a DiscoveryReport (entries + summary) from the registry,
 * observation pool and candidate list. Never mutates inputs.
 * @param {object} runMeta base report header ({run_id, started_at, finished_at, providers})
 * @param {object} registry registry doc (fixtures used in registry order)
 * @param {object[]} observations DiscoveryObservation[]
 * @param {object[]} candidates BindingCandidate[]
 * @returns {object} DiscoveryReport JSON
 */
export function buildDiscoveryReport(runMeta, registry, observations, candidates) {
  const fixtures = registry.fixtures;
  const matched = new Set();

  const entries = [];
  for (const fixture of fixtures) {
    const health = deriveHealth(fixture);
    const fixtureCandidates = candidates.filter((c) => c.fixture_id === fixture.id);
    let entry;
    const boundObs = observations.find((o) => observationMatchesBinding(o, fixture.binding));
    if (boundObs) matched.add(observationId(boundObs));
    if (health.status === "disabled") {
      entry = {
        status: "disabled",
        fixture_id: fixture.id,
        detail: "administratively disabled; discovery skipped provider lookup",
      };
    } else if (boundObs && capabilityDowngrades(fixture.capabilities, boundObs.capabilities).length) {
      entry = {
        status: "bound_degraded",
        fixture_id: fixture.id,
        observation_id: observationId(boundObs),
        detail: "capability downgrade: " + capabilityDowngrades(fixture.capabilities, boundObs.capabilities).join("; "),
      };
    } else if (boundObs && knowledgeDriftReasons(fixture, boundObs).length) {
      entry = {
        status: "bound_reconcile_available",
        fixture_id: fixture.id,
        observation_id: observationId(boundObs),
        detail: "same resource; registry update available: " + knowledgeDriftReasons(fixture, boundObs).join("; "),
      };
    } else if (health.status === "missing") {
      entry = {
        status: "missing",
        fixture_id: fixture.id,
        ...(boundObs ? { observation_id: observationId(boundObs) } : {}),
        detail: `${health.reason || "bound resource not observed"}; binding retained`,
      };
    } else {
      if (boundObs) {
        entry = {
          status: "bound_ready",
          fixture_id: fixture.id,
          observation_id: observationId(boundObs),
          detail: "unchanged",
        };
      } else {
        entry = {
          status: "missing",
          fixture_id: fixture.id,
          detail: "bound resource not observed; binding retained",
        };
      }
    }
    entries.push(entry);

    // Candidate entries mirror the sample: emitted for fixtures whose binding
    // is not currently healthy (missing), never for bound_ready / bound_degraded /
    // bound_reconcile_available.
    const showCandidate =
      fixtureCandidates.length > 0 &&
      entry.status !== "bound_ready" &&
      entry.status !== "bound_degraded" &&
      entry.status !== "bound_reconcile_available";
    if (showCandidate) {
      const weakest = fixtureCandidates[0];
      entries.push({
        status: "candidate_replacement",
        fixture_id: fixture.id,
        candidate_ids: fixtureCandidates.map((c) => `${c.fixture_id}:${c.observation_id}`),
        detail: `previous binding missing; ${weakest.compatibility} candidate available (review before rebinding)`,
      });
    }
  }

  for (const obs of observations) {
    if (obs && obs.metadata && obs.metadata.ha_aggregate) continue;
    if (!matched.has(observationId(obs))) {
      entries.push({
        status: "available_unbound",
        observation_id: observationId(obs),
        detail: obs.name ? `new resource "${obs.name}" with no fixture` : "new resource with no fixture",
      });
    }
  }

  const counts = {
    fixtures_bound_ready: 0,
    fixtures_reconcile_available: 0,
    fixtures_missing: 0,
    fixtures_disabled: 0,
    fixtures_degraded: 0,
    candidate_replacements: 0,
    unbound_observations: 0,
    conflicts: 0,
  };
  for (const entry of entries) {
    if (entry.status === "bound_ready") counts.fixtures_bound_ready += 1;
    else if (entry.status === "bound_reconcile_available") counts.fixtures_reconcile_available += 1;
    else if (entry.status === "missing") counts.fixtures_missing += 1;
    else if (entry.status === "disabled") counts.fixtures_disabled += 1;
    else if (entry.status === "bound_degraded") counts.fixtures_degraded += 1;
    else if (entry.status === "candidate_replacement") counts.candidate_replacements += 1;
    else if (entry.status === "available_unbound") counts.unbound_observations += 1;
  }

  return {
    run_id: runMeta.run_id,
    started_at: runMeta.started_at,
    finished_at: runMeta.finished_at,
    providers: runMeta.providers,
    observations: clone(observations),
    candidates: clone(candidates),
    entries,
    summary: {
      providers: new Set(observations.map((o) => o.provider)).size,
      observations_total: observations.length,
      ...counts,
    },
  };
}

/** Synthetic observation used when Double Strip is healthy (resource present). */
function doubleStripObservation() {
  return {
    provider: "hue_v2",
    provider_resource_id: "00000000-0000-4000-8000-000000000013",
    name: "Double Strip",
    location_hint: "Office",
    endpoint_hint: "bridge 001788demo000001",
    capabilities: { on_off: true, brightness: true, color_xy: true },
  };
}

/** Base event feed (newest first), in the spirit of master plan §10.7. */
function baseEvents() {
  return [
    {
      timestamp: "2026-09-10T21:04:00Z",
      level: "info",
      category: "scene",
      summary: "Twilight started",
      detail: "12 fixtures • static",
      scene_id: "twilight",
    },
    {
      timestamp: "2026-09-10T20:58:00Z",
      level: "info",
      category: "discovery",
      summary: "Discovery completed",
      detail: "0 changes detected",
    },
    {
      timestamp: "2026-09-10T20:41:00Z",
      level: "info",
      category: "system",
      summary: "Scene Studio engine started",
      detail: "mock client • contracts v1",
    },
  ];
}

const BASE_PROVIDERS = [
  { provider: "hue_v2", label: "Hue", connected: true, detail: "bridge 001788demo000001" },
  { provider: "wled", label: "WLED", connected: true, detail: "aabbccddeeff @ http://wled.local" },
  { provider: "ha_light", label: "HA", connected: true, detail: "Home Assistant light domain" },
];

const RUN_META = {
  run_id: "run-20260910-1905",
  started_at: "2026-09-10T19:05:00Z",
  finished_at: "2026-09-10T19:05:04Z",
  providers: ["hue_v2", "wled", "ha_light"],
};

const DOUBLE_STRIP_ID = "00000000-0000-4000-8000-000000000013";

function isDoubleStripObs(obs) {
  return obs.provider_resource_id === DOUBLE_STRIP_ID;
}

function isUnknownStripObs(obs) {
  return obs.provider_resource_id === "00000000-0000-4000-8000-000000000016";
}

function isWledObs(obs) {
  return obs.provider === "wled";
}

/**
 * Playback session spec (R5C): scenario-side SEED describing sessions to
 * realize. The mock client (api.js) turns each spec into a full
 * engine-shaped session — fixture ids and per-fixture execution records
 * derive from the Python golden render plan, so mock execution values
 * resemble real backend results. The mock never invents provider semantics
 * beyond this deterministic realization.
 *
 * @typedef {object} PlaybackSpec
 * @property {string} scene_id
 * @property {"active"|"paused"|"orphaned"|"stopped"} state
 * @property {string} [session_id] stable seeded id (start-generated
 *   sessions get their own `sess-mock-…` ids)
 * @property {string} [target_id] restrict ownership to one target subset
 * @property {string[]} [providers] restrict ownership to one provider slice
 *   (seed-only: lets one scene run as disjoint seeded sessions)
 * @property {string} [started_at]
 * @property {string} [paused_at]
 * @property {string} [stopped_at]
 * @property {string} [stop_reason]
 * @property {Record<string, {ok?: boolean, detail?: string, fidelity?: string, execution?: string}>} [failures]
 *   per-fixture execution overrides for degraded coverage
 */

/**
 * Build the full scenario dataset from the base sample data.
 * Deterministic: same base bytes + scenario id => same output.
 *
 * @param {object} base {registry, scenes, discovery}
 * @param {string} scenarioId one of SCENARIOS[].id
 * @returns {{
 *   registry: object,
 *   scenes: object,
 *   observations: object[],
 *   candidates: object[],
 *   discoveryRunMeta: object,
 *   providers: object[],
 *   warnings: object[],
 *   playbackSpecs: PlaybackSpec[],
 *   currentScene: {scene_id:string, name:string, target_ids:string[]}|null,
 *   events: object[],
 * }}
 */
export function buildScenarioData(base, scenarioId) {
  const registry = clone(base.registry);
  const scenes = clone(base.scenes);
  const baseObs = base.discovery.observations;
  const baseCandidates = base.discovery.candidates;

  let observations = clone(baseObs);
  let candidates = clone(baseCandidates);
  let providers = clone(BASE_PROVIDERS);
  let warnings = [];
  let playbackSpecs = [];
  let currentScene = null;
  let events = baseEvents();
  let runMeta = { ...RUN_META };
  /** null = the mock default (normal); "registry_admin" for the first-run scenario. */
  let runtimeMode = null;

  /** shared edits for a clean-slate healthy baseline */
  const normalizeHealthy = () => {
    for (const f of registry.fixtures) {
      if (!f.enabled) f.enabled = true; // double_strip + the two not-in-use lights are all bound
    }
    const cg = registry.fixtures.find((f) => f.id === "custom_gradient");
    delete cg.health;
    if (cg.metadata) delete cg.metadata.note;
    if (!observations.some(isDoubleStripObs)) {
      observations = [...observations, doubleStripObservation()];
    }
    observations = observations.filter((o) => !isUnknownStripObs(o));
    candidates = [];
  };

  /** mark a fixture explicitly missing with a reason */
  const markMissing = (fixtureId, reason) => {
    const f = registry.fixtures.find((x) => x.id === fixtureId);
    f.health = "missing";
    f.metadata = { ...(f.metadata || {}), note: reason };
  };

  switch (scenarioId) {
    case "all-healthy": {
      normalizeHealthy();
      break;
    }

    case "missing-fixture": {
      normalizeHealthy();
      observations = observations.filter((o) => !isDoubleStripObs(o));
      markMissing("double_strip", "bound resource not observed");
      events = [
        {
          timestamp: "2026-09-10T20:57:00Z",
          level: "warning",
          category: "fixture",
          summary: "Double Strip became unavailable",
          detail: "Binding retained",
          fixture_id: "double_strip",
          provider: "hue_v2",
        },
        ...events,
      ];
      break;
    }

    case "disabled-fixture": {
      // Base sample verbatim: Double Strip disabled (house-move reference case).
      break;
    }

    case "registry-stale": {
      const cg = registry.fixtures.find((f) => f.id === "custom_gradient");
      cg.health = "degraded";
      cg.capability_assessment = { status: "unknown", reasons: ["stale registry"] };
      delete cg.device_profile;
      const obs = observations.find((o) => o.provider_resource_id === "00000000-0000-4000-8000-000000000017");
      if (obs) {
        obs.device_profile = {
          manufacturer: "GLEDOPTO",
          model: "GL-C-103P",
          product_name: "Custom Gradient controller",
          source: "hue_device",
        };
        obs.capabilities = {
          ...(obs.capabilities || {}),
          on_off: true,
          brightness: true,
          color_xy: true,
          color_temp: { mirek_min: 158, mirek_max: 495 },
          dynamic_native: false,
        };
      }
      break;
    }

    case "provider-offline": {
      normalizeHealthy();
      observations = observations.filter((o) => !isWledObs(o));
      for (const f of registry.fixtures) {
        if (f.binding && f.binding.provider === "wled") {
          f.health = "missing";
          f.metadata = { ...(f.metadata || {}), note: "provider wled offline" };
        }
      }
      providers = providers.map((p) =>
        p.provider === "wled" ? { ...p, connected: false, detail: "unreachable (mock scenario)" } : p
      );
      events = [
        {
          timestamp: "2026-09-10T21:02:00Z",
          level: "error",
          category: "system",
          summary: "WLED provider unreachable",
          detail: "6 fixtures treated as missing; retry discovery after the device returns",
          provider: "wled",
        },
        ...events,
      ];
      break;
    }

    case "replacement-candidate": {
      // Base observations already include the unknown strip + no Double Strip obs.
      const ds = registry.fixtures.find((f) => f.id === "double_strip");
      ds.enabled = true;
      observations = baseObs.filter((o) => !isDoubleStripObs(o));
      candidates = baseCandidates.filter((c) => c.fixture_id === "double_strip");
      events = [
        {
          timestamp: "2026-09-10T20:58:30Z",
          level: "warning",
          category: "discovery",
          summary: "Replacement candidate found for Double Strip",
          detail: "Weak match: Unknown Gradient Strip (confidence 0.35) — review before rebinding",
          fixture_id: "double_strip",
          provider: "hue_v2",
        },
        ...events.filter((e) => e.category !== "discovery" || e.summary !== "Discovery completed"),
      ];
      break;
    }

    case "mixed-fidelity": {
      normalizeHealthy();
      // Custom Gradient degraded again: gradient capability lost.
      const cg = registry.fixtures.find((f) => f.id === "custom_gradient");
      cg.health = "degraded";
      cg.metadata = { ...(cg.metadata || {}), note: "reported gradient capability lost after firmware update" };
      // Twilight now asks Custom Gradient for a gradient -> unsupported.
      const twilight = scenes.scenes.find((s) => s.id === "twilight");
      twilight.fixture_states.custom_gradient = {
        on: true,
        brightness: 50.0,
        gradient: ["#1a237e", "#4527a0", "#7b1fa2", "#ff8f00"],
      };
      events = [
        {
          timestamp: "2026-09-10T21:03:00Z",
          level: "info",
          category: "scene",
          summary: "Twilight preview shows mixed fidelity",
          detail: "native 7 • equivalent 4 • approximate 1",
          scene_id: "twilight",
        },
        ...events,
      ];
      break;
    }

    case "dynamic-active": {
      normalizeHealthy();
      currentScene = { scene_id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"] };
      // R5C collection seed: one active Aurora Flow session realized by the
      // mock client from the Python golden plan (per-fixture executions).
      playbackSpecs = [
        {
          session_id: "sess-mock-seed-aurora",
          scene_id: "aurora_flow",
          state: "active",
          started_at: "2026-09-10T21:04:00Z",
        },
      ];
      events = [
        {
          timestamp: "2026-09-10T21:04:00Z",
          level: "info",
          category: "playback",
          summary: "Aurora Flow started",
          detail: "13 fixtures • dynamic • native dynamics where supported, snapshots elsewhere",
          scene_id: "aurora_flow",
        },
        ...events.slice(1),
      ];
      break;
    }

    case "multi-session": {
      // Same scene, TWO disjoint live sessions (per-fixture ownership is the
      // backend invariant — not one session per scene) + one retained
      // stopped session that must never render as live.
      normalizeHealthy();
      currentScene = { scene_id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"] };
      playbackSpecs = [
        {
          session_id: "sess-mock-seed-aurora-hue",
          scene_id: "aurora_flow",
          state: "active",
          providers: ["hue_v2"],
          started_at: "2026-09-10T21:04:00Z",
        },
        {
          session_id: "sess-mock-seed-aurora-wled",
          scene_id: "aurora_flow",
          state: "active",
          providers: ["wled"],
          started_at: "2026-09-10T21:06:00Z",
        },
        {
          session_id: "sess-mock-seed-aurora-past",
          scene_id: "aurora_flow",
          state: "stopped",
          providers: ["wled"],
          started_at: "2026-09-10T20:44:00Z",
          stopped_at: "2026-09-10T20:58:00Z",
          stop_reason: "stopped by user",
        },
      ];
      events = [
        {
          timestamp: "2026-09-10T21:06:00Z",
          level: "info",
          category: "playback",
          summary: "Aurora Flow started (second session)",
          detail: "2 live sessions for one scene • disjoint fixture sets (Hue / WLED)",
          scene_id: "aurora_flow",
        },
        ...events.slice(1),
      ];
      break;
    }

    case "playback-degraded": {
      normalizeHealthy();
      currentScene = { scene_id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"] };
      playbackSpecs = [
        {
          session_id: "sess-mock-seed-aurora-degraded",
          scene_id: "aurora_flow",
          state: "active",
          started_at: "2026-09-10T21:04:00Z",
          failures: {
            wled_seg_2: {
              ok: false,
              execution: "approximate_static",
              detail: "provider unavailable: device unreachable at wled.local",
            },
            custom_gradient: {
              ok: false,
              execution: "approximate_static",
              fidelity: "unsupported",
              detail: "no representable CLIP v2 scene action for this fixture state",
            },
          },
        },
      ];
      events = [
        {
          timestamp: "2026-09-10T21:04:00Z",
          level: "warning",
          category: "playback",
          summary: "Aurora Flow started with degraded execution",
          detail: "2 of 12 playback fixtures degraded (1 provider failure, 1 unsupported)",
          scene_id: "aurora_flow",
        },
        ...events.slice(1),
      ];
      break;
    }

    case "playback-paused-tropical": {
      // Stand-in for the live Tropical Smoothie scene (borrows Aurora Flow's
      // golden render plan through api.js's tropical_smoothie alias).
      normalizeHealthy();
      const aurora = scenes.scenes.find((s) => s.id === "aurora_flow");
      scenes.scenes.push({
        ...clone(aurora),
        id: "tropical_smoothie",
        name: "Tropical Smoothie",
        target_ids: ["office", "office_strip"],
        palette: ["#ff7a9c", "#ffd23f", "#ff8c42", "#7b2ff7"],
      });
      currentScene = { scene_id: "tropical_smoothie", name: "Tropical Smoothie", target_ids: ["office", "office_strip"] };
      playbackSpecs = [
        {
          session_id: "sess-mock-seed-tropical",
          scene_id: "tropical_smoothie",
          state: "paused",
          started_at: "2026-09-10T21:04:00Z",
          paused_at: "2026-09-10T21:20:00Z",
          failures: {
            custom_gradient: {
              ok: false,
              execution: "approximate_static",
              fidelity: "unsupported",
              detail: "no representable CLIP v2 scene action for this fixture state",
            },
          },
        },
      ];
      break;
    }

    case "playback-orphaned": {
      // Engine restart story: the session survives as orphaned (provider
      // may still be animating) + a retained stopped session for history.
      normalizeHealthy();
      currentScene = { scene_id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"] };
      playbackSpecs = [
        {
          session_id: "sess-mock-seed-aurora-orphan",
          scene_id: "aurora_flow",
          state: "orphaned",
          started_at: "2026-09-10T21:04:00Z",
        },
        {
          session_id: "sess-mock-seed-aurora-past",
          scene_id: "aurora_flow",
          state: "stopped",
          started_at: "2026-09-10T20:44:00Z",
          stopped_at: "2026-09-10T20:58:00Z",
          stop_reason: "stopped by user",
        },
      ];
      events = [
        {
          timestamp: "2026-09-10T21:10:00Z",
          level: "warning",
          category: "playback",
          summary: "Aurora Flow orphaned after engine restart",
          detail: "The provider may still be animating; use playback.stop to reclaim",
          scene_id: "aurora_flow",
        },
        ...events.slice(1),
      ];
      break;
    }

    case "migration-warning": {
      normalizeHealthy();
      warnings = [
        {
          code: "v1_scene_format",
          message: "2 legacy v1 scene files detected",
          details: { files: ["twilight.json", "meeting_blue.json"] },
        },
      ];
      events = [
        {
          timestamp: "2026-09-10T21:00:00Z",
          level: "warning",
          category: "system",
          summary: "Legacy v1 scene format detected",
          detail: "2 files pending migration: twilight.json, meeting_blue.json",
        },
        ...events,
      ];
      break;
    }

    case "empty-registry": {
      // First-run bootstrap (portable install): an EMPTY registry with a
      // fresh discovery report of unbound observations. The scenario
      // simulates a deployment whose hue_bridge_id is configured — first-run
      // adoption derives the HueBinding from that bridge-level hint.
      registry.fixtures = [];
      registry.targets = [];
      scenes.scenes = [];
      candidates = [];
      currentScene = null;
      playbackSpecs = [];
      observations = observations.map((o) =>
        o.provider === "hue_v2"
          ? { ...o, metadata: { ...(o.metadata || {}), bridge_id: "001788demo000001" } }
          : o
      );
      events = [
        {
          timestamp: "2026-09-10T19:00:00Z",
          level: "info",
          category: "system",
          summary: "Scene Studio engine started (first run)",
          detail: "registry_admin: provider writes blocked until the registry is deliberately built",
        },
      ];
      runtimeMode = "registry_admin";
      break;
    }

    default:
      throw new Error(`unknown scenario id: ${scenarioId}`);
  }

  return {
    registry,
    scenes,
    observations,
    candidates,
    discoveryRunMeta: runMeta,
    providers,
    warnings,
    playbackSpecs,
    currentScene,
    events,
    runtimeMode,
  };
}

/**
 * Convenience: full discovery report for a scenario.
 * @param {object} base base sample data {registry, scenes, discovery}
 * @param {string} scenarioId
 */
export function buildScenarioDiscovery(base, scenarioId) {
  const data = buildScenarioData(base, scenarioId);
  return buildDiscoveryReport(
    data.discoveryRunMeta,
    data.registry,
    data.observations,
    data.candidates
  );
}
