"""Same-binding reconcile, registry_admin policy, and registry migration commands."""

from scene_studio.domain import (
    CapabilityStatus,
    DiscoveryReport,
    FixtureRegistry,
    HealthStatus,
)
from scene_studio.domain.fixtures import derive_health
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.policy import MODE_READ_ONLY, MODE_REGISTRY_ADMIN, RuntimePolicy
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.stores import SceneStudioStore


RESOURCE = "aaaaaaaa-1111-4000-8000-000000000001"
OTHER_RESOURCE = "bbbbbbbb-2222-4000-8000-000000000002"
HUE_CAPS = {
    "on_off": True,
    "brightness": True,
    "color_xy": True,
    "color_temp": {"mirek_min": 153, "mirek_max": 500},
    "dynamic_native": False,
}


def _hue_fetchers(resource_id=RESOURCE, *, extra_light=None, devices=True):
    lights = [{
        "id": resource_id,
        "metadata": {"name": "Office Light"},
        "owner": {"rid": "device-rid", "rtype": "device"},
        "dimming": {},
        "color": {},
        "color_temperature": {"mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
    }]
    if extra_light:
        lights.append(extra_light)
    clip_devices = {"data": [{
        "id": "device-rid",
        "product_data": {
            "manufacturer_name": "GLEDOPTO",
            "model_id": "GL-C-103P",
            "product_name": "Custom Gradient controller",
        },
    }]} if devices else {"data": []}
    return DiscoveryFetchers(
        fetch_hue=lambda: {"data": lights},
        fetch_hue_devices=lambda: clip_devices,
    )


def _stale_fixture(**overrides):
    fixture = {
        "id": "office_light",
        "name": "Office Light",
        "groups": ["room"],
        "health": "degraded",
        "binding": {"provider": "hue_v2", "bridge_id": "bridge", "resource_id": RESOURCE},
        "capabilities": dict(HUE_CAPS),
        "capability_assessment": {"status": "unknown", "reasons": ["stale registry"]},
    }
    fixture.update(overrides)
    return fixture


def _engine(tmp_path, *, fixture=None, fetchers=None, policy=None, extra_fixtures=None):
    store = SceneStudioStore(tmp_path)
    store.fixtures.add_fixture(fixture or _stale_fixture())
    for item in extra_fixtures or []:
        store.fixtures.add_fixture(item)
    store.fixtures.add_target({"id": "room", "name": "Room"})
    store.scenes.add_scene({
        "schema_version": 2, "id": "s", "name": "S", "target_ids": ["room"],
        "fixture_states": {"office_light": {"on": True, "color": "#ff0000"}},
    })
    engine = SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=fetchers or _hue_fetchers(),
        policy=policy or RuntimePolicy.build(),
    )
    return store, engine


def _discover(engine, observation_id=f"hue_v2:{RESOURCE}"):
    assert engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})["ok"]
    return observation_id


def _hue_fetchers_with_room(room_name, resource_id=RESOURCE):
    """Same single-light payload as `_hue_fetchers`, plus a CLIP v2 Room
    resource the light's owning device belongs to — the real upstream
    source `location_hint` is derived from (live-verified against a real
    bridge: this is the SAME mechanism that already correctly reports
    "Studio" for a fixture whose registry `groups` never got updated)."""
    lights = [{
        "id": resource_id,
        "metadata": {"name": "Office Light"},
        "owner": {"rid": "device-rid", "rtype": "device"},
        "dimming": {},
        "color": {},
        "color_temperature": {"mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
    }]
    rooms = {"data": [{
        "id": "room-rid-1",
        "type": "room",
        "metadata": {"name": room_name},
        "children": [{"rid": "device-rid", "rtype": "device"}],
    }]}
    return DiscoveryFetchers(fetch_hue=lambda: {"data": lights}, fetch_hue_rooms=lambda: rooms)


def test_same_resource_reconcile_preview_succeeds(tmp_path):
    _store, engine = _engine(tmp_path)
    observation_id = _discover(engine)
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })
    assert preview["ok"] is True
    data = preview["data"]
    assert data["binding_unchanged"] is True
    assert data["fixture_id"] == "office_light"
    assert data["observation_id"] == observation_id
    assert data["safe_to_apply"] is True
    assert data["current"]["operational_health"] == "degraded"
    assert data["proposed"]["operational_health"] == "ready"
    assert data["proposed"]["device_profile"]["model"] == "GL-C-103P"
    assert data["current_binding"] == data["proposed_binding"]
    assert engine._executor.calls == []


def test_different_resource_reconcile_is_rejected_and_points_to_rebind(tmp_path):
    extra = {
        "id": OTHER_RESOURCE,
        "metadata": {"name": "Other Light"},
        "owner": {"rid": "other-device", "rtype": "device"},
        "dimming": {},
    }
    _store, engine = _engine(tmp_path, fetchers=_hue_fetchers(extra_light=extra, devices=False))
    _discover(engine)
    result = engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{OTHER_RESOURCE}",
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "conflict"
    assert "fixture.rebind" in result["error"]["message"]


def test_reconcile_leaves_binding_unchanged_and_updates_registry_atomically(tmp_path):
    store, engine = _engine(tmp_path, fixture=_stale_fixture(
        device_profile={"native_features": {"addressable_pixels": True}, "source": "manual_verified"},
    ))
    before = store.fixtures.get_fixture("office_light")
    prior_binding = before.binding.to_dict()
    observation_id = _discover(engine)
    applied = engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })
    assert applied["ok"] is True
    stored = store.fixtures.get_fixture("office_light")
    assert stored.binding.to_dict() == prior_binding
    assert derive_health(stored) is HealthStatus.READY
    assert stored.health is None
    assert stored.device_profile.model == "GL-C-103P"
    assert stored.device_profile.native_features.get("addressable_pixels") is True
    assert stored.capability_assessment.status is CapabilityStatus.LIMITED
    assert stored.capabilities.to_dict()["color_xy"] is True
    assert stored.binding_history[0].health is HealthStatus.DEGRADED
    assert engine._executor.calls == []


# ---------------------------------------------------------------------------
# room/group drift reconcile (live finding: a fixture's Hue Room assignment
# had already moved to "Studio", but its registered `groups` still said
# "office" — nothing synced it because location_hint was never consumed)
# ---------------------------------------------------------------------------


def test_reconcile_preview_proposes_the_matching_target_when_room_drifted(tmp_path):
    store, engine = _engine(tmp_path, fetchers=_hue_fetchers_with_room("Studio"))
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    observation_id = _discover(engine)
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })["data"]
    assert preview["groups_changed"] is True
    assert preview["current"]["groups"] == ["room"]
    assert preview["proposed"]["groups"] == ["studio"]
    assert preview["safe_to_apply"] is True
    assert preview["requires_confirmation"] is True  # a room reassignment always gets a look


def test_reconcile_apply_swaps_groups_and_preserves_the_broad_group(tmp_path):
    store, engine = _engine(
        tmp_path,
        fixture=_stale_fixture(groups=["room", "whole_house"]),
        fetchers=_hue_fetchers_with_room("Studio"),
    )
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    observation_id = _discover(engine)
    applied = engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })
    assert applied["ok"] is True
    stored = store.fixtures.get_fixture("office_light")
    assert stored.groups == ["studio", "whole_house"]


def test_reconcile_proposes_nothing_when_the_observed_room_matches_no_known_target(tmp_path):
    """No target named "Attic" exists — never invent a new group id from
    the observed room name; surface nothing rather than guess."""
    store, engine = _engine(tmp_path, fetchers=_hue_fetchers_with_room("Attic"))
    observation_id = _discover(engine)
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })["data"]
    assert preview["groups_changed"] is False
    assert preview["proposed"]["groups"] == preview["current"]["groups"]


def test_reconcile_proposes_nothing_when_already_a_member_of_the_observed_room(tmp_path):
    store, engine = _engine(
        tmp_path,
        fixture=_stale_fixture(groups=["studio", "whole_house"]),
        fetchers=_hue_fetchers_with_room("Studio"),
    )
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    observation_id = _discover(engine)
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })["data"]
    assert preview["groups_changed"] is False


def test_reconcile_preview_room_drift_shows_up_as_a_scene_targeting_impact(tmp_path):
    """The candidate registry used for impact analysis already reflects the
    proposed groups — a scene that only targets the OLD room shows this
    fixture dropping out, for free, via the existing impact machinery."""
    store, engine = _engine(tmp_path, fetchers=_hue_fetchers_with_room("Studio"))
    store.fixtures.add_target({"id": "studio", "name": "Studio"})
    observation_id = _discover(engine)
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": observation_id,
    })["data"]
    assert "s" in preview["affected_scene_ids"]  # the fixture:1 scene targets "room", which it now leaves


def test_reconcile_write_failure_leaves_prior_revision_intact(tmp_path, monkeypatch):
    store, engine = _engine(tmp_path)
    _discover(engine)
    original = store.fixtures.get_fixture("office_light").to_dict()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr("scene_studio.stores.fixtures.atomic_write_json", fail_write)
    result = engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })
    assert result["ok"] is False
    assert store.fixtures.get_fixture("office_light").to_dict() == original


def test_rollback_after_reconcile_restores_prior_complete_revision(tmp_path):
    store, engine = _engine(tmp_path, fixture=_stale_fixture(
        device_profile={"manufacturer": "Unknown", "source": "legacy_metadata"},
    ))
    before = store.fixtures.get_fixture("office_light").to_dict()
    _discover(engine)
    assert engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })["ok"]
    assert engine.handle({"command": "fixture.rebind_rollback", "fixture_id": "office_light"})["ok"]
    after = store.fixtures.get_fixture("office_light").to_dict()
    for key in ("binding", "capabilities", "device_profile", "capability_assessment", "health"):
        assert after.get(key) == before.get(key)


def test_weaker_observation_cannot_erase_stronger_verified_profile_evidence(tmp_path):
    store, engine = _engine(tmp_path, fixture=_stale_fixture(
        device_profile={
            "manufacturer": "GLEDOPTO",
            "model": "GL-C-103P",
            "native_features": {"addressable_pixels": True},
            "source": "manual_verified",
        },
    ))
    engine._last_discovery = DiscoveryReport.from_dict({
        "run_id": "r", "started_at": "2026-01-01T00:00:00Z", "providers": ["hue_v2"],
        "observations": [{
            "provider": "hue_v2", "provider_resource_id": RESOURCE, "name": "Office Light",
            "capabilities": HUE_CAPS,
            "device_profile": {"manufacturer": "Signify", "model": "LCA005", "source": "hue_device"},
        }],
        "candidates": [], "entries": [], "summary": {},
    })
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })["data"]
    assert any("conflicting" in warning for warning in preview["warnings"])
    applied = engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })
    assert applied["ok"] is True
    stored = store.fixtures.get_fixture("office_light")
    assert stored.device_profile.model == "GL-C-103P"
    assert stored.device_profile.source == "manual_verified"
    assert stored.device_profile.native_features["addressable_pixels"] is True


def test_conflicting_profile_evidence_requires_review_warning(tmp_path):
    _store, engine = _engine(tmp_path, fixture=_stale_fixture(
        device_profile={
            "native_features": {"addressable_pixels": True},
            "source": "manual_verified",
        },
    ))
    engine._last_discovery = DiscoveryReport.from_dict({
        "run_id": "r", "started_at": "2026-01-01T00:00:00Z", "providers": ["hue_v2"],
        "observations": [{
            "provider": "hue_v2", "provider_resource_id": RESOURCE, "name": "Office Light",
            "capabilities": HUE_CAPS,
            "device_profile": {
                "native_features": {"addressable_pixels": False},
                "source": "hue_device",
            },
        }],
        "candidates": [], "entries": [], "summary": {},
    })
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })["data"]
    assert preview["requires_confirmation"] is True
    assert any("conflicting native_features.addressable_pixels" in warning for warning in preview["warnings"])
    assert preview["proposed"]["device_profile"]["native_features"]["addressable_pixels"] is True


def test_discovery_reconcile_available_is_not_degradation(tmp_path):
    _store, engine = _engine(tmp_path)
    result = engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})
    assert result["ok"] is True
    summary = result["data"]["summary"]
    assert summary["fixtures_reconcile_available"] == 1
    assert summary["fixtures_degraded"] == 0
    entry = next(item for item in engine.latest_discovery().entries if item.fixture_id == "office_light")
    assert entry.status.value == "bound_reconcile_available"
    assert "same resource" in entry.detail


def test_disabled_fixture_is_skipped_by_discovery(tmp_path):
    _store, engine = _engine(
        tmp_path,
        extra_fixtures=[{
            "id": "leave_behind",
            "name": "Leave Behind",
            "enabled": False,
            "binding": {"provider": "hue_v2", "bridge_id": "bridge", "resource_id": OTHER_RESOURCE},
            "device_profile": {"manufacturer": "Tuya", "model": "TS0505B", "source": "legacy_metadata"},
        }],
    )
    result = engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})
    assert result["ok"] is True
    entry = next(item for item in engine.latest_discovery().entries if item.fixture_id == "leave_behind")
    assert entry.status.value == "disabled"


def test_read_only_permits_previews_but_not_reconcile_or_migration(tmp_path):
    path = tmp_path / "registry" / "registry.json"
    path.parent.mkdir(parents=True)
    import json
    path.write_text(json.dumps({
        "schema_version": 1,
        "fixtures": [_stale_fixture()],
        "targets": [],
    }), encoding="utf-8")
    store = SceneStudioStore(tmp_path)
    engine = SceneStudioEngine(
        store, RecordingExecutor(), SteppingClock(),
        discovery_fetchers=_hue_fetchers(),
        policy=RuntimePolicy.build(MODE_READ_ONLY),
    )
    assert engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})["ok"]
    preview = engine.handle({
        "command": "fixture.reconcile_preview",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })
    assert preview["ok"] is True
    migrate_preview = engine.handle({"command": "registry.migration_preview"})
    assert migrate_preview["ok"] is True
    assert migrate_preview["data"]["required"] is True
    for command in ("fixture.reconcile", "registry.migrate"):
        payload = {"command": command, "fixture_id": "office_light", "observation_id": f"hue_v2:{RESOURCE}"}
        if command == "registry.migrate":
            payload = {"command": command}
        result = engine.handle(payload)
        assert result["error"]["code"] == "conflict"
        assert "read_only" in result["error"]["message"]


def test_registry_admin_permits_registry_mutations_but_not_provider_execution(tmp_path):
    store, engine = _engine(tmp_path, policy=RuntimePolicy.build(MODE_REGISTRY_ADMIN))
    runtime = engine.status()["runtime"]
    assert runtime["mode"] == "registry_admin"
    assert runtime["read_only"] is False
    assert runtime["provider_writes_blocked"] is True
    assert engine.handle({"command": "discovery.run", "providers": ["hue_v2"]})["ok"]
    assert engine.handle({
        "command": "fixture.reconcile",
        "fixture_id": "office_light",
        "observation_id": f"hue_v2:{RESOURCE}",
    })["ok"]
    assert store.fixtures.get_fixture("office_light").health is None
    apply_result = engine.handle({"command": "scene.apply", "scene_id": "s"})
    assert apply_result["error"]["code"] == "conflict"
    assert "registry_admin" in apply_result["error"]["message"]
    playback = engine.handle({"command": "playback.start", "scene_id": "s"})
    assert playback["error"]["code"] == "conflict"
    assert engine._executor.calls == []


def test_registry_migration_preview_and_apply_are_deterministic(tmp_path):
    import json
    path = tmp_path / "registry" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "fixtures": [{
            "id": "office_light", "name": "Office Light", "health": "degraded",
            "binding": {"provider": "hue_v2", "bridge_id": "bridge", "resource_id": RESOURCE},
            "capabilities": HUE_CAPS,
            "metadata": {"manufacturer": "GLEDOPTO", "model": "GL-C-103P"},
        }],
        "targets": [],
    }), encoding="utf-8")
    store = SceneStudioStore(tmp_path)
    engine = SceneStudioEngine(
        store, RecordingExecutor(), SteppingClock(),
        policy=RuntimePolicy.build(MODE_REGISTRY_ADMIN),
    )
    preview = engine.handle({"command": "registry.migration_preview"})["data"]
    assert preview["required"] is True
    assert preview["from_schema_version"] == 1
    assert preview["to_schema_version"] == FixtureRegistry().schema_version
    applied = engine.handle({"command": "registry.migrate"})
    assert applied["ok"] is True
    assert applied["data"]["noop"] is False
    assert (path.parent / "registry.v1.backup.json").exists()
    fixture = store.fixtures.get_fixture("office_light")
    assert fixture.device_profile.model == "GL-C-103P"
    assert fixture.capability_assessment.status is CapabilityStatus.UNKNOWN
    again = engine.handle({"command": "registry.migrate"})["data"]
    assert again["noop"] is True
    assert again["reason"] == "already current"
    assert engine._executor.calls == []
