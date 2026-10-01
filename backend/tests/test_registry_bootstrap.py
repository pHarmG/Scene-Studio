"""First-run registry bootstrap: ``fixture.adopt`` / ``target.create`` / ``target.update``.

A clean portable install starts with an empty registry in ``registry_admin``
mode. These tests pin the narrow bootstrap surface: adoption derives the
binding/capabilities/profile/assessment/provenance from the selected
discovery observation (never client-submitted provider payloads), rejects
duplicates / stale observations / HA aggregates, persists through the
normal store path, and — the security-critical property — performs ZERO
provider writes throughout bootstrap.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from scene_studio.domain.fixtures import Fixture, derive_health
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.service.policy import MODE_NORMAL, MODE_READ_ONLY, MODE_REGISTRY_ADMIN, RuntimePolicy
from scene_studio.stores import SceneStudioStore

RESOURCE_A = "aaaaaaaa-1111-4000-8000-000000000001"
RESOURCE_B = "bbbbbbbb-2222-4000-8000-000000000002"
DEMO_BRIDGE = "001788demo01"
WLED_MAC = "aabbccddeeff"  # generic placeholder MAC (matches discovery.sample.json)


def _hue_light(resource_id: str, name: str) -> dict:
    return {
        "id": resource_id,
        "metadata": {"name": name},
        "owner": {"rid": f"device-{resource_id[:8]}", "rtype": "device"},
        "product_data": {"manufacturer_name": "Signe", "model_id": "demo-model", "product_name": "Demo light"},
        "dimming": {"brightness": 50.0},
        "color": {"xy": {"x": 0.3, "y": 0.3}},
        "color_temperature": {"mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
    }


def _fetchers(*, hue_bridge_id=DEMO_BRIDGE):
    return DiscoveryFetchers(
        fetch_hue=lambda: {"data": [_hue_light(RESOURCE_A, "Desk Light"), _hue_light(RESOURCE_B, "Ceiling Light")]},
        fetch_hue_devices=lambda: {"data": []},
        fetch_ha_states=lambda: {
            "light.desk_lamp": {
                "state": "on",
                "attributes": {
                    "friendly_name": "Desk Lamp",
                    "supported_color_modes": ["color_temp"],
                    "min_color_temp_kelvin": 2000,
                    "max_color_temp_kelvin": 6500,
                },
            },
            "light.demo_group": {"state": "on", "attributes": {"entity_id": ["light.desk_lamp"]}},
        },
        fetch_wled_info=lambda: {"mac": WLED_MAC, "ver": "0.14.0", "leds": {"count": 60, "cct": False}},
        fetch_wled_state=lambda: {
            "seg": [{"id": 0, "start": 0, "stop": 30, "n": "TV strip"}],
            "bri": 128,
            "fx": 0,
            "effects": ["Rainbow"],
            "palettes": ["Default"],
        },
        wled_endpoint_hint="http://wled.local",
        hue_bridge_id=hue_bridge_id,
    )


def _engine(tmp_path, *, mode=MODE_REGISTRY_ADMIN, fetchers=None, executor=None):
    return SceneStudioEngine(
        store=SceneStudioStore(tmp_path / "store"),
        executor=executor or RecordingExecutor(),
        clock=SteppingClock(),
        discovery_fetchers=fetchers or _fetchers(),
        policy=RuntimePolicy.build(mode),
    )


def _discover(engine):
    result = engine.handle({"command": "discovery.run"})
    assert result["ok"], result
    return result


def _adopt(engine, **overrides):
    envelope = {
        "command": "fixture.adopt",
        "observation_id": f"hue_v2:{RESOURCE_A}",
        "fixture_id": "desk_light",
        "name": "Desk Light",
        "groups": ["office"],
    }
    envelope.update(overrides)
    return engine.handle(envelope)


def _no_provider_writes(engine) -> bool:
    return list(getattr(engine._executor, "calls", [])) == []


# ---------------------------------------------------------------------------
# empty-store startup
# ---------------------------------------------------------------------------


def test_empty_store_starts_with_nothing(tmp_path):
    engine = _engine(tmp_path)
    status = engine.status()
    assert status["fixtures"]["total"] == 0
    assert status["last_discovery"] is None
    assert engine.latest_discovery() is None


def test_adopt_before_discovery_is_not_found(tmp_path):
    engine = _engine(tmp_path)
    result = _adopt(engine)
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"
    assert "discovery.run" in result["error"]["message"]


# ---------------------------------------------------------------------------
# fixture.adopt — happy path, derivation, provenance, zero provider writes
# ---------------------------------------------------------------------------


def test_adopt_hue_fixture_in_registry_admin(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    result = _adopt(engine, request_id="r-adopt-1")
    assert result["ok"], result
    assert result["request_id"] == "r-adopt-1"
    fixture = result["data"]["fixture"]
    assert result["data"]["observation_id"] == f"hue_v2:{RESOURCE_A}"
    # server-derived binding, capabilities, profile, assessment
    assert fixture["binding"]["provider"] == "hue_v2"
    assert fixture["binding"]["resource_id"] == RESOURCE_A
    assert fixture["binding"]["bridge_id"] == DEMO_BRIDGE
    assert fixture["capabilities"]["on_off"] is True
    assert fixture["capabilities"]["color_xy"] is True
    assert fixture["device_profile"]["manufacturer"] == "Signe"
    assert fixture["device_profile"]["protocol"] == "hue_zigbee"
    assert fixture["capability_assessment"]["status"] == "unknown"
    # operator-supplied identity/membership
    assert fixture["id"] == "desk_light"
    assert fixture["name"] == "Desk Light"
    assert fixture["groups"] == ["office"]
    assert fixture["enabled"] is True
    assert derive_health(Fixture.from_dict(fixture)).value == "ready"
    # provenance
    assert fixture["metadata"]["adopted_from_observation"] == f"hue_v2:{RESOURCE_A}"
    assert fixture["metadata"]["adopted_at"]
    assert fixture["metadata"]["adopted_run_id"]
    assert _no_provider_writes(engine)


def test_adopt_ha_light_and_wled_fixtures(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    lamp = engine.handle({
        "command": "fixture.adopt",
        "observation_id": "ha_light:light.desk_lamp",
        "fixture_id": "desk_lamp",
        "name": "Desk Lamp",
    })
    assert lamp["ok"], lamp
    assert lamp["data"]["fixture"]["binding"] == {"provider": "ha_light", "ha_entity_id": "light.desk_lamp"}
    strip = engine.handle({
        "command": "fixture.adopt",
        "observation_id": f"wled:{WLED_MAC}:seg:0",
        "fixture_id": "tv_strip",
        "name": "TV Strip",
    })
    assert strip["ok"], strip
    binding = strip["data"]["fixture"]["binding"]
    assert binding["provider"] == "wled"
    assert binding["device_id"] == WLED_MAC
    assert binding["segment_ids"] == [0]
    assert binding["endpoint_hint"] == "http://wled.local"
    assert strip["data"]["fixture"]["capabilities"]["dynamic_native"] is True
    assert _no_provider_writes(engine)


def test_adopt_persists_through_store_reload(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    targets = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["desk_light"]})
    assert targets["ok"], targets
    reloaded = SceneStudioStore(tmp_path / "store")
    fixture = reloaded.fixtures.get_fixture("desk_light")
    assert fixture.binding is not None and fixture.binding.provider == "hue_v2"
    assert fixture.groups == ["office"]
    assert fixture.metadata["adopted_from_observation"] == f"hue_v2:{RESOURCE_A}"
    assert [target.id for target in reloaded.fixtures.list_targets()] == ["office"]
    assert [item.id for item in reloaded.fixtures.resolve_target("office")] == ["desk_light"]


# ---------------------------------------------------------------------------
# fixture.adopt — rejections
# ---------------------------------------------------------------------------


def test_adopt_rejects_duplicate_stable_id(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    again = _adopt(engine, name="Desk Light Two")
    assert not again["ok"]
    assert again["error"]["code"] == "conflict"
    assert _no_provider_writes(engine)


def test_adopt_rejects_observation_missing_from_latest_report(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    stale = _adopt(engine, observation_id="hue_v2:ffffffff-ffff-4000-8000-0000000000ff")
    assert not stale["ok"]
    assert stale["error"]["code"] == "not_found"
    # a second run replaces the report: the previous observation disappears
    assert _adopt(engine)["ok"]
    engine.handle({"command": "discovery.run", "providers": ["wled"]})
    gone = _adopt(engine, observation_id=f"ha_light:light.desk_lamp", fixture_id="other_lamp")
    assert not gone["ok"]
    assert gone["error"]["code"] == "not_found"


def test_adopt_rejects_ha_aggregate_observation(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    result = _adopt(engine, observation_id="ha_light:light.demo_group", fixture_id="demo_group")
    assert not result["ok"]
    assert result["error"]["code"] == "conflict"
    assert "aggregate" in result["error"]["message"]


def test_adopt_hue_without_bridge_hint_conflicts_actionably(tmp_path):
    engine = _engine(tmp_path, fetchers=_fetchers(hue_bridge_id=None))
    _discover(engine)
    result = _adopt(engine)
    assert not result["ok"]
    assert result["error"]["code"] == "conflict"
    assert "hue_bridge_id" in result["error"]["message"]


def test_adopt_validates_identity_charset(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    bad_id = _adopt(engine, fixture_id="Desk-Light")
    assert not bad_id["ok"]
    assert bad_id["error"]["code"] == "validation_error"
    bad_group = _adopt(engine, fixture_id="other_light", groups=["Office"])
    assert not bad_group["ok"]
    assert bad_group["error"]["code"] == "validation_error"


def test_adopt_unknown_param_rejected(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    result = _adopt(engine, binding={"provider": "hue_v2"})
    assert not result["ok"]
    assert result["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# policy: registry_admin allows, read_only blocks, normal allows; provider
# writes stay blocked in every bootstrap path
# ---------------------------------------------------------------------------


def test_adopt_blocked_in_read_only(tmp_path):
    engine = _engine(tmp_path, mode=MODE_READ_ONLY)
    _discover(engine)
    result = _adopt(engine)
    assert not result["ok"]
    assert result["error"]["code"] == "conflict"
    assert "read_only" in result["error"]["message"]
    assert _no_provider_writes(engine)


def test_adopt_allowed_in_normal(tmp_path):
    engine = _engine(tmp_path, mode=MODE_NORMAL)
    _discover(engine)
    assert _adopt(engine)["ok"]
    assert _no_provider_writes(engine)


def test_bootstrap_mode_blocks_real_apply_and_playback(tmp_path):
    engine = _engine(tmp_path, mode=MODE_REGISTRY_ADMIN)
    _discover(engine)
    assert _adopt(engine)["ok"]
    assert engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["desk_light"]})["ok"]
    scene = engine.handle({"command": "scene.create", "scene": {
        "schema_version": 2, "name": "Bootstrap Check", "target_ids": ["office"],
        "fixture_states": {"desk_light": {"on": True, "brightness": 40}},
    }})
    assert scene["ok"], scene
    scene_id = scene["data"]["scene"]["id"]
    apply_result = engine.handle({"command": "scene.apply", "scene_id": scene_id})
    assert not apply_result["ok"]
    assert apply_result["error"]["code"] == "conflict"
    playback = engine.handle({"command": "playback.start", "scene_id": scene_id})
    assert not playback["ok"]
    assert playback["error"]["code"] == "conflict"
    assert _no_provider_writes(engine)


def test_status_runtime_lists_bootstrap_commands_in_registry_admin(tmp_path):
    engine = _engine(tmp_path)
    allowed = engine.status()["runtime"]["allowed_commands"]
    assert "fixture.adopt" in allowed
    assert "target.create" in allowed
    assert "target.update" in allowed
    assert engine.status()["runtime"]["provider_writes_blocked"] is True


# ---------------------------------------------------------------------------
# target.create / target.update
# ---------------------------------------------------------------------------


def test_target_create_derives_id_from_name(tmp_path):
    engine = _engine(tmp_path)
    result = engine.handle({"command": "target.create", "name": "Living Room"})
    assert result["ok"], result
    assert result["data"]["target"]["id"] == "living_room"
    assert result["data"]["target"]["name"] == "Living Room"
    assert result["data"]["assigned_fixture_ids"] == []


def test_target_create_assigns_membership_and_resolves(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    # desk_light is adopted directly INTO group "office" (adoption-time membership)
    assert _adopt(engine)["ok"]
    assert engine.handle({
        "command": "fixture.adopt", "observation_id": f"hue_v2:{RESOURCE_B}",
        "fixture_id": "ceiling_light", "name": "Ceiling Light",
    })["ok"]
    # target.create with fixture_ids assigns membership on the authoritative side
    result = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["ceiling_light"]})
    assert result["ok"], result
    assert result["data"]["assigned_fixture_ids"] == ["ceiling_light"]
    members = [item.id for item in engine._store.fixtures.resolve_target("office")]
    assert members == ["ceiling_light", "desk_light"]
    # desk_light's membership came from adoption, so the create-time delta skipped it
    ceiling = engine._store.fixtures.get_fixture("ceiling_light")
    assert ceiling.groups == ["office"]


def test_target_create_duplicate_conflicts(tmp_path):
    engine = _engine(tmp_path)
    assert engine.handle({"command": "target.create", "name": "Office"})["ok"]
    again = engine.handle({"command": "target.create", "name": "Office"})
    assert not again["ok"]
    assert again["error"]["code"] == "conflict"


def test_target_create_unknown_fixture_not_found(tmp_path):
    engine = _engine(tmp_path)
    result = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["ghost"]})
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"


def test_target_create_blocked_in_read_only(tmp_path):
    engine = _engine(tmp_path, mode=MODE_READ_ONLY)
    result = engine.handle({"command": "target.create", "name": "Office"})
    assert not result["ok"]
    assert result["error"]["code"] == "conflict"


def test_target_update_renames_and_mutates_membership(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    assert engine.handle({
        "command": "fixture.adopt", "observation_id": f"hue_v2:{RESOURCE_B}",
        "fixture_id": "ceiling_light", "name": "Ceiling Light",
    })["ok"]
    created = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["desk_light"]})
    target_id = created["data"]["target"]["id"]
    updated = engine.handle({
        "command": "target.update", "target_id": target_id, "name": "Workspace",
        "add_fixture_ids": ["ceiling_light"],
    })
    assert updated["ok"], updated
    assert updated["data"]["target"]["name"] == "Workspace"
    assert updated["data"]["assigned_fixture_ids"] == ["ceiling_light"]
    assert [item.id for item in engine._store.fixtures.resolve_target(target_id)] == [
        "ceiling_light", "desk_light"
    ]
    removed = engine.handle({
        "command": "target.update", "target_id": target_id, "remove_fixture_ids": ["desk_light"],
    })
    assert removed["ok"]
    assert removed["data"]["removed_fixture_ids"] == ["desk_light"]
    assert [item.id for item in engine._store.fixtures.resolve_target(target_id)] == ["ceiling_light"]


def test_target_update_remove_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    created = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["desk_light"]})
    target_id = created["data"]["target"]["id"]
    first = engine.handle({"command": "target.update", "target_id": target_id, "remove_fixture_ids": ["desk_light"]})
    assert first["ok"]
    assert first["data"]["removed_fixture_ids"] == ["desk_light"]
    again = engine.handle({"command": "target.update", "target_id": target_id, "remove_fixture_ids": ["desk_light"]})
    assert again["ok"]
    assert again["data"]["removed_fixture_ids"] == []
    assert [item.id for item in engine._store.fixtures.resolve_target(target_id)] == []


def test_target_update_unknown_target_not_found(tmp_path):
    engine = _engine(tmp_path)
    result = engine.handle({"command": "target.update", "target_id": "ghost", "name": "Ghost"})
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"


def test_target_membership_delta_unknown_fixture_not_found(tmp_path):
    engine = _engine(tmp_path)
    created = engine.handle({"command": "target.create", "name": "Office"})
    target_id = created["data"]["target"]["id"]
    result = engine.handle({"command": "target.update", "target_id": target_id, "add_fixture_ids": ["ghost"]})
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# atomicity: a failed request leaves the registry byte-for-byte unchanged
# ---------------------------------------------------------------------------


def _registry_bytes(tmp_path):
    path = Path(tmp_path / "store" / "registry" / "registry.json")
    return path.read_bytes() if path.exists() else b""


def test_target_create_with_mixed_valid_invalid_members_never_mutates(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    before = _registry_bytes(tmp_path)
    result = engine.handle({
        "command": "target.create", "name": "Office",
        "fixture_ids": ["desk_light", "ghost_fixture"],
    })
    assert not result["ok"]
    assert result["error"]["code"] == "not_found"
    assert _registry_bytes(tmp_path) == before, "failed target.create mutated the registry"
    # and the target itself must not exist
    assert engine.handle({"command": "target.update", "target_id": "office", "name": "X"})["error"]["code"] == "not_found"


def test_target_update_with_any_unknown_member_never_mutates(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    assert _adopt(engine)["ok"]
    created = engine.handle({"command": "target.create", "name": "Office", "fixture_ids": ["desk_light"]})
    target_id = created["data"]["target"]["id"]
    before = _registry_bytes(tmp_path)
    failed = engine.handle({
        "command": "target.update", "target_id": target_id, "name": "Workspace",
        "add_fixture_ids": ["desk_light"], "remove_fixture_ids": ["ghost_fixture"],
    })
    assert not failed["ok"]
    assert failed["error"]["code"] == "not_found"
    assert _registry_bytes(tmp_path) == before, "failed target.update mutated the registry"
    # the rename inside the failed request must not have landed either
    assert engine._store.fixtures.get_target(target_id).name == "Office"


def test_target_update_mixed_add_remove_same_fixture_remove_wins(tmp_path):
    engine = _engine(tmp_path)
    _discover(engine)
    # adopt WITHOUT membership so neither delta has pre-existing state
    assert engine.handle({
        "command": "fixture.adopt", "observation_id": f"hue_v2:{RESOURCE_A}",
        "fixture_id": "desk_light", "name": "Desk Light",
    })["ok"]
    created = engine.handle({"command": "target.create", "name": "Office"})
    target_id = created["data"]["target"]["id"]
    result = engine.handle({
        "command": "target.update", "target_id": target_id,
        "add_fixture_ids": ["desk_light"], "remove_fixture_ids": ["desk_light"],
    })
    assert result["ok"]
    assert result["data"]["assigned_fixture_ids"] == []
    assert result["data"]["removed_fixture_ids"] == []
    assert engine._store.fixtures.get_fixture("desk_light").groups == []


@pytest.mark.parametrize("command", ["fixture.adopt", "target.create", "target.update"])
def test_bridge_allowlist_stays_narrow(command):
    """The HA bridge must never expose registry bootstrap through HA."""
    from scene_studio.appdaemon_adapter.ui_bridge import UI_BRIDGE_ALLOWLIST

    assert command not in UI_BRIDGE_ALLOWLIST
