"""Live fixture color-state read model (plan: live fixture color-state pass).

Normalized, provider-neutral "what does this fixture look like right now"
data — deliberately separate from fixture registry definition, scene
intent, renderer fidelity, and discovery observations (see
``.cursor/plans/scene_studio_live_fixture_color_state.plan.md`` §1/§10).

This is READ-ONLY, disposable data: sampling it never mutates the registry,
never bumps ``engine.revision``, and never emits operational events. It is
truthful about what was actually observed — a provider that cannot report
per-frame color (WLED mid-effect) is marked ``configured_dynamic``, never
silently upgraded to ``live``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Small, deliberately open vocabularies (not Enums): normalizers build these
# from provider payload shapes callers don't need to import an enum for.
COLOR_MODES = ("rgb", "cct", "gradient", "dynamic", "none", "unknown")
STATE_KINDS = ("live", "configured_dynamic", "unavailable")


@dataclass
class FixtureLiveState:
    """One fixture's normalized current-state observation."""

    fixture_id: str
    provider: str
    available: bool
    on: bool | None = None
    brightness: int | None = None  # 0..100, normalized
    color_mode: str = "unknown"  # one of COLOR_MODES
    display_colors: list[str] = field(default_factory=list)  # ordered #rrggbb, display-only
    color_temp_kelvin: int | None = None
    dynamic: bool = False
    state_kind: str = "unavailable"  # one of STATE_KINDS
    # External realtime-owner evidence (hyperHDR contention pass): None when
    # nobody else owns the fixture; for WLED ``{"controller": "wled", "lor":
    # <int>, "mainseg": <int>}`` when the device reports ``lor != 0``.
    # Display-only read model — the authoritative per-decision source is the
    # fresh state read the engine performs on the apply/playback path.
    external_owner: dict | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "fixture_id": self.fixture_id,
            "provider": self.provider,
            "available": self.available,
            "on": self.on,
            "brightness": self.brightness,
            "color_mode": self.color_mode,
            "display_colors": list(self.display_colors),
            "color_temp_kelvin": self.color_temp_kelvin,
            "dynamic": self.dynamic,
            "state_kind": self.state_kind,
        }
        if self.external_owner is not None:
            out["external_owner"] = dict(self.external_owner)
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class ProviderSampleStatus:
    """Per-provider sample outcome — never blanks the other providers."""

    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "detail": self.detail}


@dataclass
class LiveStateSnapshot:
    """One committed sampling cycle across every configured provider."""

    observed_at: str
    fixtures: dict[str, FixtureLiveState] = field(default_factory=dict)
    providers: dict[str, ProviderSampleStatus] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "observed_at": self.observed_at,
            "fixtures": {fixture_id: state.to_dict() for fixture_id, state in self.fixtures.items()},
            "providers": {provider: status.to_dict() for provider, status in self.providers.items()},
        }


def kelvin_to_hex(kelvin: float) -> str:
    """Display-only blackbody-radiation approximation of a Kelvin color
    temperature as ``#rrggbb`` (Tanner Helland's widely-used approximation
    of Mitchell Charity's blackbody data, clamped to the 1000..40000 K
    range Home/Hue/WLED color temperatures fall within).

    This is illustrative only — the exact Kelvin value is the authoritative
    reading and must be surfaced alongside this color, never replaced by it
    (plan §2.1 "the aura's RGB representation is illustrative; the Kelvin
    value is authoritative").
    """
    temp = max(1000.0, min(40000.0, float(kelvin))) / 100.0

    if temp <= 66:
        red = 255.0
    else:
        red = 329.698727446 * ((temp - 60) ** -0.1332047592)

    if temp <= 66:
        green = 99.4708025861 * _log(temp) - 161.1195681661
    else:
        green = 288.1221695283 * ((temp - 60) ** -0.0755148492)

    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * _log(temp - 10) - 305.0447927307

    def clamp_byte(value: float) -> int:
        return max(0, min(255, round(value)))

    return f"#{clamp_byte(red):02x}{clamp_byte(green):02x}{clamp_byte(blue):02x}"


def _log(value: float) -> float:
    return math.log(max(value, 1e-6))


def mirek_to_kelvin(mirek: float) -> int:
    """Hue/HA mirek (micro-reciprocal-degree) -> Kelvin, rounded."""
    return round(1_000_000.0 / max(1.0, float(mirek)))
