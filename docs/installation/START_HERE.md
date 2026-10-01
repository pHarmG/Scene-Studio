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
   (no password prompt). One-time setup:
   `ssh-keygen` then copy your key to the HA host
   (`type %USERPROFILE%\.ssh\id_ed25519.pub | ssh root@homeassistant.local "cat >> ~/.ssh/authorized_keys"`).
4. **A Home Assistant long-lived token:** in Home Assistant, click your
   profile name (bottom left) > Security > Long-lived access tokens > Create.
   The wizard asks you to paste it; it is used for this installation only and
   is not saved to disk.

## Install

Open PowerShell 7 **in this folder** and run:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File .\Install-SceneStudio.ps1
```

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
  anatomy): [support/scene-studio/packaging/scene_studio/PORTABLE_INSTALL.md](support/scene-studio/packaging/scene_studio/PORTABLE_INSTALL.md)
