/**
 * Deterministic mock live fixture color-state (plan §11: "extend the
 * Workbench mock with deterministic live-state scenarios sufficient to
 * visually and functionally test" every state shape the real normalizers
 * produce — RGB, CCT warm/cool, Hue gradient, off, unavailable, WLED
 * static, WLED dynamic/configured, several distinct WLED segment colors).
 *
 * This mocks the NORMALIZED `/fixture-state` response contract directly
 * (the same shape `backend/src/scene_studio/.../live_state` produces) — it
 * never reimplements provider normalization in JS (plan §11's explicit
 * instruction).
 *
 * Specific fixture ids from the sample registry are hand-assigned an
 * illustrative state so every case in the plan is guaranteed to be
 * exercised by at least one row; everything else gets a deterministic
 * (same fixture id -> same look, every load) fallback so the whole list
 * still looks "alive" without needing a hand-authored entry per fixture.
 */

const HAND_ASSIGNED = {
  g_strip: { on: true, brightness: 80, color_mode: "gradient", display_colors: ["#ff7a45", "#7c09ff", "#1f00f6"] },
  middle_bar: { on: true, brightness: 70, color_mode: "rgb", display_colors: ["#e0245e"] },
  lower_bar: { on: true, brightness: 55, color_mode: "cct", color_temp_kelvin: 2700, display_colors: ["#ffb46b"] },
  upper_bar: { on: true, brightness: 55, color_mode: "cct", color_temp_kelvin: 6000, display_colors: ["#cfe0ff"] },
  lamp: { on: true, brightness: 65, color_mode: "rgb", display_colors: ["#2fd0e0"], dynamic: true },
  custom_gradient: { on: false, color_mode: "none" },
  office_strip: { available: false, state_kind: "unavailable", detail: "Hue bridge unreachable (mock)" },
  wled_seg_0: { on: true, brightness: 90, color_mode: "rgb", display_colors: ["#ff2d55"] },
  wled_seg_1: {
    on: true, brightness: 75, color_mode: "dynamic", display_colors: ["#ff9500", "#34c759"], dynamic: true,
    state_kind: "configured_dynamic", detail: "configured effect colors; WLED does not report per-frame LED color here",
  },
  wled_seg_2: { on: true, brightness: 60, color_mode: "rgb", display_colors: ["#5ac8fa"] },
  wled_seg_3: { on: true, brightness: 60, color_mode: "rgb", display_colors: ["#ffd60a"] },
  wled_seg_4: { on: false, color_mode: "none" },
  wled_seg_5: { on: true, brightness: 40, color_mode: "rgb", display_colors: ["#bf5af2"] },
};

const PALETTE = ["#ff7a45", "#7c09ff", "#2fd0e0", "#e0245e", "#34c759", "#ffd60a", "#5ac8fa", "#bf5af2"];

function hashId(id) {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return hash;
}

function fallbackState(fixture) {
  const hash = hashId(fixture.id);
  const on = hash % 5 !== 0; // most fixtures on, a few off, deterministically
  if (!on) return { available: true, on: false, color_mode: "none", state_kind: "live" };
  const capabilities = fixture.capabilities || {};
  if (capabilities.color_xy) {
    return {
      available: true, on: true, brightness: 30 + (hash % 60), color_mode: "rgb",
      display_colors: [PALETTE[hash % PALETTE.length]], state_kind: "live",
    };
  }
  if (capabilities.color_temp) {
    const kelvin = 2200 + (hash % 4000);
    return { available: true, on: true, brightness: 30 + (hash % 60), color_mode: "cct", color_temp_kelvin: kelvin, state_kind: "live" };
  }
  return { available: true, on: true, brightness: 30 + (hash % 60), color_mode: "none", state_kind: "live" };
}

/**
 * @param {object[]} fixtures registry fixtures (post-scenario, as served by getFixtures())
 * @returns {{observed_at: string, fixtures: object, providers: object}}
 */
export function buildMockLiveFixtureState(fixtures) {
  const out = {};
  const providersSeen = new Set();
  for (const fixture of fixtures || []) {
    if (!fixture.binding) continue;
    providersSeen.add(fixture.binding.provider);
    if (fixture.enabled === false) {
      out[fixture.id] = {
        fixture_id: fixture.id, provider: fixture.binding.provider, available: false,
        state_kind: "unavailable", color_mode: "unknown", display_colors: [], dynamic: false,
        detail: "fixture disabled",
      };
      continue;
    }
    const hand = HAND_ASSIGNED[fixture.id];
    const base = hand || fallbackState(fixture);
    out[fixture.id] = {
      fixture_id: fixture.id,
      provider: fixture.binding.provider,
      available: base.available !== undefined ? base.available : true,
      on: base.on !== undefined ? base.on : null,
      brightness: base.brightness !== undefined ? base.brightness : null,
      color_mode: base.color_mode || "unknown",
      display_colors: base.display_colors || [],
      color_temp_kelvin: base.color_temp_kelvin !== undefined ? base.color_temp_kelvin : null,
      dynamic: !!base.dynamic,
      state_kind: base.state_kind || "live",
      ...(base.detail ? { detail: base.detail } : {}),
    };
  }
  const providers = {};
  for (const provider of providersSeen) providers[provider] = { ok: true, detail: "" };
  return { observed_at: new Date().toISOString(), fixtures: out, providers };
}
