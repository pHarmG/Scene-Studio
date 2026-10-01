"""Regression coverage for capability provenance and atomic provider migration."""

from scene_studio.domain import FixtureRegistry, HealthStatus
from scene_studio.domain.fixtures import derive_health
from scene_studio.renderers import build_render_plan
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.policy import RuntimePolicy
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore
from scene_studio.discovery.hue import build_hue_observations
from scene_studio.discovery.halight import build_halight_observations


def _v1_registry():
    return {
        "schema_version": 1,
        "fixtures": [{
            "id": "custom_gradient", "name": "Custom Gradient", "health": "degraded",
            "binding": {"provider": "hue_v2", "bridge_id": "bridge", "resource_id": "old"},
            "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": False},
            "metadata": {"manufacturer": "GLEDOPTO", "model": "GL-C-103P"},
        }],
        "targets": [],
    }


def test_v1_registry_migration_is_deterministic_and_preserves_effective_caps():
    migrated = FixtureRegistry.from_dict(_v1_registry())
    assert migrated.schema_version == 2
    fixture = migrated.fixtures[0]
    assert fixture.id == "custom_gradient"
    assert derive_health(fixture) is HealthStatus.DEGRADED
    assert fixture.capability_assessment.status.value == "unknown"
    assert fixture.device_profile.model == "GL-C-103P"
    assert fixture.capabilities.gradient is None
    assert FixtureRegistry.from_dict(_v1_registry()).to_dict() == migrated.to_dict()


def test_physical_profile_cannot_grant_renderer_gradient():
    registry = FixtureRegistry.from_dict({
        "schema_version": 2,
        "fixtures": [{
            "id": "strip", "name": "Strip", "groups": ["room"],
            "binding": {"provider": "ha_light", "ha_entity_id": "light.strip"},
            "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": False},
            "device_profile": {"native_features": {"addressable_pixels": True}, "source": "manual_verified"},
        }], "targets": [{"id": "room", "name": "Room"}],
    })
    from scene_studio.domain import Scene
    scene = Scene.from_dict({"schema_version": 2, "id": "s", "name": "S", "target_ids": ["room"],
                             "fixture_states": {"strip": {"on": True, "gradient": ["#ff0000", "#0000ff"]}}})
    plan = build_render_plan(scene, registry)
    assert plan.fixture_plans[0].fidelity.value == "approximate"
    assert all("gradient" not in operation.payload for operation in plan.fixture_plans[0].operations)


def _engine(tmp_path):
    store = SceneStudioStore(tmp_path)
    store.fixtures.add_fixture({
        "id": "lamp", "name": "Lamp", "groups": ["room"], "health": "missing",
        "binding": {"provider": "hue_v2", "bridge_id": "bridge", "resource_id": "old"},
        "capabilities": {"on_off": True, "brightness": True, "color_xy": True, "dynamic_native": False},
    })
    store.fixtures.add_target({"id": "room", "name": "Room"})
    store.scenes.add_scene({"schema_version": 2, "id": "s", "name": "S", "target_ids": ["room"],
                            "fixture_states": {"lamp": {"on": True, "color": "#ff0000"}}})
    fetchers = DiscoveryFetchers(fetch_ha_states=lambda: {
        "light.lamp_new": {"attributes": {"friendly_name": "Lamp", "supported_color_modes": ["brightness"]}},
    })
    return store, SceneStudioEngine(store, RecordingExecutor(), SteppingClock(), discovery_fetchers=fetchers)


def test_rebind_preview_apply_and_rollback_replace_full_revision(tmp_path):
    store, engine = _engine(tmp_path)
    assert engine.handle({"command": "discovery.run", "providers": ["ha_light"]})["ok"]
    preview = engine.handle({"command": "fixture.rebind_preview", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})
    assert preview["ok"] and preview["data"]["safe_to_apply"]
    assert "color_xy" in preview["data"]["capabilities_lost"]
    applied = engine.handle({"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})
    assert applied["ok"]
    migrated = store.fixtures.get_fixture("lamp")
    assert migrated.binding.provider == "ha_light" and migrated.capabilities.color_xy is False
    assert migrated.health is None and len(migrated.binding_history) == 1
    rolled_back = engine.handle({"command": "fixture.rebind_rollback", "fixture_id": "lamp"})
    assert rolled_back["ok"]
    restored = store.fixtures.get_fixture("lamp")
    assert restored.binding.provider == "hue_v2" and restored.capabilities.color_xy is True


def test_rebind_without_observed_capabilities_is_rejected(tmp_path):
    _store, engine = _engine(tmp_path)
    engine._last_discovery = __import__("scene_studio.domain", fromlist=["DiscoveryReport"]).DiscoveryReport.from_dict({
        "run_id": "r", "started_at": "2026-01-01T00:00:00Z", "providers": ["ha_light"],
        "observations": [{"provider": "ha_light", "provider_resource_id": "light.no_caps", "name": "Lamp"}],
        "candidates": [], "entries": [], "summary": {},
    })
    result = engine.handle({"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.no_caps"})
    assert result["ok"] is False and result["error"]["code"] == "conflict"


def test_atomic_rebind_write_failure_leaves_previous_revision(tmp_path, monkeypatch):
    store, engine = _engine(tmp_path)
    assert engine.handle({"command": "discovery.run", "providers": ["ha_light"]})["ok"]
    original = store.fixtures.get_fixture("lamp").to_dict()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr("scene_studio.stores.fixtures.atomic_write_json", fail_write)
    result = engine.handle({"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})
    assert result["ok"] is False
    assert store.fixtures.get_fixture("lamp").to_dict() == original


def test_rebind_rollback_restores_profile_and_explicit_health(tmp_path):
    store, engine = _engine(tmp_path)
    store.fixtures._mutate_fixture("lamp", lambda fixture: _seed_profiled_fixture(fixture))
    before = store.fixtures.get_fixture("lamp").to_dict()
    engine.handle({"command": "discovery.run", "providers": ["ha_light"]})
    assert engine.handle({"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})["ok"]
    assert store.fixtures.get_fixture("lamp").binding_history[0].observation_id == "hue_v2:old"
    assert engine.handle({"command": "fixture.rebind_rollback", "fixture_id": "lamp"})["ok"]
    after = store.fixtures.get_fixture("lamp").to_dict()
    for key in ("binding", "capabilities", "device_profile", "capability_assessment", "health"):
        assert after.get(key) == before.get(key)


def _seed_profiled_fixture(fixture):
    fixture.device_profile = __import__("scene_studio.domain", fromlist=["DeviceProfile"]).DeviceProfile(
        manufacturer="Hue", model="Hue Lamp", native_features={"color": True}, source="manual_verified"
    )
    fixture.health = HealthStatus.MISSING
    fixture.capability_assessment = __import__("scene_studio.domain", fromlist=["CapabilityAssessment", "CapabilityStatus"]).CapabilityAssessment(
        status=__import__("scene_studio.domain", fromlist=["CapabilityStatus"]).CapabilityStatus.UNKNOWN,
        provider="hue_v2", observation_id="hue_v2:old", reasons=["prior observation"],
    )
    return fixture


def test_preview_matches_apply_when_candidate_has_no_profile(tmp_path):
    store, engine = _engine(tmp_path)
    store.fixtures._mutate_fixture("lamp", _seed_profiled_fixture)
    engine.handle({"command": "discovery.run", "providers": ["ha_light"]})
    preview = engine.handle({"command": "fixture.rebind_preview", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})["data"]
    assert preview["device_profile_delta"] == {}
    applied = engine.handle({"command": "fixture.rebind", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})["data"]["fixture"]
    assert applied["device_profile"]["manufacturer"] == "Hue"
    assert applied["capability_assessment"]["status"] == preview["capability_status_after"]


def test_legacy_store_requires_explicit_persistence_migration(tmp_path):
    path = tmp_path / "registry" / "registry.json"
    path.parent.mkdir(parents=True)
    import json
    path.write_text(json.dumps(_v1_registry()), encoding="utf-8")
    store = SceneStudioStore(tmp_path)
    assert store.fixtures.inspect_schema_migration()["required"] is True
    try:
        store.fixtures.set_enabled("custom_gradient", False)
        assert False, "legacy mutation must fail"
    except Exception as exc:
        assert "explicit registry migration" in str(exc)
    migrated = store.fixtures.migrate_loaded_legacy_registry()
    assert (path.parent / "registry.v1.backup.json").exists()
    assert migrated["registry"]["schema_version"] == 2
    assert store.fixtures.set_enabled("custom_gradient", False).enabled is False


def test_optional_device_enrichment_keeps_provenance_out_of_native_features():
    lights = {"data": [{"id": "light-rid", "owner": {"rid": "device-rid"}, "metadata": {"name": "Friendly Gradient"}, "dimming": {}}]}
    devices = {"data": [{"id": "device-rid", "product_data": {"manufacturer_name": "Acme", "model_id": "P1", "product_name": "Addressable"}}]}
    observation = build_hue_observations(lights, clip_devices=devices)[0]
    assert observation.metadata["hue_device_rid"] == "device-rid"
    assert observation.device_profile.model == "P1"
    assert "hue_device_rid" not in observation.device_profile.native_features
    assert observation.capabilities.gradient is None  # friendly/product metadata cannot widen control


def test_halight_enrichment_is_optional_and_observational():
    states = {"light.lamp": {"attributes": {"friendly_name": "Lamp", "supported_color_modes": ["brightness"]}}}
    fallback = build_halight_observations(states)[0]
    enriched = build_halight_observations(states, {"light.lamp": {"manufacturer": "Acme", "model": "P2", "integration": "zigbee"}})[0]
    assert fallback.device_profile is None and fallback.capabilities.color_xy is False
    assert enriched.device_profile.manufacturer == "Acme"
    assert enriched.device_profile.source == "ha_device_metadata"
    assert enriched.capabilities.to_dict() == fallback.capabilities.to_dict()


def test_read_only_allows_rebind_preview_but_not_mutation(tmp_path):
    _store, engine = _engine(tmp_path)
    engine.handle({"command": "discovery.run", "providers": ["ha_light"]})
    engine._policy = RuntimePolicy.build("read_only")
    preview = engine.handle({"command": "fixture.rebind_preview", "fixture_id": "lamp", "observation_id": "ha_light:light.lamp_new"})
    assert preview["ok"] is True
    for command in ("fixture.rebind", "fixture.rebind_rollback"):
        payload = {"command": command, "fixture_id": "lamp"}
        if command == "fixture.rebind":
            payload["observation_id"] = "ha_light:light.lamp_new"
        assert engine.handle(payload)["error"]["code"] == "conflict"
