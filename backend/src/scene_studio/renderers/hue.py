"""Hue CLIP v2 renderer (workstream B1, extended by the renderer-coverage review).

Translates canonical scene intent into Hue CLIP v2 dry-run operation plans.
Payload shapes follow the live bridge API as exercised by the existing
legacy scene tooling (``build_light_payload``):

- on:               ``{"on": {"on": bool}}``
- dimming:          ``{"dimming": {"brightness": float}}``  (0..100)
- color:            ``{"color": {"xy": {"x": float, "y": float}}}``
- color temperature:``{"color_temperature": {"mirek": int}}``
- gradient:         ``{"gradient": {"points": [{"color": {"xy": ...}}, ...],
                      "mode": "interpolated_palette"}}``
- effects:          ``{"effects": {"status": "<name>"}}`` — only when the
                      fixture capabilities list the effect.
- transitions:      ``{"dynamics": {"duration": <ms>}}`` — CLIP v2 light
                      PUTs accept ``dynamics.duration`` in milliseconds;
                      ``state.transition_ms`` maps 1:1 (rounded to int,
                      clamped to 0..60000).

Fields absent from the ``FixtureState`` are omitted from the payload.

Grouped-light gating: CLIP v2 ``grouped_light`` resources support
on/dimming/color_temperature only — color, gradient, and effects are
per-light properties. When ``binding.resource_type == "grouped_light"``
those fields are omitted and the plan becomes ``approximate`` with the
note "grouped_light supports on/dimming/color_temperature only; set
per-light states via individual fixtures".

Dynamic intent (``motion.mode != static``):

- Hue dynamic state is a **scene-resource feature**, not a direct
  light-state feature (R5D live evidence: this bridge rejects
  ``PUT /light/{id} {"dynamics": {"status": "dynamic_palette"}}`` with 207
  "attribute cannot be written"). ``capabilities.dynamic_native`` -> the
  managed bridge-scene mechanism for every dynamic-native fixture: a
  ``hue.put_light`` operation with the static state (the fixture's managed
  scene action contribution) PLUS a plan-level ``hue.put_scene_dynamic``
  operation ``{"speed": 0..1, "palette": [hex...]}`` (the playback
  orchestration realizes it through a bridge-native dynamic scene with a
  ``recall.action = "dynamic_palette"``). Fidelity ``native`` — downgraded
  to ``approximate`` only when the static state itself carried
  approximations.
- No render path ever emits ``dynamics.status`` on a Hue light PUT (the
  ``provider_ext`` dynamics escape hatch cannot inject it either).
- not dynamic-native + ``strategy == native_preferred`` -> ``unsupported``
  with no operations.
- otherwise (``strategy == auto``) -> ``approximate`` static snapshot:
  ``palette_cycle`` renders the resolved palette color (pin, auto-spread,
  or first palette color when the fixture state itself sets no color);
  ``effect`` mode renders the static fields only.
``strategy == static`` always takes the plain static path (no palette
injection, no dynamic note).

Capability gating: gradient on a fixture without gradient capability falls
back to the first gradient point as a solid color (``approximate``); color
on a color-incapable fixture is omitted (``approximate``); mirek is clamped
to the fixture range (``approximate`` only when clamping occurs); an effect
not listed in capabilities makes the whole fixture plan ``unsupported``
(nothing would be executed for it).

``provider_ext["hue_v2"]`` escape hatch (contracts §2) is merged into the
``put_light`` payload — ext values win over computed ones:

- ``{"gradient": {"mode": "<mode>"}}`` overrides the mode of the payload's
  ``gradient`` object. Allowed CLIP v2 modes: ``interpolated_palette``,
  ``interpolated_palette_mirrored``, ``repeated_linear``, ``randomized``
  (default ``interpolated_palette``). Unknown modes -> ``approximate``
  note and the computed mode is kept. Other keys inside ``gradient``, or
  a mode with no gradient in the payload, are ignored with a note.
- ``{"dynamics": {...}}`` is merged into the payload's ``dynamics`` object
  (ext wins per key, e.g. overriding the mapped ``duration``) EXCEPT
  ``status``: dynamics.status is never written to a Hue light (ignored with
  a note).
- ``{"raw": {...}}`` is merged top-level into the payload for everything
  else CLIP v2 allows (ext wins over computed keys).
- Any other top-level ext key is ignored with an ``approximate`` note.
- Merge precedence inside the ext namespace: ``raw`` beats computed keys;
  the documented ``gradient.mode`` and ``dynamics`` keys beat ``raw``.

No network access happens anywhere in this module.
"""

from __future__ import annotations

from ..domain.bindings import PROVIDER_HUE_V2, HueBinding
from ..domain.capabilities import Capabilities
from ..domain.fixtures import Fixture
from ..domain.fidelity import FidelityLevel, FixtureRenderPlan, ProviderOperation
from ..domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy
from .base import Renderer
from .color import hex_to_xy

OP_PUT_LIGHT = "hue.put_light"
OP_PUT_SCENE_DYNAMIC = "hue.put_scene_dynamic"

GRADIENT_MODES = (
    "interpolated_palette",
    "interpolated_palette_mirrored",
    "repeated_linear",
    "randomized",
)
_DEFAULT_GRADIENT_MODE = "interpolated_palette"
_DURATION_MIN_MS = 0
_DURATION_MAX_MS = 60000
_EXT_KEYS = ("dynamics", "gradient", "raw")
_GROUPED_LIGHT_NOTE = (
    "grouped_light supports on/dimming/color_temperature only; set per-light states via individual fixtures"
)


def _clamp_duration(value: float) -> int:
    """Canonical ``transition_ms`` -> CLIP v2 ``dynamics.duration`` (ms)."""
    return min(max(int(round(value)), _DURATION_MIN_MS), _DURATION_MAX_MS)


class HueRenderer(Renderer):
    """Renders canonical scene intent as Hue CLIP v2 operation plans."""

    provider = PROVIDER_HUE_V2

    def plan_fixture(
        self,
        fixture: Fixture,
        state: FixtureState,
        *,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        binding = fixture.binding
        if not isinstance(binding, HueBinding):
            raise ValueError(f"fixture '{fixture.id}' is not bound to a hue_v2 resource")
        capabilities = fixture.capabilities if fixture.capabilities is not None else Capabilities()

        dynamic_request = motion.mode is not MotionMode.STATIC and motion.strategy is not MotionStrategy.STATIC
        if dynamic_request:
            return self._plan_dynamic(fixture, binding, capabilities, state, motion, palette)
        return self._plan_static(fixture, binding, capabilities, state)

    def action_contribution(self, fixture: Fixture, state: FixtureState) -> dict:
        """The fixture's CLIP v2 scene `action` payload: the capability-gated
        static light state (on/dimming/color/temperature/gradient) a managed
        bridge scene applies to this light. Dynamics are excluded — scene
        recall supplies them. Used by the playback orchestration layer to
        aggregate per-group managed-scene actions."""
        plan = self._plan_static(fixture, fixture.binding, fixture.capabilities or Capabilities(), state)
        for operation in plan.operations:
            if operation.op == OP_PUT_LIGHT:
                return dict(operation.payload)
        return {}

    # ------------------------------------------------------------------
    # dynamic intent
    # ------------------------------------------------------------------

    def _plan_dynamic(
        self,
        fixture: Fixture,
        binding: HueBinding,
        capabilities: Capabilities,
        state: FixtureState,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        if not capabilities.dynamic_native:
            if motion.strategy is MotionStrategy.NATIVE_PREFERRED:
                return FixtureRenderPlan(
                    fixture_id=fixture.id,
                    provider=self.provider,
                    fidelity=FidelityLevel.UNSUPPORTED,
                    reason=(
                        f"motion '{motion.mode.value}' requires native execution (strategy=native_preferred) "
                        f"but fixture '{fixture.id}' has no native dynamic support"
                    ),
                    operations=[],
                )
            # strategy == auto: best-effort static snapshot.
            inject_color = None
            if motion.mode is MotionMode.PALETTE_CYCLE and palette and state.color is None:
                inject_color = palette[0]
            plan = self._plan_static(fixture, binding, capabilities, state, inject_color=inject_color)
            note = (
                "palette_cycle rendered as a static snapshot of the first palette color"
                if motion.mode is MotionMode.PALETTE_CYCLE
                else "effect motion rendered as a static snapshot (no native dynamic support)"
            )
            return FixtureRenderPlan(
                fixture_id=fixture.id,
                provider=self.provider,
                fidelity=FidelityLevel.APPROXIMATE,
                reason=(
                    f"motion '{motion.mode.value}' cannot run natively on fixture '{fixture.id}'; {note}"
                    + (f"; {plan.reason}" if plan.reason else "")
                ),
                operations=plan.operations,
            )

        # Every dynamic-native fixture — gradient-capable or not, whatever
        # the palette size — executes through the managed bridge scene
        # mechanism. Per-light dynamics.status writes are not supported by
        # the bridge (R5D live evidence) and are never planned.
        return self._plan_scene_dynamic(fixture, binding, capabilities, state, motion, palette)

    def _plan_scene_dynamic(
        self,
        fixture: Fixture,
        binding: HueBinding,
        capabilities: Capabilities,
        state: FixtureState,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        """Managed bridge-scene mechanism: the ONLY Hue dynamic execution
        path. The fixture contributes its static state as the scene action;
        the playback orchestration layer aggregates actions per Hue group
        into a managed scene resource carrying the canonical palette +
        scene-level speed, and drives it with recall actions
        (dynamic_palette / static)."""
        static_plan = self._plan_static(fixture, binding, capabilities, state)
        if static_plan.fidelity is FidelityLevel.UNSUPPORTED:
            return static_plan
        scene_operation = ProviderOperation(
            provider=self.provider,
            op=OP_PUT_SCENE_DYNAMIC,
            resource_ref=binding.resource_id,
            payload={"speed": motion.speed, "palette": list(palette)},
            description="Managed dynamic scene execution (scene palette + speed; recall dynamic_palette)",
        )
        reason = (
            f"fixture '{fixture.id}' executes motion '{motion.mode.value}' through the managed dynamic "
            f"scene mechanism (hue.put_scene_dynamic); Hue lights do not accept direct dynamics.status writes"
        )
        fidelity = FidelityLevel.NATIVE
        if static_plan.fidelity is FidelityLevel.APPROXIMATE:
            fidelity = FidelityLevel.APPROXIMATE
            reason = f"{reason}; {static_plan.reason}" if static_plan.reason else reason
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=self.provider,
            fidelity=fidelity,
            reason=reason,
            operations=[*static_plan.operations, scene_operation],
        )

    # ------------------------------------------------------------------
    # static intent
    # ------------------------------------------------------------------

    def _plan_static(
        self,
        fixture: Fixture,
        binding: HueBinding,
        capabilities: Capabilities,
        state: FixtureState,
        *,
        inject_color: str | None = None,
    ) -> FixtureRenderPlan:
        built = self._build_state_payload(binding, capabilities, state, inject_color=inject_color)
        if built is None:
            return self._unsupported_effect_plan(fixture, state)
        payload, approximations, native = built
        if approximations:
            fidelity = FidelityLevel.APPROXIMATE
        elif native:
            fidelity = FidelityLevel.NATIVE
        else:
            fidelity = FidelityLevel.EQUIVALENT
        operation = ProviderOperation(
            provider=self.provider,
            op=OP_PUT_LIGHT,
            resource_ref=binding.resource_id,
            payload=payload,
            description="Apply static scene state via Hue CLIP v2",
        )
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=self.provider,
            fidelity=fidelity,
            reason="; ".join(approximations),
            operations=[operation],
        )

    # ------------------------------------------------------------------
    # payload construction (shared by static and dynamic paths)
    # ------------------------------------------------------------------

    def _build_state_payload(
        self,
        binding: HueBinding,
        capabilities: Capabilities,
        state: FixtureState,
        *,
        inject_color: str | None = None,
        dynamics: dict | None = None,
    ) -> tuple[dict, list[str], bool] | None:
        """Build the put_light payload.

        Returns ``(payload, approximations, native)`` or ``None`` when the
        state cannot be represented at all (unknown effect).
        """
        payload: dict = {}
        approximations: list[str] = []
        native = False
        per_light = binding.resource_type == "light"

        if state.on is not None:
            if capabilities.on_off:
                payload["on"] = {"on": state.on}
            else:
                approximations.append("on/off requested but fixture reports no on/off capability; omitted")

        if state.brightness is not None:
            if capabilities.brightness:
                payload["dimming"] = {"brightness": float(state.brightness)}
            else:
                approximations.append("brightness requested but fixture reports no dimming capability; omitted")

        if state.color_temp_mirek is not None:
            mirek_range = capabilities.color_temp
            if mirek_range is None:
                approximations.append(
                    "color temperature requested but fixture reports no color_temperature capability; omitted"
                )
            else:
                mirek = min(max(state.color_temp_mirek, mirek_range.mirek_min), mirek_range.mirek_max)
                payload["color_temperature"] = {"mirek": mirek}
                if mirek != state.color_temp_mirek:
                    approximations.append(
                        f"color temperature {state.color_temp_mirek} clamped to fixture mirek range "
                        f"{mirek_range.mirek_min}..{mirek_range.mirek_max}"
                    )

        if state.effect is not None and state.effect not in capabilities.effects:
            # Unknown effect: the requested intent cannot be represented at
            # all, so nothing is planned for this fixture.
            return None

        if state.effect is not None:
            if per_light:
                # Known effect: the provider executes it directly.
                payload["effects"] = {"status": state.effect}
                native = True
            else:
                approximations.append(f"effect '{state.effect}' requested but {_GROUPED_LIGHT_NOTE}")

        color_hex = state.color if state.color is not None else inject_color
        if color_hex is not None:
            if not per_light:
                approximations.append(f"color {color_hex} requested but {_GROUPED_LIGHT_NOTE}")
            elif capabilities.color_xy:
                x, y = hex_to_xy(color_hex)
                payload["color"] = {"xy": {"x": x, "y": y}}
            else:
                approximations.append(
                    f"color {color_hex} requested but fixture reports no color capability; omitted"
                )

        if state.gradient:
            gradient_capability = capabilities.gradient
            if not per_light:
                approximations.append(f"gradient requested but {_GROUPED_LIGHT_NOTE}")
            elif gradient_capability is not None:
                points = state.gradient
                if len(points) > gradient_capability.max_points:
                    approximations.append(
                        f"gradient truncated from {len(points)} to {gradient_capability.max_points} points "
                        f"(fixture max_points)"
                    )
                    points = points[: gradient_capability.max_points]
                payload["gradient"] = {
                    "points": [
                        {"color": {"xy": {"x": x, "y": y}}} for x, y in (hex_to_xy(hex_color) for hex_color in points)
                    ],
                    "mode": _DEFAULT_GRADIENT_MODE,
                }
                native = True
            elif capabilities.color_xy:
                x, y = hex_to_xy(state.gradient[0])
                payload["color"] = {"xy": {"x": x, "y": y}}
                approximations.append(
                    f"fixture has no gradient capability; first gradient color {state.gradient[0]} "
                    "rendered as solid color"
                )
            else:
                approximations.append(
                    "gradient requested but fixture has neither gradient nor color capability; omitted"
                )

        # transitions: CLIP v2 dynamics.duration (milliseconds).
        # NOTE: dynamics.status is NEVER written to a light — Hue scene
        # recall supplies dynamics (R5D live evidence: light PUTs reject it).
        computed_dynamics: dict = {}
        if state.transition_ms is not None:
            computed_dynamics["duration"] = _clamp_duration(state.transition_ms)
        if dynamics:
            computed_dynamics.update(dynamics)
        if computed_dynamics:
            payload["dynamics"] = computed_dynamics

        self._apply_provider_ext(state, payload, approximations)
        return payload, approximations, native

    def _apply_provider_ext(self, state: FixtureState, payload: dict, approximations: list[str]) -> None:
        """Merge ``provider_ext["hue_v2"]`` into the payload (ext wins).

        Precedence: ``raw`` beats computed keys; the documented
        ``gradient.mode`` and ``dynamics`` keys beat ``raw``. See the
        module docstring for the full semantics.
        """
        ext = state.provider_ext.get(PROVIDER_HUE_V2) if state.provider_ext else None
        if ext is None:
            return
        if not isinstance(ext, dict):
            raise ValueError(
                f"provider_ext.{PROVIDER_HUE_V2} must be an object, got {type(ext).__name__}"
            )
        unknown = sorted(key for key in ext if key not in _EXT_KEYS)
        if unknown:
            approximations.append(
                f"provider_ext.{PROVIDER_HUE_V2} key(s) ignored: {', '.join(unknown)}"
            )
        raw = ext.get("raw")
        if raw is not None:
            if isinstance(raw, dict):
                payload.update(raw)
            else:
                approximations.append(f"provider_ext.{PROVIDER_HUE_V2}.raw ignored (expected an object)")
        gradient_mode = None
        ext_gradient = ext.get("gradient")
        if ext_gradient is not None:
            if isinstance(ext_gradient, dict):
                extra_keys = sorted(key for key in ext_gradient if key != "mode")
                if extra_keys:
                    approximations.append(
                        f"provider_ext.{PROVIDER_HUE_V2}.gradient key(s) ignored: {', '.join(extra_keys)}"
                    )
                mode = ext_gradient.get("mode")
                if mode is not None:
                    if mode in GRADIENT_MODES:
                        gradient_mode = mode
                    else:
                        approximations.append(
                            f"provider_ext.{PROVIDER_HUE_V2}.gradient mode {mode!r} is not a CLIP v2 "
                            "gradient mode; omitted"
                        )
            else:
                approximations.append(f"provider_ext.{PROVIDER_HUE_V2}.gradient ignored (expected an object)")
        if gradient_mode is not None:
            gradient = payload.get("gradient")
            if isinstance(gradient, dict):
                gradient["mode"] = gradient_mode
            else:
                approximations.append(
                    f"provider_ext.{PROVIDER_HUE_V2}.gradient mode ignored (no gradient in the payload)"
                )
        ext_dynamics = ext.get("dynamics")
        if ext_dynamics is not None:
            if isinstance(ext_dynamics, dict):
                if "status" in ext_dynamics:
                    # Hue lights reject dynamics.status writes outright (R5D
                    # live evidence); the escape hatch may not reintroduce it.
                    approximations.append(
                        f"provider_ext.{PROVIDER_HUE_V2}.dynamics.status ignored: dynamics.status is "
                        "never written to a Hue light (scene recall supplies dynamics)"
                    )
                    ext_dynamics = {k: v for k, v in ext_dynamics.items() if k != "status"}
                if ext_dynamics:
                    computed = payload.get("dynamics")
                    payload["dynamics"] = {**(computed if isinstance(computed, dict) else {}), **ext_dynamics}
            else:
                approximations.append(f"provider_ext.{PROVIDER_HUE_V2}.dynamics ignored (expected an object)")

    def _unsupported_effect_plan(self, fixture: Fixture, state: FixtureState) -> FixtureRenderPlan:
        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=self.provider,
            fidelity=FidelityLevel.UNSUPPORTED,
            reason=(
                f"effect '{state.effect}' is not listed in the capabilities of fixture '{fixture.id}'; "
                "no operations planned"
            ),
            operations=[],
        )
