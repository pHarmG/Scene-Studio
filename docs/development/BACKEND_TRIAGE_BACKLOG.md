# Scene Studio backend triage backlog

This is the small, evidence-led queue for Scene Studio questions that cannot
be responsibly resolved in the Workbench alone. Entries are not implementation
approval: validate the provider/device behavior first, then make the minimal
registry, discovery, or capability-model change with recorded test coverage.

## Resolved / evidence required for live validation

| ID | Priority | Question | Current evidence | Next backend investigation | Guardrail |
| --- | --- | --- | --- | --- | --- |
| SS-BE-001 | High | **resolved — ready for live evidence refresh** | `custom_gradient` now retains immutable identity, a typed GLEDOPTO GL-C-103P profile, a `limited` capability assessment, and Hue-grounded effective brightness/color/CCT capabilities with no gradient or native dynamic support. Its operational health is independent: it can be `ready` or `missing` while remaining capability-limited. | Capture fresh read-only Hue light/device + HA metadata once reachable; update profile only from that evidence. A display-name decision requires separate topology confirmation. | No direct-IP/WLED inference and no renderer gradient authorization from the profile. Live registry/provider changes remain explicitly approval-gated. |
| SS-BE-002 | Normal | **deferred — leave-behind hardware** | `double_strip` remains disabled with its historical Hue binding and a descriptive TS0505B profile. No direct endpoint or bespoke provider is assumed. | If it becomes relevant, use discovery → `fixture.rebind_preview` → explicit approved rebind → parity/rollback proof for on/off, brightness, color, CCT, and scene fidelity. | The generic atomic rebind/rollback seam exists; no live migration is authorized. |

## Entry references

- `services/scene_studio/fixtures/registry.sample.json` — checked-in v2
  fixture model for the separated physical-profile/effective-capability state.
- `services/scene_studio/reports/r2_registry_reconciliation_20260911.md` —
  live reconciliation record and its 2026-09-11 capability finding.
- `docs/scene_studio/ARCHITECTURE_CONTRACTS.md` — capability/discovery
  contracts and validation expectations for a future model change.
