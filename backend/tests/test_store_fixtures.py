"""FixtureStore semantics: CRUD, rename/enable/bind, targets, resolve_target (A1)."""

import json
from pathlib import Path

import pytest

from scene_studio.domain.bindings import HueBinding, WledBinding
from scene_studio.domain.capabilities import Capabilities, GradientCapability
from scene_studio.domain.fixtures import HealthStatus, Target, derive_health
from scene_studio.domain.serde import ValidationError
from scene_studio.stores import ConflictError, FixtureStore, NotFoundError, StoreError

STUDIO_MEMBERS = {
    "g_strip",
    "middle_bar",
    "lamp",
    "lower_bar",
    "double_strip",
    "upper_bar",
    "custom_gradient",
    "office_strip",
    "wled_seg_0",
    "wled_seg_1",
    "wled_seg_2",
    "wled_seg_3",
    "wled_seg_4",
    "wled_seg_5",
}


def _seed_registry(root: Path, registry_data: dict) -> Path:
    directory = root / "registry"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "registry.json"
    path.write_text(json.dumps(registry_data), encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path, sample_registry_data) -> FixtureStore:
    _seed_registry(tmp_path, sample_registry_data)
    return FixtureStore(tmp_path)


# ---------------------------------------------------------------------------
# loading / listing
# ---------------------------------------------------------------------------

def test_load_sample_registry_round_trip(store):
    fixtures = store.list_fixtures()
    assert [f.id for f in fixtures] == sorted(f.id for f in fixtures)
    assert len(fixtures) == 24
    assert [t.id for t in store.list_targets()] == [
        "bathroom", "bedroom", "living_room", "studio", "whole_house"
    ]

    g_strip = store.get_fixture("g_strip")
    assert g_strip.name == "Hue G Strip"
    assert g_strip.groups == ["studio", "whole_house"]
    assert g_strip.binding.provider == "hue_v2"
    assert derive_health(g_strip) is HealthStatus.READY
    assert derive_health(store.get_fixture("double_strip")) is HealthStatus.DISABLED
    custom_gradient = store.get_fixture("custom_gradient")
    assert derive_health(custom_gradient) is HealthStatus.READY
    assert custom_gradient.capability_assessment.status.value == "limited"
    assert custom_gradient.capabilities.gradient is None


def test_fresh_root_is_empty_store(tmp_path):
    root = tmp_path / "deep" / "nested"
    store = FixtureStore(root)
    assert store.list_fixtures() == []
    assert store.list_targets() == []
    store.add_fixture({"id": "solo", "name": "Solo"})
    assert [f.id for f in FixtureStore(root).list_fixtures()] == ["solo"]


def test_listing_is_deterministic_across_instances(tmp_path, sample_registry_data):
    _seed_registry(tmp_path, sample_registry_data)
    first = [f.id for f in FixtureStore(tmp_path).list_fixtures()]
    second = [f.id for f in FixtureStore(tmp_path).list_fixtures()]
    assert first == second == sorted(first)


def test_get_unknown_fixture_raises_store_error(store):
    with pytest.raises(NotFoundError) as excinfo:
        store.get_fixture("ghost_light")
    assert isinstance(excinfo.value, StoreError)
    assert "ghost_light" in str(excinfo.value)


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------

READING_CHAIR = {
    "id": "reading_chair",
    "name": "Reading Chair Lamp",
    "location": "office",
    "groups": ["office"],
    "enabled": True,
    "binding": {"provider": "ha_light", "ha_entity_id": "light.reading_chair"},
    "capabilities": {"on_off": True, "brightness": True},
}


def test_add_fixture_persists_and_round_trips(store, tmp_path):
    added = store.add_fixture(READING_CHAIR)
    assert added.id == "reading_chair"
    assert store.get_fixture("reading_chair").to_dict() == added.to_dict()
    # a fresh instance over the same root sees the persisted document
    assert FixtureStore(tmp_path).get_fixture("reading_chair").to_dict() == added.to_dict()


def test_add_duplicate_fixture_rejected(store):
    with pytest.raises(ConflictError, match="g_strip"):
        store.add_fixture({"id": "g_strip", "name": "Impostor"})
    assert len(store.list_fixtures()) == 24
    assert store.get_fixture("g_strip").name == "Hue G Strip"


def test_add_fixture_validates_strictly(store):
    with pytest.raises(ValidationError, match="fixture_id"):
        store.add_fixture({"id": "Bad Id", "name": "x"})
    with pytest.raises(ValidationError, match="provider"):
        store.add_fixture(
            {"id": "knx_light", "name": "x", "binding": {"provider": "knx", "address": "1/2/3"}}
        )
    assert len(store.list_fixtures()) == 24


def test_remove_fixture(store, tmp_path):
    store.remove_fixture("lamp")
    assert "lamp" not in {f.id for f in FixtureStore(tmp_path).list_fixtures()}
    with pytest.raises(NotFoundError):
        store.remove_fixture("lamp")


# ---------------------------------------------------------------------------
# rename / enable-disable / health / location / groups / capabilities
# ---------------------------------------------------------------------------

def test_rename_fixture_keeps_id(store, tmp_path):
    renamed = store.rename_fixture("g_strip", "Studio Gradient Strip")
    assert renamed.id == "g_strip"
    assert renamed.name == "Studio Gradient Strip"
    fresh = FixtureStore(tmp_path)
    assert fresh.get_fixture("g_strip").name == "Studio Gradient Strip"
    assert len(fresh.list_fixtures()) == 24  # id preserved, nothing duplicated


def test_rename_fixture_rejects_invalid_name(store):
    with pytest.raises(ValidationError, match="name"):
        store.rename_fixture("g_strip", "")
    with pytest.raises(ValidationError, match="name"):
        store.rename_fixture("g_strip", "x" * 200)
    assert store.get_fixture("g_strip").name == "Hue G Strip"


def test_enable_disable_flips_derived_health(store):
    store.add_fixture({"id": "test_lamp", "name": "Test Lamp"})
    assert derive_health(store.get_fixture("test_lamp")) is HealthStatus.UNBOUND
    assert derive_health(store.disable("test_lamp")) is HealthStatus.DISABLED
    assert derive_health(store.enable("test_lamp")) is HealthStatus.UNBOUND

    store.bind("test_lamp", HueBinding(bridge_id="bridge-001", resource_id="abc"))
    assert derive_health(store.get_fixture("test_lamp")) is HealthStatus.READY
    assert derive_health(store.disable("test_lamp")) is HealthStatus.DISABLED
    assert derive_health(store.enable("test_lamp")) is HealthStatus.READY
    with pytest.raises(ValidationError, match="enabled"):
        store.set_enabled("test_lamp", "yes")


def test_set_health_override_and_clear(store):
    assert derive_health(store.set_health("g_strip", "missing")) is HealthStatus.MISSING
    # explicit override wins even after enable/disable (contracts section 1)
    assert derive_health(store.enable("g_strip")) is HealthStatus.MISSING
    assert derive_health(store.set_health("g_strip", HealthStatus.CONFLICTING)) is HealthStatus.CONFLICTING
    assert derive_health(store.set_health("g_strip", None)) is HealthStatus.READY
    with pytest.raises(ValidationError, match="health"):
        store.set_health("g_strip", "on_fire")


def test_set_location_and_groups_persist(store, tmp_path):
    moved = store.set_location("g_strip", "bathroom")
    assert moved.location == "bathroom"
    regrouped = store.set_groups("g_strip", ["bathroom", "whole_house"])
    assert regrouped.groups == ["bathroom", "whole_house"]

    fresh = FixtureStore(tmp_path)
    assert fresh.get_fixture("g_strip").location == "bathroom"
    assert fresh.get_fixture("g_strip").groups == ["bathroom", "whole_house"]
    # membership resolution follows the new groups immediately
    assert "g_strip" in {f.id for f in fresh.resolve_target("bathroom")}

    with pytest.raises(ValidationError, match="groups"):
        store.set_groups("g_strip", ["Sun Room"])


def test_update_capabilities_persists_and_clears(store, tmp_path):
    caps = Capabilities(
        brightness=True,
        color_xy=True,
        gradient=GradientCapability(max_points=5),
        effects=["candle"],
        dynamic_native=True,
    )
    assert store.update_capabilities("office_strip", caps).capabilities == caps
    assert FixtureStore(tmp_path).get_fixture("office_strip").capabilities == caps

    cleared = store.update_capabilities("office_strip", None)
    assert cleared.capabilities is None
    assert "capabilities" not in FixtureStore(tmp_path).get_fixture("office_strip").to_dict()
    with pytest.raises(ValidationError):
        store.update_capabilities("office_strip", {"on_off": "sometimes"})


# ---------------------------------------------------------------------------
# bind / rebind / unbind
# ---------------------------------------------------------------------------

def test_bind_replaces_binding_and_persists(store, tmp_path):
    store.bind("office_strip", WledBinding(device_id="aabbccddeeff", segment_ids=[0]))
    rebound = FixtureStore(tmp_path).get_fixture("office_strip")
    assert isinstance(rebound.binding, WledBinding)
    assert rebound.binding.segment_ids == [0]
    # a valid binding clears unbound derivation
    assert derive_health(rebound) is HealthStatus.READY


def test_bind_validates_binding_parses(store):
    original = store.get_fixture("g_strip").binding
    with pytest.raises(ValidationError, match="provider"):
        store.bind("g_strip", {"provider": "knx", "address": "1/2/3"})
    # failed rebind must not touch the stored binding
    assert store.get_fixture("g_strip").binding.to_dict() == original.to_dict()


def test_unbind_derives_unbound(store, tmp_path):
    assert derive_health(store.unbind("g_strip")) is HealthStatus.UNBOUND
    assert FixtureStore(tmp_path).get_fixture("g_strip").binding is None


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

def test_target_crud_and_duplicates(store, tmp_path):
    store.add_target(Target(id="theater", name="Theater", description="Media wall"))
    assert "theater" in {t.id for t in FixtureStore(tmp_path).list_targets()}
    with pytest.raises(ConflictError, match="theater"):
        store.add_target({"id": "theater", "name": "Again"})
    assert store.rename_target("theater", "Home Theater").name == "Home Theater"
    store.remove_target("theater")
    with pytest.raises(NotFoundError):
        store.get_target("theater")


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------

def test_resolve_target_declared_group(store):
    studio = store.resolve_target("studio")
    assert [f.id for f in studio] == sorted(STUDIO_MEMBERS)
    assert {f.id for f in studio} == STUDIO_MEMBERS
    # membership includes disabled fixtures; skipping them is apply-time
    # semantics (contracts section 3.4), not store semantics
    assert "double_strip" in {f.id for f in studio}
    assert "office_strip" in {f.id for f in studio}


def test_resolve_target_single_fixture(store):
    resolved = store.resolve_target("g_strip")
    assert [f.id for f in resolved] == ["g_strip"]


def test_resolve_target_unknown_and_malformed(store):
    with pytest.raises(NotFoundError, match="no such target"):
        store.resolve_target("podium")
    with pytest.raises(ValidationError, match="target_id"):
        store.resolve_target("Studio")  # display name, not an id


# ---------------------------------------------------------------------------
# determinism / copy semantics
# ---------------------------------------------------------------------------

def test_registry_updated_preserved_not_autostamped(store, tmp_path, sample_registry_data):
    store.rename_fixture("lamp", "Renamed Lamp")
    store.set_enabled("lamp", False)
    fresh = FixtureStore(tmp_path)
    assert fresh.registry().updated == sample_registry_data["updated"]


def test_returned_models_are_detached_copies(store):
    fixture = store.get_fixture("g_strip")
    fixture.name = "Tampered"
    assert store.get_fixture("g_strip").name == "Hue G Strip"

    registry = store.registry()
    registry.fixtures.clear()
    assert len(store.list_fixtures()) == 24
