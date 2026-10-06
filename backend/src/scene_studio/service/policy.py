"""Runtime policy — the single backend-owned source of command permissions.

The engine owns the policy: the adapter derives it from its configuration
flags and hands it in at construction, ``status()`` exposes it to clients,
and ``handle_result`` enforces it before dispatch. Frontends only read
``status().runtime`` — they never re-implement command rules.

Modes:

- ``normal``: everything allowed.
- ``read_only``: reads, preview/dry-run, discovery, observational retry,
  and diagnostics only. Persistent mutations are rejected with ``conflict``.
- ``registry_admin``: registry/configuration mutations allowed; provider
  writes, real ``scene.apply``, and playback stay blocked. The provider
  executor must independently refuse writes in this mode. Scene authoring
  (``scene.create``/``scene.update``) is a catalog mutation and is
  permitted here alongside the existing scene save/rename/archive/restore.
  First-run bootstrap (``fixture.adopt``/``target.create``/
  ``target.update``) is a registry mutation and is permitted here so an
  empty install can build its registry without provider access.
- ``r2_validation``: like read_only but ``scene.apply``/``fixture.identify``
  (real provider execution) are permitted for executor-allowlisted fixtures;
  registry/playback mutations stay locked.
- ``r5_validation``: like r2 plus playback lifecycle commands.

The dry-run exception is encoded as the sentinel entry
``"scene.apply:dry_run"``: it grants dry-run ``scene.apply`` without
granting real execution.

Routine commands (``routine.create/update/delete/enable/disable`` — native
HA automation CRUD, routines pass) are intentionally absent from every
restricted set: normal mode derives them from the command catalog, while
``read_only`` (observational) and ``registry_admin`` (external-system
writes blocked, exactly like provider writes) reject them. The HA card's
UI bridge allowlist independently excludes them forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.commands import COMMAND_CATALOG

__all__ = [
    "MODE_NORMAL",
    "MODE_R2_VALIDATION",
    "MODE_R5_VALIDATION",
    "MODE_READ_ONLY",
    "MODE_REGISTRY_ADMIN",
    "RuntimePolicy",
]

MODE_NORMAL = "normal"
MODE_READ_ONLY = "read_only"
MODE_REGISTRY_ADMIN = "registry_admin"
MODE_R2_VALIDATION = "r2_validation"
MODE_R5_VALIDATION = "r5_validation"

_DRY_RUN_SENTINEL = "scene.apply:dry_run"

_READONLY_BASE = frozenset({
    "scene.preview",
    "scene.preview_draft",  # Builder: unsaved-draft render is observational, never persists
    _DRY_RUN_SENTINEL,
    "discovery.run",
    "fixture.retry",
    "fixture.rebind_preview",
    "fixture.reconcile_preview",
    "registry.migration_preview",
    "diagnostics.export",
})

_REGISTRY_ADMIN = frozenset({
    "fixture.enable",
    "fixture.disable",
    "fixture.set_contention_policy",  # registry mutation: external-sync policy
    "fixture.retry",
    "fixture.reconcile",
    "fixture.reconcile_preview",
    "fixture.rebind",
    "fixture.rebind_preview",
    "fixture.rebind_rollback",
    "fixture.adopt",  # first-run bootstrap: fixture from a discovery observation (no provider write)
    "target.create",  # first-run bootstrap: declare a semantic target
    "target.update",  # narrow target rename/membership administration
    "registry.migration_preview",
    "registry.migrate",
    "scene.rename",
    "scene.archive",
    "scene.restore",
    "scene.save",
    "scene.create",  # Builder authoring: catalog mutation, provider writes stay blocked
    "scene.update",
}) | _READONLY_BASE

_R2_EXTRA = frozenset({"scene.apply", "fixture.identify"}) | _READONLY_BASE

# R5 validation: playback lifecycle commands + scene.apply (executor
# allowlist remains the write boundary) + the read-only base.
# scene.play_draft is included because Builder Preview plays unsaved
# drafts through apply (static) or playback (dynamic). The sync suspend/
# resume pair is playback-adjacent provider control (hyperHDR instance
# stop/start) and stays out of the validation modes.
_R5_EXTRA = frozenset({
    "playback.start", "playback.pause", "playback.resume", "playback.stop",
    "scene.apply", "scene.play_draft", "fixture.identify",
}) | _READONLY_BASE

# Normal mode derives from the canonical catalog so a new command is
# automatically permitted there (restricted modes stay explicit).
_ALL_COMMANDS = frozenset(COMMAND_CATALOG)


@dataclass(frozen=True)
class RuntimePolicy:
    """Immutable command-permission policy (see module docstring)."""

    mode: str = MODE_NORMAL
    allowed_commands: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(cls, mode: str = MODE_NORMAL) -> "RuntimePolicy":
        if mode == MODE_READ_ONLY:
            return cls(mode=mode, allowed_commands=_READONLY_BASE)
        if mode == MODE_REGISTRY_ADMIN:
            return cls(mode=mode, allowed_commands=_REGISTRY_ADMIN)
        if mode == MODE_R2_VALIDATION:
            return cls(mode=mode, allowed_commands=_R2_EXTRA)
        if mode == MODE_R5_VALIDATION:
            return cls(mode=mode, allowed_commands=_R5_EXTRA)
        if mode == MODE_NORMAL:
            return cls(mode=mode, allowed_commands=_ALL_COMMANDS)
        raise ValueError(f"unknown runtime mode {mode!r}")

    @property
    def read_only(self) -> bool:
        return self.mode == MODE_READ_ONLY

    @property
    def provider_writes_blocked(self) -> bool:
        """True when this mode must never execute provider operations."""
        return self.mode in {MODE_READ_ONLY, MODE_REGISTRY_ADMIN}

    def allows(self, command: str, *, dry_run: bool = False) -> bool:
        """Whether the engine may dispatch ``command`` with the given params."""
        if command in self.allowed_commands:
            return True
        if command == "scene.apply" and dry_run and _DRY_RUN_SENTINEL in self.allowed_commands:
            return True
        return False

    def status_view(self) -> dict:
        """Client-facing snapshot for ``status().runtime``."""
        return {
            "mode": self.mode,
            "read_only": self.read_only,
            "provider_writes_blocked": self.provider_writes_blocked,
            "allowed_commands": sorted(self.allowed_commands),
        }
