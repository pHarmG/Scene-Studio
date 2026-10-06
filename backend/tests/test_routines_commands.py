"""Routine command-catalog, policy, and engine integration tests (routines pass).

Proves the engine-level seams: catalog registration, param validation,
runtime-policy gating (restricted modes reject HA writes like provider
writes), honest capability reporting when no gateway exists, structured
conflict results, verify-after-write through the engine, and the
``GET /routines`` read route.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scene_studio.domain.commands import COMMAND_CATALOG, parse_command
from scene_studio.domain.routines import canonical_config_digest, generate_routine_config
from scene_studio.domain.serde import ValidationError
from scene_studio.appdaemon_adapter.ui_bridge import UI_BRIDGE_ALLOWLIST
from scene_studio.service import SceneStudioEngine, SteppingClock
from scene_studio.service.api import ROUTE_PREFIX, route
from scene_studio.service.ports import DiscoveryFetchers, RecordingExecutor
from scene_studio.service.policy import MODE_NORMAL, MODE_READ_ONLY, MODE_REGISTRY_ADMIN, RuntimePolicy
from scene_studio.service.routines import ROUTINE_CACHE_TTL_SECONDS
from scene_studio.stores import SceneStudioStore

from test_routines_service import FakeHaAutomationGateway

DYNAMIC_SCENE = {
    "schema_version": 2,
    "id": "aurora_flow",
    "name": "Aurora Flow",
    "target_ids": ["office"],
    "motion": {"mode": "palette_cycle", "speed": 0.4, "strategy": "auto"},
    "fixture_states": {"lamp": {"on": True, "brightness": 30.0}},
}


@pytest.fixture
def store(tmp_path):
    store = SceneStudioStore(tmp_path)
    store.fixtures.add_target({"id": "office", "name": "Office"})
    store.scenes.add_scene(
        {
            "schema_version": 2,
            "id": "twilight",
            "name": "Twilight",
            "target_ids": ["office"],
            "fixture_states": {"lamp": {"on": True, "brightness": 40.0}},
        }
    )
    store.scenes.add_scene(DYNAMIC_SCENE)
    return store


def make_engine(store, gateway=None, mode=MODE_NORMAL):
    return SceneStudioEngine(
        store,
        RecordingExecutor(),
        SteppingClock(),
        discovery_fetchers=DiscoveryFetchers(),
        policy=RuntimePolicy.build(mode),
        automation_gateway=gateway,
    )


# ---------------------------------------------------------------------------
# catalog + params
# ---------------------------------------------------------------------------


def test_routine_commands_are_registered():
    assert {
        "routine.create", "routine.update", "routine.delete",
        "routine.enable", "routine.disable",
    } <= set(COMMAND_CATALOG)


def test_routine_create_params():
    _, params = parse_command({
        "command": "routine.create",
        "scene_id": "twilight", "behavior": "apply", "time": "19:30",
    })
    assert params.weekdays is None
    _, params = parse_command({
        "command": "routine.create",
        "scene_id": "twilight", "behavior": "play", "time": "07:00", "weekdays": ["fri"],
    })
    assert params.weekdays == ["fri"]


def test_routine_create_params_reject_bad_shapes():
    with pytest.raises(ValidationError, match="time"):
        parse_command({"command": "routine.create", "scene_id": "t", "behavior": "apply", "time": "1930"})
    with pytest.raises(ValidationError, match="behavior"):
        parse_command({"command": "routine.create", "scene_id": "t", "behavior": "toggle", "time": "19:30"})
    with pytest.raises(ValidationError, match="weekdays"):
        parse_command({
            "command": "routine.create", "scene_id": "t", "behavior": "apply",
            "time": "19:30", "weekdays": "mon",
        })
    with pytest.raises(ValidationError, match="unknown key"):
        parse_command({
            "command": "routine.create", "scene_id": "t", "behavior": "apply",
            "time": "19:30", "trigger": {"platform": "time"},
        })


def test_routine_update_params_require_digest_and_keep_weekdays_sentinel():
    _, params = parse_command({
        "command": "routine.update",
        "automation_id": "ssr_x", "source_digest": "abcd", "time": "20:00",
    })
    assert params.weekdays is ...  # absent = keep current weekdays
    _, params = parse_command({
        "command": "routine.update",
        "automation_id": "ssr_x", "source_digest": "abcd", "weekdays": None,
    })
    assert params.weekdays is None  # explicit null = every day
    with pytest.raises(ValidationError, match="source_digest"):
        parse_command({"command": "routine.update", "automation_id": "ssr_x", "time": "20:00"})


def test_routine_addressed_commands_require_digest():
    for command in ("routine.delete", "routine.enable", "routine.disable"):
        with pytest.raises(ValidationError, match="source_digest"):
            parse_command({"command": command, "automation_id": "ssr_x"})


def test_routine_commands_absent_from_ha_card_bridge_allowlist():
    """The HA card is a daily controller, never an automation editor."""
    assert not any(command.startswith("routine.") for command in UI_BRIDGE_ALLOWLIST)


# ---------------------------------------------------------------------------
# runtime policy
# ---------------------------------------------------------------------------


def test_policy_allows_routines_only_in_normal_mode():
    normal = RuntimePolicy.build(MODE_NORMAL)
    read_only = RuntimePolicy.build(MODE_READ_ONLY)
    registry_admin = RuntimePolicy.build(MODE_REGISTRY_ADMIN)
    for command in ("routine.create", "routine.update", "routine.delete",
                    "routine.enable", "routine.disable"):
        assert normal.allows(command)
        assert not read_only.allows(command)
        assert not registry_admin.allows(command)


def test_restricted_modes_reject_routine_writes(store):
    for mode in (MODE_READ_ONLY, MODE_REGISTRY_ADMIN):
        engine = make_engine(store, mode=mode)
        result = engine.handle({
            "command": "routine.create", "scene_id": "twilight",
            "behavior": "apply", "time": "19:30",
        })
        assert result["ok"] is False and result["error"]["code"] == "conflict"
        assert "mode" in result["error"]["message"]


# ---------------------------------------------------------------------------
# honest capability reporting
# ---------------------------------------------------------------------------


def test_routine_create_without_gateway_reports_capability_unavailable(store):
    engine = make_engine(store, gateway=None)
    result = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "apply", "time": "19:30",
    })
    assert result["ok"] is False
    assert result["error"]["code"] == "provider_unavailable"
    assert result["error"]["details"]["capability"] == "ha_automation_management"


def test_routines_route_reports_unavailable_without_gateway(store):
    engine = make_engine(store, gateway=None)
    status, payload = route(engine, "GET", f"{ROUTE_PREFIX}/routines")
    assert status == 200
    assert payload["available"] is False
    assert payload["routines"] == []
    assert payload["unavailable_reason"]


# ---------------------------------------------------------------------------
# engine CRUD through a gateway
# ---------------------------------------------------------------------------


def test_engine_create_update_round_trip(store):
    gateway = FakeHaAutomationGateway()
    engine = make_engine(store, gateway=gateway)

    created = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "apply", "time": "19:30", "weekdays": ["mon"],
        "request_id": "r1",
    })
    assert created["ok"] is True and created["request_id"] == "r1"
    automation_id = created["data"]["routine"]["automation_id"]
    digest = created["data"]["routine"]["source_digest"]
    assert created["data"]["routine"]["schedule"]["time"] == "19:30"
    # the engine revision moved so every surface notices the external change
    assert engine.status()["engine"]["revision"] > 0
    # an automation event was recorded
    assert any(event.category.value == "automation" for event in engine.recent_events(10))

    updated = engine.handle({
        "command": "routine.update", "automation_id": automation_id,
        "source_digest": digest, "time": "20:45", "weekdays": None,
    })
    assert updated["ok"] is True
    assert updated["data"]["routine"]["schedule"]["time"] == "20:45"
    assert updated["data"]["routine"]["schedule"]["weekdays"] is None

    # the canonical re-read after update matches what HA actually stores
    stored = gateway.configs[automation_id]
    assert canonical_config_digest(stored) == updated["data"]["routine"]["source_digest"]


def test_engine_play_requires_dynamic_scene(store):
    engine = make_engine(store, gateway=FakeHaAutomationGateway())
    result = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "play", "time": "19:30",
    })
    assert result["ok"] is False and result["error"]["code"] == "validation_error"
    assert "static" in result["error"]["message"]


def test_engine_play_allows_dynamic_scene(store):
    engine = make_engine(store, gateway=FakeHaAutomationGateway())
    result = engine.handle({
        "command": "routine.create", "scene_id": "aurora_flow",
        "behavior": "play", "time": "22:00",
    })
    assert result["ok"] is True
    assert result["data"]["routine"]["behavior"] == "play"


def test_engine_delete_happy_path(store):
    gateway = FakeHaAutomationGateway()
    engine = make_engine(store, gateway=gateway)
    created = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "apply", "time": "19:30",
    })
    automation_id = created["data"]["routine"]["automation_id"]
    deleted = engine.handle({
        "command": "routine.delete", "automation_id": automation_id,
        "source_digest": created["data"]["routine"]["source_digest"],
    })
    assert deleted["ok"] is True and deleted["data"]["removed"] is True
    assert automation_id not in gateway.configs


def test_engine_structured_conflict_when_ha_changed(store):
    gateway = FakeHaAutomationGateway()
    engine = make_engine(store, gateway=gateway)
    created = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "apply", "time": "19:30",
    })
    automation_id = created["data"]["routine"]["automation_id"]
    # HA-side edit after the Workbench loaded its projection:
    gateway.configs[automation_id]["trigger"][0]["at"] = "23:00:00"
    result = engine.handle({
        "command": "routine.update", "automation_id": automation_id,
        "source_digest": created["data"]["routine"]["source_digest"], "time": "20:00",
    })
    assert result["ok"] is False and result["error"]["code"] == "conflict"
    assert result["error"]["details"]["kind"] == "routine_source_changed"
    assert result["error"]["details"]["automation_id"] == automation_id
    assert "current_digest" in result["error"]["details"]
    assert gateway.configs[automation_id]["trigger"][0]["at"] == "23:00:00"  # not overwritten


def test_engine_advanced_automation_is_conflict_not_edit(store):
    config = generate_routine_config(
        automation_id="ssr_existing",
        scene_id="twilight", scene_name="Twilight", behavior="apply",
        schedule=type("S", (), {"time": "19:30", "weekdays": None})(),
    )
    config["action"].append({"service": "light.turn_on", "entity_id": "light.lamp"})
    gateway = FakeHaAutomationGateway(configs={"ssr_existing": config})
    engine = make_engine(store, gateway=gateway)
    digest = canonical_config_digest(config)
    result = engine.handle({
        "command": "routine.update", "automation_id": "ssr_existing",
        "source_digest": digest, "time": "20:00",
    })
    assert result["ok"] is False and result["error"]["code"] == "conflict"
    assert result["error"]["details"]["kind"] == "routine_advanced"
    assert result["error"]["details"]["unsupported_reasons"]
    for command in ("routine.delete", "routine.enable", "routine.disable"):
        result = engine.handle({
            "command": command, "automation_id": "ssr_existing", "source_digest": digest,
        })
        assert result["ok"] is False and result["error"]["code"] == "conflict"


def test_engine_unknown_scene_is_not_found(store):
    engine = make_engine(store, gateway=FakeHaAutomationGateway())
    result = engine.handle({
        "command": "routine.create", "scene_id": "ghost",
        "behavior": "apply", "time": "19:30",
    })
    assert result["ok"] is False and result["error"]["code"] == "not_found"


def test_engine_enable_disable(store):
    gateway = FakeHaAutomationGateway()
    engine = make_engine(store, gateway=gateway)
    created = engine.handle({
        "command": "routine.create", "scene_id": "twilight",
        "behavior": "apply", "time": "19:30",
    })
    automation_id = created["data"]["routine"]["automation_id"]
    digest = created["data"]["routine"]["source_digest"]
    disabled = engine.handle({
        "command": "routine.disable", "automation_id": automation_id, "source_digest": digest,
    })
    assert disabled["ok"] is True and disabled["data"]["routine"]["enabled"] is False
    enabled = engine.handle({
        "command": "routine.enable", "automation_id": automation_id,
        "source_digest": disabled["data"]["routine"]["source_digest"],
    })
    assert enabled["ok"] is True and enabled["data"]["routine"]["enabled"] is True


# ---------------------------------------------------------------------------
# read route
# ---------------------------------------------------------------------------


def test_routines_route_projects_ha_state(store):
    gateway = FakeHaAutomationGateway(configs={
        "ssr_aaaabbbbcccc": generate_routine_config(
            automation_id="ssr_aaaabbbbcccc",
            scene_id="twilight", scene_name="Twilight", behavior="apply",
            schedule=type("S", (), {"time": "19:30", "weekdays": ("mon", "tue", "wed", "thu", "fri")})(),
        ),
        "hall_light": {
            "id": "hall_light", "alias": "Hall light",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall"}],
            "action": [{"service": "light.turn_on", "entity_id": "light.hall"}],
        },
    })
    gateway.entity_states["automation.ssr_aaaabbbbcccc"] = "off"
    engine = make_engine(store, gateway=gateway)
    status, payload = route(engine, "GET", f"{ROUTE_PREFIX}/routines")
    assert status == 200
    assert payload["available"] is True
    assert len(payload["routines"]) == 1
    routine = payload["routines"][0]
    assert routine["scene_id"] == "twilight"
    assert routine["enabled"] is False
    assert routine["schedule"] == {"time": "19:30", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}
    # refresh query forces a re-read (the explicit-refresh seam)
    _, payload2 = route(engine, "GET", f"{ROUTE_PREFIX}/routines", query={"refresh": "true"})
    assert payload2["refreshed_at"] is not None
    unknown = route(engine, "GET", f"{ROUTE_PREFIX}/routines", query={"refresh": "bogus"})
    assert unknown[0] == 200  # non-flag values are simply not a refresh
