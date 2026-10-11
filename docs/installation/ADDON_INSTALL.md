# Scene Studio as a Home Assistant add-on

The recommended installation route. This repository doubles as its own
Supervisor add-on repository: you paste the repository URL once in the Home
Assistant App store, and Supervisor installs, starts, updates, and removes
Scene Studio entirely from the web UI — no second computer, no PowerShell,
no SSH, and no hand-edited `apps.yaml`.

The add-on bundles its own AppDaemon. The Scene Studio backend is an
AppDaemon app (exactly as in the workstation-installed route), and AppDaemon
serves the web Workbench at `/local/scene_studio/` on the same origin as the
API.

## Install

1. In Home Assistant, open **Settings > Add-ons > App store**.
2. Click the **⋮** menu (top right) > **Repositories**, paste
   `https://github.com/pHarmG/Scene-Studio`, click **Add**, then close the
   dialog. Refresh the App store if the new add-on does not appear yet
   (pull-to-refresh on mobile, or leave and re-enter the store).
3. Search for **Scene Studio**, open it, and click **Install**. Supervisor
   builds the image on your machine (the Workbench is compiled during the
   build), so the first install can take several minutes.
4. Open the **Configuration** tab and fill in your lighting sources (below).
5. Click **Start**. Leave **Start on boot** enabled; **Watchdog** is a
   reasonable extra.
6. Open the Workbench: `http://<your-home-assistant-host>:5050/local/scene_studio/`.

## Options (Configuration tab)

| Option | Meaning |
| --- | --- |
| `mode` | `setup` (default): fresh installs start in the safe `registry_admin` mode — discovery and scene authoring work, but nothing can switch lights yet. Flip to `normal` (the add-on restarts) after the Workbench **Setup** view is complete. `read_only` disables provider writes entirely. This replaces the manual `apps.yaml` edit the workstation route requires. |
| `hue_host` | Hue bridge address, e.g. `hue-bridge.local` or an IP. Needs `hue_key`. |
| `hue_key` | Hue CLIP v2 application key (password field). Stored only in the add-on's private `secrets.yaml`; the rendered `apps.yaml` references it by name. |
| `hue_bridge_id` | Optional bridge hardware id, stamped into Hue operations. Requires `hue_host` + `hue_key`. |
| `wled_host` | WLED controller address (`host` or `host:port`). |
| `hyperhdr_host` | hyperHDR JSON-RPC address (`host:port`). |
| `ignored_ha_entity_ids` | HA light entities discovery must not re-adopt. |

Any option can be left empty and filled in later — changing the configuration
restarts the add-on, which re-renders the runtime configuration from the
options. Nothing is written into your Home Assistant configuration by the
add-on.

### Getting a Hue application key without the LINK-button wizard

The add-on cannot press the bridge's LINK button for you (that is an
interactive pairing the workstation wizard offers). Create a key with any
Hue tool you already use — an existing AppDaemon/Hue configuration, the Hue
mobile app's developer area, Hue Essentials, or the bridge's debug/clip API —
and paste it as `hue_key`. The
[Hue developer get-started guide](https://developers.meethue.com/develop/get-started-2/)
shows the manual pairing request. You can also leave Hue unconfigured at
first, finish setup, and add `hue_host`/`hue_key` afterwards.

## Port 5050 and the community AppDaemon add-on

The Workbench and API are published on host port 5050 to match the
workstation-installed route. If you also run the community AppDaemon add-on
on the same host, remap this add-on's port in its **Network** tab (for
example to `5051`) and use that port in the Workbench URL.

## Updating

Updates are owned by Home Assistant. When a new version is released, the
add-on page shows an **Update** button — Supervisor replaces the image and
restarts; your data (scenes, registry, configuration) lives in the add-on's
persistent storage and survives updates. The Workbench reflects this: its
System panel shows version information and a link to the Release page, but
the in-app update button is replaced by a pointer to the add-on page. The
independent update companion used by the workstation route is intentionally
not provisioned on this track.

## Data layout

| Where | What |
| --- | --- |
| Image (`/opt/scene-studio`) | The immutable product: backend app (`apps/scene_studio`), Workbench (`www/scene_studio`). Replaced by Supervisor on update. |
| Add-on data (`/data`) | Rendered `appdaemon.yaml`/`apps.yaml`/`secrets.yaml` (0600), the store (`/data/scene_studio_store`), and the `www` entry point into the image payload. Persists across updates; **Uninstall** removes everything except this data. |

Fresh installs always start in `registry_admin` mode (`mode: setup`); the
Workbench **Setup** view walks you through discovery and room creation, then
you switch the option to `normal` to enable light control.

## The Lovelace card

The add-on does not write into your Home Assistant configuration, so the
optional card is installed manually (the workstation wizard automates this
when given HA config filesystem access):

1. Copy `home-assistant/scene-studio-card/dist/scene-studio-card.js` from the
   repository (or the release ZIP) into `/config/www/scene-studio-card/`
   with any file tool (File editor, Samba, Studio Code Server).
2. Add a Lovelace resource: **Settings > Dashboards > ⋮ > Resources**,
   URL `/local/scene-studio-card/scene-studio-card.js`, type **JavaScript
   module**.
3. Add the `custom:scene-studio-card` card to a dashboard.

## Troubleshooting

- **Add-on does not start / stops right after start**: open the add-on
  **Log** tab. Configuration rendering errors (invalid option values) are
  reported there before AppDaemon starts; fix the Configuration tab and start
  again.
- **Workbench unreachable**: check the **Network** tab mapping and whether
  another add-on already claims host port 5050.
- **HA connection errors in the log**: the add-on talks to Home Assistant
  through the Supervisor proxy with its own credentials — this works only
  while `Start on boot`-managed Supervisor access is intact; restarting the
  add-on re-reads it.
- **Removing completely**: **Uninstall** in the add-on UI, then delete the
  leftover add-on data if you do not plan to reinstall.
