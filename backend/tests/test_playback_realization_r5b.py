"""R5B tests — native provider playback realization (fakes only).

Uses the shared make_store registry (g_strip hue gradient dynamic-native
groups office+studio; middle_bar hue color; wled_seg wled seg 0 device
aabbccddeeff groups office; lamp ha_light office; double_strip hue
disabled; targets office + studio; scenes twilight static and aurora
dynamic on office).
"""

import json
from pathlib import Path

from scene_studio.service.playback_realization import (
    HueManagedSceneStore,
    HueManagedZoneStore,
    PlaybackRealizer,
    zone_membership_fingerprint,
)
from scene_studio.service.ports import DiscoveryFetchers, receipt

from scene_studio.stores import SceneStudioStore
from test_service_engine import make_engine, make_store


class FakeSceneBridgeExecutor:
    """Scripted Hue bridge: records ops, answers find_scene by managed name,
    hands out a create resource id."""

    def __init__(self, existing_scenes=None, create_rid="bridge-scene-1",
                 group_light_rids=None, light_states=None, topology=None):
        self.calls = []
        self.existing_scenes = dict(existing_scenes or {})
        self.create_rid = create_rid
        self.group_light_rids = group_light_rids if group_light_rids is not None else []
        self.light_states = dict(light_states or {})
        # zone-model topology: {"groups": [{rid, rtype, name, light_rids}],
        #                       "light_to_device": {light_rid: device_rid}}
        self.topology = topology or {"groups": [], "light_to_device": {}}
        self._zone_n = 0

    def execute(self, operation, fixture):
        self.calls.append({
            "op": operation.op,
            "resource_ref": operation.resource_ref,
            "payload": json.loads(json.dumps(operation.payload)),
        })
        if operation.op == "hue.get_groups_overview":
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = json.loads(json.dumps(self.topology))
            return result
        if operation.op == "hue.post_zone":
            self._zone_n += 1
            rid = f"zone-{self._zone_n}"
            children = [c["rid"] for c in operation.payload.get("children", [])]
            l2d = self.topology.get("light_to_device", {})
            members = sorted(lid for lid, did in l2d.items() if did in set(children))
            self.topology.setdefault("groups", []).append({
                "rid": rid, "rtype": "zone",
                "name": operation.payload["metadata"]["name"],
                "light_rids": members,
            })
            result = receipt(True, "hue_v2", operation.op, "HTTP 201")
            result["data"] = {"zone_rid": rid}
            return result
        if operation.op == "hue.put_zone":
            children = [c["rid"] for c in operation.payload.get("children", [])]
            l2d = self.topology.get("light_to_device", {})
            members = sorted(lid for lid, did in l2d.items() if did in set(children))
            for g in self.topology.get("groups", []):
                if g.get("rid") == operation.resource_ref:
                    g["light_rids"] = members
                    return receipt(True, "hue_v2", operation.op, "HTTP 200")
            return receipt(False, "hue_v2", operation.op, "no such zone")
        if operation.op == "hue.get_group_lights":
            if isinstance(self.group_light_rids, dict):
                rids = list(self.group_light_rids.get(operation.resource_ref, []))
            else:
                rids = list(self.group_light_rids)
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = {"light_rids": rids}
            return result
        if operation.op == "hue.get_light":
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = {"resource": self.light_states.get(
                operation.resource_ref,
                {"on": {"on": True}, "dimming": {"brightness": 50.0}},
            )}
            return result
        if operation.op == "hue.find_scene":
            wanted = (operation.payload or {}).get("name")
            match = next(
                (s for s in self.existing_scenes.values() if s["name"] == wanted), None
            )
            result = receipt(True, "hue_v2", operation.op, "found" if match else "no match")
            result["data"] = {
                "resource_id": match["resource_id"] if match else None,
                "matched": match is not None,
            }
            return result
        if operation.op == "hue.post_scene":
            result = receipt(True, "hue_v2", operation.op, "HTTP 201")
            result["data"] = {"resource_id": self.create_rid}
            return result
        return receipt(True, operation.provider, operation.op, "ok")


def _grouped_fixture(fixture_id, group_id, resource_id):
    return {
        "id": fixture_id,
        "name": fixture_id,
        "groups": [group_id],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": resource_id,
                    "hue_group_id": group_id, "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True},
    }


def _add_dynamic_scene(engine, scene_id, fixture_states, *, target_ids):
    _add_scene(engine, scene_id, fixture_states, target_ids=target_ids,
               speed=0.4, motion="palette_cycle")


def _add_scene(engine, scene_id, fixture_states, *, target_ids, speed=0.4, motion="palette_cycle", palette=None):
    document = {
        "schema_version": 2,
        "id": scene_id,
        "name": scene_id.replace("_", " ").title(),
        "target_ids": list(target_ids),
        "motion": {"mode": motion, "speed": speed, "strategy": "auto"},
        "fixture_states": fixture_states,
    }
    if palette is not None:
        document["palette"] = palette
    engine._store.scenes.add_scene(document)


def _session_execution(result, fixture_id):
    executions = result["data"]["playback"]["fixture_executions"]
    return next(e for e in executions if e["fixture_id"] == fixture_id)


def _engine_with_wled_scene(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_scene(engine, "r5_dyn", {
        "g_strip": {"on": True, "brightness": 55.0},
        "wled_seg": {"on": True, "brightness": 50.0,
                     "provider_ext": {"wled": {"fx": 9, "sx": 102}}}},
        target_ids=["office"])
    return engine


# ---------------------------------------------------------------------------
# hue: per-light path preserved
# ---------------------------------------------------------------------------


def test_dynamic_hue_without_group_metadata_fails_honestly_without_provider_writes(tmp_path):
    """R5D corrective: there is no per-light Hue dynamic path anymore. A
    dynamic-native Hue fixture without discovered group metadata cannot
    realize through a managed scene and must fail honestly — no light write,
    no scene mutation, no dynamics anywhere."""
    engine = make_engine(make_store(tmp_path / "store"))
    realizer = engine._realizer
    calls: list[dict] = []
    inner = realizer._executor

    class SpyExecutor:
        def execute(self, operation, fixture):
            calls.append({
                "op": operation.op,
                "payload": json.loads(json.dumps(operation.payload)),
            })
            return inner.execute(operation, fixture)

    realizer._executor = SpyExecutor()

    result = engine.handle({"command": "playback.start", "scene_id": "aurora"})
    assert result["ok"] is True
    sid = result["data"]["session_id"]
    session = next(
        s for s in engine.status()["playback"]["sessions"] if s["session_id"] == sid
    )
    g_strip = next(e for e in session["fixture_executions"] if e["fixture_id"] == "g_strip")
    assert g_strip["execution"] == "native_scene"
    assert g_strip["ok"] is False
    assert g_strip["fidelity"] == "unsupported"
    assert "topology read failed" in g_strip["detail"]
    # no provider op of any kind was attempted for the un-topologized fixture
    assert not any("scene" in c["op"] for c in calls)
    g_strip_calls = [c for c in calls if c["op"] == "hue.put_light"
                     and c.get("resource_ref") == "g-strip-rid"]
    assert g_strip_calls == []
    assert realizer._managed.resource_id("aurora", "group_a") is None


def test_g_strip_routes_through_managed_scene_realization(tmp_path):
    """Required regression: g_strip (gradient + dynamic_native) now realizes
    through an SS isolation zone + managed scene, not a light dynamics PUT."""
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.bind("g_strip", {
        "provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "g-strip-rid",
    })
    bridge = FakeSceneBridgeExecutor(
        create_rid="managed-1",
        topology={"groups": [], "light_to_device": {"g-strip-rid": "dev-g"}},
    )
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue.json"),
        lambda: "2026-09-12T00:00:00Z",
        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"),
    )
    _add_scene(engine, "gstrip_dyn", {"g_strip": {"on": True, "brightness": 55.0}},
               target_ids=["g_strip"])
    result = engine.handle({"command": "playback.start", "scene_id": "gstrip_dyn"})
    assert result["ok"] is True
    execution = _session_execution(result, "g_strip")
    assert execution["execution"] == "native_scene"
    assert execution["fidelity"] == "native"
    assert execution["ok"] is True
    ops = [c["op"] for c in bridge.calls]
    assert "hue.post_zone" in ops and "hue.post_scene" in ops and "hue.recall_scene" in ops
    assert not any(c["op"] == "hue.put_light" for c in bridge.calls)
    zone = next(c for c in bridge.calls if c["op"] == "hue.post_zone")
    assert zone["payload"]["metadata"]["name"] == "SS-Z-" + zone_membership_fingerprint(["g-strip-rid"])
    assert zone["payload"]["children"] == [{"rid": "g-strip-rid", "rtype": "light"}]
    assert zone["payload"]["metadata"]["archetype"] == "other"
    recall = next(c for c in bridge.calls if c["op"] == "hue.recall_scene")
    assert recall["payload"] == {"recall": {"action": "dynamic_palette"}}


# ---------------------------------------------------------------------------
# hue managed scenes: aggregation, two-group, reuse, failure
# ---------------------------------------------------------------------------


def test_grouped_hue_fixtures_aggregate_into_one_managed_scene(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.add_fixture({
        "id": "bar_a", "name": "bar_a", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-a", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_fixture({
        "id": "bar_b", "name": "bar_b", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-b", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_target({"id": "group_a", "name": "Group A"})
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"rid-a": "dev-a", "rid-b": "dev-b"}})
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z",
    )
    _add_scene(engine, "grouped_dyn", {
        "bar_a": {"on": True, "brightness": 60.0, "color": "#ff0000"},
        "bar_b": {"on": True, "brightness": 40.0, "color": "#0000ff"},
    }, target_ids=["group_a"])
    result = engine.handle({"command": "playback.start", "scene_id": "grouped_dyn"})
    assert result["ok"] is True

    mutations = [c for c in bridge.calls if c["op"] in ("hue.post_scene", "hue.put_scene")]
    assert len(mutations) == 1  # one group -> exactly one managed-scene mutation
    actions = mutations[0]["payload"]["actions"]
    assert len(actions) == 2
    assert sorted(a["target"]["rid"] for a in actions) == ["rid-a", "rid-b"]

    executions = result["data"]["playback"]["fixture_executions"]
    by_id = {e["fixture_id"]: e for e in executions if e["fixture_id"] in ("bar_a", "bar_b")}
    assert all(e["execution"] == "native_scene" and e["ok"] for e in by_id.values())


def test_scene_spanning_two_hue_groups_joins_one_exact_ss_zone(tmp_path):
    """Model change (managed zones): participants from two different rooms
    join ONE Scene-Studio-owned zone whose membership is exactly the
    participant set — the isolation boundary for group-wide animation."""
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.add_fixture({
        "id": "bar_a", "name": "bar_a", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-a", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_fixture({
        "id": "bar_b", "name": "bar_b", "groups": ["group_b"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-b", "hue_group_id": "group_b", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_target({"id": "group_a", "name": "Group A"})
    engine._store.fixtures.add_target({"id": "group_b", "name": "Group B"})
    bridge = FakeSceneBridgeExecutor(
        create_rid="bridge-scene-2",
        topology={"groups": [], "light_to_device": {"rid-a": "dev-a", "rid-b": "dev-b"}})
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_zones.json".replace("hue_zones", "hue_scenes")),
        lambda: "2026-09-12T00:00:00Z",
        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"),
    )
    _add_scene(engine, "two_group", {
        "bar_a": {"on": True, "color": "#ff0000"},
        "bar_b": {"on": True, "color": "#00ff00"},
    }, target_ids=["group_a", "group_b"])
    result = engine.handle({"command": "playback.start", "scene_id": "two_group"})
    assert result["ok"] is True

    creates = [c for c in bridge.calls if c["op"] == "hue.post_zone"]
    assert len(creates) == 1
    assert sorted(c["rid"] for c in creates[0]["payload"]["children"]) == ["rid-a", "rid-b"]
    scene_posts = [c for c in bridge.calls if c["op"] == "hue.post_scene"]
    assert len(scene_posts) == 1
    assert scene_posts[0]["payload"]["group"]["rtype"] == "zone"
    assert sorted(a["target"]["rid"] for a in scene_posts[0]["payload"]["actions"]) == ["rid-a", "rid-b"]
    recalls = [c for c in bridge.calls if c["op"] == "hue.recall_scene"]
    assert len(recalls) == 1

    state_doc = json.loads((tmp_path / "ps" / "hue_zones.json").read_text(encoding="utf-8"))
    zone_entry = next(iter(state_doc["zones"].values()))
    assert sorted(zone_entry["light_rids"]) == ["rid-a", "rid-b"]
    scene_doc = json.loads((tmp_path / "ps" / "hue_scenes.json").read_text(encoding="utf-8"))
    assert sorted(scene_doc["managed"]) == [
        f"two_group::{zone_entry['zone_rid']}",
    ]


def test_managed_resource_id_persists_across_engines(tmp_path):
    """Engine A creates the managed resource; the resource id persists in
    provider state so engine B (same store) updates instead of re-creating."""
    ps_path = tmp_path / "ps" / "hue_scenes.json"
    engine_a = make_engine(make_store(tmp_path / "store"))
    engine_a._store.fixtures.add_fixture({
        "id": "bar_a", "name": "bar_a", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-a", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine_a._store.fixtures.add_target({"id": "group_a", "name": "Group A"})
    bridge_a = FakeSceneBridgeExecutor(
        create_rid="bridge-scene-1",
        topology={"groups": [], "light_to_device": {"rid-a": "dev-a"}})
    engine_a._realizer = PlaybackRealizer(
        bridge_a, HueManagedSceneStore(ps_path), lambda: "2026-09-12T00:00:00Z")
    _add_scene(engine_a, "grouped_dyn", {
        "bar_a": {"on": True, "color": "#ff0000"}}, target_ids=["group_a"])
    assert engine_a.handle({"command": "playback.start", "scene_id": "grouped_dyn"})["ok"]
    assert len([c for c in bridge_a.calls if c["op"] == "hue.post_scene"]) == 1

    # engine B over the same store root: registry already persisted on disk
    engine_b = make_engine(SceneStudioStore(tmp_path / "store"))
    engine_b._realizer = PlaybackRealizer(
        FakeSceneBridgeExecutor(), HueManagedSceneStore(ps_path),
        lambda: "2026-09-12T00:00:00Z")
    engine_b._realizer = PlaybackRealizer(
        FakeSceneBridgeExecutor(
            topology={"groups": [], "light_to_device": {"rid-a": "dev-a"}}),
        HueManagedSceneStore(ps_path), lambda: "2026-09-12T00:00:00Z",
        HueManagedZoneStore(ps_path.parent / "hue_zones.json"))
    r = engine_b.handle({"command": "playback.start", "scene_id": "grouped_dyn"})
    assert r["ok"] is True
    doc = json.loads(ps_path.read_text(encoding="utf-8"))
    assert sorted(doc["managed"]) == ["grouped_dyn::zone-1"]
    zone_doc = json.loads((ps_path.parent / "hue_zones.json").read_text(encoding="utf-8"))
    assert len(zone_doc["zones"]) == 1  # zone reused across engines too


def test_managed_scene_create_vs_update_reuse_paths(tmp_path):
    """First play creates; second play of the SAME scene updates in place."""
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.add_fixture({
        "id": "bar_a", "name": "bar_a", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-a", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_target({"id": "group_a", "name": "Group A"})
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"rid-a": "dev-a", "rid-b": "dev-b"}})
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z",
    )
    _add_scene(engine, "grouped_dyn", {
        "bar_a": {"on": True, "color": "#ff0000"}}, target_ids=["group_a"])
    r1 = engine.handle({"command": "playback.start", "scene_id": "grouped_dyn"})
    engine.handle({"command": "playback.stop", "session_id": r1["data"]["session_id"]})
    r2 = engine.handle({"command": "playback.start", "scene_id": "grouped_dyn"})

    creates = [c for c in bridge.calls if c["op"] == "hue.post_scene"]
    assert len(creates) == 1  # second play reused the managed resource
    assert r2["ok"] is True


def test_hue_api_failure_becomes_honest_execution_failure(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.add_fixture({
        "id": "bar_a", "name": "bar_a", "groups": ["group_a"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "rid-a", "hue_group_id": "group_a", "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    engine._store.fixtures.add_target({"id": "group_a", "name": "Group A"})
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"rid-a": "dev-a"}})
    real_execute = bridge.execute

    def failing_execute(operation, fixture):
        if operation.op == "hue.post_scene":
            return receipt(False, "hue_v2", operation.op, "HTTP 503 bridge unreachable")
        return real_execute(operation, fixture)

    bridge.execute = failing_execute
    engine._realizer = PlaybackRealizer(
        bridge, HueManagedSceneStore(tmp_path / "ps" / "hue_scenes.json"),
        lambda: "2026-09-12T00:00:00Z",
    )
    _add_scene(engine, "grouped_dyn", {
        "bar_a": {"on": True, "color": "#ff0000"}}, target_ids=["group_a"])
    result = engine.handle({"command": "playback.start", "scene_id": "grouped_dyn"})
    assert result["ok"] is True  # session still starts
    executions = result["data"]["playback"]["fixture_executions"]
    bar = next(e for e in executions if e["fixture_id"] == "bar_a")
    assert bar["ok"] is False
    assert bar["execution"] == "native_scene"
    assert "bridge unreachable" in bar["detail"]
    assert engine._realizer._managed.resource_id("grouped_dyn", "group_a") is None


# ---------------------------------------------------------------------------
# wled freeze lifecycle + stale-freeze rule + batching
# ---------------------------------------------------------------------------


def _session_execution(result, fixture_id):
    executions = result["data"]["playback"]["fixture_executions"]
    return next(e for e in executions if e["fixture_id"] == fixture_id)


def _engine_with_wled_scene(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_scene(engine, "r5_dyn", {
        "g_strip": {"on": True, "brightness": 55.0},
        "wled_seg": {"on": True, "brightness": 50.0,
                     "provider_ext": {"wled": {"fx": 9, "sx": 102}}}},
        target_ids=["office"])
    return engine


def test_wled_start_emits_native_effect_data(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    result = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    assert result["ok"] is True
    wled = _session_execution(result, "wled_seg")
    assert wled["execution"] == "native_effect"
    assert wled["fidelity"] == "native"
    ops = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert ops
    seg = ops[-1]["payload"]["seg"][0]
    assert seg["fx"] == 9 and seg["sx"] == 102


def test_wled_pause_emits_per_segment_freeze_true(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    engine._executor.calls.clear()

    paused = engine.handle({"command": "playback.pause", "session_id": sid})
    assert paused["ok"] is True
    frz_ops = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert frz_ops
    for op in frz_ops:
        for seg in op["payload"]["seg"]:
            assert seg["frz"] is True


def test_wled_resume_emits_freeze_false(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    engine.handle({"command": "playback.pause", "session_id": sid})
    engine._executor.calls.clear()

    engine.handle({"command": "playback.resume", "session_id": sid})
    unfreeze = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert unfreeze
    # the LAST post_state op is the resume's frz:false clear
    for seg in unfreeze[-1]["payload"]["seg"]:
        assert seg["frz"] is False


def test_wled_stop_freezes_rather_than_resumes(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    engine._executor.calls.clear()

    engine.handle({"command": "playback.stop", "session_id": sid})
    stop_ops = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert stop_ops
    for op in stop_ops:
        for seg in op["payload"]["seg"]:
            assert seg["frz"] is True  # frozen at last frame, NOT resumed


def test_subsequent_start_clears_stale_freeze(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    engine.handle({"command": "playback.stop", "session_id": started["data"]["session_id"]})
    engine._executor.calls.clear()

    restarted = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    assert restarted["ok"] is True
    start_ops = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert start_ops
    for op in start_ops:
        for seg in op["payload"]["seg"]:
            assert seg.get("frz") is False  # stale freeze explicitly cleared


def test_subsequent_static_apply_clears_stale_freeze(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    engine.handle({"command": "playback.stop", "session_id": sid})
    engine._executor.calls.clear()

    engine.handle({"command": "scene.apply", "scene_id": "r5_dyn", "target_id": "wled_seg"})
    wled_ops = [c for c in engine._executor.calls
                if c["op"] == "wled.post_state" and "aabbccddeeff" in c["resource_ref"]]
    assert wled_ops
    for op in wled_ops:
        for seg in op["payload"]["seg"]:
            assert seg.get("frz") is False


def test_wled_sibling_segments_remain_untouched(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    engine._executor.calls.clear()

    engine.handle({"command": "playback.pause", "session_id": sid})
    wled_ops = [c for c in engine._executor.calls if c["op"] == "wled.post_state"]
    assert wled_ops
    for op in wled_ops:
        assert [seg["id"] for seg in op["payload"]["seg"]] == [0]  # owned only


def test_multiple_targeted_segments_batch_into_one_controller_mutation(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    engine._store.fixtures.add_fixture({
        "id": "wled_seg_1", "name": "WLED Segment 1", "groups": ["office"],
        "binding": {"provider": "wled", "device_id": "aabbccddeeff", "segment_ids": [1]},
        "capabilities": {"on_off": True, "brightness": True, "dynamic_native": True},
    })
    _add_scene(engine, "two_seg", {
        "wled_seg": {"on": True, "provider_ext": {"wled": {"fx": 9}}},
        "wled_seg_1": {"on": True, "provider_ext": {"wled": {"fx": 9}}},
    }, target_ids=["office"])
    started = engine.handle({"command": "playback.start", "scene_id": "two_seg"})
    sid = started["data"]["session_id"]
    engine._executor.calls.clear()

    engine.handle({"command": "playback.pause", "session_id": sid})
    frz_ops = [c for c in engine._executor.calls
               if c["op"] == "wled.post_state"
               and all(s.get("frz") for s in c["payload"]["seg"])]
    assert len(frz_ops) == 1  # batched: one mutation for both segments
    assert sorted(s["id"] for s in frz_ops[0]["payload"]["seg"]) == [0, 1]


# ---------------------------------------------------------------------------
# multi-session / preemption / failure honesty / persistence
# ---------------------------------------------------------------------------


def test_two_disjoint_sessions_execute_on_different_providers(tmp_path):
    """Single-fixture scenes on disjoint targets: g_strip (hue) and
    wled_seg (wled) coexist as separate active sessions."""
    engine = make_engine(make_store(tmp_path / "store"))
    _add_scene(engine, "hue_dyn", {
        "g_strip": {"on": True, "brightness": 55.0}}, target_ids=["g_strip"])
    _add_dynamic_scene(engine, "wled_dyn", {
        "wled_seg": {"on": True, "brightness": 50.0,
                      "provider_ext": {"wled": {"fx": 9, "sx": 102}}}},
        target_ids=["wled_seg"])
    r1 = engine.handle({"command": "playback.start", "scene_id": "hue_dyn"})
    r2 = engine.handle({"command": "playback.start", "scene_id": "wled_dyn"})
    assert r1["ok"] and r2["ok"], (r1, r2)
    status = engine.status()["playback"]
    assert status["counts"]["active"] == 2
    s1 = next(s for s in status["sessions"] if s["session_id"] == r1["data"]["session_id"])
    s2 = next(s for s in status["sessions"] if s["session_id"] == r2["data"]["session_id"])
    assert {e["provider"] for e in s1["fixture_executions"]} == {"hue_v2"}
    assert {e["provider"] for e in s2["fixture_executions"]} == {"wled"}
    assert not set(s1["fixture_ids"]) & set(s2["fixture_ids"])


def test_overlap_preemption_works_with_real_provider_realization(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    first = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid_first = first["data"]["session_id"]
    wled_exec = next(e for e in first["data"]["playback"]["fixture_executions"]
                     if e["fixture_id"] == "wled_seg")
    assert wled_exec["execution"] == "native_effect"

    second = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid_second = second["data"]["session_id"]
    assert second["data"]["preempted_session_ids"] == [sid_first]
    status = engine.status()["playback"]
    states = {s["session_id"]: s["state"] for s in status["sessions"]}
    assert states[sid_first] == "stopped" and states[sid_second] == "active"
    assert all(e["execution"] != "pending"
               for s in status["sessions"] for e in s["fixture_executions"])


def test_partial_provider_failure_keeps_session_consistent(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    engine._executor.fail_ops = {"wled.post_state"}  # wled realization fails

    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    sid = started["data"]["session_id"]
    session = next(s for s in engine.status()["playback"]["sessions"]
                   if s["session_id"] == sid)
    by_fixture = {e["fixture_id"]: e for e in session["fixture_executions"]}
    # g_strip carries no discovered group metadata in this store: the
    # corrected model fails it honestly instead of a per-light dynamics write
    assert by_fixture["g_strip"]["ok"] is False
    assert by_fixture["g_strip"]["execution"] == "native_scene"
    assert "topology read failed" in by_fixture["g_strip"]["detail"]
    wled_entries = {fid: e for fid, e in by_fixture.items() if fid.startswith("wled")}
    assert wled_entries
    for fid, e in wled_entries.items():
        assert e["ok"] is False
        assert e["detail"]
    # persisted document round-trips with real execution kinds
    from scene_studio.domain.playback import PlaybackState

    doc = json.loads(engine._playback_path.read_text(encoding="utf-8"))
    state = PlaybackState.from_dict(doc)
    assert PlaybackState.from_dict(state.to_dict()) == state


def test_restart_orphan_behavior_unchanged_under_real_realization(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    fresh = make_engine(SceneStudioStore(tmp_path / "store"))
    status = fresh.status()["playback"]
    assert status["counts"]["orphaned"] == 1
    assert status["owned_fixture_count"] == 0
    orphan = status["sessions"][0]
    assert orphan["state"] == "orphaned"
    assert all(e["execution"] != "pending" for e in orphan["fixture_executions"])


# ---------------------------------------------------------------------------
# R5B corrective hardening: topology, recovery, cardinality, lifecycle
# ---------------------------------------------------------------------------


def _add_managed_hue(engine, fixture_id, resource_id, *, group_id="bridge-room-42"):
    engine._store.fixtures.add_fixture({
        "id": fixture_id, "name": fixture_id, "groups": ["logical_studio"],
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": resource_id,
                    "hue_group_id": group_id, "hue_group_type": "room"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": True},
    })
    if not any(target.id == "logical_studio" for target in engine._store.fixtures.list_targets()):
        engine._store.fixtures.add_target({"id": "logical_studio", "name": "Logical Studio"})


def test_gradient_palette_over_capacity_uses_managed_scene_not_generic_light_put(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    # The bridge room ID is not the logical target id; it arrives beside the binding.
    engine._store.fixtures.bind("g_strip", {
        "provider": "hue_v2", "bridge_id": "bridge1", "resource_id": "g-strip-rid",
        "hue_group_id": "bridge-room-42", "hue_group_type": "room",
    })
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"g-strip-rid": "dev-g"}})
    engine._realizer = PlaybackRealizer(bridge, HueManagedSceneStore(tmp_path / "ps" / "hue.json"), lambda: "2026-09-12T00:00:00Z")
    _add_scene(engine, "too_many_gradient_colors", {"g_strip": {"on": True, "brightness": 55}},
               target_ids=["g_strip"], speed=0.4, motion="palette_cycle",
               palette=["#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff", "#00ffff"])
    result = engine.handle({"command": "playback.start", "scene_id": "too_many_gradient_colors"})
    assert result["ok"]
    assert [call["op"] for call in bridge.calls].count("hue.post_scene") == 1
    assert not any(call["op"] == "hue.put_scene_dynamic" for call in bridge.calls)
    execution = _session_execution(result, "g_strip")
    assert execution["execution"] == "native_scene"


def test_managed_scene_recovery_precedes_create_and_never_persists_label_id(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_managed_hue(engine, "managed_a", "light-a")
    _add_scene(engine, "recoverable", {"managed_a": {"on": True, "color": "#ff0000"}},
               target_ids=["logical_studio"])
    label = "SS-recoverable-zone-1"
    bridge = FakeSceneBridgeExecutor(
        {"owned": {"name": label, "resource_id": "real-scene-rid"}},
        topology={"groups": [], "light_to_device": {"light-a": "dev-a"}})
    state = HueManagedSceneStore(tmp_path / "ps" / "hue.json")
    engine._realizer = PlaybackRealizer(bridge, state, lambda: "2026-09-12T00:00:00Z",
                                        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"))
    assert engine.handle({"command": "playback.start", "scene_id": "recoverable"})["ok"]
    assert not any(call["op"] == "hue.post_scene" for call in bridge.calls)
    assert state.resource_id("recoverable", "zone-1") == "real-scene-rid"


def test_post_without_resource_id_fails_honestly_without_fake_persisted_id(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_managed_hue(engine, "managed_a", "light-a")
    _add_scene(engine, "no_post_rid", {"managed_a": {"on": True, "color": "#ff0000"}},
               target_ids=["logical_studio"])
    state = HueManagedSceneStore(tmp_path / "ps" / "hue.json")
    engine._realizer = PlaybackRealizer(FakeSceneBridgeExecutor(create_rid=None), state, lambda: "2026-09-12T00:00:00Z")
    result = engine.handle({"command": "playback.start", "scene_id": "no_post_rid"})
    assert _session_execution(result, "managed_a")["ok"] is False
    assert state.resource_id("no_post_rid", "bridge-room-42") is None


def test_stale_or_corrupt_state_recovers_owned_scene_before_creating_another(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_managed_hue(engine, "managed_a", "light-a")
    _add_scene(engine, "stale_recovery", {"managed_a": {"on": True, "color": "#ff0000"}},
               target_ids=["logical_studio"])
    label = "SS-stale_recovery-zone-1"
    bridge = FakeSceneBridgeExecutor(
        {"owned": {"name": label, "resource_id": "recovered-rid"}},
        topology={"groups": [], "light_to_device": {"light-a": "dev-a"}})
    state = HueManagedSceneStore(tmp_path / "ps" / "hue.json")
    state.record("stale_recovery", "zone-1", "stale-rid", label, "2026-09-12T00:00:00Z")
    real_execute = bridge.execute
    def stale_execute(operation, fixture):
        if operation.op == "hue.put_scene" and operation.resource_ref == "stale-rid":
            bridge.calls.append({"op": operation.op, "resource_ref": operation.resource_ref, "payload": operation.payload})
            return receipt(False, "hue_v2", operation.op, "HTTP 404")
        return real_execute(operation, fixture)
    bridge.execute = stale_execute
    engine._realizer = PlaybackRealizer(bridge, state, lambda: "2026-09-12T00:00:00Z")
    assert engine.handle({"command": "playback.start", "scene_id": "stale_recovery"})["ok"]
    assert state.resource_id("stale_recovery", "zone-1") == "recovered-rid"
    assert not any(call["op"] == "hue.post_scene" for call in bridge.calls)

    # A corrupt/missing state file follows the same label+group recovery path.
    corrupt_path = tmp_path / "ps" / "corrupt.json"
    corrupt_path.write_text("not json", encoding="utf-8")
    corrupt_state = HueManagedSceneStore(corrupt_path)
    bridge.calls.clear()
    engine._realizer = PlaybackRealizer(bridge, corrupt_state, lambda: "2026-09-12T00:00:00Z",
                                        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"))
    assert engine.handle({"command": "playback.start", "scene_id": "stale_recovery"})["ok"]
    assert corrupt_state.resource_id("stale_recovery", "zone-1") == "recovered-rid"
    assert not any(call["op"] == "hue.post_scene" for call in bridge.calls)


def test_lifecycle_groups_managed_hue_and_preserves_complete_execution_set(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_managed_hue(engine, "managed_a", "light-a")
    _add_managed_hue(engine, "managed_b", "light-b")
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"light-a": "dev-a", "light-b": "dev-b"}})
    engine._realizer = PlaybackRealizer(bridge, HueManagedSceneStore(tmp_path / "ps" / "hue.json"), lambda: "2026-09-12T00:00:00Z",
                                        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"))
    _add_scene(engine, "shared_lifecycle", {
        "managed_a": {"on": True, "color": "#ff0000"},
        "managed_b": {"on": True, "color": "#00ff00"},
    }, target_ids=["logical_studio"])
    started = engine.handle({"command": "playback.start", "scene_id": "shared_lifecycle"})
    sid = started["data"]["session_id"]
    assert {item["fixture_id"] for item in started["data"]["playback"]["fixture_executions"]} == {"managed_a", "managed_b"}
    for command in ("playback.pause", "playback.resume", "playback.stop"):
        bridge.calls.clear()
        result = engine.handle({"command": command, "session_id": sid})
        assert len([call for call in bridge.calls if call["op"] == "hue.recall_scene"]) == 1
        if command != "playback.stop":
            assert {item["fixture_id"] for item in result["data"]["playback"]["fixture_executions"]} == {"managed_a", "managed_b"}


def test_managed_scene_recovery_label_survives_scene_rename_and_lost_state(tmp_path):
    engine = make_engine(make_store(tmp_path / "store"))
    _add_managed_hue(engine, "managed_a", "light-a")
    _add_scene(engine, "stable_identity", {"managed_a": {"on": True, "color": "#ff0000"}},
               target_ids=["logical_studio"])
    state_path = tmp_path / "ps" / "hue.json"
    bridge = FakeSceneBridgeExecutor(
        topology={"groups": [], "light_to_device": {"light-a": "dev-a"}})
    engine._realizer = PlaybackRealizer(bridge, HueManagedSceneStore(state_path), lambda: "2026-09-12T00:00:00Z",
                                        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"))
    first = engine.handle({"command": "playback.start", "scene_id": "stable_identity"})
    assert first["ok"]
    original_label = "SS-stable_identity-zone-1"
    bridge.existing_scenes["created"] = {"name": original_label, "resource_id": "bridge-scene-1"}
    assert engine.handle({"command": "scene.rename", "scene_id": "stable_identity", "name": "Renamed Display Label"})["ok"]
    state_path.write_text("corrupt", encoding="utf-8")
    bridge.calls.clear()
    engine._realizer = PlaybackRealizer(bridge, HueManagedSceneStore(state_path), lambda: "2026-09-12T00:00:00Z")
    assert engine.handle({"command": "playback.start", "scene_id": "stable_identity"})["ok"]
    assert any(call["op"] == "hue.find_scene" and call["payload"]["name"] == original_label for call in bridge.calls)
    assert not any(call["op"] == "hue.post_scene" for call in bridge.calls)


def _engine_from_committed_samples(tmp_path):
    fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures"
    registry_doc = json.loads((fixtures_dir / "registry.sample.json").read_text(encoding="utf-8"))
    scenes_doc = json.loads((fixtures_dir / "scenes.sample.json").read_text(encoding="utf-8"))
    store = SceneStudioStore(tmp_path / "sample-store")
    for target in registry_doc["targets"]:
        store.fixtures.add_target(target)
    for fixture in registry_doc["fixtures"]:
        store.fixtures.add_fixture(fixture)
    for scene in scenes_doc["scenes"]:
        store.scenes.add_scene(scene)
    return make_engine(store)


def test_committed_sample_dynamic_playback_covers_hue_wled_topology_and_cardinality(tmp_path):
    engine = _engine_from_committed_samples(tmp_path)
    fixtures_dir = Path(__file__).resolve().parents[1] / "fixtures"
    engine._fetchers = DiscoveryFetchers(
        fetch_hue=lambda: json.loads((fixtures_dir / "recorded" / "hue_clip_lights.twilight.json").read_text(encoding="utf-8")),
        fetch_hue_rooms=lambda: json.loads((fixtures_dir / "recorded" / "hue_clip_rooms.twilight.json").read_text(encoding="utf-8")),
    )
    assert engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})["ok"]
    assert engine.handle({"command": "fixture.rebind", "fixture_id": "g_strip",
                          "observation_id": "hue_v2:2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"})["ok"]
    sample_registry = json.loads((fixtures_dir / "registry.sample.json").read_text(encoding="utf-8"))
    l2d = {f["binding"]["resource_id"]: f"dev-" + f["id"]
           for f in sample_registry["fixtures"]
           if (f.get("binding") or {}).get("provider") == "hue_v2"}
    bridge = FakeSceneBridgeExecutor(topology={"groups": [], "light_to_device": l2d})
    engine._realizer = PlaybackRealizer(bridge, HueManagedSceneStore(tmp_path / "ps" / "hue.json"), lambda: "2026-09-12T00:00:00Z",
                                        HueManagedZoneStore(tmp_path / "ps" / "hue_zones.json"))
    scene = engine._store.scenes.get_scene("aurora_flow")
    registry = engine._store.fixtures.registry()
    plan = engine._build_plan(scene, registry, None)
    result = engine.handle({"command": "playback.start", "scene_id": "aurora_flow"})
    assert result["ok"]
    executions = result["data"]["playback"]["fixture_executions"]
    assert len(executions) == len(plan.fixture_plans)
    assert {item["fixture_id"] for item in executions} == {item.fixture_id for item in plan.fixture_plans}
    by_id = {item["fixture_id"]: item for item in executions}
    assert by_id["g_strip"]["execution"] == "native_scene"
    assert by_id["middle_bar"]["execution"] == "native_scene"
    assert by_id["wled_seg_0"]["execution"] == "native_effect"
    middle = next(item for item in registry.fixtures if item.id == "middle_bar")
    g_strip = next(item for item in registry.fixtures if item.id == "g_strip")
    assert g_strip.binding.hue_group_id == "room-001"
    assert middle.binding.hue_group_id == "room-001"
    scene_posts = [call for call in bridge.calls if call["op"] == "hue.post_scene"]
    assert len(scene_posts) == 1
    assert scene_posts[0]["payload"]["group"]["rtype"] == "zone"
    # every dynamic participant contributes an action (default_state pulls
    # the other dynamic-native studio fixtures in beside g_strip/middle_bar)
    assert sorted(a["target"]["rid"] for a in scene_posts[0]["payload"]["actions"]) == [
        "2a2c45a9-8a61-4c04-bdaf-9bd928f9316a",  # g_strip
        "68ad5817-0907-4811-9a5c-e3a1d4a69803",  # middle_bar
        "73acf87a-8835-435d-b422-e889c5017dac",  # lamp
        "8e687c33-b45c-457e-92f6-0499fd7141eb",  # lower_bar
        "d81298a8-0ae6-4b85-9fbd-bc7eaac50898",  # upper_bar
        "ffaed708-d411-455e-843b-8bebae36f67c",  # office_strip (now a studio Hue member)
    ]


def test_archive_stops_active_provider_native_session_before_archiving(tmp_path):
    engine = _engine_with_wled_scene(tmp_path)
    started = engine.handle({"command": "playback.start", "scene_id": "r5_dyn"})
    assert started["ok"]
    engine._executor.calls.clear()
    archived = engine.handle({"command": "scene.archive", "scene_id": "r5_dyn"})
    assert archived["ok"]
    # archive-stop freezes the WLED segment; Hue lights receive NO dynamics
    # write (the refuted per-light path is gone; the group-less fixture
    # already failed honestly at start)
    assert any(call["op"] == "wled.post_state" and call["payload"]["seg"][0]["frz"] is True
               for call in engine._executor.calls)
    assert not any(call["op"] == "hue.put_light" and "dynamics" in call["payload"]
                   for call in engine._executor.calls)
