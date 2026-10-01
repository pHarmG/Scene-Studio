"""R5D local rehearsal — validate the temporary R5D validation scenes through
the real Scene Store / engine / command path with a recording (no-I/O)
executor, BEFORE any live deployment.

No device contact: every provider operation is recorded, never sent. Feed it
a registry snapshot for fixture/capability realism — by default it uses the
committed sample registry; for a pre-R5D rehearsal, capture the live registry
to a local (untracked) file and pass it via --registry.

Usage:
    python backend/r5d_rehearsal.py [--registry PATH] [--out PATH]

Scenes come from fixtures/r5d/ (the same documents installed live through the
Scene Store path). With --out, the full step/ops evidence is written as JSON;
console output always carries the human-readable summary.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.api import route
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor, receipt
from scene_studio.stores import SceneStudioStore

SCENES_DIR = HERE / "fixtures" / "r5d"
DEFAULT_REGISTRY = HERE / "fixtures" / "registry.sample.json"
ALLOWLIST = {"g_strip", "wled_seg_0"}


class ManagedSceneAnsweringExecutor(RecordingExecutor):
    """Recording executor that answers hue.find_scene like a bridge with no
    pre-existing managed scene and hands out deterministic create ids."""

    def __init__(self):
        super().__init__()
        self._created = 0

    def execute(self, operation, fixture):
        if operation.op == "hue.get_groups_overview":
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            result = receipt(True, "hue_v2", operation.op, "ok")
            # Studio room containing exactly the sample g_strip light: the
            # participant set exactly covers the authoritative room, so the
            # room itself is used (no zone manufactured).
            result["data"] = {
                "groups": [
                    {"rid": "room-001", "rtype": "room", "name": "Studio",
                     "light_rids": ["2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"]},
                ],
                "light_to_device": {"2a2c45a9-8a61-4c04-bdaf-9bd928f9316a": "dev-g"},
            }
            return result
        if operation.op in ("hue.post_zone", "hue.put_zone"):
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = {"zone_rid": "zone-rehearsal"}
            return result
        if operation.op == "hue.get_group_lights":
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = {"light_rids": ["2a2c45a9-8a61-4c04-bdaf-9bd928f9316a"]}
            return result
        if operation.op == "hue.get_light":
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            result = receipt(True, "hue_v2", operation.op, "ok")
            result["data"] = {"resource": {"on": {"on": True}, "dimming": {"brightness": 60.0}}}
            return result
        if operation.op == "hue.find_scene":
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            result = receipt(True, "hue_v2", operation.op, "no match")
            result["data"] = {"resource_id": None, "matched": False}
            return result
        if operation.op == "hue.post_scene":
            self.calls.append({"provider": operation.provider, "op": operation.op,
                               "resource_ref": operation.resource_ref,
                               "payload": dict(operation.payload), "fixture_id": fixture.id})
            self._created += 1
            result = receipt(True, "hue_v2", operation.op, "HTTP 201")
            result["data"] = {"resource_id": f"rehearsal-scene-{self._created}"}
            return result
        return super().execute(operation, fixture)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY,
                        help="registry document to seed the rehearsal store "
                             "(default: the committed sample registry)")
    parser.add_argument("--out", type=Path, default=None,
                        help="optional path for the full JSON evidence")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="r5d_rehearsal_"))
    evidence = {"tmp_store": str(tmp), "registry": str(args.registry), "steps": []}

    # 1. Store seeded with the registry + the two temporary scenes via the
    #    real SceneStore path (atomic add, domain validation).
    store_root = tmp / "store"
    (store_root / "registry").mkdir(parents=True)
    (store_root / "registry" / "registry.json").write_text(args.registry.read_text())
    store = SceneStudioStore(store_root)
    for name in ("r5_dynamic_validation", "r5_static_validation"):
        doc = json.loads((SCENES_DIR / f"{name}.json").read_text())
        store.scenes.add_scene(doc)

    # 2. Fresh store instance = reload from disk (round-trip proof).
    store = SceneStudioStore(store_root)
    ids = sorted(s.id for s in store.scenes.list_scenes())
    evidence["reloaded_scene_ids"] = ids
    assert "r5_dynamic_validation" in ids and "r5_static_validation" in ids

    # 3. Engine through api.route with the recording executor.
    ex = ManagedSceneAnsweringExecutor()
    engine = SceneStudioEngine(
        store, ex, SteppingClock(), discovery_fetchers=DiscoveryFetchers()
    )

    def cmd(eng, body):
        return route(eng, "POST", "/api/scene_studio/command", body)

    def record(step, status, payload, ops_slice_from=0):
        entry = {"step": step, "http": status, "ok": payload.get("ok"),
                 "error": payload.get("error"),
                 "ops": ex.calls[ops_slice_from:]}
        evidence["steps"].append(entry)
        return entry

    # --- Step A: playback.start r5_dynamic_validation --------------------
    n0 = len(ex.calls)
    st, pl = cmd(engine, {"command": "playback.start", "scene_id": "r5_dynamic_validation"})
    record("playback.start dynamic", st, pl, n0)
    session_id = (pl.get("data") or {}).get("session_id")
    evidence["session_id"] = session_id
    assert st == 200 and pl["ok"] and session_id, pl

    # R5D corrected Hue contract: g_strip realizes through the managed scene
    # (native_scene, find/post/put/recall), never a dynamics.status light PUT.
    executions = (pl["data"].get("playback") or {}).get("fixture_executions", [])
    by_fixture = {e["fixture_id"]: e for e in executions}
    assert by_fixture["g_strip"]["execution"] == "native_scene", by_fixture.get("g_strip")
    assert by_fixture["wled_seg_0"]["execution"] == "native_effect"
    light_puts = [c for c in ex.calls if c["op"] == "hue.put_light"]
    assert all("status" not in c["payload"].get("dynamics", {}) for c in light_puts)
    managed_ops = [c["op"] for c in ex.calls if c["op"].startswith("hue.")]
    assert "hue.post_scene" in managed_ops and "hue.recall_scene" in managed_ops
    scene_post = next(c for c in ex.calls if c["op"] == "hue.post_scene")
    assert scene_post["payload"]["group"]["rtype"] in ("room", "zone")
    assert scene_post["payload"]["group"]["rid"] == "room-001"  # exact room membership
    assert scene_post["payload"]["palette"]["color"]
    assert scene_post["payload"]["speed"] == 0.5
    recall = next(c for c in ex.calls if c["op"] == "hue.recall_scene")
    assert recall["payload"] == {"recall": {"action": "dynamic_palette"}}, recall["payload"]

    # --- Step B: pause ----------------------------------------------------
    n = len(ex.calls)
    st, pl = cmd(engine, {"command": "playback.pause", "session_id": session_id})
    record("playback.pause", st, pl, n)

    # --- Step C: resume ---------------------------------------------------
    n = len(ex.calls)
    st, pl = cmd(engine, {"command": "playback.resume", "session_id": session_id})
    record("playback.resume", st, pl, n)

    # --- Step D: wrong-session protection ---------------------------------
    n = len(ex.calls)
    st, pl = cmd(engine, {"command": "playback.stop", "session_id": "does_not_exist_1"})
    record("playback.stop unknown session", st, pl, n)
    evidence["wrong_session_ops"] = len(ex.calls) - n

    # --- Step E: stop (R5B decision: remain frozen) ------------------------
    n = len(ex.calls)
    st, pl = cmd(engine, {"command": "playback.stop", "session_id": session_id})
    record("playback.stop", st, pl, n)

    # --- Step F: restart while ACTIVE -> orphaned (fresh engine instance) --
    st, pl = cmd(engine, {"command": "playback.start", "scene_id": "r5_dynamic_validation"})
    evidence["second_session_id"] = (pl.get("data") or {}).get("session_id")
    assert pl["ok"], pl
    engine2 = SceneStudioEngine(
        SceneStudioStore(store_root), ex, SteppingClock(),
        discovery_fetchers=DiscoveryFetchers()
    )
    st, pl = route(engine2, "GET", "/api/scene_studio/status")
    pb = (pl.get("playback") or {})
    live_states = {s["session_id"]: s["state"] for s in pb.get("sessions", [])}
    evidence["after_restart_states"] = live_states
    orphan_id = evidence["second_session_id"]
    assert live_states.get(orphan_id) == "orphaned", live_states

    # --- Step G: reclaim orphan, then start + static apply overlap-cancel ---
    n = len(ex.calls)
    st, pl = cmd(engine2, {"command": "playback.stop", "session_id": orphan_id})
    record("playback.stop orphan", st, pl, n)
    n = len(ex.calls)
    st, pl = cmd(engine2, {"command": "playback.start", "scene_id": "r5_dynamic_validation"})
    third = (pl.get("data") or {}).get("session_id")
    assert pl["ok"], pl
    n = len(ex.calls)
    st, pl = cmd(engine2, {"command": "scene.apply", "scene_id": "r5_static_validation"})
    record("scene.apply static (overlap cancel)", st, pl, n)
    st2, pl2 = route(engine2, "GET", "/api/scene_studio/status")
    pb2 = (pl2.get("playback") or {})
    evidence["states_after_static_apply"] = {
        s["session_id"]: {"state": s["state"], "reason": s.get("stop_reason")}
        for s in pb2.get("sessions", [])
    }
    evidence["overlap_cancelled_session"] = third

    # --- Allowlist discipline over EVERY recorded op ------------------------
    offenders = [c for c in ex.calls if c.get("fixture_id") not in ALLOWLIST]
    evidence["total_ops"] = len(ex.calls)
    evidence["allowlist_offenders"] = offenders

    # --- Console summary ----------------------------------------------------
    print("reloaded scenes:", ids)
    print("session_id:", session_id)
    for step in evidence["steps"]:
        print(f"\n== {step['step']} -> http {step['http']} ok={step['ok']} err={step['error']}")
        for c in step["ops"]:
            payload = json.dumps(c["payload"], sort_keys=True)
            print(f"   [{c['fixture_id']}] {c['provider']}:{c['op']} ref={str(c['resource_ref'])[:24]} {payload[:150]}")
    print("\nafter restart states:", live_states)
    print("after static apply:", evidence["states_after_static_apply"])
    print("total ops:", evidence["total_ops"], "| allowlist offenders:", len(offenders))
    if args.out:
        args.out.write_text(json.dumps(evidence, indent=1))
        print("evidence written:", args.out)


if __name__ == "__main__":
    main()
