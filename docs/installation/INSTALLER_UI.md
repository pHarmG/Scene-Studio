# Scene Studio Installer UI (browser wizard)

A guided, browser-based installer for people who prefer clicking to typing.
It is the command-line wizard (`Install-SceneStudio.ps1`) with a visual
front-end: the same questions, the same read-only probes, the same review,
and — crucially — the same validated install machinery underneath.

The UI is **cross-platform**: it needs Python 3.9+, PowerShell 7 (`pwsh`),
an ssh client, and any browser — the same four prerequisites the CLI wizard
already requires. Nothing is Windows-specific (macOS/Linux work identically
as long as `pwsh` is installed).

## Launch

From the extracted release folder (or a development checkout):

```bash
python installer/ui/server.py
```

Options: `--port 8765` (default), `--no-browser`, `--host 127.0.0.1`
(keep it local). The server prints a URL like

```
http://127.0.0.1:8765/?token=…
```

and opens your browser. Keep the terminal window open while you install;
Ctrl+C stops the UI.

## What the wizard does

1. **This computer** — checks Python, ssh, and pwsh are available.
2. **Home Assistant** — your HA address, plus a long-lived access token
   (paste it once, or set `SCENE_STUDIO_HA_TOKEN` in the environment).
3. **AppDaemon host** — the ssh target (key auth required), the ssh port,
   and the Scene Studio/AppDaemon HTTP address (inferred from the ssh
   target + port 5050; asked only if inference is unreachable).
4. **Config directory** — auto-detects AppDaemon under `/addon_configs`,
   lets you pick, and reads the target read-only (fresh install vs
   upgrade, current runtime mode).
5. **Lighting sources** — HA lights / Hue / WLED / hyperHDR, with
   reachability checks and a press-the-LINK-button Hue pairing flow.
6. **Review** — the complete plan: endpoints, deployment type, runtime
   mode, the exact `apps.yaml` block that will be written, and a merged
   preview of the file. Validated by `scene-studio-profile.py`.
7. **Install** — requires the review checkbox AND typing `INSTALL` (the
   same explicit confirmation the CLI wizard asks for). The installer log
   streams live into the console.

## What actually performs the install

The UI server does **not** implement any installation logic. All read-only
probing is re-implemented 1:1 (ssh/HTTP/TCP) so the UI can show structured
results, but the install itself reuses the guided wizard in its unattended
mode (`-Unattended -Answers`). Backups, the bounded `apps.yaml`/`secrets.yaml`
change, the validated deployers (backup → stage → hash-verify → activate →
health-check → rollback), the optional dashboard card, and the sanitized
support report are the wizard's own tested code paths. If the install fails,
the UI shows the failed stage and the support-report ZIP path — send that
file to the maintainer.

## Secrets handling

- The HA token and Hue application key live **in the UI server's memory
  only** for the duration of the session; they are never written to disk,
  never logged, never part of the answers file, and scrubbed from the log
  console.
- Alternatively set `SCENE_STUDIO_HA_TOKEN` / `SCENE_STUDIO_HUE_APP_KEY` in
  the environment before launching; the UI detects and uses them.
- The deployment answers file written for the wizard contains no secret
  values by design.

## Local security model

The server binds to the loopback interface only, requires a per-launch
session token on every API call, and rejects unexpected `Host` headers.
Do not expose it beyond your machine (`--host` warns if you try); the token
exists so other pages open in your browser cannot drive the installer.
