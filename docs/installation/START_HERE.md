# Scene Studio — Start Here

Scene Studio adds lighting scenes to Home Assistant: pick the lights it
should discover, build scenes in a web Workbench, and apply them by hand or
from Home Assistant.

There are two ways to install it. **Route A installs directly from the Home
Assistant web UI** — no second computer, no PowerShell, no SSH. Route B is
the original workstation wizard for installing into an existing AppDaemon
add-on from your normal computer.

## Route A (recommended): install from the Home Assistant App store

Everything happens in the Home Assistant UI; Supervisor installs, starts, and
updates Scene Studio like any other add-on.

1. In Home Assistant, open **Settings > Add-ons > App store**, click the
   **⋮** menu (top right) > **Repositories**, paste
   `https://github.com/pHarmG/Scene-Studio`, and click **Add**.
2. Refresh the App store if Scene Studio does not appear yet (pull-to-refresh
   on mobile, or leave and re-enter the store).
3. Search for **Scene Studio**, open it, and click **Install**. The first
   install builds the image on your machine and can take several minutes.
4. Open the **Configuration** tab and fill in what you have — everything is
   optional:
   - **`mode`**: leave `setup` for a first install (Scene Studio starts in
     the safe `registry_admin` mode; nothing can switch your lights yet).
     After finishing the Workbench Setup view, change it to `normal`.
   - **`hue_host` + `hue_key`**: your Hue bridge address and an existing
     CLIP v2 application key (see [ADDON_INSTALL.md](ADDON_INSTALL.md) for
     how to get one). **`wled_host`**, **`hyperhdr_host`**,
     **`ignored_ha_entity_ids`**: only what you use.
5. Click **Start** (leave **Start on boot** enabled).
6. Open the Workbench:
   `http://<your-home-assistant-host>:5050/local/scene_studio/` — or remap
   the port first in the add-on's **Network** tab if port 5050 is already
   used by the community AppDaemon add-on.

Full walkthrough, option reference, the Lovelace card, and removal:
[ADDON_INSTALL.md](ADDON_INSTALL.md). Updates are delivered by Supervisor
(the **Update** button on the add-on page); the Workbench shows that instead
of an in-app update button.

## Route B (advanced): install from your computer into an existing AppDaemon add-on

This route drives the guided installer from a normal computer into the
community AppDaemon add-on you already run. It can do things Route A cannot:
pair a Hue bridge with the LINK button, deploy the Lovelace card
automatically, and choose providers interactively during the review.

### What you need (about 15 minutes)

1. **Your computer (this one):** Windows 10/11 with
   [PowerShell 7](https://aka.ms/powershell) and
   [Python 3.9+](https://python.org) installed. The wizard checks both.
2. **A working AppDaemon add-on** on Home Assistant (Settings > Add-ons >
   AppDaemon — installed and running at least once).
3. **ssh access from this computer to Home Assistant** with a key
   (no password prompt). One-time setup: `ssh-keygen` then copy your key to
   the HA host —
   Windows: `type %USERPROFILE%\.ssh\id_ed25519.pub | ssh root@homeassistant.local "cat >> ~/.ssh/authorized_keys"`
   macOS/Linux: `cat ~/.ssh/id_ed25519.pub | ssh root@homeassistant.local "cat >> ~/.ssh/authorized_keys"`.
4. **A Home Assistant long-lived token:** in Home Assistant, click your
   profile name (bottom left) > Security > Long-lived access tokens > Create.
   The wizard asks you to paste it; it is used for this installation only and
   is not saved to disk.

## Where to find each answer (official docs)

| The wizard asks for... | Where to get it |
| --- | --- |
| Home Assistant address | The same URL you use to open Home Assistant in a browser, e.g. `http://homeassistant.local:8123`. |
| Long-lived access token | In Home Assistant: click your profile (bottom left) > Security > Long-lived access tokens > Create — <https://www.home-assistant.io/docs/authentication/> |
| ssh access to the HA host | Install the **Advanced SSH & Web Terminal** add-on (or the official **Terminal & SSH**), add your public key — <https://github.com/hassio-addons/app-ssh> |
| AppDaemon config directory | On Home Assistant OS the add-on config lives under `/addon_configs/<add-on slug>` — <https://github.com/hassio-addons/addon-appdaemon> |
| Scene Studio / AppDaemon HTTP address | AppDaemon's dashboard port, normally `http://<appdaemon-host>:5050` (inferred by the wizard; asked only if it is not reachable) — <https://appdaemon.readthedocs.io/en/latest/ADDON.html> |
| Hue bridge address | The Hue app: Settings > My Hue bridge — developer docs: <https://developers.meethue.com/develop/get-started-2/> |
| WLED controller address | The WLED web UI address, e.g. `http://wled-1234.local` — <https://kno.wled.ge/basics/web-ui/> |
| hyperHDR address | `host:port` of your HyperHDR server, e.g. `tv.local:8090` — <https://github.com/awawa-dev/HyperHDR> |

## Install (Route B)

Start at [GitHub Releases](https://github.com/pHarmG/Scene-Studio/releases).
Sign in for private access. Download `Scene-Studio-v<version>.zip` and
`SHA256SUMS.txt`; compare the ZIP's `Get-FileHash -Algorithm SHA256` output to
its checksum before extracting. The complete payload is in `scene-studio-release/`.
The GitHub source-code archives are for developers/operators.

For a single downloadable entry point, use the Release asset
`Get-SceneStudio.ps1`: `pwsh ./Get-SceneStudio.ps1`. It downloads the full ZIP,
checks its published checksum and byte-exact manifest, and delegates to the
guided installer. Private access optionally reads `SCENE_STUDIO_GITHUB_TOKEN`
from the process environment (Contents read permission); no token is stored.
If access is unavailable, use the signed-in manual download above.

Open PowerShell 7 **in this folder** and run:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\Install-SceneStudio.ps1
```

Prefer clicking to typing? Launch the browser wizard instead — same
questions, same review, same installer underneath:

```bash
python installer/ui/server.py
```

(it opens your browser automatically; see [INSTALLER_UI.md](INSTALLER_UI.md)).

The wizard asks for your Home Assistant address and token, then tries existing
SSH access to that hostname. On a common HAOS/Supervised setup it finds the
AppDaemon add-on under `/addon_configs`, checks HTTP on port 5050, and checks
Supervisor restart capability. You see the detected setup and choose your
lighting sources; no separate AppDaemon SSH service or hostname is needed.
The wizard shows exactly what it will change and requires confirmation first.

### Common HAOS/Supervised path

Use the same Home Assistant URL you open in your browser. SSH must already
work using your computer's default account/key or SSH configuration. A single
AppDaemon candidate is selected automatically. The Workbench endpoint defaults
to `http://<HA-host>:5050` and is tested before continuing.

### Advanced/custom topology

If automatic detection is incomplete, open **Configure advanced topology** in
the browser wizard, or answer the CLI's fallback questions. You can specify the
filesystem SSH host, username and port, select among several add-on directories,
or supply a separate AppDaemon HTTP address. The filesystem host is whichever
machine exposes the add-on files; HA's API and AppDaemon HTTP may be elsewhere.
The review shows all three addresses. Current automatic deployments require
HA Supervisor's AppDaemon restart capability; manual restart targets are
reported before any writes and are not automatically installed.

Automatic dashboard-card deployment is offered only when that SSH connection
also exposes the configuration of the selected Home Assistant. Split hosts and
SSH aliases need an explicit filesystem confirmation, and the installer checks
HA markers, version and write access independently. Otherwise it installs
Scene Studio and prints manual card deployment/registration instructions.

Running the installer on the same machine does not enable direct filesystem
writes: local mode is reserved, but not implemented in this pass. Container
namespaces can make `/config` or `localhost` refer to a different installation;
the validated deployers currently require SSH.

## After the install

Open the Workbench:

```
http://<your-home-assistant-host>:5050/local/scene_studio/
```

(the wizard prints this link and offers to open it). The **Setup** view walks
you through discovery, adopting your fixtures, and creating rooms. Nothing
can switch your lights until you deliberately leave the safe
`registry_admin` mode — the Setup view shows that final step when you are
ready. (On the add-on route, leaving safe mode is the `mode: normal`
Configuration option instead of editing `apps.yaml`.)

## If something goes wrong

- The wizard backs up your AppDaemon `apps.yaml`/`secrets.yaml` before any
  change and restores them automatically if the install fails; backups stay
  on the HA host under `<appdaemon config>/backups/`. On the add-on route,
  the add-on **Log** tab is the failure channel — configuration rendering
  errors appear there before AppDaemon starts.
- To remove Scene Studio: delete the `scene_studio:` block from `apps.yaml`
  (the marked managed block), then restart AppDaemon. Your data lives at
  `store_root` and survives removal. On the add-on route, **Uninstall** in
  the add-on UI removes everything except the persistent add-on data
  (`/data`, which holds the rendered config and the store).
- Detailed reference (manual profile, noninteractive answers file, rollback
  anatomy): [PORTABLE_INSTALL.md](PORTABLE_INSTALL.md) in this folder
