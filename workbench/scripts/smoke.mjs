/**
 * Smoke test for the Scene Studio Workbench (pure Node, no DOM).
 *
 * Parts:
 *   A. Mock layer — imports only pure modules:
 *        - src/mocks/scenarios.js  (deterministic scenario transforms)
 *        - src/api.js              (MockSceneStudioClient factory; DOM-free)
 *      The sample JSON files are read with fs (verbatim copies of
 *      backend/fixtures/*.sample.json) — NOT via Vite JSON
 *      imports, so this script runs under plain `node scripts/smoke.mjs`.
 *
 *      The mock's dry-run plans are the Python-generated goldens in
 *      src/mocks/render_plans/ (regenerate via `npm run goldens`; Python is
 *      the canonical renderer). Per scenario it asserts:
 *        1. valid fixture statuses + consistent readiness/discovery counts;
 *        2. a working fixture.disable -> fixture.enable round trip (request_id echo);
 *        3. a dry-run render plan served verbatim from the golden
 *           (scenario-independent demo data).
 *      Plus scenario-specific and contract-specific checks (unknown_command,
 *      validation errors, playback semantics).
 *
 *   B. Revision-cache decision (src/state.js shouldRefetchCatalogs) — pure.
 *
 *   C. Live HTTP client — spawns the Python dev server
 *      (`python devserver.py --seed-demo ...`, cwd=backend)
 *      and runs the same client contract checks against the REAL engine over
 *      HTTP (status shape, rename round trip, real-renderer dry run, not_found
 *      failures). If python cannot be spawned or the server never becomes
 *      ready, falls back to a tiny in-node HTTP stub serving canned
 *      engine-shaped responses.
 *
 *   D. AppDaemon transport — the same client with `transport: "appdaemon"`
 *      against a tiny in-node mimic of the AppDaemon 4.5 named endpoint
 *      (envelope POST -> always HTTP 200 `{status, body}`): status shape,
 *      one command round trip, 404 route envelope, malformed-envelope 400,
 *      and unknown-endpoint-name transport failure.
 */

import { readFileSync, readdirSync, mkdtempSync, rmSync } from "node:fs";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { createServer as httpCreateServer } from "node:http";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { SCENARIOS, HEALTH_STATUSES, buildScenarioData, buildScenarioDiscovery, observationMatchesBinding, deriveHealth } from "../src/mocks/scenarios.js";
import {
  createMockSceneStudioClient,
  createHttpSceneStudioClient,
  detectAppDaemonHosting,
} from "../src/api.js";
import {
  createStore,
  playbackActionEnvelope,
  liveStateFreshness,
  LIVE_STATE_FRESH_MS,
  LIVE_STATE_AGING_MS,
  shouldRefetchCatalogs,
  defaultBuilderDraft,
  describeAdvancedFields,
  describeFixtureState,
  duplicateBuilderDraft,
  resolveTargetFixtures,
  summarizePreviewQuality,
  targetReadiness,
} from "../src/state.js";
import {
  clusterLabel,
  controllerDisplayName,
  ecosystemGroupsFromDiscovery,
  groupByRoom,
  selectionOf,
  toggleIdSet,
  withControllerClusters,
} from "../src/grouping.js";
import { canonicalizeStaticPalette, resolveClusterPalette, resolveFixturePalette } from "../src/palette_assign.js";
import { sceneLookSwatches } from "../src/scene_look.js";
import { buildInspectorDescriptor, buildOverviewExceptions } from "../src/inspector.js";
import { auraBackground } from "../src/components/aura.js";
import {
  normalizePlayback,
  liveSessions,
  activeSessions,
  pausedSessions,
  orphanedSessions,
  stoppedSessions,
  sessionsForScene,
  sessionById,
  hasSendableSessionId,
  summarizeSession,
  summarizePlayback,
  sessionTone,
  controlsForSession,
} from "../src/playback.js";

const here = dirname(fileURLToPath(import.meta.url));
const readSample = (name) =>
  JSON.parse(readFileSync(join(here, "..", "src", "mocks", name), "utf8"));

const baseData = {
  registry: readSample("registry.sample.json"),
  scenes: readSample("scenes.sample.json"),
  discovery: readSample("discovery.sample.json"),
};

// --- Python-generated golden render plans (npm run goldens) --------------
const GOLDENS_DIR = join(here, "..", "src", "mocks", "render_plans");
const goldenNames = readdirSync(GOLDENS_DIR).filter((name) => name.endsWith(".json"));
/** scene_id -> golden document ({scene_id, target_ids, registry_updated, render_plan}). */
const goldens = {};
for (const name of goldenNames) {
  const golden = JSON.parse(readFileSync(join(GOLDENS_DIR, name), "utf8"));
  goldens[golden.scene_id] = golden;
}

/** Order-insensitive structural equality (render-plan key order differs
 *  between sorted-keys golden files and live-engine serialization). */
function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .map(([key, item]) => [key, sortDeep(item)])
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    );
  }
  return value;
}

function deepEqual(a, b) {
  return JSON.stringify(sortDeep(a)) === JSON.stringify(sortDeep(b));
}

// The live devserver seeds from the PYTHON sample fixtures (source of truth
// for the engine); the workbench copies may lag behind a resync, so derive
// live expectations from that document instead of hardcoding counts.
const BACKEND_FIXTURES = join(here, "..", "..", "backend", "fixtures", "registry.sample.json");
const liveExpectedFixtureCount = (() => {
  try {
    return JSON.parse(readFileSync(BACKEND_FIXTURES, "utf8")).fixtures.length;
  } catch {
    return baseData.registry.fixtures.length;
  }
})();

let passed = 0;
let failed = 0;

function check(label, condition, detail = "") {
  if (condition) {
    passed += 1;
    console.log(`  ok  ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

/** Default dry-run scene per scenario. */
const SCENE_FOR = {
  "dynamic-active": "aurora_flow",
};

/** Fidelity levels (contracts §6.5 — mirrors FidelityLevel in the domain). */
const FIDELITY_LEVELS = ["native", "equivalent", "approximate", "unsupported"];

/** Python round() (half-to-even) for sample-derived payload expectations. */
function pyRound(value, digits = 0) {
  const factor = Math.pow(10, digits);
  const scaled = value * factor;
  const floor = Math.floor(scaled);
  const diff = scaled - floor;
  let rounded;
  if (diff > 0.5) rounded = floor + 1;
  else if (diff < 0.5) rounded = floor;
  else rounded = floor % 2 === 0 ? floor : floor + 1;
  return digits === 0 ? rounded : rounded / factor;
}

function findPlan(plan, fixtureId) {
  return (plan ? plan.fixture_plans : []).find((p) => p.fixture_id === fixtureId) || null;
}

/**
 * First-run bootstrap scenario (portable pass): the generic per-scenario
 * assertions assume a mature install (24 fixtures, sample scenes), so the
 * empty-install scenario runs its OWN bounded contract section instead —
 * runtime registry_admin, adoption derivation/rejections, narrow target
 * administration, and the blocked provider-write surface.
 */
async function runEmptyRegistryScenario(client) {
  console.log("\n== scenario: empty-registry (first-run bootstrap) ==");
  const status = await client.getStatus();
  check("empty install: registry is empty", status.fixtures.total === 0, `got ${status.fixtures.total}`);
  check("empty install: runtime mode is registry_admin", status.runtime.mode === "registry_admin");
  check("empty install: provider writes blocked", status.runtime.provider_writes_blocked === true);
  check(
    "empty install: bootstrap commands are allowed",
    ["fixture.adopt", "target.create", "target.update"].every((c) => status.runtime.allowed_commands.includes(c))
  );
  check("empty install: scene.apply is NOT allowed", !status.runtime.allowed_commands.includes("scene.apply"));
  check("empty install: playback.start is NOT allowed", !status.runtime.allowed_commands.includes("playback.start"));

  const discovery = await client.getDiscovery();
  const entries = (discovery && discovery.entries) || [];
  const observations = (discovery && discovery.observations) || [];
  const obsOf = (id) => observations.find((o) => `${o.provider}:${o.provider_resource_id}` === id);
  const unbound = entries.filter((e) => e.status === "available_unbound" && obsOf(e.observation_id));
  check("empty install: discovery has unbound observations to adopt", unbound.length > 0);

  const aggregate = observations.find((o) => o.metadata && o.metadata.ha_aggregate);
  check("empty install: an HA aggregate observation is present for the rejection case", !!aggregate);
  const simple = unbound.find((e) => obsOf(e.observation_id).provider !== "hue_v2") || unbound[0];
  const simpleObs = obsOf(simple.observation_id);

  const adopted = await client.sendCommand({
    command: "fixture.adopt",
    observation_id: simple.observation_id,
    fixture_id: "desk_lamp",
    name: "Desk Lamp",
    request_id: "req-bootstrap-adopt",
  });
  check("bootstrap: fixture.adopt ok", adopted.ok === true, JSON.stringify(adopted).slice(0, 200));
  check(
    "bootstrap: binding derived from the observation (not client-supplied)",
    adopted.data && adopted.data.fixture && adopted.data.fixture.binding
      ? adopted.data.fixture.binding.provider === simpleObs.provider
      : false,
    JSON.stringify(adopted.data || {})
  );
  check(
    "bootstrap: adoption provenance recorded",
    adopted.data && adopted.data.fixture && adopted.data.fixture.metadata
      ? adopted.data.fixture.metadata.adopted_from_observation === simple.observation_id
      : false
  );
  const after = await client.getFixtures();
  check(
    "bootstrap: adopted fixture listed and derived ready",
    after.fixtures.some((f) => f.id === "desk_lamp" && f.health === "ready")
  );

  const dup = await client.sendCommand({
    command: "fixture.adopt", observation_id: unbound[0].observation_id, fixture_id: "desk_lamp", name: "Dup",
  });
  check("bootstrap: duplicate stable id conflicts", dup.ok === false && dup.error && dup.error.code === "conflict");
  const stale = await client.sendCommand({
    command: "fixture.adopt", observation_id: "hue_v2:ffffffff-ffff-4000-8000-0000000000ff", fixture_id: "ghost", name: "Ghost",
  });
  check("bootstrap: observation missing from the report is not_found", stale.ok === false && stale.error && stale.error.code === "not_found");
  const badId = await client.sendCommand({
    command: "fixture.adopt", observation_id: unbound[0].observation_id, fixture_id: "Desk-Light", name: "Bad",
  });
  check("bootstrap: invalid identity charset rejected", badId.ok === false && badId.error && badId.error.code === "validation_error");
  if (aggregate) {
    const agg = await client.sendCommand({
      command: "fixture.adopt", observation_id: `ha_light:${aggregate.provider_resource_id}`, fixture_id: "agg", name: "Agg",
    });
    check("bootstrap: HA aggregate observation rejected", agg.ok === false && agg.error && agg.error.code === "conflict");
  }
  check(
    "bootstrap: runtime still blocks provider writes after adoption",
    (await client.getStatus()).runtime.provider_writes_blocked === true
  );

  const target = await client.sendCommand({
    command: "target.create", name: "Office", fixture_ids: ["desk_lamp"], request_id: "req-bootstrap-target",
  });
  check(
    "bootstrap: target.create derives the id and assigns membership",
    target.ok === true && target.data && target.data.target && target.data.target.id === "office" &&
      Array.isArray(target.data.assigned_fixture_ids) && target.data.assigned_fixture_ids.includes("desk_lamp"),
    JSON.stringify(target.data || target.error || {})
  );
  const dupTarget = await client.sendCommand({ command: "target.create", name: "Office" });
  check("bootstrap: duplicate target id conflicts", dupTarget.ok === false && dupTarget.error && dupTarget.error.code === "conflict");
  const renamed = await client.sendCommand({ command: "target.update", target_id: "office", name: "Workspace" });
  check("bootstrap: target.update renames", renamed.ok === true && renamed.data && renamed.data.target && renamed.data.target.name === "Workspace");
  const removed = await client.sendCommand({ command: "target.update", target_id: "office", remove_fixture_ids: ["desk_lamp"] });
  check(
    "bootstrap: target.update removes membership",
    removed.ok === true && removed.data && Array.isArray(removed.data.removed_fixture_ids) && removed.data.removed_fixture_ids.includes("desk_lamp")
  );
  const ghostMember = await client.sendCommand({ command: "target.update", target_id: "office", add_fixture_ids: ["ghost"] });
  check("bootstrap: unknown member fixture is not_found", ghostMember.ok === false && ghostMember.error && ghostMember.error.code === "not_found");
}


async function runScenario(scenarioId) {
  const client = createMockSceneStudioClient(baseData, { scenarioId });
  if (scenarioId === "empty-registry") {
    // Dedicated first-run bootstrap contract section (mature-install
    // assertions below assume the 24-fixture sample registry).
    return runEmptyRegistryScenario(client);
  }
  console.log(`\n== scenario: ${scenarioId} ==`);

  // --- 1. valid statuses + counts -------------------------------------
  const fixturesDoc = await client.getFixtures();
  const fixtures = fixturesDoc.fixtures;
  check("fixtures loaded from sample", fixtures.length === 24, `got ${fixtures.length}`);

  const badHealth = fixtures.filter((f) => !HEALTH_STATUSES.includes(f.health));
  check("every fixture health is valid", badHealth.length === 0, badHealth.map((f) => f.id).join(","));

  // --- live fixture color-state (plan: live fixture color-state pass) --
  const liveState = await client.getFixtureState();
  check("live state observed_at is a real ISO timestamp", !Number.isNaN(Date.parse(liveState.observed_at)));
  const boundFixtures = fixtures.filter((f) => f.binding);
  check(
    "live state has an entry for every bound fixture",
    boundFixtures.every((f) => liveState.fixtures[f.id]),
    boundFixtures.filter((f) => !liveState.fixtures[f.id]).map((f) => f.id).join(",")
  );
  const disabledFixtures = fixtures.filter((f) => f.enabled === false && f.binding);
  check(
    "disabled fixtures never carry a live aura regardless of provider payload",
    disabledFixtures.every((f) => liveState.fixtures[f.id] && liveState.fixtures[f.id].available === false),
    disabledFixtures.map((f) => f.id).join(",")
  );
  const colorModes = new Set(Object.values(liveState.fixtures).map((s) => s.color_mode));
  const stateKinds = new Set(Object.values(liveState.fixtures).map((s) => s.state_kind));
  if (scenarioId === "all-healthy") {
    // the hand-assigned mock ids only exist while nothing has stripped/
    // overridden the sample registry — a representative sample of every
    // shape the real normalizers produce (plan §11's required coverage).
    check("live state exercises gradient/rgb/cct/dynamic color modes", ["gradient", "rgb", "cct", "dynamic"].every((m) => colorModes.has(m)), [...colorModes].join(","));
    check("live state exercises live + configured_dynamic state kinds", ["live", "configured_dynamic"].every((k) => stateKinds.has(k)), [...stateKinds].join(","));
  }

  const status = client.getStatus();
  const counts = status.fixtures;
  check(
    "status readiness counts sum to total",
    counts.ready + counts.missing + counts.disabled + counts.degraded + counts.unbound + (counts.conflicting || 0) === counts.total,
    JSON.stringify(counts)
  );
  const derived = {};
  for (const f of fixtures) derived[f.health] = (derived[f.health] || 0) + 1;
  check(
    "status counts match derived fixture health",
    ["ready", "missing", "disabled", "degraded"].every((k) => (counts[k] || 0) === (derived[k] || 0)),
    JSON.stringify({ counts, derived })
  );

  const discovery = await client.getDiscovery();
  const sum = discovery.summary;
  check(
    "discovery summary fixture counts sum to total",
    sum.fixtures_bound_ready + sum.fixtures_missing + sum.fixtures_disabled + sum.fixtures_degraded + (sum.fixtures_reconcile_available || 0) === fixtures.length,
    JSON.stringify(sum)
  );
  check(
    "discovery summary observations_total matches observations",
    sum.observations_total === discovery.observations.length
  );
  {
    // unbound = observations not matched by any non-disabled fixture binding
    const matched = new Set();
    for (const f of fixtures) {
      if (f.health === "disabled" || !f.binding) continue;
      const obs = discovery.observations.find((o) => observationMatchesBinding(o, f.binding));
      if (obs) matched.add(`${obs.provider}:${obs.provider_resource_id}`);
    }
    const expectedUnbound = discovery.observations.filter(
      (o) =>
        !matched.has(`${o.provider}:${o.provider_resource_id}`) &&
        !(o.metadata && o.metadata.ha_aggregate)
    ).length;
    check(
      "unbound_observations consistent with matched bindings",
      sum.unbound_observations === expectedUnbound,
      `summary=${sum.unbound_observations} expected=${expectedUnbound}`
    );
  }
  check(
    "status is engine-shaped (stable keys)",
    ["engine", "fixtures", "providers", "provider_links", "current", "playback", "last_discovery", "warnings"]
      .every((k) => k in status) &&
      typeof status.engine.revision === "number" &&
      status.engine.ok === true &&
      typeof status.fixtures.conflicting === "number",
    JSON.stringify(Object.keys(status))
  );
  check(
    "providers dict carries per-provider readiness buckets",
    typeof status.providers === "object" &&
      !Array.isArray(status.providers) &&
      Object.values(status.providers).every(
        (c) => ["total", "ready", "missing", "degraded", "other"].every((k) => typeof c[k] === "number")
      )
  );
  check(
    "provider_links list connected booleans",
    Array.isArray(status.provider_links) &&
      status.provider_links.length === 3 &&
      status.provider_links.every((p) => typeof p.connected === "boolean" && typeof p.label === "string")
  );

  // --- 2. disable -> enable round trip with request_id echo ------------
  const readyFixture = fixtures.find((f) => f.health === "ready");
  check("scenario has a ready fixture to round-trip", !!readyFixture);
  if (readyFixture) {
    const reqId = `req-${scenarioId}-disable`;
    const off = await client.sendCommand({
      command: "fixture.disable",
      fixture_id: readyFixture.id,
      request_id: reqId,
    });
    check("fixture.disable ok", off.ok === true, JSON.stringify(off));
    check("fixture.disable echoes request_id", off.request_id === reqId);
    check("fixture.disable data.health disabled", off.data && off.data.health === "disabled");
    const afterOff = await client.getFixtures();
    check(
      "getFixtures shows disabled after disable",
      afterOff.fixtures.find((f) => f.id === readyFixture.id).health === "disabled"
    );
    const on = await client.sendCommand({
      command: "fixture.enable",
      fixture_id: readyFixture.id,
      request_id: `req-${scenarioId}-enable`,
    });
    check("fixture.enable ok", on.ok === true);
    check("fixture.enable data.health ready again", on.data && on.data.health === "ready");
    const afterOn = await client.getFixtures();
    check(
      "getFixtures shows ready after enable",
      afterOn.fixtures.find((f) => f.id === readyFixture.id).health === "ready"
    );
  }

  // --- 3. dry-run render plan served from the Python golden -------------
  const sceneId = SCENE_FOR[scenarioId] || "twilight";
  const reqId = `req-${scenarioId}-dry`;
  const dry = await client.sendCommand({
    command: "scene.apply",
    scene_id: sceneId,
    dry_run: true,
    request_id: reqId,
  });
  check("dry-run scene.apply ok", dry.ok === true, JSON.stringify(dry).slice(0, 300));
  check("dry-run echoes request_id", dry.request_id === reqId);
  const plan = dry.data && dry.data.render_plan;
  check("render_plan present", !!plan);
  const golden = goldens[sceneId];
  check("golden exists for the dry-run scene", !!golden);
  if (plan) {
    check("render_plan scene_id matches", plan.scene_id === sceneId);
    check(
      "render_plan has fixture plans with fidelity fields",
      plan.fixture_plans.length >= 1 &&
        plan.fixture_plans.every(
          (p) =>
            typeof p.fixture_id === "string" &&
            typeof p.provider === "string" &&
            FIDELITY_LEVELS.includes(p.fidelity) &&
            (p.reason === undefined || typeof p.reason === "string") &&
            (p.operations === undefined || Array.isArray(p.operations))
        ),
      JSON.stringify(plan.fixture_plans.map((p) => p.fidelity))
    );
    check(
      "render_plan fixture_plans sorted by fixture id (plan.py order)",
      plan.fixture_plans.every((p, i, a) => i === 0 || a[i - 1].fixture_id < p.fixture_id),
      JSON.stringify(plan.fixture_plans.map((p) => p.fixture_id))
    );
    check(
      "every fixture plan operation carries provider/op/resource_ref",
      plan.fixture_plans.every((p) =>
        (p.operations || []).every((op) => typeof op.provider === "string" && typeof op.op === "string" && typeof op.resource_ref === "string")
      )
    );
    check(
      "skipped_fixture_ids match the golden (scenario-independent demo data)",
      deepEqual(plan.skipped_fixture_ids || [], (golden && golden.render_plan.skipped_fixture_ids) || []),
      JSON.stringify(plan.skipped_fixture_ids)
    );
    check("render_plan notes array-or-omitted (to_dict shape)", plan.notes === undefined || Array.isArray(plan.notes));
    if (golden) {
      check(
        "dry-run plan equals the Python golden verbatim (no JS recomputation)",
        deepEqual(plan, golden.render_plan)
      );
    }
  }

  return { client, status, discovery, plan };
}

async function main() {
  console.log("Scene Studio Workbench mock smoke test");

  // determinism of scenario transforms
  const a = JSON.stringify(buildScenarioData(baseData, "provider-offline"));
  const b = JSON.stringify(buildScenarioData(baseData, "provider-offline"));
  check("scenario transforms are deterministic", a === b);

  // contract parity: disabled-fixture scenario keeps the sample's
  // administrative-disabled phrasing
  const disc = buildScenarioDiscovery(baseData, "disabled-fixture");
  check(
    "disabled-fixture discovery has administratively-disabled entry",
    disc.entries.some((e) => e.status === "disabled" && /administratively disabled/.test(e.detail))
  );

  // --- goldens: Python-generated render-plan fixtures (npm run goldens) --
  console.log("\n== goldens (Python-rendered mock fixtures) ==");
  {
    const sampleSceneIds = baseData.scenes.scenes.map((s) => s.id).sort();
    const goldenSceneIds = Object.keys(goldens).sort();
    check(
      "goldens load and match the loaded sample scenes (one per scene id)",
      JSON.stringify(goldenSceneIds) === JSON.stringify(sampleSceneIds),
      `goldens=${goldenSceneIds.join(",")} samples=${sampleSceneIds.join(",")}`
    );
    check(
      "golden file names match their scene ids",
      goldenNames.every((name) => name.replace(/\.json$/, "") === goldens[name.replace(/\.json$/, "")].scene_id)
    );
    check(
      "goldens carry the sample registry timestamp",
      Object.values(goldens).every((g) => g.registry_updated === baseData.registry.updated),
      JSON.stringify(Object.values(goldens).map((g) => g.registry_updated))
    );
    check(
      "golden target_ids match the sample scene targets",
      Object.values(goldens).every(
        (g) =>
          JSON.stringify(g.target_ids) ===
          JSON.stringify(baseData.scenes.scenes.find((s) => s.id === g.scene_id).target_ids)
      )
    );
    check(
      "golden render_plan scene_id matches the envelope",
      Object.values(goldens).every((g) => g.render_plan.scene_id === g.scene_id)
    );
    // Mock dry-run must serve the golden verbatim: twilight's g_strip op is
    // the Python-rendered hue.put_light carrying CLIP v2 dynamics — asserted
    // against the golden file content, never recomputed in JS.
    const client = createMockSceneStudioClient(baseData);
    const dry = await client.sendCommand({
      command: "scene.apply",
      scene_id: "twilight",
      dry_run: true,
      request_id: "req-golden-dry",
    });
    check("golden dry-run: mock scene.apply ok", dry.ok === true, JSON.stringify(dry).slice(0, 200));
    const plan = dry.data && dry.data.render_plan;
    const goldenGStrip = goldens.twilight.render_plan.fixture_plans.find((p) => p.fixture_id === "g_strip");
    const servedGStrip = plan ? plan.fixture_plans.find((p) => p.fixture_id === "g_strip") : null;
    check(
      "golden dry-run: twilight g_strip is hue.put_light carrying dynamics (golden content)",
      !!goldenGStrip &&
        goldenGStrip.operations &&
        goldenGStrip.operations[0].op === "hue.put_light" &&
        !!goldenGStrip.operations[0].payload.dynamics,
      JSON.stringify(goldenGStrip)
    );
    check(
      "golden dry-run: served g_strip op equals the golden op (no JS recomputation)",
      !!servedGStrip && !!goldenGStrip && deepEqual(servedGStrip.operations, goldenGStrip.operations)
    );
  }

  const results = {};
  for (const scenario of SCENARIOS) {
    results[scenario.id] = await runScenario(scenario.id);
  }

  // --- AppDaemon-hosted production connection ------------------------------
{
  const hosted = detectAppDaemonHosting("/scene_studio/");
  check(
    "detectAppDaemonHosting: /scene_studio path -> live appdaemon same-origin",
    !!hosted && hosted.mode === "live" && hosted.url === "" && hosted.transport === "appdaemon"
        && hosted.endpointName === "scene_studio_api"
  );
  check("detectAppDaemonHosting: dev path -> null", detectAppDaemonHosting("/") === null);

  // same-origin appdaemon client: empty base is valid, adUrl is root-relative
  let client = null;
  try {
    client = createHttpSceneStudioClient("", { transport: "appdaemon" });
  } catch {
    client = null;
  }
  check("same-origin appdaemon client constructs with empty base", !!client);
  let directRejected = false;
  try {
    createHttpSceneStudioClient("", { transport: "direct" });
  } catch {
    directRejected = true;
  }
  check("direct transport still requires a base URL", directRejected);
}

// --- AppDaemon-hosted production connection ------------------------------
{
  const hosted = detectAppDaemonHosting("/local/scene_studio/index.html");
  check(
    "detectAppDaemonHosting: /local/scene_studio -> live appdaemon same-origin",
    !!hosted && hosted.mode === "live" && hosted.url === "" && hosted.transport === "appdaemon"
        && hosted.endpointName === "scene_studio_api"
  );
  check(
    "detectAppDaemonHosting: /scene_studio static_dirs variant also detected",
    (() => { const h = detectAppDaemonHosting("/scene_studio/"); return !!h && h.transport === "appdaemon"; })()
  );
  check("detectAppDaemonHosting: dev path -> null", detectAppDaemonHosting("/") === null);

  // same-origin appdaemon client: empty base is valid, adUrl is root-relative
  let client = null;
  try {
    client = createHttpSceneStudioClient("", { transport: "appdaemon" });
  } catch {
    client = null;
  }
  check("same-origin appdaemon client constructs with empty base", !!client);
  let directRejected = false;
  try {
    createHttpSceneStudioClient("", { transport: "direct" });
  } catch {
    directRejected = true;
  }
  check("direct transport still requires a base URL", directRejected);
}

// --- scenario-specific expectations -----------------------------------
  // --- external light-sync contention (hyperHDR pass) --------------------
  console.log("\n== contention (hyperHDR ownership pass) ==");
  {
    const client = createMockSceneStudioClient(baseData);
    const st = await client.getStatus();
    check(
      "mock status carries the contention block (configured:false, nothing held)",
      st.contention && st.contention.configured === false
        && Array.isArray(st.contention.held_fixture_ids) && st.contention.held_fixture_ids.length === 0
        && st.contention.default_policy === "yield",
      JSON.stringify(st.contention || null).slice(0, 160)
    );
    const fx = await client.getFixtures();
    const withContention = fx.fixtures.filter((f) => f.contention && f.contention.held === false);
    check(
      "mock fixtures carry an idle contention view",
      fx.fixtures.length > 0 && withContention.length === fx.fixtures.length
    );
    const override = await client.sendCommand({
      command: "scene.apply",
      scene_id: "twilight",
      contention_override: "takeover",
      request_id: "req-contention-override",
    });
    check("scene.apply accepts contention_override=takeover", override.ok === true, JSON.stringify(override).slice(0, 160));
    const badOverride = await client.sendCommand({
      command: "scene.apply",
      scene_id: "twilight",
      contention_override: "nonsense",
      request_id: "req-contention-bad",
    });
    check(
      "scene.apply passes contention_override through (value semantics are backend-owned)",
      badOverride.ok === true,
      JSON.stringify(badOverride).slice(0, 140)
    );
    const policy = await client.sendCommand({
      command: "fixture.set_contention_policy",
      fixture_id: fx.fixtures[0].id,
      policy: "takeover",
      request_id: "req-policy",
    });
    check(
      "fixture.set_contention_policy validates and resolves",
      policy.ok === true && policy.data && policy.data.fixture && policy.data.fixture.contention_policy === "takeover"
    );
    const resumeNone = await client.sendCommand({ command: "sync.resume", request_id: "req-resume-none" });
    check(
      "sync.resume without a suspend record conflicts",
      resumeNone.ok === false && resumeNone.error && resumeNone.error.code === "conflict"
    );
    const suspend = await client.sendCommand({ command: "sync.suspend", request_id: "req-suspend" });
    check("sync.suspend resolves (mock no-op, no instances)", suspend.ok === true
      && Array.isArray(suspend.data.instances) && suspend.data.instances.length === 0);
  }

  console.log("\n== scenario-specific expectations ==");

  {
    const { status } = results["all-healthy"];
    check("all-healthy: every fixture ready", status.fixtures.ready === status.fixtures.total);
  }
  {
    const { status } = results["missing-fixture"];
    check("missing-fixture: exactly 1 missing", status.fixtures.missing === 1);
  }
  {
    const { status, discovery } = results["disabled-fixture"];
    check("disabled-fixture: exactly 3 disabled (double_strip + water + food)", status.fixtures.disabled === 3);
    check(
      "disabled-fixture: discovery lists candidate (base sample behavior)",
      discovery.summary.candidate_replacements === 1
    );
  }
  {
    const { client, status, discovery } = results["registry-stale"];
    check(
      "registry-stale: custom_gradient still degraded until reconcile",
      status.fixtures.degraded >= 1
    );
    check(
      "registry-stale: discovery reports reconcile available, not degradation",
      discovery.entries.some((e) => e.fixture_id === "custom_gradient" && e.status === "bound_reconcile_available") &&
        !discovery.entries.some((e) => e.fixture_id === "custom_gradient" && e.status === "bound_degraded")
    );
    check(
      "registry-stale: double_strip stays intentionally disabled",
      discovery.entries.some((e) => e.fixture_id === "double_strip" && e.status === "disabled")
    );
    const store = createStore(client);
    await store.init();
    const exceptions = buildOverviewExceptions(store.state);
    const issues = exceptions.filter((e) => e.tone === "err" || e.tone === "warn");
    check(
      "registry-stale: Custom Gradient is not a Needs Attention issue",
      !issues.some((e) => /Custom Gradient/.test(e.text)),
      JSON.stringify(issues)
    );
    check(
      "registry-stale: Double Strip is not a Needs Attention issue",
      !issues.some((e) => /Double Strip/.test(e.text)),
      JSON.stringify(issues)
    );
    check(
      "registry-stale: reconcile available is an idle configuration item",
      exceptions.some((e) => e.tone === "idle" && /registry update available/.test(e.text))
    );
    store.select({ type: "fixture", id: "custom_gradient" });
    const desc = buildInspectorDescriptor(store.state);
    check(
      "registry-stale inspector: registry update copy and Review update",
      !!desc &&
        desc.explanation.some((t) => /REGISTRY UPDATE AVAILABLE/.test(t)) &&
        desc.actions.some((a) => a.id === "fixture.reconcile_preview" && a.label === "Review update")
    );
    const obsId = discovery.entries.find((e) => e.fixture_id === "custom_gradient" && e.status === "bound_reconcile_available").observation_id;
    const preview = await store.sendCommand({
      command: "fixture.reconcile_preview",
      fixture_id: "custom_gradient",
      observation_id: obsId,
    });
    check("registry-stale: reconcile preview ok and binding unchanged", preview.ok === true && preview.data.binding_unchanged === true, JSON.stringify(preview));
    const applied = await store.sendCommand({
      command: "fixture.reconcile",
      fixture_id: "custom_gradient",
      observation_id: obsId,
    });
    check("registry-stale: reconcile apply ok", applied.ok === true, JSON.stringify(applied));
    const cg = store.state.fixtures.fixtures.find((f) => f.id === "custom_gradient");
    check(
      "registry-stale: custom_gradient ready after reconcile, binding unchanged",
      cg.health === "ready" && cg.binding.resource_id === "00000000-0000-4000-8000-000000000017"
    );
    const afterIssues = buildOverviewExceptions(store.state).filter((e) => e.tone === "err" || e.tone === "warn");
    check(
      "registry-stale: Custom Gradient still not a Needs Attention issue after apply",
      !afterIssues.some((e) => /Custom Gradient/.test(e.text))
    );
  }
  {
    const client = createMockSceneStudioClient(baseData, { scenarioId: "disabled-fixture" });
    const store = createStore(client);
    await store.init();
    store.select({ type: "fixture", id: "custom_gradient" });
    const limited = buildInspectorDescriptor(store.state);
    check(
      "capability-limited ready fixture stays healthy in inspector",
      limited && limited.tone === "ok" && limited.explanation.some((t) => /reachable and usable/.test(t)) &&
        !limited.explanation.some((t) => /check the fidelity/.test(t))
    );
    store.select({ type: "fixture", id: "double_strip" });
    const disabled = buildInspectorDescriptor(store.state);
    check(
      "disabled fixture inspector copy",
      disabled && disabled.explanation.includes("Intentionally disabled.") && disabled.explanation.includes("Excluded from scenes.")
    );
    const blocked = buildInspectorDescriptor({
      ...store.state,
      status: { ...store.state.status, runtime: { mode: "read_only", read_only: true, allowed_commands: ["fixture.reconcile_preview"] } },
    });
    check(
      "Enable is explained rather than mysteriously disabled in read_only",
      blocked && blocked.actions.some((a) => a.id === "__policy_note")
    );
    const productionCommands = [
      "scene.apply", "playback.start", "playback.pause", "playback.resume",
      "playback.stop", "scene.rename", "scene.archive", "scene.restore",
    ];
    check(
      "normal mock policy enables production scene/playback commands",
      productionCommands.every((command) => store.commandAllowed(command))
    );
    const originalRuntime = store.state.status.runtime;
    store.state.status.runtime = {
      mode: "read_only",
      read_only: true,
      provider_writes_blocked: true,
      allowed_commands: ["scene.preview", "scene.apply:dry_run"],
    };
    check(
      "read_only policy disables the same mutating scene/playback actions",
      productionCommands.every((command) => !store.commandAllowed(command)) &&
        store.commandAllowed("scene.preview")
    );
    store.state.status.runtime = originalRuntime;
  }
  {
    const { client, status } = results["provider-offline"];
    const wled = (status.provider_links || []).find((p) => p.provider === "wled");
    check("provider-offline: wled marked disconnected", wled && wled.connected === false);
    check("provider-offline: wled bucket all-missing in providers dict", status.providers.wled && status.providers.wled.missing === 6);
    check("provider-offline: 6 fixtures missing", status.fixtures.missing === 6);
    const run = await client.sendCommand({
      command: "discovery.run",
      providers: ["wled"],
      request_id: "req-disc-wled",
    });
    check("provider-offline: discovery.run ok", run.ok === true);
    check(
      "provider-offline: discovery.run reports skipped offline provider",
      Array.isArray(run.data.skipped_providers) && run.data.skipped_providers.includes("wled")
    );
  }
  {
    const { client, discovery } = results["replacement-candidate"];
    check(
      "replacement-candidate: candidate replacement entry present",
      discovery.summary.candidate_replacements >= 1
    );
    check(
      "replacement-candidate: unbound observation present",
      discovery.entries.some((e) => e.status === "available_unbound")
    );
    const preview = await client.sendCommand({
      command: "fixture.rebind_preview",
      fixture_id: "double_strip",
      observation_id: "hue_v2:00000000-0000-4000-8000-000000000016",
      request_id: "req-rebind-preview",
    });
    check("replacement-candidate: rebind preview ok", preview.ok === true, JSON.stringify(preview));
    const rebind = await client.sendCommand({
      command: "fixture.rebind",
      fixture_id: "double_strip",
      observation_id: "hue_v2:00000000-0000-4000-8000-000000000016",
      request_id: "req-rebind",
    });
    check("replacement-candidate: rebind ok", rebind.ok === true, JSON.stringify(rebind));
    check("replacement-candidate: rebind echoes request_id", rebind.request_id === "req-rebind");
    const after = await client.getFixtures();
    check(
      "replacement-candidate: double_strip ready after rebind",
      after.fixtures.find((f) => f.id === "double_strip").health === "ready"
    );
  }
  {
    const { plan } = results["mixed-fidelity"];
    const golden = goldens["twilight"];
    // Goldens are scenario-independent: the mixed-fidelity scenario changes
    // the health/discovery surfaces, but the mock dry-run still serves the
    // Python-rendered base-sample plan verbatim (JS renders nothing).
    check(
      "mixed-fidelity: dry-run serves the scenario-independent golden",
      !!plan && !!golden && deepEqual(plan, golden.render_plan)
    );
    // Fidelity mix asserted against the golden content itself.
    const levels = new Set(golden ? golden.render_plan.fixture_plans.map((p) => p.fidelity) : []);
    check(
      "mixed-fidelity: golden twilight fidelity mix is native+equivalent (Python-rendered)",
      levels.has("native") && levels.has("equivalent") && !levels.has("unsupported"),
      [...levels].join(",")
    );
  }
  {
    const { client, status } = results["dynamic-active"];
    const playbackSession = status.playback && status.playback.sessions[0];
    check(
      "dynamic-active: playback session active (engine shape: collection, one active session)",
      !!status.playback && status.playback.counts.active === 1
        && playbackSession && playbackSession.state === "active"
        && playbackSession.scene_id === "aurora_flow"
    );
    check(
      "dynamic-active: playback session carries engine keys",
      !!playbackSession && ["session_id", "scene_id", "state", "started_at", "fixture_ids", "fixture_executions"].every((k) => k in playbackSession)
    );
    const plan = results["dynamic-active"].plan;
    const levels = new Set(plan ? plan.fixture_plans.map((p) => p.fidelity) : []);
    check("dynamic-active: native + approximate, no unsupported", levels.has("native") && levels.has("approximate") && !levels.has("unsupported"), [...levels].join(","));
    check(
      "dynamic-active: no fallback provider operations",
      plan.fixture_plans.every((p) => p.provider !== "fallback")
    );
    // R5D corrected contract: ALL dynamic Hue state routes through the
    // managed scene mechanism — a static-action put_light (the fixture's
    // state gradient, never dynamics.status) PLUS the hue.put_scene_dynamic
    // marker carrying the canonical palette + scene-level speed.
    const scene = baseData.scenes.scenes.find((s) => s.id === "aurora_flow");
    const hueState = scene.fixture_states.g_strip;
    const gStrip = findPlan(plan, "g_strip");
    check(
      "dynamic-active: g_strip carries static-action put_light plus managed-scene marker",
      !!gStrip &&
        gStrip.operations &&
        gStrip.operations.length === 2 &&
        gStrip.operations[0].op === "hue.put_light" &&
        Array.isArray(gStrip.operations[0].payload.gradient.points) &&
        gStrip.operations[0].payload.gradient.points.length === hueState.gradient.length &&
        !(gStrip.operations[0].payload.dynamics || {}).status &&
        gStrip.operations[1].op === "hue.put_scene_dynamic",
      JSON.stringify(gStrip)
    );
    check(
      "dynamic-active: managed-scene marker carries canonical palette + numeric scene speed, no dynamics on the light",
      !!gStrip &&
        gStrip.operations[1].payload.palette &&
        gStrip.operations[1].payload.palette.length === scene.palette.length &&
        typeof gStrip.operations[1].payload.speed === "number" &&
        gStrip.operations[1].payload.speed === scene.motion.speed &&
        !(gStrip.operations[0].payload.dynamics || {}).status,
      JSON.stringify(gStrip && [gStrip.operations[0].payload.dynamics, gStrip.operations[1].payload])
    );
    check(
      "dynamic-active: gradient points are xy objects (CLIP v2 shape)",
      !!gStrip &&
        gStrip.operations[0].payload.gradient.points.every(
          (pt) =>
            typeof pt.color.xy.x === "number" &&
            typeof pt.color.xy.y === "number" &&
            !("color_hex" in pt)
        ) &&
        gStrip.operations[0].payload.gradient.mode === "interpolated_palette"
    );
    // Fidelity follows the loaded document: native, unless the fixture state
    // carries unknown provider_ext.hue_v2 keys (ignored with a note, which
    // downgrades to approximate) — mirrors the Python parity test.
    const KNOWN_HUE_EXT_KEYS = ["dynamics", "gradient", "raw"];
    const extKeys = Object.keys((hueState.provider_ext && hueState.provider_ext.hue_v2) || {});
    const unknownExt = extKeys.filter((k) => !KNOWN_HUE_EXT_KEYS.includes(k));
    check(
      "dynamic-active: g_strip fidelity matches provider_ext contract (native, or approximate when ext keys are ignored)",
      !!gStrip &&
        (unknownExt.length
          ? gStrip.fidelity === "approximate" && /provider_ext\.hue_v2 key\(s\) ignored/.test(gStrip.reason)
          : gStrip.fidelity === "native"),
      `fidelity=${gStrip && gStrip.fidelity} unknownExt=${unknownExt.join(",")}`
    );
    // WLED dynamic path: a resolvable fx (explicit numeric ext, or a state
    // effect name resolved through a capabilities.effects catalog) runs native
    // fx + sx = round(speed*255); without one the documented approximate
    // static fallback applies. No legacy placeholder-only dynamic ops.
    for (const fp of plan.fixture_plans.filter((p) => p.fixture_id.startsWith("wled_"))) {
      const fixture = baseData.registry.fixtures.find((f) => f.id === fp.fixture_id);
      const state = scene.fixture_states[fp.fixture_id] || scene.default_state;
      const ext = (state.provider_ext && state.provider_ext.wled) || {};
      const explicitFx = Number.isInteger(ext.fx) ? ext.fx : null;
      const catalog = (fixture.capabilities && fixture.capabilities.effects) || [];
      const namedFx =
        state.effect && catalog.some((e) => typeof e === "string" && e.toLowerCase() === state.effect.toLowerCase())
          ? catalog.findIndex((e) => e.toLowerCase() === state.effect.toLowerCase())
          : null;
      const fx = explicitFx !== null ? explicitFx : namedFx;
      const dynamicNative = !!(fixture.capabilities && fixture.capabilities.dynamic_native);
      const op = fp.operations && fp.operations[0];
      const segs = (op && op.payload && op.payload.seg) || [];
      if (fx !== null && dynamicNative && scene.motion.mode === "palette_cycle") {
        check(
          `dynamic-active: ${fp.fixture_id} native WLED fx+sx`,
          !!op &&
            op.op === "wled.post_state" &&
            fp.fidelity === "native" &&
            segs.length > 0 &&
            segs.every((s) => s.fx === fx && s.sx === pyRound(scene.motion.speed * 255)),
          JSON.stringify(fp)
        );
      } else {
        check(
          `dynamic-active: ${fp.fixture_id} documented approximate static fallback`,
          !!op &&
            op.op === "wled.post_state" &&
            fp.fidelity === "approximate" &&
            segs.length > 0 &&
            segs.every((s) => !("fx" in s) && Array.isArray(s.col)),
          JSON.stringify(fp)
        );
      }
      check(
        `dynamic-active: ${fp.fixture_id} carries no legacy placeholder payload`,
        !op || !op.payload || (!("palette_hex" in op.payload) && op.payload.dynamics !== "native_scene" && !(op.payload.seg && !Array.isArray(op.payload.seg)))
      );
    }
    // session-addressed controls: start our own session via the mock client
    const startResult = await client.sendCommand({
      command: "playback.start", scene_id: "aurora_flow", request_id: "req-start",
    });
    const playbackSessionId = startResult && startResult.data && startResult.data.session_id;
    check("dynamic-active: playback.start returned session_id", !!playbackSessionId);
    const pause = await client.sendCommand({ command: "playback.pause", session_id: playbackSessionId, request_id: "req-pause" });
    check("dynamic-active: playback.pause ok (session-addressed)", pause.ok === true);
    const resume = await client.sendCommand({ command: "playback.resume", session_id: playbackSessionId, request_id: "req-resume" });
    check("dynamic-active: playback.resume ok (session-addressed)", resume.ok === true);
    const stop = await client.sendCommand({ command: "playback.stop", session_id: playbackSessionId, request_id: "req-stop" });
    check("dynamic-active: playback.stop ok (session-addressed)", stop.ok === true);
    const stopAgain = await client.sendCommand({ command: "playback.stop", session_id: playbackSessionId, request_id: "req-stop2" });
    check(
      "dynamic-active: second playback.stop -> conflict",
      stopAgain.ok === false && stopAgain.error.code === "conflict"
    );
  }
  {
    const { status } = results["migration-warning"];
    check(
      "migration-warning: warning banner present",
      status.warnings.length >= 1 && status.warnings[0].code === "v1_scene_format"
    );
  }

  // --- contract-level checks --------------------------------------------
  console.log("\n== contract checks ==");
  const client = createMockSceneStudioClient(baseData);
  const unknown = await client.sendCommand({ command: "scene.explode", request_id: "req-unknown" });
  check(
    "unknown command -> unknown_command",
    unknown.ok === false && unknown.error.code === "unknown_command"
  );
  check("unknown command echoes request_id", unknown.request_id === "req-unknown");
  check("unknown command lists known commands", /known commands:/.test(unknown.error.message));

  const missingParam = await client.sendCommand({ command: "scene.apply" });
  check(
    "missing scene_id -> validation_error",
    missingParam.ok === false && missingParam.error.code === "validation_error"
  );

  const unknownParam = await client.sendCommand({
    command: "fixture.disable",
    fixture_id: "lamp",
    bogus: 1,
  });
  check(
    "unknown param -> validation_error",
    unknownParam.ok === false && unknownParam.error.code === "validation_error"
  );

  const notFound = await client.sendCommand({ command: "scene.apply", scene_id: "nope" });
  check(
    "unknown scene -> not_found",
    notFound.ok === false && notFound.error.code === "not_found"
  );

  const rename = await client.sendCommand({
    command: "scene.rename",
    scene_id: "twilight",
    name: "Twilight Extended",
    request_id: "req-rename",
  });
  check("scene.rename ok", rename.ok === true);
  const scenes = await client.getScenes();
  check(
    "rename changed name only (id stable)",
    scenes.scenes.find((s) => s.id === "twilight").name === "Twilight Extended"
  );

  const archive = await client.sendCommand({ command: "scene.archive", scene_id: "twilight" });
  check("scene.archive ok", archive.ok === true);
  const restore = await client.sendCommand({ command: "scene.restore", scene_id: "twilight" });
  check("scene.restore ok", restore.ok === true);

  const events = await client.getRecentEvents();
  check(
    "getRecentEvents returns newest-first events",
    Array.isArray(events.events) &&
      events.events.length >= 1 &&
      events.events[0].timestamp >= events.events[events.events.length - 1].timestamp
  );
  check(
    "events carry OperationalEvent shape",
    events.events.every(
      (e) =>
        typeof e.timestamp === "string" &&
        ["info", "warning", "error"].includes(e.level) &&
        ["scene", "playback", "fixture", "discovery", "system"].includes(e.category) &&
        typeof e.summary === "string"
    )
  );

  const exportRes = await client.sendCommand({
    command: "diagnostics.export",
    redact: true,
    recent_events: 10,
  });
  check(
    "diagnostics.export ok + redacted",
    exportRes.ok === true && exportRes.data.redacted === true && exportRes.data.events.length <= 10
  );

  // --- R5C playback model: pure helper coverage --------------------------
  console.log("\n== R5C playback helpers (playback.js) ==");
  {
    const exec = (fixture_id, provider, execution, fidelity, ok, detail = "") => ({
      fixture_id,
      provider,
      execution,
      fidelity,
      ok,
      ...(detail ? { detail } : {}),
    });
    const makeSession = (over = {}) => ({
      session_id: "sess-t-1",
      scene_id: "aurora_flow",
      scene_name: "Aurora Flow",
      target_ids: ["office"],
      fixture_ids: ["g_strip", "wled_seg_0"],
      state: "active",
      started_at: "2026-09-10T21:04:00Z",
      fixture_executions: [],
      ...over,
    });
    const NORMAL_POLICY = ["scene.apply", "scene.preview", "playback.start", "playback.pause", "playback.resume", "playback.stop"];
    const READ_ONLY_POLICY = ["scene.preview", "diagnostics.export"];

    // normalizePlayback: empty + collection + legacy singular shapes
    check("R5C: normalizePlayback(null) -> empty collection", (() => { const pb = normalizePlayback(null); return pb.sessions.length === 0 && pb.counts.active === 0 && pb.owned_fixture_count === 0; })());
    check("R5C: normalizePlayback(undefined/number) -> empty collection", normalizePlayback(undefined).sessions.length === 0 && normalizePlayback(5).sessions.length === 0);
    const collection = normalizePlayback({
      sessions: [makeSession(), makeSession({ session_id: "sess-t-2", state: "paused" })],
      counts: { active: 1, paused: 1, orphaned: 0, stopped: 0 },
      owned_fixture_count: 4,
    });
    check(
      "R5C: normalizePlayback passes the R5A collection shape through",
      collection.sessions.length === 2 && collection.counts.active === 1 && collection.counts.paused === 1 && collection.owned_fixture_count === 4
    );
    const legacy = normalizePlayback({ scene_id: "aurora_flow", target_id: "office", started_at: "2026-09-10T21:04:00Z", paused: true });
    check(
      "R5C: legacy singular shape -> one synthesized session marked legacy",
      legacy.sessions.length === 1 && legacy.sessions[0].legacy === true && legacy.sessions[0].state === "paused" && legacy.sessions[0].scene_id === "aurora_flow"
    );
    check("R5C: legacy synthesized session_id is never sendable", hasSendableSessionId(legacy.sessions[0]) === false);
    check("R5C: legacy singular counts classify as paused", legacy.counts.paused === 1 && legacy.counts.active === 0);
    check("R5C: real backend session_id is sendable", hasSendableSessionId(makeSession({ session_id: "sess-20260912T000000-abcd1234efgh" })) === true);

    // classification
    const mixed = normalizePlayback({
      sessions: [
        makeSession({ session_id: "s-a", state: "active" }),
        makeSession({ session_id: "s-p", state: "paused", scene_id: "twilight" }),
        makeSession({ session_id: "s-o", state: "orphaned" }),
        makeSession({ session_id: "s-x", state: "stopped", stopped_at: "2026-09-10T21:00:00Z" }),
      ],
      counts: { active: 1, paused: 1, orphaned: 1, stopped: 1 },
      owned_fixture_count: 6,
    });
    check(
      "R5C: liveSessions = active+paused+orphaned (never stopped)",
      liveSessions(mixed).length === 3 && stoppedSessions(mixed).every((s) => !liveSessions(mixed).some((l) => l.session_id === s.session_id))
    );
    check(
      "R5C: per-state selectors classify exactly",
      activeSessions(mixed).length === 1 && pausedSessions(mixed).length === 1 && orphanedSessions(mixed).length === 1 && stoppedSessions(mixed).length === 1
    );

    // sessionsForScene: ZERO / ONE / MANY (the R5C cardinality correction)
    check("R5C: sessionsForScene -> ZERO for an idle scene", sessionsForScene(mixed, "meeting_blue").length === 0);
    check("R5C: sessionsForScene -> ONE for a single-session scene", sessionsForScene(mixed, "twilight").length === 1);
    const sameScene = normalizePlayback({
      sessions: [
        makeSession({ session_id: "s-hue", fixture_ids: ["g_strip"] }),
        makeSession({ session_id: "s-wled", state: "paused", fixture_ids: ["wled_seg_0"] }),
      ],
      counts: { active: 1, paused: 1, orphaned: 0, stopped: 0 },
      owned_fixture_count: 2,
    });
    check(
      "R5C: sessionsForScene -> MANY for the same scene (disjoint fixture sets coexist)",
      sessionsForScene(sameScene, "aurora_flow").length === 2
    );
    check("R5C: sessionById resolves by exact id", sessionById(mixed, "s-o").state === "orphaned" && sessionById(mixed, "nope") === null);

    // summarizeSession: aggregate without hiding failure
    const degradedSession = makeSession({
      fixture_executions: [
        exec("g_strip", "hue_v2", "native_dynamic_palette", "native", true),
        exec("lamp", "hue_v2", "native_scene", "native", true),
        exec("wled_seg_2", "wled", "approximate_static", "approximate", false, "provider unavailable"),
        exec("custom_gradient", "hue_v2", "approximate_static", "unsupported", false, "no representable CLIP v2 scene action"),
      ],
    });
    const degradedSum = summarizeSession(degradedSession);
    check(
      "R5C: summarizeSession counts fixtures/ok/failed from backend records",
      degradedSum.fixture_count === 4 && degradedSum.ok_count === 2 && degradedSum.failed_count === 2
    );
    check(
      "R5C: summarizeSession groups per provider",
      degradedSum.providers.length === 2 &&
        degradedSum.providers.find((p) => p.provider === "hue_v2").total === 3 &&
        degradedSum.providers.find((p) => p.provider === "wled").ok + "/" + degradedSum.providers.find((p) => p.provider === "wled").total === "0/1"
    );
    check(
      "R5C: summarizeSession counts backend fidelity values exactly (never inferred)",
      degradedSum.fidelity.native === 2 && degradedSum.fidelity.approximate === 1 && degradedSum.fidelity.unsupported === 1 && degradedSum.fidelity.equivalent === 0
    );
    check(
      "R5C: summarizeSession degraded = failed OR unsupported, with detail",
      JSON.stringify(degradedSum.degraded.map((d) => d.fixture_id)) === JSON.stringify(["wled_seg_2", "custom_gradient"]) &&
        degradedSum.degraded[0].detail === "provider unavailable"
    );
    check(
      "R5C: summarizePlayback aggregates live counts + degraded sessions",
      (() => {
        const sum = summarizePlayback(normalizePlayback({
          sessions: [degradedSession, makeSession({ session_id: "s-2", state: "orphaned" })],
          counts: { active: 1, orphaned: 1, paused: 0, stopped: 2 },
          owned_fixture_count: 6,
        }));
        return sum.active === 1 && sum.orphaned === 1 && sum.live === 2 && sum.stopped === 2 && sum.degradedSessions === 1;
      })()
    );

    // sessionTone matrix (existing status semantics — no new color system)
    const allOkExecs = [exec("g_strip", "hue_v2", "native_scene", "native", true), exec("wled_seg_0", "wled", "native_effect", "native", true)];
    check("sessionTone: active, all executions ok -> ok", sessionTone(summarizeSession(makeSession({ fixture_executions: allOkExecs }))) === "ok");
    check("sessionTone: paused but healthy -> ok (Paused is carried by state text)", sessionTone(summarizeSession(makeSession({ state: "paused", fixture_executions: allOkExecs }))) === "ok");
    check(
      "sessionTone: partial failure -> warn",
      sessionTone(summarizeSession(makeSession({ fixture_executions: [...allOkExecs, exec("wled_seg_2", "wled", "approximate_static", "approximate", false, "provider down")] }))) === "warn"
    );
    check(
      "sessionTone: every reported execution failed -> err",
      sessionTone(summarizeSession(makeSession({ fixture_executions: [exec("a", "wled", "approximate_static", "approximate", false, "x"), exec("b", "wled", "approximate_static", "approximate", false, "y")] }))) === "err"
    );
    check(
      "sessionTone: unsupported realization warns even when ok reported",
      sessionTone(summarizeSession(makeSession({ fixture_executions: [exec("c", "hue_v2", "native_scene", "unsupported", true, "lossy")] }))) === "warn"
    );
    check("sessionTone: orphaned always warns (provider may still be animating)", sessionTone(summarizeSession(makeSession({ state: "orphaned", fixture_executions: allOkExecs }))) === "warn");
    check("sessionTone: stopped -> idle", sessionTone(summarizeSession(makeSession({ state: "stopped", fixture_executions: allOkExecs }))) === "idle");
    check("sessionTone: no execution records (legacy) -> warn (unverifiable)", sessionTone(summarizeSession(makeSession({ legacy: true }))) === "warn");

    // controlsForSession: state x policy matrix (R5C plan §5)
    const controlsOf = (sess, policy = NORMAL_POLICY) => controlsForSession(sess, policy).controls;
    const activeSession = makeSession();
    const pausedSession = makeSession({ state: "paused" });
    const orphanSession = makeSession({ state: "orphaned" });
    const stoppedSession = makeSession({ state: "stopped", stopped_at: "2026-09-10T21:00:00Z" });
    const legacySession = makeSession({ legacy: true });
    check(
      "R5C controls: active -> Pause+Stop offered+enabled, no Resume",
      controlsOf(activeSession).pause.enabled && controlsOf(activeSession).stop.enabled && !controlsOf(activeSession).resume.offered
    );
    check(
      "R5C controls: paused -> Resume+Stop offered+enabled, NO Pause (intent-accurate UI)",
      controlsOf(pausedSession).resume.enabled && controlsOf(pausedSession).stop.enabled && !controlsOf(pausedSession).pause.offered
    );
    check(
      "R5C controls: orphaned -> Stop only (no Resume/Pause)",
      controlsOf(orphanSession).stop.enabled && !controlsOf(orphanSession).pause.offered && !controlsOf(orphanSession).resume.offered
    );
    check(
      "R5C controls: stopped -> nothing offered",
      ["pause", "resume", "stop"].every((a) => !controlsOf(stoppedSession)[a].offered)
    );
    check(
      "R5C controls: legacy session -> offered controls disabled with the R5-backend explanation",
      controlsOf(legacySession).pause.offered && !controlsOf(legacySession).pause.enabled && controlsOf(legacySession).pause.reason === "legacy" && /R5 backend/.test(controlsOf(legacySession).pause.title)
    );
    check(
      "R5C controls: read-only runtime policy disables every mutation with a policy reason",
      ["pause", "resume", "stop"].every((a) => {
        const c = controlsForSession(activeSession, READ_ONLY_POLICY).controls[a];
        const p = controlsForSession(pausedSession, READ_ONLY_POLICY).controls[a];
        return (a === "pause" ? !c.enabled && c.reason === "policy" : true) && (a === "resume" ? !p.enabled && p.reason === "policy" : true);
      }) && controlsForSession(activeSession, READ_ONLY_POLICY).controls.stop.reason === "policy"
    );
    check("R5C controls: unknown session (null) -> nothing offered", ["pause", "resume", "stop"].every((a) => !controlsForSession(null, NORMAL_POLICY).controls[a].offered));

    // --- R5C smoke matrix (plan §11) against the mock client + store ------
    console.log("\n== R5C workbench matrix (mock + helpers) ==");
    // 1. zero sessions
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
      const pb = normalizePlayback(client.getStatus().playback);
      check("matrix 1: zero sessions -> empty live collection, nothing controllable", liveSessions(pb).length === 0 && pb.counts.active === 0);
    }
    // 2. one active session — controls target the exact session_id
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "dynamic-active" });
      const pb = normalizePlayback(client.getStatus().playback);
      const s = liveSessions(pb)[0];
      check("matrix 2: one active session with backend-key fixture executions", liveSessions(pb).length === 1 && s.state === "active" && s.fixture_executions.length === 13 && s.fixture_ids.length === 13);
      const controls = controlsForSession(s, client.getStatus().runtime.allowed_commands).controls;
      check("matrix 2: Pause+Stop enabled under normal policy, Resume not offered", controls.pause.enabled && controls.stop.enabled && !controls.resume.offered);
    }
    // 3. one paused session — Resume+Stop, no Pause
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "dynamic-active" });
      const started = client.getStatus().playback.sessions[0];
      const pausedRes = await client.sendCommand({ command: "playback.pause", session_id: started.session_id });
      check("matrix 3: pause of the seeded session ok", pausedRes.ok === true, JSON.stringify(pausedRes));
      const pb = normalizePlayback(client.getStatus().playback);
      const s = liveSessions(pb)[0];
      check("matrix 3: session state refreshed to paused", s.state === "paused" && !!s.paused_at);
      const controls = controlsForSession(s, client.getStatus().runtime.allowed_commands).controls;
      check("matrix 3: Resume+Stop enabled, NO Pause offered", controls.resume.enabled && controls.stop.enabled && !controls.pause.offered);
    }
    // 4+5. two disjoint sessions for the SAME scene; targeted mutation
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "multi-session" });
      const pb = normalizePlayback(client.getStatus().playback);
      const sessions = sessionsForScene(pb, "aurora_flow");
      check(
        "matrix 4: two disjoint live sessions render independently",
        sessions.length === 2 && sessions[0].session_id !== sessions[1].session_id && sessions.every((s) => s.scene_id === "aurora_flow")
      );
      check(
        "matrix 4: fixture ownership sets are disjoint",
        sessions[0].fixture_ids.every((id) => !sessions[1].fixture_ids.includes(id))
      );
      check(
        "matrix 5: same-scene sessions return MANY from sessionsForScene (no singleton assumption)",
        sessionsForScene(pb, "aurora_flow").length === 2
      );
      const hue = sessions.find((s) => s.fixture_ids.includes("g_strip"));
      const wled = sessions.find((s) => s.fixture_ids.includes("wled_seg_0"));
      const pausedRes = await client.sendCommand({ command: "playback.pause", session_id: hue.session_id });
      check("matrix 4: pausing one session succeeds", pausedRes.ok === true, JSON.stringify(pausedRes));
      const pbAfter = normalizePlayback(client.getStatus().playback);
      const hueAfter = sessionById(pbAfter, hue.session_id);
      const wledAfter = sessionById(pbAfter, wled.session_id);
      check(
        "matrix 4: targeted mutation — the other session is untouched",
        hueAfter.state === "paused" && wledAfter.state === "active"
      );
      // 8. stopped retention: present in the collection, never live
      const stopped = stoppedSessions(pbAfter);
      check(
        "matrix 8: retained stopped session excluded from live panel data",
        stopped.length === 1 && stopped[0].state === "stopped" && stoppedSessions(pbAfter).every((s) => !liveSessions(pbAfter).includes(s))
      );
    }
    // 6. partial provider failure — session stays visible with obvious degradation
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "playback-degraded" });
      const pb = normalizePlayback(client.getStatus().playback);
      const s = liveSessions(pb)[0];
      const sum = summarizeSession(s);
      check("matrix 6: degraded session remains live", s.state === "active" && liveSessions(pb).length === 1);
      check(
        "matrix 6: failed fixture/provider summary is derivable and obvious",
        sum.failed_count === 2 && sum.degraded.length === 2 && sum.degraded.some((d) => d.fixture_id === "wled_seg_2") && sum.degraded.some((d) => d.fidelity === "unsupported")
      );
      check("matrix 6: session tone warns (partial failure)", sessionTone(sum) === "warn");
    }
    // 7. orphaned session — warning copy data + Stop only
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "playback-orphaned" });
      const pb = normalizePlayback(client.getStatus().playback);
      const s = orphanedSessions(pb)[0];
      check("matrix 7: orphaned session present", !!s && orphanedSessions(pb).length === 1);
      const controls = controlsForSession(s, client.getStatus().runtime.allowed_commands).controls;
      check("matrix 7: Stop available, no Resume/Pause", controls.stop.enabled && !controls.resume.offered && !controls.pause.offered);
      check("matrix 7: orphaned tone warns", sessionTone(summarizeSession(s)) === "warn");
    }
    // 9+10. runtime policy is authoritative (pure matrix above re-asserted
    // through a store-connected client status)
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "dynamic-active" });
      const runtime = client.getStatus().runtime;
      const pb = normalizePlayback(client.getStatus().playback);
      const controls = controlsForSession(liveSessions(pb)[0], runtime.allowed_commands).controls;
      check("matrix 10: normal (R5-like) policy -> lifecycle controls enabled", controls.pause.enabled && controls.stop.enabled);
      const controlsRo = controlsForSession(liveSessions(pb)[0], ["scene.preview"]).controls;
      check("matrix 9: read-only policy -> all playback mutations disabled", !controlsRo.pause.enabled && !controlsRo.stop.enabled && !controlsRo.resume.enabled);
    }
    // 11. legacy live-backend shape — display compatible, controls disabled
    {
      const pb = normalizePlayback({ scene_id: "aurora_flow", target_id: "office", started_at: "2026-09-10T21:04:00Z", paused: false });
      const s = liveSessions(pb)[0];
      check("matrix 11: legacy session displayable with resolved fields", s.legacy === true && s.scene_id === "aurora_flow" && s.target_ids.includes("office"));
      const controls = controlsForSession(s, ["playback.pause", "playback.resume", "playback.stop"]).controls;
      check(
        "matrix 11: legacy controls disabled with legacy reason even under a permissive policy",
        !controls.pause.enabled && !controls.stop.enabled && controls.pause.reason === "legacy" && controls.stop.reason === "legacy"
      );
    }
    // 12. command refresh — pause/resume/stop round trip through the store,
    // routed via the ONE shared playbackActionEnvelope seam that Overview
    // and Scenes both use for their rendered panels.
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "dynamic-active" });
      const store = createStore(client);
      await store.init();
      const sessionId = normalizePlayback(store.state.status.playback).sessions[0].session_id;
      const pause = await store.sendCommand(playbackActionEnvelope("pause", sessionId));
      const pausedState = normalizePlayback(store.state.status.playback).sessions[0];
      check(
        "matrix 12: pause round trip refreshes rendered state from status",
        pause.ok === true && pausedState.state === "paused" && store.state.notice && /Playback paused: Aurora Flow/.test(store.state.notice.text),
        JSON.stringify(store.state.notice)
      );
      const resume = await store.sendCommand(playbackActionEnvelope("resume", sessionId));
      const resumedState = normalizePlayback(store.state.status.playback).sessions[0];
      check(
        "matrix 12: resume round trip returns the session to active",
        resume.ok === true && resumedState.state === "active" && /Playback resumed: Aurora Flow/.test(store.state.notice.text)
      );
      const stop = await store.sendCommand(playbackActionEnvelope("stop", sessionId));
      const pbFinal = normalizePlayback(store.state.status.playback);
      check(
        "matrix 12: stop round trip leaves the session retained-but-stopped (refresh-driven, no optimistic mutation)",
        stop.ok === true && liveSessions(pbFinal).length === 0 && pbFinal.counts.stopped === 1 && /Playback stopped: Aurora Flow/.test(store.state.notice.text)
      );
      // R5C corrective pass: the session_id must survive the seam verbatim —
      // a transformed/dropped id would address the wrong (or no) session.
      const stopWrongId = await store.sendCommand(playbackActionEnvelope("stop", sessionId + "x"));
      check(
        "seam: an altered session_id through the seam addresses nothing (no silent success)",
        stopWrongId.ok === false && stopWrongId.error && stopWrongId.error.code === "conflict"
      );
    }

    // R5C corrective pass: the shared seam's mapping contract + the view
    // wiring guards. The full RENDERED event path (real clicks on the panel
    // buttons inside shadow DOM, both views) is exercised by
    // scripts/browser_regression.mjs (`npm run browser`) — these checks
    // fail fast in the DOM-free suite if the seam or a panel binding is
    // removed.
    check(
      "seam: playbackActionEnvelope maps pause/resume/stop to exact session-addressed envelopes",
      JSON.stringify(playbackActionEnvelope("pause", "sess-1")) === JSON.stringify({ command: "playback.pause", session_id: "sess-1" }) &&
        JSON.stringify(playbackActionEnvelope("resume", "sess-2")) === JSON.stringify({ command: "playback.resume", session_id: "sess-2" }) &&
        JSON.stringify(playbackActionEnvelope("stop", "sess-3")) === JSON.stringify({ command: "playback.stop", session_id: "sess-3" })
    );
    check(
      "seam: unknown/non-lifecycle actions map to null (no fabricated command)",
      playbackActionEnvelope("reclaim", "sess-1") === null && playbackActionEnvelope(undefined, "sess-1") === null
    );
    {
      const overviewSrc = readFileSync(join(here, "..", "src", "views", "overview.js"), "utf8");
      const scenesSrc = readFileSync(join(here, "..", "src", "views", "scenes.js"), "utf8");
      const rowSrc = readFileSync(join(here, "..", "src", "components", "ss-scene-row.js"), "utf8");
      check(
        "wiring: Overview binds @playback-action on its rendered panel (regression: its controls must do something)",
        /@playback-action=\$\{this\.#onPlaybackAction\}/.test(overviewSrc)
      );
      check(
        "wiring: Scenes binds @playback-action on its rendered panel",
        /@playback-action=\$\{this\.#onPlaybackAction\}/.test(scenesSrc)
      );
      check(
        "wiring: both views route panel events through the shared playbackActionEnvelope seam",
        overviewSrc.includes("playbackActionEnvelope") && scenesSrc.includes("playbackActionEnvelope")
      );
      check(
        "wiring: scene rows emit no session lifecycle actions (the panel is the canonical control surface)",
        !/this\.#emit\("(pause|resume|stop)"/.test(rowSrc)
      );
    }
    // R5C corrective pass: scene-row label contract — the runtime tag owns
    // playback-state wording; the glyph is a status dot only when healthy.
    {
      const rowSrc = readFileSync(join(here, "..", "src", "components", "ss-scene-row.js"), "utf8");
      const statusFn = rowSrc.match(/#statusLabel\([\s\S]*?\n  \}/);
      const body = statusFn ? statusFn[0] : "";
      check(
        "row labels: glyph helper is archived | empty when live/healthy | error/degraded — never spelled-out 'ready' or 'playing'",
        body.includes("archived") &&
          /return ""/.test(body) &&
          body.includes('"error"') &&
          body.includes('"degraded"') &&
          !body.includes('"ready"') &&
          !body.includes("playing"),
        body
      );
      check(
        "scenes: rows reuse Fixtures <ss-scope-control> for target membership",
        rowSrc.includes("ss-scope-control") && /<ss-scope-control[\s\S]*target_ids/.test(rowSrc)
      );
      check(
        "scenes: row grid does not cap the name at 200px or park leftover in a gutter",
        !rowSrc.includes("minmax(90px, 200px)") && !/\.gutter\s*\{/.test(rowSrc)
      );
      check(
        "scenes: rows do not paint fidelity bucket counts (nat/eq/approx)",
        !rowSrc.includes("#fidelityText") && !rowSrc.includes('class="fid"')
      );
    }
    {
      const builderSrc = readFileSync(join(here, "..", "src", "views", "scene_builder.js"), "utf8");
      check(
        "builder targets: fixture ids are not painted as underscore labels",
        !builderSrc.includes('class="tid"') && builderSrc.includes("Home Assistant groups") && builderSrc.includes("target-cluster")
      );
      const client = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
      const fixtures = (await client.getFixtures()).fixtures;
      const discovery = await client.getDiscovery();
      const wled = fixtures.filter((f) => f.binding && f.binding.provider === "wled");
      const clustered = withControllerClusters(wled);
      check(
        "grouping: WLED segments collapse to one controller cluster",
        clustered.length === 1 && clustered[0].type === "cluster" && clustered[0].fixtures.length === wled.length,
        JSON.stringify(clustered.map((c) => c.type))
      );
      check(
        "grouping: WLED cluster uses a human controller name, not fixture ids",
        controllerDisplayName(wled) === "WLED" && clusterLabel(wled).includes("Segment")
      );
      const eco = ecosystemGroupsFromDiscovery(discovery, fixtures, (await client.getFixtures()).targets);
      check(
        "grouping: Office Lights is an HA member-set (not a Scene Studio fixture)",
        eco.length === 1 &&
          eco[0].name === "Office Lights" &&
          eco[0].children.length === 3 &&
          eco[0].fixtures.length >= 6 &&
          !fixtures.some((f) => f.id === "office_lights"),
        JSON.stringify(eco.map((g) => ({ name: g.name, members: g.fixtures.length, children: g.children.map((c) => c.name) })))
      );
      const fallbackEco = ecosystemGroupsFromDiscovery(null, fixtures, (await client.getFixtures()).targets);
      check(
        "grouping: Office Lights still appears when discovery has not run",
        fallbackEco.length === 1 &&
          fallbackEco[0].name === "Office Lights" &&
          fallbackEco[0].children.map((c) => c.name).join(",") === "Office Front Lights,Office Side Lights,Office Back Lights",
        JSON.stringify(fallbackEco.map((g) => ({ name: g.name, children: g.children.map((c) => c.name) })))
      );
      const officeIds = eco[0].fixtures.map((f) => f.id);
      const partial = selectionOf(["lamp"], officeIds);
      const allOn = selectionOf(officeIds, officeIds);
      check(
        "grouping: HA group checkbox is tri-state over member fixture ids",
        partial.indeterminate === true && partial.checked === false && allOn.checked === true
      );
      check(
        "grouping: checking an HA group adds member fixture ids, never office_lights",
        JSON.stringify(toggleIdSet([], officeIds, true).sort()) === JSON.stringify([...officeIds].sort()) &&
          !toggleIdSet([], officeIds, true).includes("office_lights")
      );
      check("grouping: rooms still group by membership, not by underscore id labels", groupByRoom(fixtures).length >= 3);
    }
    {
      const collapsed = {
        schema_version: 2,
        id: "meeting_look",
        name: "Meeting Look",
        target_ids: ["office"],
        palette: ["#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff"],
        motion: { mode: "static", speed: 0.5, strategy: "auto" },
        fixture_states: {
          g_strip: { on: true, color: "#39f3ff", gradient: ["#39f3ff", "#39f3ff", "#39f3ff", "#39f3ff"] },
          lamp: { on: true, color: "#56c1ff" },
          office_strip: { on: true, color: "#7aff8d" },
          wled_seg_2: { on: true, color: "#ffffff" },
        },
      };
      const look = sceneLookSwatches(collapsed);
      check(
        "palette: a collapsed cyan palette band uses the per-fixture mix",
        look.includes("#56c1ff") && look.includes("#ffffff") && look.includes("#7aff8d") && new Set(look).size > 1,
        JSON.stringify(look)
      );
      const canonical = canonicalizeStaticPalette(collapsed);
      check(
        "palette: canonicalize rebuilds the palette and pins matching hexes",
        canonical.palette.includes("#56c1ff") &&
          canonical.fixture_states.lamp.palette_index === canonical.palette.indexOf("#56c1ff") &&
          canonical.fixture_states.lamp.color === undefined &&
          canonical.fixture_states.g_strip.gradient.length === 4,
        JSON.stringify(canonical.fixture_states.lamp)
      );
      const lampAssign = resolveFixturePalette(canonical, { id: "lamp", capabilities: { color_xy: true }, binding: { provider: "hue_v2" } }, []);
      check(
        "palette: pinned lights resolve as palette slots, not custom hex",
        lampAssign.pinned === true && lampAssign.custom === false && lampAssign.color === "#56c1ff"
      );
      {
        const segs = [
          { id: "wled_seg_0", binding: { provider: "wled" }, capabilities: { color_xy: true } },
          { id: "wled_seg_1", binding: { provider: "wled" }, capabilities: { color_xy: true } },
        ];
        const clusterDraft = {
          palette: ["#ffee94", "#39f3ff", "#ffffff"],
          motion: { mode: "static" },
          fixture_states: {
            wled_seg_0: { palette_index: 2 },
            wled_seg_1: { palette_index: 2 },
          },
        };
        const unified = resolveClusterPalette(clusterDraft, segs, segs);
        check(
          "palette: a clustered fixture reports one slot when every segment shares it",
          unified.mixed === false && unified.pinned === true && unified.index === 2 && unified.color === "#ffffff",
          JSON.stringify(unified)
        );
        clusterDraft.fixture_states.wled_seg_1 = { palette_index: 0 };
        const mixed = resolveClusterPalette(clusterDraft, segs, segs);
        check(
          "palette: mixed segment colors do not pretend the cluster is one pin",
          mixed.mixed === true && mixed.pinned === false && mixed.colors.includes("#ffffff") && mixed.colors.includes("#ffee94"),
          JSON.stringify(mixed)
        );
      }
      const twilight = baseData.scenes.scenes.find((s) => s.id === "twilight");
      check(
        "palette: a multi-stop authored palette still paints the catalog band as-is",
        JSON.stringify(sceneLookSwatches(twilight)) === JSON.stringify(twilight.palette)
      );
    }
    // mock fidelity: start preemption + bounded stopped retention
    {
      const client = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
      const first = await client.sendCommand({ command: "playback.start", scene_id: "aurora_flow" });
      check("R5C mock: playback.start returns a real session_id", first.ok === true && !!first.data.session_id);
      const second = await client.sendCommand({ command: "playback.start", scene_id: "aurora_flow" });
      const pb = normalizePlayback(client.getStatus().playback);
      check(
        "R5C mock: overlapping start preempts only the overlapping session",
        second.ok === true && pb.counts.active === 1 && pb.counts.stopped === 1 && sessionById(pb, first.data.session_id).state === "stopped" && sessionById(pb, first.data.session_id).preempted_by === second.data.session_id
      );
      for (let i = 0; i < 9; i++) {
        const start = await client.sendCommand({ command: "playback.start", scene_id: "aurora_flow" });
        await client.sendCommand({ command: "playback.stop", session_id: start.data.session_id });
      }
      const pbTrim = normalizePlayback(client.getStatus().playback);
      check("R5C mock: stopped retention stays bounded (never dumped into live)", pbTrim.counts.stopped <= 5 && liveSessions(pbTrim).length === 0);
    }
  }

  // --- Scene Builder vertical slice (Pass 2 plan §8) ---------------------
  console.log("\n== Scene Builder (Pass 2 authoring contract) ==");
  {
    // Mock-contract level: preview_draft / create / update envelopes.
    const client = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const scenesBefore = (await client.getScenes()).scenes.length;
    const draft = {
      schema_version: 2,
      id: "",
      name: "Evening Glow",
      target_ids: ["office"],
      palette: ["#FF7A45", "#6F4BFF"],
      brightness: 65,
      motion: { mode: "palette_cycle", speed: 0.45, strategy: "auto" },
      default_state: { on: true, brightness: 60 },
    };
    const preview = await client.sendCommand({ command: "scene.preview_draft", scene: draft });
    check(
      "builder mock: preview_draft derives the candidate id server-side (no JS slug rules)",
      preview.ok === true && preview.data.scene.id === "evening_glow",
      JSON.stringify(preview).slice(0, 200)
    );
    check(
      "builder mock: preview_draft returns a canonical render-plan shape (dry_run)",
      preview.data.dry_run === true && preview.data.render_plan.scene_id === "evening_glow" && Array.isArray(preview.data.render_plan.fixture_plans)
    );
    check(
      "builder mock: preview skips the disabled fixture into skipped_fixture_ids (fidelity warning scenario)",
      (() => {
        const disabledClient = createMockSceneStudioClient(baseData, { scenarioId: "disabled-fixture" });
        return (
          disabledClient.getStatus().fixtures.disabled >= 1 &&
          disabledClient
            .getStatus()
            .runtime.allowed_commands.includes("scene.preview_draft")
        );
      })()
    );
    const disabledPreview = await createMockSceneStudioClient(baseData, { scenarioId: "disabled-fixture" }).sendCommand({
      command: "scene.preview_draft",
      scene: draft,
    });
    check(
      "builder mock: disabled-fixture scenario preview warns via skipped_fixture_ids + notes",
      disabledPreview.ok === true &&
        disabledPreview.data.render_plan.skipped_fixture_ids.includes("double_strip") &&
        disabledPreview.data.render_plan.notes.some((n) => n.includes("double_strip"))
    );
    check("builder mock: preview normalizes palette hexes like the backend", preview.data.scene.palette[0] === "#ff7a45");
    check("builder mock: preview does not mutate the catalog", (await client.getScenes()).scenes.length === scenesBefore);

    const play = await client.sendCommand({ command: "scene.play_draft", scene: draft });
    check(
      "builder mock: play_draft plays the unsaved draft (not a dry-run)",
      play.ok === true &&
        play.data.played === true &&
        play.data.dry_run === false &&
        play.data.kind === "playback" &&
        !!play.data.session_id &&
        play.data.scene.id === "evening_glow",
      JSON.stringify(play).slice(0, 240)
    );
    check(
      "builder mock: play_draft still does not mutate the catalog",
      (await client.getScenes()).scenes.length === scenesBefore &&
        !(await client.getScenes()).scenes.some((s) => s.id === "evening_glow")
    );
    check(
      "builder mock: play_draft is live on playback status",
      (client.getStatus().playback.sessions || []).some((s) => s.session_id === play.data.session_id && s.state === "active")
    );
    check(
      "builder mock: play_draft is in the normal command catalog",
      client.getStatus().runtime.allowed_commands.includes("scene.play_draft")
    );
    const unknownPlay = await client.sendCommand({
      command: "scene.play_draft",
      scene: { ...draft, target_ids: ["not_a_real_target"] },
    });
    check(
      "builder mock: play_draft rejects unknown targets",
      unknownPlay.ok === false && unknownPlay.error.code === "validation_error"
    );
    const staticPlay = await client.sendCommand({
      command: "scene.play_draft",
      scene: { ...draft, name: "Static Glow", motion: { mode: "static", speed: 0, strategy: "auto" } },
    });
    check(
      "builder mock: static play_draft applies without persisting",
      staticPlay.ok === true &&
        staticPlay.data.kind === "apply" &&
        staticPlay.data.played === true &&
        !(await client.getScenes()).scenes.some((s) => s.id === "static_glow")
    );

    // Canonical parity for the fields the Builder now authors (default_state /
    // fixture_states): the mock must reject the same mistakes, at the same
    // dotted paths, instead of only failing live.
    const stateCases = [
      ["a non-object default_state", { default_state: "on" }, "scene.default_state"],
      ["an empty default_state", { default_state: {} }, "scene.default_state"],
      ["an out-of-range default brightness", { default_state: { brightness: 150 } }, "scene.default_state.brightness"],
      ["an invalid default color", { default_state: { on: true, color: "#zzz" } }, "scene.default_state.color"],
      ["an out-of-range color temperature", { default_state: { color_temp_mirek: 5 } }, "scene.default_state.color_temp_mirek"],
      ["an unknown default field", { default_state: { brightness: 10, nonsense: 1 } }, "scene.default_state.nonsense"],
      ["a non-object fixture_states", { fixture_states: [] }, "scene.fixture_states"],
      ["an empty override", { fixture_states: { g_strip: {} } }, "scene.fixture_states.g_strip"],
      ["an unknown override field", { fixture_states: { g_strip: { bri: 10 } } }, "scene.fixture_states.g_strip.bri"],
      [
        "an out-of-range palette_index",
        { fixture_states: { g_strip: { palette_index: 24 } } },
        "scene.fixture_states.g_strip.palette_index",
      ],
      [
        "color and palette_index together",
        { fixture_states: { g_strip: { color: "#112233", palette_index: 0 } } },
        "scene.fixture_states.g_strip",
      ],
      [
        "a provider_ext namespace that is not a provider",
        { fixture_states: { g_strip: { provider_ext: { zzz: {} } } } },
        "scene.fixture_states.g_strip.provider_ext.zzz",
      ],
    ];
    for (const [label, patch, expectedPath] of stateCases) {
      const rejected = await client.sendCommand({
        command: "scene.preview_draft",
        scene: { ...draft, ...patch },
      });
      check(
        `builder mock parity: rejects ${label} at the canonical path`,
        rejected.ok === false &&
          rejected.error.code === "validation_error" &&
          rejected.error.details.path === expectedPath,
        JSON.stringify(rejected).slice(0, 200)
      );
    }
    const validStates = await client.sendCommand({
      command: "scene.preview_draft",
      scene: {
        ...draft,
        default_state: { on: true, brightness: 40, color: "#112233", color_temp_mirek: 300 },
        fixture_states: { g_strip: { on: false, brightness: 70, gradient: ["#111111", "#222222"], transition_ms: 400 } },
      },
    });
    check(
      "builder mock parity: canonical defaults + a rich override still validate",
      validStates.ok === true && validStates.data.render_plan.fixture_plans.length > 0,
      JSON.stringify(validStates).slice(0, 200)
    );
    const validPin = await client.sendCommand({
      command: "scene.preview_draft",
      scene: {
        ...draft,
        palette: ["#112233", "#445566"],
        motion: { mode: "static", speed: 0, strategy: "auto" },
        default_state: { on: true, brightness: 40 },
        fixture_states: { lamp: { on: true, brightness: 40, palette_index: 1 } },
      },
    });
    check(
      "builder mock parity: static palette_index pins validate",
      validPin.ok === true && validPin.data.render_plan.fixture_plans.length > 0,
      JSON.stringify(validPin).slice(0, 200)
    );
    const badPreview = await client.sendCommand({ command: "scene.preview_draft", scene: { ...draft, palette: ["nope"] } });
    check(
      "builder mock: preview validation failure carries the canonical dotted path",
      badPreview.ok === false && badPreview.error.code === "validation_error" && badPreview.error.details.path === "scene.palette.0"
    );

    const created = await client.sendCommand({ command: "scene.create", scene: draft });
    check(
      "builder mock: scene.create persists and returns the canonical document",
      created.ok === true && created.data.scene.id === "evening_glow" && created.data.scene.name === "Evening Glow",
      JSON.stringify(created).slice(0, 200)
    );
    check("builder mock: created scene appears in the catalog", (await client.getScenes()).scenes.some((s) => s.id === "evening_glow"));
    const dupe = await client.sendCommand({ command: "scene.create", scene: draft });
    check("builder mock: duplicate active id -> conflict", dupe.ok === false && dupe.error.code === "conflict");
    const archivedCollision = await client.sendCommand({
      command: "scene.create",
      scene: { ...draft, name: "Meeting Blue", target_ids: ["office"] },
    });
    check(
      "builder mock: archived-id collision -> conflict naming restore",
      archivedCollision.ok === false && archivedCollision.error.code === "conflict" && /archived/.test(archivedCollision.error.message)
    );
    const unknownTarget = await client.sendCommand({ command: "scene.create", scene: { ...draft, target_ids: ["ghost_room"] } });
    check(
      "builder mock: unknown target rejected at save time with canonical path",
      unknownTarget.ok === false && unknownTarget.error.code === "validation_error" && unknownTarget.error.details.path === "scene.target_ids.0"
    );

    const update = await client.sendCommand({
      command: "scene.update",
      scene_id: "evening_glow",
      scene: {
        ...draft,
        id: "evening_glow",
        name: "Evening Glow Deep",
        palette: ["#6f4bff", "#ff7a45"],
        motion: { mode: "static", speed: 0.0, strategy: "auto" },
      },
    });
    check(
      "builder mock: scene.update atomically replaces the document",
      update.ok === true && update.data.scene.name === "Evening Glow Deep" && update.data.scene.palette[0] === "#6f4bff" && update.data.scene.motion.mode === "static",
      JSON.stringify(update).slice(0, 200)
    );
    const idChange = await client.sendCommand({
      command: "scene.update",
      scene_id: "evening_glow",
      scene: { ...draft, id: "some_other_id" },
    });
    check(
      "builder mock: update cannot change the stable id",
      idChange.ok === false && idChange.error.code === "validation_error" && /immutable/.test(idChange.error.message)
    );
    const archivedEdit = await client.sendCommand({
      command: "scene.update",
      scene_id: "meeting_blue",
      scene: { schema_version: 2, id: "meeting_blue", name: "X", target_ids: ["office"], motion: { mode: "static", speed: 0.0, strategy: "auto" } },
    });
    check(
      "builder mock: editing an archived scene -> conflict (restore first)",
      archivedEdit.ok === false && archivedEdit.error.code === "conflict" && /restore/.test(archivedEdit.error.message)
    );
    const twilightBefore = (await client.getScenes()).scenes.find((s) => s.id === "twilight");
    check(
      "builder mock: precondition — twilight carries migrated_from_v1 provenance",
      !!(twilightBefore.metadata && twilightBefore.metadata.migrated_from_v1)
    );
    const provenance = await client.sendCommand({
      command: "scene.update",
      scene_id: "twilight",
      scene: {
        schema_version: 2,
        id: "twilight",
        name: "Twilight",
        target_ids: ["office"],
        palette: twilightBefore.palette,
        brightness: 50,
        motion: { mode: "static", speed: 0.0, strategy: "auto" },
      },
    });
    check(
      "builder mock: update preserves migrated_from_v1 provenance the payload omitted",
      provenance.ok === true &&
        provenance.data.scene.metadata.migrated_from_v1.filename === "twilight.json" &&
        provenance.data.scene.brightness === 50,
      JSON.stringify(provenance).slice(0, 240)
    );

    // Store-level journeys (the real user path through the Workbench store).
    const storeClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const envelopes = [];
    const origSend = storeClient.sendCommand.bind(storeClient);
    storeClient.sendCommand = async (envelope) => {
      envelopes.push(envelope);
      return origSend(envelope);
    };
    const store = createStore(storeClient);
    await store.init();

    check("builder 1: New Scene opens a sensible draft with scene defaults", deepEqual(defaultBuilderDraft(), {
      schema_version: 2,
      id: "",
      name: "",
      target_ids: [],
      palette: [],
      brightness: 60,
      motion: { mode: "static", speed: 0.0, strategy: "auto" },
      default_state: { on: true, brightness: 60 },
    }));
    await store.openBuilder({});
    check(
      "builder 1: openBuilder(create) routes to the Builder with a clean draft",
      store.state.view === "scene_builder" && store.state.builder.mode === "create" && store.state.builder.dirty === false
    );
    const earlySave = await store.saveBuilderDraft();
    check(
      "builder 2: cannot save without required name/target data (local guard at the canonical path)",
      earlySave.ok === false && earlySave.error.details.path === "scene.name" && !!store.state.builder
    );
    store.patchBuilderDraft({ name: "Smoke Scene", target_ids: ["office"] });
    check("builder 2: a filled draft is dirty", store.state.builder.dirty === true);
    store.patchBuilderDraft({ palette: ["#111111", "#222233"], motion: { mode: "palette_cycle", speed: 0.4, strategy: "auto" } });
    const previewRes = await store.previewBuilderDraft();
    const catalogNow = await storeClient.getScenes();
    check(
      "builder 3+6: preview renders a plan for the draft without mutating the catalog",
      previewRes.ok === true &&
        store.state.builder.preview.render_plan.scene_id === "smoke_scene" &&
        store.state.scenes.scenes.filter((s) => !s.metadata || !s.metadata.archived_at).length ===
          catalogNow.scenes.filter((s) => !s.metadata || !s.metadata.archived_at).length
    );
    check(
      "builder 3: Preview plays the draft on fixtures (play_draft) rather than a dry-run plan",
      previewRes.ok === true &&
        store.state.builder.preview.played === true &&
        store.state.builder.preview.kind === "playback" &&
        envelopes.some((e) => e.command === "scene.play_draft")
    );
    // Static <-> Dynamic + normalized speed mapping (controls feed canonical motion).
    store.patchBuilderDraft({ motion: { mode: "static", speed: 0.0, strategy: "auto" } });
    check("builder 4: Static maps to canonical mode=static/speed=0", store.state.builder.draft.motion.mode === "static" && store.state.builder.draft.motion.speed === 0);
    store.patchBuilderOverride("lamp", { on: true, brightness: 60, palette_index: 1 });
    check(
      "builder: a palette_index pin is stored without a competing hex",
      store.state.builder.draft.fixture_states.lamp.palette_index === 1 &&
        store.state.builder.draft.fixture_states.lamp.color === undefined
    );
    store.patchBuilderOverride("lamp", { color: "#abcdef" });
    check(
      "builder: setting a custom hex clears the palette pin",
      store.state.builder.draft.fixture_states.lamp.color === "#abcdef" &&
        store.state.builder.draft.fixture_states.lamp.palette_index === undefined
    );
    store.removeBuilderOverride("lamp");
    store.patchBuilderDraft({ motion: { mode: "palette_cycle", speed: 0.85, strategy: "auto" } });
    check(
      "builder 4+5: Dynamic maps to palette_cycle with the normalized speed",
      store.state.builder.draft.motion.mode === "palette_cycle" && store.state.builder.draft.motion.speed === 0.85 && store.state.builder.draft.motion.strategy === "auto"
    );
    // Palette order is preserved into the outbound payload (checked on save).
    store.patchBuilderDraft({ palette: ["#222233", "#111111"] });

    await store.setView("overview");
    check(
      "builder guard: navigating away while dirty parks on the in-app confirmation (no silent discard)",
      store.state.view === "scene_builder" && !!store.state.builderExit && store.state.builderExit.view === "overview"
    );
    store.cancelBuilderExit();
    check("builder guard: Stay dismisses the confirmation and keeps the dirty draft", store.state.builderExit === null && store.state.builder.dirty === true);

    const saveRes = await store.saveBuilderDraft();
    check(
      "builder 7: Save New sends scene.create with the exact ordered palette + motion",
      saveRes.ok === true &&
        envelopes.some(
          (e) => e.command === "scene.create" && e.scene.palette[0] === "#222233" && e.scene.palette[1] === "#111111" && e.scene.motion.speed === 0.85
        ),
      JSON.stringify(envelopes[envelopes.length - 1]).slice(0, 240)
    );
    check(
      "builder 7: after save the draft closes, the view returns to Scenes, and the saved scene is selected",
      store.state.builder === null && store.state.view === "scenes" && !!store.state.selection && store.state.selection.type === "scene" && store.state.selection.id === "smoke_scene"
    );
    check(
      "builder 7: the catalog holds the server document, not the optimistic draft",
      store.state.scenes.scenes.some((s) => s.id === "smoke_scene" && s.name === "Smoke Scene" && s.motion.speed === 0.85)
    );

    // 10. backend validation failure leaves the draft intact (no store mutation).
    await store.openBuilder({});
    store.patchBuilderDraft({ name: "Twilight", target_ids: ["office"] }); // collides with the migrated scene id
    const conflictRes = await store.saveBuilderDraft();
    check(
      "builder 10: a save conflict keeps the Builder open with the draft and error attached",
      conflictRes.ok === false && conflictRes.error.code === "conflict" && !!store.state.builder && store.state.builder.draft.name === "Twilight" && store.state.builder.error.path === null
    );
    store.closeBuilder();

    // 8. Edit Existing pre-populates the current scene intent.
    await store.openBuilder({ sceneId: "smoke_scene" });
    check(
      "builder 8: Edit pre-populates the canonical document (clean, edit mode)",
      store.state.builder.mode === "edit" && store.state.builder.sceneId === "smoke_scene" && store.state.builder.draft.name === "Smoke Scene" && store.state.builder.dirty === false
    );
    store.patchBuilderDraft({ brightness: 25, name: "Smoke Scene Renamed" });
    const updateRes = await store.saveBuilderDraft();
    const updatedDoc = store.state.scenes.scenes.find((s) => s.id === "smoke_scene");
    check(
      "builder 9: Save Changes sends scene.update with the authoritative scene_id",
      updateRes.ok === true && envelopes.some((e) => e.command === "scene.update" && e.scene_id === "smoke_scene" && e.scene.brightness === 25)
    );
    check(
      "builder 9: id stays stable and the refreshed catalog carries the new values",
      updatedDoc.id === "smoke_scene" && updatedDoc.name === "Smoke Scene Renamed" && updatedDoc.brightness === 25 && store.state.builder === null && store.state.view === "scenes"
    );

    // 12. Apply/Play route through the EXISTING command paths (no Builder
    // execution commands exist to begin with).
    const applyRes = await store.sendCommand({ command: "scene.apply", scene_id: "smoke_scene" });
    check(
      "builder 12: a Builder-created static scene applies through the existing scene.apply path",
      applyRes.ok === true && applyRes.data.applied === true,
      JSON.stringify(applyRes).slice(0, 200)
    );
    await store.openBuilder({});
    store.patchBuilderDraft({ name: "Smoke Dynamic", target_ids: ["office"], palette: ["#112233"], motion: { mode: "palette_cycle", speed: 0.5, strategy: "auto" }, default_state: { on: true, brightness: 40 } });
    await store.saveBuilderDraft();
    const playRes = await store.sendCommand({ command: "playback.start", scene_id: "smoke_dynamic" });
    check(
      "builder 12: a Builder-created dynamic scene plays through the existing playback.start path",
      playRes.ok === true && !!playRes.data.session_id,
      JSON.stringify(playRes).slice(0, 240)
    );
    const stopRes = await store.sendCommand({ command: "playback.stop", session_id: playRes.data.session_id });
    check("builder 12: the returned session stops through the existing session-addressed control", stopRes.ok === true);

    // 11. read-only policy: persistence blocked, preview still permitted.
    await store.openBuilder({});
    store.patchBuilderDraft({ name: "Read Only Draft", target_ids: ["office"] });
    store.state.status = {
      ...(store.state.status || {}),
      runtime: {
        mode: "read_only",
        read_only: true,
        provider_writes_blocked: true,
        allowed_commands: ["scene.preview_draft", "scene.preview", "scene.apply"],
      },
    };
    check(
      "builder 11: read-only policy blocks persistence actions (UI + rawCommand)",
      store.commandAllowed("scene.create") === false && store.commandAllowed("scene.update") === false
    );
    check(
      "builder 11: read-only still allows Builder ENTRY (observational preview_draft)",
      store.commandAllowed("scene.preview_draft") === true
    );
    const roPreview = await store.previewBuilderDraft();
    check("builder 11: read-only policy still permits draft preview", roPreview.ok === true && !store.state.builder.preview.played);
    const roSave = await store.saveBuilderDraft();
    check(
      "builder 11: saving under read-only is refused with the mode conflict (draft intact)",
      roSave.ok === false && roSave.error.code === "conflict" && !!store.state.builder
    );
    store.closeBuilder();
    store.state.status = await storeClient.getStatus();

    // Corrective pass B: a stale preview must never survive a draft change.
    await store.openBuilder({});
    store.patchBuilderDraft({ name: "Stale Preview", target_ids: ["office"] });
    const firstPreview = await store.previewBuilderDraft();
    check(
      "builder stale: first preview succeeds and is presented as current",
      firstPreview.ok === true && !!store.state.builder.preview && store.state.builder.previewStale === false
    );
    store.patchBuilderDraft({ brightness: 33 });
    check(
      "builder stale: any actual draft change immediately invalidates the previous preview",
      store.state.builder.preview === null && store.state.builder.previewStale === true
    );
    const secondPreview = await store.previewBuilderDraft();
    check(
      "builder stale: re-preview produces a current server-authoritative result and clears staleness",
      secondPreview.ok === true && !!store.state.builder.preview && store.state.builder.previewStale === false
    );
    // Corrected input clears its own stale field error (corrective pass B).
    store.patchBuilderDraft({ name: "" });
    const nameErr = await store.saveBuilderDraft();
    check(
      "builder stale: a save blocked on the name attaches the error to scene.name",
      nameErr.ok === false && nameErr.error.details.path === "scene.name" && store.state.builder.error.path === "scene.name"
    );
    store.patchBuilderDraft({ name: "Stale Preview Fixed" });
    check(
      "builder stale: correcting the field clears its stale error (draft intact)",
      store.state.builder.error === null && store.state.builder.draft.name === "Stale Preview Fixed"
    );
    store.closeBuilder();

    // Corrective pass A: rename action wiring — the overflow Rename item
    // enters the row's LOCAL inline editor; persistence keeps using the
    // existing scene-action "rename" -> scene.rename command seam.
    {
      const rowSrc = readFileSync(join(here, "..", "src", "components", "ss-scene-row.js"), "utf8");
      const scenesSrc = readFileSync(join(here, "..", "src", "views", "scenes.js"), "utf8");
      check(
        "rename wiring: the overflow Rename item enters the row's local editor directly (no dead rename-start emission)",
        /key: "rename-start"[\s\S]*?this\.#startRename\(e\)/.test(rowSrc) && !/this\.#emit\("rename-start"\)/.test(rowSrc)
      );
      check(
        "rename wiring: submitted renames still route through scene.rename",
        scenesSrc.includes('command: "scene.rename"') && rowSrc.includes('this.#emit("rename"')
      );
      check(
        "rename wiring: Builder ENTRY gates on the observational preview_draft command, not scene.update",
        /edit: "scene\.preview_draft"/.test(rowSrc)
      );
    }

    await store.openBuilder({ sceneId: "twilight" });
    check(
      "builder: opening Twilight pins lights whose hex is already in the palette",
      store.state.builder.dirty === false &&
        store.state.builder.draft.fixture_states.lamp.palette_index === store.state.builder.draft.palette.indexOf("#ff8f00") &&
        store.state.builder.draft.fixture_states.lamp.color === undefined &&
        Array.isArray(store.state.builder.draft.fixture_states.g_strip.gradient),
      JSON.stringify(store.state.builder.draft.fixture_states.lamp)
    );
    store.closeBuilder();

    // Clean drafts are disposable: navigating away without edits closes silently.
    await store.openBuilder({});
    await store.setView("fixtures");
    check("builder guard: a CLEAN draft navigates away freely (disposable local state)", store.state.builder === null && store.state.view === "fixtures");

    // Advanced-field preservation flags (Pass 2 plan §7 → expansion §2/§3).
    const advancedFlags = describeAdvancedFields({
      schema_version: 2,
      id: "twilight",
      name: "Twilight",
      target_ids: ["office"],
      motion: { mode: "static", speed: 0.0, strategy: "auto" },
      fixture_states: { g_strip: { on: true }, wled_seg_0: { on: true, provider_ext: { wled: { fx: 10 } } } },
      default_state: { on: true, brightness: 50 },
      metadata: { origin: "migrated", migrated_from_v1: { filename: "twilight.json" } },
    });
    check(
      "builder §7: advanced-field detection covers advanced overrides, advanced defaults, and custom metadata",
      deepEqual(advancedFlags.overrideFixtures, ["g_strip", "wled_seg_0"]) &&
        deepEqual(advancedFlags.advancedOverrideFixtures, ["wled_seg_0"]) &&
        deepEqual(advancedFlags.advancedDefault, []) &&
        deepEqual(advancedFlags.customMetadata, ["origin"]) &&
        advancedFlags.any === true
    );
    const advancedDefaultFlags = describeAdvancedFields({
      schema_version: 2,
      id: "grad",
      name: "Grad",
      target_ids: ["office"],
      motion: { mode: "static", speed: 0.0, strategy: "auto" },
      default_state: { on: true, gradient: ["#111111", "#222222"] },
    });
    check(
      "builder §7: an advanced DEFAULT state (gradient) is flagged as preserved content",
      deepEqual(advancedDefaultFlags.advancedDefault, ["gradient"]) && advancedDefaultFlags.any === true
    );
    const provenanceOnly = describeAdvancedFields({
      schema_version: 2,
      id: "x",
      name: "X",
      target_ids: ["office"],
      motion: { mode: "static", speed: 0.0, strategy: "auto" },
      metadata: { migrated_from_v1: { filename: "x.json" } },
    });
    check(
      "builder §7: server-owned provenance alone is not flagged (it is never lost)",
      provenanceOnly.any === false
    );
  }

  // --- Builder expansion (targets / defaults / overrides / duplicate) -----
  console.log("\n== Builder expansion (targets, defaults, overrides, duplicate) ==");
  {
    const healthFixtures = baseData.registry.fixtures.map((f) => ({
      ...f,
      health: deriveHealth(f, {}).status,
    }));

    // §1 — target selection groups + readiness aggregation (existing health only).
    const officeMembers = healthFixtures.filter((f) => (f.groups || []).includes("office"));
    const officeReady = officeMembers.filter((f) => f.health === "ready").length;
    check(
      "builder §1: group readiness aggregates existing fixture health (no new health algorithm)",
      officeMembers.length > 0 &&
        targetReadiness(healthFixtures, "office", { isGroup: true }) === `${officeReady}/${officeMembers.length} ready`,
      targetReadiness(healthFixtures, "office", { isGroup: true })
    );
    check(
      "builder §1: individual-fixture readiness is that fixture's own derived health",
      targetReadiness(healthFixtures, "double_strip") === "disabled" &&
        targetReadiness(healthFixtures, "g_strip") === "ready",
      JSON.stringify({ double_strip: targetReadiness(healthFixtures, "double_strip"), g_strip: targetReadiness(healthFixtures, "g_strip") })
    );
    check(
      "builder §1: unknown targets report no fixtures instead of inventing one",
      targetReadiness(healthFixtures, "ghost_room", { isGroup: true }) === "no fixtures" &&
        targetReadiness(healthFixtures, "ghost_room") === ""
    );
    check(
      "builder §1: a declared target and a single fixture id resolve through the canonical rule",
      resolveTargetFixtures(healthFixtures, "office").length === officeMembers.length &&
        resolveTargetFixtures(healthFixtures, "lamp").length === 1
    );
    // Canonical precedence parity with renderers/plan.py: a DECLARED target id
    // is authoritative and never falls back to a same-named fixture, even when
    // that target currently has no member fixtures.
    const collidingIds = [
      { id: "office", name: "Fixture named like the target", groups: [] },
      { id: "g_strip", name: "G", groups: ["living_room"] },
    ];
    const exactResolution = resolveTargetFixtures(collidingIds, "office", { declaredTargetIds: ["office"] });
    const legacyResolution = resolveTargetFixtures(collidingIds, "office");
    check(
      "builder §1: a declared target id never falls back to a same-named fixture (renderer parity)",
      exactResolution.length === 0 &&
        legacyResolution.length === 1 &&
        legacyResolution[0].id === "office",
      JSON.stringify({ exact: exactResolution, legacy: legacyResolution })
    );

    // §7 — preview quality semantics are derived from the canonical render plan only.
    const planOf = (plans, skipped = [], notes = []) => ({
      scene_id: "x",
      target_ids: ["office"],
      fixture_plans: plans,
      skipped_fixture_ids: skipped,
      notes,
    });
    const clean = summarizePreviewQuality(
      planOf([
        { fixture_id: "a", provider: "hue_v2", fidelity: "native" },
        { fixture_id: "b", provider: "wled", fidelity: "equivalent" },
      ])
    );
    check(
      "builder §7: a clean plan is the ONLY case that earns an unqualified ready/ok headline",
      clean.quality === "ready" && clean.tone === "ok" && clean.planned === 2 && clean.issues.length === 0
    );
    const approximate = summarizePreviewQuality(
      planOf([{ fixture_id: "a", provider: "hue_v2", fidelity: "native" }, { fixture_id: "b", provider: "hue_v2", fidelity: "approximate", reason: "no gradient" }])
    );
    check(
      "builder §7: an approximate result downgrades the headline (never green 'valid')",
      approximate.quality === "reductions" && approximate.tone === "warn" && approximate.fidelity.approximate === 1
    );
    const unsupported = summarizePreviewQuality(
      planOf([{ fixture_id: "a", provider: "hue_v2", fidelity: "unsupported", reason: "no color" }])
    );
    check(
      "builder §7: an unsupported fixture reports partially unsupported",
      unsupported.quality === "partially_unsupported" && unsupported.tone === "bad"
    );
    const skippedOnly = summarizePreviewQuality(planOf([{ fixture_id: "a", provider: "hue_v2", fidelity: "native" }], ["double_strip"]));
    check(
      "builder §7: skipped fixtures prevent an unqualified ready headline",
      skippedOnly.quality === "reductions" && skippedOnly.skipped === 1
    );
    const notesOnly = summarizePreviewQuality(planOf([{ fixture_id: "a", provider: "hue_v2", fidelity: "native" }], [], ["target 'ghost' did not resolve"]));
    check("builder §7: renderer notes also prevent an unqualified ready headline", notesOnly.quality === "reductions");
    const empty = summarizePreviewQuality(planOf([], [], ["no state"]));
    check(
      "builder §7: nothing renderable reports cannot_render (never zeros as a valid result)",
      empty.quality === "cannot_render" && empty.tone === "bad" && empty.planned === 0
    );
    check(
      "builder §7: quality is a UI summary over the existing fidelity levels (no new enum)",
      ["native", "equivalent", "approximate", "unsupported"].every((k) => k in clean.fidelity)
    );

    // §2/§3 — scene defaults + per-fixture overrides through the store.
    const stateClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const stateStore = createStore(stateClient);
    await stateStore.init();
    await stateStore.openBuilder({});
    stateStore.patchBuilderDraft({ name: "Defaults Probe", target_ids: ["office"] });
    stateStore.patchBuilderDefaultState({ brightness: 40, color: "#112233", color_temp_mirek: 300, on: false });
    check(
      "builder §2: defaults expose on/off (explicit off), brightness, color, and color temp as canonical fields",
      deepEqual(stateStore.state.builder.draft.default_state, { on: false, brightness: 40, color: "#112233", color_temp_mirek: 300 }),
      JSON.stringify(stateStore.state.builder.draft.default_state)
    );
    stateStore.patchBuilderDefaultState({ color: null, color_temp_mirek: null, brightness: null, on: null });
    check(
      "builder §2: unsetting every default drops default_state entirely (never null fields)",
      stateStore.state.builder.draft.default_state === null,
      JSON.stringify(stateStore.state.builder.draft.default_state)
    );

    stateStore.patchBuilderDefaultState({ on: true, brightness: 55, color: "#abcdef" });
    stateStore.patchBuilderOverride("g_strip", { brightness: 55 });
    check(
      "builder: customizing a field writes only that field, without a color/palette pin",
      deepEqual(stateStore.state.builder.draft.fixture_states.g_strip, { brightness: 55 }),
      JSON.stringify(stateStore.state.builder.draft.fixture_states.g_strip)
    );
    stateStore.patchBuilderOverride("g_strip", { brightness: 33, color: null, on: false });
    check(
      "builder §3: editing an override changes only the patched fields and can unset one field",
      deepEqual(stateStore.state.builder.draft.fixture_states.g_strip, { on: false, brightness: 33 }),
      JSON.stringify(stateStore.state.builder.draft.fixture_states.g_strip)
    );
    stateStore.removeBuilderOverride("g_strip");
    check(
      "builder §3: removing an override is explicit and returns the fixture to the defaults",
      stateStore.state.builder.draft.fixture_states.g_strip === undefined
    );
    stateStore.patchBuilderOverride('g_strip', { palette_index: 2, brightness: 37, gradient: ['#123456', '#abcdef'], effect: 'sparkle', provider_ext: { hue_v2: { sample: true } } });
    stateStore.resetBuilderFixtureDetails('g_strip');
    check('palette details: reset retains an independent palette pin', deepEqual(stateStore.state.builder.draft.fixture_states.g_strip, { palette_index: 2 }));
    stateStore.patchBuilderOverride('g_strip', { color: '#123456', brightness: 25 });
    stateStore.resetBuilderFixtureDetails('g_strip');
    check('palette details: reset retains an explicit custom color', deepEqual(stateStore.state.builder.draft.fixture_states.g_strip, { color: '#123456' }));
    stateStore.removeBuilderOverride('g_strip');
    stateStore.patchBuilderOverride('g_strip', { brightness: 40 });
    check('palette details: unrelated customization does not create a palette pin', stateStore.state.builder.draft.fixture_states.g_strip.palette_index === undefined && stateStore.state.builder.draft.fixture_states.g_strip.color === undefined);
    stateStore.resetBuilderFixtureDetails('g_strip');
    check('palette details: resetting automatic customization returns to no explicit state', stateStore.state.builder.draft.fixture_states.g_strip === undefined);

    // §3 — advanced fields survive a supported-field edit (real advanced doc).
    const advStore = createStore(createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" }));
    await advStore.init();
    await advStore.openBuilder({ sceneId: "twilight" });
    const beforeGradient = [...advStore.state.builder.draft.fixture_states.g_strip.gradient];
    advStore.patchBuilderOverride("g_strip", { brightness: 37 });
    const advAfter = advStore.state.builder.draft.fixture_states.g_strip;
    check(
      "builder §3: editing one supported field preserves the fixture's advanced fields (gradient + transition)",
      deepEqual(advAfter.gradient, beforeGradient) &&
        advAfter.transition_ms === 400 &&
        advAfter.brightness === 37,
      JSON.stringify(advAfter)
    );
    check(
      "builder §3: advanced fixture content is surfaced for warning, not hidden",
      deepEqual(describeFixtureState(advAfter).advanced, ["gradient", "transition"])
    );
    // §5 — an unrelated edit preserves advanced motion, and the
    // "preserved" notice follows the DRAFT rather than the frozen original.
    advStore.state.builder.draft.motion = { mode: "effect", speed: 0.3, strategy: "native_preferred" };
    advStore.patchBuilderDefaultState({ brightness: 12 });
    check(
      "builder §5: an unrelated edit preserves advanced motion (mode/strategy untouched)",
      deepEqual(advStore.state.builder.draft.motion, { mode: "effect", speed: 0.3, strategy: "native_preferred" })
    );
    check(
      "builder §5+§7: the preserved-content flags track the DRAFT (advanced motion is flagged while it is present)",
      describeAdvancedFields(advStore.state.builder.draft).advancedMotionMode === "effect"
    );
    advStore.patchBuilderDraft({ motion: { mode: "static", speed: 0.0, strategy: "auto" } });
    check(
      "builder §5+§7: explicitly choosing Static clears the preserved-advanced-motion flag",
      describeAdvancedFields(advStore.state.builder.draft).advancedMotionMode === null
    );
    // Removing an advanced override must stop flagging that fixture, and the
    // flags must be read from the DRAFT (not a frozen copy of the original).
    const advFlagsBeforeRemoval = describeAdvancedFields(advStore.state.builder.draft);
    advStore.removeBuilderOverride("g_strip");
    const advFlagsAfterRemoval = describeAdvancedFields(advStore.state.builder.draft);
    check(
      "builder §3+§7: a removed override stops being flagged as preserved advanced content",
      advFlagsBeforeRemoval.advancedOverrideFixtures.includes("g_strip") &&
        !advFlagsAfterRemoval.advancedOverrideFixtures.includes("g_strip") &&
        advFlagsAfterRemoval.overrideFixtures.length === advFlagsBeforeRemoval.overrideFixtures.length - 1,
      JSON.stringify({ before: advFlagsBeforeRemoval, after: advFlagsAfterRemoval })
    );

    // §4 — duplicate draft construction + server-derived provenance.
    const dupClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const twilightDoc = (await dupClient.getScenes()).scenes.find((s) => s.id === "twilight");
    const dupDraft = duplicateBuilderDraft(twilightDoc);
    check(
      "builder §4: a duplicate draft drops source identity and server/history provenance but keeps intent",
      dupDraft.id === undefined &&
        dupDraft.name === "Twilight Copy" &&
        dupDraft.metadata.origin === "migrated" &&
        !dupDraft.metadata.migrated_from_v1 &&
        Object.keys(dupDraft.fixture_states).length === 12 &&
        dupDraft.fixture_states.g_strip.gradient.length === 4,
      JSON.stringify({ name: dupDraft.name, metadata: dupDraft.metadata })
    );
    const dupCreated = await dupClient.sendCommand({ command: "scene.create", duplicate_of: "twilight", scene: dupDraft });
    check(
      "builder §4 mock: scene.create with duplicate_of records SERVER-derived provenance",
      dupCreated.ok === true &&
        dupCreated.data.scene.id === "twilight_copy" &&
        dupCreated.data.scene.metadata.duplicated_from === "twilight" &&
        !dupCreated.data.scene.metadata.migrated_from_v1,
      JSON.stringify(dupCreated).slice(0, 240)
    );
    const dupOriginal = (await dupClient.getScenes()).scenes.find((s) => s.id === "twilight");
    check(
      "builder §4 mock: the source scene is untouched by a duplicate",
      dupOriginal.name === "Twilight" &&
        !dupOriginal.metadata.duplicated_from &&
        !!dupOriginal.metadata.migrated_from_v1 &&
        dupOriginal.fixture_states.g_strip.gradient.length === 4
    );
    const dupUnknown = await dupClient.sendCommand({ command: "scene.create", duplicate_of: "ghost_scene", scene: { ...dupDraft, name: "Ghost Copy" } });
    check(
      "builder §4 mock: an unknown duplicate_of is not_found (no silent create)",
      dupUnknown.ok === false && dupUnknown.error.code === "not_found"
    );
    const dupCollision = await dupClient.sendCommand({ command: "scene.create", duplicate_of: "twilight", scene: { ...dupDraft, name: "Twilight" } });
    check(
      "builder §4 mock: a duplicate whose derived id collides reports normal conflict handling",
      dupCollision.ok === false && dupCollision.error.code === "conflict"
    );

    // §8 — backend-authoritative catalog fidelity (no goldens as production truth).
    const fidClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    let previewCalls = 0;
    const fidOriginalSend = fidClient.sendCommand.bind(fidClient);
    fidClient.sendCommand = async (envelope) => {
      if (envelope.command === "scene.preview") previewCalls += 1;
      return fidOriginalSend(envelope);
    };
    const fidStore = createStore(fidClient);
    await fidStore.init();
    const waitFid = async (ms = 4000) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        if (Object.keys(fidStore.state.sceneFidelity).length > 0) return true;
        await new Promise((r) => setTimeout(r, 20));
      }
      return false;
    };
    await waitFid();
    check(
      "builder §8: catalog rows are backed by backend scene.preview summaries (not sample goldens)",
      fidStore.sceneFidelityFor("twilight") &&
        fidStore.sceneFidelityFor("twilight").status === "ok" &&
        fidStore.sceneFidelityFor("twilight").planned > 0 &&
        fidStore.sceneFidelityFor("twilight").fidelity.native >= 1
    );
    check(
      "builder §8: archived scenes are not previewed for row fidelity",
      !fidStore.sceneFidelityFor("meeting_blue")
    );
    const callsAfterInitial = previewCalls;
    await new Promise((r) => setTimeout(r, 30));
    await fidStore.refreshSceneFidelity();
    check(
      "builder §8: an unchanged engine revision does NOT re-preview every scene",
      previewCalls === callsAfterInitial,
      JSON.stringify({ callsAfterInitial, previewCalls })
    );
    fidStore.state.status.engine.revision = (fidStore.state.status.engine.revision || 0) + 1;
    await fidStore.refreshSceneFidelity();
    check(
      "builder §8: an engine revision change refreshes row fidelity (bounded, explicit trigger)",
      previewCalls > callsAfterInitial
    );
    // A failing preview must render honestly, never as zeros.
    const failClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const failOriginalSend = failClient.sendCommand.bind(failClient);
    failClient.sendCommand = async (envelope) =>
      envelope.command === "scene.preview"
        ? { command: "scene.preview", ok: false, error: { code: "internal_error", message: "preview boom" } }
        : failOriginalSend(envelope);
    const failStore = createStore(failClient);
    await failStore.init();
    const waitFail = async (ms = 4000) => {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) {
        if (Object.keys(failStore.state.sceneFidelity).length > 0) return true;
        await new Promise((r) => setTimeout(r, 20));
      }
      return false;
    };
    await waitFail();
    check(
      "builder §8: a preview failure reports 'unavailable' honestly (never zeros as valid fidelity)",
      failStore.sceneFidelityFor("twilight") &&
        failStore.sceneFidelityFor("twilight").status === "unavailable" &&
        /boom/.test(failStore.sceneFidelityFor("twilight").reason),
      JSON.stringify(failStore.sceneFidelityFor("twilight"))
    );
    // A FORCED refresh must not be silently dropped when it lands mid-flight
    // (the in-flight pass may hold an older catalog, e.g. right after a save).
    const forceClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    let forcePreviewCalls = 0;
    const forceOriginalSend = forceClient.sendCommand.bind(forceClient);
    forceClient.sendCommand = async (envelope) => {
      if (envelope.command === "scene.preview") {
        forcePreviewCalls += 1;
        await new Promise((r) => setTimeout(r, 15));
      }
      return forceOriginalSend(envelope);
    };
    const forceStore = createStore(forceClient);
    await forceStore.init();
    await new Promise((r) => setTimeout(r, 5));
    const forceCallsBefore = forcePreviewCalls;
    await Promise.all([
      forceStore.refreshSceneFidelity({ force: true }),
      forceStore.refreshSceneFidelity({ force: true }),
    ]);
    await new Promise((r) => setTimeout(r, 600));
    check(
      "builder §8: a forced fidelity refresh that arrives mid-flight is re-run, not dropped",
      forcePreviewCalls > forceCallsBefore,
      JSON.stringify({ forceCallsBefore, forcePreviewCalls })
    );
    // A FAILED preview must not leave the previous server plan on screen as if
    // it still described the current draft (bypasses the edit-invalidation
    // path deliberately, simulating a draft that only becomes invalid later).
    const stalePreviewStore = createStore(createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" }));
    await stalePreviewStore.init();
    await stalePreviewStore.openBuilder({});
    stalePreviewStore.patchBuilderDraft({ name: "Stale Preview Probe", target_ids: ["office"] });
    const goodPreview = await stalePreviewStore.previewBuilderDraft();
    const hadPreview = !!stalePreviewStore.state.builder.preview;
    stalePreviewStore.state.builder.draft.default_state = { on: true, color: "#zzz" };
    const badPreview = await stalePreviewStore.previewBuilderDraft();
    check(
      "builder §7: a failed preview clears the previous server preview (no stale 'ready' panel)",
      hadPreview &&
        goodPreview.ok === true &&
        badPreview.ok === false &&
        stalePreviewStore.state.builder.preview === null &&
        stalePreviewStore.state.builder.previewError.path === "scene.default_state.color",
      JSON.stringify({
        hadPreview,
        badOk: badPreview.ok,
        preview: stalePreviewStore.state.builder.preview,
        error: stalePreviewStore.state.builder.previewError,
      })
    );
  }

  // --- revision-cache decision (pure, plan §9.4 skip logic) -------------
  console.log("\n== revision cache (shouldRefetchCatalogs) ==");
  check("same revision -> skip heavy refetch", shouldRefetchCatalogs(7, 7) === false);
  check("changed revision -> refetch", shouldRefetchCatalogs(7, 8) === true);
  check("missing previous revision -> refetch (safe default)", shouldRefetchCatalogs(undefined, 0) === true);
  check("non-numeric revision -> refetch (safe default)", shouldRefetchCatalogs(null, null) === true);

  // --- live-state freshness classification (pure, plan §5) --------------
  console.log("\n== live-state freshness (liveStateFreshness) ==");
  const t0 = Date.parse("2026-09-16T12:00:00Z");
  check("fresh at 0s", liveStateFreshness("2026-09-16T12:00:00Z", t0) === "fresh");
  check("fresh at the boundary", liveStateFreshness("2026-09-16T12:00:00Z", t0 + LIVE_STATE_FRESH_MS) === "fresh");
  check("aging just past the fresh boundary", liveStateFreshness("2026-09-16T12:00:00Z", t0 + LIVE_STATE_FRESH_MS + 1) === "aging");
  check("aging at its boundary", liveStateFreshness("2026-09-16T12:00:00Z", t0 + LIVE_STATE_AGING_MS) === "aging");
  check("stale just past the aging boundary", liveStateFreshness("2026-09-16T12:00:00Z", t0 + LIVE_STATE_AGING_MS + 1) === "stale");
  check("stale far in the past", liveStateFreshness("2026-09-16T12:00:00Z", t0 + 60000) === "stale");
  check("missing sampledAt is unavailable, never fresh", liveStateFreshness(null, t0) === "unavailable");
  check("unparseable sampledAt is unavailable, never fresh", liveStateFreshness("not-a-date", t0) === "unavailable");
  check("future timestamp (clock skew) reads fresh, not penalized", liveStateFreshness("2026-09-16T12:00:05Z", t0) === "fresh");

  // --- scene.apply honesty: partial provider-receipt failures surface ---
  // (reliability pass: a real GLEDOPTO light on the live bridge fails
  // read-back confirmation with error_code "communication_error" — the
  // engine reports that per-fixture in `data.receipts`; the Workbench must
  // not paper over it with a blanket green "Applied".)
  console.log("\n== scene.apply notice honesty (partial receipt failure) ==");
  {
    const receiptClient = createMockSceneStudioClient(baseData, { scenarioId: "all-healthy" });
    const originalSend = receiptClient.sendCommand.bind(receiptClient);
    receiptClient.sendCommand = async (envelope) =>
      envelope.command === "scene.apply" && !envelope.dry_run
        ? {
            command: "scene.apply",
            ok: true,
            data: {
              scene_name: "Twilight",
              scene_id: "twilight",
              skipped_fixture_ids: [],
              receipts: [
                { ok: true, provider: "hue_v2", op: "hue.put_light", fixture_id: "g_strip" },
                { ok: false, provider: "hue_v2", op: "hue.put_light", fixture_id: "custom_gradient", detail: "communication_error" },
              ],
            },
          }
        : originalSend(envelope);
    const receiptStore = createStore(receiptClient);
    await receiptStore.init();
    await receiptStore.sendCommand({ command: "scene.apply", scene_id: "twilight" });
    const notice = receiptStore.state.notice;
    check(
      "scene.apply: a failed receipt is named in the notice text, not hidden behind a blanket 'Applied'",
      !!notice && /Custom Gradient/.test(notice.text) && /didn't confirm/.test(notice.text),
      JSON.stringify(notice)
    );
    check(
      "scene.apply: the notice tone is 'warn', not a false green 'ok', when a fixture didn't confirm",
      !!notice && notice.level === "warn",
      JSON.stringify(notice)
    );

    // A clean apply (every receipt ok) must still read as a normal success.
    receiptClient.sendCommand = async (envelope) =>
      envelope.command === "scene.apply" && !envelope.dry_run
        ? {
            command: "scene.apply",
            ok: true,
            data: {
              scene_name: "Twilight",
              scene_id: "twilight",
              skipped_fixture_ids: [],
              receipts: [{ ok: true, provider: "hue_v2", op: "hue.put_light", fixture_id: "g_strip" }],
            },
          }
        : originalSend(envelope);
    await receiptStore.sendCommand({ command: "scene.apply", scene_id: "twilight" });
    const cleanNotice = receiptStore.state.notice;
    check(
      "scene.apply: every receipt ok -> the notice stays a plain 'ok' success",
      !!cleanNotice && cleanNotice.level === "ok" && !/didn't confirm/.test(cleanNotice.text),
      JSON.stringify(cleanNotice)
    );
  }

  // --- fixture row aura (pure, plan §6) ----------------------------------
  console.log("\n== fixture row aura (auraBackground) ==");
  const onRgb = { enabled: true, available: true, on: true, brightness: 60, displayColors: ["#ff0000"], freshness: "fresh" };
  check("on + fresh + a color -> an aura is drawn", typeof auraBackground(onRgb) === "string" && auraBackground(onRgb).includes("radial-gradient"));
  check("off -> no aura", auraBackground({ ...onRgb, on: false }) === null);
  check("on but no color reported -> no aura (nothing to draw honestly)", auraBackground({ ...onRgb, displayColors: [] }) === null);
  check("registry-disabled -> no aura regardless of a stale live payload", auraBackground({ ...onRgb, enabled: false }) === null);
  check("provider-unavailable -> no aura", auraBackground({ ...onRgb, available: false }) === null);
  check("stale sample -> no aura (never shows an old color as current)", auraBackground({ ...onRgb, freshness: "stale" }) === null);
  check("unavailable freshness -> no aura", auraBackground({ ...onRgb, freshness: "unavailable" }) === null);

  const gradientAura = auraBackground({ ...onRgb, displayColors: ["#ff0000", "#00ff00", "#0000ff"] });
  check("3-color gradient -> 3 overlapping lobes, not 1", (gradientAura.match(/radial-gradient/g) || []).length === 3);
  const soloAura = auraBackground({ ...onRgb, displayColors: ["#ff0000"] });
  check(
    "a single-color fixture's aura reaches exactly as far as a gradient's (uniform footprint, not 'fewer colors = shorter reach')",
    (soloAura.match(/radial-gradient/g) || []).length === (gradientAura.match(/radial-gradient/g) || []).length,
    JSON.stringify({ soloAura, gradientAura })
  );
  const cappedAura = auraBackground({ ...onRgb, displayColors: ["#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff"] });
  check("5-color gradient is capped, not one noisy aura per stop", (cappedAura.match(/radial-gradient/g) || []).length === 3);

  const dimAlpha = (bg) => Number(/rgba\([^)]*,\s*([\d.]+)\)/.exec(bg)[1]);
  const veryDim = dimAlpha(auraBackground({ ...onRgb, brightness: 1 }));
  const ordinary = dimAlpha(auraBackground({ ...onRgb, brightness: 50 }));
  const full = dimAlpha(auraBackground({ ...onRgb, brightness: 100 }));
  check("very dim but on is still subtly visible, not invisible", veryDim > 0.05);
  check("full brightness reads only moderately stronger than ordinary, not overpowering", full < ordinary * 2 && full > ordinary);
  check("aging freshness visibly reduces intensity vs fresh", dimAlpha(auraBackground({ ...onRgb, freshness: "aging" })) < dimAlpha(auraBackground(onRgb)));

  const missingBrightness = auraBackground({ ...onRgb, brightness: null });
  check("missing brightness still draws a moderate aura, not a crash/blank", typeof missingBrightness === "string");

  // --- live HTTP client against the real engine (devserver) -------------
  console.log("\n== live HTTP client (scene_studio.devserver) ==");
  await runLiveChecks();

  // --- appdaemon named-endpoint transport (envelope POST -> {status, body})
  await runAppDaemonTransportChecks();

  // Explicit update checks never run on init/poll and keep failures local.
  const updateClient = createMockSceneStudioClient(baseData);
  let checkCalls = 0;
  let resolveCheck;
  updateClient.checkUpdates = () => {
    checkCalls++;
    return new Promise(resolve => { resolveCheck = resolve; });
  };
  const updateStore = createStore(updateClient);
  await updateStore.init();
  await updateStore.poll();
  check('updates: init and polling never query release source', checkCalls === 0);
  const checking = updateStore.checkUpdates();
  check('updates: checking state is immediate', updateStore.state.updateCheck.state === 'checking');
  await updateStore.checkUpdates();
  check('updates: duplicate in-flight checks suppressed', checkCalls === 1);
  resolveCheck({ state: 'available', latest_version: '0.2.0' });
  await checking;
  check('updates: result reaches store', updateStore.state.updateCheck.state === 'available');
  updateClient.checkUpdates = async () => { throw new Error('synthetic-private-access-marker'); };
  await updateStore.checkUpdates();
  check('updates: failure does not leak exception detail', updateStore.state.updateCheck.state === 'error' && !JSON.stringify(updateStore.state.updateCheck).includes('synthetic-private-access-marker'));

  const { followUpdate, updateReloadUrl } = await import('../src/update_execution.js');
  const oldStatic = {version:'0.1.0',source_sha:'old',source_tree_sha256:'old-tree'};
  const newStatic = {version:'0.1.1',source_sha:'new',source_tree_sha256:'new-tree'};
  const updated = {conn:{mode:'live',url:''},status:{engine:{ok:true},product:{build:newStatic}},
    updateExecution:{state:'succeeded',target_version:'0.1.1'}};
  const page = 'http://runtime.example.test/scene_studio/?keep=yes#scene';
  const reload = updateReloadUrl(updated, oldStatic, page);
  check('updates: verified success reloads the new static bundle with a fresh URL',
    reload?.includes('_scene_studio_build=0.1.1-new-tree') && reload.includes('keep=yes') && reload.endsWith('#scene'));
  check('updates: new bundle and an already-attempted reload do not loop',
    updateReloadUrl(updated, newStatic, page) === null && updateReloadUrl(updated, oldStatic, reload) === null);
  check('updates: dirty drafts defer static reload',
    updateReloadUrl({...updated,builder:{dirty:true}}, oldStatic, page) === null);
  check('updates: local mock/remote developer pages never reload from a live update',
    updateReloadUrl({...updated,conn:{mode:'mock'}}, oldStatic, page) === null &&
    updateReloadUrl({...updated,conn:{mode:'live',url:'http://other.example.test'}}, oldStatic, page) === null);
  check('updates: rollback and unverified new identity never reload static files',
    updateReloadUrl({...updated,updateExecution:{state:'failed',rolled_back:true}}, oldStatic, page) === null &&
    updateReloadUrl({...updated,status:{engine:{ok:true},product:{build:oldStatic}}}, oldStatic, page) === null);
  let tick = 0;
  const fast = { timeout: 20, interval: 1, now: () => tick, sleep: async () => { tick++; } };
  let phases = ['downloading', 'verifying', 'activating', 'restarting', 'disconnect', 'verifying_new_build', 'succeeded'];
  const progress = [];
  const execution = {
    getUpdateStatus: async () => {
      const state = phases.shift() || 'succeeded';
      if (state === 'disconnect') throw new Error('synthetic-private-access-marker');
      return { state, target_version: '0.1.1', installed_version: '0.1.0' };
    },
    getStatus: async () => ({ engine: { ok: true }, product: { build: { version: '0.1.1' } } }),
  };
  await followUpdate(execution, '0.1.1', s => progress.push(s), fast);
  check('updates: disconnect is reconnecting progress followed by exact NEW build success',
    progress.some(s => s.state === 'reconnecting') && progress.at(-1).state === 'succeeded' && !JSON.stringify(progress).includes('synthetic-private-access-marker'));
  execution.getStatus = async () => ({ engine: { ok: true }, product: { build: { version: '0.1.0' } } });
  const rolled = [];
  await followUpdate(execution, '0.1.1', s => rolled.push(s), fast);
  check('updates: healthy OLD build is rollback, never success', rolled.at(-1).rolled_back === true && rolled.at(-1).state === 'failed');
  execution.getStatus = async () => ({ engine: { ok: false }, product: { build: { version: '0.1.1' } } });
  const timed = [];
  await followUpdate(execution, '0.1.1', s => timed.push(s), fast);
  check('updates: unhealthy NEW build times out with explicit recovery state', timed.at(-1).recovery_required && timed.at(-1).message.includes('timed out'));

  let starts = 0, resolveStart;
  updateClient.startUpdate = target => { starts++; check('updates: browser sends only target identity', target === '0.1.1'); return new Promise(resolve => { resolveStart = resolve; }); };
  updateClient.getUpdateStatus = async () => ({ state: 'failed', rolled_back: true, message: 'Previous build restored.' });
  updateStore.state.updateCheck = { state: 'available', latest_version: '0.1.1' };
  updateStore.reviewUpdate();
  check('updates: review shows installed/target and causes no execution', starts === 0 && updateStore.state.updateConfirmation.target === '0.1.1');
  const applying = updateStore.confirmUpdate();
  await updateStore.confirmUpdate();
  check('updates: repeated confirmation starts exactly one transaction', starts === 1);
  resolveStart({ state: 'restarting', target_version: '0.1.1' });
  await applying;
  check('updates: server rollback is preserved by store', updateStore.state.updateExecution.rolled_back === true);

  const savedFetch = globalThis.fetch;
  const updateWire = [];
  try {
    globalThis.fetch = async (url, options) => {
      updateWire.push({url, body: JSON.parse(options.body)});
      return { ok: true, json: async () => ({ status: 202, body: {state:'downloading'} }) };
    };
    const adminClient = createHttpSceneStudioClient('http://runtime.example.test', {transport:'appdaemon'});
    await adminClient.startUpdate('0.1.1');
    check('updates: administration uses independent endpoint and version-only RPC body', updateWire[0].url.endsWith('/api/appdaemon/scene_studio_update_api') && JSON.stringify(updateWire[0].body) === JSON.stringify({method:'POST',path:'/update',body:{target_version:'0.1.1'}}));
    globalThis.fetch = async () => ({ok:false, status:404, json: async () => { throw new Error('synthetic-private-access-marker'); }});
    let missingExecutor;
    try { await adminClient.startUpdate('0.1.1'); } catch (error) { missingExecutor = error; }
    check('updates: missing companion is actionable rejection, without parsing upstream HTML/secrets', missingExecutor?.rejected && missingExecutor.message.includes('companion') && !missingExecutor.message.includes('private-access-marker'));
  } finally { globalThis.fetch = savedFetch; }

  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

// ---------------------------------------------------------------------------
// live checks: real `scene_studio.devserver` preferred, canned stub fallback
// ---------------------------------------------------------------------------

const BACKEND_DIR = join(here, "..", "..", "backend");
const READY_TIMEOUT_MS = 20000;

function pickPort() {
  return 20000 + Math.floor(Math.random() * 20000);
}

function spawnDevserver(port, storeDir) {
  const child = spawn(
    process.platform === "win32" ? "python" : "python3",
    [
      "devserver.py",
      "--seed-demo",
      "--store", storeDir,
      "--port", String(port),
      "--shutdown-after-seconds", "120",
    ],
    { cwd: BACKEND_DIR, stdio: ["ignore", "pipe", "pipe"] }
  );
  let stderrTail = "";
  let spawnError = null;
  child.stderr.on("data", (d) => {
    stderrTail = (stderrTail + d.toString()).slice(-2000);
  });
  child.on("error", (err) => {
    spawnError = err;
  });
  return { child, aborted: () => spawnError, stderrTail: () => stderrTail };
}

async function waitReady(baseUrl, aborted) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (aborted && aborted()) return false;
    try {
      const res = await fetch(baseUrl + "/api/scene_studio/status", { signal: AbortSignal.timeout(1500) });
      if (res.ok) return true;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

/** Tiny in-node http server with canned engine-shaped responses (fallback). */
async function startStubBackend() {
  const scenes = [
    {
      schema_version: 2, id: "twilight", name: "Twilight", target_ids: ["office"],
      palette: [], motion: { mode: "static", speed: 0, strategy: "auto" }, fixture_states: {}, metadata: {},
    },
    {
      schema_version: 2, id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"],
      palette: [], motion: { mode: "palette_cycle", speed: 0.5, strategy: "auto" }, fixture_states: {}, metadata: {},
    },
  ];
  let revision = 3;
  const statusPayload = () => ({
    engine: { ok: true, revision, event_capacity: 500, events: 2 },
    fixtures: { total: 15, ready: 12, missing: 1, disabled: 1, unbound: 0, degraded: 0, conflicting: 0 },
    providers: {
      hue_v2: { total: 8, ready: 8, missing: 0, degraded: 0, other: 0 },
      wled: { total: 6, ready: 4, missing: 1, degraded: 0, other: 1 },
      ha_light: { total: 1, ready: 0, missing: 0, degraded: 0, other: 1 },
    },
    current: null,
    playback: null,
    last_discovery: null,
  });
  const errPayload = (code, message) => ({ command: null, ok: false, error: { code, message } });

  const server = httpCreateServer((req, res) => {
    const send = (status, payload) => {
      const body = JSON.stringify(payload);
      res.writeHead(status, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
      });
      res.end(body);
    };
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/api/scene_studio/status" && req.method === "GET") return send(200, statusPayload());
    if (url.pathname === "/api/scene_studio/fixtures" && req.method === "GET") {
      return send(200, { fixtures: [], targets: [] });
    }
    if (url.pathname === "/api/scene_studio/scenes" && req.method === "GET") return send(200, { scenes });
    if (url.pathname === "/api/scene_studio/discovery" && req.method === "GET") return send(200, { report: null });
    if (url.pathname === "/api/scene_studio/diagnostics/recent" && req.method === "GET") {
      return send(200, { events: [], count: 0 });
    }
    if (url.pathname === "/api/scene_studio/command" && req.method === "POST") {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        let envelope = null;
        try {
          envelope = JSON.parse(raw);
        } catch {
          envelope = null;
        }
        if (!envelope || typeof envelope !== "object") {
          return send(400, errPayload("validation_error", "body must be a JSON object"));
        }
        if (envelope.command === "scene.rename") {
          const scene = scenes.find((s) => s.id === envelope.scene_id);
          if (!scene) {
            return send(200, {
              command: "scene.rename", ok: false,
              error: { code: "not_found", message: `scene '${envelope.scene_id}' not found` },
            });
          }
          scene.name = envelope.name;
          revision += 1;
          return send(200, {
            command: "scene.rename", ok: true, request_id: envelope.request_id,
            data: { scene: { ...scene } },
          });
        }
        return send(200, {
          command: envelope.command ?? null, ok: false,
          error: { code: "unknown_command", message: "stub backend" },
        });
      });
      return;
    }
    send(404, errPayload("not_found", `no route for ${req.method} ${url.pathname}`));
  });
  const port = pickPort();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  console.log(`  ..  stub backend ready on ${baseUrl} (canned engine shapes)`);
  return { kind: "stub", baseUrl, close: () => new Promise((r) => server.close(r)) };
}

async function startLiveBackend() {
  const storeDir = mkdtempSync(join(tmpdir(), "ss-smoke-store-"));
  const port = pickPort();
  const baseUrl = `http://127.0.0.1:${port}`;
  let spawned = null;
  try {
    spawned = spawnDevserver(port, storeDir);
  } catch (err) {
    console.log(`  ..  python spawn failed (${err.message}); using in-node stub`);
  }
  if (spawned) {
    const ready = await waitReady(baseUrl, spawned.aborted);
    if (ready) {
      console.log(`  ..  devserver ready on ${baseUrl} (real engine)`);
      return {
        kind: "real",
        baseUrl,
        async close() {
          spawned.child.kill();
          await new Promise((r) => {
            spawned.child.on("exit", r);
            setTimeout(r, 3000);
          });
          try {
            rmSync(storeDir, { recursive: true, force: true });
          } catch {
            // best-effort temp cleanup
          }
        },
      };
    }
    spawned.child.kill();
    const tail = spawned.stderrTail();
    console.log(
      `  ..  devserver not ready in time; using in-node stub${tail ? ` (stderr tail: ${tail.slice(-400)})` : ""}`
    );
    try {
      rmSync(storeDir, { recursive: true, force: true });
    } catch {
      // best-effort temp cleanup
    }
  }
  return startStubBackend();
}

async function runLiveChecks() {
  const backend = await startLiveBackend();
  const client = createHttpSceneStudioClient(backend.baseUrl);
  try {
    check("live: client mode marker is 'live'", client.mode === "live");

    const status = await client.getStatus();
    check("live: status fetch ok", !!status && status.engine.ok === true);
    check(
      "live: status is engine-shaped (stable keys)",
      ["engine", "fixtures", "providers", "current", "playback", "last_discovery"].every((k) => k in status) &&
        typeof status.engine.revision === "number" &&
        ["total", "ready", "missing", "disabled", "unbound", "degraded", "conflicting"].every(
          (k) => typeof status.fixtures[k] === "number"
        ),
      JSON.stringify(Object.keys(status || {}))
    );
    check(
      "live: provider_links derived from providers dict",
      Array.isArray(status.provider_links) &&
        status.provider_links.length >= 1 &&
        status.provider_links.every((p) => p.connected === true && typeof p.label === "string")
    );

    const fixturesDoc = await client.getFixtures();
    check(
      `live: fixtures doc has ${liveExpectedFixtureCount} fixtures with derived health`,
      fixturesDoc.fixtures.length === liveExpectedFixtureCount &&
        fixturesDoc.fixtures.every((f) => HEALTH_STATUSES.includes(f.health)),
      `got ${fixturesDoc.fixtures.length}, expected ${liveExpectedFixtureCount}`
    );
    const derived = {};
    for (const f of fixturesDoc.fixtures) derived[f.health] = (derived[f.health] || 0) + 1;
    check(
      "live: derived fixture health matches engine status counts",
      ["ready", "missing", "disabled", "degraded", "unbound"].every(
        (k) => (derived[k] || 0) === status.fixtures[k]
      ),
      JSON.stringify({ derived, statusCounts: status.fixtures })
    );

    const scenesDoc = await client.getScenes();
    check(
      "live: 3 sample scenes via HTTP",
      scenesDoc.scenes.length === 3 && scenesDoc.scenes.some((s) => s.id === "twilight")
    );

    const discovery = await client.getDiscovery();
    check("live: discovery empty state is null (no report yet)", discovery === null);

    const events = await client.getRecentEvents(10);
    check("live: recent events envelope", Array.isArray(events.events));

    // scene.rename round trip over HTTP
    const revisionBefore = status.engine.revision;
    const renamed = await client.sendCommand({
      command: "scene.rename",
      scene_id: "twilight",
      name: "Twilight Live",
      request_id: "req-live-rename",
    });
    check("live: scene.rename ok over HTTP", renamed.ok === true, JSON.stringify(renamed).slice(0, 200));
    check("live: request_id echoed over HTTP", renamed.request_id === "req-live-rename");
    const scenesAfter = await client.getScenes();
    check(
      "live: rename persisted (name changed, id stable)",
      scenesAfter.scenes.find((s) => s.id === "twilight").name === "Twilight Live"
    );
    const statusAfter = await client.getStatus();
    check("live: revision bumped by rename", statusAfter.engine.revision === revisionBefore + 1);

    // Dry-run scene.apply exercises the REAL Python renderer — the primary
    // behavior gate now that the mock serves static goldens. The live plan
    // must match the committed golden (same engine, same samples).
    const dry = await client.sendCommand({
      command: "scene.apply",
      scene_id: "twilight",
      dry_run: true,
      request_id: "req-live-dry",
    });
    check("live: dry-run scene.apply ok (real renderer)", dry.ok === true, JSON.stringify(dry).slice(0, 200));
    const livePlan = dry.data && dry.data.render_plan;
    check("live: dry-run render_plan present", !!livePlan && livePlan.scene_id === "twilight");
    const liveGStrip = livePlan ? (livePlan.fixture_plans || []).find((p) => p.fixture_id === "g_strip") : null;
    check(
      "live: g_strip hue.put_light carries CLIP v2 dynamics.duration 400 (real renderer)",
      !!liveGStrip &&
        liveGStrip.operations &&
        liveGStrip.operations[0].op === "hue.put_light" &&
        liveGStrip.operations[0].payload.dynamics &&
        liveGStrip.operations[0].payload.dynamics.duration === 400,
      JSON.stringify(liveGStrip)
    );
    check(
      "live: dry-run plan matches the committed golden (Python canonical)",
      !!livePlan && !!goldens.twilight && deepEqual(livePlan, goldens.twilight.render_plan)
    );

    // command failures stay results (HTTP 200 + ok:false), never throw
    const notFound = await client.sendCommand({
      command: "scene.apply",
      scene_id: "nope",
      request_id: "req-live-nf",
    });
    check(
      "live: unknown scene -> ok:false not_found (no throw)",
      notFound.ok === false && notFound.error.code === "not_found"
    );

    // Builder authoring contract against the REAL engine (Pass 2 WP3 exit
    // gate: create/edit work end-to-end against the local backend).
    check(
      "live: normal mode exposes the authoring commands",
      ["scene.preview_draft", "scene.create", "scene.update"].every((c) => status.runtime.allowed_commands.includes(c))
    );
    const authoringDraft = {
      schema_version: 2,
      id: "",
      name: "Builder Live Check",
      target_ids: ["office"],
      palette: ["#112233", "#445566"],
      brightness: 42,
      motion: { mode: "palette_cycle", speed: 0.3, strategy: "auto" },
      default_state: { on: true, brightness: 40 },
    };
    const livePreview = await client.sendCommand({ command: "scene.preview_draft", scene: authoringDraft });
    check(
      "live: preview_draft validates + renders the unsaved draft (real renderer path)",
      livePreview.ok === true &&
        livePreview.data.dry_run === true &&
        livePreview.data.scene.id === "builder_live_check" &&
        (livePreview.data.render_plan.fixture_plans || []).length >= 1,
      JSON.stringify(livePreview).slice(0, 240)
    );
    const scenesAfterPreview = await client.getScenes();
    check(
      "live: preview_draft persisted nothing",
      !scenesAfterPreview.scenes.some((s) => s.id === "builder_live_check")
    );
    const liveCreate = await client.sendCommand({ command: "scene.create", scene: authoringDraft });
    check(
      "live: scene.create persists the canonical document",
      liveCreate.ok === true && liveCreate.data.scene.id === "builder_live_check" && liveCreate.data.scene.brightness === 42,
      JSON.stringify(liveCreate).slice(0, 240)
    );
    const liveUpdate = await client.sendCommand({
      command: "scene.update",
      scene_id: "builder_live_check",
      scene: { ...authoringDraft, id: "builder_live_check", name: "Builder Live Check 2", brightness: 77 },
    });
    check(
      "live: scene.update replaces the document, id stable",
      liveUpdate.ok === true && liveUpdate.data.scene.name === "Builder Live Check 2" && liveUpdate.data.scene.id === "builder_live_check" && liveUpdate.data.scene.brightness === 77
    );
    const liveIdChange = await client.sendCommand({
      command: "scene.update",
      scene_id: "builder_live_check",
      scene: { ...authoringDraft, id: "hijacked_id" },
    });
    check(
      "live: update cannot change the stable id",
      liveIdChange.ok === false && liveIdChange.error.code === "validation_error" && /immutable/.test(liveIdChange.error.message)
    );
    const twilightDoc = (await client.getScenes()).scenes.find((s) => s.id === "twilight");
    const twilightProvenance =
      twilightDoc && twilightDoc.metadata && twilightDoc.metadata.migrated_from_v1 ? twilightDoc.metadata.migrated_from_v1 : null;
    if (twilightProvenance) {
      const liveProvenance = await client.sendCommand({
        command: "scene.update",
        scene_id: "twilight",
        scene: {
          schema_version: 2,
          id: "twilight",
          name: twilightDoc.name,
          target_ids: twilightDoc.target_ids,
          palette: twilightDoc.palette,
          brightness: 55,
          motion: twilightDoc.motion,
        },
      });
      check(
        "live: update re-attaches migrated_from_v1 the payload omitted (no provenance erase)",
        liveProvenance.ok === true &&
          liveProvenance.data.scene.metadata.migrated_from_v1.filename === twilightProvenance.filename &&
          liveProvenance.data.scene.brightness === 55,
        JSON.stringify(liveProvenance).slice(0, 240)
      );
    } else {
      check("live: sample twilight carries migrated_from_v1 provenance (precondition)", false);
    }
  } catch (err) {
    check(`live checks aborted: ${err.message}`, false);
  } finally {
    await backend.close();
  }
}

// ---------------------------------------------------------------------------
// appdaemon transport checks: an in-node mimic of the AD 4.5 named endpoint.
// The real endpoint (scene_studio appdaemon_adapter) is served at
// /api/appdaemon/<endpoint_name>, accepts ONE JSON envelope POST
// {method, path, query?, body?}, and ALWAYS answers HTTP 200 with
// {status, body} (AppDaemon replaces 404/500 bodies with HTML error pages).
// ---------------------------------------------------------------------------

const AD_ENDPOINT_NAME = "scene_studio_api";

/**
 * Minimal mirror of service/api.route() for the transport checks.
 * @param {{method?: string, path: string, query?: object, body?: object}} envelope
 * @param {{scenes: object[], revision: () => number, bump: () => void}} state
 */
function adRoute(envelope, state) {
  const errPayload = (code, message) => ({ command: null, ok: false, error: { code, message } });
  const statusPayload = () => ({
    engine: { ok: true, revision: state.revision(), event_capacity: 500, events: 2 },
    fixtures: { total: 15, ready: 12, missing: 1, disabled: 1, unbound: 0, degraded: 0, conflicting: 0 },
    providers: {
      hue_v2: { total: 8, ready: 8, missing: 0, degraded: 0, other: 0 },
      wled: { total: 6, ready: 4, missing: 1, degraded: 0, other: 1 },
      ha_light: { total: 1, ready: 0, missing: 0, degraded: 0, other: 1 },
    },
    current: null,
    playback: null,
    last_discovery: null,
  });

  const method = envelope.method || "GET";
  const rawPath = envelope.path || "";
  const path = rawPath.startsWith("/api/scene_studio") ? rawPath.slice("/api/scene_studio".length) : rawPath;
  const query = envelope.query || {};
  const notFound = () => ({ status: 404, body: errPayload("not_found", `no route for ${method} ${rawPath}`) });
  if (path === "/status" && method === "GET") return { status: 200, body: statusPayload() };
  if (path === "/fixtures" && method === "GET") return { status: 200, body: { fixtures: [], targets: [] } };
  if (path === "/scenes" && method === "GET") {
    return { status: 200, body: { scenes: query.archived === "true" ? [] : state.scenes } };
  }
  if (path === "/discovery" && method === "GET") return { status: 200, body: { report: null } };
  if (path === "/diagnostics/recent" && method === "GET") return { status: 200, body: { events: [], count: 0 } };
  if (path === "/command" && method === "POST") {
    const body = envelope.body;
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      return {
        status: 400,
        body: errPayload("validation_error", "request body must be a JSON object command envelope"),
      };
    }
    if (body.command === "scene.rename") {
      const scene = state.scenes.find((s) => s.id === body.scene_id);
      if (!scene) {
        return {
          status: 200,
          body: {
            command: "scene.rename",
            ok: false,
            error: { code: "not_found", message: `scene '${body.scene_id}' not found` },
          },
        };
      }
      scene.name = body.name;
      state.bump();
      return {
        status: 200,
        body: { command: "scene.rename", ok: true, request_id: body.request_id, data: { scene: { ...scene } } },
      };
    }
    return {
      status: 200,
      body: { command: body.command ?? null, ok: false, error: { code: "unknown_command", message: "ad stub" } },
    };
  }
  return notFound();
}

/** In-node AppDaemon endpoint mimic (always HTTP 200 JSON envelope results). */
async function startAppDaemonStub() {
  const scenes = [
    {
      schema_version: 2, id: "twilight", name: "Twilight", target_ids: ["office"],
      palette: [], motion: { mode: "static", speed: 0, strategy: "auto" }, fixture_states: {}, metadata: {},
    },
    {
      schema_version: 2, id: "aurora_flow", name: "Aurora Flow", target_ids: ["office"],
      palette: [], motion: { mode: "palette_cycle", speed: 0.5, strategy: "auto" }, fixture_states: {}, metadata: {},
    },
  ];
  let revision = 3;
  const state = {
    scenes,
    revision: () => revision,
    bump: () => {
      revision += 1;
    },
  };

  const server = httpCreateServer((req, res) => {
    const sendJson = (payload) => {
      const body = JSON.stringify(payload);
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
      });
      res.end(body);
    };
    const sendHtml = (status, message) => {
      const body = `<html><head><title>${status}</title></head><body><h1>${message}</h1></body></html>`;
      res.writeHead(status, { "Content-Type": "text/html", "Content-Length": Buffer.byteLength(body) });
      res.end(body);
    };
    const url = new URL(req.url, "http://127.0.0.1");
    const match = /^\/api\/appdaemon\/([^/]+)$/.exec(url.pathname);
    if (req.method === "POST" && match) {
      let raw = "";
      req.on("data", (c) => (raw += c));
      req.on("end", () => {
        if (match[1] !== AD_ENDPOINT_NAME) {
          // AppDaemon answers unknown endpoint names with an HTML 404 page.
          return sendHtml(404, "404 App Not Found");
        }
        let envelope = null;
        try {
          envelope = JSON.parse(raw);
        } catch {
          envelope = null;
        }
        if (
          !envelope ||
          typeof envelope !== "object" ||
          Array.isArray(envelope) ||
          typeof envelope.path !== "string" ||
          !envelope.path
        ) {
          return sendJson({
            status: 400,
            body: { command: null, ok: false, error: { code: "validation_error", message: "endpoint payload must be a JSON object envelope with a 'path'" } },
          });
        }
        const result = adRoute(envelope, state);
        return sendJson({ status: result.status, body: result.body });
      });
      return;
    }
    sendHtml(404, "404 Not Found");
  });
  const port = pickPort();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  console.log(`  ..  appdaemon endpoint stub ready on ${baseUrl}/api/appdaemon/${AD_ENDPOINT_NAME}`);
  return { baseUrl, close: () => new Promise((r) => server.close(r)) };
}

async function runAppDaemonTransportChecks() {
  console.log("\n== appdaemon transport (named-endpoint envelope) ==");
  const ad = await startAppDaemonStub();
  const postEnvelope = async (envelope, endpoint = AD_ENDPOINT_NAME) => {
    const res = await fetch(`${ad.baseUrl}/api/appdaemon/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(envelope),
    });
    return { httpStatus: res.status, json: await res.json().catch(() => null), res };
  };
  try {
    const client = createHttpSceneStudioClient(ad.baseUrl, {
      transport: "appdaemon",
      endpointName: AD_ENDPOINT_NAME,
    });
    check("ad: client mode marker is still 'live'", client.mode === "live");
    check(
      "ad: client reports appdaemon transport + endpoint name",
      client.transport === "appdaemon" && client.endpointName === AD_ENDPOINT_NAME
    );

    // status shape through the envelope
    const status = await client.getStatus();
    check("ad: status fetch ok via envelope", !!status && status.engine.ok === true);
    check(
      "ad: status is engine-shaped (same unwrapped shape as direct)",
      ["engine", "fixtures", "providers", "current", "playback", "last_discovery"].every((k) => k in status) &&
        typeof status.engine.revision === "number",
      JSON.stringify(Object.keys(status || {}))
    );
    check(
      "ad: provider_links derived from providers dict",
      Array.isArray(status.provider_links) && status.provider_links.every((p) => p.connected === true)
    );

    // every read route reachable through the envelope
    const fixturesDoc = await client.getFixtures();
    check("ad: fixtures via envelope", Array.isArray(fixturesDoc.fixtures));
    const scenesDoc = await client.getScenes();
    check(
      "ad: scenes via envelope (active + archived merged)",
      scenesDoc.scenes.length === 2 && scenesDoc.scenes.some((s) => s.id === "twilight")
    );
    check("ad: discovery empty state is null via envelope", (await client.getDiscovery()) === null);
    const events = await client.getRecentEvents(10);
    check("ad: recent events envelope", Array.isArray(events.events));

    // one command round trip through the envelope
    const renamed = await client.sendCommand({
      command: "scene.rename",
      scene_id: "twilight",
      name: "Twilight AD",
      request_id: "req-ad-rename",
    });
    check("ad: scene.rename ok via envelope", renamed.ok === true, JSON.stringify(renamed).slice(0, 200));
    check("ad: request_id echoed via envelope", renamed.request_id === "req-ad-rename");
    const scenesAfter = await client.getScenes();
    check(
      "ad: rename persisted (getScenes reflects the command)",
      scenesAfter.scenes.find((s) => s.id === "twilight").name === "Twilight AD"
    );
    const statusAfter = await client.getStatus();
    check("ad: revision bumped by the command", statusAfter.engine.revision === 4);

    // 404 envelope: an unknown route rides HTTP 200 with {status: 404, body}
    const miss = await postEnvelope({ method: "GET", path: "/bogus" });
    check(
      "ad: 404 rides inside a 200 body envelope (never an HTML error page)",
      miss.httpStatus === 200 &&
        miss.json &&
        miss.json.status === 404 &&
        miss.json.body &&
        miss.json.body.error &&
        miss.json.body.error.code === "not_found",
      JSON.stringify(miss.json)
    );

    // malformed envelope -> 400-style body
    const bad = await postEnvelope({});
    check(
      "ad: malformed envelope -> 400 body envelope",
      bad.httpStatus === 200 && bad.json && bad.json.status === 400 && bad.json.body.error.code === "validation_error"
    );

    // unknown endpoint name -> AD-style HTML 404 -> client throws (transport failure)
    const wrongClient = createHttpSceneStudioClient(ad.baseUrl, {
      transport: "appdaemon",
      endpointName: "wrong_endpoint",
    });
    let threw = false;
    try {
      await wrongClient.getStatus();
    } catch {
      threw = true;
    }
    check("ad: unknown endpoint name throws like any transport failure", threw);
  } catch (err) {
    check(`appdaemon transport checks aborted: ${err.message}`, false);
  } finally {
    await ad.close();
  }
}
