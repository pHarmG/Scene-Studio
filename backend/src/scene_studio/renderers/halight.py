"""Ordinary Home Assistant light renderer (Workstream B3).

Translates canonical scene intent into dry-run HA service-level light
operations. This renderer is intentionally small: plain HA lights have no
provider-native palette animation or gradient control, so reduced fidelity
is reported transparently instead of being hidden behind clever fallbacks.

Operation split (executor maps op names to HA services):

- ``ha.call_light``      -> ``light.turn_on``  with the payload as service data
- ``ha.call_light_off``  -> ``light.turn_off`` with ``{"entity_id": ...}``

Documented translation rules:

- ``brightness`` (0..100 float) -> ``brightness_pct`` int (rounded to the
  nearest integer). Emitted only when brightness is set and greater than 0;
  ``on: true`` at 0% brightness is not a meaningful HA call, so the field is
  omitted rather than clamped.
- ``color`` ``#rrggbb`` -> ``rgb_color`` ``[r, g, b]``.
- ``color_temp_mirek`` -> ``color_temp_mired`` (mirek passthrough; HA's
  modern light platform accepts ``color_temp_mired`` directly, no Kelvin
  conversion is performed here).
- ``transition_ms`` -> ``transition`` in SECONDS, ``round(ms / 1000, 1)``.
  Emitted on turn-on payloads only; the off payload intentionally carries
  just ``entity_id`` (transition-on-off is dropped, reported here as a
  documented reduction).
- ``gradient`` -> first gradient color applied as a solid ``rgb_color``;
  fidelity ``approximate`` ("no gradient support at HA level").
- ``effect`` -> included in the payload only when provided; fidelity
  ``approximate`` because effect support varies across ordinary HA lights.
- ``on: false`` -> ``ha.call_light_off``; this short-circuits even under
  dynamic motion (turning off is exact regardless of animation intent).
- ``provider_ext`` is ignored by this renderer (escape-hatch data is not
  interpreted at HA service level).

Capability transparency:

- A color (or gradient) on a fixture whose stored capabilities have neither
  ``color_xy`` nor ``color_temp`` cannot be rendered: the plan is
  ``unsupported`` when color is the only actionable intent, otherwise
  ``approximate`` with the color omitted and the degradation explained.
- When ``fixture.capabilities`` is absent, the fixture is assumed to be a
  generic dimmable HA light (no capability-based degradation) and the
  assumption is noted in the plan reason.
- A color requested on a temp-only light (``color_xy`` false but
  ``color_temp`` present) is emitted as-is: HA filters service fields the
  physical light cannot honor, and the capability check defined for this
  renderer only fires when no color channel exists at all.

Dynamic intent: fidelity is never ``native`` for this renderer because
provider-native dynamics are not available through plain HA lights.

- ``motion.mode != static`` with ``strategy == "native_preferred"`` ->
  ``unsupported`` (no operations).
- dynamic with ``strategy == "auto"`` -> ``approximate``: static render of
  the first palette color (palette is the canonical dynamic intent);
  without a palette the fixture state is rendered statically.
- dynamic with ``strategy == "static"`` -> plain static render.

No entity, area, or room assumptions beyond ``HaLightBinding.ha_entity_id``.
"""

from __future__ import annotations

from ..domain.bindings import HaLightBinding
from ..domain.capabilities import Capabilities
from ..domain.fidelity import FidelityLevel, FixtureRenderPlan, ProviderOperation
from ..domain.fixtures import Fixture
from ..domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy

PROVIDER = "ha_light"
OP_CALL_LIGHT = "ha.call_light"        # executor -> light.turn_on
OP_CALL_LIGHT_OFF = "ha.call_light_off"  # executor -> light.turn_off

_NO_NATIVE_DYNAMICS = (
    "provider-native dynamics are not available through plain HA lights"
)
_GENERIC_DIMMABLE_NOTE = (
    "fixture capabilities absent; assumed generic dimmable HA light"
)


def _hex_to_rgb(value: str) -> list[int]:
    """Convert canonical ``#rrggbb`` to ``[r, g, b]`` ints."""
    text = value.strip().lower()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        raise ValueError(f"expected #rrggbb hex color, got {value!r}")
    try:
        return [int(text[index : index + 2], 16) for index in (0, 2, 4)]
    except ValueError:
        raise ValueError(f"expected #rrggbb hex color, got {value!r}") from None


class HaLightRenderer:
    """Renders scene intent for ``HaLightBinding`` fixtures at HA service level."""

    provider = PROVIDER

    def plan_fixture(
        self,
        fixture: Fixture,
        state: FixtureState,
        *,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        binding = fixture.binding
        if not isinstance(binding, HaLightBinding):
            raise ValueError(
                f"fixture {fixture.id!r} requires an HaLightBinding, got "
                f"{type(binding).__name__ if binding is not None else None!r}"
            )

        # Turning off is exact regardless of motion; short-circuit first.
        if state.on is False:
            operation = ProviderOperation(
                provider=PROVIDER,
                op=OP_CALL_LIGHT_OFF,
                resource_ref=binding.ha_entity_id,
                payload={"entity_id": binding.ha_entity_id},
                description="executor maps ha.call_light_off to light.turn_off",
            )
            return FixtureRenderPlan(
                fixture_id=fixture.id,
                provider=PROVIDER,
                fidelity=FidelityLevel.EQUIVALENT,
                reason="exact: maps to light.turn_off",
                operations=[operation],
            )

        if motion.mode == MotionMode.STATIC or motion.strategy == MotionStrategy.STATIC:
            return self._plan_static(fixture, binding, state)

        if motion.strategy == MotionStrategy.NATIVE_PREFERRED:
            return FixtureRenderPlan(
                fixture_id=fixture.id,
                provider=PROVIDER,
                fidelity=FidelityLevel.UNSUPPORTED,
                reason=_NO_NATIVE_DYNAMICS,
                operations=[],
            )

        # Dynamic "auto": approximate with a static palette-color render.
        # Prefer the color already resolved onto the fixture state (pin /
        # auto-spread); fall back to palette[0] when the caller did not.
        color_override = None
        if palette and state.color is None and not state.gradient:
            color_override = palette[0]
        payload, degradations, assumptions = self._build_turn_on(
            fixture, binding, state, color_override=color_override
        )
        if not payload:
            return self._unsupported_plan(fixture, degradations)
        reasons = ["dynamic intent rendered as static: " + _NO_NATIVE_DYNAMICS]
        if color_override is not None:
            reasons.append(f"first palette color {color_override} applied as a solid color")
        reasons.extend(degradations)
        reasons.extend(assumptions)
        operation = self._turn_on_operation(payload)
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=PROVIDER,
            fidelity=FidelityLevel.APPROXIMATE,
            reason="; ".join(reasons),
            operations=[operation],
        )

    # ------------------------------------------------------------------
    # static rendering

    def _plan_static(
        self, fixture: Fixture, binding: HaLightBinding, state: FixtureState
    ) -> FixtureRenderPlan:
        payload, degradations, assumptions = self._build_turn_on(fixture, binding, state)
        if not payload:
            return self._unsupported_plan(fixture, degradations)
        reason = "; ".join(degradations + assumptions)
        fidelity = FidelityLevel.APPROXIMATE if degradations else FidelityLevel.EQUIVALENT
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=PROVIDER,
            fidelity=fidelity,
            reason=reason,
            operations=[self._turn_on_operation(payload)],
        )

    @staticmethod
    def _unsupported_plan(fixture: Fixture, notes: list[str]) -> FixtureRenderPlan:
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=PROVIDER,
            fidelity=FidelityLevel.UNSUPPORTED,
            reason="; ".join(notes),
            operations=[],
        )

    def _turn_on_operation(self, payload: dict) -> ProviderOperation:
        return ProviderOperation(
            provider=PROVIDER,
            op=OP_CALL_LIGHT,
            resource_ref=payload["entity_id"],
            payload=payload,
            description="executor maps ha.call_light to light.turn_on",
        )

    def _build_turn_on(
        self,
        fixture: Fixture,
        binding: HaLightBinding,
        state: FixtureState,
        *,
        color_override: str | None = None,
    ) -> tuple[dict, list[str], list[str]]:
        """Build the ``ha.call_light`` payload.

        Returns ``(payload, degradations, assumptions)``. ``degradations``
        list reductions that lower fidelity to ``approximate``;
        ``assumptions`` disclose guesses (e.g. missing capabilities) without
        changing fidelity.
        """
        capabilities = fixture.capabilities
        payload: dict = {"entity_id": binding.ha_entity_id}
        degradations: list[str] = []
        assumptions: list[str] = []

        color_hex, color_is_gradient = self._resolve_color(state, color_override)

        if color_hex is not None and self._is_colorless(capabilities):
            if self._color_is_only_intent(state):
                # Nothing else actionable: a color the fixture cannot show.
                return {}, [
                    "fixture does not support color (no color_xy or color_temp capability)"
                ], []
            degradations.append("fixture lacks color support; color omitted")
        elif color_hex is not None:
            payload["rgb_color"] = _hex_to_rgb(color_hex)
            if color_is_gradient:
                degradations.append(
                    "no gradient support at HA level; first gradient color "
                    f"{color_hex} applied as a solid color"
                )

        if state.color_temp_mirek is not None:
            payload["color_temp_mired"] = state.color_temp_mirek

        if state.effect:
            payload["effect"] = state.effect
            degradations.append("effect support varies on ordinary HA lights")

        if state.brightness is not None and state.brightness > 0:
            # Half-up rounding (brightness is non-negative) avoids Python's
            # banker's rounding surprising users at .5 boundaries.
            payload["brightness_pct"] = int(state.brightness + 0.5)

        if state.transition_ms is not None:
            payload["transition"] = round(state.transition_ms / 1000, 1)

        if capabilities is None:
            assumptions.append(_GENERIC_DIMMABLE_NOTE)

        return payload, degradations, assumptions

    @staticmethod
    def _resolve_color(
        state: FixtureState, color_override: str | None
    ) -> tuple[str | None, bool]:
        """Pick the solid color to emit; ``(hex, is_gradient_reduction)``."""
        if color_override is not None:
            return color_override, False
        if state.gradient:
            return state.gradient[0], True
        if state.color:
            return state.color, False
        return None, False

    @staticmethod
    def _is_colorless(capabilities: Capabilities | None) -> bool:
        """True when stored capabilities show no color channel at all."""
        if capabilities is None:
            return False
        return not capabilities.color_xy and capabilities.color_temp is None

    @staticmethod
    def _color_is_only_intent(state: FixtureState) -> bool:
        """True when color/gradient is the only actionable field (besides
        transition, which cannot carry a command on its own)."""
        return (
            state.on is None
            and state.brightness is None
            and state.color_temp_mirek is None
            and state.effect is None
        )
