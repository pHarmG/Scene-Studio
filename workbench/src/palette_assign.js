/**
 * Client-side palette pin / auto-spread for Builder assignment chips.
 * Mirrors `scene_studio.domain.palette_resolve` for display only — apply
 * and preview still go through the Python render plan.
 */

const HEX = /^#[0-9a-fA-F]{6}$/;

function isHex(value) {
  return typeof value === "string" && HEX.test(value);
}

function normalizeHex(value) {
  return isHex(value) ? value.toLowerCase() : null;
}

function uniquePreserve(colors) {
  const seen = new Set();
  const out = [];
  for (const color of colors || []) {
    const hex = normalizeHex(color);
    if (!hex || seen.has(hex)) continue;
    seen.add(hex);
    out.push(hex);
  }
  return out;
}

function healthSkipped(fixture) {
  if (fixture && fixture.enabled === false) return true;
  const health = fixture && fixture.health;
  return health === "disabled" || health === "missing" || health === "unbound";
}

export function fixtureTakesRgb(fixture) {
  const binding = (fixture && fixture.binding) || {};
  if (binding.provider === "hue_v2" && binding.resource_type === "grouped_light") return false;
  if (binding.provider === "wled") return true;
  return !!(fixture && fixture.capabilities && fixture.capabilities.color_xy);
}

function clampIndex(index, size) {
  if (!size) return 0;
  if (index < 0) return 0;
  if (index >= size) return size - 1;
  return index;
}

export function usesPalette(draft) {
  const palette = (draft && draft.palette) || [];
  if (!palette.length) return false;
  const mode = (draft && draft.motion && draft.motion.mode) || "static";
  return mode === "static" || mode === "palette_cycle";
}

function isSpreadCandidate(fixture, state, isOverride) {
  if (!fixtureTakesRgb(fixture) || healthSkipped(fixture)) return false;
  if (state && Array.isArray(state.gradient) && state.gradient.length) return false;
  if (isOverride && state && state.color) return false;
  if (state && state.palette_index !== undefined && state.palette_index !== null) return false;
  return true;
}

function isMultiGradient(state) {
  if (!state || !Array.isArray(state.gradient)) return false;
  return uniquePreserve(state.gradient).length >= 2;
}

function uniqueFixtureColors(scene) {
  const ordered = [];
  const seen = new Set();
  const add = (color) => {
    const hex = normalizeHex(color);
    if (!hex || seen.has(hex)) return;
    seen.add(hex);
    ordered.push(hex);
  };
  const palette = Array.isArray(scene && scene.palette) ? scene.palette : [];
  const states = (scene && scene.fixture_states) || {};
  for (const id of Object.keys(states).sort()) {
    const state = states[id];
    if (!state) continue;
    if (Array.isArray(state.gradient) && state.gradient.length) {
      const distinct = uniquePreserve(state.gradient);
      if (distinct.length >= 2) {
        distinct.forEach(add);
        continue;
      }
      add(distinct[0]);
      continue;
    }
    if (state.color) {
      add(state.color);
      continue;
    }
    if (state.palette_index !== undefined && state.palette_index !== null && palette.length) {
      add(palette[clampIndex(state.palette_index, palette.length)]);
    }
  }
  return ordered;
}

/**
 * Rebuild a collapsed palette from fixture colors and turn matching hex
 * overrides into `palette_index` pins. Pure: does not mutate `scene`.
 * @param {object} scene
 * @returns {object}
 */
export function canonicalizeStaticPalette(scene) {
  if (!scene || typeof scene !== "object") return scene;
  const mode = (scene.motion && scene.motion.mode) || "static";
  if (mode !== "static" && mode !== "palette_cycle") return scene;
  const states = scene.fixture_states || {};
  let palette = uniquePreserve(scene.palette || []);
  let rebuilt = false;
  if (palette.length < 2) {
    const fromStates = uniqueFixtureColors(scene);
    if (fromStates.length) {
      palette = fromStates;
      rebuilt = true;
    }
  } else {
    palette = (scene.palette || []).filter(isHex).map((color) => color.toLowerCase());
  }
  if (!palette.length) return scene;
  const nextStates = { ...states };
  let pinsChanged = false;
  for (const [id, state] of Object.entries(states)) {
    if (!state || typeof state !== "object" || isMultiGradient(state)) continue;
    const hex = normalizeHex(state.color);
    if (!hex) continue;
    const index = palette.indexOf(hex);
    if (index < 0) continue;
    const next = { ...state, palette_index: index };
    delete next.color;
    nextStates[id] = next;
    pinsChanged = true;
  }
  if (!rebuilt && !pinsChanged) return scene;
  return { ...scene, palette, fixture_states: nextStates };
}

/**
 * @param {object} draft
 * @param {object[]} fixturesInTargetOrder resolved fixtures, target order
 * @returns {Map<string, number>}
 */
export function computeSpreadIndices(draft, fixturesInTargetOrder) {
  const palette = (draft && draft.palette) || [];
  const map = new Map();
  if (!usesPalette(draft)) return map;
  const states = (draft && draft.fixture_states) || {};
  const defaults = (draft && draft.default_state) || {};
  let slot = 0;
  for (const fixture of fixturesInTargetOrder || []) {
    const isOverride = Object.prototype.hasOwnProperty.call(states, fixture.id);
    const state = isOverride ? states[fixture.id] : defaults;
    if (!state) continue;
    if (!isSpreadCandidate(fixture, state, isOverride)) continue;
    map.set(fixture.id, slot % palette.length);
    slot += 1;
  }
  return map;
}

/**
 * Effective palette slot and hex for one fixture (display).
 * @returns {{ index: number|null, color: string|null, pinned: boolean, custom: boolean }}
 */
export function resolveFixturePalette(draft, fixture, fixturesInTargetOrder) {
  const palette = ((draft && draft.palette) || []).map((color) => (isHex(color) ? color.toLowerCase() : color));
  const states = (draft && draft.fixture_states) || {};
  const isOverride = Object.prototype.hasOwnProperty.call(states, fixture.id);
  const state = isOverride ? states[fixture.id] : (draft && draft.default_state) || {};
  if (isMultiGradient(state)) {
    return { index: null, color: uniquePreserve(state.gradient)[0], pinned: false, custom: true };
  }
  const overrideHex = isOverride ? normalizeHex(state && state.color) : null;
  if (overrideHex) {
    const match = palette.indexOf(overrideHex);
    if (match >= 0) return { index: match, color: palette[match], pinned: true, custom: false };
    return { index: null, color: overrideHex, pinned: false, custom: true };
  }
  if (!palette.length) {
    const hex = normalizeHex(state && state.color);
    return { index: null, color: hex, pinned: false, custom: !!hex };
  }
  if (state && state.palette_index !== undefined && state.palette_index !== null) {
    const index = clampIndex(state.palette_index, palette.length);
    return { index, color: palette[index], pinned: true, custom: false };
  }
  const uniform = Array.isArray(state && state.gradient) ? uniquePreserve(state.gradient) : [];
  if (uniform.length === 1) {
    const match = palette.indexOf(uniform[0]);
    if (match >= 0) return { index: match, color: palette[match], pinned: true, custom: false };
    return { index: null, color: uniform[0], pinned: false, custom: true };
  }
  const spread = computeSpreadIndices(draft, fixturesInTargetOrder);
  if (spread.has(fixture.id)) {
    const index = spread.get(fixture.id);
    return { index, color: palette[index], pinned: false, custom: false };
  }
  return { index: null, color: null, pinned: false, custom: false };
}

function assignmentSignature(assignment) {
  return [
    assignment && assignment.pinned ? "1" : "0",
    assignment && assignment.custom ? "1" : "0",
    assignment && assignment.index != null ? String(assignment.index) : "",
    assignment && assignment.color ? String(assignment.color).toLowerCase() : "",
  ].join(":");
}

/**
 * Effective palette for a controller cluster (WLED segments). One shared
 * pin when every RGB member agrees; `mixed` when they differ so the
 * collapsed row does not pretend the whole fixture is a single slot.
 * @returns {{ index: number|null, color: string|null, pinned: boolean, custom: boolean, mixed: boolean, colors: string[] }}
 */
export function resolveClusterPalette(draft, fixtures, fixturesInTargetOrder) {
  const members = (fixtures || []).filter((fixture) => fixtureTakesRgb(fixture));
  if (!members.length) {
    return { index: null, color: null, pinned: false, custom: false, mixed: false, colors: [] };
  }
  const assignments = members.map((fixture) => resolveFixturePalette(draft, fixture, fixturesInTargetOrder));
  const colors = [];
  for (const assignment of assignments) {
    const hex = typeof assignment.color === "string" ? assignment.color.toLowerCase() : "";
    if (hex && !colors.includes(hex)) colors.push(hex);
  }
  const first = assignmentSignature(assignments[0]);
  const mixed = assignments.some((assignment) => assignmentSignature(assignment) !== first);
  if (mixed) {
    return { index: null, color: colors[0] || null, pinned: false, custom: false, mixed: true, colors };
  }
  return { ...assignments[0], mixed: false, colors };
}

/**
 * How many resolved fixtures currently show each palette slot (pin or auto).
 * Custom hex / gradient fixtures are omitted.
 * @param {object} draft
 * @param {object[]} fixturesInTargetOrder
 * @returns {number[]}
 */
export function paletteUsageCounts(draft, fixturesInTargetOrder) {
  const palette = (draft && draft.palette) || [];
  const counts = palette.map(() => 0);
  for (const fixture of fixturesInTargetOrder || []) {
    const assignment = resolveFixturePalette(draft, fixture, fixturesInTargetOrder);
    if (assignment.custom || assignment.index == null) continue;
    counts[assignment.index] += 1;
  }
  return counts;
}
