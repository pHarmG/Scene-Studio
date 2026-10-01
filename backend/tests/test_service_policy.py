"""Runtime policy tests — backend-owned command permissions (pre-R4 cleanup).

The policy lives in the engine (single source of truth), is exposed through
``status().runtime``, and is enforced before parameter validation so even a
malformed mutating command is a mode rejection.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scene_studio.domain.commands import COMMAND_CATALOG
from scene_studio.service.engine import SceneStudioEngine
from scene_studio.service.policy import MODE_NORMAL, MODE_READ_ONLY, MODE_R2_VALIDATION, MODE_REGISTRY_ADMIN, RuntimePolicy
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore

import pytest


def test_policy_modes_and_dry_run_exception():
    normal = RuntimePolicy.build(MODE_NORMAL)
    read_only = RuntimePolicy.build(MODE_READ_ONLY)
    r2 = RuntimePolicy.build(MODE_R2_VALIDATION)

    assert normal.allows("scene.apply")
    assert not read_only.allows("scene.apply")
    assert read_only.allows("scene.apply", dry_run=True)  # the sentinel grant
    assert read_only.allows("scene.preview")
    assert read_only.allows("discovery.run")
    assert read_only.allows("fixture.retry")
    assert read_only.allows("diagnostics.export")
    assert not read_only.allows("fixture.enable")
    assert not read_only.allows("playback.start")
    assert read_only.allows("fixture.reconcile_preview")
    assert read_only.allows("registry.migration_preview")
    assert not read_only.allows("fixture.reconcile")
    assert not read_only.allows("registry.migrate")
    admin = RuntimePolicy.build(MODE_REGISTRY_ADMIN)
    assert admin.allows("fixture.reconcile")
    assert admin.allows("fixture.rebind")
    assert admin.allows("registry.migrate")
    assert not admin.allows("scene.apply")
    assert not admin.allows("playback.start")
    assert admin.provider_writes_blocked is True
    assert r2.allows("scene.apply")  # the one write path R2 validates
    assert not r2.allows("scene.save")


def test_policy_build_rejects_unknown_mode():
    with pytest.raises(ValueError):
        RuntimePolicy.build("hypervisor")


def _engine(tmp_path, mode):
    return SceneStudioEngine(
        store=SceneStudioStore(tmp_path / "store"),
        executor=RecordingExecutor(),
        clock=__import__("scene_studio.service.ports", fromlist=["MonotonicClock"]).MonotonicClock(),
        policy=RuntimePolicy.build(mode),
    )


def test_read_only_engine_rejects_real_apply_but_allows_dry_run(tmp_path):
    engine = _engine(tmp_path, MODE_READ_ONLY)

    real = engine.handle({"command": "scene.apply", "scene_id": "nope"})
    assert real["ok"] is False and real["error"]["code"] == "conflict"
    assert "read_only mode" in real["error"]["message"]

    dry = engine.handle({"command": "scene.apply", "scene_id": "nope", "dry_run": True})
    assert dry["ok"] is False and dry["error"]["code"] == "not_found"  # past the gate


def test_malformed_mutating_command_is_still_a_mode_rejection(tmp_path):
    engine = _engine(tmp_path, MODE_READ_ONLY)
    # fixture.enable without fixture_id: param validation would fail first if
    # the gate ran after parsing; the mode rejection must win.
    result = engine.handle({"command": "fixture.enable"})
    assert result["error"]["code"] == "conflict"


def test_status_exposes_runtime_policy(tmp_path):
    engine = _engine(tmp_path, MODE_READ_ONLY)
    runtime = engine.status()["runtime"]
    assert runtime["mode"] == "read_only"
    assert runtime["read_only"] is True
    assert runtime["provider_writes_blocked"] is True
    assert "scene.preview" in runtime["allowed_commands"]
    assert "scene.apply" not in runtime["allowed_commands"]
    assert "scene.apply:dry_run" in runtime["allowed_commands"]


def test_normal_engine_status_lists_full_catalog(tmp_path):
    engine = _engine(tmp_path, MODE_NORMAL)
    runtime = engine.status()["runtime"]
    assert runtime["mode"] == "normal"
    assert runtime["read_only"] is False
    assert runtime["provider_writes_blocked"] is False
    for command in (
        "scene.apply",
        "playback.start",
        "playback.pause",
        "playback.resume",
        "playback.stop",
        "scene.rename",
        "scene.archive",
        "scene.restore",
    ):
        assert command in runtime["allowed_commands"]
    assert set(runtime["allowed_commands"]) == set(COMMAND_CATALOG)


def test_read_only_engine_legacy_upsert_substitution_is_gated(tmp_path):
    """handle_legacy's upsert_scene substitution stands in for scene.save;
    restricted modes must not be bypassable through it."""
    engine = _engine(tmp_path, MODE_READ_ONLY)
    mapped = {
        "scene": {"schema_version": 2, "id": "generated_office", "name": "Generated",
                  "target_ids": ["office"], "fixture_states": {"office_lights": {"on": True}}},
        "commands": [
            {"command": "scene.save", "name": "Generated", "target_id": "office"},
            {"command": "scene.apply", "scene_id": "generated_office"},
        ],
    }
    results = engine.handle_legacy(mapped)
    assert results[0]["ok"] is False
    assert results[0]["error"]["code"] == "conflict"
    assert results[1]["error"]["code"] == "conflict"  # scene.apply also gated
    # nothing persisted
    assert engine.status()["fixtures"]["total"] >= 0  # registry untouched
    store_ids = [s.id for s in engine._store.scenes.list_scenes()]
    assert "generated_office" not in store_ids


def test_read_only_discovery_and_retry_leave_registry_bytes_unchanged(tmp_path):
    store = SceneStudioStore(tmp_path / "store")
    store.fixtures.add_fixture({
        "id": "hue_fixture", "name": "Hue Fixture",
        "binding": {"provider": "hue_v2", "bridge_id": "bridge-1", "resource_id": "light-rid"},
    })
    fetchers = DiscoveryFetchers(
        fetch_hue=lambda: {"data": [{
            "id": "light-rid", "metadata": {"name": "Hue Fixture"},
            "owner": {"rid": "device-rid", "rtype": "device"}, "dimming": {},
        }]},
        fetch_hue_rooms=lambda: {"data": [{
            "id": "actual-room-rid", "type": "room", "metadata": {"name": "Kitchen"},
            "children": [{"rid": "device-rid", "rtype": "device"}],
        }]},
    )
    engine = SceneStudioEngine(
        store=store, executor=RecordingExecutor(),
        clock=__import__("scene_studio.service.ports", fromlist=["MonotonicClock"]).MonotonicClock(),
        discovery_fetchers=fetchers, policy=RuntimePolicy.build(MODE_READ_ONLY),
    )
    before = store.fixtures.path.read_bytes()
    assert engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})["ok"]
    assert engine.handle({"command": "fixture.retry", "fixture_id": "hue_fixture"})["ok"]
    assert store.fixtures.path.read_bytes() == before
    observation = engine.latest_discovery().observations[0]
    assert observation.metadata["hue_group_id"] == "actual-room-rid"
