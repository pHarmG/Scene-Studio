/**
 * <ss-swatch-band> — a scene's palette (facelift plan §6, Stage 1; band
 * variant added Stage 3+ per §4.3/§5; segmented chips in the product-polish
 * pass, Task 2).
 *
 * Stage-1 scope (`variant="squares"`, the default): a byte-identical
 * extraction of the markup/CSS that used to live inline in <ss-scene-row>
 * (a row of up to `max` 13px squares) — no visual change for any existing
 * caller that doesn't pass `variant`.
 *
 * `variant="band"`: the palette as one larger, more prominent chip — a
 * single smooth interpolated blend across the ordered stops (matching the
 * HA-native Scene Studio card's own treatment, so the two surfaces read as
 * one system), rather than hard-edged equal-width segments. A single-color
 * palette renders as one solid chip. An empty palette renders an explicit
 * dashed "no palette" placeholder instead of a colored chip, so a scene
 * truly carrying no palette is never drawn as if it had a null/
 * border-colored stop.
 *
 * @prop {string[]} palette hex color strings
 * @prop {number} max max swatches shown in "squares" mode (default 6,
 *   matches prior behavior); ignored in "band" mode, which always uses the
 *   full ordered palette as segments.
 * @prop {"squares"|"band"} variant default "squares"
 */
import { LitElement, html, css } from "lit";

/** Each color gets an equal 1/N share of the bar (a flat run at its own
 * hex), with the blend into its neighbor confined to a narrow band
 * straddling each 1/N boundary — not one continuous interpolation across
 * the whole width. A continuous N-stop blend gives an interior color a
 * transition on both sides and an edge color a transition on only one,
 * so the edge colors end up occupying roughly HALF the visual width of an
 * interior color even though every color is one equally-weighted list
 * entry — this is what that looked like ("are we underprioritizing the
 * end colors?"). Confining the blend to a fixed fraction of the segment
 * width around each boundary keeps every color's flat run exactly 1/N of
 * the bar, while still crossfading softly instead of a hard-edged seam. */
export function bandGradient(colors) {
  if (!colors.length) return "transparent";
  if (colors.length === 1) return colors[0];
  const n = colors.length;
  const segment = 100 / n;
  const half = segment * 0.3;
  const stops = [`${colors[0]} 0%`];
  for (let i = 0; i < n - 1; i += 1) {
    const boundary = (i + 1) * segment;
    stops.push(`${colors[i]} ${boundary - half}%`);
    stops.push(`${colors[i + 1]} ${boundary + half}%`);
  }
  stops.push(`${colors[n - 1]} 100%`);
  return `linear-gradient(90deg, ${stops.join(", ")})`;
}

/** A very faint hairline at each 1/N boundary — the center of that
 * boundary's blend band (see bandGradient) and the edge between two
 * equal-width color shares, so it lines up with both the actual color
 * transition and the segment count a reader expects from N list entries. */
export function bandDividers(colors) {
  if (colors.length < 2) return "none";
  const lines = [];
  for (let i = 1; i < colors.length; i += 1) {
    const pct = (i / colors.length) * 100;
    lines.push(`linear-gradient(90deg, transparent calc(${pct}% - 1px), rgba(0, 0, 0, 0.22) ${pct}%, transparent calc(${pct}% + 1px))`);
  }
  return lines.join(", ");
}

export class SsSwatchBand extends LitElement {
  static properties = {
    palette: { attribute: false },
    max: { type: Number },
    variant: { type: String },
  };

  static styles = css`
    :host {
      display: flex;
      gap: 2px;
      align-items: center;
      min-width: 0;
      width: 100%;
    }
    .swatch {
      width: 15px;
      height: 15px;
      border-radius: 3px;
      border: 1px solid rgba(255, 255, 255, 0.18);
    }
    .band {
      position: relative;
      width: 100%;
      min-width: 4.5rem;
      height: 30px;
      border-radius: 9px;
      border: 1px solid rgba(255, 255, 255, 0.22);
      overflow: hidden;
      flex: 1 1 auto;
    }
    /* The fill paints on an inset pseudo-element, never the same box as the
       border-radius clip — putting a 90deg gradient directly on the
       rounded-corner box causes the GPU to bleed the far-end stop into the
       corner's antialiasing. A 1px inset breaks that (same fix as the
       HA-native card's blendStrip). */
    .band::after {
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: inherit;
      background: var(--ss-band-lines, none), var(--ss-band-fill, transparent);
    }
    .band.empty {
      border-style: dashed;
      border-color: var(--ss-border);
    }
    .band.empty::after {
      background: repeating-linear-gradient(
        135deg,
        var(--ss-border) 0 5px,
        transparent 5px 10px
      );
    }
  `;

  constructor() {
    super();
    this.palette = [];
    this.max = 6;
    this.variant = "squares";
  }

  render() {
    const palette = this.palette || [];
    if (this.variant === "band") {
      if (!palette.length) {
        return html`<span class="band empty" title="No palette set"></span>`;
      }
      return html`<span
        class="band"
        style=${`--ss-band-fill:${bandGradient(palette)};--ss-band-lines:${bandDividers(palette)}`}
        title=${`Palette, in order: ${palette.join(" → ")}`}
      ></span>`;
    }
    return html`
      ${palette.slice(0, this.max).map(
        (hex) => html`<span class="swatch" style="background:${hex}" title=${hex}></span>`
      )}
    `;
  }
}

customElements.define("ss-swatch-band", SsSwatchBand);
