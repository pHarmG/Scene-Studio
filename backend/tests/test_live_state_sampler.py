"""Live-state sampling orchestration tests (plan §3, §12: "one provider
failure does not suppress successful providers")."""

from scene_studio.domain.fixtures import Fixture
from scene_studio.live_state.sampler import sample_live_state
from scene_studio.service.ports import DiscoveryFetchers


def hue_fixture():
    return Fixture.from_dict(
        {"id": "g_strip", "name": "G Strip", "binding": {"provider": "hue_v2", "bridge_id": "b1", "resource_id": "rid-1"}}
    )


def wled_fixture():
    return Fixture.from_dict(
        {"id": "seg0", "name": "Seg0", "binding": {"provider": "wled", "device_id": "aabbcc", "segment_ids": [0]}}
    )


def ha_fixture():
    return Fixture.from_dict(
        {"id": "lamp", "name": "Lamp", "binding": {"provider": "ha_light", "ha_entity_id": "light.lamp"}}
    )


def test_one_provider_failure_does_not_blank_others():
    fixtures = [hue_fixture(), wled_fixture(), ha_fixture()]
    fetchers = DiscoveryFetchers(
        fetch_hue=lambda: {"data": [{"id": "rid-1", "on": {"on": True}, "color": {"xy": {"x": 0.4, "y": 0.4}}}]},
        fetch_wled_state=lambda: (_ for _ in ()).throw(RuntimeError("connection refused")),
        fetch_ha_states=lambda: {"light.lamp": {"state": "on", "attributes": {"rgb_color": [1, 2, 3]}}},
    )
    snapshot = sample_live_state(fixtures, fetchers, "2026-09-16T12:00:00Z")

    assert snapshot.providers["hue_v2"].ok is True
    assert snapshot.providers["wled"].ok is False
    assert "connection refused" in snapshot.providers["wled"].detail
    assert snapshot.providers["ha_light"].ok is True

    # the failing provider's fixture is simply absent, not present-but-wrong
    assert "g_strip" in snapshot.fixtures
    assert "seg0" not in snapshot.fixtures
    assert "lamp" in snapshot.fixtures


def test_no_fetcher_configured_reports_honest_status_without_calling_anything():
    fixtures = [hue_fixture()]
    fetchers = DiscoveryFetchers()  # every fetcher None
    snapshot = sample_live_state(fixtures, fetchers, "2026-09-16T12:00:00Z")
    assert snapshot.providers["hue_v2"].ok is False
    assert "no fetcher" in snapshot.providers["hue_v2"].detail
    assert snapshot.fixtures == {}


def test_provider_with_no_bound_fixtures_is_never_sampled():
    fixtures = [hue_fixture()]  # no wled/ha_light fixtures at all
    calls = []
    fetchers = DiscoveryFetchers(
        fetch_hue=lambda: {"data": []},
        fetch_wled_state=lambda: calls.append("wled") or {},
        fetch_ha_states=lambda: calls.append("ha") or {},
    )
    sample_live_state(fixtures, fetchers, "2026-09-16T12:00:00Z")
    assert calls == []  # no wasted network calls for providers nothing is bound to


def test_malformed_payload_is_isolated_not_raised():
    fixtures = [hue_fixture()]
    fetchers = DiscoveryFetchers(fetch_hue=lambda: {"data": "not-a-list"})
    snapshot = sample_live_state(fixtures, fetchers, "2026-09-16T12:00:00Z")
    assert snapshot.providers["hue_v2"].ok is True  # a malformed/empty payload still normalizes cleanly
    assert snapshot.fixtures["g_strip"].available is False


def test_snapshot_carries_observed_at_verbatim():
    snapshot = sample_live_state([], DiscoveryFetchers(), "2026-09-16T12:34:56Z")
    assert snapshot.observed_at == "2026-09-16T12:34:56Z"
    assert snapshot.to_dict()["observed_at"] == "2026-09-16T12:34:56Z"
