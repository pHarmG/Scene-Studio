"""Room-level power policy for HA ``script.scene_studio_target_power``.

The Home Assistant script cannot import this module; it reimplements the
same rule in Jinja. Keep the YAML and these tests in lockstep:

- ``on`` / ``off`` pass through.
- ``toggle`` is a *room* decision, not per-entity ``light.toggle``:
  if any member is currently ``on``, turn **all** members off;
  otherwise turn **all** members on.

Unavailable / unknown / off members do not count as on.
"""

from __future__ import annotations

__all__ = ["resolve_target_power_action"]


def resolve_target_power_action(action: str, member_states: list[str] | tuple[str, ...] = ()) -> str:
    """Return the HA light service to call for every target member: ``on`` or ``off``."""
    cleaned = action.strip().lower() if isinstance(action, str) else ""
    if cleaned in ("on", "off"):
        return cleaned
    if cleaned != "toggle":
        raise ValueError("action must be on, off, or toggle")
    any_on = any(isinstance(state, str) and state.strip().lower() == "on" for state in member_states)
    return "off" if any_on else "on"
