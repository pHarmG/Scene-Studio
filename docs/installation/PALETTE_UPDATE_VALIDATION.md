# Palette release update validation

The sentinel pair is **v0.1.0** (independent update companion and the old
Overrides editor) → **v0.1.1** (Palette as the sole fixture-look editor).
The v0.1.1 release workflow consumes the published v0.1.0 ZIP and its checksum
and exercises the production transaction engine with both actual product
trees in a temporary installation. It checks the compiled UI sentinel,
failed-activation rollback, reconnect, build identity and preserved data.
This controlled test does not establish live HA restart permission or success.

## Prerequisites for an older installation

The development instance inspected during preparation predates installed build
metadata and has no update endpoint. Its current version is **unreported**.
It cannot perform the in-app test until v0.1.0 is installed using the canonical
guided installer. This is a separate live write requiring explicit approval.

Use `pwsh ./Get-SceneStudio.ps1 -Version 0.1.0` to retrieve the verified baseline
and open its installer. Review the existing-install upgrade: backend and
Workbench trees, the two companion modules in `apps/`, the managed
`scene_studio`/`scene_studio_update` apps.yaml block, and AppDaemon restart.
Keep existing named secrets and runtime mode; decline the optional card change.
Do not substitute source deploys or copying static assets for this baseline.

For this private repository, give the AppDaemon **server process** a
`SCENE_STUDIO_GITHUB_TOKEN` with repository Contents read access. The bootstrap
may also need workstation access. Never place a token in browser settings,
answers, profiles or support reports. Restart authorization for the configured
AppDaemon add-on and writable product paths are also runtime prerequisites.
See [the update architecture](IN_APP_UPDATES.md).

## Eight-step manual test

1. Open the baseline Workbench; System → Product must show **0.1.0**.
2. Open System → Update and click **Check for updates**.
3. Confirm **0.1.1**, tag **v0.1.1**, is offered.
4. Click **Update** and explicitly confirm **0.1.0 → 0.1.1**.
5. Observe download, verification, backup, staging, activation and reconnect.
   AppDaemon restarts once; a temporary disconnect is expected progress.
6. Reload the page once, then confirm **0.1.1** and the release source SHA in
   System → Product. The original published v0.1.0 reconnects its API but keeps
   its running JavaScript; v0.1.1 adds automatic static reload for future updates
   and defers that reload while a scene draft has unsaved changes.
7. Edit a scene: expand a Palette row, then one assigned fixture's details.
   Exceptional fixtures/controllers are under Other lights. There is no
   standalone Overrides panel or Add Override selector.
8. Open and save an existing scene to confirm its authored data survived.

The update replaces only backend and Workbench. Scene stores, topology,
configuration, apps.yaml, secrets, unrelated files and Lovelace remain intact.
