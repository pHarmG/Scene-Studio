# Scene Studio Contention Runbook — external light sync (hyperHDR)

Implemented: 2026-09-24 (hyperHDR ownership pass). Fulfills the R5 plan §3.3
ownership seam (`docs/scene_studio/R5_DYNAMIC_PLAYBACK_PLAN.md`).

## The problem

hyperHDR (`http://hyperhdr.local:8090`, v21) runs two LED instances —
**"Workstation WLED Instance"** (drives WLED `aabbccddeeff` at `wled.local`)
and **"Workstation HUE Instance"** (drives the Hue bridge at `hue-bridge.local`,
typically the gradient strip). While hyperHDR streams (TV flatbuffer feed at
priority 150 is the usual trigger), direct WLED `/json/state` writes and Hue
CLIP v2 light PUTs / managed-scene recalls are silently overridden. Before
this pass Scene Studio fought or lost to that stream with no visibility.

## Ownership model (user decisions baked in)

1. **hyperHDR wins by default; manual scenes yield.** A scene apply or
   playback start touching held fixtures skips them (partial apply) with an
   explicit yield receipt; uncontended fixtures still execute.
2. **hyperHDR holds until it surrenders.** A hold persists while hyperHDR is
   actively streaming. Scene Studio never re-asserts onto held fixtures and
   never runs timers to take control back. Surrender = hyperHDR's input goes
   idle (e.g. TV off → priority entry clears / WLED `lor` returns to 0).
3. **Surrender auto-restore.** When the hold clears, Scene Studio
   automatically re-applies the last yielded static scene look to exactly
   the yielded fixtures (full gradient point sets on Hue, per-segment state
   on WLED). Superseded playback sessions become `paused` (manually
   resumable) — never auto-resumed.
4. **One-shot override.** `contention_override: "takeover"` on
   `scene.apply` / `playback.start` / `playback.resume` suspends the
   overlapping hyperHDR instance(s) for that single action (handback record
   persisted; "Take over & apply" in the Workbench). The override is NOT
   pinned: the next external grab wins again.
5. **Durable per-fixture policy.** `fixture.set_contention_policy`
   (`yield` | `takeover` | `ignore` | `default`) stored in the registry;
   resolution order: one-shot override > fixture policy > target policy >
   engine default (`yield`). Workbench inspector exposes it as a single
   "External sync" policy select (current value visible; each option's
   next-apply behavior in its tooltip).

## Detection (asymmetric by design)

- **TV sleep transients**: the target is a rooted webOS OLED - webOS can freeze the hyperHDR app across screen-state changes, so a probe read can time out for a tick and fail open; it self-heals on the next TTL tick and is not a network fault (verified: HA reaches hyperhdr.local:8090 and :19444). The hyperhdr HA integration (`light.hyperhdr_sync`) points at the same TV on port 19444.
- **WLED**: authoritative from the device — root `lor` of `GET /json/state`
  (`lor != 0` = a realtime source owns the device). hyperHDR is not consulted
  for WLED contention.
- **Hue**: authoritative from the bridge, per light — `GET
  /clip/v2/resource/entertainment_configuration`. hyperHDR streams to Hue
  over the Entertainment API (DTLS) against one named configuration (this
  deployment has two: a single-light "Hue Gradient EA" and a 4-light "HQ"
  covering the TV-adjacent bias bars + gradient strip); the bridge reports
  that configuration's own `status` as `"active"` for exactly as long as
  it's receiving the stream, and its `channels` list exactly which lights
  are wired into it (resolved via `GET /clip/v2/resource/entertainment`
  service→device, then `GET /clip/v2/resource/device` device→light). Only
  THOSE lights are held — every other `hue_v2` fixture (lamp, office
  strip, bathroom/bedroom lights, etc.) participates in scenes normally
  even while hyperHDR is actively streaming.
  **Superseded (2026-09-24 → 2026-09-29):** the original pass used
  hyperHDR's own `serverinfo` "an input is active" flag (a running
  hue-named instance + an active+visible priority component) and held
  EVERY `hue_v2` fixture whenever it fired. That signal is server-wide —
  it says nothing about which specific lights are on the wire — so it
  silently dropped scene/playback writes to every Hue light in the house
  any time hyperHDR was streaming anything at all, not just the handful it
  actually owns. The `serverinfo` probe is still read for
  `status().contention.streaming`/`streaming_sources` display and for
  identifying the WLED-side hyperHDR instance, but no longer decides which
  Hue fixtures are held.
- **Read unavailable/empty → fail open** (no hold) on the Hue side; the
  unavailability is surfaced in `status().contention.detail`. WLED truth is
  independent of it.

## Runtime surfaces

- `status().contention`: `{configured, default_policy, checked_at, available,
  detail, streaming, streaming_sources, instances, instance_mapping,
  held_fixture_ids, held_owners, wled_probe_ok, pending_restore, handback}`.
- `/fixtures`: each fixture carries `contention: {held, owner}`; the
  Workbench shows a `SYNC` badge on held rows and a "Take over & apply"
  action on scene rows while any fixture is held.
- New commands: `sync.suspend` (explicitly stop the overlapping hyperHDR
  instances, record handback), `sync.resume` (restart handback instances,
  re-asserting the current scene look first), `fixture.set_contention_policy`.
- New failure code: `contended` — the WHOLE requested fixture set was held;
  nothing was executed. `error.details.contention` carries the yield detail.
- Session state `held` (playback schema v2, v1 documents load unchanged):
  sessions whose fixtures get grabbed mid-playback. No provider writes are
  made while held (a `frz` write or static recall would fight the stream).
- Heartbeat: the adapter schedules `refresh_contention()` (3× probe TTL) and
  `reconcile_wled_freezes()` (60 s) in normal mode; Workbench `status`
  polling drives the same TTL-guarded probe in devserver runtimes.

## Stale WLED freezes

A paused/stopped session freezes its WLED segments provider-side (`frz:
true`); a crash or restart can leave that freeze with no owning session —
and a frozen segment silently swallows BOTH Scene Studio renders and
external sync (observed live on seg0 before this pass).
`reconcile_wled_freezes()` clears `frz` on segments no live session owns
(engine startup + scheduled; idempotent; never bumps the revision).

## Configuration (AppDaemon `apps.yaml`)

```yaml
hyperhdr_host: hyperhdr.local:8090     # empty/absent = contention layer off
contention_default_policy: yield  # yield (default) | takeover | ignore
hyperhdr_probe_ttl_seconds: 5     # probe cache TTL; heartbeat at 3x TTL
# Optional instance pinning (else auto-matched by friendly name wled/hue):
# hyperhdr_wled_instance_ids: [0]   # WLED lor-vs-hyperHDR label only
# hyperhdr_hue_instance_ids: [1]    # display/label only -- does NOT decide holds
```

## Verification

- Live probe (read-only, display/labeling only — no longer decides Hue holds):
  `curl -s -X POST http://hyperhdr.local:8090/json-rpc -H "Content-Type: application/json" -d '{"command":"serverinfo","tan":1}'`
  → check `info.priorities` for an `active: true, visible: true` entry and
  `info.instance` running flags.
- Hue truth (authoritative for which lights are held): `curl -sk -H
  "hue-application-key: $HUE_APP_KEY"
  https://hue-bridge.local/clip/v2/resource/entertainment_configuration` → the
  streaming configuration's `status` should read `"active"`; cross-reference
  its `channels[].members[].service.rid` against `GET
  /clip/v2/resource/entertainment` (service→device owner) and `GET
  /clip/v2/resource/device` (device→light `rid`) to see exactly which lights
  it covers. On this deployment: "Hue Gradient EA" = G Strip only; "HQ" =
  G Strip + Upper/Middle/Lower Bar. Lamp, Office Strip, Custom Gradient,
  and Double Strip are never members of either, so they must never be
  held even while hyperHDR is streaming.
- WLED truth: `curl -s http://wled.local/json/state` → root `lor` / `mainseg`
  / `seg[n].frz`.
- Turn the TV sync on: `status().contention.held_fixture_ids` should list
  only the WLED segments plus the Hue lights that are actual members of the
  now-active Entertainment Configuration (within one probe TTL); scene apply
  yields just those; turn it off: auto-restore fires and held sessions
  become `paused`.

## Tests

`services/scene_studio/tests/test_contention.py` (parser, mapping, policy,
gates, takeover, hold/surrender transitions, auto-restore, reconciliation);
recorded live payloads in `services/scene_studio/fixtures/recorded/`
(`hyperhdr_serverinfo.{streaming,idle}.json`, `wled_state.lor_active.json`).
Workbench: `packages/scene_studio_workbench/scripts/smoke.mjs` contention
section.
