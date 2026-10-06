/**
 * Shared inline-SVG icon vocabulary (Stage 3+ facelift — see
 * docs/scene_studio/WORKBENCH_STAGE3_DESIGN_HANDOFF.md "Icon vocabulary").
 *
 * One 24-viewBox stroke family (`stroke="currentColor" stroke-width="2"
 * stroke-linecap="round" stroke-linejoin="round"`, no fill unless noted),
 * sized 13-18px by the caller. Pure presentation: every export is a lit
 * `html` TemplateResult with no state and no side effects. Callers that put
 * an icon inside a button MUST also supply an explicit accessible name
 * (title/aria-label) — icons never carry the label alone.
 *
 * Inbox and gear/settings are NOT here: app.js's header renders those
 * inline with its own long-standing paths (kept byte-identical for shell
 * consistency and because `scripts/browser_regression.mjs` selects the
 * gear button by `.icon-btn[title^='System']`, unrelated to its icon markup
 * but a reminder that header icon markup is otherwise left alone).
 */
import { svg } from "lit";

const base = (size, content) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${content}</svg>`;

export const iconPlay = (size = 16) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>`;

export const iconPause = (size = 16) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="5" height="18" rx="1"/><rect x="14" y="3" width="5" height="18" rx="1"/></svg>`;

export const iconStop = (size = 16) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>`;

export const iconApply = (size = 16) =>
  base(size, svg`<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>`);

export const iconPencil = (size = 16) =>
  base(size, svg`<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>`);

export const iconArchive = (size = 16) =>
  base(
    size,
    svg`<polyline points="21 8 21 21 3 21 3 8"/><rect x="1" y="3" width="22" height="5" rx="1"/><line x1="10" y1="12" x2="14" y2="12"/>`
  );

export const iconRestore = (size = 16) =>
  base(
    size,
    svg`<polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>`
  );

export const iconDryRun = (size = 16) =>
  base(size, svg`<path d="M9 3h6M10 3v5.5L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8.5V3"/>`);

export const iconWaveform = (size = 16) =>
  base(size, svg`<polyline points="2 12 7 12 9 5 13 19 15 12 22 12"/>`);

export const iconSparkle = (size = 16) =>
  base(
    size,
    svg`<path d="M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8L12 2z"/><path d="M19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15z"/>`
  );

export const iconSteadyDot = (size = 16) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="5"/></svg>`;

export const iconChevronRight = (size = 15) =>
  base(size, svg`<polyline points="9 18 15 12 9 6"/>`);

export const iconChevronDown = (size = 15) =>
  base(size, svg`<polyline points="6 9 12 15 18 9"/>`);

export const iconAlertCircle = (size = 16) =>
  base(
    size,
    svg`<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>`
  );

export const iconAlertTriangle = (size = 16) =>
  base(
    size,
    svg`<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>`
  );

export const iconCheckCircle = (size = 16) =>
  base(size, svg`<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>`);

export const iconMore = (size = 17) =>
  svg`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>`;

export const iconRefresh = (size = 15) =>
  base(
    size,
    svg`<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>`
  );

export const iconSearch = (size = 16) =>
  base(size, svg`<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>`);

export const iconDroplet = (size = 15) =>
  base(size, svg`<path d="M12 2.7l5.7 6.7a7 7 0 1 1-11.4 0z"/>`);

export const iconPin = (size = 14) =>
  base(size, svg`<path d="M12 21v-6"/><path d="M8.5 3h7l-1 7 2.5 3.5H7L9.5 10 8.5 3z"/>`);

export const iconThermometer = (size = 15) =>
  base(size, svg`<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>`);

export const iconLayers = (size = 15) =>
  base(size, svg`<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>`);

export const iconIdentify = (size = 16) =>
  base(
    size,
    svg`<circle cx="12" cy="12" r="2.5"/><path d="M12 4.5v2M12 17.5v2M19.5 12h-2M6.5 12h-2M17.5 6.5l-1.4 1.4M7.9 16.1l-1.4 1.4M17.5 17.5l-1.4-1.4M7.9 7.9L6.5 6.5"/>`
  );

export const iconDuplicate = (size = 16) =>
  base(
    size,
    svg`<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>`
  );

export const iconClock = (size = 16) =>
  base(
    size,
    svg`<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>`
  );

export const iconTrash = (size = 16) =>
  base(
    size,
    svg`<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M6 6l1 14a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-14"/>`
  );

export const iconPlus = (size = 15) =>
  base(
    size,
    svg`<path d="M12 5v14M5 12h14"/>`
  );
