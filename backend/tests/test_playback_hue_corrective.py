"""R5D managed-zone isolation — required regressions (final R5 correction).

The live bridge animates EVERY light in a scene's group on a
dynamic_palette recall, so room-scoped managed scenes cannot isolate a
fixture subset. The provider-native isolation boundary is Scene-Studio-
owned ZONES keyed by the membership fingerprint (sorted participating Hue
light service RIDs): a managed scene hangs on the zone whose members are
exactly the scene's participants.
"""

import json

from scene_studio.service.playback_realization import (
    HueManagedSceneStore,
    HueManagedZoneStore,
    PlaybackRealizer,
    zone_membership_fingerprint,
)
from scene_studio.service.ports import receipt

from test_playback_realization_r5b import _add_scene, _session_execution
from test_service_engine import make_engine, make_store

GROUP = "bridge-room-42"
G_STRIP_RID = "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"
MATE_RID = "68ad5817-0907-4811-9a5c-e3a1d4a69803"


class ZoneBridge:
    """Bridge fake with room/zone topology: records every op, serves the
    read-only overview, and materializes created/repaired zones."""

    def __init__(self, rooms=None, zones=None, light_to_device=None, fail_ops=()):
        self.calls = []
        self.rooms = list(rooms or [])
        self.zones = list(zones or [])
        self.light_to_device = dict(light_to_device or {})
        self.fail_ops = set(fail_ops)
        self._scene_n = 0
        self._zone_n = 0

    def _lights_of(self, group):
        kids = set(group.get("children", []))
        return sorted(lid for lid, did in self.light_to_device.items() if did in kids)

    def _overview(self):
        groups = [
            {"rid": g["rid"], "rtype": g["rtype"], "name": g["name"], "light_rids": self._lights_of(g)}
            for g in [*self.rooms, *self.zones]
        ]
        return {"groups": groups, "light_to_device": dict(self.light_to_device)}

    def execute(self, operation, fixture):
        self.calls.append({
            "op": operation.op,
            "resource_ref": operation.resource_ref,
            "payload": json.loads(json.dumps(operation.payload)),
        })
        if operation.op in self.fail_ops:
            return receipt(False, "hue_v2", operation.op, "injected failure")

        def result(ok, data=None, detail="ok"):
            r = receipt(ok, "hue_v2", operation.op, detail)
            if ok and data is not None:
                r["data"] = data
            return r

        if operation.op == "hue.get_groups_overview":
            return result(True, self._overview())
        if operation.op == "hue.get_group_lights":
            for g in [*self.rooms, *self.zones]:
                if g["rid"] == operation.resource_ref:
                    return result(True, {"light_rids": self._lights_of(g)})
            return result(False, detail="no such group")
        if operation.op == "hue.get_light":
            return result(True, {"resource": {"on": {"on": True}, "dimming": {"brightness": 50.0}}})
        if operation.op == "hue.post_zone":
            self._zone_n += 1
            rid = f"zone-{self._zone_n}"
            # the real bridge normalizes light children to device rids on GET
            children = [self.light_to_device.get(c["rid"], c["rid"])
                        for c in operation.payload.get("children", [])]
            self.zones.append({
                "rid": rid, "rtype": "zone", "name": operation.payload["metadata"]["name"],
                "children": children,
            })
            return result(True, {"zone_rid": rid})
        if operation.op == "hue.put_zone":
            for g in self.zones:
                if g["rid"] == operation.resource_ref:
                    g["children"] = [self.light_to_device.get(c["rid"], c["rid"])
                                     for c in operation.payload.get("children", [])]
                    return result(True)
            return result(False, detail="no such zone")
        if operation.op == "hue.find_scene":
            wanted = (operation.payload or {}).get("name")
            wanted_group = (operation.payload or {}).get("group", {}).get("rid")
            match = next(
                (s for s in getattr(self, "scene_existing", {}).values()
                 if s["name"] == wanted
                 and (s.get("group") is None or s.get("group") == wanted_group)),
                None,
            )
            return result(True,
                          {"resource_id": match["resource_id"] if match else None,
                           "matched": match is not None},
                          "found" if match else "no match")
        if operation.op == "hue.post_scene":
            self._scene_n += 1
            return result(True, {"resource_id": f"scene-{self._scene_n}"}, "HTTP 201")
        return result(True, detail="ok")


def _engine(tmp_path, *, bridge=None, participants=(G_STRIP_RID,)):
    """Engine with the participants bound as Hue fixtures, wired to the
    given (or a fresh) ZoneBridge."""
    engine = make_engine(make_store(tmp_path / "store"))
    by_rid = {G_STRIP_RID: "g_strip", MATE_RID: "middle_bar"}
    for rid in participants:
        fid = by_rid.get(rid, f"fx_{rid[:8]}")
        if not any(f.id == fid for f in engine._store.fixtures.registry().fixtures):
            engine._store.fixtures.add_fixture({
                "id": fid, "name": fid, "groups": [],
                "binding": {"provider": "hue_v2", "bridge_id": "bridge1", "resource_id": rid},
                "capabilities": {"on_off": True, "brightness": True, "color_xy": True,
                                 "gradient": {"max_points": 5}, "dynamic_native": True},
            })
        else:
            engine._store.fixtures.bind(fid, {
                "provider": "hue_v2", "bridge_id": "bridge1", "resource_id": rid,
            })
            if fid == "middle_bar":
                engine._store.fixtures.update_capabilities(fid, {
                    "dynamic_native": True, "gradient": {"max_points": 5}})
    if bridge is None:
        bridge = ZoneBridge(light_to_device={rid: f"dev-{rid[:8]}" for rid in participants})
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z",
        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"),
    )
    return engine, bridge


def _scene(engine, scene_id, participants, palette):
    by_rid = {G_STRIP_RID: "g_strip", MATE_RID: "middle_bar"}
    added = getattr(engine, "_r5d_added_scenes", None)
    if added is None:
        added = engine._r5d_added_scenes = set()
    if scene_id not in added:
        states = {
            by_rid.get(rid, f"fx_{rid[:8]}"): {"on": True, "brightness": 55.0,
                                               "gradient": ["#ff0000", "#00ff00"]}
            for rid in participants
        }
        _add_scene(engine, scene_id, states,
                   target_ids=[by_rid.get(r, f"fx_{r[:8]}") for r in participants],
                   speed=0.4, motion="palette_cycle", palette=palette)
        added.add(scene_id)
    return engine.handle({"command": "playback.start", "scene_id": scene_id})


def _zone_calls(bridge):
    return [c for c in bridge.calls if c["op"] in ("hue.post_zone", "hue.put_zone")]


FP_ONE = zone_membership_fingerprint([G_STRIP_RID])


def test_one_light_subset_gets_an_ss_zone_with_only_g_strip(tmp_path):
    engine, bridge = _engine(tmp_path)
    result = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000", "#00ff00", "#0000ff"])
    assert result["ok"] is True
    execution = _session_execution(result, "g_strip")
    assert execution["execution"] == "native_scene"
    assert execution["fidelity"] == "native" and execution["ok"]

    created = _zone_calls(bridge)
    assert len(created) == 1 and created[0]["op"] == "hue.post_zone"
    label = created[0]["payload"]["metadata"]["name"]
    assert label == f"SS-Z-{FP_ONE}"
    assert created[0]["payload"]["children"] == [{"rid": G_STRIP_RID, "rtype": "light"}]
    assert created[0]["payload"]["metadata"]["archetype"] == "other"

    scene_post = next(c for c in bridge.calls if c["op"] == "hue.post_scene")
    assert scene_post["payload"]["group"] == {"rid": "zone-1", "rtype": "zone"}
    assert scene_post["payload"]["metadata"]["name"] == "SS-solo_scene-zone-1"
    recall = next(c for c in bridge.calls if c["op"] == "hue.recall_scene")
    assert recall["payload"] == {"recall": {"action": "dynamic_palette"}}

    zone_state = json.loads((tmp_path / "ps" / "hue_zones.json").read_text())
    entry = zone_state["zones"][FP_ONE]
    assert entry["light_rids"] == [G_STRIP_RID]
    assert entry["zone_rid"] == "zone-1"
    assert entry["label"] == label
    assert entry["created_at"] and entry["updated_at"]


def test_same_participant_set_reuses_one_zone_across_scenes(tmp_path):
    engine, bridge = _engine(tmp_path)
    r1 = _scene(engine, "scene_a", [G_STRIP_RID], ["#ff0000"])
    engine.handle({"command": "playback.stop", "session_id": r1["data"]["session_id"]})
    r2 = _scene(engine, "scene_b", [G_STRIP_RID], ["#00ff00"])
    assert r1["ok"] and r2["ok"]
    assert len([c for c in bridge.calls if c["op"] == "hue.post_zone"]) == 1
    assert len([c for c in bridge.calls if c["op"] == "hue.post_scene"]) == 2
    zone_state = json.loads((tmp_path / "ps" / "hue_zones.json").read_text())
    assert len(zone_state["zones"]) == 1


def test_different_participant_sets_get_different_zones(tmp_path):
    engine, bridge = _engine(tmp_path, participants=(G_STRIP_RID, MATE_RID))
    r1 = _scene(engine, "scene_a", [G_STRIP_RID], ["#ff0000"])
    engine.handle({"command": "playback.stop", "session_id": r1["data"]["session_id"]})
    r2 = _scene(engine, "scene_b", [G_STRIP_RID, MATE_RID], ["#00ff00"])
    assert r1["ok"] and r2["ok"]
    zone_creates = [c for c in bridge.calls if c["op"] == "hue.post_zone"]
    assert len(zone_creates) == 2
    labels = sorted(c["payload"]["metadata"]["name"] for c in zone_creates)
    assert labels[0] != labels[1]
    assert all(l.startswith("SS-Z-") for l in labels)
    zone_state = json.loads((tmp_path / "ps" / "hue_zones.json").read_text())
    assert len(zone_state["zones"]) == 2


def test_exact_room_membership_uses_the_room_not_a_zone(tmp_path):
    bridge = ZoneBridge(
        rooms=[{"rid": GROUP, "rtype": "room", "name": "Studio",
                "children": ["dev-2a2c45a9", "dev-68ad5817"]}],
        light_to_device={G_STRIP_RID: "dev-2a2c45a9", MATE_RID: "dev-68ad5817"},
    )
    engine, _ = _engine(tmp_path, bridge=bridge, participants=(G_STRIP_RID, MATE_RID))
    result = _scene(engine, "whole_room", [G_STRIP_RID, MATE_RID], ["#ff0000"])
    assert result["ok"] is True
    assert _zone_calls(bridge) == [], "exact room membership must not manufacture a zone"
    scene_post = next(c for c in bridge.calls if c["op"] == "hue.post_scene")
    assert scene_post["payload"]["group"] == {"rid": GROUP, "rtype": "room"}
    assert all(a["target"]["rid"] in (G_STRIP_RID, MATE_RID)
               for a in scene_post["payload"]["actions"])


def test_fixture_can_live_in_its_room_and_an_ss_zone(tmp_path):
    bridge = ZoneBridge(
        rooms=[{"rid": GROUP, "rtype": "room", "name": "Studio",
                "children": ["dev-2a2c45a9", "dev-68ad5817"]}],
        light_to_device={G_STRIP_RID: "dev-2a2c45a9", MATE_RID: "dev-68ad5817"},
    )
    engine, _ = _engine(tmp_path, bridge=bridge)  # g_strip only
    result = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert result["ok"] is True
    assert bridge.rooms[0]["children"] == ["dev-2a2c45a9", "dev-68ad5817"]
    assert bridge.zones[0]["children"] == ["dev-2a2c45a9"]
    assert _session_execution(result, "g_strip")["ok"] is True


def test_managed_scene_actions_exactly_cover_zone_membership(tmp_path):
    bridge = ZoneBridge(
        rooms=[{"rid": GROUP, "rtype": "room", "name": "Studio",
                "children": ["dev-2a2c45a9", "dev-68ad5817"]}],
        light_to_device={G_STRIP_RID: "dev-2a2c45a9", MATE_RID: "dev-68ad5817"},
    )
    engine, _ = _engine(tmp_path, bridge=bridge)
    _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000", "#00ff00", "#0000ff"])
    scene_post = next(c for c in bridge.calls if c["op"] == "hue.post_scene")
    actions = scene_post["payload"]["actions"]
    assert [a["target"]["rid"] for a in actions] == [G_STRIP_RID]
    assert scene_post["payload"]["palette"]["color"]
    assert scene_post["payload"]["speed"] == 0.4
    assert not any("dynamics" in a["action"] for a in actions)
    assert not any(c["op"] == "hue.put_light" for c in bridge.calls)


def test_zone_recovery_requires_exact_membership_and_repairs_drift(tmp_path):
    ps_zones = tmp_path / "ps" / "hue_zones.json"
    bridge = ZoneBridge(light_to_device={G_STRIP_RID: "dev-2a2c45a9",
                                         MATE_RID: "dev-68ad5817"})
    engine, _ = _engine(tmp_path, bridge=bridge)
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z", HueManagedZoneStore(ps_zones))
    r1 = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert r1["ok"]
    store = HueManagedZoneStore(ps_zones)
    assert store.by_fingerprint(FP_ONE)["zone_rid"] == "zone-1"

    bridge.zones[0]["children"].append("dev-68ad5817")
    bridge.calls.clear()

    r2 = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert r2["ok"]
    repairs = [c for c in bridge.calls if c["op"] == "hue.put_zone"]
    assert len(repairs) == 1 and repairs[0]["resource_ref"] == "zone-1"
    assert repairs[0]["payload"]["children"] == [{"rid": G_STRIP_RID, "rtype": "light"}]
    assert not any(c["op"] == "hue.post_zone" for c in bridge.calls)


def test_user_zone_with_matching_name_is_never_adopted(tmp_path):
    user_zone = {"rid": "user-zone-9", "rtype": "zone", "name": f"SS-Z-{FP_ONE}",
                 "children": ["dev-2a2c45a9", "dev-68ad5817"]}
    bridge = ZoneBridge(
        zones=[user_zone],
        light_to_device={G_STRIP_RID: "dev-2a2c45a9", MATE_RID: "dev-68ad5817"},
    )
    engine, _ = _engine(tmp_path, bridge=bridge)
    result = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert result["ok"] is True
    assert not any(c["op"] == "hue.put_zone" and c["resource_ref"] == "user-zone-9"
                   for c in bridge.calls)
    created = [c for c in bridge.calls if c["op"] == "hue.post_zone"]
    assert len(created) == 1
    assert created[0]["payload"]["metadata"]["name"] == f"SS-Z-{FP_ONE}"
    assert created[0]["payload"]["children"] == [{"rid": G_STRIP_RID, "rtype": "light"}]
    zone_state = json.loads((tmp_path / "ps" / "hue_zones.json").read_text())
    assert zone_state["zones"][FP_ONE]["zone_rid"] != "user-zone-9"


def test_stale_persisted_zone_rid_is_recovered_by_label(tmp_path):
    ps_zones = tmp_path / "ps" / "hue_zones.json"
    bridge = ZoneBridge(light_to_device={G_STRIP_RID: "dev-2a2c45a9"})
    engine, _ = _engine(tmp_path, bridge=bridge)
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z", HueManagedZoneStore(ps_zones))
    r1 = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert r1["ok"]
    bridge.zones[0]["rid"] = "zone-recreated"
    store = HueManagedZoneStore(ps_zones)
    store.record(FP_ONE, [G_STRIP_RID], "zone-gone", f"SS-Z-{FP_ONE}", "2026-09-12T00:00:00Z")
    bridge.calls.clear()

    r2 = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert r2["ok"]
    assert not any(c["op"] == "hue.post_zone" for c in bridge.calls)
    scene_post = next(c for c in bridge.calls if c["op"] == "hue.post_scene")
    assert scene_post["payload"]["group"]["rid"] == "zone-recreated"
    assert store.by_fingerprint(FP_ONE)["zone_rid"] == "zone-recreated"

    bridge.zones.clear()
    bridge.calls.clear()
    r3 = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    assert r3["ok"]
    assert len([c for c in bridge.calls if c["op"] == "hue.post_zone"]) == 1


def test_mixed_hue_zone_and_wled_cardinality(tmp_path):
    engine, bridge = _engine(tmp_path)
    _add_scene(engine, "mixed_dyn", {
        "g_strip": {"on": True, "brightness": 55.0, "gradient": ["#ff0000"]},
        "wled_seg": {"on": True, "brightness": 50.0,
                     "provider_ext": {"wled": {"fx": 9, "sx": 102}}},
    }, target_ids=["g_strip", "wled_seg"], speed=0.4, motion="palette_cycle",
        palette=["#ff0000"])
    result = engine.handle({"command": "playback.start", "scene_id": "mixed_dyn"})
    assert result["ok"] is True
    executions = result["data"]["playback"]["fixture_executions"]
    assert {e["fixture_id"] for e in executions} == {"g_strip", "wled_seg"}
    by_id = {e["fixture_id"]: e for e in executions}
    assert by_id["g_strip"]["execution"] == "native_scene" and by_id["g_strip"]["ok"]
    assert by_id["wled_seg"]["execution"] == "native_effect" and by_id["wled_seg"]["ok"]
    scene_post = next(c for c in bridge.calls if c["op"] == "hue.post_scene")
    assert scene_post["payload"]["group"]["rtype"] == "zone"
    wled_ops = [c for c in bridge.calls if c["op"] == "wled.post_state"]
    assert wled_ops and wled_ops[0]["payload"]["seg"][0]["fx"] == 9


def test_lifecycle_recalls_and_fidelity_on_the_zone_path(tmp_path):
    engine, bridge = _engine(tmp_path)
    started = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    sid = started["data"]["session_id"]
    bridge.calls.clear()

    engine.handle({"command": "playback.pause", "session_id": sid})
    engine.handle({"command": "playback.resume", "session_id": sid})
    engine.handle({"command": "playback.stop", "session_id": sid})
    recalls = [c for c in bridge.calls if c["op"] == "hue.recall_scene"]
    assert [c["payload"] for c in recalls] == [
        {"recall": {"action": "static"}},
        {"recall": {"action": "dynamic_palette"}},
        {"recall": {"action": "static"}},
    ]
    session = next(s for s in engine.status()["playback"]["sessions"] if s["session_id"] == sid)
    stop_exec = next(e for e in session["fixture_executions"] if e["fixture_id"] == "g_strip")
    assert stop_exec["fidelity"] == "approximate"


def test_no_hue_light_writes_and_no_dynamics_status_anywhere(tmp_path):
    engine, bridge = _engine(tmp_path)
    started = _scene(engine, "solo_scene", [G_STRIP_RID], ["#ff0000"])
    sid = started["data"]["session_id"]
    for command in ("playback.pause", "playback.resume", "playback.stop"):
        engine.handle({"command": command, "session_id": sid})
    assert not any(c["op"] == "hue.put_light" for c in bridge.calls)
    assert all("dynamics" not in c["payload"] for c in bridge.calls
               if c["op"] == "hue.recall_scene")
