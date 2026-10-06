/**
 * Routine presentation helpers (routines pass) — DOM-free module.
 *
 * Presentation labels for the derived HA routine projection
 * (domain/routines.py): the 12-hour time label, concise recurrence words,
 * and the generated `Scene Studio · <scene> · <recurrence> <time>` alias
 * shape. The mock client (api.js) reuses these so mock and live UI text can
 * never drift apart; nothing here interprets or mutates automations —
 * Home Assistant stays canonical and the backend stays the only editor of
 * the HA-side config.
 */

export const ROUTINE_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

/** `"19:30"` -> `"7:30 PM"` (display labels only). */
export function formatRoutineTime12h(time) {
  const [hour, minute] = String(time || "").split(":").map(Number);
  if (Number.isNaN(hour) || Number.isNaN(minute)) return String(time || "");
  const suffix = hour < 12 ? "AM" : "PM";
  return `${hour % 12 || 12}:${String(minute).padStart(2, "0")} ${suffix}`;
}

/** Concise recurrence label: Daily / Weekdays / Weekends / Thursdays / Mon, Thu. */
export function describeRoutineWeekdays(weekdays) {
  if (!weekdays || weekdays.length === 0 || weekdays.length === ROUTINE_WEEKDAYS.length) return "Daily";
  const set = new Set(weekdays);
  if (set.size === 5 && ["mon", "tue", "wed", "thu", "fri"].every((d) => set.has(d))) return "Weekdays";
  if (set.size === 2 && set.has("sat") && set.has("sun")) return "Weekends";
  const labels = {
    mon: "Mondays", tue: "Tuesdays", wed: "Wednesdays", thu: "Thursdays",
    fri: "Fridays", sat: "Saturdays", sun: "Sundays",
  };
  if (set.size === 1) return labels[weekdays[0]];
  return weekdays.map((d) => d[0].toUpperCase() + d.slice(1)).join(", ");
}

/** `"Weekdays 7:30 PM"`-style human summary of a routine schedule. */
export function describeRoutineSchedule(schedule) {
  if (!schedule || !schedule.time) return "";
  return `${describeRoutineWeekdays(schedule.weekdays)} ${formatRoutineTime12h(schedule.time)}`;
}

/** Backend alias shape for generated routines (`Scene Studio · Evening Glow · Weekdays 7:00 PM`). */
export function routineAlias(sceneName, time, weekdays) {
  return `Scene Studio · ${sceneName} · ${describeRoutineWeekdays(weekdays)} ${formatRoutineTime12h(time)}`;
}

/**
 * Compact temporal state for a scene row's affordance (progressive
 * disclosure): null when routine awareness has not loaded; otherwise
 * `{count, text, hasAdvanced, allDisabled}`:
 * - no routines -> text "" (the row renders the bare clock affordance)
 * - one routine -> `"Weekdays 7:30 PM"` (the concise recurrence + time)
 * - several -> `"3 routines"`
 * Advanced (HA-managed) routines count toward the summary.
 *
 * @param {object[]|null|undefined} routines routine projections for ONE scene
 * @returns {{count:number, text:string, hasAdvanced:boolean, allDisabled:boolean}|null}
 */
export function routineRowSummary(routines) {
  if (routines === null || routines === undefined) return null;
  const list = Array.isArray(routines) ? routines : [];
  const native = list.filter((r) => r.classification === "native_routine");
  const hasAdvanced = list.length > native.length;
  const allDisabled = list.length > 0 && list.every((r) => r.enabled === false);
  if (list.length === 0) {
    return { count: 0, text: "", hasAdvanced, allDisabled };
  }
  if (list.length === 1) {
    const routine = list[0];
    const text = routine.schedule
      ? describeRoutineSchedule(routine.schedule)
      : describeRoutineWeekdays(null);
    return { count: 1, text, hasAdvanced, allDisabled };
  }
  return { count: list.length, text: `${list.length} routines`, hasAdvanced, allDisabled };
}

/**
 * Sort key for routine lists in the editor: scheduled (native) routines by
 * time first, then advanced ones, then alias — deterministic and stable
 * across refreshes.
 */
export function routineSortKey(routine) {
  const time = routine.schedule && routine.schedule.time ? routine.schedule.time : "99:99";
  const advanced = routine.classification === "recognized_advanced" ? 1 : 0;
  return `${advanced}-${time}-${routine.alias || routine.automation_id}`;
}
