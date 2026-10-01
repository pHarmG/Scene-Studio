"""Read-only HA-area / Scene-Studio-group membership drift report.

Answers, per fixture:

    SS fixture -> HA entity -> observed HA/provider room -> SS primary group
    -> projected HA target(s)

and flags room mismatch, aggregate helpers modeled as fixtures, missing HA
coverage, duplicate HA entities, and empty/stale Scene Studio targets.

HA convenience groups never define Scene Studio membership. Canonical
groups feed the HA target projection, never the reverse.
"""

from __future__ import annotations

from ..domain.bindings import binding_ha_entity_ids
from ..domain.fixtures import (
    BROAD_GROUP_ID,
    FixtureRegistry,
    is_render_skipped,
    project_target_membership,
)
from .aggregates import is_aggregate_observation

__all__ = ["membership_drift_report"]


def _primary_group(groups: list[str]) -> str | None:
    for group in groups:
        if group != BROAD_GROUP_ID:
            return group
    return None


def membership_drift_report(
    registry: FixtureRegistry,
    observations: list | None = None,
    *,
    scenes: list | None = None,
) -> dict:
    """Deterministic drift report. Never mutates the registry or contacts a provider."""
    observations = list(observations or [])
    obs_by_resource: dict[str, object] = {}
    for observation in observations:
        provider = getattr(observation, "provider", None)
        resource = getattr(observation, "provider_resource_id", None)
        if isinstance(provider, str) and isinstance(resource, str):
            obs_by_resource[f"{provider}:{resource}"] = observation

    targets = project_target_membership(registry)
    targets_by_id = {item["id"]: item for item in targets}
    entity_owners: dict[str, list[str]] = {}
    rows: list[dict] = []

    for fixture in sorted(registry.fixtures, key=lambda item: item.id):
        entity_ids = binding_ha_entity_ids(fixture.binding)
        for entity_id in entity_ids:
            entity_owners.setdefault(entity_id, []).append(fixture.id)
        primary = _primary_group(list(fixture.groups))
        observation = None
        binding = fixture.binding
        if binding is not None:
            if getattr(binding, "provider", None) == "ha_light" and entity_ids:
                observation = obs_by_resource.get(f"ha_light:{entity_ids[0]}")
            elif getattr(binding, "provider", None) == "hue_v2":
                observation = obs_by_resource.get(f"hue_v2:{binding.resource_id}")
        observed_room = getattr(observation, "location_hint", None) if observation is not None else None
        flags: list[str] = []
        if observation is not None and is_aggregate_observation(observation):
            flags.append("aggregate_helper_as_fixture")
        if (
            observed_room
            and primary
            and observed_room.strip().lower()
            != next((target.name.strip().lower() for target in registry.targets if target.id == primary), primary)
        ):
            flags.append("room_mismatch")
        projected_targets = [
            target["id"] for target in targets if fixture.id in target["fixture_ids"]
        ]
        rows.append(
            {
                "fixture_id": fixture.id,
                "ha_entity_ids": entity_ids,
                "observed_room": observed_room,
                "primary_group": primary,
                "groups": list(fixture.groups),
                "projected_targets": projected_targets,
                "render_skipped": is_render_skipped(fixture),
                "flags": flags,
            }
        )

    duplicate_entities = {
        entity_id: owners
        for entity_id, owners in sorted(entity_owners.items())
        if len(owners) > 1
    }
    empty_targets = [
        target["id"] for target in targets if target["enabled_fixture_count"] == 0
    ]
    incomplete_targets = [
        {
            "id": target["id"],
            "enabled_fixture_count": target["enabled_fixture_count"],
            "ha_covered_fixture_count": target["ha_covered_fixture_count"],
        }
        for target in targets
        if not target["complete_ha_coverage"]
    ]
    scene_refs = []
    for scene in scenes or []:
        target_ids = list(getattr(scene, "target_ids", None) or scene.get("target_ids") or [])
        if any(target_id in empty_targets for target_id in target_ids):
            scene_id = getattr(scene, "id", None) or scene.get("id")
            scene_refs.append({"scene_id": scene_id, "target_ids": target_ids})

    flags_present = sorted({flag for row in rows for flag in row["flags"]})
    if duplicate_entities:
        flags_present.append("duplicate_ha_entity")
    if empty_targets:
        flags_present.append("empty_stale_target")
    if incomplete_targets:
        flags_present.append("incomplete_ha_coverage")

    return {
        "rows": rows,
        "targets": targets,
        "duplicate_ha_entities": duplicate_entities,
        "empty_targets": empty_targets,
        "incomplete_targets": incomplete_targets,
        "scenes_targeting_empty": scene_refs,
        "flags": flags_present,
    }
