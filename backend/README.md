# Scene Studio — domain core

Provider-neutral scene/fixture domain core and runtime services for
Scene Studio (contracts: `docs/architecture/ARCHITECTURE_CONTRACTS.md`;
status ledger: `docs/development/IMPLEMENTATION_STATUS.md`). The project
originated inside the `pHarmG/Home-Tech` monorepo; see
`docs/MIGRATION_FROM_HOME_TECH.md` for provenance.

Design rules:

- Pure Python, stdlib only in the core (the AppDaemon adapter lazily
  imports `requests` + AppDaemon). Importable and testable without
  AppDaemon.
- Logical identity (fixture/scene/target IDs) is separate from provider
  bindings, discovery observations, capabilities, and health.
- Scenes store intent (what light should do), never provider UUIDs/IPs as
  identity.
- All persistence-ready models implement `to_dict()` / `from_dict()` with
  strict validation raising `scene_studio.domain.ValidationError`.

## Layout (all implemented)

| Package | Purpose |
| --- | --- |
| `scene_studio.domain` | Contract layer: fixtures/bindings/capabilities, scene v2, discovery models, fidelity/render plans, command catalog, events, sanitize |
| `scene_studio.stores` | Atomic JSON persistence: registry, scenes, archive/restore, target resolution |
| `scene_studio.discovery` | Provider payload parsers (Hue CLIP v2 / WLED / HA lights) + deterministic `run_discovery` with explainable candidate scoring |
| `scene_studio.migration` | v1→v2 analyzer/converter, deterministic dry-run reports, backup planning |
| `scene_studio.renderers` | Hue CLIP v2, WLED, HA-light renderers emitting dry-run operation plans with fidelity; shared scene→plan builder |
| `scene_studio.service` | Command engine (full command catalog), HTTP `route()` surface, legacy-event mappers, ports (executor/clock/fetchers) |
| `scene_studio.appdaemon_adapter` | Thin AppDaemon app + requests-based provider executor (deployment-pending, R1) |

Sample/mock data lives in `fixtures/` (`*.sample.json` are consumed by the
Workbench mocks in `workbench/` — keep the copies in
sync). Historical migration reports for the original deployment remain in
the source monorepo.

## Commands

```powershell
# all domain/service/store/renderer/migration tests (run from repo root)
python -m pytest backend/tests -q

# deterministic v1->v2 migration dry-run report (no activation)
python -m scene_studio.migration.dryrun   # PYTHONPATH=backend/src

# local dev server: real engine + built Workbench on one origin
python -m scene_studio.devserver --seed-sample --port 8765

# same, seeded from the GENERIC demo topology (no household data)
python -m scene_studio.devserver --seed-demo --port 8765
```
