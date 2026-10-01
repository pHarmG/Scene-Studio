/**
 * Right-side radiant state aura — pure CSS computation (facelift plan:
 * live fixture color-state pass, §6).
 *
 * Deliberately NOT a rectangular swatch: a soft radial glow anchored near
 * the row's right edge (provider/capability side), fading to fully
 * transparent before the identity/name region. One ellipse per color, up
 * to 3, layered as overlapping lobes for a gradient/multi-color fixture
 * rather than one linear-gradient row split (a plain multi-stop gradient
 * blurs adjacent hues together — the exact problem already fixed for the
 * Scenes palette band; the aura avoids it from the start).
 *
 * Pure and DOM-free so it's unit-testable without a browser (see
 * scripts/smoke.mjs's "fixture row aura" section).
 */

const LOBE_POSITIONS_PCT = [96, 84, 72]; // right-anchored, fading leftward
const MAX_LOBES = 3;

/**
 * @param {object} input
 * @param {boolean} input.enabled fixture.enabled !== false (registry-level; a
 *   disabled fixture never shows an aura regardless of any stale payload)
 * @param {boolean} [input.available] live_state.available
 * @param {boolean|null} [input.on] live_state.on
 * @param {number|null} [input.brightness] live_state.brightness (0..100)
 * @param {string[]} [input.displayColors] live_state.display_colors (#rrggbb, ordered)
 * @param {"fresh"|"aging"|"stale"|"unavailable"} [input.freshness]
 * @returns {string|null} a CSS `background` value, or null for "no aura"
 */
export function auraBackground({ enabled, available, on, brightness, displayColors, freshness }) {
  if (enabled === false) return null; // registry-disabled: never driven, never glows
  if (available === false) return null;
  if (on !== true) return null; // off, or truth not established — no aura, not a guess
  if (freshness === "stale" || freshness === "unavailable") return null;
  const colors = (displayColors || []).filter(Boolean).slice(0, MAX_LOBES);
  if (colors.length === 0) return null;

  const intensity = auraIntensity(brightness) * (freshness === "aging" ? 0.55 : 1);
  // Always fill every anchor position (96/84/72) so a solid-color fixture's
  // glow reaches exactly as far as a 3-color gradient's — one uniform
  // footprint across the whole list, not "more colors reach further."
  // Fewer colors than slots repeat the last one into the remaining,
  // farther-left slots.
  const lobes = LOBE_POSITIONS_PCT.map((pos, index) => {
    const hex = colors[Math.min(index, colors.length - 1)];
    const rgb = hexToRgb(hex);
    return rgb ? radialLobe(rgb, pos, intensity) : null;
  }).filter(Boolean);
  return lobes.length ? lobes.join(", ") : null;
}

/** Perceptual brightness curve (plan §6.4): off->none (handled by the
 *  caller), very dim->subtle, ordinary->clear, full->only moderately
 *  stronger than ordinary. A sqrt curve compresses the top end and lifts
 *  the low end instead of a linear 0..100 sweep. */
function auraIntensity(brightness) {
  if (typeof brightness !== "number" || Number.isNaN(brightness)) return 0.72; // on, brightness unknown: moderate default
  const normalized = Math.max(0, Math.min(100, brightness)) / 100;
  return Math.max(0.3, Math.sqrt(normalized));
}

// The gradient box's explicit ellipse size is a PERCENTAGE OF THE ROW
// ITSELF (spec: percentages on an explicit radial-gradient size resolve
// against the gradient box), not of some inner circle — so this number
// directly controls how far left the glow reaches from its anchor. Sized
// so a right-anchored (96%) lobe fades to nothing by roughly the row's
// middle, never reaching the name/identity region (plan §6.1/§6.7).
function radialLobe(rgb, positionPct, intensity) {
  const strong = rgba(rgb, 0.8 * intensity);
  const medium = rgba(rgb, 0.42 * intensity);
  const weak = rgba(rgb, 0.14 * intensity);
  return (
    `radial-gradient(ellipse 34% 130% at ${positionPct}% 50%, ` +
    `${strong} 0%, ${medium} 40%, ${weak} 68%, transparent 92%)`
  );
}

function rgba([r, g, b], alpha) {
  return `rgba(${r}, ${g}, ${b}, ${Math.round(alpha * 1000) / 1000})`;
}

function hexToRgb(hex) {
  const match = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
  if (!match) return null;
  const value = match[1];
  return [parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16)];
}
