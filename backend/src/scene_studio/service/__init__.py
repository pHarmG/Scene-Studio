"""Command service (integration wave 1): engine, ports, legacy mappers, HTTP API.

Pure stdlib. The engine talks to devices only through the
:class:`~scene_studio.service.ports.ProviderExecutor` port; the AppDaemon
adapter (``scene_studio.appdaemon_adapter``) is the only place allowed to
import ``requests``.
"""

from .engine import SceneStudioEngine
from .ports import (
    Clock,
    DiscoveryFetchers,
    MonotonicClock,
    ProviderExecutor,
    RecordingExecutor,
    SteppingClock,
    receipt,
)

__all__ = [
    "Clock",
    "DiscoveryFetchers",
    "MonotonicClock",
    "ProviderExecutor",
    "RecordingExecutor",
    "SceneStudioEngine",
    "SteppingClock",
    "receipt",
]
