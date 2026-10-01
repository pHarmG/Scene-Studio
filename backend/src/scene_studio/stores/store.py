"""SceneStudioStore — facade wiring the fixture registry and scene store (Wave A1).

Owns the cross-store rules that neither store can enforce alone:

- `remove_fixture` is BLOCKED (raises ConflictError) while any *active*
  scene's `fixture_states` references the fixture. This is the chosen
  referential policy for Workstream A1 (of the two allowed options —
  block or report): removal is refused so active scenes can never go
  silently stale. Archived scenes do not block; they preserve history,
  and restoring into a registry without the fixture is safe because
  resolution is fixture-driven (ARCHITECTURE_CONTRACTS §3) and unknown
  `fixture_states` keys are inert.
- `resolve_target` delegates to the fixture store (membership lives in
  `fixture.groups`; contracts §1).
"""

from __future__ import annotations

from pathlib import Path

from .errors import ConflictError
from .fixtures import FixtureStore
from .scenes import SceneStore

__all__ = ["SceneStudioStore"]


class SceneStudioStore:
    """Single entry point over `<root>/registry`, `<root>/scenes`, `<root>/scenes_archive`."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.fixtures = FixtureStore(self.root)
        self.scenes = SceneStore(self.root)

    def reload(self) -> None:
        """Re-read all persisted documents from disk."""
        self.fixtures.reload()
        self.scenes.reload()

    def resolve_target(self, target_id: str):
        """Resolve a target id (or single fixture id) to fixtures — see FixtureStore."""
        return self.fixtures.resolve_target(target_id)

    def remove_fixture(self, fixture_id: str) -> None:
        """Remove a fixture unless an active scene still references it."""
        referencing = self.scenes.scenes_referencing_fixture(fixture_id)
        if referencing:
            raise ConflictError(
                f"fixture {fixture_id!r} is referenced by active scene(s) "
                f"{', '.join(referencing)}; archive the scene(s) or update their "
                "fixture_states first"
            )
        self.fixtures.remove_fixture(fixture_id)
