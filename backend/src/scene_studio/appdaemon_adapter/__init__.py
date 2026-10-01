"""Thin AppDaemon adapter for the Scene Studio command service (plan §9.2).

Exposes :class:`SceneStudioApp` (deployment-pending, live HA validation is
deferred to R1) and :class:`RequestsProviderExecutor` (the production
:class:`~scene_studio.service.ports.ProviderExecutor`).
"""

from .adapter import (
    HAS_APPDAEMON,
    RequestsProviderExecutor,
    SceneStudioApp,
    _build_discovery_fetchers,
)

__all__ = [
    "HAS_APPDAEMON",
    "RequestsProviderExecutor",
    "SceneStudioApp",
    "_build_discovery_fetchers",
]
