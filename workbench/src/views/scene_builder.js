/**
 * <ss-view-scene-builder> — the Scenes workflow's Builder workspace.
 *
 * One canonical Scene v2 draft (a deep copy held by the store) edited through
 * ordinary authoring controls, in this information hierarchy:
 *
 *   Identity → Targets → Scene Defaults → Motion (Palette; Static assignment) →
 *   Overrides → Preview → Actions
 *
 * Capability scope (Builder-expansion plan):
 * - §1 targets: declared Rooms as named chips, HA Light Group helpers as
 *   collapsible member sets (never a revived aggregate fixture), and
 *   individual fixtures grouped by room with WLED clustered by controller;
 * - §2 `default_state` as first-class defaults (On/Off, brightness, color,
 *   color temperature);
 * - §3 per-fixture `fixture_states` overrides with progressive disclosure,
 *   where editing a supported field never deletes advanced content
 *   (gradient/effect/transition/provider_ext) on the same fixture;
 * - §4 Duplicate opens the same Builder with the source INTENT and no source
 *   identity; Save New lets the backend derive/validate the id and records
 *   the duplicate provenance itself;
 * - §5 static / palette-cycle motion + speed stay editable, advanced motion
 *   (effect mode, non-auto strategy) is preserved and warned about;
 * - §6 ordered palette authoring (add/remove/reorder/duplicate/hex/picker +
 *   compact order preview + confirmed clear);
 * - §7 Preview plays the unsaved draft on fixtures (`scene.play_draft`);
 *   the render plan still drives an honest quality headline (playing /
 *   reductions / partially unsupported / cannot render). Read-only
 *   runtimes fall back to observational `scene.preview_draft`.
 *
 * Server authority stays server-side: no renderer logic, no slug rules, no
 * provider payloads, and no second scene store live here. Preview sends
 * `scene.play_draft` (or `scene.preview_draft` in read-only); Save sends
 * `scene.create` / `scene.update` and adopts the returned server document.
 */
import { html, css } from "lit";
import { SsLightElement } from "../components/ss-light-element.js";
import "../components/ss-empty-state.js";
import { iconAlertTriangle, iconCheckCircle, iconAlertCircle, iconChevronRight } from "../components/icons.js";
import { auraBackground } from "../components/aura.js";
import { resolveTargetFixtures, summarizePreviewQuality, describeFixtureState, describeAdvancedFields } from "../state.js";
import {
  clusterLabel,
  controllerDisplayName,
  ecosystemGroupsFromDiscovery,
  fixtureHealthNote,
  groupByRoom,
  groupCountMeta,
  nestedGroupLabel,
  orderDeclaredTargets,
  roomLabel,
  segmentDisplayName,
  selectionOf,
  toggleIdSet,
  withControllerClusters,
} from "../grouping.js";
import {
  fixtureTakesRgb,
  paletteUsageCounts,
  resolveClusterPalette,
  resolveFixturePalette,
  usesPalette,
} from "../palette_assign.js";

/** Neutral starting swatches for "Add color" (cycled). */
const STARTER_COLORS = ["#ff7a45", "#6f4bff", "#00c853", "#2962ff", "#ff8f00", "#e05d5d"];

/** Motion modes the ordinary editor owns; anything else is preserved. */
const EDITABLE_MOTION_MODES = ["static", "palette_cycle"];

/** Seeds used when a property is switched from "not set" to "set". */
const PROPERTY_SEEDS = { brightness: 60, color: "#ffffff", color_temp_mirek: 270 };

function mirekApproxHex(mirek) {
  const u = Math.max(0, Math.min(1, (Number(mirek) - 100) / 900));
  const r = Math.round(207 + (255 - 207) * u);
  const g = Math.round(232 + (154 - 232) * u);
  const b = Math.round(255 + (74 - 255) * u);
  return `#${[r, g, b].map((n) => n.toString(16).padStart(2, "0")).join("")}`;
}

/** CSS background for the Scene Defaults look wash. */
function lookWash(state) {
  if (!state || state.on === false) {
    return "background: repeating-linear-gradient(-45deg, var(--ss-surface-3) 0 7px, var(--ss-surface-2) 7px 14px);";
  }
  const br = typeof state.brightness === "number" ? state.brightness : 60;
  const mix = Math.round(24 + Math.sqrt(Math.max(0, Math.min(100, br)) / 100) * 56);
  let hex = state.color;
  if (!hex && state.color_temp_mirek != null) hex = mirekApproxHex(state.color_temp_mirek);
  if (!hex || !/^#[0-9a-fA-F]{6}$/.test(hex)) hex = "#f0d5a0";
  return `background: radial-gradient(130% 160% at 18% 32%, ${hex} 0%, transparent 56%), color-mix(in srgb, ${hex} ${mix}%, var(--ss-surface-2));`;
}

export class SsViewSceneBuilder extends SsLightElement {
  static properties = { store: { attribute: false } };

  static styles = css`
    @scope (ss-view-scene-builder) {
    :scope {
      display: flex;
      flex-direction: column;
      min-width: 0;
      width: 100%;
      container-type: inline-size;
      container-name: ss-builder;
    }
    .head {
      display: flex;
      align-items: baseline;
      gap: 10px;
      margin: 2px 0 10px;
      flex-wrap: wrap;
    }
    .head h2 {
      margin: 0;
      font-size: var(--ss-size-15);
      font-weight: 650;
    }
    .head .scene-id {
      font-family: var(--ss-font-mono);
      font-size: 13px;
      color: var(--ss-text-faint);
    }
    .head .scene-id.collide {
      color: var(--ss-warn);
    }
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    @container ss-builder (max-width: 720px) {
      .workspace {
        grid-template-columns: 1fr;
      }
    }
    .col-targets,
    .col-look {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .section {
      background: var(--ss-surface);
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
      padding: 16px;
      margin-bottom: 12px;
    }
    .col-targets .section,
    .col-look .section {
      margin-bottom: 0;
    }
    #builder-overrides {
      margin-top: 16px;
    }
    .motion-section {
      padding: 12px;
    }
    .motion-section .section-head {
      margin-bottom: 8px;
    }
    .section-head {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 22px;
      margin-bottom: 12px;
    }
    .section-head .field-label {
      margin: 0;
    }
    .section-head .count {
      font-size: 11px;
      font-weight: 650;
      color: var(--ss-text-faint);
      border: 1px solid var(--ss-border-soft);
      border-radius: 999px;
      padding: 1px 8px;
      line-height: 1.4;
    }
    .section-head .hint {
      margin: 0 0 0 auto;
      font-size: 12px;
    }
    .section > label.field-label,
    .field-label {
      display: block;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
      margin-bottom: 10px;
    }
    .hint {
      font-size: 13px;
      color: var(--ss-text-faint);
      margin: 8px 0 0;
    }
    .hint.warn {
      color: var(--ss-warn);
    }
    input[type="text"],
    input[type="number"],
    select {
      appearance: none;
      background: var(--ss-bg);
      color: var(--ss-text);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      padding: 7px 12px;
      font-size: 15px;
    }
    select {
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%2398a2b0' d='M2.6 4.2L6 8l3.4-3.8'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 12px center;
      padding-right: 30px;
    }
    input[type="text"]:focus,
    input[type="number"]:focus,
    select:focus {
      outline: none;
      border-color: var(--ss-accent);
    }
    input[type="text"].invalid,
    input[type="number"].invalid {
      border-color: var(--ss-err);
    }
    input[type="checkbox"] {
      appearance: none;
      width: 20px;
      height: 20px;
      margin: 0;
      flex: 0 0 auto;
      border: 1.5px solid var(--ss-border);
      border-radius: 7px;
      background: var(--ss-bg) center / 14px 14px no-repeat;
      cursor: pointer;
    }
    input[type="checkbox"]:checked {
      background-color: var(--ss-accent);
      border-color: var(--ss-accent);
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='none' stroke='%23fff' stroke-width='2' d='M2.5 6.2L5 8.7 9.5 3.5'/%3E%3C/svg%3E");
    }
    input[type="range"] {
      appearance: none;
      height: 6px;
      background: color-mix(in srgb, var(--ss-text) 14%, var(--ss-surface-3));
      border-radius: 999px;
    }
    input[type="range"]::-webkit-slider-runnable-track {
      height: 6px;
      background: color-mix(in srgb, var(--ss-text) 14%, var(--ss-surface-3));
      border-radius: 999px;
    }
    input[type="range"]::-webkit-slider-thumb {
      appearance: none;
      width: 18px;
      height: 18px;
      margin-top: -6px;
      border: 0;
      border-radius: 50%;
      background: var(--ss-accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--ss-accent) 22%, transparent);
    }
    input[type="range"]::-moz-range-track {
      height: 6px;
      background: color-mix(in srgb, var(--ss-text) 14%, var(--ss-surface-3));
      border-radius: 999px;
      border: none;
    }
    input[type="range"]::-moz-range-thumb {
      width: 18px;
      height: 18px;
      border: 0;
      border-radius: 50%;
      background: var(--ss-accent);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--ss-accent) 22%, transparent);
    }
    #builder-name {
      width: 100%;
      max-width: none;
      padding: 10px 14px;
      font-size: 17px;
      border-radius: var(--ss-radius);
    }
    .target-groups {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .target-group > .group-label {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
      margin-bottom: 8px;
    }
    .target-chips {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(146px, 1fr));
      gap: 10px;
    }
    .target-chip {
      display: flex;
      align-items: center;
      gap: 8px;
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      min-height: var(--ss-control-h);
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border);
      border-radius: 14px;
      cursor: pointer;
      font-size: 14px;
      color: var(--ss-text);
    }
    .target-chip:hover {
      background: color-mix(in srgb, var(--ss-surface-2) 70%, var(--ss-text) 12%);
    }
    .target-chip:has(input:checked) {
      border-color: color-mix(in srgb, var(--ss-accent) 55%, var(--ss-border));
      background: color-mix(in srgb, var(--ss-accent) 14%, var(--ss-surface-2));
    }
    .target-chip .name {
      font-weight: 650;
    }
    .target-chip .meta {
      margin-left: auto;
      font-size: 12px;
      color: var(--ss-text-faint);
    }
    .target-chip.warn .meta {
      color: var(--ss-warn);
    }
    .target-fold {
      border: 1px solid var(--ss-border-soft);
      border-radius: 14px;
      background: var(--ss-surface-2);
      margin-bottom: 8px;
    }
    .target-fold:last-child {
      margin-bottom: 0;
    }
    .target-fold > summary {
      list-style: none;
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      cursor: pointer;
    }
    .target-fold > summary::-webkit-details-marker {
      display: none;
    }
    .target-fold .chev {
      display: inline-flex;
      color: var(--ss-text-faint);
      transition: transform 120ms ease;
      flex: 0 0 auto;
    }
    .target-fold[open] > summary .chev {
      transform: rotate(90deg);
    }
    .target-fold .body {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
      gap: 4px 10px;
      padding: 0 10px 10px;
    }
    .target-cluster,
    .target-eco {
      grid-column: 1 / -1;
    }
    .target-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      min-height: 38px;
      padding: 7px 10px;
      border-radius: 10px;
      cursor: pointer;
      font-size: 14px;
      color: var(--ss-text);
    }
    .target-row:hover {
      background: color-mix(in srgb, var(--ss-text) 6%, transparent);
    }
    .target-row .name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .target-row .meta {
      margin-left: auto;
      font-size: 12px;
      color: var(--ss-text-faint);
      flex: 0 0 auto;
    }
    .target-row .meta.warn,
    .target-fold.warn > summary .meta {
      color: var(--ss-warn);
    }
    .target-fold > summary .pick {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      flex: 1;
      cursor: pointer;
    }
    .target-fold > summary .name {
      font-weight: 650;
    }
    .target-fold > summary .meta {
      margin-left: auto;
      font-size: 12px;
      color: var(--ss-text-faint);
    }
    .target-cluster,
    .target-eco.target-fold .target-fold {
      background: var(--ss-surface-3);
    }
    .state-grid {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .look-hero {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      align-items: stretch;
    }
    .look-wash {
      min-height: 108px;
      border-radius: 16px;
      border: 1px solid var(--ss-border);
      box-shadow: inset 0 -28px 36px color-mix(in srgb, var(--ss-bg) 35%, transparent);
    }
    .look-power {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
      padding: 14px;
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border-soft);
      border-radius: 16px;
    }
    .look-power .look-k {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
    }
    .look-power .state-on {
      font-size: 15px;
      font-weight: 650;
      padding: 10px 14px;
      border-radius: var(--ss-radius-sm);
      background: var(--ss-bg);
      width: 100%;
    }
    .state-grid > .look-power {
      flex-direction: row;
      align-items: center;
      justify-content: space-between;
    }
    .state-grid > .look-power .state-on {
      width: auto;
      min-width: 128px;
    }
    .look-control {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 118px;
      gap: 8px 12px;
      align-items: center;
      padding: 12px 14px;
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border-soft);
      border-radius: 16px;
    }
    .look-control.is-off {
      opacity: 0.55;
    }
    .look-control .look-label {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 650;
      font-size: 14px;
      color: var(--ss-text);
    }
    .look-control .look-val {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      width: 118px;
      font-family: var(--ss-font-mono);
      font-size: 14px;
      color: var(--ss-text-dim);
    }
    .look-control input[type="range"] {
      grid-column: 1 / -1;
      width: 100%;
      max-width: none;
      height: 16px;
      accent-color: var(--ss-accent);
    }
    .look-control input[type="number"] {
      width: 4.5rem;
      text-align: right;
      padding: 6px 10px;
    }
    .look-control input[type="color"] {
      appearance: none;
      width: 36px;
      height: 36px;
      padding: 0;
      border: 1px solid var(--ss-border);
      border-radius: 10px;
      background: var(--ss-bg);
      cursor: pointer;
      overflow: hidden;
    }
    .look-control input[type="color"]::-webkit-color-swatch-wrapper {
      padding: 0;
    }
    .look-control input[type="color"]::-webkit-color-swatch {
      border: none;
      border-radius: 9px;
    }
    .look-control input.hex {
      width: 84px;
      font-family: var(--ss-font-mono);
      font-size: 15px;
      padding: 6px 10px;
    }
    .look-control .look-swatch {
      grid-column: 1 / -1;
      height: 8px;
      border-radius: 999px;
      background: var(--ss-surface-3);
      border: 1px solid var(--ss-border-soft);
    }
    .look-control .look-extra {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .look-control .look-ends {
      display: flex;
      justify-content: space-between;
      grid-column: 1 / -1;
      font-size: 11px;
      color: var(--ss-text-faint);
    }
    .state-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      color: var(--ss-text-dim);
      flex-wrap: wrap;
    }
    .state-row .prop {
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 128px;
    }
    .state-row input[type="range"] {
      max-width: 220px;
    }
    .state-row input[type="number"] {
      width: 72px;
    }
    .state-row input[type="color"] {
      width: 30px;
      height: 24px;
      padding: 0;
      border: 1px solid var(--ss-border);
      border-radius: 8px;
      background: var(--ss-bg);
      cursor: pointer;
    }
    .state-row input.hex {
      width: 96px;
      font-family: var(--ss-font-mono);
      font-size: 15px;
    }
    .palette-panel {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid var(--ss-border-soft);
    }
    .palette-panel .field-label {
      margin-bottom: 0;
    }
    .palette-panel .hint {
      margin: 0;
    }
    .palette-rows {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .palette-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border-soft);
      border-radius: 14px;
    }
    .palette-row .ord {
      width: 18px;
      font-size: 11px;
      color: var(--ss-text-faint);
      font-family: var(--ss-font-mono);
      text-align: right;
    }
    .palette-row input[type="color"] {
      appearance: none;
      width: 32px;
      height: 32px;
      min-width: 32px;
      min-height: 32px;
      padding: 0;
      border: 1px solid var(--ss-border);
      border-radius: 8px;
      background: var(--ss-bg);
      cursor: pointer;
      overflow: hidden;
    }
    .palette-row input[type="color"]::-webkit-color-swatch-wrapper {
      padding: 0;
    }
    .palette-row input[type="color"]::-webkit-color-swatch {
      border: none;
      border-radius: 9px;
    }
    .palette-row input.hex {
      width: 96px;
      font-family: var(--ss-font-mono);
      font-size: 15px;
    }
    .palette-row button,
    .row-btn {
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: var(--ss-control-h);
      padding: 6px 12px;
      font-size: 14px;
      font-weight: 600;
      color: var(--ss-text);
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border);
      border-radius: var(--ss-radius-sm);
      cursor: pointer;
    }
    .palette-row .row-btn {
      width: var(--ss-row-btn);
      min-width: var(--ss-row-btn);
      padding: 0;
    }
    .palette-row button:hover,
    .row-btn:hover {
      background: color-mix(in srgb, var(--ss-text) 8%, var(--ss-surface-2));
      border-color: var(--ss-accent);
    }
    .palette-row button:disabled,
    .row-btn:disabled {
      opacity: 0.4;
      cursor: default;
    }
    .palette-preview {
      height: 12px;
      border-radius: 999px;
      border: 1px solid var(--ss-border-soft);
      margin: 4px 0 0;
    }
    .palette-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 44px;
      width: 100%;
      border: 1px dashed var(--ss-border);
      border-radius: 12px;
      background: var(--ss-surface-2);
      color: var(--ss-text-dim);
      font-size: 14px;
      cursor: pointer;
    }
    .palette-empty:hover {
      border-color: var(--ss-accent);
      color: var(--ss-text);
    }
    .palette-actions {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-top: 2px;
    }
    .paint-use {
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-weight: 600;
      color: var(--ss-text-faint);
      min-width: 4.5rem;
      margin-left: auto;
      white-space: nowrap;
      background: transparent;
      border: 1px solid transparent;
      border-radius: var(--ss-radius-sm);
      padding: 4px 6px;
      cursor: pointer;
    }
    .paint-use:hover {
      color: var(--ss-text);
      border-color: var(--ss-border);
    }
    .paint-use.open {
      color: var(--ss-accent);
    }
    .paint-use .chev {
      display: inline-flex;
      transition: transform 120ms ease;
    }
    .paint-use.open .chev {
      transform: rotate(90deg);
    }
    /* A color row's expanded fixture list — reads as detail of that row,
       not a second floating section, per the unified single-row design. */
    .palette-row-fixtures {
      margin: -2px 0 4px 26px;
      padding-left: 10px;
      border-left: 2px solid var(--ss-border-soft);
    }
    .palette-row-fixtures .hint {
      margin: 4px 0;
    }
    .assign-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .assign-cluster {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .assign-segments {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .assign-fold {
      appearance: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 32px;
      min-width: 32px;
      height: 44px;
      margin: 0;
      padding: 0;
      border: 0;
      background: transparent;
      color: var(--ss-text-faint);
      cursor: pointer;
      flex: 0 0 auto;
    }
    .assign-fold svg {
      transition: transform 120ms ease;
    }
    .assign-cluster.open > .assign-row .assign-fold svg {
      transform: rotate(90deg);
    }
    .assign-name-meta {
      margin-left: 6px;
      font-size: 11px;
      font-weight: 650;
      letter-spacing: 0.04em;
      color: var(--ss-text-faint);
    }
    .assign-row {
      position: relative;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      overflow: hidden;
      border-radius: 10px;
      min-height: 40px;
    }
    .assign-aura {
      position: absolute;
      inset: 0;
      z-index: 0;
      pointer-events: none;
      border-radius: inherit;
      transition: opacity 220ms ease;
    }
    @media (prefers-reduced-motion: reduce) {
      .assign-aura {
        transition: none;
      }
    }
    .assign-row > *:not(.assign-aura) {
      position: relative;
      z-index: 1;
    }
    .assign-main {
      appearance: none;
      display: flex;
      align-items: center;
      gap: 8px;
      flex: 1;
      min-width: 0;
      min-height: 40px;
      padding: 6px 10px;
      border: 0;
      background: transparent;
      color: inherit;
      font: inherit;
      text-align: left;
      cursor: pointer;
    }
    .assign-row:hover .assign-main {
      background: color-mix(in srgb, var(--ss-text) 6%, transparent);
    }
    .assign-row.open .assign-main {
      box-shadow: inset 2px 0 0 var(--ss-accent);
    }
    .assign-main:disabled {
      cursor: default;
      opacity: 0.55;
    }
    .assign-name {
      font-size: 13px;
      min-width: 0;
      flex: 1;
    }
    .assign-mode {
      font-size: 11px;
      font-weight: 650;
      color: var(--ss-text-faint);
      letter-spacing: 0.02em;
    }
    .assign-mode.custom {
      color: var(--ss-text-dim);
    }
    .assign-picks {
      display: none;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      width: 100%;
      padding: 2px 10px 10px;
    }
    .assign-row.open .assign-picks {
      display: flex;
    }
    .assign-picks .label {
      font-size: 11px;
      font-weight: 650;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--ss-text-faint);
      margin-right: 4px;
    }
    .assign-picks input[type="color"] {
      appearance: none;
      width: 32px;
      height: 32px;
      min-width: 32px;
      min-height: 32px;
      padding: 0;
      border: 1px solid var(--ss-border);
      border-radius: 8px;
      background: var(--ss-bg);
      cursor: pointer;
      overflow: hidden;
    }
    .assign-picks input[type="color"]::-webkit-color-swatch-wrapper {
      padding: 0;
    }
    .assign-picks input[type="color"]::-webkit-color-swatch {
      border: none;
      border-radius: 7px;
    }
    .assign-picks input.hex {
      width: 96px;
      font-family: var(--ss-font-mono);
      font-size: 15px;
    }
    .assign-auto {
      appearance: none;
      min-height: 32px;
      padding: 5px 12px;
      border-radius: 8px;
      border: 1px dashed var(--ss-border);
      background: transparent;
      color: var(--ss-text-dim);
      font-size: 13px;
      font-weight: 650;
      cursor: pointer;
    }
    .assign-auto.selected,
    .assign-auto:hover {
      border-color: var(--ss-accent);
      color: var(--ss-text);
    }
    .paint-swatch {
      appearance: none;
      width: 32px;
      height: 32px;
      min-width: 32px;
      min-height: 32px;
      padding: 0;
      border-radius: 8px;
      border: 1px solid var(--ss-border);
      cursor: pointer;
      flex: 0 0 auto;
      box-shadow: inset 0 0 0 1px color-mix(in srgb, #000 22%, transparent);
    }
    .paint-swatch:hover,
    .paint-swatch.selected {
      border-color: var(--ss-accent);
      box-shadow: 0 0 0 2px color-mix(in srgb, var(--ss-accent) 55%, transparent);
    }
    .paint-swatch.custom {
      border-radius: 4px;
    }
    .motion-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      align-items: stretch;
    }
    .motion-row label {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 0;
      padding: 12px 14px;
      font-size: 15px;
      font-weight: 650;
      color: var(--ss-text);
      cursor: pointer;
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border);
      border-radius: 12px;
    }
    .motion-row input[type="radio"] {
      appearance: none;
      width: 18px;
      height: 18px;
      margin: 0;
      flex: 0 0 auto;
      border: 1.5px solid var(--ss-border);
      border-radius: 50%;
      background: var(--ss-bg);
    }
    .motion-row input[type="radio"]:checked {
      border-color: var(--ss-accent);
      box-shadow: inset 0 0 0 4px var(--ss-accent);
    }
    .motion-row label span {
      display: flex;
      flex-direction: column;
      gap: 1px;
      min-width: 0;
    }
    .motion-row label small {
      font-size: 12px;
      font-weight: 500;
      color: var(--ss-text-faint);
    }
    .motion-row label:has(input:checked) {
      border-color: color-mix(in srgb, var(--ss-accent) 55%, var(--ss-border));
      background: color-mix(in srgb, var(--ss-accent) 14%, var(--ss-surface-2));
    }
    .slider-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 6px;
      padding: 6px 10px;
      font-size: 13px;
      color: var(--ss-text-dim);
      background: var(--ss-surface-2);
      border: 1px solid var(--ss-border-soft);
      border-radius: 12px;
    }
    .slider-row input[type="range"] {
      flex: 1;
      max-width: none;
    }
    .slider-row input[type="number"] {
      width: 3.75rem;
      padding: 4px 8px;
      text-align: right;
    }
    .slider-row .val {
      font-family: var(--ss-font-mono);
      font-size: 13px;
      color: var(--ss-text-dim);
      min-width: 34px;
    }
    .slider-row .ends {
      font-size: 11px;
      color: var(--ss-text-faint);
    }
    .override-list {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .override {
      border: 1px solid var(--ss-border-soft);
      border-radius: 14px;
      background: var(--ss-surface-2);
    }
    .override > summary {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 12px;
      min-height: var(--ss-control-h);
      box-sizing: border-box;
      cursor: pointer;
      font-size: 15px;
      list-style: none;
    }
    .override > summary::-webkit-details-marker {
      display: none;
    }
    .override > summary .fname {
      font-weight: 600;
    }
    .override > summary .fid {
      display: none;
    }
    .override > summary .summary {
      font-size: 13px;
      color: var(--ss-text-faint);
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .override .body {
      padding: 4px 12px 12px;
    }
    .override-add {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      font-size: 14px;
      color: var(--ss-text-dim);
      flex-wrap: wrap;
    }
    .override-add select {
      min-width: 220px;
    }
    .orphan-overrides {
      margin-top: 8px;
      border-top: 1px dashed var(--ss-border-soft);
      padding-top: 6px;
    }
    .orphan-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      border: 1px solid var(--ss-border-soft);
      border-radius: 12px;
      background: var(--ss-surface-2);
      font-size: 14px;
    }
    .orphan-row .fname {
      font-weight: 600;
      font-family: var(--ss-font-mono);
      font-size: 13px;
    }
    .orphan-row .fid {
      flex: 1;
      color: var(--ss-text-faint);
      font-size: 13px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .advanced-warn {
      border: 1px solid var(--ss-warn);
      background: var(--ss-warn-dim);
      color: var(--ss-warn);
      border-radius: var(--ss-radius);
      padding: 8px 10px;
      font-size: 14px;
      margin-bottom: 10px;
      display: flex;
      gap: 8px;
      align-items: flex-start;
    }
    .advanced-warn ul {
      margin: 4px 0 0;
      padding-left: 18px;
    }
    .error-box {
      border: 1px solid var(--ss-err);
      background: var(--ss-err-dim);
      color: var(--ss-err);
      border-radius: var(--ss-radius);
      padding: 8px 10px;
      font-size: 14px;
      margin-bottom: 10px;
    }
    .field-error {
      color: var(--ss-err);
      font-size: 13px;
      margin-top: 4px;
    }
    .preserved {
      border: 1px solid var(--ss-border-soft);
      background: var(--ss-surface-2);
      color: var(--ss-text-faint);
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 13px;
    }
    .preview-panel {
      border: 1px solid var(--ss-border-soft);
      border-radius: var(--ss-radius);
      padding: 10px 12px;
      margin-bottom: 10px;
      background: var(--ss-surface-3);
    }
    .preview-panel .pv-head {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 14px;
      font-weight: 600;
      margin-bottom: 6px;
    }
    .preview-panel .pv-head.ok {
      color: var(--ss-ok);
    }
    .preview-panel .pv-head.warn {
      color: var(--ss-warn);
    }
    .preview-panel .pv-head.bad {
      color: var(--ss-err);
    }
    .pv-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 18px;
      font-size: 14px;
      color: var(--ss-text-dim);
      margin: 6px 0;
    }
    .pv-grid .mono {
      font-family: var(--ss-font-mono);
      font-size: 11px;
    }
    .pv-grid .warn {
      color: var(--ss-warn);
    }
    .pv-grid .bad {
      color: var(--ss-err);
    }
    .pv-providers {
      display: flex;
      flex-wrap: wrap;
      gap: 6px 14px;
      font-size: 13px;
      color: var(--ss-text-faint);
      margin-bottom: 6px;
    }
    .preview-panel details {
      font-size: 13px;
      color: var(--ss-text-dim);
    }
    .preview-panel details summary {
      cursor: pointer;
      color: var(--ss-text-faint);
    }
    .preview-panel details li.warn {
      color: var(--ss-warn);
    }
    .preview-panel details li.bad {
      color: var(--ss-err);
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-top: 8px;
      flex-wrap: wrap;
      padding: 4px 0 8px;
    }
    .actions .spacer {
      flex: 1;
    }
    .actions .primary {
      background: color-mix(in srgb, var(--ss-accent) 22%, var(--ss-surface-2));
      border-color: color-mix(in srgb, var(--ss-accent) 70%, var(--ss-border));
      color: var(--ss-text);
    }
    .dirty-tag {
      font-size: 11px;
      font-weight: 650;
      color: var(--ss-warn);
      border: 1px solid var(--ss-warn);
      border-radius: 999px;
      padding: 1px 8px;
    }
    @container ss-builder (max-width: 700px) {
      .state-row .prop {
        min-width: 100%;
      }
      .palette-row .hex {
        width: 74px;
      }
      .look-hero {
        grid-template-columns: 1fr;
      }
      .motion-row {
        grid-template-columns: 1fr;
      }
    }
    /* Touch/mobile (same query as the token block in tokens.css): native
       form controls need finger-scale hit areas, and text inputs at >=16px
       stop iOS Safari zooming the viewport on focus. Sliders get fatter
       tracks/thumbs; palette swatches get finger-sized tap targets. */
    @media (max-width: 640px), (pointer: coarse) {
      input[type="text"],
      input[type="number"],
      select {
        font-size: 16px;
      }
      input[type="checkbox"] {
        width: 24px;
        height: 24px;
        border-radius: 8px;
      }
      input[type="range"] {
        height: 8px;
      }
      input[type="range"]::-webkit-slider-runnable-track {
        height: 8px;
      }
      input[type="range"]::-webkit-slider-thumb {
        width: 24px;
        height: 24px;
        margin-top: -8px;
      }
      input[type="range"]::-moz-range-track {
        height: 8px;
      }
      input[type="range"]::-moz-range-thumb {
        width: 24px;
        height: 24px;
      }
      .target-row {
        min-height: 44px;
        font-size: 15px;
      }
      .assign-main,
      .assign-name {
        font-size: 15px;
      }
      .assign-row,
      .assign-main {
        min-height: 44px;
      }
      .assign-auto {
        min-height: 44px;
        font-size: 15px;
      }
      .palette-row input[type="color"],
      .assign-picks input[type="color"],
      .paint-swatch {
        width: 38px;
        height: 38px;
        min-width: 38px;
        min-height: 38px;
      }
      .look-control input[type="color"] {
        width: 40px;
        height: 40px;
      }
      .state-row input[type="color"] {
        width: 36px;
        height: 32px;
      }
      .palette-row .row-btn {
        width: 44px;
        min-width: 44px;
      }
    }
    /* Visible keyboard focus for every Builder control (Builder-expansion
       §9) — the editor is keyboard-navigable end to end (targets, palette,
       defaults, overrides, actions). */
    :scope :is(button, input, select, summary, details):focus-visible {
      outline: 2px solid var(--ss-accent);
      outline-offset: 1px;
    }
    }
  `;

  constructor() {
    super();
    this.store = null;
    this._unsub = null;
    this._clearPaletteArmed = false;
    this._assigningFixtureId = null;
    this._expandedAssignClusters = new Set();
    this._expandedPaletteIndex = null;
  }

  connectedCallback() {
    super.connectedCallback();
    if (this.store) this._unsub = this.store.subscribe(() => this.requestUpdate());
  }

  disconnectedCallback() {
    if (this._unsub) this._unsub();
    this._unsub = null;
    super.disconnectedCallback();
  }

  // ---- draft plumbing ------------------------------------------------------

  #patch(patch) {
    this.store.patchBuilderDraft(patch);
  }

  #stateFor(scope) {
    const draft = this.store.state.builder.draft;
    if (scope === "default") return draft.default_state || {};
    return (draft.fixture_states || {})[scope] || {};
  }

  #patchState(scope, patch) {
    if (scope === "default") this.store.patchBuilderDefaultState(patch);
    else this.store.patchBuilderOverride(scope, patch);
  }

  #onNameInput(e) {
    this.#patch({ name: e.target.value });
  }

  #onTargetToggle(e) {
    const input = e.target;
    if (!input || input.type !== "checkbox") return;
    const b = this.store.state.builder;
    const current = Array.isArray(b.draft.target_ids) ? b.draft.target_ids : [];
    if (input.classList.contains("builder-target-set")) {
      const ids = (input.dataset.ids || "").split(",").filter(Boolean);
      this.#patch({ target_ids: toggleIdSet(current, ids, input.checked) });
      return;
    }
    if (!input.classList.contains("builder-target")) return;
    const id = input.value;
    const next = input.checked
      ? [...current.filter((t) => t !== id), id]
      : current.filter((t) => t !== id);
    this.#patch({ target_ids: next });
  }

  #stopToggle(e) {
    e.stopPropagation();
  }

  #setIds(fixtures) {
    return (fixtures || []).map((f) => f.id).filter(Boolean);
  }

  #renderFixturePick(fixture, selected) {
    const note = fixtureHealthNote(fixture);
    return html`
      <label class="target-row">
        <input
          class="builder-target"
          type="checkbox"
          value=${fixture.id}
          .checked=${selected.includes(fixture.id)}
        />
        <span class="name">${fixture.name || fixture.id}</span>
        ${note ? html`<span class="meta warn">${note.text}</span>` : ""}
      </label>
    `;
  }

  #renderClusterPick(item, selected) {
    const ids = this.#setIds(item.fixtures);
    const sel = selectionOf(selected, ids);
    const mixed = item.fixtures.some((f) => (f.health || "ready") !== (item.fixtures[0].health || "ready"));
    return html`
      <details class="target-fold target-cluster">
        <summary>
          <span class="chev">${iconChevronRight(14)}</span>
          <label class="pick" @click=${this.#stopToggle}>
            <input
              class="builder-target-set"
              type="checkbox"
              data-ids=${ids.join(",")}
              .checked=${sel.checked}
              .indeterminate=${sel.indeterminate}
              aria-label=${controllerDisplayName(item.fixtures)}
            />
            <span class="name">${controllerDisplayName(item.fixtures)}</span>
            <span class="meta ${mixed ? "warn" : ""}">${clusterLabel(item.fixtures)}</span>
          </label>
        </summary>
        <div class="body">
          ${item.fixtures.map((f) => this.#renderFixturePick({ ...f, name: segmentDisplayName(f) }, selected))}
        </div>
      </details>
    `;
  }

  #renderGroupedFixtures(fixtures, selected) {
    return withControllerClusters(fixtures).map((item) =>
      item.type === "cluster"
        ? this.#renderClusterPick(item, selected)
        : this.#renderFixturePick(item.fixture, selected)
    );
  }

  #renderEcoGroup(node, selected, { open = false, parentName = "" } = {}) {
    const ids = this.#setIds(node.fixtures);
    const sel = selectionOf(selected, ids);
    const ready = node.fixtures.filter((f) => f.health === "ready").length;
    const warn = ready !== node.fixtures.length;
    const meta = warn ? `${ready}/${node.fixtures.length}` : String(node.fixtures.length);
    const label = parentName ? nestedGroupLabel(parentName, node.name) : node.name;
    return html`
      <details class="target-fold target-eco" ?open=${open}>
        <summary>
          <span class="chev">${iconChevronRight(14)}</span>
          <label class="pick" @click=${this.#stopToggle}>
            <input
              class="builder-target-set"
              type="checkbox"
              data-ids=${ids.join(",")}
              .checked=${sel.checked}
              .indeterminate=${sel.indeterminate}
              aria-label=${label}
            />
            <span class="name">${label}</span>
            <span class="meta ${warn ? "warn" : ""}">${meta}</span>
          </label>
        </summary>
        <div class="body">
          ${node.children.map((child) => this.#renderEcoGroup(child, selected, { parentName: node.name }))}
          ${this.#renderGroupedFixtures(node.directFixtures, selected)}
        </div>
      </details>
    `;
  }

  /**
   * ONE delegated handler for every default/override state control: each
   * control carries `data-scope` ("default" or a fixture id) and
   * `data-field`; `data-prop` names the property for the set/unset toggles.
   */
  #onStateInput(e) {
    const el = e.target;
    const scope = el.dataset ? el.dataset.scope : null;
    const field = el.dataset ? el.dataset.field : null;
    if (!scope || !field) return;
    if (field === "set") {
      const prop = el.dataset.prop;
      if (el.checked) this.#patchState(scope, { [prop]: PROPERTY_SEEDS[prop] });
      else this.#patchState(scope, { [prop]: null });
      return;
    }
    if (field === "on") {
      this.#patchState(scope, { on: el.value === "" ? null : el.value === "on" });
      return;
    }
    if (field === "color") {
      let value = String(el.value || "").trim();
      if (value && !value.startsWith("#")) value = `#${value}`;
      if (value) this.#patchState(scope, { color: value.toLowerCase() });
      return;
    }
    if (field === "brightness") {
      if (el.value === "") return;
      this.#patchState(scope, { brightness: Math.max(0, Math.min(100, Number(el.value))) });
      return;
    }
    if (field === "color_temp_mirek") {
      if (el.value === "") return;
      this.#patchState(scope, {
        color_temp_mirek: Math.max(100, Math.min(1000, Math.round(Number(el.value)))),
      });
    }
  }

  // ---- palette -------------------------------------------------------------

  #onColorInput(e) {
    const index = Number(e.target.dataset.index);
    const palette = [...this.store.state.builder.draft.palette];
    palette[index] = e.target.value.toLowerCase();
    this.#patch({ palette });
  }

  #onHexInput(e) {
    const index = Number(e.target.dataset.index);
    const palette = [...this.store.state.builder.draft.palette];
    let value = e.target.value.trim();
    if (value && !value.startsWith("#")) value = `#${value}`;
    palette[index] = value.toLowerCase();
    this.#patch({ palette });
  }

  #moveColor(index, delta) {
    const palette = [...this.store.state.builder.draft.palette];
    const target = index + delta;
    if (target < 0 || target >= palette.length) return;
    [palette[index], palette[target]] = [palette[target], palette[index]];
    this.#patchPaletteAndPins(palette, (pin) => {
      if (pin === index) return target;
      if (pin === target) return index;
      return pin;
    });
  }

  #removeColor(index) {
    const palette = [...this.store.state.builder.draft.palette];
    palette.splice(index, 1);
    this.#patchPaletteAndPins(palette, (pin) => {
      if (pin === index) return null;
      if (pin > index) return pin - 1;
      return pin;
    });
  }

  #duplicateColor(index) {
    const palette = [...this.store.state.builder.draft.palette];
    palette.splice(index + 1, 0, palette[index]);
    this.#patchPaletteAndPins(palette, (pin) => (pin > index ? pin + 1 : pin));
  }

  #addColor() {
    const palette = [...this.store.state.builder.draft.palette];
    const next = STARTER_COLORS[palette.length % STARTER_COLORS.length];
    palette.push(next);
    this.#patch({ palette });
  }

  #clearPalette() {
    if (!this._clearPaletteArmed) {
      this._clearPaletteArmed = true;
      this.requestUpdate();
      return;
    }
    this._clearPaletteArmed = false;
    this._assigningFixtureId = null;
    this.#patchPaletteAndPins([], () => null);
  }

  #cancelClearPalette() {
    this._clearPaletteArmed = false;
    this.requestUpdate();
  }

  #patchPaletteAndPins(palette, remapPin) {
    const states = { ...(this.store.state.builder.draft.fixture_states || {}) };
    for (const [fixtureId, state] of Object.entries(states)) {
      if (state.palette_index === undefined || state.palette_index === null) continue;
      const nextIndex = remapPin(state.palette_index);
      const next = { ...state };
      if (nextIndex === null || nextIndex === undefined) delete next.palette_index;
      else next.palette_index = nextIndex;
      if (Object.keys(next).length === 0) delete states[fixtureId];
      else states[fixtureId] = next;
    }
    this.#patch({ palette, fixture_states: states });
  }

  #pinFixture(fixtureId, index) {
    this.#pinFixtures([fixtureId], index);
  }

  #unpinFixture(fixtureId) {
    this.#unpinFixtures([fixtureId]);
  }

  #pinFixtures(fixtureIds, index) {
    const states = { ...(this.store.state.builder.draft.fixture_states || {}) };
    for (const fixtureId of fixtureIds || []) {
      const next = { ...(states[fixtureId] || {}), palette_index: index };
      delete next.color;
      delete next.gradient;
      states[fixtureId] = next;
    }
    this.#patch({ fixture_states: states });
  }

  #colorFixtures(fixtureIds, hex) {
    const states = { ...(this.store.state.builder.draft.fixture_states || {}) };
    for (const fixtureId of fixtureIds || []) {
      const next = { ...(states[fixtureId] || {}), color: hex };
      delete next.palette_index;
      delete next.gradient;
      states[fixtureId] = next;
    }
    this.#patch({ fixture_states: states });
  }

  #unpinFixtures(fixtureIds) {
    const states = { ...(this.store.state.builder.draft.fixture_states || {}) };
    for (const fixtureId of fixtureIds || []) {
      if (!states[fixtureId]) continue;
      const next = { ...states[fixtureId] };
      delete next.palette_index;
      delete next.color;
      delete next.gradient;
      if (Object.keys(next).length === 0) delete states[fixtureId];
      else states[fixtureId] = next;
    }
    this.#patch({ fixture_states: states });
  }

  #toggleAssigning(assignKey) {
    this._assigningFixtureId = this._assigningFixtureId === assignKey ? null : assignKey;
    this.requestUpdate();
  }

  #toggleAssignCluster(deviceId, e) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    if (this._expandedAssignClusters.has(deviceId)) {
      this._expandedAssignClusters.delete(deviceId);
    } else {
      this._expandedAssignClusters.add(deviceId);
      if (this._assigningFixtureId === `cluster:${deviceId}`) this._assigningFixtureId = null;
    }
    this.requestUpdate();
  }

  #chooseFixtureColor(assignKey, index, applyIds) {
    const ids = applyIds && applyIds.length ? applyIds : [assignKey];
    if (index === null) this.#unpinFixtures(ids);
    else this.#pinFixtures(ids, index);
    this._assigningFixtureId = assignKey;
    this.requestUpdate();
  }

  #chooseFixtureHex(assignKey, hex, applyIds) {
    const ids = applyIds && applyIds.length ? applyIds : [assignKey];
    const palette = this.store.state.builder.draft.palette || [];
    const match = palette.findIndex((color) => String(color).toLowerCase() === hex);
    if (match >= 0) this.#pinFixtures(ids, match);
    else this.#colorFixtures(ids, hex);
    this._assigningFixtureId = assignKey;
    this.requestUpdate();
  }

  #spreadPalette() {
    const states = { ...(this.store.state.builder.draft.fixture_states || {}) };
    for (const [fixtureId, state] of Object.entries(states)) {
      const next = { ...state };
      delete next.palette_index;
      delete next.color;
      delete next.gradient;
      if (Object.keys(next).length === 0) delete states[fixtureId];
      else states[fixtureId] = next;
    }
    this._assigningFixtureId = null;
    this.#patch({ fixture_states: states });
  }

  #assignmentAura(draft, fixture, assignment) {
    if (!fixtureTakesRgb(fixture)) return null;
    const states = draft.fixture_states || {};
    const isOverride = Object.prototype.hasOwnProperty.call(states, fixture.id);
    const state = isOverride ? states[fixture.id] : draft.default_state || {};
    if (state && state.on === false) return null;
    const brightness =
      typeof draft.brightness === "number"
        ? draft.brightness
        : typeof state.brightness === "number"
          ? state.brightness
          : 60;
    const displayColors = assignment.mixed
      ? (assignment.colors || []).slice(0, 3)
      : assignment.custom && state && Array.isArray(state.gradient) && state.gradient.length
        ? state.gradient
        : assignment.color
          ? [assignment.color]
          : [];
    if (!displayColors.length) return null;
    return auraBackground({
      enabled: fixture.enabled !== false,
      available: true,
      on: true,
      brightness,
      displayColors,
      freshness: "fresh",
    });
  }

  /**
   * Palette authoring — Static assigns fixtures to slots; Dynamic cycles
   * them. `showAssignment` covers both palette-driven motion modes (static
   * and palette_cycle) — a fixture's palette slot means the same thing
   * (which color it shows / starts its cycle phase on) in either, so
   * there is one assignment UI, not a static-only one. Each color row's
   * "N lights" toggle expands in place to the fixtures currently on that
   * slot — see `#paletteBuckets` — instead of a second, always-visible,
   * room-grouped list living below the whole palette.
   */
  #renderPalette(draft, { showAssignment = false, resolved = [] } = {}) {
    const colors = draft.palette || [];
    const usage = showAssignment ? paletteUsageCounts(draft, resolved) : [];
    const buckets = showAssignment ? this.#paletteBuckets(draft, resolved, colors) : null;
    const rgbCount = resolved.filter((fixture) => fixtureTakesRgb(fixture)).length;
    const hint = !showAssignment
      ? "Colors this scene shows, in order."
      : !resolved.length
        ? "Select fixtures in Targets, then tap a color's light count to assign it."
        : !rgbCount
          ? "These lights don't take RGB palette colors."
          : colors.length < 2
            ? "Add another color if you want lights to differ, then tap its light count to assign it."
            : "Tap a color's light count to see or change which lights use it.";
    return html`
      <div class="palette-panel" id="builder-palette">
        <label class="field-label">Palette</label>
        <p class="hint">${hint}</p>
        ${colors.length
          ? html`<div class="palette-rows">
              ${colors.map(
                (color, index) => html`
                  <div class="palette-row">
                    <span class="ord">${index + 1}</span>
                    <input
                      class="builder-palette-color"
                      type="color"
                      data-index=${index}
                      .value=${/^#[0-9a-f]{6}$/.test(color) ? color : "#000000"}
                      @input=${this.#onColorInput}
                    />
                    <input
                      class="hex builder-palette-hex ${this.#errorFor("scene.palette") ? "invalid" : ""}"
                      type="text"
                      data-index=${index}
                      .value=${color}
                      maxlength="7"
                      spellcheck="false"
                      @input=${this.#onHexInput}
                    />
                    ${showAssignment
                      ? html`<button
                          type="button"
                          class="paint-use ${this._expandedPaletteIndex === index ? "open" : ""}"
                          aria-expanded=${this._expandedPaletteIndex === index ? "true" : "false"}
                          title="See or change which lights use this color"
                          @click=${() => this.#togglePaletteRow(index)}
                        >
                          <span class="chev">${iconChevronRight(12)}</span>
                          ${usage[index] ? `${usage[index]} light${usage[index] === 1 ? "" : "s"}` : "Unused"}
                        </button>`
                      : ""}
                    <button
                      class="row-btn builder-move-up"
                      data-index=${index}
                      title="Move color up"
                      aria-label="Move color ${index + 1} up"
                      ?disabled=${index === 0}
                      @click=${() => this.#moveColor(index, -1)}
                    >
                      ↑
                    </button>
                    <button
                      class="row-btn builder-move-down"
                      data-index=${index}
                      title="Move color down"
                      aria-label="Move color ${index + 1} down"
                      ?disabled=${index === colors.length - 1}
                      @click=${() => this.#moveColor(index, 1)}
                    >
                      ↓
                    </button>
                    <button
                      class="row-btn builder-duplicate-color"
                      data-index=${index}
                      title="Duplicate color"
                      aria-label="Duplicate color ${index + 1}"
                      @click=${() => this.#duplicateColor(index)}
                    >
                      ⧉
                    </button>
                    <button
                      class="row-btn builder-remove-color"
                      data-index=${index}
                      title="Remove color"
                      aria-label="Remove color ${index + 1}"
                      @click=${() => this.#removeColor(index)}
                    >
                      ✕
                    </button>
                  </div>
                  ${showAssignment && this._expandedPaletteIndex === index
                    ? this.#renderPaletteRowFixtures(draft, resolved, colors, buckets.byIndex.get(index) || [])
                    : ""}
                `
              )}
            </div>
            <div
              class="palette-preview"
              id="builder-palette-preview"
              style=${`background: linear-gradient(90deg, ${colors.join(", ")})`}
            ></div>
            <div class="palette-actions">
              <button id="builder-add-color" class="row-btn" @click=${() => this.#addColor()}>+ Add color</button>
              ${this._clearPaletteArmed
                ? html`
                    <button
                      id="builder-clear-palette-confirm"
                      class="row-btn builder-clear-confirm"
                      @click=${() => this.#clearPalette()}
                    >
                      Confirm clear
                    </button>
                    <button
                      id="builder-clear-palette-cancel"
                      class="row-btn"
                      @click=${() => this.#cancelClearPalette()}
                    >
                      Cancel
                    </button>
                  `
                : html`<button
                    id="builder-clear-palette"
                    class="row-btn"
                    @click=${() => this.#clearPalette()}
                  >
                    Clear
                  </button>`}
              ${showAssignment
                ? html`<button
                    type="button"
                    id="builder-spread-palette"
                    class="row-btn"
                    title="Put every light back on automatic palette colors"
                    ?disabled=${!rgbCount}
                    @click=${() => this.#spreadPalette()}
                  >
                    Automatic for all
                  </button>`
                : ""}
            </div>
            ${showAssignment && buckets.other.length
              ? html`
                  <div class="section-head">
                    <span class="field-label">Other lights</span>
                    <span class="count">${buckets.other.length}</span>
                  </div>
                  <p class="hint">Custom colors and lights that can't take a palette color.</p>
                  ${this.#renderAssignList(draft, resolved, colors, buckets.other)}
                `
              : ""}`
          : html`
              <button id="builder-add-color" class="palette-empty" @click=${() => this.#addColor()}>
                ${showAssignment ? "+ Add colors, then tap a light to choose" : "+ Add the first palette color"}
              </button>
            `}
        ${this.#errorFor("scene.palette")
          ? html`<div class="field-error">${this.#errorFor("scene.palette").message}</div>`
          : ""}
      </div>
    `;
  }

  #togglePaletteRow(index) {
    this._expandedPaletteIndex = this._expandedPaletteIndex === index ? null : index;
    this.requestUpdate();
  }

  /**
   * Bucket every resolved fixture/cluster by its CURRENT palette slot
   * (`resolveFixturePalette`/`resolveClusterPalette` — pinned or
   * auto-spread, same resolution the renderer uses). Auto-spread is
   * computed per FIXTURE, not per cluster, so a WLED controller's segments
   * frequently disagree before anyone explicitly pins them together; a
   * `mixed` cluster goes to `other` as ONE row (its existing combined
   * picker + segment fold still works there) rather than being exploded —
   * that keeps "pin the whole cluster at once" available exactly when a
   * fresh, still-mixed cluster needs it most. Anything else with no single
   * palette index (custom hex, multi-stop gradient, or a fixture that
   * cannot take RGB at all) also goes in `other` — nothing is ever
   * silently dropped.
   * @returns {{byIndex: Map<number, object[]>, other: object[]}}
   */
  #paletteBuckets(draft, resolved, colors) {
    const byIndex = new Map(colors.map((_, index) => [index, []]));
    const other = [];
    const place = (item, index) => {
      if (index != null && byIndex.has(index)) byIndex.get(index).push(item);
      else other.push(item);
    };
    for (const item of withControllerClusters(resolved)) {
      if (item.type === "cluster") {
        const assignment = resolveClusterPalette(draft, item.fixtures, resolved);
        place(item, assignment.mixed ? null : assignment.index);
        continue;
      }
      place(item, resolveFixturePalette(draft, item.fixture, resolved).index);
    }
    return { byIndex, other };
  }

  /** One color row's expanded fixture list, or an explanatory empty state. */
  #renderPaletteRowFixtures(draft, resolved, colors, items) {
    return html`
      <div class="palette-row-fixtures">
        ${items.length
          ? this.#renderAssignList(draft, resolved, colors, items)
          : html`<p class="hint">
              No lights use this color yet. Pick it from any other light's color picker to move it here.
            </p>`}
      </div>
    `;
  }

  #renderAssignList(draft, resolved, colors, items) {
    return html`
      <div class="assign-list">
        ${items.map((item) =>
          item.type === "cluster"
            ? this.#renderAssignCluster(draft, item, resolved, colors)
            : this.#renderAssignRow(draft, item.fixture, resolved, colors)
        )}
      </div>
    `;
  }

  #renderAssignCluster(draft, item, resolved, colors) {
    const members = item.fixtures || [];
    const deviceId = item.deviceId || members.map((fixture) => fixture.id).join("+");
    const assignKey = `cluster:${deviceId}`;
    const applyIds = members.map((fixture) => fixture.id).filter(Boolean);
    const assignment = resolveClusterPalette(draft, members, resolved);
    const proxy = members.find((fixture) => fixtureTakesRgb(fixture)) || members[0];
    const expanded = this._expandedAssignClusters.has(deviceId);
    if (!proxy) return "";
    return html`
      <div class="assign-cluster ${expanded ? "open" : ""}" data-cluster=${deviceId}>
        ${this.#renderAssignRow(draft, proxy, resolved, colors, {
          name: controllerDisplayName(members),
          nameMeta: clusterLabel(members),
          assignKey,
          applyIds,
          assignment,
          hidePicks: expanded,
          fold: {
            expanded,
            controlsId: `assign-segments-${deviceId}`,
            onToggle: (e) => this.#toggleAssignCluster(deviceId, e),
            label: expanded ? `Hide ${controllerDisplayName(members)} segments` : `Show ${controllerDisplayName(members)} segments`,
          },
        })}
        ${expanded
          ? html`<div class="assign-segments" id=${`assign-segments-${deviceId}`}>
              ${members.map((fixture) =>
                this.#renderAssignRow(draft, { ...fixture, name: segmentDisplayName(fixture) }, resolved, colors)
              )}
            </div>`
          : ""}
      </div>
    `;
  }

  #renderAssignRow(draft, fixture, resolved, colors, options = {}) {
    const assignKey = options.assignKey || fixture.id;
    const applyIds = options.applyIds && options.applyIds.length ? options.applyIds : [fixture.id];
    const assignment = options.assignment || resolveFixturePalette(draft, fixture, resolved);
    const name = options.name || fixture.name || roomLabel(fixture.id);
    const nameMeta = options.nameMeta || "";
    const fold = options.fold || null;
    const hidePicks = !!options.hidePicks;
    const rgb = fixtureTakesRgb(fixture);
    const mixed = !!assignment.mixed;
    const mode = !rgb ? "none" : mixed ? "mixed" : assignment.custom ? "custom" : assignment.pinned ? "pinned" : assignment.index != null ? "auto" : "none";
    const open = rgb && !hidePicks && this._assigningFixtureId === assignKey;
    const title = !rgb ? "This light cannot take a palette color" : "Choose a color for this light";
    const aura = this.#assignmentAura(draft, fixture, assignment);
    const currentHex = /^#[0-9a-f]{6}$/i.test(assignment.color || "") ? assignment.color : "#ffffff";
    const selectedIndex = !mixed && assignment.pinned ? assignment.index : null;
    const autoSelected = !mixed && mode === "auto";
    return html`
      <div class="assign-row ${open ? "open" : ""}" data-fixture=${assignKey} data-mode=${mode}>
        <div class="assign-aura" style=${aura ? `background:${aura};opacity:1` : "opacity:0"} aria-hidden="true"></div>
        ${fold
          ? html`<button
              type="button"
              class="assign-fold builder-assign-fold"
              data-cluster-fold=""
              title=${fold.label}
              aria-label=${fold.label}
              aria-expanded=${fold.expanded ? "true" : "false"}
              aria-controls=${fold.controlsId || ""}
              @click=${fold.onToggle}
            >
              ${iconChevronRight(14)}
            </button>`
          : ""}
        <button
          type="button"
          class="assign-main builder-assign-chip"
          data-fixture=${assignKey}
          ?disabled=${!rgb}
          title=${title}
          aria-label=${`Choose color for ${name}`}
          aria-expanded=${open ? "true" : "false"}
          @click=${() => {
            if (hidePicks) return;
            this.#toggleAssigning(assignKey);
          }}
        >
          <span class="assign-name">${name}${nameMeta ? html`<span class="assign-name-meta">${nameMeta}</span>` : ""}</span>
        </button>
        ${rgb && !hidePicks
          ? html`
        <div class="assign-picks" role="group" aria-label=${`Color for ${name}`}>
          <span class="label">Color</span>
          <input
            type="color"
            class="builder-assign-color"
            data-fixture=${assignKey}
            .value=${currentHex}
            @change=${(e) => this.#chooseFixtureHex(assignKey, e.target.value.toLowerCase(), applyIds)}
          />
          <input
            class="hex builder-assign-hex"
            type="text"
            data-fixture=${assignKey}
            .value=${currentHex}
            maxlength="7"
            spellcheck="false"
            @change=${(e) => {
              let value = String(e.target.value || "").trim();
              if (value && !value.startsWith("#")) value = `#${value}`;
              if (/^#[0-9a-f]{6}$/i.test(value)) this.#chooseFixtureHex(assignKey, value.toLowerCase(), applyIds);
            }}
          />
          <button
            type="button"
            class="assign-auto ${autoSelected ? "selected" : ""}"
            data-fixture=${assignKey}
            @click=${() => this.#chooseFixtureColor(assignKey, null, applyIds)}
          >
            Automatic
          </button>
          ${colors.map(
            (color, index) => html`
              <button
                type="button"
                class="paint-swatch builder-paint-swatch ${selectedIndex === index ? "selected" : ""}"
                data-index=${index}
                data-fixture=${assignKey}
                style=${`background:${color}`}
                title=${`Palette color ${index + 1}`}
                aria-label=${`Set ${name} to palette color ${index + 1}`}
                aria-pressed=${selectedIndex === index ? "true" : "false"}
                @click=${() => this.#chooseFixtureColor(assignKey, index, applyIds)}
              ></button>
            `
          )}
        </div>
          `
          : ""}
      </div>
    `;
  }

  // ---- motion --------------------------------------------------------------

  #onMotionChange(e) {
    if (!e.target.checked) return;
    const b = this.store.state.builder;
    const motion = { ...(b.draft.motion || {}) };
    if (e.target.value === "dynamic") {
      motion.mode = "palette_cycle";
      // An explicit Static/Dynamic choice intentionally replaces advanced
      // strategy intent (Builder-expansion §5); speed gets a usable value.
      motion.strategy = "auto";
      if (!(typeof motion.speed === "number" && motion.speed > 0)) motion.speed = 0.5;
      this._assigningFixtureId = null;
    } else {
      motion.mode = "static";
      motion.strategy = "auto";
      motion.speed = 0.0;
      this._clearPaletteArmed = false;
    }
    this.#patch({ motion });
  }

  #onSpeed(e) {
    const motion = { ...this.store.state.builder.draft.motion, speed: Number(e.target.value) };
    this.#patch({ motion });
  }

  #onPaletteBrightness(e) {
    const raw = e.target.value;
    if (raw === "") return;
    this.#patch({ brightness: Math.max(0, Math.min(100, Number(raw))) });
  }

  // ---- actions -------------------------------------------------------------

  #preview() {
    return this.store.previewBuilderDraft();
  }

  #stopPreview() {
    return this.store.stopBuilderPreview();
  }

  #save() {
    return this.store.saveBuilderDraft();
  }

  #cancel() {
    this.store.closeBuilder();
    this.store.setView("scenes");
  }

  /**
   * Map a backend error path to the nearest control context (or null).
   *
   * BOTH error surfaces are consulted: a SAVE failure (`b.error`) and a
   * PREVIEW validation failure (`b.previewError`). Preview is where most
   * canonical validation actually surfaces (it runs the same Scene v2
   * parser), so reading only save errors left the palette/motion/defaults
   * field highlighting dead. `patchBuilderDraft` already clears the
   * matching stale error when the field it points at changes.
   */
  #errorFor(pathPrefix) {
    const b = this.store.state.builder;
    if (!b) return null;
    for (const err of [b.error, b.previewError]) {
      if (err && err.path && err.path.startsWith(pathPrefix)) return err;
    }
    return null;
  }

  /** Resolved fixtures for the draft's current targets (dedup, stable order). */
  #resolvedFixtures() {
    const s = this.store.state;
    const fixtures = (s.fixtures && s.fixtures.fixtures) || [];
    const declaredTargetIds = ((s.fixtures && s.fixtures.targets) || []).map((t) => t.id);
    const draft = s.builder.draft;
    const seen = new Set();
    const out = [];
    for (const targetId of draft.target_ids || []) {
      for (const fixture of resolveTargetFixtures(fixtures, targetId, { declaredTargetIds })) {
        if (seen.has(fixture.id)) continue;
        seen.add(fixture.id);
        out.push(fixture);
      }
    }
    return out;
  }

  /** Compact one-line summary of what a state document sets. */
  #stateSummary(state) {
    const parts = [];
    if (state.on === true) parts.push("on");
    if (state.on === false) parts.push("off");
    if (state.brightness !== undefined && state.brightness !== null) parts.push(`${Math.round(state.brightness)}%`);
    if (state.color) parts.push(state.color);
    if (state.palette_index !== undefined && state.palette_index !== null) {
      parts.push(`palette #${Number(state.palette_index) + 1}`);
    }
    if (state.color_temp_mirek !== undefined && state.color_temp_mirek !== null) {
      parts.push(`${state.color_temp_mirek} mirek`);
    }
    if (state.gradient) parts.push("gradient");
    if (state.effect) parts.push(`effect ${state.effect}`);
    return parts.join(" · ") || "no fields set";
  }

  /**
   * The canonical state editor for one scope ("default" or a fixture id):
   * On/Off, brightness, color, color temperature. Unset properties are
   * simply absent from the canonical document (never written as null).
   */
  #renderStateFields(scope, state, { idPrefix, preview = false, hideColor = false, washHex = null } = {}) {
    const isSet = (value) => value !== undefined && value !== null;
    const brightnessSet = isSet(state.brightness);
    const colorSet = !!state.color;
    const tempSet = isSet(state.color_temp_mirek);
    const onValue = state.on === true ? "on" : state.on === false ? "off" : "";
    const errorPrefix = scope === "default" ? "scene.default_state" : `scene.fixture_states.${scope}`;
    const scopeError = this.#errorFor(errorPrefix);
    const invalidClass = scopeError ? "invalid" : "";
    const brightnessValue = brightnessSet ? Math.round(state.brightness) : 60;
    const mirekValue = tempSet ? Number(state.color_temp_mirek) : 270;
    const kelvin = tempSet ? Math.round(1000000 / mirekValue) : "";
    const washState = washHex && !state.color ? { ...state, color: washHex } : state;
    const power = html`
      <div class="look-power">
        <span class="look-k">Power</span>
        <select class="state-on" data-scope=${scope} data-field="on" aria-label="On or off">
          <option value="" ?selected=${onValue === ""}>not set</option>
          <option value="on" ?selected=${onValue === "on"}>On</option>
          <option value="off" ?selected=${onValue === "off"}>Off</option>
        </select>
      </div>
    `;
    return html`
      <div class="state-grid" @input=${this.#onStateInput} @change=${this.#onStateInput}>
        ${preview
          ? html`
              <div class="look-hero">
                <div class="look-wash" style=${lookWash(washState)} title="Preview of the default look"></div>
                ${power}
              </div>
            `
          : power}
        <div class="look-control ${brightnessSet ? "" : "is-off"}">
          <span class="look-label">
            <input
              type="checkbox"
              class="state-set builder-state-set-brightness"
              data-scope=${scope}
              data-field="set"
              data-prop="brightness"
              .checked=${brightnessSet}
              aria-label="Set brightness"
            />
            Brightness
          </span>
          <span class="look-val">
            <input
              type="number"
              id=${`${idPrefix}-brightness-num`}
              class=${`state-brightness-num ${invalidClass}`}
              data-scope=${scope}
              data-field="brightness"
              min="0"
              max="100"
              ?disabled=${!brightnessSet}
              .value=${brightnessSet ? String(brightnessValue) : ""}
              aria-label="Brightness value"
            />
            %
          </span>
          <input
            type="range"
            class="state-brightness"
            data-scope=${scope}
            data-field="brightness"
            min="0"
            max="100"
            step="1"
            ?disabled=${!brightnessSet}
            .value=${String(brightnessValue)}
            aria-label="Brightness"
          />
        </div>
        ${hideColor
          ? ""
          : html`
        <div class="look-control ${colorSet ? "" : "is-off"}">
          <span class="look-label">
            <input
              type="checkbox"
              class="state-set builder-state-set-color"
              data-scope=${scope}
              data-field="set"
              data-prop="color"
              .checked=${colorSet}
              aria-label="Set color"
            />
            Color
          </span>
          <span class="look-val">
            <input
              type="color"
              class="state-color"
              data-scope=${scope}
              data-field="color"
              ?disabled=${!colorSet}
              .value=${/^#[0-9a-f]{6}$/.test(state.color || "") ? state.color : "#ffffff"}
              aria-label="Color"
            />
            <input
              type="text"
              class="hex state-color-hex ${invalidClass}"
              data-scope=${scope}
              data-field="color"
              ?disabled=${!colorSet}
              .value=${state.color || ""}
              maxlength="7"
              spellcheck="false"
              aria-label="Color hex"
            />
          </span>
          <span
            class="look-swatch"
            style=${colorSet && /^#[0-9a-fA-F]{6}$/.test(state.color || "")
              ? `background:${state.color}`
              : ""}
          ></span>
        </div>`}
        <div class="look-control ${tempSet ? "" : "is-off"}">
          <span class="look-label">
            <input
              type="checkbox"
              class="state-set builder-state-set-mirek"
              data-scope=${scope}
              data-field="set"
              data-prop="color_temp_mirek"
              .checked=${tempSet}
              aria-label="Set color temperature"
            />
            Color temp
          </span>
          <span class="look-val">
            <input
              type="number"
              class="state-mirek ${invalidClass}"
              data-scope=${scope}
              data-field="color_temp_mirek"
              min="100"
              max="1000"
              ?disabled=${!tempSet}
              .value=${tempSet ? String(mirekValue) : ""}
              aria-label="Color temperature in mirek"
            />
            ${kelvin ? html`${kelvin} K` : html`mirek`}
          </span>
          <input
            type="range"
            class="state-mirek-range"
            data-scope=${scope}
            data-field="color_temp_mirek"
            min="100"
            max="1000"
            step="1"
            ?disabled=${!tempSet}
            .value=${String(mirekValue)}
            aria-label="Color temperature"
          />
          <div class="look-ends"><span>Warm</span><span>Cool</span></div>
        </div>
        ${scopeError
          ? html`<div class="field-error" id=${`${idPrefix}-state-error`}>${scopeError.message}</div>`
          : ""}
      </div>
    `;
  }

  #fidelityText(fidelity) {
    const parts = [];
    if (fidelity.native) parts.push(`${fidelity.native} native`);
    if (fidelity.equivalent) parts.push(`${fidelity.equivalent} equivalent`);
    if (fidelity.approximate) parts.push(`${fidelity.approximate} approximate`);
    if (fidelity.unsupported) parts.push(`${fidelity.unsupported} unsupported`);
    return parts.join(" · ") || "no fixture plans";
  }

  #renderPreview(b, quality, preview) {
    const tone = quality.tone;
    const canStop = !!(preview && preview.played && preview.session_id);
    return html`
      <div class="preview-panel" id="builder-preview-panel">
        <div class="pv-head ${tone}" id="builder-preview-head">
          ${tone === "ok" ? iconCheckCircle(16) : iconAlertCircle(16)} ${quality.label}
        </div>
        ${canStop
          ? html`<button
              id="builder-preview-stop"
              class="ss-btn"
              ?disabled=${b.busy}
              @click=${() => this.#stopPreview()}
            >
              Stop preview
            </button>`
          : ""}
        <div class="pv-grid">
          <span><strong>${quality.planned}</strong> fixtures planned</span>
          <span class=${quality.skipped ? "warn" : ""}
            ><strong>${quality.skipped}</strong> skipped/unavailable</span
          >
          <span class="mono">
            native ${quality.fidelity.native} · eq ${quality.fidelity.equivalent} ·
            <span class=${quality.fidelity.approximate ? "warn" : ""}>approx ${quality.fidelity.approximate}</span> ·
            <span class=${quality.fidelity.unsupported ? "bad" : ""}>unsup ${quality.fidelity.unsupported}</span>
          </span>
        </div>
        ${Object.keys(quality.providers).length
          ? html`<div class="pv-providers">
              ${Object.entries(quality.providers).map(
                ([provider, count]) => html`<span>${provider}: ${count}</span>`
              )}
            </div>`
          : ""}
        ${b.preview && b.preview.scene && b.mode !== "edit" && b.preview.scene.id
          ? html`<p class="hint" id="builder-candidate-id">
              Candidate id <span class="scene-id">${b.preview.scene.id}</span> — the backend derives and
              validates the final id when you save.
            </p>`
          : ""}
        ${quality.issues.length
          ? html`
              <details>
                <summary>${quality.issues.length} detail${quality.issues.length === 1 ? "" : "s"}</summary>
                <ul>
                  ${quality.issues.map(
                    (issue) => html`<li class=${issue.tone === "bad" ? "bad" : "warn"}>${issue.text}</li>`
                  )}
                </ul>
              </details>
            `
          : ""}
      </div>
    `;
  }

  render() {
    const b = this.store.state.builder;
    if (!b) {
      return html`<ss-empty-state>No draft open.</ss-empty-state>`;
    }
    const draft = b.draft;
    const motion = draft.motion || {};
    // Advanced-content flags are derived from the DRAFT, not from the frozen
    // original document: once the user removes an override or switches away
    // from an advanced motion mode, the "preserved unchanged" notice must
    // stop claiming that content is still there.
    const advanced = describeAdvancedFields(draft);
    const allowedMotionMode = EDITABLE_MOTION_MODES.includes(motion.mode) ? motion.mode : null;
    const advancedMotionPreserved = !!advanced.advancedMotionMode;
    const fixturesDoc = this.store.state.fixtures || {};
    const declaredTargets = orderDeclaredTargets(fixturesDoc.targets || []);
    const allFixtures = [...(fixturesDoc.fixtures || [])].sort((a, c) =>
      String(a.name || a.id).localeCompare(String(c.name || c.id))
    );
    const selectedTargets = draft.target_ids || [];
    const ecoGroups = ecosystemGroupsFromDiscovery(this.store.state.discovery, allFixtures, declaredTargets);
    const fixtureRooms = groupByRoom(allFixtures);
    const canSaveCommand = this.store.commandAllowed(b.mode === "edit" ? "scene.update" : "scene.create");
    const canSave = canSaveCommand && !!(draft.name && draft.name.trim()) && (draft.target_ids || []).length > 0;
    const saveBlockedReason = !canSaveCommand
      ? "Saving is blocked in the current backend runtime mode"
      : !canSave
        ? "A name and at least one target are required to save"
        : "";
    const canPlayPreview = this.store.commandAllowed("scene.play_draft");
    const canObservationalPreview = this.store.commandAllowed("scene.preview_draft") || canPlayPreview;
    const previewIdentityReady = !!(draft.name && draft.name.trim()) && (draft.target_ids || []).length > 0;
    const canPreview =
      canObservationalPreview && !b.busy && (!canPlayPreview || previewIdentityReady);
    const previewBlockedReason = !canObservationalPreview
      ? "Preview is blocked in the current backend runtime mode"
      : canPlayPreview && !previewIdentityReady
        ? "A name and at least one target are required to preview on lights"
        : "";
    const generalError = b.error && !b.error.path ? b.error : null;
    const preview = b.preview || null;
    const quality = preview ? summarizePreviewQuality(preview.render_plan) : null;
    if (quality && preview && preview.played) {
      const playing = preview.kind === "playback";
      if (quality.quality === "ready") {
        quality.label = playing ? "Playing on fixtures" : "Applied to fixtures";
      } else if (quality.quality === "reductions") {
        quality.label = playing
          ? "Playing on fixtures with reductions"
          : "Applied to fixtures with reductions";
      } else if (quality.quality === "partially_unsupported") {
        quality.label = playing ? "Playing with unsupported fixtures" : "Applied with unsupported fixtures";
      }
    }
    const resolved = this.#resolvedFixtures();
    const fixtureStates = draft.fixture_states || {};
    // A fixture whose entire override is a palette pin and/or a plain color
    // is fully owned by the palette assignment UI above (paint-use counts +
    // "tap a light"); listing it again here as a bare "palette #N" line with
    // only a Remove button was the redundant disjoint-panel UX being fixed.
    // Anything with real advanced content (brightness, gradient, effect,
    // on/off, provider_ext, ...) still belongs here.
    const paletteOnlyOverride = (state) => {
      if (!state) return false;
      const keys = Object.keys(state);
      return keys.length > 0 && keys.every((key) => key === "palette_index" || key === "color");
    };
    const paletteActive = usesPalette(draft);
    const overrideIds = resolved
      .filter((f) => fixtureStates[f.id] && !(paletteActive && paletteOnlyOverride(fixtureStates[f.id])))
      .map((f) => f.id);
    const orphanOverrides = Object.keys(fixtureStates).filter(
      (id) => !resolved.some((f) => f.id === id)
    );
    const addableFixtures = resolved.filter((f) => !fixtureStates[f.id]);
    const defaults = draft.default_state || {};
    const defaultsUsed = resolved.filter((f) => !fixtureStates[f.id]).length;
    const capabilitySummary = {
      color: resolved.filter((f) => f.capabilities && f.capabilities.color_xy).length,
      temp: resolved.filter((f) => f.capabilities && f.capabilities.color_temp).length,
      brightness: resolved.filter((f) => f.capabilities && f.capabilities.brightness).length,
    };
    const heading =
      b.mode === "edit"
        ? `Edit scene — ${b.original.name || b.sceneId}`
        : b.mode === "duplicate"
          ? `Duplicate scene — ${b.original.name || b.duplicateOf}`
          : "New scene";
    const candidateId = !b.preview || b.mode === "edit" ? null : (b.preview.scene && b.preview.scene.id) || null;
    const catalogIds = new Set(((this.store.state.scenes && this.store.state.scenes.scenes) || []).map((s) => s.id));
    const candidateCollides = !!(candidateId && catalogIds.has(candidateId));

    return html`
      <div class="head">
        <h2>${heading}</h2>
        ${b.mode === "edit"
          ? html`<span class="scene-id" title="Scene ids are immutable">id: ${b.sceneId}</span>`
          : candidateId
            ? html`<span
                class="scene-id ${candidateCollides ? "collide" : ""}"
                title=${candidateCollides
                  ? "The loaded catalog already uses this id; the backend decides at save time"
                  : "Proposed stable id (derived from the name)"}
                >${candidateCollides ? "id in use: " : "will save as: "}${candidateId}</span
              >`
            : html`<span class="scene-id">id is derived from the name when saved</span>`}
        ${b.mode === "duplicate"
          ? html`<span class="scene-id">duplicate of ${b.duplicateOf} — the original is untouched</span>`
          : ""}
        ${b.dirty ? html`<span class="dirty-tag">unsaved</span>` : ""}
      </div>

      ${advanced.any
        ? html`
            <div class="advanced-warn" role="note">
              ${iconAlertTriangle(17)}
              <div>
                This scene contains content this editor preserves unchanged:
                <ul>
                  ${advanced.advancedOverrideFixtures.length || advanced.advancedDefault.length
                    ? html`<li>
                        advanced fixture state content
                        (${[...advanced.advancedOverrideFixtures, ...(advanced.advancedDefault.length ? ["default state"] : [])].join(", ")})
                      </li>`
                    : ""}
                  ${advanced.advancedMotionMode
                    ? html`<li>advanced motion (${advanced.advancedMotionMode}) — preserved unless you pick Static or Dynamic below</li>`
                    : ""}
                  ${advanced.customMetadata.length
                    ? html`<li>metadata (${advanced.customMetadata.join(", ")})</li>`
                    : ""}
                </ul>
              </div>
            </div>
          `
        : ""}

      ${generalError ? html`<div class="error-box" role="alert">${generalError.message}</div>` : ""}
      ${b.previewError ? html`<div class="error-box" role="alert">Preview failed: ${b.previewError.message}</div>` : ""}

      <div class="workspace">
      <div class="col-targets">
      <div class="section">
        <label class="field-label" for="builder-name">Identity</label>
        <input
          id="builder-name"
          type="text"
          class=${this.#errorFor("scene.name") ? "invalid" : ""}
          .value=${draft.name || ""}
          maxlength="128"
          placeholder="e.g. Evening Glow"
          @input=${this.#onNameInput}
        />
        ${this.#errorFor("scene.name") ? html`<div class="field-error">${this.#errorFor("scene.name").message}</div>` : ""}
      </div>

      <div class="section">
        <label class="field-label">Targets</label>
        <div class="target-groups" @change=${this.#onTargetToggle}>
          <div class="target-group">
            <div class="group-label">Rooms</div>
            <div class="target-chips">
              ${declaredTargets.length
                ? declaredTargets.map((t) => {
                    const meta = groupCountMeta(allFixtures, t.id);
                    return html`
                      <label class="target-chip ${meta.warn ? "warn" : ""}">
                        <input
                          class="builder-target"
                          type="checkbox"
                          value=${t.id}
                          .checked=${selectedTargets.includes(t.id)}
                        />
                        <span class="name">${t.name || roomLabel(t.id)}</span>
                        <span class="meta">${meta.text}</span>
                      </label>
                    `;
                  })
                : html`<span class="hint">No rooms declared in the fixture catalog.</span>`}
            </div>
          </div>
          ${ecoGroups.length
            ? html`
                <div class="target-group">
                  <div class="group-label">Home Assistant groups</div>
                  ${ecoGroups.map((node) => this.#renderEcoGroup(node, selectedTargets, { open: true }))}
                </div>
              `
            : ""}
          <div class="target-group">
            <div class="group-label">Fixtures</div>
            ${allFixtures.length
              ? fixtureRooms.map((g) => html`
                    <details
                      class="target-fold"
                      ?open=${selectedTargets.includes(g.room) ||
                        g.fixtures.some((f) => selectedTargets.includes(f.id))}
                    >
                      <summary>
                        <span class="chev">${iconChevronRight(14)}</span>
                        <span class="name">${roomLabel(g.room)}</span>
                        <span class="meta">${g.fixtures.length}</span>
                      </summary>
                      <div class="body">${this.#renderGroupedFixtures(g.fixtures, selectedTargets)}</div>
                    </details>
                  `)
              : html`<span class="hint">No fixtures in the registry.</span>`}
          </div>
        </div>
        ${this.#errorFor("scene.target_ids")
          ? html`<div class="field-error">${this.#errorFor("scene.target_ids").message}</div>`
          : ""}
        ${resolved.length
          ? html`<p class="hint">
              ${resolved.length} fixture${resolved.length === 1 ? "" : "s"} · color
              ${capabilitySummary.color}/${resolved.length} · temp ${capabilitySummary.temp}/${resolved.length} ·
              brightness ${capabilitySummary.brightness}/${resolved.length}.
            </p>`
          : ""}
      </div>
      </div>

      <div class="col-look">
      <div class="section">
        <div class="section-head">
          <span class="field-label">Scene Defaults</span>
          <span class="count">${defaultsUsed}/${resolved.length || 0}</span>
        </div>
        <p class="hint">
          Shared look for fixtures without an override (${defaultsUsed} of ${resolved.length || 0}).
        </p>
        ${this.#renderStateFields("default", defaults, {
          idPrefix: "builder-default",
          preview: true,
          hideColor: (draft.palette || []).length > 0,
          washHex: (draft.palette || [])[0] || null,
        })}
      </div>

      <div class="section motion-section">
        <div class="section-head">
          <span class="field-label">Motion</span>
        </div>
        ${advancedMotionPreserved
          ? html`<p class="hint">
              Current mode <strong>${advanced.advancedMotionMode}</strong> is not editable here and is preserved
              as-is unless you explicitly pick Static or Dynamic. Effect mode names per-fixture provider effects, so
              this pass deliberately does not offer a universal effect picker.
            </p>`
          : ""}
        <div class="motion-row" @change=${this.#onMotionChange}>
          <label class="motion-card">
            <input
              type="radio"
              name="builder-motion"
              class="builder-motion-static"
              value="static"
              .checked=${allowedMotionMode === "static"}
            />
            <span>
              Static
              <small>Hold one look</small>
            </span>
          </label>
          <label class="motion-card">
            <input
              type="radio"
              name="builder-motion"
              class="builder-motion-dynamic"
              value="dynamic"
              .checked=${allowedMotionMode === "palette_cycle"}
            />
            <span>
              Dynamic
              <small>Cycle the palette</small>
            </span>
          </label>
          ${!allowedMotionMode
            ? html`<label class="motion-card"><input type="radio" disabled .checked=${true} />
                <span>Advanced <small>Preserved as-is</small></span></label>`
            : ""}
        </div>
        ${allowedMotionMode === "palette_cycle"
          ? html`
              <div class="slider-row">
                <span class="ends">slow</span>
                <input
                  id="builder-speed"
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  .value=${String(typeof motion.speed === "number" ? motion.speed : 0.5)}
                  @input=${this.#onSpeed}
                />
                <span class="ends">fast</span>
                <span class="val">${(typeof motion.speed === "number" ? motion.speed : 0.5).toFixed(2)}</span>
              </div>
              <div class="slider-row">
                <span class="ends">palette brightness</span>
                <input
                  id="builder-palette-brightness"
                  type="range"
                  min="0"
                  max="100"
                  step="1"
                  .value=${String(draft.brightness == null ? 60 : draft.brightness)}
                  @input=${this.#onPaletteBrightness}
                />
                <input
                  id="builder-palette-brightness-num"
                  type="number"
                  min="0"
                  max="100"
                  .value=${draft.brightness == null ? "" : String(draft.brightness)}
                  @input=${this.#onPaletteBrightness}
                />
                <span class="val">%</span>
              </div>
            `
          : ""}
        ${allowedMotionMode === "static" || allowedMotionMode === "palette_cycle"
          ? this.#renderPalette(draft, { showAssignment: true, resolved })
          : ""}
        ${this.#errorFor("scene.motion")
          ? html`<div class="field-error">${this.#errorFor("scene.motion").message}</div>`
          : ""}
      </div>
      </div>
      </div>

      <div class="section" id="builder-overrides">
        <div class="section-head">
          <span class="field-label">Overrides</span>
          <span class="count">${overrideIds.length}</span>
        </div>
        <p class="hint">
          Per-fixture intent. Unset fields inherit Scene Defaults; removing an override returns the fixture to those
          defaults.
        </p>
        <div class="override-list">
          ${overrideIds.map((fixtureId) => {
            const fixture = resolved.find((f) => f.id === fixtureId);
            const state = fixtureStates[fixtureId];
            const advancedState = describeFixtureState(state);
            return html`
              <details class="override" data-fixture=${fixtureId}>
                <summary>
                  <span class="fname">${(fixture && fixture.name) || fixtureId}</span>
                  <span class="fid">${fixtureId}</span>
                  <span class="summary">${this.#stateSummary(state)}</span>
                  <button
                    class="row-btn builder-override-remove"
                    data-fixture=${fixtureId}
                    title="Remove this override"
                    aria-label="Remove override for ${(fixture && fixture.name) || fixtureId}"
                    @click=${(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      this.store.removeBuilderOverride(fixtureId);
                    }}
                  >
                    Remove
                  </button>
                </summary>
                <div class="body">
                  ${advancedState.advanced.length
                    ? html`<p class="hint warn">
                        This override also carries ${advancedState.advanced.join(", ")} content. It is preserved
                        exactly; the controls below only touch the fields they own.
                      </p>`
                    : ""}
                  ${this.#renderStateFields(fixtureId, state, { idPrefix: `builder-override-${fixtureId}` })}
                  ${Object.keys(state).length === 0
                    ? html`<p class="hint">No fields set — this override is empty and will be removed.</p>`
                    : ""}
                </div>
              </details>
            `;
          })}
          ${overrideIds.length === 0
            ? html`<p class="hint">No overrides — every selected fixture uses the scene defaults.</p>`
            : ""}
        </div>
        ${orphanOverrides.length
          ? html`
              <div class="orphan-overrides" id="builder-orphan-overrides">
                <p class="hint warn">
                  ${orphanOverrides.length} override${orphanOverrides.length === 1 ? "" : "s"} target fixtures that are
                  not in the selected targets. The renderer ignores them, and they keep the preview reduced until they
                  are removed — either remove them here, or add the fixture to the targets above.
                </p>
                <div class="override-list">
                  ${orphanOverrides.map(
                    (fixtureId) => html`
                      <div class="orphan-row" data-fixture=${fixtureId}>
                        <span class="fname">${fixtureId}</span>
                        <span class="fid">${this.#stateSummary(fixtureStates[fixtureId])}</span>
                        <button
                          class="row-btn builder-orphan-remove"
                          data-fixture=${fixtureId}
                          title="Remove this override"
                          aria-label="Remove override for ${fixtureId}"
                          @click=${() => this.store.removeBuilderOverride(fixtureId)}
                        >
                          Remove
                        </button>
                      </div>
                    `
                  )}
                </div>
              </div>
            `
          : ""}
        ${addableFixtures.length
          ? html`
              <div class="override-add">
                <label for="builder-override-add">Add override for</label>
                <select id="builder-override-add" @change=${this.#onAddOverride}>
                  <option value="">choose a selected fixture…</option>
                  ${addableFixtures.map(
                    (f) => html`<option value=${f.id}>${f.name}</option>`
                  )}
                </select>
              </div>
            `
          : ""}
      </div>

      ${quality ? this.#renderPreview(b, quality, preview) : ""}
      ${b.previewError
        ? html`
            <div class="preview-panel" id="builder-preview-panel">
              <div class="pv-head bad" id="builder-preview-head">Preview could not validate this draft.</div>
            </div>
          `
        : ""}
      ${b.previewStale && !b.preview && !b.previewError
        ? html`<p class="hint" id="builder-preview-stale">
            Draft changed since the last preview — run Preview again for current results.
          </p>`
        : ""}

      <div class="actions">
        <button id="builder-cancel" class="ss-btn" @click=${() => this.#cancel()}>Cancel</button>
        <div class="spacer"></div>
        <button
          id="builder-preview"
          class="ss-btn"
          title=${previewBlockedReason || (canPlayPreview ? "Play this draft on the configured lights" : "Validate and render this draft")}
          ?disabled=${!canPreview}
          @click=${() => this.#preview()}
        >
          ${b.busy ? "Working…" : "Preview"}
        </button>
        <button
          id="builder-save"
          class="ss-btn primary"
          title=${saveBlockedReason || ""}
          ?disabled=${!canSave || b.busy}
          @click=${() => this.#save()}
        >
          ${b.mode === "edit" ? "Save Changes" : b.mode === "duplicate" ? "Save New" : "Save Scene"}
        </button>
      </div>
      ${saveBlockedReason && !canSave ? html`<p class="hint">${saveBlockedReason}</p>` : ""}
      ${previewBlockedReason && !canPreview && !b.busy ? html`<p class="hint">${previewBlockedReason}</p>` : ""}
    `;
  }

  #onAddOverride(e) {
    const fixtureId = e.target.value;
    if (fixtureId) this.store.addBuilderOverride(fixtureId);
  }
}

customElements.define("ss-view-scene-builder", SsViewSceneBuilder);
