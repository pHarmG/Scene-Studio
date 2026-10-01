# Scene Studio — Start Here

Scene Studio adds lighting scenes to Home Assistant: pick the lights it
should discover, build scenes in a web Workbench, and apply them by hand or
from Home Assistant. This ZIP installs it onto your Home Assistant + AppDaemon
system **from your normal computer** — nothing is installed on your PC.

## What you need (about 15 minutes)

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

## Install

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

The wizard asks roughly six questions — your Home Assistant address, the
ssh target, which lighting sources to discover (Hue / WLED / HA lights), and
any addresses those need — then shows exactly what it will change and asks
for a final confirmation before touching anything.

## After the install

Open the Workbench:

```
http://<your-home-assistant-host>:5050/local/scene_studio/
```

(the wizard prints this link and offers to open it). The **Setup** view walks
you through discovery, adopting your fixtures, and creating rooms. Nothing
can switch your lights until you deliberately leave the safe
`registry_admin` mode — the Setup view shows that final step when you are
ready.

## If something goes wrong

- The wizard backs up your AppDaemon `apps.yaml`/`secrets.yaml` before any
  change and restores them automatically if the install fails; backups stay
  on the HA host under `<appdaemon config>/backups/`.
- To remove Scene Studio: delete the `scene_studio:` block from `apps.yaml`
  (the marked managed block), then restart AppDaemon. Your data lives at
  `store_root` and survives removal.
- Detailed reference (manual profile, noninteractive answers file, rollback
  anatomy): [PORTABLE_INSTALL.md](PORTABLE_INSTALL.md) in this folder
