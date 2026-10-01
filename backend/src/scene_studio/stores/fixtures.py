"""FixtureStore — persistent fixture registry and target resolution (Wave A1).

Storage layout (single registry document; the registry is the small,
stable identity seed for the whole lighting stack):

    <root>/registry/registry.json     # FixtureRegistry document (schema_version 1)

Design policy (documented for reviewers):

- The registry document holds fixtures AND targets. Targets are display
  definitions only — membership lives in `fixture.groups`
  (ARCHITECTURE_CONTRACTS §1); there is no separate target-membership
  storage anywhere in the store layer.
- Every mutation is validated through the strict domain `from_dict`
  constructors and persisted immediately with an atomic temp+`os.replace`
  write. The in-memory registry is only swapped after the write succeeds,
  so a failed write can never leave the store diverged from disk.
- Saves are canonical: fixtures and targets sorted by id, stable JSON key
  order (see `stores.atomic.dump_canonical_json`). The registry `updated`
  timestamp is preserved exactly as loaded and is never auto-stamped —
  timestamp policy belongs to callers. This keeps two stores holding the
  same content byte-identical on disk.
- Group ids are NOT required to be declared targets: `fixture.groups`
  drives membership even when no Target display entry exists. Renaming a
  fixture changes `name` only and never its id (contracts §1: ids are
  immutable, so storage identity never moves).
- `resolve_target` accepts a declared `Target.id` or a single `fixture.id`
  (contracts §1). On the (discouraged) case of one id being both, the
  declared target wins. The result lists every member regardless of
  health — skipping disabled/missing fixtures is apply/plan semantics
  (contracts §3.4), not store semantics.
- All publicly returned models are detached copies; mutate through the
  store methods so changes persist.
- Cross-store rules (removing a fixture referenced by an active scene)
  live on `SceneStudioStore`, which owns both stores; a standalone
  `FixtureStore` has no scene knowledge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..domain.bindings import Binding, HaLightBinding, HueBinding, WledBinding, binding_from_dict
from ..domain.capabilities import Capabilities
from ..domain.contention import parse_contention_policy
from ..domain.fixtures import (
    BindingRevision, CapabilityAssessment, DeviceProfile, Fixture,
    FixtureRegistry, HealthStatus, Target,
)
from ..domain.identities import validate_id
from ..domain.serde import ValidationError
from .atomic import atomic_write_json, read_json
from .errors import ConflictError, CorruptStoreError, NotFoundError

__all__ = ["FixtureStore"]


# ---------------------------------------------------------------------------
# normalization / copy helpers (all validation flows through domain from_dict)
# ---------------------------------------------------------------------------

def _copy_fixture(fixture: Fixture) -> Fixture:
    return Fixture.from_dict(fixture.to_dict())


def _copy_target(target: Target) -> Target:
    return Target.from_dict(target.to_dict())


def _normalize_fixture(fixture: Fixture | dict) -> Fixture:
    if isinstance(fixture, Fixture):
        return Fixture.from_dict(fixture.to_dict())
    return Fixture.from_dict(fixture)


def _normalize_target(target: Target | dict) -> Target:
    if isinstance(target, Target):
        return Target.from_dict(target.to_dict())
    return Target.from_dict(target)


def _normalize_binding(binding: Binding | dict) -> Binding:
    """Parse/validate through the tagged binding union (contracts §2)."""
    if isinstance(binding, (HueBinding, WledBinding, HaLightBinding)):
        return binding_from_dict(binding.to_dict())
    return binding_from_dict(binding)


def _normalize_capabilities(capabilities: Capabilities | dict | None) -> Capabilities | None:
    if capabilities is None:
        return None
    if isinstance(capabilities, Capabilities):
        return Capabilities.from_dict(capabilities.to_dict())
    return Capabilities.from_dict(capabilities)


def _normalize_health(health: HealthStatus | str | None) -> HealthStatus | None:
    if health is None or isinstance(health, HealthStatus):
        return health
    if isinstance(health, str):
        try:
            return HealthStatus(health)
        except ValueError:
            allowed = ", ".join(status.value for status in HealthStatus)
            raise ValidationError("fixture.health", f"must be one of: {allowed}") from None
    raise ValidationError("fixture.health", "expected HealthStatus, status string, or None")


def _normalize_profile(profile: DeviceProfile | dict | None) -> DeviceProfile | None:
    if profile is None or isinstance(profile, DeviceProfile):
        return DeviceProfile.from_dict(profile.to_dict()) if profile is not None else None
    return DeviceProfile.from_dict(profile)


def _normalize_assessment(assessment: CapabilityAssessment | dict | None) -> CapabilityAssessment | None:
    if assessment is None or isinstance(assessment, CapabilityAssessment):
        return CapabilityAssessment.from_dict(assessment.to_dict()) if assessment is not None else None
    return CapabilityAssessment.from_dict(assessment)


class FixtureStore:
    """Persists the fixture registry document under `<root>/registry/registry.json`."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.path = self.root / "registry" / "registry.json"
        self._legacy_source: dict | None = None
        self._registry = self._load_from_disk()

    # ------------------------------------------------------------------
    # document access
    # ------------------------------------------------------------------

    def _load_from_disk(self) -> FixtureRegistry:
        if not self.path.exists():
            return FixtureRegistry()
        data = read_json(self.path)
        self._legacy_source = dict(data) if isinstance(data, dict) and data.get("schema_version") == 1 else None
        try:
            return FixtureRegistry.from_dict(data, "registry")
        except ValidationError as exc:
            raise CorruptStoreError(self.path, f"registry failed validation: {exc}") from exc

    def registry(self) -> FixtureRegistry:
        """A detached copy of the current registry document."""
        return FixtureRegistry.from_dict(self._registry.to_dict())

    def reload(self) -> None:
        """Re-read the registry from disk (last good file wins)."""
        self._registry = self._load_from_disk()

    def _commit(self, registry: FixtureRegistry) -> None:
        """Canonicalize (sort by id) and persist; memory updates only on success."""
        if self._legacy_source is not None:
            raise ConflictError(
                "registry schema v1 is loaded read-only; run the explicit registry migration before mutating it"
            )
        canonical = FixtureRegistry(
            fixtures=sorted(registry.fixtures, key=lambda fixture: fixture.id),
            targets=sorted(registry.targets, key=lambda target: target.id),
            updated=registry.updated,
            schema_version=registry.schema_version,
        )
        atomic_write_json(self.path, canonical.to_dict())
        self._registry = canonical

    def inspect_schema_migration(self) -> dict:
        """Read-only deterministic v1 -> v2 migration preview."""
        return {
            "required": self._legacy_source is not None,
            "from_schema_version": 1 if self._legacy_source is not None else self._registry.schema_version,
            "to_schema_version": FixtureRegistry().schema_version,
            "registry": self.registry().to_dict(),
        }

    def migrate_loaded_legacy_registry(self) -> dict:
        """Persist a loaded v1 registry only through this explicit boundary.

        The original bytes are preserved as a deterministic sibling backup,
        then the migrated document is atomically written, validated, and
        read back before mutations are unblocked.
        """
        if self._legacy_source is None:
            return {
                "noop": True,
                "reason": "already current",
                "from_schema_version": self._registry.schema_version,
                "to_schema_version": FixtureRegistry().schema_version,
                "registry": self.registry().to_dict(),
            }
        backup_path = self.path.with_name(f"{self.path.stem}.v1.backup.json")
        atomic_write_json(backup_path, self._legacy_source)
        canonical = FixtureRegistry(
            fixtures=sorted(self._registry.fixtures, key=lambda fixture: fixture.id),
            targets=sorted(self._registry.targets, key=lambda target: target.id),
            updated=self._registry.updated,
            schema_version=self._registry.schema_version,
        )
        atomic_write_json(self.path, canonical.to_dict())
        try:
            reread = FixtureRegistry.from_dict(read_json(self.path), "registry")
        except Exception as exc:
            raise CorruptStoreError(self.path, f"migrated registry failed read-back validation: {exc}") from exc
        if reread.to_dict() != canonical.to_dict():
            raise CorruptStoreError(self.path, "migrated registry changed during read-back validation")
        self._registry = reread
        self._legacy_source = None
        return {
            "noop": False,
            "backup_path": str(backup_path),
            "from_schema_version": 1,
            "to_schema_version": reread.schema_version,
            "registry": self.registry().to_dict(),
        }

    # ------------------------------------------------------------------
    # fixture semantics
    # ------------------------------------------------------------------

    def _find_fixture(self, fixture_id: str) -> Fixture | None:
        for fixture in self._registry.fixtures:
            if fixture.id == fixture_id:
                return fixture
        return None

    def _require_fixture(self, fixture_id: str) -> Fixture:
        fixture = self._find_fixture(fixture_id)
        if fixture is None:
            raise NotFoundError("fixture", fixture_id)
        return fixture

    def get_fixture(self, fixture_id: str) -> Fixture:
        """The fixture with `fixture_id` (detached copy); NotFoundError otherwise."""
        return _copy_fixture(self._require_fixture(fixture_id))

    def list_fixtures(self) -> list[Fixture]:
        """All fixtures in stable id order (deterministic catalog output)."""
        return [_copy_fixture(fixture) for fixture in sorted(self._registry.fixtures, key=lambda f: f.id)]

    def add_fixture(self, fixture: Fixture | dict) -> Fixture:
        """Add a fixture (dict or model; strictly validated). Duplicate ids conflict."""
        validated = _normalize_fixture(fixture)
        if self._find_fixture(validated.id) is not None:
            raise ConflictError(f"fixture id already exists: {validated.id!r}")
        self._commit(
            FixtureRegistry(
                fixtures=[*self._registry.fixtures, validated],
                targets=self._registry.targets,
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_fixture(validated)

    def remove_fixture(self, fixture_id: str) -> None:
        """Remove a fixture. Scene references are checked by `SceneStudioStore`."""
        self._require_fixture(fixture_id)
        self._commit(
            FixtureRegistry(
                fixtures=[fixture for fixture in self._registry.fixtures if fixture.id != fixture_id],
                targets=self._registry.targets,
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )

    def _mutate_fixture(self, fixture_id: str, mutate: Callable[[Fixture], Fixture]) -> Fixture:
        current = self._require_fixture(fixture_id)
        updated = mutate(_copy_fixture(current))
        validated = Fixture.from_dict(updated.to_dict())
        self._commit(
            FixtureRegistry(
                fixtures=[
                    validated if fixture.id == fixture_id else fixture
                    for fixture in self._registry.fixtures
                ],
                targets=self._registry.targets,
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_fixture(validated)

    def rename_fixture(self, fixture_id: str, new_name: str) -> Fixture:
        """Change the display label only; id and storage identity never move."""

        def mutate(fixture: Fixture) -> Fixture:
            fixture.name = new_name
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def set_enabled(self, fixture_id: str, enabled: bool) -> Fixture:
        """Enable/disable; derived health flips per `derive_health` (contracts §1)."""
        if not isinstance(enabled, bool):
            raise ValidationError("fixture.enabled", "expected a boolean")

        def mutate(fixture: Fixture) -> Fixture:
            fixture.enabled = enabled
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def enable(self, fixture_id: str) -> Fixture:
        return self.set_enabled(fixture_id, True)

    def disable(self, fixture_id: str) -> Fixture:
        return self.set_enabled(fixture_id, False)

    def set_contention_policy(self, fixture_id: str, policy: str | None) -> Fixture:
        """Set/clear the durable external light-sync (hyperHDR) policy.

        ``None`` clears the fixture-level override so the target/engine
        default applies again (the ``"default"`` command value maps here).
        """
        normalized = parse_contention_policy(policy, "fixture.contention_policy")

        def mutate(fixture: Fixture) -> Fixture:
            fixture.contention_policy = normalized
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def set_health(self, fixture_id: str, health: HealthStatus | str | None) -> Fixture:
        """Set/clear the explicit health override (override wins per contracts §1)."""
        normalized = _normalize_health(health)

        def mutate(fixture: Fixture) -> Fixture:
            fixture.health = normalized
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def set_location(self, fixture_id: str, location: str | None) -> Fixture:
        if location is not None and not isinstance(location, str):
            raise ValidationError("fixture.location", "expected a string or None")

        def mutate(fixture: Fixture) -> Fixture:
            fixture.location = location
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def set_groups(self, fixture_id: str, groups: list[str]) -> Fixture:
        """Replace target/group membership (the authoritative side, contracts §1)."""
        if not isinstance(groups, list):
            raise ValidationError("fixture.groups", "expected a list of target/group ids")

        def mutate(fixture: Fixture) -> Fixture:
            fixture.groups = list(groups)
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def update_capabilities(self, fixture_id: str, capabilities: Capabilities | dict | None) -> Fixture:
        """Replace or clear (None) the normalized capability set."""
        normalized = _normalize_capabilities(capabilities)

        def mutate(fixture: Fixture) -> Fixture:
            fixture.capabilities = normalized
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def bind(self, fixture_id: str, binding: Binding | dict) -> Fixture:
        """Replace the binding (rebind). The binding must parse per contracts §2."""
        parsed = _normalize_binding(binding)

        def mutate(fixture: Fixture) -> Fixture:
            fixture.binding = parsed
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def reconcile_binding(
        self,
        fixture_id: str,
        binding: Binding | dict,
        effective_capabilities: Capabilities | dict,
        *,
        capability_assessment: CapabilityAssessment | dict | None,
        device_profile: DeviceProfile | dict | None = None,
        health: HealthStatus | str | None = None,
        changed_at: str,
        observation_id: str | None = None,
        reason: str | None = None,
        groups: list[str] | None = None,
    ) -> Fixture:
        """Atomically commit a complete provider binding revision.

        The history entry is the *previous* coherent revision.  The one
        mutation/commit is intentional: failed writes leave the old registry
        untouched and cannot retain capabilities from a former provider.

        ``groups`` (optional) replaces target/group membership in the SAME
        atomic mutation, for the room-membership-drift case surfaced
        alongside capability drift (a fixture whose fresh discovery
        observation reports a different physical room than its current
        group membership). ``None`` (default) leaves ``groups`` untouched —
        every existing caller that doesn't pass it keeps prior behavior
        exactly.
        """
        parsed = _normalize_binding(binding)
        caps = _normalize_capabilities(effective_capabilities)
        if caps is None:
            raise ValidationError("fixture.capabilities", "effective capabilities are required for a rebind")
        assessment = _normalize_assessment(capability_assessment)
        profile = _normalize_profile(device_profile)
        normalized_health = _normalize_health(health)

        def mutate(fixture: Fixture) -> Fixture:
            previous = BindingRevision(
                changed_at=changed_at,
                binding=fixture.binding,
                capabilities=fixture.capabilities,
                capability_assessment=fixture.capability_assessment,
                device_profile=fixture.device_profile,
                health=fixture.health,
                observation_id=(fixture.capability_assessment.observation_id if fixture.capability_assessment else None),
                reason=reason,
            )
            fixture.binding_history = ([previous] + fixture.binding_history)[:8]
            fixture.binding = parsed
            fixture.capabilities = caps
            fixture.capability_assessment = assessment
            if profile is not None:
                fixture.device_profile = profile
            fixture.health = normalized_health
            if groups is not None:
                fixture.groups = list(groups)
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    def rollback_binding(self, fixture_id: str, *, changed_at: str) -> Fixture:
        """Atomically restore the most recent complete binding revision."""
        def mutate(fixture: Fixture) -> Fixture:
            if not fixture.binding_history:
                raise ConflictError(f"fixture '{fixture_id}' has no binding revision to roll back")
            previous = fixture.binding_history.pop(0)
            current = BindingRevision(
                changed_at=changed_at,
                binding=fixture.binding,
                capabilities=fixture.capabilities,
                capability_assessment=fixture.capability_assessment,
                device_profile=fixture.device_profile,
                health=fixture.health,
                observation_id=(fixture.capability_assessment.observation_id if fixture.capability_assessment else None),
                reason="rollback backup",
            )
            fixture.binding_history = ([current] + fixture.binding_history)[:8]
            fixture.binding = previous.binding
            fixture.capabilities = previous.capabilities
            fixture.capability_assessment = previous.capability_assessment
            fixture.device_profile = previous.device_profile
            fixture.health = previous.health
            return fixture
        return self._mutate_fixture(fixture_id, mutate)

    def unbind(self, fixture_id: str) -> Fixture:
        """Clear the binding (fixture derives to `unbound` health)."""

        def mutate(fixture: Fixture) -> Fixture:
            fixture.binding = None
            return fixture

        return self._mutate_fixture(fixture_id, mutate)

    # ------------------------------------------------------------------
    # target display definitions
    # ------------------------------------------------------------------

    def _find_target(self, target_id: str) -> Target | None:
        for target in self._registry.targets:
            if target.id == target_id:
                return target
        return None

    def _require_target(self, target_id: str) -> Target:
        target = self._find_target(target_id)
        if target is None:
            raise NotFoundError("target", target_id)
        return target

    def get_target(self, target_id: str) -> Target:
        return _copy_target(self._require_target(target_id))

    def list_targets(self) -> list[Target]:
        return [_copy_target(target) for target in sorted(self._registry.targets, key=lambda t: t.id)]

    def add_target(self, target: Target | dict) -> Target:
        validated = _normalize_target(target)
        if self._find_target(validated.id) is not None:
            raise ConflictError(f"target id already exists: {validated.id!r}")
        self._commit(
            FixtureRegistry(
                fixtures=self._registry.fixtures,
                targets=[*self._registry.targets, validated],
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_target(validated)

    def remove_target(self, target_id: str) -> None:
        self._require_target(target_id)
        self._commit(
            FixtureRegistry(
                fixtures=self._registry.fixtures,
                targets=[target for target in self._registry.targets if target.id != target_id],
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )

    def rename_target(self, target_id: str, new_name: str) -> Target:

        def mutate(target: Target) -> Target:
            target.name = new_name
            return target

        current = self._require_target(target_id)
        validated = Target.from_dict(mutate(_copy_target(current)).to_dict())
        self._commit(
            FixtureRegistry(
                fixtures=self._registry.fixtures,
                targets=[
                    validated if target.id == target_id else target
                    for target in self._registry.targets
                ],
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_target(validated)

    # -- atomic bootstrap operations (portability pass) --------------------
    #
    # A failed bootstrap command must leave the registry semantically
    # unchanged, so target creation/update + membership deltas commit ONE
    # canonical registry document after the WHOLE request is validated.

    @staticmethod
    def _validated_membership_delta(
        fixtures: list[Fixture],
        target_id: str,
        add_fixture_ids: list[str],
        remove_fixture_ids: list[str],
    ) -> dict[str, list[str]]:
        """Validate a membership request against the current fixtures.

        Returns ``{fixture_id: new_groups}`` for every fixture whose groups
        change. Raises NotFoundError for ANY unknown id before any mutation,
        so the caller can reject the whole request atomically.
        """
        known = {fixture.id for fixture in fixtures}
        for fixture_id in [*add_fixture_ids, *remove_fixture_ids]:
            if fixture_id not in known:
                raise NotFoundError("fixture", fixture_id)
        planned: dict[str, list[str]] = {}
        for fixture_id in add_fixture_ids:
            fixture = next(item for item in fixtures if item.id == fixture_id)
            groups = list(fixture.groups)
            if target_id not in groups:
                groups.append(target_id)
                planned[fixture_id] = groups
        for fixture_id in remove_fixture_ids:
            if fixture_id in planned:
                planned.pop(fixture_id)
                continue
            fixture = next(item for item in fixtures if item.id == fixture_id)
            groups = list(fixture.groups)
            if target_id in groups:
                groups.remove(target_id)
                planned[fixture_id] = groups
        return planned

    def create_target_with_members(self, target: Target | dict, fixture_ids: list[str]) -> Target:
        """Declare a target and assign initial membership in ONE atomic commit.

        The complete request is validated first: a duplicate target id or ANY
        unknown fixture id fails with the registry byte-for-byte unchanged.
        Membership lands on the authoritative side (`fixture.groups`).
        """
        if not isinstance(fixture_ids, list) or not all(isinstance(item, str) for item in fixture_ids):
            raise ValidationError("target.create fixture_ids", "expected a list of fixture ids")
        validated_target = _normalize_target(target)
        if self._find_target(validated_target.id) is not None:
            raise ConflictError(f"target id already exists: {validated_target.id!r}")
        planned = self._validated_membership_delta(self._registry.fixtures, validated_target.id, fixture_ids, [])

        updated_fixtures = [
            _copy_fixture(fixture)
            for fixture in self._registry.fixtures
        ]
        for fixture in updated_fixtures:
            if fixture.id in planned:
                fixture.groups = planned[fixture.id]
        validated_fixtures = [Fixture.from_dict(fixture.to_dict()) for fixture in updated_fixtures]
        self._commit(
            FixtureRegistry(
                fixtures=validated_fixtures,
                targets=[*self._registry.targets, validated_target],
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_target(validated_target)

    def update_target_definition(
        self,
        target_id: str,
        new_name: str | None,
        add_fixture_ids: list[str],
        remove_fixture_ids: list[str],
    ) -> Target:
        """Rename a target and/or apply a membership delta in ONE atomic commit.

        The target must exist and EVERY requested fixture id must be known
        before anything is written; a failing request changes nothing (idempotent
        removes that target no member fixtures stay no-ops).
        """
        if not isinstance(add_fixture_ids, list) or not all(isinstance(item, str) for item in add_fixture_ids):
            raise ValidationError("target.update add_fixture_ids", "expected a list of fixture ids")
        if not isinstance(remove_fixture_ids, list) or not all(isinstance(item, str) for item in remove_fixture_ids):
            raise ValidationError("target.update remove_fixture_ids", "expected a list of fixture ids")
        current = self._require_target(target_id)
        planned = self._validated_membership_delta(self._registry.fixtures, target_id, add_fixture_ids, remove_fixture_ids)

        validated_target = _copy_target(current)
        if new_name is not None:
            validated_target.name = new_name
        validated_target = Target.from_dict(validated_target.to_dict())

        updated_fixtures = [_copy_fixture(fixture) for fixture in self._registry.fixtures]
        for fixture in updated_fixtures:
            if fixture.id in planned:
                fixture.groups = planned[fixture.id]
        validated_fixtures = [Fixture.from_dict(fixture.to_dict()) for fixture in updated_fixtures]
        self._commit(
            FixtureRegistry(
                fixtures=validated_fixtures,
                targets=[
                    validated_target if target.id == target_id else target
                    for target in self._registry.targets
                ],
                updated=self._registry.updated,
                schema_version=self._registry.schema_version,
            )
        )
        return _copy_target(validated_target)

    # ------------------------------------------------------------------
    # target resolution (no separate membership storage — contracts §1)
    # ------------------------------------------------------------------

    def resolve_target(self, target_id: str) -> list[Fixture]:
        """Resolve a command target id to its fixtures in stable id order.

        `target_id` is either a declared `Target.id` (resolves to every
        fixture whose `groups` contains it) or a single `fixture.id`
        (resolves to that one fixture). Declared targets win on id
        collision. Unknown ids raise NotFoundError; malformed ids raise
        domain ValidationError.
        """
        validate_id(target_id, "target_id", "resolve_target")
        if self._find_target(target_id) is not None:
            members: dict[str, Fixture] = {}
            for fixture in self._registry.fixtures:
                if target_id in fixture.groups and fixture.id not in members:
                    members[fixture.id] = fixture
            return [_copy_fixture(members[member_id]) for member_id in sorted(members)]
        fixture = self._find_fixture(target_id)
        if fixture is not None:
            return [_copy_fixture(fixture)]
        raise NotFoundError("target", target_id, detail="not a declared target id or a fixture id")
