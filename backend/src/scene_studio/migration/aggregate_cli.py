"""CLI for dry-run-first aggregate HA-helper fixture removal.

Usage::

    python -m scene_studio.migration.aggregate_cli STORE_ROOT --fixture-id office_lights
    python -m scene_studio.migration.aggregate_cli STORE_ROOT --fixture-id office_lights --apply

`--apply` rewrites scene documents and the
registry **only when** ``safe_to_apply`` is true; without ``--apply`` the
plan is printed and the store is untouched. An unsafe plan always
refuses before any write.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scene_studio.domain.sanitize import sanitize_tree
from scene_studio.stores import SceneStudioStore

from .aggregate_fixture import apply_aggregate_fixture_plan, plan_aggregate_fixture_removal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store_root", type=Path, help="Scene Studio store root")
    parser.add_argument("--fixture-id", required=True, help="Aggregate fixture id to remove")
    parser.add_argument(
        "--ha-member",
        action="append",
        dest="ha_members",
        default=[],
        help="HA light entity id that is a member of the aggregate (repeatable)",
    )
    parser.add_argument(
        "--child-id",
        action="append",
        dest="child_ids",
        default=[],
        help="Explicit child fixture id (repeatable; overrides HA member mapping)",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the plan (default is dry-run)")
    parser.add_argument("--out", type=Path, help="Write the JSON plan to this path")
    args = parser.parse_args(argv)

    store = SceneStudioStore(args.store_root)
    plan = plan_aggregate_fixture_removal(
        store,
        args.fixture_id,
        child_ids=args.child_ids or None,
        ha_member_entity_ids=args.ha_members or None,
    )
    payload = sanitize_tree(plan)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered + "\n")
    if args.apply:
        if not plan.get("safe_to_apply"):
            sys.stderr.write(
                "refusing --apply: plan is not safe_to_apply; "
                "resolve warnings and re-run dry-run first\n"
            )
            return 1
        result = apply_aggregate_fixture_plan(store, plan)
        sys.stderr.write(json.dumps(sanitize_tree(result), indent=2, sort_keys=True) + "\n")
        return 0 if not result.get("warnings") else 2
    return 0 if plan.get("safe_to_apply") else 1


if __name__ == "__main__":
    raise SystemExit(main())
