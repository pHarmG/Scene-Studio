/**
 * Inspector descriptor builder: turns a WorkbenchSelection + store state into
 * a display descriptor for <ss-inspector>.
 *
 * Information levels (master plan §10.2):
 *  - facts/explanation  -> Level 1/2 (visible by default in the inspector)
 *  - technical          -> Level 3 (rendered inside a collapsed disclosure)
 *
 * Pure-ish: reads plain state, returns JSON; no DOM.
 */
import {
  normalizePlayback,
  liveSessions,
  orphanedSessions,
  providerLabel,
  sessionsForScene,
  summarizeSession,
} from "./playback.js";
import { observationMatchesBinding } from "./mocks/scenarios.js";
import { iconPlay, iconRefresh, iconIdentify } from "./components/icons.js";
import { liveStateFreshness } from "./state.js";
import { sceneLookSwatches } from "./scene_look.js";

/** Semantic status -> glyph tone (AGENTS.md styling mandate). */
export function statusTone(status) {
  switch (status) {
    case "ready":
    case "ok":
    case "bound_ready":
      return "ok";
    case "degraded":
    case "bound_degraded":
    case "candidate_replacement":
    case "warning":
    case "paused":
    case "warn":
      return "warn";
    case "missing":
    case "error":
    case "unbound":
    case "conflict":
    case "err":
      return "err";
    default:
      return "idle"; // disabled, archived, idle, info, unknown, bound_reconcile_available
  }
}

function findFixture(state, id) {
  return (state.fixtures ? state.fixtures.fixtures : []).find((f) => f.id === id) || null;
}

/** The per-fixture external light-sync policy enum, with the one-line
 *  "what happens on the next apply while hyperHDR holds this fixture"
 *  behavior for each option's tooltip. `value` is the exact policy string
 *  `fixture.set_contention_policy` accepts; `label` is display-only. */
const CONTENTION_POLICY_OPTIONS = [
  {
    value: "yield",
    label: "yield",
    description: "hyperHDR keeps this fixture while it syncs; Scene Studio skips it and auto-restores your look when sync stops.",
  },
  {
    value: "takeover",
    label: "takeover",
    description: "Each scene apply stops the hyperHDR instance covering this fixture, then applies. The next external grab wins again until then.",
  },
  {
    value: "ignore",
    label: "ignore",
    description: "Write anyway without stopping the stream — the write usually loses to it (colors snap back while hyperHDR syncs).",
  },
  {
    value: "default",
    label: "default (inherit)",
    description: "Clear this fixture's override and inherit the target/engine default.",
  },
];

function findScene(state, id) {
  return (state.scenes ? state.scenes.scenes : []).find((s) => s.id === id) || null;
}

function capabilityChips(caps) {
  if (!caps) return [];
  const chips = [];
  if (caps.on_off) chips.push("ON/OFF");
  if (caps.brightness) chips.push("BRI");
  if (caps.color_xy) chips.push("RGB");
  if (caps.color_temp) chips.push("TEMP");
  if (caps.cct) chips.push("CCT");
  if (caps.gradient) chips.push(`GRAD×${caps.gradient.max_points}`);
  if (caps.dynamic_native) chips.push("DYN");
  return chips;
}

function deviceLabel(profile) {
  if (!profile) return "unknown";
  const parts = [profile.manufacturer, profile.model].filter(Boolean);
  return parts.join(" ") || profile.product_name || "profiled";
}

function formatShift(before, after) {
  if (!before && !after) return "unchanged";
  if ((before || "unknown") === (after || "unknown")) return "unchanged";
  return `${before || "unknown"} → ${after || "unknown"}`;
}

function allowedCommands(state) {
  const commands = state.status && state.status.runtime ? state.status.runtime.allowed_commands : null;
  return Array.isArray(commands) ? commands : null;
}

function commandAllowed(state, command) {
  const commands = allowedCommands(state);
  return !commands || commands.includes(command);
}

function policyReason(state, command) {
  const mode = state.status && state.status.runtime ? state.status.runtime.mode : "unknown";
  if (command === "fixture.reconcile" || command === "fixture.enable" || command === "fixture.rebind") {
    return `Unavailable in the current backend mode (${mode}). Registry updates require registry_admin; provider writes stay blocked.`;
  }
  return `Unavailable in the current backend mode (${mode}).`;
}

function discoveryEntryForFixture(state, fixtureId) {
  return (state.discovery ? state.discovery.entries : []).filter((e) => e.fixture_id === fixtureId);
}

function candidatesForFixture(state, fixtureId) {
  return (state.discovery ? state.discovery.candidates : []).filter((c) => c.fixture_id === fixtureId);
}

function observationById(state, observationId) {
  return (state.discovery ? state.discovery.observations : []).find(
    (o) => `${o.provider}:${o.provider_resource_id}` === observationId
  );
}

/**
 * Build the inspector descriptor for the current selection.
 * @param {object} state store state (see state.js)
 * @returns {object|null} descriptor for <ss-inspector> or null
 */
export function buildInspectorDescriptor(state) {
  const sel = state.selection;
  if (!sel) return null;

  if (sel.type === "fixture") return fixtureDescriptor(state, sel.id);
  if (sel.type === "scene") return sceneDescriptor(state, sel.id);
  if (sel.type === "entry") return entryDescriptor(state, sel.id);
  if (sel.type === "event") return eventDescriptor(state, sel.id);
  // {type: "playback", id: session_id} (R5C §8) deliberately has NO
  // inspector descriptor: the session row in <ss-playback-panel> already
  // displays everything an inspector would duplicate. The selection only
  // drives focus/highlight in the Scenes view's Live playback panel (and
  // keeps the dock closed via the null return).
  return null;
}

/** "3s ago" / "just now"; null when there's nothing trustworthy to say. */
function relativeAge(sampledAt) {
  if (!sampledAt) return null;
  const ms = Date.now() - Date.parse(sampledAt);
  if (Number.isNaN(ms) || ms < 0) return null;
  return ms < 1000 ? "just now" : `${Math.round(ms / 1000)}s ago`;
}

/**
 * Compact "Current state" inspector facts (plan §7) — the live_state.js
 * observation, never provider JSON: On/Off, brightness, color mode +
 * current color(s)/Kelvin, dynamic status, and sample freshness/age. Only
 * appended when a live sample actually exists for this fixture; an
 * unsampled fixture gets no fabricated row.
 */
function currentStateFacts(state, fixtureId) {
  const live = state.fixtureLiveState;
  const observation = live && live.byId ? live.byId[fixtureId] : null;
  if (!observation) return [];
  const freshness = liveStateFreshness(live.sampledAt);
  const age = relativeAge(live.sampledAt);
  const facts = [];
  if (observation.available === false) {
    facts.push({ label: "Current state", value: `Unavailable${observation.detail ? ` — ${observation.detail}` : ""}` });
    return facts;
  }
  const power = observation.on === true ? "On" : observation.on === false ? "Off" : "Unknown";
  const brightnessText = typeof observation.brightness === "number" ? ` · ${observation.brightness}%` : "";
  facts.push({ label: "Current state", value: `${power}${brightnessText} · ${observation.color_mode}` });
  if (observation.display_colors && observation.display_colors.length) {
    facts.push({ label: "Current color", value: observation.display_colors.join(", ") });
  }
  if (observation.color_temp_kelvin) {
    facts.push({ label: "Current color temp", value: `${observation.color_temp_kelvin} K` });
  }
  if (observation.dynamic) {
    facts.push({
      label: "Dynamic",
      value: observation.state_kind === "configured_dynamic" ? "Active (configured colors, not per-frame-sampled)" : "Active",
    });
  }
  facts.push({ label: "Sample", value: `${freshness}${age ? ` · ${age}` : ""}` });
  return facts;
}

function fixtureDescriptor(state, fixtureId) {
  const fixture = findFixture(state, fixtureId);
  if (!fixture) return null;
  const binding = fixture.binding;
  const entries = discoveryEntryForFixture(state, fixtureId);
  const candidateList = candidatesForFixture(state, fixtureId).filter((candidate) => {
    const obs = observationById(state, candidate.observation_id);
    return obs && !observationMatchesBinding(obs, binding);
  });
  const candidateObs = candidateList.length
    ? observationById(state, candidateList[0].observation_id)
    : null;
  const reconcileEntry = entries.find((entry) => entry.status === "bound_reconcile_available");
  const reconcileObs = reconcileEntry && reconcileEntry.observation_id
    ? observationById(state, reconcileEntry.observation_id)
    : null;

  const explanation = [];
  const assessment = fixture.capability_assessment;
  const intentionallyDisabled = !fixture.enabled || fixture.health === "disabled";
  const readyLimited = fixture.health === "ready" && assessment && assessment.status === "limited";

  if (intentionallyDisabled) {
    explanation.push("Intentionally disabled.");
    explanation.push("Excluded from scenes.");
  } else if (readyLimited) {
    explanation.push(
      "The fixture is reachable and usable. The current provider exposes fewer capabilities than the known physical device."
    );
    if (assessment.reasons && assessment.reasons.length) {
      explanation.push(assessment.reasons.join(" "));
    }
  } else if (assessment && assessment.status === "limited") {
    explanation.push(
      `Provider capability is limited: ${(assessment.reasons || []).join(" ") || "the effective provider controls are narrower than available device evidence."}`
    );
  }
  if (reconcileEntry) {
    explanation.push("REGISTRY UPDATE AVAILABLE");
    explanation.push("This is still the same light. Scene Studio simply has newer or better evidence about it.");
    const proposedDevice = deviceLabel(reconcileObs && reconcileObs.device_profile);
    const proposedCaps = capabilityChips(reconcileObs && reconcileObs.capabilities).join(" · ") || "—";
    const currentCaps = capabilityChips(fixture.capabilities).join(" · ") || "—";
    explanation.push(`Binding unchanged. Health ${formatShift(fixture.health, "ready")}. Device ${formatShift(deviceLabel(fixture.device_profile), proposedDevice)}. Effective controls ${formatShift(currentCaps, proposedCaps)}. Assessment ${formatShift(assessment ? assessment.status : "unknown", assessment && assessment.status === "limited" ? "limited" : "review")}. Scene fidelity unchanged.`);
  }
  if (fixture.health_reason && !intentionallyDisabled && !reconcileEntry) explanation.push(fixture.health_reason + ".");
  if (fixture.health === "degraded" && !reconcileEntry) {
    explanation.push("Provider-effective capability is reduced versus the stored set.");
  }
  if (fixture.health === "missing") {
    explanation.push("Scenes skip this fixture until the resource returns or the fixture is rebound; the binding is retained so a simple retry may be enough.");
  }
  for (const entry of entries) {
    if (entry.status === "candidate_replacement" && candidateObs) {
      explanation.push(
        `Discovery suggests a possible replacement: "${candidateObs.name}" (confidence ${candidateList[0].confidence}, ${candidateList[0].compatibility}). Rebinding is a registry action and never happens automatically.`
      );
    }
  }
  if (explanation.length === 0) explanation.push("Bound, enabled and observed by the last discovery run; scenes resolve this fixture normally.");

  const actions = [];
  if (intentionallyDisabled) {
    if (commandAllowed(state, "fixture.enable")) {
      actions.push({ id: "fixture.enable", label: "Enable", tone: "ok", args: { fixture_id: fixture.id } });
    } else {
      actions.push({
        id: "__policy_note",
        label: "Enable",
        tone: "idle",
        note: policyReason(state, "fixture.enable"),
        args: { fixture_id: fixture.id },
      });
    }
  } else {
    actions.push({ id: "fixture.disable", label: "Disable", tone: "idle", args: { fixture_id: fixture.id } });
  }
  if (fixture.health === "missing") {
    actions.push({ id: "fixture.retry", label: "Retry", tone: "warn", args: { fixture_id: fixture.id } });
  } else if (fixture.binding) {
    // Same command as the urgent "Retry" above, offered any time the
    // fixture isn't already flagged missing: an everyday re-check, not a
    // recovery action, so it's a quiet glyph rather than warn-toned text.
    actions.push({ id: "fixture.retry", label: iconRefresh(15), ariaLabel: "Refresh state", tone: "idle", args: { fixture_id: fixture.id } });
  }
  // External light-sync (hyperHDR) policy: only meaningful for providers an
  // external sync stream can own. Rendered as ONE labeled select (current
  // value stays visible; each option's next-apply behavior lives in its
  // tooltip) instead of a spread of "Sync: …" buttons that hid the current
  // value. The select emits the real fixture.set_contention_policy action;
  // this entry is presentational.
  if (fixture.binding && (fixture.binding.provider === "wled" || fixture.binding.provider === "hue_v2")) {
    const current = fixture.contention_policy || "default";
    explanation.push(
      `External light-sync policy: ${current}.`
      + (fixture.contention && fixture.contention.held
        ? " hyperHDR is syncing this fixture right now — applies yield until it surrenders."
        : " hyperHDR wins while it syncs; this policy decides what a scene apply does then.")
    );
    actions.push({
      id: "__contention_policy",
      fixture_id: fixture.id,
      current,
      options: CONTENTION_POLICY_OPTIONS,
    });
  }
  if (fixture.binding) {
    actions.push({
      id: "fixture.identify",
      label: iconIdentify(16),
      ariaLabel: "Identify (flash the light)",
      tone: "idle",
      args: { fixture_id: fixture.id },
    });
  }
  if (reconcileObs) {
    actions.push({
      id: "fixture.reconcile_preview",
      label: "Review update",
      tone: "ok",
      args: { fixture_id: fixture.id, observation_id: reconcileEntry.observation_id },
    });
  }
  if (candidateObs) {
    actions.push({
      id: "fixture.rebind_preview",
      label: `Review rebind to "${candidateObs.name}"`,
      tone: "warn",
      args: { fixture_id: fixture.id, observation_id: `${candidateObs.provider}:${candidateObs.provider_resource_id}` },
      confirm: true,
    });
  }

  return {
    kind: "fixture",
    title: fixture.name,
    subtitle: fixture.location ? `Fixture • ${fixture.location}` : "Fixture",
    tone: intentionallyDisabled ? "idle" : readyLimited || fixture.health === "ready" ? "ok" : statusTone(fixture.health),
    status: intentionallyDisabled
      ? "intentionally disabled"
      : readyLimited
        ? "ready · capability limited"
        : reconcileEntry
          ? `${fixture.health} · registry update available`
          : fixture.health,
    // "Capabilities" is rendered by <ss-inspector> as capability badges
    // (ss-capability-indicators, same icons as the Fixtures list rows) read
    // straight from `technical.capabilities` below — not a text fact, so it
    // is not duplicated here. "Discovery" (the raw discovery-entry status
    // string, e.g. "bound_ready"/"bound_reconcile_available") was also
    // dropped: it always duplicated the status glyph and/or the explanation
    // callout in plainer language, and the full entries remain available
    // in Technical details (`technical.discovery_entries`) for anyone who
    // wants the raw value — level-1/2 facts stay non-redundant.
    // Live current state leads (user feedback: registry status is
    // secondary/reference information, not the primary signal — "what is
    // this fixture doing right now" belongs above "what does the registry
    // say about it"); registry facts follow.
    facts: [
      ...currentStateFacts(state, fixture.id),
      { label: "Groups", value: (fixture.groups || []).join(", ") || "—" },
      { label: "Provider", value: binding ? providerLabel(binding.provider) : "unbound" },
      { label: "Device", value: fixture.device_profile ? deviceLabel(fixture.device_profile) : "not profiled" },
      { label: "Capability assessment", value: assessment ? assessment.status : "unknown" },
    ],
    explanation,
    actions,
    technical: {
      fixture_id: fixture.id,
      binding,
      capabilities: fixture.capabilities,
      device_profile: fixture.device_profile || null,
      capability_assessment: assessment || null,
      discovery_entries: entries,
      candidates: candidateList,
      metadata: fixture.metadata || {},
    },
  };
}

function sceneDescriptor(state, sceneId) {
  const scene = findScene(state, sceneId);
  if (!scene) return null;
  const archived = !!(scene.metadata && scene.metadata.archived_at);
  const migrated = !!(scene.metadata && scene.metadata.migrated_from_v1);
  const explanation = [];
  if (archived) explanation.push("Archived scenes stay in the catalog for reference and can be restored; they are hidden from everyday apply lists.");
  if (migrated) {
    explanation.push(
      `Migrated from v1 file "${scene.metadata.migrated_from_v1.filename}" — palette/motion values were reconstructed and may be approximate.`
    );
  }
  if (scene.motion && scene.motion.mode !== "static") {
    explanation.push(
      `Dynamic scene (${scene.motion.mode}, speed ${scene.motion.speed}). Playback prefers provider-native execution; fixtures without native dynamics hold a static palette anchor.`
    );
  }
  if (explanation.length === 0) explanation.push("Static scene; applying sends per-fixture state to every target member.");

  const actions = [];
  if (!archived) {
    actions.push({ id: "scene.apply", label: "Apply", tone: "ok", args: { scene_id: scene.id } });
    actions.push({ id: "scene.preview", label: "Dry run", tone: "idle", args: { scene_id: scene.id } });
    if (scene.motion && scene.motion.mode !== "static") {
      actions.push({ id: "playback.start", label: iconPlay(16), ariaLabel: "Play", tone: "ok", args: { scene_id: scene.id } });
    }
    actions.push({ id: "scene.archive", label: "Archive", tone: "idle", args: { scene_id: scene.id } });
  } else {
    actions.push({ id: "scene.restore", label: "Restore", tone: "ok", args: { scene_id: scene.id } });
  }

  // Live-playback issues (R5C): the same degraded fixture executions the
  // row's issue tag counts, resolved to display names and plain-language
  // reasons — the inspector is where "what is the actual issue" is
  // answered, not just counted.
  const playback = state.status && state.status.playback ? normalizePlayback(state.status.playback) : null;
  const seenIssues = new Set();
  const issues = playback
    ? sessionsForScene(playback, scene.id)
        .flatMap((session) =>
          summarizeSession(session).degraded.map((exec) => {
            const detail = exec.detail || exec.fidelity || exec.execution || "degraded";
            const key = `${exec.fixture_id}|${detail}`;
            if (seenIssues.has(key)) return null;
            seenIssues.add(key);
            const fixture = findFixture(state, exec.fixture_id);
            return { fixture: (fixture && fixture.name) || exec.fixture_id, detail };
          })
        )
        .filter(Boolean)
    : [];

  return {
    kind: "scene",
    title: scene.name,
    subtitle: `Scene • ${(scene.target_ids || []).join(", ")}`,
    tone: archived ? "idle" : "ok",
    status: archived ? "archived" : "active",
    facts: [
      { label: "Targets", value: scene.target_ids.join(", ") || "—" },
      { label: "Mode", value: scene.motion ? `${scene.motion.mode} (speed ${scene.motion.speed})` : "static" },
      { label: "Palette", swatches: sceneLookSwatches(scene) },
      { label: "Fixtures with explicit state", value: String(Object.keys(scene.fixture_states || {}).length) },
    ],
    issues,
    explanation,
    actions,
    technical: {
      scene_id: scene.id,
      schema_version: scene.schema_version,
      default_state: scene.default_state || null,
      fixture_states: scene.fixture_states,
      metadata: scene.metadata || {},
    },
  };
}

function entryDescriptor(state, key) {
  const entry = (state.discovery ? state.discovery.entries : []).find(
    (e, i) => entryKey(e, i) === key
  );
  if (!entry) return null;
  const fixture = entry.fixture_id ? findFixture(state, entry.fixture_id) : null;
  const obs = entry.observation_id ? observationById(state, entry.observation_id) : null;
  const candidateList = entry.fixture_id ? candidatesForFixture(state, entry.fixture_id) : [];
  const candidate = candidateList.length ? candidateList[0] : null;
  const candidateObs = candidate ? observationById(state, candidate.observation_id) : null;

  const explanation = [];
  if (entry.detail) explanation.push(capitalize(entry.detail) + ".");
  if (entry.status === "available_unbound" && obs) {
    explanation.push(
      "This resource is not associated with any logical fixture. Leaving it unbound changes nothing; creating a fixture is a registry-seeding action, not a discovery action."
    );
  }
  if (entry.status === "candidate_replacement" && candidateObs) {
    explanation.push(
      `Candidate reasons: ${candidate.reasons.join("; ")}. Automatic silent rebinding is prohibited — review, then rebind explicitly.`
    );
  }
  if (entry.status === "bound_reconcile_available") {
    explanation.push("This is still the same light. Scene Studio simply has newer or better evidence about it.");
  }
  if (entry.status === "disabled") {
    explanation.push("Intentionally disabled.");
    explanation.push("Excluded from scenes.");
  }

  const actions = [];
  if (entry.status === "candidate_replacement" && entry.fixture_id) {
    if (candidateObs) {
      actions.push({
        id: "fixture.rebind_preview",
        label: `Review rebind to "${candidateObs.name}"`,
        tone: "warn",
        args: { fixture_id: entry.fixture_id, observation_id: candidate.observation_id },
        confirm: true,
      });
    }
    actions.push({ id: "fixture.disable", label: "Disable fixture", tone: "idle", args: { fixture_id: entry.fixture_id } });
    actions.push({ id: "__leave_unbound", label: "Leave unbound", tone: "idle", args: { key } });
  } else if (entry.status === "bound_reconcile_available" && entry.fixture_id && obs) {
    actions.push({
      id: "fixture.reconcile_preview",
      label: "Review update",
      tone: "ok",
      args: { fixture_id: entry.fixture_id, observation_id: entry.observation_id },
    });
  } else if (entry.status === "missing" && entry.fixture_id) {
    actions.push({ id: "fixture.retry", label: "Retry", tone: "warn", args: { fixture_id: entry.fixture_id } });
    actions.push({ id: "fixture.disable", label: "Disable fixture", tone: "idle", args: { fixture_id: entry.fixture_id } });
  } else if (entry.status === "available_unbound") {
    actions.push({ id: "__leave_unbound", label: "Leave unbound", tone: "idle", args: { key } });
  }

  return {
    kind: "entry",
    title: fixture ? fixture.name : obs ? obs.name || entry.observation_id : entry.fixture_id || entry.observation_id,
    subtitle: `Discovery • ${entry.status}`,
    tone: statusTone(entry.status),
    status: entry.status,
    facts: [
      { label: "Status", value: entry.status },
      { label: "Provider", value: obs ? providerLabel(obs.provider) : fixture && fixture.binding ? providerLabel(fixture.binding.provider) : "—" },
      { label: "Candidate confidence", value: candidate ? String(candidate.confidence) : "—" },
      { label: "Location hint", value: (obs && obs.location_hint) || (fixture && fixture.location) || "—" },
    ],
    explanation,
    actions,
    technical: {
      entry,
      observation: obs,
      candidates: candidateList,
      observation_capabilities: obs ? obs.capabilities : null,
      observation_metadata: (obs && obs.metadata) || {},
    },
  };
}

function eventDescriptor(state, key) {
  const event = state.events.find((e, i) => eventKey(e, i) === key);
  if (!event) return null;
  return {
    kind: "event",
    title: event.summary,
    subtitle: `Diagnostics • ${event.timestamp.slice(11, 16)} UTC`,
    tone: event.level === "error" ? "err" : event.level === "warning" ? "warn" : "ok",
    status: event.level,
    facts: [
      { label: "Category", value: event.category },
      { label: "Scene", value: event.scene_id || "—" },
      { label: "Fixture", value: event.fixture_id || "—" },
      { label: "Provider", value: event.provider ? providerLabel(event.provider) : "—" },
    ],
    explanation: [event.detail || "No additional detail."],
    actions: [],
    technical: event.data && Object.keys(event.data).length ? event.data : { note: "no structured payload on this event" },
  };
}

/**
 * Overview's "Needs attention" exceptions AND the header status pill's
 * severity/count (facelift plan §2/§4.2, Stage 2 corrective pass §3) —
 * the ONE shared source of truth for "is anything wrong," so the shell
 * and Overview can never disagree (the pill derives its tone/count purely
 * from this array's `tone` values — see app.js).
 *
 * Considers, in order: live connection health (client-side transport
 * state — a down/negotiating connection means everything below may be
 * stale), engine health, provider connectivity, fixture health, discovery
 * replacement candidates, backend warnings, orphaned playback sessions
 * (R5A — still animating on the bridge/controller after an engine
 * restart), and degraded live playback sessions (R5C — any reported
 * execution failure or unsupported fidelity).
 *
 * @param {object} state store state (see state.js)
 * @returns {{tone:"ok"|"warn"|"err"|"idle", text:string,
 *            view:string, selection:object|null}[]}
 */
export function buildOverviewExceptions(state) {
  const exceptions = [];

  // Client-side transport health — independent of whether a status doc has
  // loaded yet, since a failed/negotiating connection is itself the thing
  // to report (and explains why nothing else below may be current).
  if (state.conn.mode === "live") {
    if (state.conn.status === "error") {
      exceptions.push({
        tone: "err",
        text: `Live connection unreachable${state.conn.error ? ` — ${state.conn.error}` : ""}`,
        view: "diagnostics",
        selection: null,
      });
    } else if (state.conn.status === "connecting") {
      exceptions.push({
        tone: "warn",
        text: "Connecting to live engine…",
        view: "diagnostics",
        selection: null,
      });
    }
  }

  const st = state.status;
  if (!st) return exceptions;

  if (st.engine && st.engine.ok === false) {
    exceptions.push({ tone: "err", text: "Engine reporting unhealthy", view: "diagnostics", selection: null });
  }

  const links = st.provider_links || [];
  const offlineProviders = new Set(links.filter((p) => !p.connected).map((p) => p.provider));
  const fixtures = state.fixtures ? state.fixtures.fixtures : [];
  // Post-R5 visual polish: how many otherwise-unremarkable fixtures are
  // "missing" SOLELY because their own provider is down — folded into that
  // provider's one exception line (with a count) instead of one
  // near-identical line per fixture. A 6-segment WLED controller outage
  // doesn't need "WLED provider offline" plus 6x "Segment N missing —
  // provider wled offline"; the provider line already says it all, and the
  // Fixtures view already condenses those same segments into one cluster
  // row (components/ss-fixture-cluster.js). A fixture missing for its OWN
  // reason (its provider is otherwise connected) still gets its own line
  // below, unchanged.
  const missingDueToOfflineProvider = new Map();
  for (const f of fixtures) {
    if (f.health === "missing" && f.binding && offlineProviders.has(f.binding.provider)) {
      missingDueToOfflineProvider.set(f.binding.provider, (missingDueToOfflineProvider.get(f.binding.provider) || 0) + 1);
    }
  }
  for (const p of links) {
    if (!p.connected) {
      const count = missingDueToOfflineProvider.get(p.provider) || 0;
      exceptions.push({
        tone: "err",
        text: count
          ? `${p.label} provider offline — ${count} fixture${count === 1 ? "" : "s"} treated as missing`
          : `${p.label} provider offline — its fixtures are treated as missing`,
        view: "fixtures",
        selection: null,
      });
    }
  }
  const discoveryByFixture = new Map();
  for (const entry of state.discovery ? state.discovery.entries : []) {
    if (entry.fixture_id) discoveryByFixture.set(entry.fixture_id, entry);
  }
  for (const f of fixtures) {
    const disc = discoveryByFixture.get(f.id);
    if (!f.enabled || f.health === "disabled") continue;
    if (disc && disc.status === "bound_reconcile_available") {
      exceptions.push({
        tone: "idle",
        text: `${f.name}: registry update available — same light, newer evidence`,
        view: "fixtures",
        selection: { type: "fixture", id: f.id },
      });
      continue;
    }
    if (f.health === "ready") continue;
    if (f.health === "missing" && f.binding && offlineProviders.has(f.binding.provider)) continue;
    const tone = f.health === "missing" ? "err" : f.health === "degraded" ? "warn" : "idle";
    exceptions.push({
      tone,
      text: `${f.name} ${f.health}${f.health_reason ? ` — ${f.health_reason}` : ""}`,
      view: "fixtures",
      selection: { type: "fixture", id: f.id },
    });
  }
  const candidateEntries = (state.discovery ? state.discovery.entries : []).filter(
    (e) => e.status === "candidate_replacement"
  );
  candidateEntries.forEach((entry, i) => {
    const fixture = entry.fixture_id ? findFixture(state, entry.fixture_id) : null;
    if (fixture && (!fixture.enabled || fixture.health === "disabled")) return;
    exceptions.push({
      tone: "warn",
      text: `Replacement candidate for ${entry.fixture_id} — review in Discovery`,
      view: "discovery",
      selection: { type: "entry", id: entryKey(entry, i) },
    });
  });
  for (const w of st.warnings || []) {
    exceptions.push({ tone: "warn", text: w.message, view: "diagnostics", selection: null });
  }

  const playback = normalizePlayback(st.playback);
  // Orphaned playback: the provider may still be animating after an engine
  // restart — always needs attention (R5 plan §1.4).
  for (const session of orphanedSessions(playback)) {
    exceptions.push({
      tone: "warn",
      text: `${session.scene_name || session.scene_id}: playback orphaned after engine restart — provider may still be animating`,
      view: "scenes",
      selection: { type: "playback", id: session.session_id },
    });
  }
  // Degraded live sessions (R5C §6): any reported execution failure or an
  // explicitly unsupported realization surfaces here, single-sourced with
  // the header pill through this builder.
  for (const session of liveSessions(playback)) {
    const summary = summarizeSession(session);
    if (!summary.degraded.length) continue;
    const allFailed = summary.ok_count === 0 && summary.failed_count > 0;
    exceptions.push({
      tone: allFailed ? "err" : "warn",
      text: `${summary.scene_name || session.scene_id}: ${summary.degraded.length} playback fixture${summary.degraded.length === 1 ? "" : "s"} ${allFailed ? "failed" : "degraded"}`,
      view: "scenes",
      selection: { type: "playback", id: session.session_id },
    });
  }

  return exceptions;
}

/**
 * Discovery's open-exception count (facelift plan §2) — the same filter
 * discovery.js's own `#exceptions()` applies (non-`bound_ready`, not
 * session-dismissed), exposed here so the header Inbox badge (app.js,
 * Stage 2) can compute it without reaching into the view component.
 * @param {object} state store state
 * @returns {number}
 */
export function discoveryOpenCount(state) {
  const report = state.discovery;
  if (!report) return 0;
  const skip = new Set(["bound_ready", "disabled"]);
  return report.entries.filter((entry, i) => !skip.has(entry.status) && !state.dismissed[entryKey(entry, i)])
    .length;
}

/** Stable key for a discovery entry within the current report. */
export function entryKey(entry, index) {
  return entry.observation_id
    ? `obs:${entry.observation_id}`
    : entry.fixture_id
      ? `fix:${entry.fixture_id}:${entry.status}`
      : `idx:${index}`;
}

/** Stable key for an event within the current feed. */
export function eventKey(event, index) {
  return `${event.timestamp}|${event.summary}|${index}`;
}

function capitalize(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
