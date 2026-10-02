/* Scene Studio installer UI — wizard front-end.
   Vanilla JS, no build step. Talks to the local installer server (server.py)
   which performs all read-only probes and drives the guided installer. */

"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";

const state = {
  session: null,
  probes: {},
  plan: null,
  step: 1,
  run: { status: "idle", seen: 0, support_report: null, returncode: null },
  pollTimer: null,
};

const STEPS = [
  { n: 1, title: "This computer" },
  { n: 2, title: "Home Assistant" },
  { n: 3, title: "Detected setup" },
  { n: 4, title: "Config directory" },
  { n: 5, title: "Lighting sources" },
  { n: 6, title: "Review" },
  { n: 7, title: "Install" },
];

const DOCS = {
  haAuth: "https://www.home-assistant.io/docs/authentication/",
  sshAddon: "https://github.com/hassio-addons/app-ssh",
  appdaemonAddon: "https://github.com/hassio-addons/addon-appdaemon",
  appdaemonHttp: "https://appdaemon.readthedocs.io/en/latest/ADDON.html",
  hue: "https://developers.meethue.com/develop/get-started-2/",
  wled: "https://kno.wled.ge/basics/web-ui/",
  hyperhdr: "https://github.com/awawa-dev/HyperHDR",
};

// ---------------------------------------------------------------------------
// tiny DOM + API helpers
// ---------------------------------------------------------------------------

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child);
  }
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, headers: { "X-Scene-Studio-Token": TOKEN } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (err) {
    throw new Error(`cannot reach the installer server: ${err.message}`);
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    /* non-JSON error body */
  }
  if (!response.ok) {
    throw new Error(payload.error || `${method} ${path} failed (HTTP ${response.status})`);
  }
  return payload;
}

let toastTimer = null;
function toast(message) {
  document.querySelectorAll(".toast").forEach((node) => node.remove());
  document.body.append(el("div", { class: "toast", text: message }));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => document.querySelectorAll(".toast").forEach((node) => node.remove()), 6000);
}

function answers() {
  return state.session ? state.session.answers : {};
}

async function saveAnswers(patch) {
  try {
    const previous = answers();
    const payload = await api("/api/answers", { method: "POST", body: patch });
    const topologyKeys = ["ha_url", "ssh_host", "ssh_user", "ssh_port", "appdaemon_config_root", "addon_root_choice", "appdaemon_http_url", "ha_config_filesystem_confirmed"];
    if (topologyKeys.some((key) => previous[key] !== payload.answers[key])) state.probes = {};
    state.session.answers = payload.answers;
    state.plan = null; // any answer change invalidates the review plan
    updateStatusbar();
  } catch (err) {
    toast(err.message);
  }
}

async function runProbes(names, statusRow) {
  if (statusRow) statusRow.replaceChildren(pill("pending", "checking…"));
  try {
    const payload = await api("/api/probe", { method: "POST", body: { names } });
    Object.assign(state.probes, payload.results);
    if (payload.answers) state.session.answers = payload.answers; // probes may refine answers
    if (statusRow) {
      const worst = worstStatus(names);
      statusRow.replaceChildren(pill(worst, worst === "ok" ? "all checks passed" : worst));
    }
    renderStep(); // refresh cards with results
  } catch (err) {
    if (statusRow) statusRow.replaceChildren(pill("fail", "check failed"));
    toast(err.message);
  }
}

function worstStatus(names) {
  const rank = { fail: 0, warn: 1, skipped: 2, ok: 3, pending: 4 };
  let worst = "ok";
  for (const name of names) {
    const status = (state.probes[name] || {}).status || "pending";
    if (rank[status] < rank[worst]) worst = status;
  }
  return worst;
}

function pill(status, label) {
  const text = label || { ok: "ok", warn: "warning", fail: "failed", skipped: "skipped", pending: "pending" }[status] || status;
  return el("span", { class: `pill ${status}`, text });
}

function probeCard(label, name) {
  const result = state.probes[name];
  const card = el("div", { class: "card result-card" }, el("div", { class: "status-row" }, el("strong", { text: label })));
  if (!result) {
    card.append(el("div", { class: "status-detail", text: "not checked yet" }));
    return card;
  }
  card.querySelector(".status-row").append(pill(result.status));
  card.append(el("div", { class: "status-detail", text: result.detail }));
  return card;
}

function field(labelText, input, hintText) {
  const wrap = el("div", { class: "field" });
  const id = `f-${labelText.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`;
  input.id = id;
  wrap.append(el("label", { for: id, text: labelText }), input);
  if (hintText) wrap.append(el("div", { class: "hint", text: hintText }));
  return wrap;
}

function textInput(value, placeholder, onCommit) {
  const input = el("input", { type: "text", value: value || "", placeholder: placeholder || "" });
  input.addEventListener("change", () => onCommit(input.value.trim()));
  return input;
}

function actions(...buttons) {
  return el("div", { class: "actions" }, ...buttons);
}

function checkRow({ key, title, desc, checked, onChange }) {
  const input = el("input", { type: "checkbox" });
  input.checked = !!checked;
  input.addEventListener("change", () => onChange(input.checked));
  return el(
    "label",
    { class: "check-row" },
    input,
    el("div", { class: "grow" }, el("div", { class: "title", text: title }), el("div", { class: "desc", text: desc }))
  );
}

function inferredHttpUrl() {
  const a = answers();
  if (a.appdaemon_http_url) return String(a.appdaemon_http_url);
  let host = (a.ssh_host || (a.ha_url ? new URL(a.ha_url).hostname : "")).toString().replace(/^.*@/, "").replace(/:\d+$/, "");
  if (!host && a.ha_url) {
    try {
      host = new URL(a.ha_url).hostname;
    } catch (err) { /* ignore */ }
  }
  return `http://${host}:5050`;
}

// ---------------------------------------------------------------------------
// step views
// ---------------------------------------------------------------------------

function stepHeading(n, title, lede) {
  return [el("h1", { text: `${n}. ${title}` }), el("p", { class: "step-lede", text: lede })];
}

function renderStep1(container) {
  const ws = state.session.workstation || {};
  container.append(
    ...stepHeading(1, "This computer", "The installer runs from here and contacts only what you enter next. Nothing has been probed or changed yet."),
  );
  const checks = el("div", { class: "card-grid" });
  const rows = [
    ["Python", ws.python, `python ${ws.python}`, true],
    ["ssh client", ws.ssh, ws.ssh || "install the OpenSSH client", !!ws.ssh],
    ["PowerShell 7 (pwsh)", ws.pwsh, ws.pwsh ? `pwsh ${ws.pwsh}` : "required to run the installer — get it from https://aka.ms/powershell", !!ws.pwsh],
  ];
  for (const [title, , detail, ok] of rows) {
    const card = el("div", { class: "card result-card" }, el("div", { class: "status-row" }, el("strong", { text: title }), pill(ok ? "ok" : "fail")));
    card.append(el("div", { class: "status-detail", text: detail }));
    checks.append(card);
  }
  container.append(checks);
  container.append(
    el("div", { class: "card" },
      el("h3", { text: "What will be installed" }),
      el("div", { class: "value", text: `Scene Studio ${state.session.version} · ${state.session.bundle_mode} bundle` }),
      el("div", { class: "hint" },
        el("span", { text: `wizard: ${state.session.wizard}  ·  dashboard card: ${state.session.card_dist_present ? "included" : "prebuilt card missing (the card step will be skipped)"} · ` }),
        el("a", { href: DOCS.haAuth, target: "_blank", rel: "noreferrer", text: "HA tokens" }), " · ",
        el("a", { href: DOCS.sshAddon, target: "_blank", rel: "noreferrer", text: "ssh add-on" }), " · ",
        el("a", { href: DOCS.appdaemonAddon, target: "_blank", rel: "noreferrer", text: "AppDaemon add-on" }),
      ),
    ),
  );
  const tokenNote = state.session.env_tokens.ha_token
    ? "SCENE_STUDIO_HA_TOKEN is set in this terminal's environment — it will be used for the install."
    : "You will paste a Home Assistant long-lived access token in the next step. It is kept in this installer's memory only and never written to disk.";
  container.append(el("div", { class: "note", text: tokenNote }));
}

function renderStep2(container) {
  container.append(
    ...stepHeading(2, "Home Assistant", "The address is the same one you use to open Home Assistant in a browser."),
  );
  const a = answers();
  const urlInput = textInput(a.ha_url, "http://homeassistant.local:8123", (value) => saveAnswers({ ha_url: value }));
  container.append(field("Home Assistant API address", urlInput, "e.g. http://homeassistant.local:8123"));

  if (state.session.env_tokens.ha_token) {
    container.append(el("div", { class: "note", text: "SCENE_STUDIO_HA_TOKEN is set in the environment; the installer will use it." }));
  } else {
    const tokenInput = el("input", { type: "password", placeholder: "paste a long-lived access token", autocomplete: "off" });
    const saveBtn = el(
      "button",
      {
        text: "Store token",
        onclick: async () => {
          try {
            await api("/api/secrets", { method: "POST", body: { ha_token: tokenInput.value } });
            tokenInput.value = "";
            state.session.secrets_entered.ha_token = true;
            toast("Token stored in memory only.");
            renderStep();
          } catch (err) {
            toast(err.message);
          }
        },
      },
    );
    container.append(
      field("Home Assistant access token", el("div", { style: null }, tokenInput), "Create one in Home Assistant: click your profile (bottom left) > Security > Long-lived access tokens — https://www.home-assistant.io/docs/authentication/ · kept in memory only, never written to disk."),
      actions(saveBtn),
    );
  }

  const row = el("div", { class: "status-row" });
  container.append(
    actions(
      el("button", {
        class: "primary",
        text: "Test connection",
        onclick: async () => {
          await saveAnswers({ ha_url: urlInput.value.trim() });
          await runProbes(["ha_api", "ha_auth"], row);
          if (state.probes.ha_auth?.status === "ok") await runProbes(["topology"], row);
        },
      }),
      row,
    ),
    probeCard("Reachability", "ha_api"),
    probeCard("Token", "ha_auth"),
  );
}

function renderStep3(container) {
  const detected = state.probes.topology;
  const topology = detected?.data?.topology || state.session.topology;
  const ready = detected?.status === "ok";
  container.append(...stepHeading(3, "Detected setup", "For an AppDaemon add-on, SSH accesses Home Assistant's add-on files. AppDaemon does not need its own SSH service."));
  const checks = el("div", { class: "card" }, el("h3", { text: ready ? "Home Assistant + AppDaemon detected" : "Automatic setup detection was incomplete." }));
  for (const [label, key] of [["SSH filesystem access", "ssh"], ["AppDaemon add-on", "appdaemon_root"], ["AppDaemon HTTP", "appdaemon_http"], ["Restart capability", "supervisor"]]) {
    checks.append(el("div", { class: "status-row" }, pill(state.probes[key]?.status || "pending"), el("span", { text: label })));
  }
  if (topology?.appdaemon_config_root) checks.append(el("div", { text: `AppDaemon config: ${topology.appdaemon_config_root}` }));
  checks.append(el("button", { text: "Detect setup", onclick: () => runProbes(["topology"]) }));
  if (ready) checks.append(el("button", { class: "primary", text: "Continue to lighting sources", onclick: () => { state.step = 5; renderStep(); } }));
  container.append(checks);
  const advanced = el("details", { class: "fold", ...(ready ? {} : { open: "" }) }, el("summary", { text: "Configure advanced topology" }));
  container.append(advanced);
  renderAdvancedTopology(advanced);
}

function renderAdvancedTopology(container) {
  container.append(
    ...stepHeading(3, "Filesystem access", "Use the SSH host that can access the AppDaemon add-on files. Custom and split hosts are supported."),
  );
  const a = answers();
  const haHost = a.ha_url ? (() => { try { return new URL(a.ha_url).hostname; } catch (err) { return ""; } })() : "";
  const sshHost = textInput(a.ssh_host, haHost || "homeassistant.local", (value) => saveAnswers({ ssh_host: value }));
  const sshUser = textInput(a.ssh_user, "empty = your ssh default", (value) => saveAnswers({ ssh_user: value }));
  const portInput = el("input", { type: "number", min: "1", max: "65535", value: a.ssh_port || 22 });
  portInput.addEventListener("change", () => saveAnswers({ ssh_port: Number(portInput.value) || 22 }));
  const httpInput = textInput(a.appdaemon_http_url, `${inferredHttpUrl()}  (inferred — leave empty to use)`, (value) => saveAnswers({ appdaemon_http_url: value || null }));

  container.append(
    field("Home Assistant filesystem host (SSH)", sshHost, "key authentication required — the installer cannot type passwords; https://github.com/hassio-addons/app-ssh"),
    field("ssh username (optional)", sshUser),
    field("ssh port", portInput),
    field("Scene Studio / AppDaemon HTTP address (optional)", httpInput, "AppDaemon serves HTTP on its dashboard port (default 5050). Leave empty to infer from the ssh host — asked again only if inference is unreachable; https://appdaemon.readthedocs.io/en/latest/ADDON.html"),
  );

  const row = el("div", { class: "status-row" });
  container.append(
    actions(
      el("button", {
        class: "primary",
        text: "Test connections",
        onclick: async () => {
          await saveAnswers({
            ssh_host: sshHost.value.trim(),
            ssh_user: sshUser.value.trim(),
            ssh_port: Number(portInput.value) || 22,
            appdaemon_http_url: httpInput.value.trim() || null,
          });
          runProbes(["topology"], row);
        },
      }),
      row,
    ),
    probeCard("ssh target", "ssh"),
    probeCard("Scene Studio HTTP", "appdaemon_http"),
    checkRow({ title: "This SSH host also exposes the configuration of the Home Assistant API above", desc: "For split hosts or SSH aliases only: confirm the filesystem belongs to this Home Assistant, not a different installation. HA markers and write access are checked independently before offering the card.", checked: a.ha_config_filesystem_confirmed, onChange: (value) => saveAnswers({ ha_config_filesystem_confirmed: value }).then(() => runProbes(["topology"])) }),
  );
  renderStep4(container);
}

function renderStep4(container) {
  container.append(
    ...stepHeading(4, "AppDaemon config directory", "Where the Scene Studio app and Workbench live on the target — usually the AppDaemon add-on slug under /addon_configs."),
  );
  const a = answers();
  const rootProbe = state.probes.appdaemon_root;
  const candidates = (rootProbe && rootProbe.data && rootProbe.data.candidates) || [];
  const selected = a.appdaemon_config_root;
  const rootInput = textInput(selected, "/addon_configs/a0d7b954_appdaemon", (value) => saveAnswers({ appdaemon_config_root: value || null }));
  const storeInput = textInput(a.store_root, "/config/scene_studio_store", (value) => saveAnswers({ store_root: value }));

  const detectRow = el("div", { class: "status-row" });
  container.append(
    actions(
      el("button", {
        class: "primary",
        text: "Detect automatically",
        onclick: async () => {
          await saveAnswers({ appdaemon_config_root: rootInput.value.trim() || null });
          runProbes(["appdaemon_root"], detectRow);
        },
      }),
      detectRow,
    ),
  );

  if (candidates.length > 1) {
    const list = el("div", { class: "card radio-list" }, el("h3", { text: "Several AppDaemon installations were found — pick one:" }));
    for (const candidate of candidates) {
      const radio = el("input", { type: "radio", name: "addon-root" });
      radio.checked = candidate === selected;
      radio.addEventListener("change", () => {
        saveAnswers({ appdaemon_config_root: candidate }).then(() => runProbes(["remote_state"]));
      });
      list.append(el("label", {}, radio, el("span", { text: candidate })));
    }
    container.append(list);
  }

  container.append(
    field(
      "AppDaemon config directory on the target",
      rootInput,
      "absolute path, e.g. /addon_configs/a0d7b954_appdaemon",
    ),
    field(
      "Scene Studio data directory on the target",
      storeInput,
      "your scenes, rooms, and registry are stored here and survive removal",
    ),
    probeCard("AppDaemon config directory", "appdaemon_root"),
  );

  if (selected) {
    const readRow = el("div", { class: "status-row" });
    const remote = state.probes.remote_state;
    container.append(
      actions(
        el("button", {
          class: "primary",
          text: "Read the target (read-only)",
          onclick: async () => {
            await saveAnswers({ appdaemon_config_root: rootInput.value.trim() || null, store_root: storeInput.value.trim() });
            runProbes(["remote_state"], readRow);
          },
        }),
        readRow,
      ),
    );
    if (remote) {
      const kind = remote.data.install_kind === "upgrade" ? "Existing install — this will be an UPGRADE" : "Fresh install";
      const modeNote =
        remote.data.install_kind === "upgrade"
          ? `current runtime mode "${remote.data.runtime_mode}" is preserved`
          : "the backend will come up in registry_admin mode (provider writes blocked until you deliberately enable them)";
      const stateCard = el("div", { class: "card result-card" },
        el("div", { class: "status-row" }, el("strong", { text: "Target state" }), pill(remote.status)),
        el("div", { class: "status-detail", text: `${kind} · ${modeNote}` }),
        el("div", { class: "value", text: `apps.yaml: ${remote.data.apps_yaml_present ? "present" : "absent (will be created)"} · secrets.yaml: ${remote.data.secrets_yaml_present ? "present" : "absent (will be created)"}` }),
      );
      container.append(stateCard);
    }
  }
}

function renderStep5(container) {
  container.append(
    ...stepHeading(5, "Lighting sources", "Pick what Scene Studio should discover. At least one source is required; discovery reads, it never writes during setup."),
  );
  const providers = answers().providers;
  let hueHostInput = null;
  let wledHostInput = null;
  let hdrHostInput = null;

  container.append(
    checkRow({
      title: "Home Assistant lights",
      desc: "reads through the AppDaemon plugin; no separate address",
      checked: providers.ha_light,
      onChange: (value) => { saveAnswers({ providers: { ha_light: value } }).then(renderStep); },
    }),
  );

  const hueCard = checkRow({
    title: "Philips Hue",
    desc: "bridge address from the Hue app (Settings > My Hue bridge) — https://developers.meethue.com/develop/get-started-2/",
    checked: providers.hue.enabled,
    onChange: (value) => { saveAnswers({ providers: { hue: { enabled: value } } }).then(renderStep); },
  });
  container.append(hueCard);
  if (providers.hue.enabled) {
    const sub = el("div", { class: "subfields" });
    hueHostInput = textInput(providers.hue.host, "e.g. 192.168.1.2 or bridge.local", (value) => saveAnswers({ providers: { hue: { host: value } } }));
    sub.append(field("Hue bridge address", hueHostInput));
    if (providers.hue.bridge_id) {
      sub.append(el("div", { class: "note", text: `bridge id ${providers.hue.bridge_id} (detected; used for first-run fixture adoption)` }));
    }
    const pairRow = el("div", { class: "status-row" });
    const pairBtn = el("button", {
      class: "primary",
      text: "Pair with the bridge",
      onclick: async () => {
        if (!providers.hue.host) {
          toast("Enter the Hue bridge address first.");
          return;
        }
        pairRow.replaceChildren(pill("pending", "press the LINK button on the bridge, then wait…"));
        try {
          const result = await api("/api/hue_pair", { method: "POST", body: { host: providers.hue.host } });
          if (result.ok) {
            state.probes.hue_paired = { status: "ok", detail: "paired with the bridge" };
            pairRow.replaceChildren(pill("ok", "paired"));
            renderStep();
          } else {
            pairRow.replaceChildren(pill("fail", "pairing failed"));
            toast(result.error || "Pairing failed.");
          }
        } catch (err) {
          pairRow.replaceChildren(pill("fail", "pairing failed"));
          toast(err.message);
        }
      },
    });
    const keyRow = el("div", { class: "status-row" });
    const keyInput = el("input", { type: "password", placeholder: "paste an existing application key", autocomplete: "off" });
    const keyBtn = el("button", {
      text: "Use existing key",
      onclick: async () => {
        try {
          await api("/api/secrets", { method: "POST", body: { hue_key: keyInput.value } });
          keyInput.value = "";
          state.session.secrets_entered.hue_key = true;
          toast("Hue application key stored in memory only.");
        } catch (err) {
          toast(err.message);
        }
      },
    });
    sub.append(
      el("div", { class: "note", text: "Pairing presses nothing by itself: press the LINK button on the bridge first, then click Pair. Alternatively paste an existing application key. Either way the key is kept in memory only and lands in the target's secrets.yaml." }),
      actions(pairBtn, pairRow),
      actions(keyBtn, keyInput, keyRow),
    );
    container.append(sub);
  }

  const wledCard = checkRow({
    title: "WLED",
    desc: "the controller's web UI address, e.g. http://wled-1234.local — https://kno.wled.ge/basics/web-ui/",
    checked: providers.wled.enabled,
    onChange: (value) => { saveAnswers({ providers: { wled: { enabled: value } } }).then(renderStep); },
  });
  container.append(wledCard);
  if (providers.wled.enabled) {
    const sub = el("div", { class: "subfields" });
    wledHostInput = textInput(providers.wled.host, "e.g. 192.168.1.50 or wled-1234.local", (value) => saveAnswers({ providers: { wled: { host: value } } }));
    sub.append(field("WLED controller address", wledHostInput));
    container.append(sub);
  }

  const hdrCard = checkRow({
    title: "hyperHDR (light-sync contention)",
    desc: "optional integration; address is host:port, e.g. tv.local:8090 — https://github.com/awawa-dev/HyperHDR",
    checked: providers.hyperhdr.enabled,
    onChange: (value) => { saveAnswers({ providers: { hyperhdr: { enabled: value } } }).then(renderStep); },
  });
  container.append(hdrCard);
  if (providers.hyperhdr.enabled) {
    const sub = el("div", { class: "subfields" });
    hdrHostInput = textInput(providers.hyperhdr.host, "e.g. tv.local:8090", (value) => saveAnswers({ providers: { hyperhdr: { host: value } } }));
    sub.append(field("hyperHDR address (host:port)", hdrHostInput));
    container.append(sub);
  }

  const anyEnabled = providers.ha_light || providers.hue.enabled || providers.wled.enabled || providers.hyperhdr.enabled;
  const row = el("div", { class: "status-row" });
  container.append(
    actions(
      el("button", {
        class: "primary",
        text: "Test reachability",
        onclick: async () => {
          const patch = { providers: {} };
          if (providers.hue.enabled) patch.providers.hue = { host: hueHostInput ? hueHostInput.value.trim() : null };
          if (providers.wled.enabled) patch.providers.wled = { host: wledHostInput ? wledHostInput.value.trim() : null };
          if (providers.hyperhdr.enabled) patch.providers.hyperhdr = { host: hdrHostInput ? hdrHostInput.value.trim() : null };
          await saveAnswers(patch);
          runProbes(["providers"], row);
        },
      }),
      row,
    ),
    probeCard("Lighting sources", "providers"),
  );
  if (!anyEnabled) {
    container.append(el("div", { class: "note warn", text: "At least one lighting source must be enabled before continuing." }));
  }
}

function renderStep6(container) {
  container.append(
    ...stepHeading(6, "Review", "Everything below was validated read-only. Nothing is written until you confirm in the next step."),
  );

  const row = el("div", { class: "status-row" });
  container.append(actions(el("button", { class: "primary", text: state.plan ? "Rebuild the plan" : "Build the plan", onclick: () => buildPlan(row) }), row));

  if (!state.plan) {
    container.append(el("div", { class: "note", text: "Click “Build the plan” to validate the answers and generate the exact apps.yaml change." }));
    return;
  }
  const plan = state.plan;
  if (!plan.ok) {
    const errorCard = el("div", { class: "card" }, el("h3", { text: "The deployment configuration is invalid" }), el("ul", { class: "error-list" }));
    const list = errorCard.querySelector("ul");
    for (const message of plan.errors) list.append(el("li", { text: message }));
    container.append(errorCard);
    return;
  }

  const summary = el("div", { class: "card" }, el("h3", { text: "Deployment topology" }), el("table", { class: "summary" }));
  const table = summary.querySelector("table");
  for (const rowDef of plan.summary) {
    table.append(el("tr", {}, el("td", { text: rowDef.label }), el("td", { text: rowDef.value })));
  }
  container.append(summary);

  if (plan.notes && plan.notes.length) {
    for (const note of plan.notes) container.append(el("div", { class: "note warn", text: note }));
  }

  const blockCard = el("div", { class: "card" }, el("h3", { text: "apps.yaml change (a clearly marked block; everything else is untouched)" }));
  const blockPre = el("pre", { class: "block" });
  for (const line of plan.block.split("\n")) {
    const span = el("span", { text: line + "\n" });
    if (line.startsWith("# >>>") || line.startsWith("# <<<")) span.className = "marker";
    blockPre.append(span);
  }
  blockCard.append(blockPre);
  if (plan.hue_secret_entry) {
    blockCard.append(el("div", { class: "note", text: 'secrets.yaml: adds the entry "scene_studio_hue_app_key" — the value is never displayed, printed, or stored in any answers file.' }));
  }
  if (plan.apps_yaml_present && plan.merged_preview) {
    const fold = el("details", { class: "fold" }, el("summary", { text: "Preview the full merged apps.yaml" }), el("pre", { class: "block", text: plan.merged_preview }));
    blockCard.append(fold);
    if (!plan.apps_yaml_changed) {
      blockCard.append(el("div", { class: "note", text: "apps.yaml is already up to date — no configuration change is needed (the backend/Workbench trees are still deployed)." }));
    }
  }
  blockCard.append(el("div", { class: "note", text: "The installer backs up apps.yaml/secrets.yaml before changing them and restores them automatically if the install fails. Deployer-level backups stay on the target under <appdaemon config>/backups/." }));
  container.append(blockCard);

  const cardToggle = checkRow({
    title: "Install the Scene Studio Home Assistant dashboard card",
    desc: state.session.card_dist_present
      ? "deploys the prebuilt card to /config/www/scene-studio-card/ (Scene Studio-owned path; no dashboard is modified)"
      : "prebuilt card is missing in this bundle — the card step will be skipped",
    checked: answers().install_ha_card,
    onChange: (value) => { saveAnswers({ install_ha_card: value }).then(() => buildPlan()); },
  });
  if (plan.card_auto_available) container.append(cardToggle);
  else container.append(el("div", { class: "note", text: "Automatic HA card deployment is unavailable because HA config filesystem access is not confirmed. Copy home-assistant/scene-studio-card/dist/scene-studio-card.js to HA /config/www/scene-studio-card/ and register /local/scene-studio-card/scene-studio-card.js as a JavaScript module; use custom:scene-studio-card." }));
}

async function buildPlan(statusRow) {
  if (statusRow) statusRow.replaceChildren(pill("pending", "validating…"));
  try {
    state.plan = await api("/api/plan", { method: "POST", body: {} });
    if (statusRow) statusRow.replaceChildren(pill(state.plan.ok ? "ok" : "fail", state.plan.ok ? "plan ready" : "validation failed"));
  } catch (err) {
    state.plan = { ok: false, errors: [err.message] };
    if (statusRow) statusRow.replaceChildren(pill("fail", "plan failed"));
  }
  renderStep();
}

function renderStep7(container) {
  container.append(
    ...stepHeading(7, "Install", "The guided installer runs exactly as it does on the command line: review, bounded apps.yaml/secrets.yaml change with backups, validated deployers, health checks, and automatic rollback on failure."),
  );

  if (state.run.status === "running" || state.run.status === "success" || state.run.status === "failed") {
    renderRunView(container);
    return;
  }

  const planOk = state.plan && state.plan.ok;
  const consent = el("input", { type: "checkbox" });
  const confirmInput = el("input", { type: "text", placeholder: "type INSTALL to confirm", autocomplete: "off" });
  const installBtn = el("button", { class: "danger", text: "Install now", disabled: "" });
  const refresh = () => {
    installBtn.disabled = !(planOk && consent.checked && confirmInput.value === "INSTALL");
  };
  consent.addEventListener("change", refresh);
  confirmInput.addEventListener("input", refresh);

  container.append(
    el("div", { class: "card" },
      el("h3", { text: "Before you continue" }),
      el("div", { class: "status-detail", text: planOk
        ? "The plan was built and validated. Installing will: write the marked scene_studio block into apps.yaml (backed up first), optionally add the named Hue secret, deploy the backend (AppDaemon restarts) and Workbench through the validated deployers, and optionally the dashboard card."
        : "Build and validate the plan in step 6 first — the Install step stays disabled until the plan is ready." }),
    ),
    el("div", { class: "card" },
      el("label", { class: "check-row" }, consent, el("div", { class: "grow" }, el("div", { class: "title", text: "I have reviewed the changes shown in step 6 and approve them" }))),
      el("div", { class: "field" }, confirmInput),
      el("div", { class: "note", text: "This is the same explicit confirmation the command-line wizard asks for (type INSTALL). The install is performed by the same validated installer the CLI wizard uses." }),
    ),
    actions(installBtn),
  );

  installBtn.addEventListener("click", async () => {
    installBtn.disabled = true;
    try {
      await api("/api/apply", { method: "POST", body: { confirm: "INSTALL" } });
      state.run = { status: "running", seen: 0, support_report: null, returncode: null };
      renderStep();
      startPolling();
    } catch (err) {
      toast(err.message);
      installBtn.disabled = false;
    }
  });
}

function renderRunView(container) {
  const statusRow = el("div", { class: "status-row" }, el("strong", { text: "Install progress" }));
  const console_ = el("div", { class: "log-console", id: "log-console" });
  container.append(el("div", { class: "card" }, statusRow), console_);

  const refreshStatusPill = () => {
    const label = { running: "running…", success: "installed", failed: "failed", idle: "not started" }[state.run.status] || state.run.status;
    const kind = { running: "pending", success: "ok", failed: "fail", idle: "skipped" }[state.run.status] || "pending";
    statusRow.replaceChildren(el("strong", { text: "Install progress" }), pill(kind, label));
    document.getElementById("statusbar-run").textContent = {
      running: "install running…", success: "install finished — Scene Studio is installed",
      failed: "install FAILED — see the log and the support report", idle: "no install running",
    }[state.run.status] || "";
  };
  state.runRefreshPill = refreshStatusPill;
  refreshStatusPill();

  // rebuild the whole console from the server's ring buffer (re-renders must
  // not lose the installer's earlier output)
  replayLog();
  if (state.run.status === "running") {
    startPolling();
  } else {
    renderFinalPanel(container, state.run.status === "success");
  }
}

function appendLogLines(lines) {
  const console_ = document.getElementById("log-console");
  if (!console_) return 0;
  let last = null;
  for (const line of lines) {
    if (line.n <= state.run.seen) continue;
    const classes = ["log-line"];
    if (line.text.startsWith("==>")) classes.push("step-header");
    if (/error|failed|Traceback/i.test(line.text)) classes.push("error-line");
    console_.append(el("div", { class: classes.join(" ") }, el("span", { class: "log-time", text: line.t }), el("span", { text: line.text })));
    last = line;
  }
  if (last) {
    state.run.seen = last.n;
    console_.scrollTop = console_.scrollHeight;
  }
  return lines.length;
}

async function replayLog() {
  try {
    const snapshot = await api("/api/run?since=0");
    state.run.status = snapshot.status;
    state.run.returncode = snapshot.returncode;
    state.run.support_report = snapshot.support_report || state.run.support_report;
    // a replay is a full rebuild: drop the stale cursor and any old content,
    // otherwise lines already counted by an earlier poll are filtered out
    // even though this (re-created) console has never shown them
    state.run.seen = 0;
    const console_ = document.getElementById("log-console");
    if (console_) console_.replaceChildren();
    appendLogLines(snapshot.lines);
    if (state.runRefreshPill) state.runRefreshPill();
  } catch (err) {
    /* transient — polling retries */
  }
}

function renderFinalPanel(container, success) {
  const a = answers();
  const workbenchUrl = `${inferredHttpUrl()}/local/scene_studio/`;
  const card = el("div", { class: "card" });
  if (success) {
    card.append(
      el("h3", { text: "Scene Studio is installed" }),
      el("div", { class: "status-detail", text: "Next: confirm the apps.yaml block is merged (the installer did it), open the Workbench, run discovery, adopt your fixtures, and author your first scene. Provider writes stay blocked in registry_admin mode until you deliberately enable them." }),
      el("div", { class: "actions" },
        el("button", { class: "primary", text: "Open the Workbench", onclick: () => window.open(workbenchUrl, "_blank") }),
        el("button", { text: "Start over", onclick: resetSession }),
      ),
      el("div", { class: "value", text: workbenchUrl }),
    );
  } else {
    card.append(
      el("h3", { text: "The install failed" }),
      el("div", { class: "status-detail", text: "Any configuration the installer changed was restored automatically (see the log above). A sanitized support report — versions, endpoints, probes, and a redacted log, never tokens or keys — can be sent to the maintainer:" }),
    );
    if (state.run.support_report) {
      const pathInput = el("input", { type: "text", value: state.run.support_report, readonly: "" });
      card.append(
        el("div", { class: "actions" },
          el("button", { text: "Copy report path", onclick: () => { navigator.clipboard.writeText(state.run.support_report).then(() => toast("Support report path copied.")); } }),
        ),
        el("div", { class: "value", text: state.run.support_report }),
      );
    } else {
      card.append(el("div", { class: "note", text: "No support report was produced — the failure happened before the report stage. The full log above is the evidence." }));
    }
    card.append(
      el("div", { class: "actions" },
        el("button", { text: "Start over", onclick: resetSession }),
      ),
    );
  }
  container.append(card);
}

async function resetSession() {
  try {
    await api("/api/reset", { method: "POST", body: {} });
    state.probes = {};
    state.plan = null;
    state.run = { status: "idle", seen: 0, support_report: null, returncode: null };
    stopPolling();
    state.step = 1;
    await loadSession();
    renderStep();
  } catch (err) {
    toast(err.message);
  }
}

// ---------------------------------------------------------------------------
// install log polling
// ---------------------------------------------------------------------------

function startPolling() {
  stopPolling();
  pollOnce();
  state.pollTimer = setInterval(pollOnce, 1000);
}

function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function pollOnce() {
  let snapshot;
  try {
    snapshot = await api(`/api/run?since=${state.run.seen}`);
  } catch (err) {
    return; // transient — the next tick retries
  }
  state.run.status = snapshot.status;
  state.run.returncode = snapshot.returncode;
  state.run.support_report = snapshot.support_report || state.run.support_report;
  appendLogLines(snapshot.lines);
  if (state.runRefreshPill) state.runRefreshPill();
  if (snapshot.status !== "running") {
    stopPolling();
    renderStep();
    updateStatusbar();
  } else if (document.getElementById("statusbar-run")) {
    document.getElementById("statusbar-run").textContent = `install running… (log line ${state.run.seen})`;
  }
}

// ---------------------------------------------------------------------------
// shell: stepper, statusbar, boot
// ---------------------------------------------------------------------------

function renderStepper() {
  const nav = document.getElementById("stepper");
  nav.replaceChildren();
  for (const step of STEPS) {
    const item = el(
      "button",
      { class: `step-item${step.n === state.step ? " active" : ""}${step.n < state.step ? " done" : ""}`, onclick: () => { state.step = step.n; renderStep(); } },
      el("span", { class: "num", text: String(step.n) }),
      el("span", { text: step.title }),
    );
    nav.append(item);
  }
}

function updateStatusbar() {
  const run = document.getElementById("statusbar-run");
  const meta = document.getElementById("statusbar-hint");
  const a = answers();
  const parts = [];
  if (a.ha_url) parts.push(a.ha_url);
  if (a.ssh_host) parts.push(a.ssh_user ? `${a.ssh_user}@${a.ssh_host}` : a.ssh_host);
  meta.textContent = parts.join(" · ");
  if (state.run.status === "running") run.textContent = "install running…";
  else if (state.run.status === "success") run.textContent = "install finished — Scene Studio is installed";
  else if (state.run.status === "failed") run.textContent = "install FAILED — see the Install step";
  else run.textContent = "no install running";
}

function renderTopMeta() {
  const node = document.getElementById("top-meta");
  if (!state.session) return;
  node.replaceChildren(
    el("div", { text: `Scene Studio ${state.session.version} · ${state.session.bundle_mode} bundle` }),
    el("div", { text: state.session.bundle_root }),
  );
}

function renderStep() {
  renderStepper();
  const container = document.getElementById("content");
  container.replaceChildren();
  const view = el("div", { class: "step-view" });
  container.append(view);
  const renderer = [renderStep1, renderStep2, renderStep3, renderStep4, renderStep5, renderStep6, renderStep7][state.step - 1];
  try {
    renderer(view);
  } catch (err) {
    view.replaceChildren(el("div", { class: "card" }, el("h3", { text: "Rendering problem" }), el("div", { class: "status-detail", text: String(err) })));
  }
  updateStatusbar();
}

async function loadSession() {
  state.session = await api("/api/session");
  state.probes = state.session.probes || {};
  // refresh mid-install: jump straight to the run console
  if (state.run.status === "idle") {
    const runStatus = state.session.run.status;
    if (runStatus === "running" || runStatus === "success" || runStatus === "failed") {
      state.run.status = runStatus;
      state.run.support_report = state.session.run.support_report;
      state.run.returncode = state.session.run.returncode;
      state.step = 7;
      startPolling();
    }
  }
  renderTopMeta();
}

async function boot() {
  const content = document.getElementById("content");
  if (!TOKEN) {
    content.replaceChildren(
      el("div", { class: "card" },
        el("h3", { text: "Missing session token" }),
        el("div", { class: "status-detail", text: "Open the installer UI with the full URL the server printed when it started (it ends with ?token=…)." }),
      ),
    );
    return;
  }
  try {
    await loadSession();
  } catch (err) {
    content.replaceChildren(
      el("div", { class: "card" },
        el("h3", { text: "Cannot reach the installer server" }),
        el("div", { class: "status-detail", text: err.message }),
      ),
    );
    return;
  }
  renderStep();
}

boot();
