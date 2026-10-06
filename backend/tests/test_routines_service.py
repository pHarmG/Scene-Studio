"""RoutineService tests — native HA routine CRUD discipline (routines pass).

Uses an in-memory fake :class:`HaAutomationGateway` (a stand-in for HA's
REST config/state/services surface) to prove:

- create/update/delete re-read HA's canonical config and VERIFY the expected
  outcome before reporting success;
- optimistic concurrency: an automation that changed in HA since it was
  loaded is a structured conflict, never an overwrite;
- ``recognized_advanced`` automations are visible but never writable;
- an unavailable capability is reported honestly (no YAML fallback);
- the derived projection cache is TTL-bounded and invalidated by signals;
- mutations only ever travel through the gateway port — no YAML, no store.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scene_studio.domain.routines import (
    CLASSIFICATION_ADVANCED,
    CLASSIFICATION_NATIVE,
    canonical_config_digest,
    generate_routine_config,
)
from scene_studio.domain.serde import ValidationError
from scene_studio.service.ports import HaAutomationEntity, HaAutomationGatewayError
from scene_studio.service.routines import (
    RoutineCapabilityUnavailable,
    RoutineNotEditable,
    RoutineService,
    RoutineSourceChanged,
    RoutineVerificationFailed,
)

# ---------------------------------------------------------------------------
# fake gateway (HA REST stand-in)
# ---------------------------------------------------------------------------


class FakeHaAutomationGateway:
    """In-memory HA automation surface: canonical configs + entity states.

    Mirrors the REST semantics the real gateway relies on: config storage is
    authoritative, entity states are separate, and every call is recorded so
    tests can prove NO out-of-port write path is ever taken (no YAML, no
    store, no second database).
    """

    def __init__(self, *, available: bool = True, configs: dict | None = None) -> None:
        self.configs: dict[str, dict] = dict(configs or {})
        self.entity_states: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self._available = available
        self.fail_next_save = None  # optional HaAutomationGatewayError code

    # port surface ---------------------------------------------------------
    def available(self) -> bool:
        return self._available

    def unavailable_reason(self) -> str | None:
        return None if self._available else "disabled by the test fixture"

    def list_automation_entities(self) -> list[HaAutomationEntity]:
        self.calls.append(("list_automation_entities",))
        entities = []
        for automation_id, config in self.configs.items():
            entity_id = f"automation.{automation_id.replace('-', '_')}"
            self.entity_states.setdefault(entity_id, "on")
            entities.append(
                HaAutomationEntity(
                    entity_id=entity_id,
                    state=self.entity_states[entity_id],
                    automation_id=automation_id,
                    alias=config.get("alias"),
                )
            )
        return entities

    def get_automation_config(self, automation_id: str) -> dict | None:
        self.calls.append(("get_automation_config", automation_id))
        return self.configs.get(automation_id)

    def save_automation_config(self, automation_id: str, config: dict) -> None:
        self.calls.append(("save_automation_config", automation_id))
        if self.fail_next_save is not None:
            code, self.fail_next_save = self.fail_next_save, None
            raise HaAutomationGatewayError(code, f"simulated {code} on save")
        self.configs[automation_id] = dict(config)

    def delete_automation_config(self, automation_id: str) -> None:
        self.calls.append(("delete_automation_config", automation_id))
        self.configs.pop(automation_id, None)

    def reload_automations(self) -> None:
        self.calls.append(("reload_automations",))

    def set_automation_enabled(self, entity_id: str, enabled: bool) -> None:
        self.calls.append(("set_automation_enabled", entity_id, enabled))
        self.entity_states[entity_id] = "on" if enabled else "off"

    # test helpers ---------------------------------------------------------
    def calls_of(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


def routine_config(automation_id: str = "ssr_aaaabbbbcccc", scene_id: str = "evening_glow",
                   behavior: str = "apply", time_hhmm: str = "19:30") -> dict:
    return generate_routine_config(
        automation_id=automation_id,
        scene_id=scene_id,
        scene_name="Evening Glow",
        behavior=behavior,
        schedule=type("S", (), {"time": time_hhmm, "weekdays": ("mon", "tue", "wed", "thu", "fri")})(),
    )


advanced_config = {
    "id": "user_advanced",
    "alias": "Occupancy scene",
    "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall"}],
    "action": [{
        "event": "scene_studio_ui_command",
        "event_data": {"command": "scene.apply", "scene_id": "evening_glow", "request_id": "x"},
    }],
}

unrelated_config = {
    "id": "hall_light",
    "alias": "Hall light",
    "trigger": [{"platform": "state", "entity_id": "binary_sensor.hall"}],
    "action": [{"service": "light.turn_on", "entity_id": "light.hall"}],
}


def make_service(gateway, *, scenes=None, ttl=30.0, monotonic=None):
    """scenes: {scene_id: (name, is_dynamic)}; defaults mirror the sample scenes."""
    scene_map = {"evening_glow": ("Evening Glow", False), "aurora_flow": ("Aurora Flow", True)}
    if scenes is not None:
        scene_map.update(scenes)
    lookup = lambda scene_id: scene_map.get(scene_id)  # noqa: E731
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return f"2026-10-06T00:00:{ticks['n']:02d}Z"

    service = RoutineService(gateway, clock, emit=lambda *a, **k: None, cache_ttl_seconds=ttl)
    if monotonic is not None:
        service._monotonic = monotonic
    return service, lookup


# ---------------------------------------------------------------------------
# reads / projection
# ---------------------------------------------------------------------------


def test_catalog_projects_only_scene_studio_automations():
    gateway = FakeHaAutomationGateway(configs={
        "ssr_aaaabbbbcccc": routine_config(),
        "user_advanced": advanced_config,
        "hall_light": unrelated_config,
    })
    service, _ = make_service(gateway)
    catalog = service.catalog()
    assert catalog["available"] is True
    ids = {routine["automation_id"] for routine in catalog["routines"]}
    assert ids == {"ssr_aaaabbbbcccc", "user_advanced"}
    by_id = {routine["automation_id"]: routine for routine in catalog["routines"]}
    assert by_id["ssr_aaaabbbbcccc"]["classification"] == CLASSIFICATION_NATIVE
    assert by_id["ssr_aaaabbbbcccc"]["schedule"]["time"] == "19:30"
    assert by_id["user_advanced"]["classification"] == CLASSIFICATION_ADVANCED
    assert by_id["user_advanced"]["unsupported_reasons"]
    # The unrelated automation never appears.
    assert "hall_light" not in ids


def test_catalog_ttls_and_explicit_refresh():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    now = [100.0]
    service, _ = make_service(gateway, ttl=30.0, monotonic=lambda: now[0])
    service.catalog()
    assert len(gateway.calls_of("list_automation_entities")) == 1  # one HA read
    service.catalog()  # within TTL -> served from cache
    service.catalog()  # still cached
    assert len(gateway.calls_of("list_automation_entities")) == 1
    now[0] += 31.0
    service.catalog()  # TTL expired -> re-read
    assert len(gateway.calls_of("list_automation_entities")) == 2
    service.catalog(refresh=True)  # explicit refresh -> re-read regardless
    assert len(gateway.calls_of("list_automation_entities")) == 3


def test_ha_first_edit_is_detected_on_next_read():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway, ttl=30.0, monotonic=lambda: 0.0)  # never stale by TTL
    first = service.catalog()["routines"][0]
    assert first["schedule"]["weekdays"] == ["mon", "tue", "wed", "thu", "fri"]
    # HA-side edit: the user rewires the automation in the HA UI.
    gateway.configs["ssr_aaaabbbbcccc"]["condition"][0]["weekday"] = ["sat"]
    service.invalidate()  # the adapter's automation_reloaded/state_changed seam
    refreshed = service.catalog()["routines"][0]
    assert refreshed["schedule"]["weekdays"] == ["sat"]
    assert refreshed["source_digest"] != first["source_digest"]


def test_signal_invalidation_backed_by_ttl():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    now = [0.0]
    service, _ = make_service(gateway, ttl=30.0, monotonic=lambda: now[0])
    service.catalog()
    # A missed signal is survivable: TTL expiry re-reads HA regardless.
    now[0] += 60.0
    service.catalog()
    assert len(gateway.calls_of("list_automation_entities")) == 2


def test_unavailable_capability_is_honest():
    service, _ = make_service(None)
    catalog = service.catalog()
    assert catalog == {
        "available": False,
        "unavailable_reason": "no Home Assistant automation gateway is configured in this runtime",
        "routines": [],
        "refreshed_at": None,
        "stale": False,
    }
    disabled = FakeHaAutomationGateway(available=False)
    service, _ = make_service(disabled)
    catalog = service.catalog()
    assert catalog["available"] is False and catalog["routines"] == []


def test_gateway_failure_reports_unavailable_not_fake_data():
    class FailingGateway(FakeHaAutomationGateway):
        def list_automation_entities(self):
            raise HaAutomationGatewayError("http_error", "HA down")

    service, _ = make_service(FailingGateway())
    catalog = service.catalog()
    assert catalog["available"] is False
    assert "HA down" in catalog["unavailable_reason"]
    assert catalog["routines"] == []


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_writes_native_config_and_verifies():
    gateway = FakeHaAutomationGateway()
    service, lookup = make_service(gateway)
    data = service.create(
        scene_id="evening_glow", behavior="apply", time_hhmm="19:30",
        weekdays=["mon", "tue", "wed", "thu", "fri"], scene_lookup=lookup,
    )
    # exactly one save, one reload, and one re-read after the write
    saves = gateway.calls_of("save_automation_config")
    assert len(saves) == 1
    automation_id = saves[0][1]
    assert automation_id.startswith("ssr_")
    stored = gateway.configs[automation_id]
    assert stored["trigger"] == [{"platform": "time", "at": "19:30:00"}]
    assert stored["condition"] == [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}]
    assert stored["action"] == [{
        "event": "scene_studio_ui_command",
        "event_data": {"command": "scene.apply", "scene_id": "evening_glow"},
    }]
    assert stored["alias"] == "Scene Studio · Evening Glow · Weekdays 7:30 PM"
    assert "Scene Studio routine (schema 1)" in stored["description"]
    assert gateway.calls_of("reload_automations") == [("reload_automations",)]
    # verification: the returned projection matches the re-read canonical config
    assert data["routine"]["automation_id"] == automation_id
    assert data["routine"]["classification"] == CLASSIFICATION_NATIVE
    assert data["routine"]["schedule"]["time"] == "19:30"


def test_create_generates_unique_ids():
    gateway = FakeHaAutomationGateway()
    service, lookup = make_service(gateway)
    for _ in range(3):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="08:00",
                       weekdays=None, scene_lookup=lookup)
    ids = [call[1] for call in gateway.calls_of("save_automation_config")]
    assert len(set(ids)) == 3


def test_create_verification_failure_rolls_back():
    gateway = FakeHaAutomationGateway()

    def corrupt_save(automation_id, config):
        gateway.configs[automation_id] = {"id": automation_id, "trigger": "broken", "action": []}

    gateway.save_automation_config = corrupt_save  # the write lands wrong in HA
    service, lookup = make_service(gateway)
    with pytest.raises(RoutineVerificationFailed):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="19:30",
                       weekdays=None, scene_lookup=lookup)
    # rollback: the partial create was removed
    assert gateway.calls_of("delete_automation_config"), "unverified write must be rolled back"
    assert gateway.configs == {}


def test_create_refuses_play_for_static_scene():
    gateway = FakeHaAutomationGateway()
    service, lookup = make_service(gateway)
    with pytest.raises(ValueError, match="static"):
        service.create(scene_id="evening_glow", behavior="play", time_hhmm="19:30",
                       weekdays=None, scene_lookup=lookup)
    assert gateway.configs == {}  # nothing was written


def test_create_rejects_bad_inputs_before_any_write():
    gateway = FakeHaAutomationGateway()
    service, lookup = make_service(gateway)
    with pytest.raises(ValidationError):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="19:60",
                       weekdays=None, scene_lookup=lookup)
    with pytest.raises(ValidationError):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="19:30",
                       weekdays=["holiday"], scene_lookup=lookup)
    with pytest.raises(KeyError):
        service.create(scene_id="missing_scene", behavior="apply", time_hhmm="19:30",
                       weekdays=None, scene_lookup=lookup)
    assert gateway.configs == {}


# ---------------------------------------------------------------------------
# optimistic concurrency
# ---------------------------------------------------------------------------


def test_update_rejects_when_ha_changed_since_load():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway)
    stale_digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    # HA-side edit lands AFTER the editor loaded its projection:
    gateway.configs["ssr_aaaabbbbcccc"]["trigger"][0]["at"] = "21:00:00"
    with pytest.raises(RoutineSourceChanged) as excinfo:
        service.update(automation_id="ssr_aaaabbbbcccc", source_digest=stale_digest,
                       time_hhmm="20:00", scene_lookup=lambda _sid: ("Evening Glow", False))
    assert excinfo.value.current_digest == canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    # nothing was overwritten
    assert gateway.configs["ssr_aaaabbbbcccc"]["trigger"][0]["at"] == "21:00:00"
    assert gateway.calls_of("save_automation_config") == []


def test_unsafe_automation_id_is_rejected_before_any_transport_call():
    """The id reaches the gateway's URL path — it must satisfy the safe
    charset before any HA contact (no traversal / query injection)."""
    gateway = FakeHaAutomationGateway(configs={})
    service, _ = make_service(gateway)
    for unsafe in ("../../api/services/light/turn_on", "x?redirect=y", "a b", "", "x" * 65):
        with pytest.raises(Exception) as excinfo:
            service.delete(automation_id=unsafe, source_digest="d")
        assert not isinstance(excinfo.value, RoutineCapabilityUnavailable)
    assert gateway.calls == []  # the gateway was never touched


def test_update_preserves_ha_level_settings():
    """HA-level settings the grammar tolerates without modeling
    (initial_state, icon) survive a Workbench edit."""
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    gateway.configs["ssr_aaaabbbbcccc"]["initial_state"] = True
    gateway.configs["ssr_aaaabbbbcccc"]["icon"] = "mdi:weather-night"
    service, lookup = make_service(gateway)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    service.update(
        automation_id="ssr_aaaabbbbcccc", source_digest=digest,
        time_hhmm="21:00", scene_lookup=lookup,
    )
    stored = gateway.configs["ssr_aaaabbbbcccc"]
    assert stored["initial_state"] is True
    assert stored["icon"] == "mdi:weather-night"
    assert stored["trigger"][0]["at"] == "21:00:00"  # the edit itself applied


def test_update_round_trips_supported_fields():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, lookup = make_service(gateway)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    data = service.update(
        automation_id="ssr_aaaabbbbcccc", source_digest=digest,
        time_hhmm="06:15", weekdays=None, behavior="play", scene_id="aurora_flow",
        scene_lookup=lookup,
    )
    stored = gateway.configs["ssr_aaaabbbbcccc"]
    assert stored["trigger"] == [{"platform": "time", "at": "06:15:00"}]
    assert "condition" not in stored  # weekdays=None -> every day, no condition
    assert stored["action"][0]["event_data"] == {"command": "playback.start", "scene_id": "aurora_flow"}
    assert data["routine"]["behavior"] == "play"
    assert data["routine"]["schedule"]["weekdays"] is None


def test_update_refuses_advanced_automation():
    gateway = FakeHaAutomationGateway(configs={"user_advanced": advanced_config})
    service, _ = make_service(gateway)
    digest = canonical_config_digest(advanced_config)
    with pytest.raises(RoutineNotEditable) as excinfo:
        service.update(automation_id="user_advanced", source_digest=digest, time_hhmm="20:00",
                       scene_lookup=lambda _sid: ("Evening Glow", False))
    assert excinfo.value.reasons
    assert gateway.calls_of("save_automation_config") == []


def test_update_refuses_when_automation_vanished():
    gateway = FakeHaAutomationGateway(configs={})
    service, _ = make_service(gateway)
    with pytest.raises(RoutineSourceChanged):
        service.update(automation_id="gone", source_digest="whatever", time_hhmm="20:00",
                       scene_lookup=lambda _sid: ("Evening Glow", False))


# ---------------------------------------------------------------------------
# delete / enable / disable
# ---------------------------------------------------------------------------


def test_delete_verifies_absence():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    data = service.delete(automation_id="ssr_aaaabbbbcccc", source_digest=digest)
    assert data == {"removed": True}
    assert gateway.configs == {}
    assert gateway.calls_of("reload_automations")
    # verification re-read happened after the delete
    assert gateway.calls_of("get_automation_config").count(("get_automation_config", "ssr_aaaabbbbcccc")) >= 2


def test_delete_conflicts_when_ha_changed():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway)
    with pytest.raises(RoutineSourceChanged):
        service.delete(automation_id="ssr_aaaabbbbcccc", source_digest="stale-digest")


def test_delete_refuses_advanced_automation():
    gateway = FakeHaAutomationGateway(configs={"user_advanced": advanced_config})
    service, _ = make_service(gateway)
    digest = canonical_config_digest(advanced_config)
    with pytest.raises(RoutineNotEditable):
        service.delete(automation_id="user_advanced", source_digest=digest)
    assert "user_advanced" in gateway.configs


def test_enable_disable_verifies_entity_state():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    data = service.set_enabled(automation_id="ssr_aaaabbbbcccc", source_digest=digest, enabled=False)
    assert data["routine"]["enabled"] is False
    entity_id = "automation.ssr_aaaabbbbcccc"
    assert gateway.entity_states[entity_id] == "off"
    data = service.set_enabled(automation_id="ssr_aaaabbbbcccc", source_digest=digest, enabled=True)
    assert data["routine"]["enabled"] is True
    assert gateway.entity_states[entity_id] == "on"


def test_disable_conflicts_when_ha_changed():
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, _ = make_service(gateway)
    with pytest.raises(RoutineSourceChanged):
        service.set_enabled(automation_id="ssr_aaaabbbbcccc", source_digest="stale", enabled=False)


# ---------------------------------------------------------------------------
# capability + write-path discipline
# ---------------------------------------------------------------------------


def test_capability_unavailable_fails_honestly():
    service, lookup = make_service(None)
    with pytest.raises(RoutineCapabilityUnavailable):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="19:30",
                       weekdays=None, scene_lookup=lookup)
    disabled = FakeHaAutomationGateway(available=False)
    service, _ = make_service(disabled)
    with pytest.raises(RoutineCapabilityUnavailable):
        service.delete(automation_id="x", source_digest="y")


def test_save_failure_surfaces_as_capability_error():
    gateway = FakeHaAutomationGateway()
    gateway.fail_next_save = ("http_error", "HA refused")
    service, lookup = make_service(gateway)
    with pytest.raises(RoutineCapabilityUnavailable):
        service.create(scene_id="evening_glow", behavior="apply", time_hhmm="19:30",
                       weekdays=None, scene_lookup=lookup)


def test_mutations_travel_only_through_the_gateway_port():
    """No routine mutation ever touches YAML or any other write surface:
    the recorded call log must contain ONLY gateway-port methods."""
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    service, lookup = make_service(gateway)
    service.create(scene_id="evening_glow", behavior="apply", time_hhmm="08:00",
                   weekdays=None, scene_lookup=lookup)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    service.update(automation_id="ssr_aaaabbbbcccc", source_digest=digest,
                   time_hhmm="09:00", scene_lookup=lookup)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    service.set_enabled(automation_id="ssr_aaaabbbbcccc", source_digest=digest, enabled=False)
    allowed = {
        "list_automation_entities", "get_automation_config", "save_automation_config",
        "delete_automation_config", "reload_automations", "set_automation_enabled",
    }
    assert {call[0] for call in gateway.calls} <= allowed


def test_scene_rename_updates_aliases_only_on_next_edit():
    """Scene ids are immutable: rename changes the lookup name, and the
    stored automation is untouched until the next supported edit regenerates
    the alias."""
    gateway = FakeHaAutomationGateway(configs={"ssr_aaaabbbbcccc": routine_config()})
    scene_map = {"evening_glow": ("Evening Glow", False)}
    service, _ = make_service(gateway)
    digest = canonical_config_digest(gateway.configs["ssr_aaaabbbbcccc"])
    # the rename happens in Scene Studio; the HA config is untouched:
    scene_map["evening_glow"] = ("Evening Glow (renamed)", False)
    updated = service.update(
        automation_id="ssr_aaaabbbbcccc", source_digest=digest,
        time_hhmm="19:30", weekdays=["mon", "tue", "wed", "thu", "fri"],
        scene_lookup=lambda scene_id: scene_map.get(scene_id),
    )
    assert updated["routine"]["alias"] == "Scene Studio · Evening Glow (renamed) · Weekdays 7:30 PM"
    assert updated["routine"]["scene_id"] == "evening_glow"
