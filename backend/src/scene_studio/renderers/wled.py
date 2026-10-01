"""WLED renderer — canonical scene intent to WLED ``/json/state`` operations.

Dry-run only: this module never touches the network and never embeds a
host/IP in its operations. Transport resolves ``binding.endpoint_hint``
elsewhere (contracts §2: endpoints are hints, never identity).

Operation shape — exactly one merged ``wled.post_state`` per fixture::

    {"on": <bool>?, "bri": <0..255>?, "transition": <0..65535>?,
     "ps"/"pl"/... via provider_ext.wled.top, "seg": [{...}, ...]}

``resource_ref`` is ``<device_id>:seg:<i>`` for a single-segment binding,
``<device_id>:seg:<i1>,<i2>,...`` for a multi-segment binding, and
``<device_id>:dev`` for a whole-device binding (empty ``segment_ids``).

Mapping decisions (documented for review, task B2 + renderer-coverage
review round):

- ``on``: per-segment (``{"on": bool}`` inside each ``seg`` entry) for
  segment bindings; top-level device ``on`` only when the state sets it
  and the binding is whole-device.
- ``brightness`` 0..100 -> ``bri`` 0..255 via ``round(brightness * 2.55)``
  clamped. Placement follows the binding scope so one fixture never dims
  another: segment-scoped bindings write per-segment ``bri`` inside each
  ``seg`` entry (top-level ``bri`` would dim ALL device segments,
  including segments owned by other fixtures); whole-device bindings
  write top-level ``bri``. Whole-device brightness is ``equivalent``
  because the device-level primitive drives all segments; segment-scoped
  plans are ``native``.
- ``color``: per-segment ``col: [[r, g, b]]`` — ``native`` on segment
  bindings. A whole-device binding has no segment ids to target, so the
  color entry is emitted without an ``id`` (WLED applies it to the
  device's default segment) and is classified ``approximate``.
- ``color_temp_mirek``: when ``capabilities.cct`` is true (discovered
  from ``info.leds.cct``), mirek maps to WLED's relative segment ``cct``
  scale (0 warmest .. 255 coldest over 1900K..10091K:
  ``cct = round((1_000_000/mirek - 1900) / 8191 * 255)`` clamped) via the
  white channel — ``native``. Without the capability the field is omitted
  and classified ``approximate``. (Neutral-white substitution was
  considered and rejected: it would emit a specific visible color the
  scene never asked for.)
- ``transition_ms``: WLED's top-level ``transition`` counts 100 ms units;
  ``transition = round(ms / 100)`` clamped to 0..65535 — ``equivalent``
  (pure unit conversion).
- ``gradient``: WLED has no native multi-point static gradient on a
  segment; the first gradient color is applied and the plan is
  ``approximate``.
- ``effect``: names resolve against the discovered effect catalog
  (``capabilities.effects``, captured from the device's ``/json/effects``
  array; list position = ``fx`` index) by exact case-insensitive match.
  Without a resolvable name the effect is ``unsupported`` unless
  ``state.provider_ext["wled"]`` carries an explicit numeric ``fx`` —
  precedence: explicit ext numeric ``fx`` > resolved name > none.
- provider palette names: a ``pal`` string in ``provider_ext["wled"]``
  resolves against ``capabilities.palettes`` (discovered
  ``/json/palettes``; position = ``pal`` index) the same way; a numeric
  ``pal`` passes through. Unknown palette names are ``unsupported``.
  Scene palette hex colors are never mapped to palette indexes.
- ``provider_ext["wled"]`` keys are merged into the segment payload as
  the namespaced escape hatch (contracts §2). A ``"top"`` sub-object is
  instead merged into the top-level state payload, enabling native
  preset recall (``ps``) and playlists (``pl``) for dynamic scenes; its
  keys are validated against the WLED 0.14 top-level state allowlist
  (``on, bri, transition, ps, pl, nl, lor, mainseg, tt``) and anything
  else raises a ``ValueError`` (validation-style rejection). Ext values
  win over computed ones in both placements.
- Motion: ``palette_cycle`` with ``capabilities.dynamic_native`` and a
  resolvable effect — an explicit ext numeric ``fx``, or the fixture
  state's own ``state.effect`` name resolved via the catalog — runs
  ``native`` with ``sx = round(speed * 255)`` and ``pal`` only when
  provider_ext specifies one. Without a resolvable ``fx``:
  ``native_preferred`` -> ``unsupported``; otherwise ``approximate``
  (static render using the first scene palette color). A missing
  ``dynamic_native`` capability blocks the native path even when ``fx``
  is present. ``motion.strategy == "static"`` or ``motion.mode ==
  "static"`` renders statically. ``motion.mode == "effect"`` resolves
  the fixture's own ``state.effect`` via the effect rules above.

Plan fidelity is the worst per-field fidelity; operations are still
emitted for the representable fields of a mixed state so dry-run output
shows exactly what would execute. A plan whose state yields no
representable payload has no operations.
"""

from __future__ import annotations

from ..domain.bindings import PROVIDER_WLED, WledBinding
from ..domain.capabilities import Capabilities
from ..domain.fidelity import FidelityLevel, FixtureRenderPlan, ProviderOperation
from ..domain.fixtures import Fixture
from ..domain.scenes import FixtureState, Motion, MotionMode, MotionStrategy

OP_POST_STATE = "wled.post_state"

_FIDELITY_SEVERITY = {
    FidelityLevel.NATIVE: 0,
    FidelityLevel.EQUIVALENT: 1,
    FidelityLevel.APPROXIMATE: 2,
    FidelityLevel.UNSUPPORTED: 3,
}

# WLED 0.14 top-level state keys accepted through provider_ext.wled.top.
_TOP_LEVEL_KEYS = frozenset({"on", "bri", "transition", "ps", "pl", "nl", "lor", "mainseg", "tt"})

# WLED cct scale endpoints (0.14 relative white-channel CCT): 1900K..10091K.
_CCT_KELVIN_WARM = 1900
_CCT_KELVIN_COLD = 10091


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse canonical ``#rrggbb`` into an ``(r, g, b)`` tuple."""
    value = hex_color.strip()
    if value.startswith("#"):
        value = value[1:]
    if len(value) != 6:
        raise ValueError(f"expected #rrggbb hex color, got {hex_color!r}")
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        raise ValueError(f"expected #rrggbb hex color, got {hex_color!r}") from None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _bri_from_brightness(brightness: float) -> int:
    """Canonical 0..100 brightness -> WLED ``bri`` 0..255.

    Computed as ``round(brightness * 255 / 100)`` (mathematically identical
    to ``round(brightness * 2.55)`` but free of the binary float error that
    makes ``50.0 * 2.55`` land on 127.4999... -> 127 instead of 128).
    """
    return _clamp(round(brightness * 255 / 100), 0, 255)


def _sx_from_speed(speed: float) -> int:
    """Normalized 0.0..1.0 speed -> WLED effect speed ``sx`` 0..255."""
    return _clamp(round(speed * 255), 0, 255)


def _transition_from_ms(duration_ms: float) -> int:
    """Milliseconds -> WLED top-level ``transition`` (100 ms units)."""
    return _clamp(round(duration_ms / 100), 0, 65535)


def _cct_from_mirek(mirek: float) -> int:
    """Mirek -> WLED relative segment ``cct`` (0 warmest .. 255 coldest)."""
    kelvin = 1_000_000 / mirek
    span = _CCT_KELVIN_COLD - _CCT_KELVIN_WARM
    return _clamp(round((kelvin - _CCT_KELVIN_WARM) / span * 255), 0, 255)


def _name_index(name: str, catalog: list[str]) -> int | None:
    """Exact case-insensitive name lookup; list position is the provider id."""
    target = name.casefold()
    for index, entry in enumerate(catalog):
        if isinstance(entry, str) and entry.casefold() == target:
            return index
    return None


def _wled_ext(state: FixtureState) -> dict:
    ext = state.provider_ext.get(PROVIDER_WLED) if state.provider_ext else None
    return dict(ext) if isinstance(ext, dict) else {}


def _explicit_fx(ext: dict) -> int | None:
    """The escape-hatch fx index, when the state carries a real integer."""
    fx = ext.get("fx")
    if isinstance(fx, int) and not isinstance(fx, bool):
        return fx
    return None


def _wled_top_ext(ext: dict) -> dict:
    """Validated ``provider_ext.wled.top`` sub-object (top-level state keys)."""
    top = ext.get("top")
    if top is None:
        return {}
    if not isinstance(top, dict):
        raise ValueError(f"provider_ext.{PROVIDER_WLED}.top: expected an object, got {type(top).__name__}")
    unknown = sorted(set(top) - _TOP_LEVEL_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_TOP_LEVEL_KEYS))
        raise ValueError(
            f"provider_ext.{PROVIDER_WLED}.top: unknown key(s): {', '.join(unknown)} (allowed: {allowed})"
        )
    return dict(top)


def _resolved_effect_fx(state: FixtureState, fixture: Fixture) -> int | None:
    """``state.effect`` name -> fx index via the discovered effect catalog."""
    if not state.effect or fixture.capabilities is None:
        return None
    return _name_index(state.effect, fixture.capabilities.effects)


def _is_dynamic_native(fixture: Fixture) -> bool:
    return fixture.capabilities is not None and bool(fixture.capabilities.dynamic_native)


def _require_wled_binding(fixture: Fixture) -> WledBinding:
    binding = getattr(fixture, "binding", None)
    if not isinstance(binding, WledBinding):
        provider = getattr(binding, "provider", None)
        raise ValueError(
            f"fixture {getattr(fixture, 'id', '?')!r} is not bound to a WLED device "
            f"(binding provider: {provider!r}); WledRenderer requires a WledBinding"
        )
    return binding


def _resource_ref(binding: WledBinding) -> str:
    if not binding.segment_ids:
        return f"{binding.device_id}:dev"
    if len(binding.segment_ids) == 1:
        return f"{binding.device_id}:seg:{binding.segment_ids[0]}"
    joined = ",".join(str(segment) for segment in binding.segment_ids)
    return f"{binding.device_id}:seg:{joined}"


def _aggregate(fields: list[tuple[FidelityLevel, str]]) -> tuple[FidelityLevel, str]:
    """Worst field fidelity wins; reasons of that level join in field order."""
    worst = max(
        (level for level, _ in fields),
        key=lambda level: _FIDELITY_SEVERITY[level],
        default=FidelityLevel.EQUIVALENT,
    )
    reasons = [reason for level, reason in fields if level is worst and reason]
    return worst, "; ".join(reasons)


class WledRenderer:
    """Render canonical fixture state into WLED device state operations."""

    provider = PROVIDER_WLED

    def plan_fixture(
        self,
        fixture: Fixture,
        state: FixtureState,
        *,
        motion: Motion,
        palette: list[str],
    ) -> FixtureRenderPlan:
        binding = _require_wled_binding(fixture)
        if state is None:
            raise ValueError("a FixtureState is required to plan a WLED fixture")

        static_motion = motion.mode is MotionMode.STATIC or motion.strategy is MotionStrategy.STATIC
        if not static_motion and motion.mode is MotionMode.PALETTE_CYCLE:
            ext = _wled_ext(state)
            # Precedence: explicit ext numeric fx > state.effect name > none.
            fx = _explicit_fx(ext)
            if fx is None:
                fx = _resolved_effect_fx(state, fixture)
            if fx is not None and _is_dynamic_native(fixture):
                return self._plan(fixture, binding, state, motion, palette, dynamic_fx=fx)
            if motion.strategy is MotionStrategy.NATIVE_PREFERRED:
                return self._plan(
                    fixture,
                    binding,
                    state,
                    motion,
                    palette,
                    extra_field=(
                        FidelityLevel.UNSUPPORTED,
                        "palette_cycle needs a native WLED effect (a discovered effect name in the fixture state, "
                        "or provider_ext.wled.fx); native_preferred strategy forbids an approximate fallback",
                    ),
                )
            return self._plan(fixture, binding, state, motion, palette, cycle_fallback=True)
        return self._plan(fixture, binding, state, motion, palette)

    # ------------------------------------------------------------------
    # Plan construction

    def _plan(
        self,
        fixture: Fixture,
        binding: WledBinding,
        state: FixtureState,
        motion: Motion,
        palette: list[str],
        *,
        dynamic_fx: int | None = None,
        cycle_fallback: bool = False,
        extra_field: tuple[FidelityLevel, str] | None = None,
    ) -> FixtureRenderPlan:
        ext = _wled_ext(state)
        capabilities = fixture.capabilities if fixture.capabilities is not None else Capabilities()
        whole_device = not binding.segment_ids
        fields: list[tuple[FidelityLevel, str]] = []
        if extra_field is not None:
            fields.append(extra_field)

        seg: dict = {}
        top: dict = {}

        # on: device-level only for whole-device bindings, per-segment otherwise.
        if state.on is not None:
            if whole_device:
                top["on"] = bool(state.on)
                fields.append((FidelityLevel.EQUIVALENT, "whole-device on/off applied at device level"))
            else:
                seg["on"] = bool(state.on)
                fields.append((FidelityLevel.NATIVE, ""))

        # brightness: per-segment for segment bindings (top-level bri would
        # dim every device segment, bleeding into other fixtures);
        # top-level only for whole-device bindings.
        if state.brightness is not None:
            bri = _bri_from_brightness(state.brightness)
            if whole_device:
                top["bri"] = bri
                fields.append(
                    (FidelityLevel.EQUIVALENT, "whole-device brightness mapped onto all segments via device bri")
                )
            else:
                seg["bri"] = bri
                fields.append((FidelityLevel.NATIVE, ""))

        # color / gradient / palette-cycle fallback color.
        color_hex = state.color
        if state.gradient:
            color_hex = state.gradient[0]
        override_color = palette[0] if cycle_fallback and palette else None
        used_override = color_hex is None and override_color is not None
        if used_override:
            color_hex = override_color
        if color_hex:
            rgb = _hex_to_rgb(color_hex)
            seg["col"] = [list(rgb)]
            if whole_device:
                fields.append(
                    (
                        FidelityLevel.APPROXIMATE,
                        "WLED color is per-segment; device-level color reaches only the device default segment",
                    )
                )
            elif state.gradient:
                fields.append(
                    (
                        FidelityLevel.APPROXIMATE,
                        "gradient approximated with its first color; WLED has no native multi-point static gradient",
                    )
                )
            else:
                fields.append((FidelityLevel.NATIVE, ""))

        # color temperature: relative CCT via the white channel when the
        # device reports cct support; otherwise omitted.
        if state.color_temp_mirek is not None:
            if capabilities.cct:
                seg["cct"] = _cct_from_mirek(state.color_temp_mirek)
                fields.append(
                    (
                        FidelityLevel.NATIVE,
                        "color temperature mapped to relative segment cct via the white channel "
                        "(0 warmest..255 coldest)",
                    )
                )
            else:
                fields.append(
                    (
                        FidelityLevel.APPROXIMATE,
                        "color_temp_mirek omitted; WLED segments have no native color-temperature control",
                    )
                )

        # transition: top-level 100 ms units (pure unit conversion).
        if state.transition_ms is not None:
            top["transition"] = _transition_from_ms(state.transition_ms)
            fields.append(
                (
                    FidelityLevel.EQUIVALENT,
                    f"transition {state.transition_ms} ms mapped to WLED 'transition' "
                    f"{top['transition']} (100 ms units)",
                )
            )

        # palette names: ext numeric pal passes through; a pal string
        # resolves against the discovered palette catalog. A resolved name
        # is consumed (the numeric index it maps to is what ships, WLED
        # pal values are numeric).
        pal_resolved: int | None = None
        consumed_ext_keys: set[str] = set()
        ext_pal = ext.get("pal")
        if isinstance(ext_pal, str):
            consumed_ext_keys.add("pal")
            pal_resolved = _name_index(ext_pal, capabilities.palettes)
            if pal_resolved is None:
                return FixtureRenderPlan(
                    fixture_id=fixture.id,
                    provider=self.provider,
                    fidelity=FidelityLevel.UNSUPPORTED,
                    reason=(
                        f"palette {ext_pal!r} is not listed in the discovered palettes of fixture '{fixture.id}'; "
                        "no operations planned"
                    ),
                    operations=[],
                )

        # effect: explicit ext numeric fx wins, then the discovered catalog.
        explicit_fx = _explicit_fx(ext)
        resolved_fx = _resolved_effect_fx(state, fixture)
        if state.effect:
            if dynamic_fx is not None:
                via = "explicit provider_ext fx" if explicit_fx is not None else "discovered effect catalog"
                fields.append(
                    (
                        FidelityLevel.NATIVE,
                        f"effect {state.effect!r} applied as native WLED fx {dynamic_fx} via {via}",
                    )
                )
            elif explicit_fx is not None:
                fields.append(
                    (
                        FidelityLevel.NATIVE,
                        f"effect {state.effect!r} applied via explicit provider_ext fx {explicit_fx}",
                    )
                )
            elif resolved_fx is not None:
                seg["fx"] = resolved_fx
                fields.append(
                    (
                        FidelityLevel.NATIVE,
                        f"effect {state.effect!r} resolved to fx {resolved_fx} via discovered capabilities",
                    )
                )
            else:
                fields.append(
                    (
                        FidelityLevel.UNSUPPORTED,
                        f"effect {state.effect!r} cannot be resolved to a WLED fx index: not in the discovered "
                        "effect catalog and no numeric provider_ext.wled.fx set",
                    )
                )

        # Dynamic motion: native effect parameters.
        if dynamic_fx is not None:
            seg["fx"] = dynamic_fx
            if "sx" not in ext:  # explicit ext sx wins over the speed mapping
                seg["sx"] = _sx_from_speed(motion.speed)
            if not state.effect:  # effect wording above covers the named case
                fields.append((FidelityLevel.NATIVE, f"native WLED effect via explicit fx {dynamic_fx}"))
        elif cycle_fallback:
            if used_override:
                fields.append(
                    (
                        FidelityLevel.APPROXIMATE,
                        f"palette_cycle approximated with static first palette color {override_color}; "
                        "set provider_ext.wled.fx (or the fixture state effect) for a native WLED effect",
                    )
                )
            else:
                fields.append(
                    (
                        FidelityLevel.APPROXIMATE,
                        "palette_cycle cannot run natively without a resolvable fx index; "
                        "rendered as static colors",
                    )
                )

        if pal_resolved is not None:
            seg["pal"] = pal_resolved

        # provider_ext escape hatch: non-"top" keys merge into the segment
        # payload; ext wins over computed keys (consumed keys excepted).
        for key, value in ext.items():
            if key == "top" or key in consumed_ext_keys:
                continue
            seg[key] = value
        # "top" merges into the state payload after computed keys (ext wins).
        top.update(_wled_top_ext(ext))

        return self._finalize(fixture, binding, fields, top, seg)

    def _finalize(
        self,
        fixture: Fixture,
        binding: WledBinding,
        fields: list[tuple[FidelityLevel, str]],
        top: dict,
        seg: dict,
    ) -> FixtureRenderPlan:
        fidelity, reason = _aggregate(fields)

        # Stale-freeze rule (R5 plan v2 3.2): segment ops clear frz explicitly
        # so a leftover freeze from a stopped playback session can never
        # swallow a new scene. (A whole-device op with no segment content
        # cannot address segments; it keeps top-level on/bri only.)
        if seg:
            seg.setdefault("frz", False)

        segments: list[dict] = []
        if binding.segment_ids:
            segments = [dict({"id": int(segment_id)}, **seg) for segment_id in binding.segment_ids]
        elif seg:
            # Whole device: no id key; WLED applies the entry to its default segment.
            segments = [dict(seg)]

        payload = dict(top)
        if segments:
            payload["seg"] = segments

        operations: list[ProviderOperation] = []
        if payload and (top or seg):  # never emit a no-op payload (e.g. ids only)
            operations.append(
                ProviderOperation(
                    provider=self.provider,
                    op=OP_POST_STATE,
                    resource_ref=_resource_ref(binding),
                    payload=payload,
                    description=f"WLED state for fixture {fixture.id} on device {binding.device_id}",
                )
            )

        return FixtureRenderPlan(
            fixture_id=fixture.id,
            provider=self.provider,
            fidelity=fidelity,
            reason=reason,
            operations=operations,
        )
