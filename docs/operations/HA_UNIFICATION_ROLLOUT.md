# Scene Studio ↔ Home Assistant Unification — implementation / rollout record

Status: **live applied** (2026-09-16 evening / 2026-09-17 UTC, America/Chicago)

Plan: [.cursor/plans/scene_studio_ha_unification_canonical_bridge.plan.md](../../.cursor/plans/scene_studio_ha_unification_canonical_bridge.plan.md)

This pass promotes the existing HA-native Scene Studio bridge into the
canonical HA boundary, replaces the dead Meeting Blue pipeline, retires
dead apply/delete compatibility, migrates dashboards off
`input_select.saved_scenes`, demotes the aggregate `office_lights` helper
from the fixture model, and adds Scene Studio-derived target power.

Staged HA patches:
[`ha_legacy_staging/scene_studio_unification/`](../../ha_legacy_staging/scene_studio_unification/README.md).

---

## 0. Reconciliation (read-only, 2026-09-16)

| Item | Finding |
| --- | --- |
| Local HEAD at recon | `fe86db9` (plan-only) plus this unification working tree |
| Live backend vs local | VerifyOnly: live Python package matched the then-deployed tree, **not** this working tree |
| Live Workbench | Served; schema v2 / `targets` not yet published |
| HA Core | 2026.9.2 |
| `runtime.mode` | `normal` |
| Provider writes | enabled |
| `legacy_events_enabled` | `false` |
| `sensor.scene_studio_ui` | exists, updating; no `bridge_schema_version` / `targets` yet |
| `scene_studio_ui_command` | registered/consumed by Scene Studio |
| Fixtures | 25 total / 23 ready / 2 disabled (live still includes aggregate `office_lights`) |
| `current.scene_id` | `meeting_blue` |
| `studio` | 14 members including `office_strip` |
| `office` | only `office_lights` |
| `whole_house` | 23 including `office_lights` |
| `light.office_lights` | HA Light Group helper; members are already canonical Scene Studio fixtures (WLED segments, hue bars, lamp, office_strip, …). Not a Hue grouped-light. |
| Live scene `fixture_states.office_lights` | none on active catalog; archived `cutover_test_scene` still targets `office` |
| `wind_down` | still lists target `office` |
| `apply_scene` / `delete_scene` apps | already unregistered |
| `script.delete_scene_script` | already gone |
| `script.apply_scene_script` | `rest_command.scene_studio_apply_scene` with slug from `input_select.saved_scenes` |
| `automation.test_apply_meeting_blue_scene` | already gone |
| `automation.apply_office_work_scene` | 7 AM weekdays: set helper to Meeting Blue, delay, `script.apply_scene_script` |
| `automation.office_lights_off` / NFC `toggle_room_lights` | `light.office_lights` |
| Test Bench + Office Control | still pass deprecated saved-scenes keys to the scene-controls card; light-group-card `main_entity: light.office_lights` |
| Hue-native "Work lights" | **not modified** |

Production preflight at recon: **PASSED** (read-only).

---

## 1. Repo implementation

- `ui_bridge` is documented as the general HA seam. `BRIDGE_SCHEMA_VERSION = 3`.
  Projection adds `current`, per-scene `target_ids`, and canonical `targets`
  from `project_target_membership` (same skip semantics as apply/render).
  Scene `target_ids` let the compact card resolve the brightness trim's
  lights from authoring data — live sessions and `current` are runtime
  state that automations and restarts clear, and slider visibility no
  longer depends on them.
- Bindings flatten `ha_entity_id` / `ha_entity_ids` with de-dupe.
- Discovery classifies HA aggregates via member-list evidence, plus
  `ignored_ha_entity_ids` as a narrow fallback. Aggregates are not
  `available_unbound` and are not rebind candidates.
- Dry-run-first `migration/aggregate_fixture.py` (+ CLI
  `python -m scene_studio.migration.aggregate_cli`). `safe_to_apply` is
  false when unique aggregate intent cannot be assigned, children cannot
  be resolved for a unique override, or an active scene would be left on
  an empty target. `--apply` refuses before any write when the plan is
  unsafe.
- `diagnostics.export` includes `membership_drift`. Preflight reports it.
- Sample registry/scenes/discovery + Workbench mocks: `office_lights`
  removed, `office_strip` in `studio`, `office` target retired (24
  fixtures / 5 targets). Synthetic renderer tests still use an
  independently addressable `office_lights` ha_light fixture.

---

## 2. Live apply (2026-09-16/17)

Followed `ha_legacy_staging/scene_studio_unification/README.md` apply order. Mode-preserving
backend deploy, then scripts, proven apply, then automations/dashboards, then
rest_command delete, then ignore-list + aggregate `--apply`.

| Step | Result |
| --- | --- |
| Backend `-Apply` | Backup `HA:/addon_configs/a0d7b954_appdaemon/backups/scene-studio-backend-20260917T010417Z/`. AppDaemon only. `runtime.mode=normal`, writes enabled, `legacy_events_enabled=false`. Projection `bridge_schema_version: 2` with 6 then 5 `targets`. |
| `script.scene_studio_apply` + `script.scene_studio_target_power` | Patched into `/config/scripts.yaml` (not replaced). `apply_scene_script` left on rest_command until apply proof. |
| Apply proof | `meeting_blue` and `twilight` via `script.scene_studio_apply`. `last_command.ok=true`, `command=scene.apply`, matching `ha-…` request ids, `current.scene_id` matched. AppDaemon log: `scene_studio_ui_command scene.apply -> ok=True`. No `apply_scene_event`. |
| Transitional wrapper | `apply_scene_script` now slugs `input_select.saved_scenes` → `scene_studio_apply`. "Meeting Blue" → `meeting_blue` ok. |
| Automations | Patched only Office Lights Off (`1733877045406`), 7 AM Meeting Blue (`1733947344161` / `automation.apply_office_work_scene`), and anchored NFC `&id001` / `*id001`. Unrelated automations preserved. |
| Dashboards | Fresh live pull matched staged baselines. Pushed Test Bench + Office Control. Scene-controls card assets deployed (`-AssetsOnly`). Core restarted; storage hashes survived. |
| `rest_command.scene_studio_apply_scene` | Deleted from `/config/home_tech/domains/rest_commands.yaml`. Other rest_commands preserved. `rest_command.reload` — service gone. |
| `ignored_ha_entity_ids` | Added under live `scene_studio:`. AppDaemon restarted. Helper was **not** re-adopted. |
| Aggregate CLI | Dry-run `safe_to_apply=true`, warnings `[]`, retire `office`. Store backup `scene-studio-store-unification-20260917T011900Z.tgz`. `--apply` removed `office_lights`, retired `office` (`wind_down` dropped emptied `office`; archived `cutover_test_scene` kept last empty target). Live store is AppDaemon `/config/scene_studio_store` = host `/addon_configs/a0d7b954_appdaemon/scene_studio_store`. |
| Target power | Unquoted YAML `on`/`off` field options left the script **unavailable** on Core 2026.9.2. Quoted `"on"`/`"off"` + explicit choose template conditions; automation `data.action` quoted. After fix: studio toggle any-on → 13 available members off, all-off → 13 on. `meeting_blue` restored. `light.custom_gradient` remains unavailable. |
| Preflight | **PASSED**. 24 fixtures / 22 ready / 0 ha_light. Notes: `custom_gradient` apply error, incomplete HA coverage on 3 non-studio targets, Workbench index differs (Workbench not deployed this pass). |

Hue-native "Work lights" was **not** modified. It can now be disabled or
deleted **manually** in the Hue app; the HA 7 AM path is
`script.scene_studio_apply` / `meeting_blue`.

Not done (by design):

- `script.apply_scene_script`, `scene_manager`, and `input_select.saved_scenes`
  remain — wrapper still reads the helper.
- `light.office_lights` Light Group helper remains. Dashboards and the three
  patched automations no longer reference it. Delete it in the HA UI (do not
  hand-edit `.storage`).
- Workbench SPA was not redeployed; live Workbench still talks to the API.

---

## 3. Remaining operator steps

1. After **zero-consumer proof**, remove `script.apply_scene_script`, unregister
   `scene_manager`, and delete `input_select.saved_scenes` in the HA UI.
2. Delete the `light.office_lights` helper in the HA UI.
3. Optionally retire Hue-native "Work lights" manually.
4. Optional later: deploy Workbench static assets if the local build should
   match the served index.

`legacy_events_enabled` stays **false**.

---

## 4. Rollback

- Backend backup: `HA:/addon_configs/a0d7b954_appdaemon/backups/scene-studio-backend-20260917T010417Z/`
- Store backup: `HA:/addon_configs/a0d7b954_appdaemon/backups/scene-studio-store-unification-20260917T011900Z.tgz`
- `scripts.yaml` / `automations.yaml` / `apps.yaml` / rest_commands / Lovelace
  `.bak-unification-*` copies on HA
- `legacy_events_enabled` stays **false**. Do not re-enable legacy listeners.

---

## 5. Validation

Repo (2026-09-16, pre-deploy):

| Gate | Result |
| --- | --- |
| `python -m pytest services/scene_studio/tests` | **847 passed**, 1 skipped |
| `packages/scene_studio_workbench` `npm run smoke` | **715 / 0** |
| `npm run build` (chained by browser) | green |
| `npm run browser` | **95 / 0** |
| `packages/test_bench_scene_controls_card` `npm test` | **13 / 0** |
| `python scripts/security/scan_secrets.py` | exit 0 |

Live (2026-09-17 UTC):

| Gate | Result |
| --- | --- |
| Backend deployer | passed, mode `normal` preserved |
| `script.scene_studio_apply` meeting_blue + twilight | `last_command.ok`, `scene.apply` |
| Transitional `apply_scene_script` | Meeting Blue → meeting_blue |
| Studio target-power toggle | any-on → all off; all-off → all on |
| Production preflight | **PASSED** (24/22 fixtures; notes above) |
| `test_target_power.py` after YAML quote fix | 6 passed |
