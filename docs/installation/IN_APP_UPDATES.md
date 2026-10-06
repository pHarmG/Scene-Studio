# Transactional Workbench updates (protocol 1)

The installer provisions a separate `scene_studio_update` AppDaemon app. Its
modules live directly in `apps/`, outside `apps/scene_studio/`, and import no
Scene Studio modules. Updates NEVER replace the supervisor itself. Updating
that trust boundary requires an explicit external installer deployment.

On the HA AppDaemon add-on, the host add-on configuration directory is mounted
as `/config`. Product trees are `/config/apps/scene_studio` and
`/config/www/scene_studio`. The installer derives the add-on slug from the
operator's config root; generic code contains no house topology. Read-only
reconciliation found both host product directories present and an older API
healthy. Container inspection was blocked by SSH protection mode and the
available HA credential could not read add-on metadata. Container UID and HA
restart authorization therefore remain runtime preflight requirements, not
assumed facts. The upstream add-on declares writable config mounts:
https://github.com/hassio-addons/addon-appdaemon/blob/main/appdaemon/config.yaml

The supervisor uses the existing HASS plugin to request only
`hassio/addon_restart` for its configured add-on. This clears Python module
caches consistently with the external backend deployer. It does not rely on
PowerShell, SSH, or a workstation. After the add-on restarts, AppDaemon loads
the independent supervisor even if the new Scene Studio app cannot import.
Its durable journal is under `/config/scene_studio_updates`, outside all
replacement trees. Startup reacquires an OS lock and resumes verification or
restores both backups. A failed add-on/container startup itself requires
external recovery; no in-container runner can recover a dead host.
Production success requires a new Python-process witness (retained across
ordinary app/module reloads; independent of reusable container PIDs). The
activating process waits for shutdown, with a bounded watchdog that rolls
back if restart never happens. Restart service disconnects are expected.

`scene_studio_update_api` uses AppDaemon's existing RPC transport. Its logical
routes are `POST /update` with **only** `{target_version: "0.1.1"}` and
`GET /update/status`. The browser confirms installed and target versions.
The server resolves the exact tag and immutable asset IDs independently.
All credentials remain in process memory/environment. Network errors expose
fixed messages, never upstream bodies, exception strings, or signed URLs.

The canonical release-trust source is `backend/src/scene_studio/release_trust.py`.
The installer copies it as `apps/scene_studio_release.py` for the independent
supervisor. Discovery imports the same source. Protocol 1 requires checksum,
safe ZIP paths, byte-exact manifest, release BUILD identity, consistent backend
and Workbench build metadata, and no configuration migration.

Before mutation the runner checks writable, non-symlink, distinct product and
journal paths, healthy old API, and restart-service availability. It stages
and hashes both trees and persists backups before activation. Every rename
has persisted recovery intent. Startup after interrupted activation rolls
back conservatively; startup after restart checks the exact new build, runtime
mode and served Workbench identity. Rollback verifies the previous healthy
build after another restart. Failed recovery retains backups and a clear
failure state. An OS lock excludes multiple workers across reloads/processes.

Only the two product trees are replaced. Stores, authored scenes, registry,
provider configuration, `apps.yaml`, `secrets.yaml`, HA Core, dashboards,
the optional Lovelace card, and unrelated apps are preserved. Protocol 1 has
no config migrations. Future migrations need a versioned protocol.

Existing installations need the companion provisioned through the approved
external installer before in-app Update is available. Provisioning changes
two companion files and the managed apps.yaml block and restarts AppDaemon;
it is a live write requiring the operator's approval. This foundation does
not deploy itself to the developer's live host.

Workbench display semantics (update UX pass): the journal is a record of
the LAST transaction, so a terminal `succeeded` entry persists after the
update it describes has long been reloaded. The Workbench therefore treats
a recorded success as historical once the running backend build and the
loaded Workbench bundle both reflect the journal's target — the Update
row (checked availability) stays the single live version story. A recorded
success is displayed only in the one window that needs action: the backend
restarted onto the new build while the open page still runs the old
bundle. That reload is real and observable — the page navigates to the
fresh build stamp with bounded retries, and the panel shows a manual
"Reload Workbench now" fallback. A healthy running build that is neither
the target nor the previous build fails the follow-up immediately with a
readable explanation instead of spinning to the recovery timeout.
