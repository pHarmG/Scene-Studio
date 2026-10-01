/**
 * Presentation grouping for fixture catalogs (Fixtures list + Scene Builder
 * Targets). Pure: no DOM, no Lit. Does not invent Scene Studio fixtures or
 * declared targets — HA Light Group helpers stay visual/select-members
 * surfaces, never a revived aggregate fixture id.
 */

/**
 * `whole_house` -> "Whole House". Canonical ids are never mutated, only displayed.
 * @param {string} id
 * @returns {string}
 */
export function humanizeId(id) {
  return String(id || "")
    .split("_")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export const roomLabel = humanizeId;

/**
 * Group fixtures by the first membership that is not the `whole_house`
 * catch-all. First-seen room order.
 * @param {object[]} fixtures
 * @returns {{room:string, fixtures:object[]}[]}
 */
export function groupByRoom(fixtures) {
  const order = [];
  const byRoom = new Map();
  for (const f of fixtures || []) {
    const groups = f.groups || [];
    const room = groups.find((g) => g !== "whole_house") || groups[0] || "Ungrouped";
    if (!byRoom.has(room)) {
      byRoom.set(room, []);
      order.push(room);
    }
    byRoom.get(room).push(f);
  }
  return order.map((room) => ({ room, fixtures: byRoom.get(room) }));
}

/**
 * Condense "one physical controller, many segment fixtures" (WLED) into one
 * cluster. Key is `binding.device_id` only — Hue/HA share a bridge, not a
 * per-controller device_id, so clustering on that would lump unrelated lights.
 * A lone fixture on a device_id stays a normal row.
 * @param {object[]} fixtures
 * @returns {Array<{type:'fixture', fixture:object}|{type:'cluster', deviceId:string, provider:string, fixtures:object[]}>}
 */
export function withControllerClusters(fixtures) {
  const byDevice = new Map();
  for (const f of fixtures || []) {
    const deviceId = f.binding && f.binding.device_id;
    if (!deviceId) continue;
    if (!byDevice.has(deviceId)) byDevice.set(deviceId, []);
    byDevice.get(deviceId).push(f);
  }
  const clustered = new Set();
  for (const [, members] of byDevice) {
    if (members.length > 1) for (const f of members) clustered.add(f.id);
  }
  const emittedDevice = new Set();
  const items = [];
  for (const f of fixtures || []) {
    const deviceId = f.binding && f.binding.device_id;
    if (clustered.has(f.id)) {
      if (emittedDevice.has(deviceId)) continue;
      emittedDevice.add(deviceId);
      items.push({ type: "cluster", deviceId, provider: f.binding.provider, fixtures: byDevice.get(deviceId) });
    } else {
      items.push({ type: "fixture", fixture: f });
    }
  }
  return items;
}

/** "Segment 0–5" from a cluster's member fixtures' segment_ids. */
export function clusterLabel(members) {
  const segIds = (members || [])
    .flatMap((f) => (f.binding && f.binding.segment_ids) || [])
    .sort((a, b) => a - b);
  if (!segIds.length) return `${(members || []).length} fixture${(members || []).length === 1 ? "" : "s"}`;
  const lo = segIds[0];
  const hi = segIds[segIds.length - 1];
  return `Segment ${lo}${hi !== lo ? `–${hi}` : ""}`;
}

/** Controller title for a clustered group ("WLED", not a fixture id). */
export function controllerDisplayName(members) {
  const list = members || [];
  if (!list.length) return "Controller";
  if (list.every((f) => /^WLED\b/i.test(f.name || ""))) return "WLED";
  const provider = list[0].binding && list[0].binding.provider;
  if (provider === "wled") return "WLED";
  return list[0].name || "Controller";
}

/** "WLED Segment 0" -> "Segment 0"; other names pass through. */
export function segmentDisplayName(fixture) {
  const name = String((fixture && fixture.name) || (fixture && fixture.id) || "");
  return name.replace(/^WLED\s+/i, "") || name;
}

/**
 * HA entity ids recorded on a fixture binding (`ha_entity_id` and/or
 * `ha_entity_ids`). Used to map Light Group helper members onto fixtures.
 * @param {object} fixture
 * @returns {string[]}
 */
export function fixtureHaEntityIds(fixture) {
  const binding = (fixture && fixture.binding) || {};
  const ids = [];
  if (typeof binding.ha_entity_id === "string" && binding.ha_entity_id) ids.push(binding.ha_entity_id);
  if (Array.isArray(binding.ha_entity_ids)) {
    for (const id of binding.ha_entity_ids) {
      if (typeof id === "string" && id && !ids.includes(id)) ids.push(id);
    }
  }
  return ids;
}

export function isHaAggregateObservation(obs) {
  return !!(obs && obs.metadata && obs.metadata.ha_aggregate);
}

/**
 * HA Light Group helpers that exist in the live house but are not Scene
 * Studio fixtures (unification retired the aggregate office_lights fixture).
 * Discovery observations override these when present; this fallback keeps
 * Office Lights / Front / Side / Back selectable in the Builder even when
 * AppDaemon has no discovery report in memory.
 */
export const FALLBACK_HA_LIGHT_GROUPS = [
  {
    entityId: "light.office_lights",
    name: "Office Lights",
    members: [
      "light.lg_wled_segment_0",
      "light.lg_wled_segment_1",
      "light.lg_wled_segment_2",
      "light.lg_wled_segment_3",
      "light.lg_wled_segment_4",
      "light.lg_wled_segment_5",
      "light.hue_g_strip",
      "light.double_strip",
      "light.lamp",
      "light.office_strip",
      "light.left",
      "light.middle",
      "light.right",
    ],
  },
  {
    entityId: "light.office_front_lights",
    name: "Office Front Lights",
    members: [
      "light.lg_wled_segment_0",
      "light.lg_wled_segment_1",
      "light.lg_wled_segment_2",
      "light.lg_wled_segment_3",
      "light.lg_wled_segment_4",
      "light.lg_wled_segment_5",
      "light.hue_g_strip",
    ],
  },
  {
    entityId: "light.office_side_lights",
    name: "Office Side Lights",
    members: ["light.double_strip", "light.lamp", "light.office_strip"],
  },
  {
    entityId: "light.office_back_lights",
    name: "Office Back Lights",
    members: ["light.left", "light.middle", "light.right"],
  },
];

/** "Office Front Lights" under "Office Lights" -> "Front Lights". */
export function nestedGroupLabel(parentName, childName) {
  const parent = String(parentName || "").trim();
  const child = String(childName || "").trim();
  if (!parent || !child) return child;
  const prefix = parent.replace(/\s+lights$/i, "").trim();
  if (prefix && child.toLowerCase().startsWith(prefix.toLowerCase())) {
    const rest = child.slice(prefix.length).trim();
    return rest || child;
  }
  return child;
}

function uniqueFixtures(list) {
  const seen = new Set();
  const out = [];
  for (const f of list || []) {
    if (!f || !f.id || seen.has(f.id)) continue;
    seen.add(f.id);
    out.push(f);
  }
  return out;
}

function fixtureIdKey(fixtures) {
  return uniqueFixtures(fixtures)
    .map((f) => f.id)
    .sort()
    .join("\n");
}

/**
 * Declared Scene Studio rooms/groups, with the whole-house catch-all last.
 * @param {object[]} targets
 * @returns {object[]}
 */
export function orderDeclaredTargets(targets) {
  const list = [...(targets || [])];
  return [...list.filter((t) => t.id !== "whole_house"), ...list.filter((t) => t.id === "whole_house")];
}

function declaredMemberKeys(fixtures, targets) {
  const keys = new Set();
  for (const t of targets || []) {
    if (!t || !t.id) continue;
    const members = (fixtures || []).filter((f) => Array.isArray(f.groups) && f.groups.includes(t.id));
    if (members.length) keys.add(fixtureIdKey(members));
  }
  return keys;
}

function preferredGroupOrder(name) {
  const n = String(name || "").toLowerCase();
  if (n.includes("front")) return 0;
  if (n.includes("side")) return 1;
  if (n.includes("back")) return 2;
  return 10;
}

function idSet(fixtures) {
  return new Set(uniqueFixtures(fixtures).map((f) => f.id));
}

function isProperSubset(inner, outer) {
  const outerIds = idSet(outer);
  const innerList = uniqueFixtures(inner);
  if (!innerList.length || innerList.length >= outerIds.size) return false;
  return innerList.every((f) => outerIds.has(f.id));
}

function collectHaGroups(discovery) {
  const byId = new Map();
  for (const g of FALLBACK_HA_LIGHT_GROUPS) {
    byId.set(g.entityId, { entityId: g.entityId, name: g.name, members: [...g.members] });
  }
  for (const obs of (discovery && discovery.observations) || []) {
    if (!isHaAggregateObservation(obs)) continue;
    const entityId = obs.provider_resource_id;
    const members = Array.isArray(obs.metadata.ha_group_members)
      ? obs.metadata.ha_group_members.filter((id) => typeof id === "string" && id)
      : [];
    if (typeof entityId !== "string" || !entityId || !members.length) continue;
    byId.set(entityId, {
      entityId,
      name: typeof obs.name === "string" && obs.name ? obs.name : humanizeId(entityId.replace(/^light\./, "")),
      members,
    });
  }
  return [...byId.values()];
}

/**
 * HA Light Group helpers expanded onto Scene Studio fixtures via binding
 * HA entity ids. Nested helpers (Office Lights → Front/Side/Back) become
 * a tree by fixture-set subset so a flat HA parent still shows its
 * component groups. Never creates an `office_lights` fixture id.
 *
 * @param {object|null} discovery DiscoveryReport JSON
 * @param {object[]} fixtures
 * @param {object[]} [declaredTargets]
 * @returns {Array<{entityId:string, name:string, fixtures:object[], children:object[], directFixtures:object[]}>}
 */
export function ecosystemGroupsFromDiscovery(discovery, fixtures, declaredTargets = []) {
  const aggregates = collectHaGroups(discovery);
  if (!aggregates.length) return [];

  const byEntity = new Map(aggregates.map((g) => [g.entityId, g]));
  const fixtureByEntity = new Map();
  for (const f of fixtures || []) {
    for (const eid of fixtureHaEntityIds(f)) {
      if (!fixtureByEntity.has(eid)) fixtureByEntity.set(eid, f);
    }
  }

  const expandToFixtures = (entityId, seen) => {
    if (seen.has(entityId)) return [];
    seen.add(entityId);
    const fixture = fixtureByEntity.get(entityId);
    if (fixture) return [fixture];
    const nested = byEntity.get(entityId);
    if (!nested) return [];
    return uniqueFixtures(nested.members.flatMap((m) => expandToFixtures(m, seen)));
  };

  const declaredKeys = declaredMemberKeys(fixtures, declaredTargets);
  const allFixtureKey = fixtureIdKey(fixtures);
  const nodes = aggregates
    .map((g) => ({
      entityId: g.entityId,
      name: g.name,
      fixtures: expandToFixtures(g.entityId, new Set()),
      children: [],
      directFixtures: [],
    }))
    .filter((node) => node.fixtures.length >= 2)
    .filter((node) => {
      const key = fixtureIdKey(node.fixtures);
      if (declaredKeys.has(key)) return false;
      if (allFixtureKey && key === allFixtureKey) return false;
      return true;
    });

  const parentOf = new Map();
  for (const child of nodes) {
    let best = null;
    for (const parent of nodes) {
      if (parent.entityId === child.entityId) continue;
      if (!isProperSubset(child.fixtures, parent.fixtures)) continue;
      if (!best || parent.fixtures.length < best.fixtures.length) best = parent;
    }
    if (best) parentOf.set(child.entityId, best.entityId);
  }

  const byId = new Map(nodes.map((n) => [n.entityId, n]));
  for (const [childId, parentId] of parentOf) {
    byId.get(parentId).children.push(byId.get(childId));
  }
  for (const node of nodes) {
    node.children.sort((a, b) => preferredGroupOrder(a.name) - preferredGroupOrder(b.name) || a.name.localeCompare(b.name));
    const nestedIds = new Set(node.children.flatMap((c) => c.fixtures.map((f) => f.id)));
    node.directFixtures = node.fixtures.filter((f) => !nestedIds.has(f.id));
  }

  return nodes.filter((n) => !parentOf.has(n.entityId)).sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * @param {string[]} selectedIds
 * @param {string[]} memberIds
 * @returns {{checked:boolean, indeterminate:boolean}}
 */
export function selectionOf(selectedIds, memberIds) {
  const selected = new Set(selectedIds || []);
  const ids = (memberIds || []).filter(Boolean);
  if (!ids.length) return { checked: false, indeterminate: false };
  let n = 0;
  for (const id of ids) if (selected.has(id)) n += 1;
  return { checked: n === ids.length, indeterminate: n > 0 && n < ids.length };
}

/**
 * Add or remove a set of ids from `current` without inventing duplicates.
 * @param {string[]} current
 * @param {string[]} memberIds
 * @param {boolean} checked
 * @returns {string[]}
 */
export function toggleIdSet(current, memberIds, checked) {
  const members = new Set((memberIds || []).filter(Boolean));
  const list = Array.isArray(current) ? current : [];
  if (checked) {
    const next = [...list];
    for (const id of members) if (!next.includes(id)) next.push(id);
    return next;
  }
  return list.filter((id) => !members.has(id));
}

/**
 * Ready-count chip text without spelling "ready". All-ready → "14"; mixed → "12/14".
 * @param {object[]} fixtures
 * @param {string} groupId
 * @returns {{text:string, warn:boolean}}
 */
export function groupCountMeta(fixtures, groupId) {
  const members = (fixtures || []).filter((f) => Array.isArray(f.groups) && f.groups.includes(groupId));
  if (!members.length) return { text: "none", warn: true };
  const ready = members.filter((f) => f.health === "ready").length;
  if (ready === members.length) return { text: String(members.length), warn: false };
  return { text: `${ready}/${members.length}`, warn: true };
}

/**
 * Health note for an individual fixture row. Empty when healthy — the
 * checkbox is enough.
 * @param {object} fixture
 * @returns {{text:string, warn:boolean}|null}
 */
export function fixtureHealthNote(fixture) {
  if (!fixture) return null;
  if (fixture.enabled === false || fixture.health === "disabled") return { text: "disabled", warn: true };
  if (fixture.health && fixture.health !== "ready") return { text: fixture.health, warn: true };
  return null;
}
