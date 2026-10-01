/**
 * Playback session-collection model — the ONE read-side home for
 * `status().playback` (R5C; docs/scene_studio/R5C_WORKBENCH_INTEGRATION_PLAN.md).
 *
 * `status().playback` is authoritatively the R5A collection shape
 * (R5_DYNAMIC_PLAYBACK_PLAN.md §1.5, engine.py PlaybackState.status_view):
 *   {
 *     sessions: PlaybackSession[],
 *     counts: {active, paused, orphaned, stopped},
 *     owned_fixture_count: number,
 *   }
 * where each PlaybackSession carries {session_id, scene_id, scene_name,
 * target_ids, fixture_ids, state, started_at, paused_at?, stopped_at?,
 * stop_reason?, preempted_by?, fixture_executions[]} with state one of
 * "active"|"paused"|"stopped"|"orphaned", and each fixture execution carries
 * {fixture_id, provider, execution, fidelity, ok, detail} — fidelity one of
 * "native"|"equivalent"|"approximate"|"unsupported", execution one of the
 * backend EXECUTION_KINDS ("pending", "native_dynamic_palette",
 * "native_scene", "native_effect", "native_preset", "approximate_static").
 *
 * The live HA backend has not received R5A/R5B yet (R5D is the deployment
 * gate), so it may still answer `getStatus()` with the PRE-R5A singular
 * shape: {scene_id, target_id, started_at, revision, paused} | null.
 * `normalizePlayback` is the ONE place that knows both shapes exist — every
 * other Workbench surface consumes only the normalized collection via the
 * helpers below, never the raw `status.playback` field directly.
 *
 * CARDINALITY (R5C critical correction): the backend guarantees one
 * live (active/paused) owner per FIXTURE, not one live session per scene.
 * The same scene may legitimately run as several disjoint sessions against
 * different target/fixture subsets. Nothing outside this module may assume
 * "at most one live session per scene" — use `sessionsForScene`, which
 * returns zero, one, or many.
 *
 * Legacy-synthesized sessions (from the pre-R5A singular shape) are marked
 * `legacy: true` and carry a synthetic, non-backend `session_id`. They stay
 * displayable but never yield a sendable session id: command senders MUST
 * treat them as "no usable session_id" (see `hasSendableSessionId` and
 * `controlsForSession`) rather than sending a fabricated id to a backend
 * that doesn't understand it. Legacy-shape awareness must not spread past
 * this module.
 */

/** @typedef {"active"|"paused"|"stopped"|"orphaned"} PlaybackSessionState */

/** @typedef {"native"|"equivalent"|"approximate"|"unsupported"} FidelityLevel */

/** Backend EXECUTION_KINDS (domain/playback.py). */
export const EXECUTION_KINDS = [
  "pending",
  "native_dynamic_palette",
  "native_scene",
  "native_effect",
  "native_preset",
  "approximate_static",
];

/** Contract fidelity levels (ARCHITECTURE_CONTRACTS §6.5). */
export const FIDELITY_LEVELS = ["native", "equivalent", "approximate", "unsupported"];

/** Display labels for providers (single Workbench-wide source). */
const PROVIDER_LABELS = { hue_v2: "Hue", wled: "WLED", ha_light: "HA" };

/** @param {string} provider */
export function providerLabel(provider) {
  return PROVIDER_LABELS[provider] || provider || "—";
}

/** Session states that make a session "live" for the operational cockpit. */
// "held" is live too: the session is shown (with its held note) so the
// user can see what the external sync superseded.
const LIVE_STATES = ["active", "paused", "held", "orphaned"];

const EMPTY_COUNTS = { active: 0, paused: 0, held: 0, orphaned: 0, stopped: 0 };

/** @returns {{sessions: NormalizedSession[], counts: typeof EMPTY_COUNTS, owned_fixture_count: number}} */
function empty() {
  return { sessions: [], counts: { ...EMPTY_COUNTS }, owned_fixture_count: 0 };
}

/** Fill conservative per-session defaults so callers never guard keys. */
function normalizeSession(session) {
  return {
    paused_at: null,
    stopped_at: null,
    stop_reason: null,
    preempted_by: null,
    scene_name: null,
    target_ids: [],
    fixture_ids: [],
    fixture_executions: [],
    ...session,
  };
}

/**
 * Normalize a raw `status.playback` value into the R5A collection shape,
 * tolerating the pre-R5A singular shape from a not-yet-upgraded live
 * backend.
 * @param {object|null|undefined} raw
 * @returns {{sessions: NormalizedSession[], counts: typeof EMPTY_COUNTS, owned_fixture_count: number}}
 */
export function normalizePlayback(raw) {
  if (raw && Array.isArray(raw.sessions)) {
    // Already the R5A collection shape — pass through, filling conservative
    // defaults so callers never need to guard missing keys.
    return {
      sessions: raw.sessions.map(normalizeSession),
      counts: { ...EMPTY_COUNTS, ...(raw.counts || {}) },
      owned_fixture_count:
        typeof raw.owned_fixture_count === "number" ? raw.owned_fixture_count : 0,
    };
  }
  if (raw && typeof raw === "object" && typeof raw.scene_id === "string") {
    // Pre-R5A singular shape (live backend not yet upgraded): synthesize a
    // single-session collection. No real session_id exists pre-R5A — the
    // key is stable per scene (not per-play) purely to avoid template
    // re-render churn; it must never be sent to the backend as if real.
    const state = raw.paused ? "paused" : "active";
    /** @type {NormalizedSession} */
    const session = normalizeSession({
      session_id: `legacy:${raw.scene_id}`,
      scene_id: raw.scene_id,
      scene_name: raw.scene_id,
      target_ids: raw.target_id ? [raw.target_id] : [],
      state,
      started_at: raw.started_at || null,
      paused_at: raw.paused ? raw.started_at || null : null,
      legacy: true,
    });
    return {
      sessions: [session],
      counts: { ...EMPTY_COUNTS, [state]: 1 },
      owned_fixture_count: 0,
    };
  }
  return empty();
}

/** Sessions that are live for cockpit purposes: active + paused + orphaned. */
export function liveSessions(playback) {
  return playback.sessions.filter((s) => LIVE_STATES.includes(s.state));
}

/** Sessions with state "active". */
export function activeSessions(playback) {
  return playback.sessions.filter((s) => s.state === "active");
}

/** Sessions with state "paused". */
export function pausedSessions(playback) {
  return playback.sessions.filter((s) => s.state === "paused");
}

/**
 * Sessions with state "orphaned" — the engine restarted while they were
 * running; the bridge/controller may still be animating them unmanaged
 * (R5 plan §1.4). Always a needs-attention state.
 */
export function orphanedSessions(playback) {
  return playback.sessions.filter((s) => s.state === "orphaned");
}

/** Stopped/history sessions (bounded backend retention) — never live. */
export function stoppedSessions(playback) {
  return playback.sessions.filter((s) => s.state === "stopped");
}

/**
 * ALL live (non-"stopped") sessions for a scene — ZERO, ONE, OR MANY. The
 * backend's ownership invariant is per fixture, so the same scene can run
 * as several disjoint sessions. Never route lifecycle controls by picking
 * one of these implicitly; the session panel controls each by session_id.
 * @param {ReturnType<typeof normalizePlayback>} playback
 * @param {string} sceneId
 * @returns {NormalizedSession[]}
 */
export function sessionsForScene(playback, sceneId) {
  return liveSessions(playback).filter((s) => s.scene_id === sceneId);
}

/** Look up a session by its session_id (session-addressed commands, R5A). */
export function sessionById(playback, sessionId) {
  return playback.sessions.find((s) => s.session_id === sessionId) || null;
}

/**
 * Whether `session` has a real, backend-issued session_id that is safe to
 * send in a session-addressed command. Legacy-synthesized sessions (no
 * R5A backend) do not — sending one would either be silently wrong or
 * rejected as an unknown param by a pre-R5A backend. Callers must disable
 * session-addressed actions (pause/resume/stop) rather than fabricate a
 * sessionless fallback call, which R5A no longer accepts.
 * @param {NormalizedSession|null} session
 * @returns {boolean}
 */
export function hasSendableSessionId(session) {
  return !!session && !session.legacy && typeof session.session_id === "string" && session.session_id.length > 0;
}

// ---------------------------------------------------------------------------
// Session summary model — aggregate WITHOUT hiding failure (R5C plan §3).
// The Workbench never recreates provider logic; it only summarizes the
// backend-issued FixtureExecution records, and `fidelity` is authoritative
// (never inferred).
// ---------------------------------------------------------------------------

const EMPTY_FIDELITY = { native: 0, equivalent: 0, approximate: 0, unsupported: 0 };

/**
 * Compact view model for one session, derived purely from backend records.
 * Degraded = executions with ok === false OR fidelity === "unsupported"
 * (a reported-but-unrealizable fixture is an issue even when it "ran").
 * @param {NormalizedSession|null} session
 */
export function summarizeSession(session) {
  const executions = (session && session.fixture_executions) || [];
  const fidelity = { ...EMPTY_FIDELITY };
  /** @type {Record<string, {provider: string, total: number, ok: number, failed: number, kinds: Record<string, number>}>} */
  const providerMap = {};
  const degraded = [];
  for (const exec of executions) {
    if (fidelity[exec.fidelity] !== undefined) fidelity[exec.fidelity] += 1;
    const bucket =
      providerMap[exec.provider] ||
      (providerMap[exec.provider] = { provider: exec.provider, total: 0, ok: 0, failed: 0, kinds: {} });
    bucket.total += 1;
    if (exec.ok) bucket.ok += 1;
    else bucket.failed += 1;
    if (exec.execution) bucket.kinds[exec.execution] = (bucket.kinds[exec.execution] || 0) + 1;
    if (!exec.ok || exec.fidelity === "unsupported") degraded.push(exec);
  }
  const providerSummaries = Object.values(providerMap).sort((a, b) =>
    providerLabel(a.provider).localeCompare(providerLabel(b.provider))
  );
  return {
    state: session ? session.state : "stopped",
    scene_id: session ? session.scene_id : null,
    scene_name: (session && session.scene_name) || (session && session.scene_id) || null,
    target_ids: session ? session.target_ids || [] : [],
    // Reported executions are primary; declared fixture ids are the fallback
    // (legacy sessions carry no execution records at all).
    fixture_count: executions.length || (session ? (session.fixture_ids || []).length : 0),
    ok_count: executions.filter((e) => e.ok).length,
    failed_count: executions.filter((e) => !e.ok).length,
    unsupported_count: fidelity.unsupported,
    providers: providerSummaries,
    fidelity,
    degraded,
    legacy: !!(session && session.legacy),
  };
}

/**
 * Aggregate view model for the whole collection — the compact panel header.
 * Live counts are derived from the sessions themselves (what is rendered);
 * `counts` from the backend is displayed verbatim only for the stopped
 * history total.
 * @param {ReturnType<typeof normalizePlayback>} playback
 */
export function summarizePlayback(playback) {
  const live = liveSessions(playback);
  return {
    live: live.length,
    active: live.filter((s) => s.state === "active").length,
    paused: live.filter((s) => s.state === "paused").length,
    orphaned: live.filter((s) => s.state === "orphaned").length,
    stopped: playback.counts ? playback.counts.stopped || 0 : 0,
    owned_fixture_count: playback.owned_fixture_count || 0,
    degradedSessions: live.filter((s) => summarizeSession(s).degraded.length > 0).length,
  };
}

/**
 * Semantic tone for a session, using the EXISTING status semantics (no new
 * color system; R5C plan §3). Precedence:
 *   1. stopped/history            -> idle (gray)
 *   2. orphaned                   -> warn (always attention: the provider
 *                                    may still be animating post-restart)
 *   3. executions all failed      -> err (no useful provider realization)
 *   4. any failed or unsupported  -> warn (partial failure / no real
 *                                    realization on some fixtures)
 *   5. executions all ok, none
 *      unsupported                -> ok (green; paused-but-healthy stays
 *                                    green — the "Paused" state is carried
 *                                    by the state text, not the tone)
 *   6. no execution records
 *      (legacy/pre-R5A shape)     -> warn (transitional ambiguity: health
 *                                    cannot be verified)
 * @param {ReturnType<typeof summarizeSession>} summary
 * @returns {"ok"|"warn"|"err"|"idle"}
 */
export function sessionTone(summary) {
  if (summary.state === "stopped") return "idle";
  if (summary.state === "orphaned") return "warn";
  if (summary.state === "held") return "warn";
  const reported = summary.ok_count + summary.failed_count;
  if (reported > 0) {
    if (summary.ok_count === 0) return "err";
    if (summary.failed_count > 0 || summary.unsupported_count > 0) return "warn";
    return "ok";
  }
  return "warn";
}

// ---------------------------------------------------------------------------
// Canonical lifecycle control behavior (R5C plan §5). The panel is the
// authoritative session-addressed control surface; a button requires BOTH a
// real sendable session_id AND runtime.allowed_commands membership. There is
// deliberately no frontend shadow permission table and no sessionless
// fallback call shape.
// ---------------------------------------------------------------------------

/** Session lifecycle action -> backend command. */
export const LIFECYCLE_COMMANDS = {
  pause: "playback.pause",
  resume: "playback.resume",
  stop: "playback.stop",
};

/** Which lifecycle actions apply to which session state. */
const STATE_ACTIONS = {
  active: ["pause", "stop"],
  paused: ["resume", "stop"],
  // External light sync (hyperHDR) holds the fixtures: stop releases the
  // session bookkeeping without touching the external stream; resume is
  // only possible via an explicit takeover override (not offered here).
  held: ["stop"],
  orphaned: ["stop"],
  stopped: [],
};

const LEGACY_TITLE = "Session controls require the R5 backend upgrade.";

/**
 * Resolved lifecycle controls for one session under the backend-declared
 * runtime policy. Only actions valid for the session's state are offered at
 * all (a paused session is NOT offered Pause merely because backend pause is
 * idempotent — the UI expresses intent accurately). Every non-available
 * action carries the specific cause: "legacy" (no real backend session id),
 * "policy" (runtime mode forbids the command), or "state".
 *
 * @param {NormalizedSession|null} session
 * @param {string[]|null|undefined} allowedCommands status().runtime.allowed_commands
 * @returns {{offered: string[], controls: Record<string, {offered: boolean, enabled: boolean, reason: "legacy"|"policy"|"state"|null, title: string}>}}
 */
export function controlsForSession(session, allowedCommands) {
  const state = session ? session.state : "stopped";
  const offered = STATE_ACTIONS[state] || [];
  const policy = Array.isArray(allowedCommands) ? allowedCommands : null;
  const controls = {};
  for (const [action, command] of Object.entries(LIFECYCLE_COMMANDS)) {
    const isOffered = offered.includes(action);
    let reason = null;
    if (!isOffered) {
      reason = "state";
    } else if (!hasSendableSessionId(session)) {
      reason = "legacy";
    } else if (policy && !policy.includes(command)) {
      reason = "policy";
    }
    controls[action] = {
      offered: isOffered,
      enabled: isOffered && reason === null,
      reason,
      title:
        reason === "legacy"
          ? LEGACY_TITLE
          : reason === "policy"
            ? "Unavailable in the current backend runtime mode"
            : "",
    };
  }
  return { offered, controls };
}
