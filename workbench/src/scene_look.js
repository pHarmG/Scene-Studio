/**
 * Catalog/overview color identity for a Scene v2 document.
 *
 * Static scenes are palette assignments. The band is that ordered palette
 * once it has two or more hues. A collapsed reconstructed palette
 * (Meeting Blue's five identical G Strip cyans) is rebuilt from the
 * per-fixture mix so the row matches apply and the Builder pins.
 */
import { canonicalizeStaticPalette } from "./palette_assign.js";

const HEX = /^#[0-9a-fA-F]{6}$/;

function isHex(value) {
  return typeof value === "string" && HEX.test(value);
}

function uniquePreserve(colors) {
  const seen = new Set();
  const out = [];
  for (const color of colors || []) {
    if (!isHex(color)) continue;
    const hex = color.toLowerCase();
    if (seen.has(hex)) continue;
    seen.add(hex);
    out.push(hex);
  }
  return out;
}

/**
 * Ordered hex list for swatch bands and hero washes.
 *
 * @param {object} scene Scene v2 JSON
 * @returns {string[]}
 */
export function sceneLookSwatches(scene) {
  const canonical = canonicalizeStaticPalette(scene) || scene;
  const palette = Array.isArray(canonical.palette)
    ? canonical.palette.filter(isHex).map((color) => color.toLowerCase())
    : [];
  if (uniquePreserve(palette).length >= 2) return palette;
  return uniquePreserve(palette);
}
