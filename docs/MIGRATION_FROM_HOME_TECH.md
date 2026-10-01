# Migration from Home-Tech

Scene Studio was extracted from the `pHarmG/Home-Tech` monorepo into this
dedicated repository.

| | |
| --- | --- |
| Source repository | `pHarmG/Home-Tech` |
| Extraction baseline commit | `c299397cd286dd98e56534e4e86e676ea03d24ed` (*scene-studio: add guided portable installer*) |
| Extraction date | 2026-09-30 |
| First canonical standalone commit | the initial commit of this repository that carries this file |

## What was migrated

- `services/scene_studio` → `backend/` (domain core, stores, discovery,
  renderers, service, AppDaemon adapter, fixtures, full test suite).
- `packages/scene_studio_workbench` → `workbench/`.
- `packages/test_bench_scene_controls_card` → `home-assistant/scene-studio-card/`
  (productized as `custom:scene-studio-card`; the historical
  `test-bench-scene-controls-card` element name ships as a compatibility alias).
- `packaging/scene_studio` + `scripts/scene_studio` + the two Scene Studio
  deployers → `installer/` and `scripts/build_release.py` (release packaging
  rewritten for the standalone layout; the monorepo `support/scene-studio`
  nesting was removed entirely).
- `docs/scene_studio` → `docs/architecture|development|installation|operations|design/`.

Not migrated (they stay in Home-Tech, specific to that installation): the
v1→v2 activation gate (`migration/activation.py`), the recorded
`migration/` reports, the demo-fixture generator, and the production
preflight script.

## Why history was not transferred

Git history was intentionally **not** carried over. The source repository's
history contains historical credentials and private household topology, and
that contamination must not become inherited history in Scene Studio. All
migrated content was additionally re-sanitized: production LAN addresses,
device/bridge ids, and personal names were replaced with generic demo values
or RFC 5737 documentation addresses before the first commit.

For pre-migration archaeology, use the Home-Tech repository's history up to
the baseline commit above.

## Canonical going forward

Subsequent Scene Studio development is canonical **here**. Home-Tech retains
only its own consuming dashboards/automations/configuration plus a pointer
document (`docs/integrations/SCENE_STUDIO.md` in that repository). Do not
recreate a second Scene Studio implementation in Home-Tech.
