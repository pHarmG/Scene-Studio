# Scene Studio — Legacy Retirement Readiness (audit only, no deletion)

Status: **audit updated 2026-09-17 UTC** after the HA unification live apply.
Live result: [HA_UNIFICATION_ROLLOUT.md](HA_UNIFICATION_ROLLOUT.md).
Staging notes remain in [`ha_legacy_staging/scene_studio_unification/`](../../ha_legacy_staging/scene_studio_unification/README.md).

Scope note: production Scene Studio is live in `normal` mode with
`legacy_events_enabled: false`. As of 2026-09-16 the live AppDaemon
`apply_scene` and `delete_scene` apps are **already unregistered**.
`scene_manager`, `save_light_states`, `generate_office_scene`,
`generate_crosswalk`, and `voice_stack_control` remain. Scene Studio still
does **not** register compatibility listeners.

Canonical HA seam (this pass): `sensor.scene_studio_ui` +
`scene_studio_ui_command`. Do not reintroduce `/config/custom_scenes/` or
enable legacy listeners as an intermediate fix.

Legacy sources referenced here:

- `ha_legacy_staging/appdaemon_scene_tools/*.py` and
  `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/` (live pull, 2026-09-10)
- `ha_legacy_staging/scene_studio_unification/` (2026-09-16 live recon + staged patches)
- `docs/scene_studio/PASS1_PRODUCTION_CUTOVER.md` (live app inventory)
- `services/scene_studio/src/scene_studio/service/legacy.py` and
  `appdaemon_adapter/adapter.py` (the compatibility seam)

---

## 1. Legacy AppDaemon apps

| Legacy app/artifact | Current producer / consumer | Scene Studio replacement | Remaining dependency | Retirement prerequisite | Classification |
| --- | --- | --- | --- | --- | --- |
| `apply_scene`: `apply_scene.py` (`ApplySceneApp`) listens for `apply_scene_event` | **Live unregistered**. Repo Python retained as reference. Canonical path is `script.scene_studio_apply` | `scene.apply` through `scene_studio_ui_command` / `script.scene_studio_apply` | none | rest_command deleted 2026-09-17 | **retired live** (app + rest_command) |
| `save_light_states`: `save_light_states.py` listens for `save_light_states_event` | `ha_legacy_staging/scripts.yaml` → `script.save_light_states_script` (uses `input_text.dynamic_scene_name`) | `scene.save` exists (empty-draft v2 parity) and is reachable through the compatibility seam, but live provider **capture** into a new scene is still a documented engine gap | `input_text.dynamic_scene_name` helper; v1 captured real provider state, v2 `scene.save` writes an empty draft | Decide whether provider-state capture is in product scope; until then, retire the script but keep the v1 files as reference | retain temporarily (`scene.save` semantics differ) — **out of this unification pass** |
| `delete_scene`: `delete_scene.py` listens for `delete_scene_event` | **Live unregistered**; `script.delete_scene_script` already gone | `scene.archive` | none | none remaining | **retired live** (app + script); Python retained as reference |
| `generate_office_scene`: `generate_office_scene.py` listens for `generate_office_scene` | `ha_legacy_staging/scripts.yaml` → `script.generate_scene`; automation `Generate Scene` fires it with three `input_text.custom_color_*` colors | Deterministic `legacy.py:map_generate_office_scene` → `scene.save` + `scene.apply` on the stable id `generated_office` | The automation + three `input_text.custom_color_*` helpers | Confirm the deterministic replacement is acceptable | retain temporarily — **out of this unification pass** |
| `scene_manager`: `scene_manager.py` (`SceneManager`) | `input_select.saved_scenes` state management | Scene Studio catalog + `script.scene_studio_apply` | Transitional `apply_scene_script` wrapper still reads the helper | Zero-consumer proof (dashboards + 7 AM already migrated; wrapper remains) | **live, pending zero-consumer retirement** |
| `generate_crosswalk`: `generate_crosswalk.py` (`GenerateCrosswalk`) | Wrote `Resources/crosswalk.json` | Discovery + registry v2 | Historical crosswalk is v1 apply-scope authority | Existing retirement conditions | retain temporarily — **out of this unification pass** |
| `voice_stack_control` | Pi3/Mac voice stack control | **Not** a Scene Studio concern | Unrelated | Never retire as part of Scene Studio work | out of scope |

---

## 2. Legacy events still emitted / consumed

| Event | Emitted by | Consumed by (today) | Scene Studio seam | Classification |
| --- | --- | --- | --- | --- |
| `apply_scene_event` | no live emitter (`apply_scene_script` wraps `scene_studio_apply`) | no live app | `legacy.LEGACY_EVENT_MAPPERS["apply_scene_event"]`; registered only when `legacy_events_enabled: true` | ready to forget; do not re-enable |
| `save_light_states_event` | `script.save_light_states_script` | `save_light_states` legacy app | `LEGACY_EVENT_MAPPERS["save_light_states_event"]` | retain temporarily (capture gap) |
| `delete_scene_event` | no live emitter | no live app | `LEGACY_EVENT_MAPPERS["delete_scene_event"]` | **retired live** |
| `generate_office_scene` | automation `Generate Scene` | `generate_office_scene` legacy app | `adapter._on_generate_office_scene` | retain temporarily |
| `scene_studio_ui_command` | scene-control card + staged `script.scene_studio_apply` | Scene Studio `ui_bridge` (always on) | canonical HA command ingress | **canonical — keep** |

`legacy_events_enabled: false` remains mandatory.

---

## 3. `input_select.saved_scenes` and related helper entities

| Artifact | Current role | Scene Studio replacement | Classification |
| --- | --- | --- | --- |
| `input_select.saved_scenes` | Live picker still used by `script.apply_scene_script` and unmigrated Lovelace keys | `sensor.scene_studio_ui.scenes` + stable `scene_id` | staged dashboards drop it; retire helper in HA UI after wrapper + scene_manager have no callers |
| `input_text.dynamic_scene_name` | Name input for `script.save_light_states_script` | Builder Identity field | retain temporarily |
| `input_text.custom_color_1..3` | Color inputs for `Generate Scene` | Builder palette | retain temporarily |
| `light.office_lights` | HA Light Group helper; duplicate membership vs Scene Studio `studio` | `script.scene_studio_target_power` reading projected `targets[]` | staged consumers migrate to `studio`; retire helper in HA UI after coverage is complete — **never hand-edit `.storage`** |

---

## 4. Dashboards / cards / scripts that still read the legacy interface

| Consumer | Reference | Impact if the legacy interface disappears | Classification |
| --- | --- | --- | --- |
| `packages/test_bench_scene_controls_card` | Canonical path is `sensor.scene_studio_ui` / `scene_studio_ui_command`. Deprecated saved-scenes config keys are accepted and ignored. Schema v2 adds `targets` / `bridge_schema_version` / `current`; v3 adds per-scene `target_ids` so the brightness trim resolves lights from authoring data (card keeps a runtime fallback for pre-v3 projections) | none for the card | ready; card and backend schema v3 deploy in any order |
| Test Bench / Office Control Lovelace | Live boards use `sensor.scene_studio_ui`, Studio power buttons, Front/Side/Back Office zones (no fake parent) | none for the migrated boards | **live** |
| `script.apply_scene_script` | Live slug wrapper → `scene_studio_apply` | none required (7 AM uses `scene_studio_apply` directly) | transitional; delete after zero-consumer proof |
| `rest_command.scene_studio_apply_scene` | **deleted live** 2026-09-17 | Canonical script replaced it | **retired live** |

---

## 5. Legacy v1 storage

| Artifact | Location | Role today | Classification |
| --- | --- | --- | --- |
| v1 scene files (7) | HA `/config/custom_scenes/*.json`; repo copies at `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/custom_scenes/` | Migration source of record; byte-identical to the R4 activation inputs; already consumed into v2 scenes | retain temporarily — keep as the rollback/reference artifact |
| v1 archive folder | HA `/config/custom_scenes_archive/`; repo copies in the same pull directory | Historical archived v1 scenes | retain temporarily |
| `Resources/crosswalk.json` / `crosswalk_generated.json` | `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/Resources/` | v1 apply-scope authority used by R4 migration | retain temporarily |
| R4 immutable backup directory on HA | `scene_studio_store/backups/v1_immutable_R4_20260912T045209Z/` | Found **empty** during the R5D preflight (contradicts the R4 record); preserved untouched as evidence | unknown / user decision — do not delete; the R5D-reconstructed directory is the accurate reference |
| R5D reconstructed v1 reference | `scene_studio_store/backups/v1_reference_reconstructed_R5D_20260913T201454Z/` | SHA-256-verified v1 reference with a provenance manifest | retain temporarily |
| Pre-cutover / pre-R5D tarballs | `backups/pre-pass1-normal-20260915T150502Z.tar.gz`, `pre-R5D3-*.tar.gz`, `pre-R5D2-*.tar.gz`, `store-pre-R4-*.tar.gz` | Rollback archives | retain temporarily — the Pass 1 archive is the current production rollback artifact |

---

## 6. Rollback artifacts and credential-bearing files worth retaining

- `HA:/addon_configs/a0d7b954_appdaemon/backups/pre-pass1-normal-20260915T150502Z.tar.gz`
  plus `apps/apps.yaml.bak-pass1-20260915T150502Z` — returns production to
  `read_only: true`. **Keep until legacy Apps are retired.**
- `HA:.../backups/scene-studio-backend-20260915T150943Z/` and
  `HA:.../backups/scene-studio-workbench-20260915T152127Z/` — package/asset
  rollback for the currently deployed revisions.
- `ha_legacy_staging/appdaemon_scene_tools/live-pulled-20260910/apps/apps.yaml` — sanitized,
  but the **live** `apps.yaml` still carries the Hue application key and HA
  long-lived tokens (by reference/secrets) for the legacy apps. Retiring the
  legacy apps also retires those credential consumers; rotation is tracked
  separately in [SECURITY.md](SECURITY.md) and is still `deployment-pending`.

---

## 7. Summary

- **Already retired live:** `apply_scene` / `delete_scene` AppDaemon apps,
  `script.delete_scene_script`, `automation.test_apply_meeting_blue_scene`,
  `rest_command.scene_studio_apply_scene`, Scene Studio fixture
  `office_lights` / target `office`.
- **Live this unification pass:** canonical `script.scene_studio_apply`
  + `script.scene_studio_target_power`; 7 AM Meeting Blue on stable
  `scene_id`; NFC/Office Lights Off on `studio` projection; dashboards off
  `input_select.saved_scenes` / `light.office_lights`; ignore-list the HA
  helper in discovery.
- **Retain temporarily (explicitly out of this pass):** `save_light_states`,
  `generate_office_scene` + `custom_color_*`, `generate_crosswalk`, v1
  files/backups, `voice_stack_control`.
- **After live zero-consumer proof:** `script.apply_scene_script`,
  `scene_manager`, `input_select.saved_scenes` (HA UI; no `.storage` edits).
- **Ready for HA UI deletion:** `light.office_lights` helper (dashboards and
  the three patched automations no longer reference it).
- **Operator note:** Hue-native "Work lights" can be retired **manually**.
  Do not modify it from this repo.
