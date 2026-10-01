"""Live-state sampling orchestration (plan §3, §13).

Fetches each configured provider's batch read (reusing the exact same
``DiscoveryFetchers`` the engine already wires for ``discovery.run`` — the
payload shapes are identical), normalizes it, and commits one atomic
:class:`LiveStateSnapshot`. A provider fetch/normalize failure never blanks
the other providers' fixtures, and this module never touches the network
itself — the fetchers own that (transport-free, like ``discovery/``).

Deliberately NOT reused from ``service/engine.py::_run_discovery``: that
path emits WARNING events on every skipped/failed provider, which is fine
for an explicit, infrequent ``discovery.run`` but would flood the event log
at a ~3 s live-sampling cadence (plan §3 "no operational event spam on
successful sampling", §13 "no event-log spam"). Failures are represented in
the response's ``providers`` map instead.
"""

from __future__ import annotations

from ..domain.fixtures import Fixture
from ..domain.live_state import FixtureLiveState, LiveStateSnapshot, ProviderSampleStatus
from ..domain.sanitize import sanitize_string
from ..service.ports import DiscoveryFetchers
from .halight import normalize_ha_live_state
from .hue import normalize_hue_live_state
from .wled import normalize_wled_live_state


def sample_live_state(fixtures: list[Fixture], fetchers: DiscoveryFetchers, observed_at: str) -> LiveStateSnapshot:
    fixture_states: dict[str, FixtureLiveState] = {}
    provider_status: dict[str, ProviderSampleStatus] = {}

    _sample_one(fixtures, "hue_v2", fetchers.fetch_hue, normalize_hue_live_state, fixture_states, provider_status)
    _sample_one(fixtures, "wled", fetchers.fetch_wled_state, normalize_wled_live_state, fixture_states, provider_status)
    _sample_one(fixtures, "ha_light", fetchers.fetch_ha_states, normalize_ha_live_state, fixture_states, provider_status)

    return LiveStateSnapshot(observed_at=observed_at, fixtures=fixture_states, providers=provider_status)


def _sample_one(fixtures, provider: str, fetch, normalize, fixture_states: dict, provider_status: dict) -> None:
    """Fetch + normalize one provider; isolate its failure into
    ``provider_status`` without touching ``fixture_states`` for anyone else."""
    if not any(_bound_to(f, provider) for f in fixtures):
        return  # nothing of this provider bound: don't fetch, don't report
    if fetch is None:
        provider_status[provider] = ProviderSampleStatus(ok=False, detail="no fetcher configured")
        return
    try:
        payload = fetch()
    except Exception as exc:
        provider_status[provider] = ProviderSampleStatus(ok=False, detail=_safe_detail(exc))
        return
    if payload is None:
        provider_status[provider] = ProviderSampleStatus(ok=False, detail="no payload returned")
        return
    try:
        fixture_states.update(normalize(payload, fixtures))
        provider_status[provider] = ProviderSampleStatus(ok=True)
    except Exception as exc:  # never let a malformed payload take down the whole sample
        provider_status[provider] = ProviderSampleStatus(ok=False, detail=_safe_detail(exc))


def _bound_to(fixture: Fixture, provider: str) -> bool:
    return fixture.binding is not None and getattr(fixture.binding, "provider", None) == provider


def _safe_detail(exc: Exception) -> str:
    return sanitize_string(f"{type(exc).__name__}: {exc}")[:256]
