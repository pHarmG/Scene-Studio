#!/usr/bin/env node
/**
 * Rendered-path browser regression for the Scene Studio Workbench
 * (R5C corrective pass). Dependency-free: spawns headless Chrome with
 * --remote-debugging-port=0, serves the PRODUCTION BUILD (dist/) over a
 * local static server, and drives the real UI over the Chrome DevTools
 * Protocol using Node's built-in WebSocket.
 *
 * Unlike scripts/smoke.mjs (DOM-free), this exercises the actual rendered
 * event path — real clicks on the panel buttons inside shadow DOM — to
 * prove the things pure helpers cannot:
 *
 *   1. Overview's playback panel lifecycle controls actually perform
 *      (active -> Pause, paused -> Resume, active/paused -> Stop) through
 *      the store routing, with the sibling session untouched.
 *   2. Scenes' playback panel still routes the same controls.
 *   3. Scene-row labels never contradict themselves (the status glyph must
 *      not say "playing" while the runtime tag says Paused/Orphaned/N
 *      sessions) across active / paused / orphaned / multi-session states.
 *   4. No console errors; Stage-2 shell basics intact (nav, pill, System
 *      drawer, Discovery).
 *
 * Prereq: a current production build (`npm run build` — the npm `browser`
 * script chains it). Exit code 0 = all checks passed.
 */

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const DIST = resolve(join(here, "..", "dist"));

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  join(process.env.LOCALAPPDATA || "", "Google\\Chrome\\Application\\chrome.exe"),
].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let passed = 0;
let failed = 0;
function check(label, condition, detail = "") {
  if (condition) {
    passed += 1;
    console.log(`  ok  ${label}`);
  } else {
    failed += 1;
    console.error(`FAIL  ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

/** Order-sensitive list equality (palette order assertions). */
function deepEqualList(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// --- static file server for the production build -------------------------

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
};

function serveDist() {
  const server = createServer((req, res) => {
    const urlPath = decodeURIComponent(new URL(req.url, "http://127.0.0.1").pathname);
    const rel = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
    const file = resolve(join(DIST, rel));
    if (!file.startsWith(DIST) || !existsSync(file)) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("not found");
      return;
    }
    const body = readFileSync(file);
    res.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream" });
    res.end(body);
  });
  return new Promise((resolvePromise) => {
    server.listen(0, "127.0.0.1", () => resolvePromise({ server, port: server.address().port }));
  });
}

// --- headless Chrome over CDP (built-in WebSocket, no dependencies) -------

async function launchChrome() {
  const chromePath = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!chromePath) throw new Error("Chrome not found (set CHROME_PATH)");
  const userDataDir = mkdtempSync(join(tmpdir(), "ss-browser-"));
  const child = spawn(
    chromePath,
    [
      "--headless=new",
      "--remote-debugging-port=0",
      `--user-data-dir=${userDataDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--window-size=1440,900",
      "about:blank",
    ],
    { stdio: ["ignore", "pipe", "pipe"] }
  );
  const portFile = join(userDataDir, "DevToolsActivePort");
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (existsSync(portFile)) {
      const content = readFileSync(portFile, "utf8").trim().split("\n");
      if (content.length >= 2 && content[0].trim()) {
        return { child, userDataDir, port: content[0].trim(), browserPath: content[1].trim() };
      }
    }
    await sleep(150);
  }
  child.kill();
  throw new Error("Chrome DevTools endpoint never became ready");
}

function connectWs(url) {
  return new Promise((resolvePromise, rejectPromise) => {
    const ws = new WebSocket(url);
    ws.addEventListener("open", () => resolvePromise(ws));
    ws.addEventListener("error", () => rejectPromise(new Error(`websocket connect failed: ${url}`)));
  });
}

async function main() {
  if (!existsSync(join(DIST, "index.html"))) {
    throw new Error("dist/index.html missing — run `npm run build` first (npm run browser chains it)");
  }
  console.log("Scene Studio Workbench rendered-path browser regression");

  const { server, port: httpPort } = await serveDist();
  const chrome = await launchChrome();
  const consoleErrors = [];
  let ws = null;
  let msgId = 0;
  const pending = new Map();
  let sessionId = null;

  try {
    ws = await connectWs(`ws://127.0.0.1:${chrome.port}${chrome.browserPath}`);
    let loadFired = null;
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolvePromise, rejectPromise } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rejectPromise(new Error(`${msg.error.message || "cdp error"}`));
        else resolvePromise(msg.result || {});
      } else if (msg.method === "Page.loadEventFired") {
        if (loadFired) loadFired();
      } else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
        consoleErrors.push(msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
      } else if (msg.method === "Runtime.exceptionThrown") {
        consoleErrors.push(msg.params.exceptionDetails?.exception?.description || "uncaught page exception");
      }
    });
    const send = (method, params = {}) =>
      new Promise((resolvePromise, rejectPromise) => {
        const id = ++msgId;
        pending.set(id, { resolvePromise, rejectPromise });
        ws.send(JSON.stringify(sessionId ? { id, method, params, sessionId } : { id, method, params }));
      });

    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    ({ sessionId } = await send("Target.attachToTarget", { targetId, flatten: true }));
    await send("Runtime.enable");
    await send("Page.enable");
    // Wait for the document load event before evaluating: the bundle is a
    // deferred module script, so by then ss-app is defined and store.init()
    // is the only thing still in flight (the boot poll below handles it).
    const loaded = new Promise((r) => {
      loadFired = r;
    });
    await send("Page.navigate", { url: `http://127.0.0.1:${httpPort}/` });
    await Promise.race([loaded, sleep(5000)]);

    /** Evaluate an async page function; returns its JSON value. */
    const evaluate = async (asyncFnExpression) => {
      const res = await send("Runtime.evaluate", {
        expression: asyncFnExpression,
        awaitPromise: true,
        returnByValue: true,
        userGesture: true,
      });
      if (res.exceptionDetails) {
        throw new Error(`page eval failed: ${(res.exceptionDetails.exception?.description || res.exceptionDetails.text || "").slice(0, 400)}`);
      }
      return res.result?.value;
    };

    await evaluate(`window.dom = (el) => (el == null ? el : (el.shadowRoot ?? el)); true`);

    // 0. boot: mock client + first status rendered
    const booted = await evaluate(`(async () => {
      const t0 = Date.now();
      while (Date.now() - t0 < 10000) {
        const app = document.querySelector("ss-app");
        if (app && app.store && app.store.state && app.store.state.status) break;
        await new Promise((r) => setTimeout(r, 60));
      }
      const app = document.querySelector("ss-app");
      if (!app || !app.store) return { booted: false };
      return {
        booted: true,
        mode: app.store.state.conn.mode,
        scenario: app.store.state.scenarioId,
        live: app.store.state.conn.mode === "mock",
      };
    })()`);
    check("browser: app boots in mock mode against the production build", !!booted && booted.booted && booted.live, JSON.stringify(booted));

    // 0.5. A transient notice (live connection / command feedback) must not
    // consume the shell's flexible content row. Regression for the original
    // four-second "blank upper panel" at load: the conditional notice used
    // to land in the 1fr grid track and displaced main into an implicit row.
    const noticeLayout = await evaluate(`(async () => {
      const app = document.querySelector("ss-app");
      app.store.state.notice = { text: "Live: same-origin", level: "ok", at: Date.now() };
      app.requestUpdate();
      await app.updateComplete;
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
      const root = dom(app);
      const notice = root.querySelector(".notice");
      const main = root.querySelector("main");
      const noticeBox = notice.getBoundingClientRect();
      const mainBox = main.getBoundingClientRect();
      app.store.state.notice = null;
      app.requestUpdate();
      return {
        noticeHeight: Math.round(noticeBox.height),
        mainTopAfterNotice: Math.round(mainBox.top),
        noticeBottom: Math.round(noticeBox.bottom),
        mainHeight: Math.round(mainBox.height),
      };
    })()`);
    check(
      "shell: a transient notice stays compact and main remains in the flexible viewport row",
      !!noticeLayout &&
        noticeLayout.noticeHeight > 0 &&
        Math.abs(noticeLayout.mainTopAfterNotice - noticeLayout.noticeBottom) <= 1 &&
        noticeLayout.mainHeight > 400,
      JSON.stringify(noticeLayout)
    );

    // 0.6. List panels keep their rows edge-to-edge, but an optional heading
    // must use the same left inset as the row content. This specifically
    // covers Overview's Playback label.
    const playbackHeadingLayout = await evaluate(`(async () => {
      const app = document.querySelector("ss-app");
      const overview = dom(app).querySelector("ss-view-overview");
      const playback = overview && dom(overview).querySelector("ss-playback-panel");
      const panel = playback && dom(playback).querySelector("ss-panel");
      const heading = panel && dom(panel).querySelector("h3");
      const counts = playback && dom(playback).querySelector(".head");
      const firstCount = counts && counts.querySelector("span");
      if (!panel || !heading || !firstCount) return { rendered: false };
      const panelBox = panel.getBoundingClientRect();
      const headingText = document.createRange();
      headingText.selectNodeContents(heading);
      const headingBox = headingText.getBoundingClientRect();
      const countsBox = firstCount.getBoundingClientRect();
      return {
        rendered: true,
        headingInset: Math.round(headingBox.left - panelBox.left),
        countsInset: Math.round(countsBox.left - panelBox.left),
      };
    })()`);
    check(
      "overview: Playback heading is inset to align with its count row",
      !!playbackHeadingLayout &&
        playbackHeadingLayout.rendered === true &&
        playbackHeadingLayout.headingInset >= 8 &&
        Math.abs(playbackHeadingLayout.headingInset - playbackHeadingLayout.countsInset) <= 1,
      JSON.stringify(playbackHeadingLayout)
    );

    // 1. Overview playback panel: real-click Pause -> Resume -> Stop on one
    //    session of two; the sibling session must stay untouched.
    const overviewFlow = await evaluate(`(async () => {
      const store = document.querySelector("ss-app").store;
      await store.setScenario("multi-session");
      store.setView("overview");
      const root = () => dom(dom(app()).querySelector("ss-view-overview"));
      const app = () => document.querySelector("ss-app");
      const panelRoot = () => dom(root().querySelector("ss-playback-panel"));
      const rows = () => [...panelRoot().querySelectorAll("ss-playback-session")];
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      if (!(await waitFor(() => rows().length >= 2 && rows().every((r) => dom(r).querySelector("ss-action-menu")))))
        return { fail: "panel did not render 2 live session rows" };
      const firstId = rows()[0].session.session_id;
      const secondId = rows()[1].session.session_id;
      const dumpRow = (id) => {
        const row = rows().find((r) => r.session.session_id === id);
        if (!row) return { missing: true, ids: rows().map((r) => r.session.session_id) };
        return [...dom(row).querySelectorAll("ss-action-menu")]
          .flatMap((m) => [...dom(m).querySelectorAll("button")])
          .map((b) => ({ text: b.textContent.trim(), disabled: b.disabled }));
      };
      // Click a rendered, enabled button — waits for Lit to finish rendering
      // the row's shadow content (element hosts connect one microtask before
      // their shadow content exists, so a naive click can race).
      const clickWhenReady = async (id, label, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          const row = rows().find((r) => r.session.session_id === id);
          const btn = row
            ? [...dom(row).querySelectorAll("ss-action-menu")]
                .flatMap((m) => [...dom(m).querySelectorAll("button")])
                .find((b) => (b.getAttribute("aria-label") || "").startsWith(label) && !b.disabled)
            : null;
          if (btn) {
            btn.click();
            return true;
          }
          await new Promise((r) => setTimeout(r, 60));
        }
        return false;
      };
      // active -> Pause
      if (!(await clickWhenReady(firstId, "Pause")))
        return { fail: "no enabled Pause button on the active session", firstId, dump: dumpRow(firstId) };
      const paused = await waitFor(() => rows().find((r) => r.session.session_id === firstId && r.session.state === "paused"));
      const pauseNotice = app().store.state.notice && app().store.state.notice.text;
      // paused -> Resume
      if (!(await clickWhenReady(firstId, "Resume"))) return { fail: "no enabled Resume button after pause" };
      const resumed = await waitFor(() => rows().find((r) => r.session.session_id === firstId && r.session.state === "active"));
      const resumeNotice = app().store.state.notice && app().store.state.notice.text;
      // active -> Stop
      if (!(await clickWhenReady(firstId, "Stop"))) return { fail: "no enabled Stop button after resume" };
      const stopped = await waitFor(() => {
        const pb = app().store.state.status.playback;
        const row = rows().find((r) => r.session.session_id === firstId);
        return pb.counts.active === 1 && pb.counts.stopped === 2 && !row ? pb : null;
      });
      const stopNotice = app().store.state.notice && app().store.state.notice.text;
      const other = rows().find((r) => r.session.session_id === secondId);
      return {
        paused: !!paused,
        pauseNotice,
        resumed: !!resumed,
        resumeNotice,
        stopped: !!stopped,
        stopNotice,
        otherStillActive: !!other && other.session.state === "active",
        countsLine: panelRoot().querySelector(".head").textContent.replace(/\\s+/g, " ").trim(),
      };
    })()`);
    check("overview panel: Pause on the rendered active session performs (playback.pause routed)", !!overviewFlow && overviewFlow.paused === true, JSON.stringify(overviewFlow));
    check(
      "overview panel: pause notice is scene-named from refreshed status",
      !!overviewFlow && /Playback paused: Aurora Flow/.test(overviewFlow.pauseNotice || ""),
      String(overviewFlow && overviewFlow.pauseNotice)
    );
    check("overview panel: Resume on the rendered paused session performs (playback.resume routed)", !!overviewFlow && overviewFlow.resumed === true);
    check("overview panel: Stop on the rendered session performs (playback.stop routed, row leaves the live panel)", !!overviewFlow && overviewFlow.stopped === true);
    check(
      "overview panel: stop notice is scene-named",
      !!overviewFlow && /Playback stopped: Aurora Flow/.test(overviewFlow.stopNotice || ""),
      String(overviewFlow && overviewFlow.stopNotice)
    );
    check("overview panel: the sibling session is untouched (targeted mutation only)", !!overviewFlow && overviewFlow.otherStillActive === true);
    check(
      "overview panel: counts header reflects the round trip (1 playing, 2 recent stopped)",
      !!overviewFlow && /1 playing/.test(overviewFlow.countsLine || "") && /2 recent stopped/.test(overviewFlow.countsLine || ""),
      String(overviewFlow && overviewFlow.countsLine)
    );

    // 2. Scenes playback panel still routes the same controls.
    const scenesFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      store.setView("scenes");
      const panelRoot = () => dom(dom(dom(app()).querySelector("ss-view-scenes")).querySelector("ss-playback-panel"));
      const rows = () => [...panelRoot().querySelectorAll("ss-playback-session")];
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      if (!(await waitFor(() => rows().length >= 1 && rows().every((r) => dom(r).querySelector("ss-action-menu")))))
        return { fail: "scenes panel did not render" };
      const id = rows()[0].session.session_id;
      const clickWhenReady = async (label, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          const row = rows().find((r) => r.session.session_id === id);
          const btn = row
            ? [...dom(row).querySelectorAll("ss-action-menu")]
                .flatMap((m) => [...dom(m).querySelectorAll("button")])
                .find((b) => (b.getAttribute("aria-label") || "").startsWith(label) && !b.disabled)
            : null;
          if (btn) {
            btn.click();
            return true;
          }
          await new Promise((r) => setTimeout(r, 60));
        }
        return false;
      };
      if (!(await clickWhenReady("Pause"))) return { fail: "no enabled Pause in the scenes panel" };
      const paused = await waitFor(() => rows().find((r) => r.session.session_id === id && r.session.state === "paused"));
      if (!(await clickWhenReady("Resume"))) return { fail: "no enabled Resume in the scenes panel" };
      const resumed = await waitFor(() => rows().find((r) => r.session.session_id === id && r.session.state === "active"));
      return { paused: !!paused, resumed: !!resumed };
    })()`);
    check("scenes panel: Pause/Resume still route through the shared seam", !!scenesFlow && scenesFlow.paused && scenesFlow.resumed, JSON.stringify(scenesFlow));

    // 3. Scene-row labels across live states: the glyph must never claim
    //    "playing"; the runtime tag owns the state wording.
    const rowLabels = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const sceneRows = () => [...dom(dom(app()).querySelector("ss-view-scenes")).querySelectorAll("ss-scene-row")];
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const readRows = () =>
        sceneRows().map((row) => {
          const glyph = dom(row).querySelector(".name ss-status-glyph");
          const tag = dom(row).querySelector(".runtime-tag");
          return {
            id: row.scene.id,
            glyphLabel: glyph ? String(glyph.label) : null,
            tag: tag ? tag.textContent.trim() : "",
          };
        });
      const out = {};
      // Content-aware readiness: a scene row's shadow content (glyph, tag)
      // lands one microtask after the host connects, so wait for the tag.
      const auroraRowReady = (predicate) =>
        sceneRows().some(
          (r) => r.scene.id === "aurora_flow" && predicate(r) && dom(r).querySelector(".runtime-tag")
        );
      // active
      await store.setScenario("dynamic-active");
      await store.setView("scenes");
      await waitFor(() => auroraRowReady(() => true));
      out.active = readRows();
      // paused (setup via the store; the row rendering is what is asserted)
      const activeSession = app().store.state.status.playback.sessions.find((s) => s.state === "active");
      await store.sendCommand({ command: "playback.pause", session_id: activeSession.session_id });
      await waitFor(() => auroraRowReady((r) => r.sessionSummary && r.sessionSummary.states[0] === "paused"));
      out.paused = readRows();
      // orphaned + retained stop
      await store.setScenario("playback-orphaned");
      await waitFor(() => auroraRowReady((r) => r.sessionSummary && r.sessionSummary.states[0] === "orphaned"));
      out.orphaned = readRows();
      // multiple sessions (same scene, disjoint sets)
      await store.setScenario("multi-session");
      await waitFor(() => auroraRowReady((r) => r.sessionSummary && r.sessionSummary.count === 2));
      out.multi = readRows();
      return out;
    })()`);
    const auroraTag = (collection) => {
      const row = (collection || []).find((r) => r.id === "aurora_flow");
      return row ? row.tag : null;
    };
    const auroraGlyph = (collection) => {
      const row = (collection || []).find((r) => r.id === "aurora_flow");
      return row ? row.glyphLabel : null;
    };
    check("row labels (active): runtime tag reads Playing", auroraTag(rowLabels && rowLabels.active) === "Playing", JSON.stringify(rowLabels && rowLabels.active));
    check("row labels (paused): runtime tag reads Paused", auroraTag(rowLabels && rowLabels.paused) === "Paused", JSON.stringify(rowLabels && rowLabels.paused));
    check("row labels (orphaned): runtime tag reads Orphaned", auroraTag(rowLabels && rowLabels.orphaned) === "Orphaned", JSON.stringify(rowLabels && rowLabels.orphaned));
    check("row labels (multi): runtime tag reads 2 sessions", auroraTag(rowLabels && rowLabels.multi) === "2 sessions", JSON.stringify(rowLabels && rowLabels.multi));
    const allRows = [].concat(rowLabels ? [rowLabels.active, rowLabels.paused, rowLabels.orphaned, rowLabels.multi] : []).flat();
    check(
      "row labels: no live row's status glyph claims 'playing' (no contradiction with Paused/Orphaned/N sessions)",
      allRows.length > 0 && allRows.every((r) => (r.glyphLabel || "").toLowerCase() !== "playing"),
      JSON.stringify(allRows.filter((r) => (r.glyphLabel || "").toLowerCase() === "playing"))
    );
    check(
      "row labels: no row spells out 'ready' (healthy and live rows are a status dot only)",
      allRows.length > 0 &&
        allRows.every((r) => (r.glyphLabel || "").toLowerCase() !== "ready") &&
        allRows.filter((r) => (r.tag || "").length > 0).every((r) => (r.glyphLabel || "") === ""),
      JSON.stringify(allRows.filter((r) => (r.glyphLabel || "").toLowerCase() === "ready" || ((r.tag || "") && (r.glyphLabel || "") !== "")))
    );
    check(
      "row labels: every live row still carries a non-empty runtime tag (state text present)",
      ["active", "paused", "orphaned", "multi"].every((k) => (auroraTag(rowLabels && rowLabels[k]) || "").length > 0)
    );

    // 4. Stage-2 shell basics + console cleanliness.
    const shell = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const root = () => dom(app());
      const waitFor = async (fn, ms = 5000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const tabs = [...root().querySelectorAll("nav.tabs button")].map((b) => b.textContent.trim());
      const pill = root().querySelector(".status-pill");
      root().querySelector(".icon-btn[title^='System']").click();
      const drawer = await waitFor(() => root().querySelector("ss-drawer[floating]"));
      const drawerOpens = !!drawer;
      const build = await fetch('build-info.json').then(r => r.json());
      const versionRenders = root().querySelector('.product-version')?.textContent === 'v' + build.version;
      const identityRenders = drawer?.querySelector('.build-label')?.textContent.includes(build.short_sha);
      const updateStates = [];
      for (const [state, label] of [['unchecked','Not checked'], ['checking','Checking…'], ['current','Current'], ['available','Update available'], ['unavailable','Check unavailable'], ['error','Error']]) {
        app().store.state.updateCheck = { state, message: 'test', latest_version: state === 'available' ? '9.0.0' : null, release_url: 'https://github.com/pHarmG/Scene-Studio/releases' };
        app().requestUpdate();
        await app().updateComplete;
        const section = root().querySelector('.update-section');
        updateStates.push(section.querySelector('.update-state')?.textContent === label &&
          section.querySelector('button').disabled === (state === 'checking') &&
          (state !== 'available' || section.textContent.includes('Release notes')));
      }
      app().store.state.updateCheck = null;
      app().requestUpdate();
      await app().updateComplete;
      root().querySelector('.update-section button').click();
      const checkAction = !!(await waitFor(() => app().store.state.updateCheck?.state === 'unavailable'));
      if (drawer) drawer.querySelector(".system-head .close").click();
      await waitFor(() => !root().querySelector("ss-drawer[floating]"));
      app().store.setView("discovery");
      const discovery = await waitFor(() => root().querySelector("ss-view-discovery"));
      return {
        tabs,
        pillOk: !!pill,
        drawerOpens,
        versionRenders, identityRenders, updateStates, checkAction,
        discoveryRenders: !!discovery,
      };
    })()`);
    check(
      "stage-2: nav tabs, status pill, System drawer, and Discovery intact",
      !!shell && JSON.stringify(shell.tabs) === JSON.stringify(["Overview", "Scenes", "Fixtures"]) && shell.pillOk && shell.drawerOpens && shell.discoveryRenders,
      JSON.stringify(shell)
    );
    check('product: canonical version and exact build render in shell/System', shell?.versionRenders && shell?.identityRenders, JSON.stringify(shell));
    check('updates: all states render and explicit Check action works', shell?.updateStates?.every(Boolean) && shell?.checkAction, JSON.stringify(shell));
    const updateExecution = await evaluate(`(async () => {
      const app = document.querySelector('ss-app');
      const root = app;
      root.querySelector('.icon-btn[title^="System"]').click();
      await app.updateComplete;
      app.store.state.updateCheck = { state: 'available', latest_version: '0.1.1', release_url: 'https://github.com/pHarmG/Scene-Studio/releases/tag/v0.1.1' };
      app.requestUpdate(); await app.updateComplete;
      const available = !!root.querySelector('.update-apply');
      root.querySelector('.update-apply').click(); await app.updateComplete;
      const dialog = root.querySelector('.update-confirmation');
      const confirmation = dialog?.textContent.includes('Installed: v') && dialog?.textContent.includes('Target: v0.1.1') && dialog?.textContent.includes('preserved') && !app.store.state.updateExecution;
      root.querySelector('.update-confirm').click();
      await new Promise(resolve => setTimeout(resolve, 50)); await app.updateComplete;
      const mockRejected = app.store.state.updateExecution?.state === 'failed';
      const stages = [];
      for (const [state, label] of [['downloading','Downloading…'],['verifying','Verifying…'],['activating','Installing…'],['restarting','Restarting…'],['reconnecting','Waiting for Scene Studio…'],['verifying_new_build','Verifying update…'],['succeeded','Update complete'],['rollback','Rolling back…'],['failed','Update failed']]) {
        app.store.state.updateExecution = { state, message: 'controlled executor fixture' };
        app.requestUpdate(); await app.updateComplete;
        stages.push(root.querySelector('.update-progress')?.textContent.includes(label));
      }
      app.store.state.updateExecution = {state:'failed',rolled_back:true,message:'Previous healthy build restored.'};
      app.requestUpdate(); await app.updateComplete;
      const rollback = root.querySelector('.update-progress')?.textContent.includes('Update rolled back');
      app.store.state.updateCheck = null; app.store.state.updateExecution = null;
      root.querySelector('ss-drawer[floating] .system-head .close').click();
      await app.updateComplete;
      return {available,confirmation,mockRejected,stages,rollback};
    })()`);
    check('updates: available, explicit confirmation, all execution phases and rollback render', updateExecution?.available && updateExecution?.confirmation && updateExecution?.mockRejected && updateExecution?.stages.every(Boolean) && updateExecution?.rollback, JSON.stringify(updateExecution));
    // 5. WLED controller clustering (post-R5 visual polish): the 6
    //    wled_seg_* fixtures collapse into one <ss-fixture-cluster>, and a
    //    segment inside it is still individually selectable/inspectable by
    //    its real fixture id — the same select-fixture contract as an
    //    unclustered row.
    const clusterFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-fixtures"));
      const cluster = await waitFor(() => root().querySelector("ss-fixture-cluster"));
      if (!cluster) return { fail: "no ss-fixture-cluster rendered for the WLED segments" };
      const details = dom(cluster).querySelector("details");
      details.open = true;
      const rows = await waitFor(() => {
        const rs = [...dom(cluster).querySelectorAll("ss-fixture-row")];
        return rs.length ? rs : null;
      });
      const memberCount = rows ? rows.length : 0;
      const healthIsOnlyInNameColumn = (rows || []).every((row) => {
        const glyph = dom(row).querySelector(".name ss-status-glyph");
        // Live fixture color-state pass (user feedback): a routine "ready"
        // no longer prints its text label in the row (dot only, so the
        // aura leads instead) — only non-ready health stays labeled there.
        const expectedLabel = row.fixture.health === "ready" ? "" : row.fixture.health;
        return glyph && glyph.label === expectedLabel && dom(row).querySelectorAll(".status").length === 0;
      });
      const target = (rows || []).find((r) => r.fixture && r.fixture.id === "wled_seg_2");
      if (!target) return { fail: "wled_seg_2 row not found inside the cluster", memberIds: (rows || []).map((r) => r.fixture && r.fixture.id) };
      dom(target).querySelector(".row").click();
      await waitFor(() => app().store.state.selection && app().store.state.selection.id === "wled_seg_2");
      return { memberCount, healthIsOnlyInNameColumn, selection: app().store.state.selection };
    })()`);
    check(
      "fixtures: the 6 WLED segments render as one collapsed cluster",
      !!clusterFlow && clusterFlow.memberCount === 6,
      JSON.stringify(clusterFlow)
    );
    check(
      "fixtures: health appears once beside the fixture name (no duplicate trailing health column)",
      !!clusterFlow && clusterFlow.healthIsOnlyInNameColumn === true,
      JSON.stringify(clusterFlow)
    );
    check(
      "fixtures: selecting a segment inside the cluster addresses the real fixture (wled_seg_2)",
      !!clusterFlow && !!clusterFlow.selection && clusterFlow.selection.type === "fixture" && clusterFlow.selection.id === "wled_seg_2",
      JSON.stringify(clusterFlow && clusterFlow.selection)
    );

    // 5a-2. Product-polish pass: grouped-row column alignment (Task 1) and
    // truthful, disabled-aware capability indicators (Task 3).
    const fixturePolish = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-fixtures"));
      const plainRow = await waitFor(() => root().querySelector("ss-fixture-row"));
      const cluster = await waitFor(() => root().querySelector("ss-fixture-cluster"));
      if (!plainRow || !cluster) return { fail: "missing plain row or cluster" };
      const plainCols = getComputedStyle(dom(plainRow).querySelector(".row")).gridTemplateColumns;
      const summaryCols = getComputedStyle(dom(cluster).querySelector("summary")).gridTemplateColumns;
      const plainLeft = dom(plainRow).querySelector(".row").getBoundingClientRect().left;
      const summaryLeft = dom(cluster).querySelector("summary").getBoundingClientRect().left;
      const details = dom(cluster).querySelector("details");
      details.open = true;
      const memberRows = await waitFor(() => {
        const rs = [...dom(cluster).querySelectorAll("ss-fixture-row")];
        return rs.length ? rs : null;
      });
      const memberLeft = memberRows ? dom(memberRows[0]).querySelector(".row").getBoundingClientRect().left : null;
      // g_strip: color_xy + gradient + dynamic_native true, color_temp true.
      const gStrip = await waitFor(() => [...root().querySelectorAll("ss-fixture-row")].find((r) => r.fixture && r.fixture.id === "g_strip"));
      const capsOf = (row) => {
        const ci = row ? dom(row).querySelector("ss-capability-indicators") : null;
        return ci ? [...dom(ci).querySelectorAll(".cap")].map((el) => el.classList.contains("on")) : null;
      };
      const gStripCaps = capsOf(gStrip);
      // A WLED segment has no color_xy/color_temp/gradient, but is dynamic_native.
      const wled = memberRows ? memberRows.find((r) => r.fixture && r.fixture.id === "wled_seg_0") : null;
      const wledCaps = capsOf(wled);
      // Disabled fixture: every capability indicator renders ghosted, never "on".
      await store.setScenario("disabled-fixture");
      store.setView("fixtures");
      const disabledRow = await waitFor(() => [...root().querySelectorAll("ss-fixture-row")].find((r) => r.fixture && r.fixture.id === "double_strip"));
      const disabledCapsArr = capsOf(disabledRow);
      const disabledCapsOn = disabledCapsArr ? disabledCapsArr.some(Boolean) : null;
      const disabledCapsCount = disabledCapsArr ? disabledCapsArr.length : 0;
      const disabledRowClass = disabledRow ? dom(disabledRow).querySelector(".row").className : null;
      return {
        plainCols, summaryCols, colsEqual: plainCols === summaryCols,
        plainLeft, summaryLeft, memberLeft,
        leftAligned: plainLeft === summaryLeft && plainLeft === memberLeft,
        gStripCaps, wledCaps, disabledCapsOn, disabledCapsCount, disabledRowClass,
      };
    })()`);
    check(
      "fixtures: a cluster summary's columns line up exactly with a plain row's columns",
      !!fixturePolish && fixturePolish.colsEqual === true,
      JSON.stringify(fixturePolish)
    );
    check(
      "fixtures: a plain row, the cluster summary, and a nested member row all start at the same x (no block indent)",
      !!fixturePolish && fixturePolish.leftAligned === true,
      JSON.stringify(fixturePolish)
    );
    check(
      "fixtures: g_strip's capability indicators are truthfully all 'on' (color, temp, gradient, dynamic)",
      !!fixturePolish && Array.isArray(fixturePolish.gStripCaps) && fixturePolish.gStripCaps.length === 4 && fixturePolish.gStripCaps.every(Boolean),
      JSON.stringify(fixturePolish && fixturePolish.gStripCaps)
    );
    check(
      "fixtures: a WLED segment's capability indicators truthfully show color/temp/gradient off, dynamic on",
      !!fixturePolish &&
        Array.isArray(fixturePolish.wledCaps) &&
        fixturePolish.wledCaps.length === 4 &&
        fixturePolish.wledCaps[0] === false &&
        fixturePolish.wledCaps[1] === false &&
        fixturePolish.wledCaps[2] === false &&
        fixturePolish.wledCaps[3] === true,
      JSON.stringify(fixturePolish && fixturePolish.wledCaps)
    );
    check(
      "fixtures: a disabled fixture never shows an 'on' capability indicator and its row is marked disabled",
      !!fixturePolish && fixturePolish.disabledCapsCount === 4 && fixturePolish.disabledCapsOn === false && /\bdisabled\b/.test(fixturePolish.disabledRowClass || ""),
      JSON.stringify(fixturePolish)
    );

    // 5a-2. User feedback: a routine "ready" status shouldn't compete with
    // the row's live-state aura for attention — the glyph goes dot-only.
    // A real problem status (missing/degraded/disabled/...) stays labeled
    // so the list is still scannable for what actually needs a look; full
    // health text always remains one click away in the inspector.
    const statusLabelFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-fixtures"));
      const rows = await waitFor(() => {
        const found = [...root().querySelectorAll("ss-fixture-row")];
        return found.length ? found : null;
      });
      const glyphLabel = (rowSet, id) => {
        const row = rowSet.find((r) => r.fixture && r.fixture.id === id);
        const glyph = row ? dom(row).querySelector(".name ss-status-glyph") : null;
        return glyph ? { label: glyph.label, health: row.fixture.health, ariaLabel: dom(glyph).querySelector(".dot").getAttribute("aria-label") } : null;
      };
      const readyResult = glyphLabel(rows, "g_strip");

      // "all-healthy" re-enables Double Strip (its whole point) — use the
      // scenario that keeps it disabled verbatim for the other half.
      await store.setScenario("disabled-fixture");
      const disabledRows = await waitFor(() => {
        const found = [...root().querySelectorAll("ss-fixture-row")];
        return found.length ? found : null;
      });
      const disabledResult = glyphLabel(disabledRows, "double_strip");
      return { ready: readyResult, disabled: disabledResult };
    })()`);
    check(
      "fixtures: a routine 'ready' fixture shows a dot only, no text label",
      !!statusLabelFlow && statusLabelFlow.ready && statusLabelFlow.ready.health === "ready" && statusLabelFlow.ready.label === "",
      JSON.stringify(statusLabelFlow)
    );
    check(
      "fixtures: the dot still carries the real status for assistive tech even with no visible label",
      !!statusLabelFlow && statusLabelFlow.ready && statusLabelFlow.ready.ariaLabel === "ok"
    );
    check(
      "fixtures: a disabled fixture keeps its visible text label (not dot-only)",
      !!statusLabelFlow && statusLabelFlow.disabled && statusLabelFlow.disabled.health === "disabled" && statusLabelFlow.disabled.label === "disabled",
      JSON.stringify(statusLabelFlow)
    );

    // 5b. Same-binding registry reconcile inspector flow.
    const reconcileFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("registry-stale");
      store.setView("fixtures");
      store.select({ type: "fixture", id: "custom_gradient" });
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const inspectorRoot = () => dom(dom(app()).querySelector("ss-inspector"));
      const reviewBtn = await waitFor(() =>
        [...inspectorRoot().querySelectorAll("button")].find((b) => b.textContent.trim() === "Review update")
      );
      if (!reviewBtn) {
        return { fail: "Review update missing", text: inspectorRoot().textContent };
      }
      reviewBtn.click();
      const applyBtn = await waitFor(() =>
        [...inspectorRoot().querySelectorAll("button")].find((b) => b.textContent.trim() === "Apply registry update")
      );
      if (!applyBtn) {
        return { fail: "Apply registry update missing after review", text: inspectorRoot().textContent };
      }
      applyBtn.click();
      const ready = await waitFor(() => {
        const fx = store.state.fixtures && store.state.fixtures.fixtures.find((f) => f.id === "custom_gradient");
        return fx && fx.health === "ready" ? fx : null;
      });
      store.select({ type: "fixture", id: "double_strip" });
      const disabledText = await waitFor(() => {
        const text = inspectorRoot().textContent || "";
        return text.includes("Intentionally disabled") && text.includes("Excluded from scenes") ? text : null;
      });
      store.setView("overview");
      const overview = await waitFor(() => dom(app()).querySelector("ss-view-overview"));
      const ovText = overview ? overview.textContent : "";
      const pill = dom(app()).querySelector(".status-pill");
      return {
        ready: !!(ready && ready.binding && ready.binding.resource_id === "00000000-0000-4000-8000-000000000017"),
        disabledCopy: !!disabledText,
        overviewOmitsCustomGradientIssue: !/Custom Gradient (degraded|missing)/.test(ovText),
        overviewOmitsDoubleStripIssue: !/Double Strip (degraded|missing)/.test(ovText),
        pillText: pill ? pill.textContent.trim() : "",
      };
    })()`);
    check(
      "inspector: registry-stale Review/Apply leaves Custom Gradient ready on the same binding",
      !!reconcileFlow && reconcileFlow.ready === true,
      JSON.stringify(reconcileFlow)
    );
    check(
      "inspector: disabled Double Strip explains intentional exclusion",
      !!reconcileFlow && reconcileFlow.disabledCopy === true,
      JSON.stringify(reconcileFlow)
    );
    check(
      "overview: Custom Gradient and Double Strip are not Needs Attention issues after reconcile",
      !!reconcileFlow && reconcileFlow.overviewOmitsCustomGradientIssue && reconcileFlow.overviewOmitsDoubleStripIssue,
      JSON.stringify(reconcileFlow)
    );

    // 5b-2. Inspector facelift (2026-09-16): action buttons previously used
    // a `ss-btn` class that only exists in the document-level base.css,
    // which never reaches a shadow-DOM component — every inspector action
    // (Disable, Apply registry update, ...) silently rendered as an
    // unstyled native button. Pin that the button now actually carries the
    // app's own styling (a real border/background from tokens.css, not the
    // UA default) so this can't regress silently again.
    const inspectorButtonStyle = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      store.select({ type: "fixture", id: "g_strip" });
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const inspector = await waitFor(() => document.querySelector("ss-inspector"));
      const btn = await waitFor(() => [...dom(inspector).querySelectorAll("button")].find((b) => b.textContent.trim() === "Disable"));
      if (!btn) return { fail: "Disable button not found" };
      const cs = getComputedStyle(btn);
      return { backgroundColor: cs.backgroundColor, borderColor: cs.borderColor, borderStyle: cs.borderStyle, color: cs.color };
    })()`);
    check(
      "inspector: action buttons carry real app styling, not an unstyled native button",
      !!inspectorButtonStyle &&
        !inspectorButtonStyle.fail &&
        inspectorButtonStyle.borderStyle === "solid" &&
        inspectorButtonStyle.backgroundColor !== "rgba(0, 0, 0, 0)" &&
        inspectorButtonStyle.backgroundColor !== "",
      JSON.stringify(inspectorButtonStyle)
    );

    // 5b-3. User feedback (2026-09-16): "we have capability badges we can
    // use" — the fixture inspector's Capabilities fact used to be a plain
    // text chip string; it now reuses the SAME <ss-capability-indicators>
    // truthful icon badges the Fixtures list rows use, and the redundant
    // raw "Discovery: bound_ready"-style fact (duplicating the status
    // glyph / explanation callout, already preserved in Technical details)
    // was dropped from the primary facts card.
    const inspectorCapsFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      store.select({ type: "fixture", id: "g_strip" });
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const inspector = await waitFor(() => document.querySelector("ss-inspector"));
      const badges = await waitFor(() => {
        const ci = dom(inspector).querySelector("ss-capability-indicators");
        const caps = ci ? [...dom(ci).querySelectorAll(".cap")] : null;
        return caps && caps.length === 4 ? caps : null;
      });
      const factLabels = [...dom(inspector).querySelectorAll(".facts dt")].map((el) => el.textContent.trim());
      return {
        badgeCount: badges ? badges.length : 0,
        badgesOn: badges ? badges.map((el) => el.classList.contains("on")) : null,
        factLabels,
      };
    })()`);
    check(
      "inspector: Capabilities renders the 4 truthful capability badges, not a text chip string",
      !!inspectorCapsFlow && inspectorCapsFlow.badgeCount === 4 && inspectorCapsFlow.badgesOn.every(Boolean),
      JSON.stringify(inspectorCapsFlow)
    );
    check(
      "inspector: the redundant raw 'Discovery' status fact is gone from the primary facts card",
      !!inspectorCapsFlow &&
        !inspectorCapsFlow.factLabels.includes("Discovery") &&
        inspectorCapsFlow.factLabels.includes("Capabilities"),
      JSON.stringify(inspectorCapsFlow)
    );

    // 5b-4. User feedback (2026-09-16): "give capabilities description on
    // icon tap/click" — hover-only title tooltips never reach touch/mobile
    // users. Each capability glyph is now a real button that opens a
    // description popover naming the capability + its truthful state, and
    // (regression guard) tapping it must NOT also select the fixture row —
    // the badge is nested inside a selectable row and must stopPropagation
    // like every other in-row control (ss-overflow-menu's trigger does the
    // same for the identical reason).
    const capTapFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("fixtures");
      store.select(null);
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-fixtures"));
      const row = await waitFor(() => root().querySelectorAll("ss-fixture-row")[0]);
      if (!row) return { fail: "no fixture row found" };
      const ci = dom(row).querySelector("ss-capability-indicators");
      const capBtn = dom(ci).querySelectorAll(".cap")[0];
      capBtn.click();
      await new Promise((r) => setTimeout(r, 60));
      const popover = dom(ci).querySelector(".tip:popover-open");
      return {
        popoverOpened: !!popover,
        popoverText: popover ? popover.textContent.trim() : "",
        rowSelectedAsSideEffect: !!(store.state.selection && store.state.selection.type === "fixture"),
      };
    })()`);
    check(
      "fixtures: tapping a capability glyph opens a description popover",
      !!capTapFlow && capTapFlow.popoverOpened === true && capTapFlow.popoverText.length > 0,
      JSON.stringify(capTapFlow)
    );
    check(
      "fixtures: tapping a capability glyph does NOT also select the row (stopPropagation)",
      !!capTapFlow && capTapFlow.rowSelectedAsSideEffect === false,
      JSON.stringify(capTapFlow)
    );

    // 5b-5. Live fixture color-state pass: entering Fixtures samples
    // immediately, rows with a reported color get a right-side aura, an
    // off/disabled row gets none, the Scope control opens all memberships
    // without selecting the row, and leaving Fixtures stops the periodic
    // sampler (no further getFixtureState calls once the view moves away).
    const liveStateFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };

      store.setView("overview");
      await new Promise((r) => setTimeout(r, 30));
      // A prior test block earlier in this same suite run may already have
      // visited Fixtures (the last sample is deliberately kept in memory,
      // plan §4 "keep the last sample in memory for fast return") — so this
      // proves entering triggers a GENUINELY NEW fetch (the value changes),
      // not merely that some value already exists from a stale leftover.
      const sampledAtBeforeEnter = store.state.fixtureLiveState.sampledAt;

      store.setView("fixtures");
      const sampled = await waitFor(() => {
        const now = store.state.fixtureLiveState.sampledAt;
        return now && now !== sampledAtBeforeEnter ? now : null;
      });
      const root = () => dom(dom(app()).querySelector("ss-view-fixtures"));
      const rows = await waitFor(() => {
        const found = [...root().querySelectorAll("ss-fixture-row")];
        return found.length ? found : null;
      });
      const auraOf = (row) => dom(row).querySelector(".aura")?.getAttribute("style") || "";
      const onRow = rows.find((r) => r.fixture && r.fixture.id === "g_strip");
      const offRow = rows.find((r) => r.fixture && r.fixture.id === "custom_gradient");
      const disabledRow = rows.find((r) => r.fixture && r.fixture.enabled === false);

      // Scope control: open it, read its content, confirm no row selection.
      store.select(null);
      const scope = dom(onRow).querySelector("ss-scope-control");
      const scopeBtn = dom(scope).querySelector(".scope");
      scopeBtn.click();
      await new Promise((r) => setTimeout(r, 60));
      const scopePopover = dom(scope).querySelector(".tip:popover-open");
      const scopeText = scopePopover ? scopePopover.textContent.replace(/\\s+/g, " ").trim() : "";
      const scopeSelectedRowAsSideEffect = !!(store.state.selection && store.state.selection.type === "fixture");

      // Inspector "Current state" facts for the same fixture.
      store.select({ type: "fixture", id: "g_strip" });
      const inspector = await waitFor(() => document.querySelector("ss-inspector"));
      const factRow = await waitFor(() => {
        const dts = [...dom(inspector).querySelectorAll(".facts dt")].map((el) => el.textContent.trim());
        return dts.includes("Current state") ? dts : null;
      });

      // WLED cluster summary: an aggregate aura from already-loaded children,
      // never a new provider fetch.
      const cluster = root().querySelector("ss-fixture-cluster");
      const clusterAura = cluster ? dom(cluster).querySelector(".aura")?.getAttribute("style") || "" : "";

      // The periodic sampler actually ticks (~3s) while Fixtures stays the
      // active view, and actually STOPS once the view moves away — proven
      // by real elapsed time, not by inspecting a private timer handle.
      const sampledAtT0 = store.state.fixtureLiveState.sampledAt;
      await new Promise((r) => setTimeout(r, 3200));
      const sampledAtT1 = store.state.fixtureLiveState.sampledAt;
      const tickedWhileActive = sampledAtT1 !== sampledAtT0;

      store.setView("scenes");
      await new Promise((r) => setTimeout(r, 30));
      const sampledAtOnLeave = store.state.fixtureLiveState.sampledAt;
      await new Promise((r) => setTimeout(r, 3200));
      const sampledAtAfterWaitingWhileAway = store.state.fixtureLiveState.sampledAt;
      const stoppedAfterLeaving = sampledAtAfterWaitingWhileAway === sampledAtOnLeave;

      return {
        sampledAfterEnter: !!sampled,
        onRowHasAura: /background:/.test(auraOf(onRow)),
        offRowHasNoAura: !/background:/.test(auraOf(offRow)),
        disabledRowHasNoAura: disabledRow ? !/background:/.test(auraOf(disabledRow)) : true,
        scopeOpened: !!scopePopover,
        scopeText,
        scopeSelectedRowAsSideEffect,
        hasCurrentStateFact: !!factRow,
        clusterHasAura: /background:/.test(clusterAura),
        tickedWhileActive,
        stoppedAfterLeaving,
      };
    })()`);
    check("live state: entering Fixtures triggers a genuinely fresh sample immediately (no waiting for a timer tick)", liveStateFlow && liveStateFlow.sampledAfterEnter === true, JSON.stringify(liveStateFlow));
    check("live state: a fixture reporting a color gets a right-side aura", liveStateFlow && liveStateFlow.onRowHasAura === true);
    check("live state: an off fixture has no aura", liveStateFlow && liveStateFlow.offRowHasNoAura === true);
    check("live state: a disabled fixture never shows an aura", liveStateFlow && liveStateFlow.disabledRowHasNoAura === true);
    check("scope control: opens and lists every membership", liveStateFlow && liveStateFlow.scopeOpened === true && /Office/.test(liveStateFlow.scopeText) && /Whole House/.test(liveStateFlow.scopeText), JSON.stringify(liveStateFlow && liveStateFlow.scopeText));
    check("scope control: opening it does not also select the row (stopPropagation)", liveStateFlow && liveStateFlow.scopeSelectedRowAsSideEffect === false);
    check("inspector: a sampled fixture shows a 'Current state' fact", liveStateFlow && liveStateFlow.hasCurrentStateFact === true);
    check("WLED cluster: the collapsed summary shows its own aggregate aura from already-loaded children", liveStateFlow && liveStateFlow.clusterHasAura === true);
    check("live state: the sampler actually ticks on its cadence while Fixtures stays active", liveStateFlow && liveStateFlow.tickedWhileActive === true);
    check("live state: leaving Fixtures actually stops the periodic sampler (no further ticks)", liveStateFlow && liveStateFlow.stoppedAfterLeaving === true);

    // 5c. Swatch redesign (HA-native card parity pass): a multi-color
    // scene's swatch renders one smooth interpolated blend across the
    // ordered palette stops (matching the HA-native card's own blendStrip),
    // not discrete per-color segments — the fill lives in a CSS custom
    // property consumed by an inset ::after (never the same box as the
    // border-radius clip, which is what the corner-bleed fix depends on).
    const swatchFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const row = await waitFor(() => [...root().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "twilight"));
      if (!row) return { fail: "twilight row not found" };
      const band = dom(row).querySelector("ss-swatch-band");
      const bandEl = dom(band).querySelector(".band");
      const noSegs = dom(band).querySelectorAll(".seg").length === 0;
      const fill = bandEl ? bandEl.style.getPropertyValue("--ss-band-fill") : "";
      const palette = row.scene.palette || [];
      let expected;
      if (palette.length <= 1) {
        expected = palette[0] || "transparent";
      } else {
        const n = palette.length;
        const segment = 100 / n;
        const half = segment * 0.3;
        const stops = [palette[0] + " 0%"];
        for (let i = 0; i < n - 1; i += 1) {
          const boundary = (i + 1) * segment;
          stops.push(palette[i] + " " + (boundary - half) + "%");
          stops.push(palette[i + 1] + " " + (boundary + half) + "%");
        }
        stops.push(palette[n - 1] + " 100%");
        expected = "linear-gradient(90deg, " + stops.join(", ") + ")";
      }
      return { fill, expected, paletteLength: palette.length, noSegs };
    })()`);
    check(
      "scenes: a 4-color scene's swatch renders one smooth blend across the ordered palette (no discrete segments)",
      !!swatchFlow && swatchFlow.noSegs === true && swatchFlow.paletteLength === 4,
      JSON.stringify(swatchFlow)
    );
    check(
      "scenes: swatch blend stops match the scene's palette, in order",
      !!swatchFlow && swatchFlow.fill === swatchFlow.expected,
      JSON.stringify(swatchFlow)
    );

    // 5c-2. Carry Fixtures <ss-scope-control> onto scene rows: target_ids
    // render as the same compact pill, not concatenated "office · office"
    // text, and opening it does not select the row.
    const sceneScopeFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const row = await waitFor(() => [...root().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "twilight"));
      if (!row) return { fail: "twilight row not found" };
      store.select(null);
      const sr = dom(row);
      const joined = sr.querySelector(".targets");
      const scope = sr.querySelector("ss-scope-control");
      if (!scope) return { fail: "no ss-scope-control on the scene row" };
      const btn = dom(scope).querySelector(".scope");
      const pillText = btn ? btn.textContent.replace(/\\s+/g, " ").trim() : "";
      btn.click();
      await new Promise((r) => setTimeout(r, 60));
      const popover = dom(scope).querySelector(".tip:popover-open");
      const scopeText = popover ? popover.textContent.replace(/\\s+/g, " ").trim() : "";
      const selected = !!(store.state.selection && store.state.selection.type === "scene");
      if (popover && popover.hidePopover) popover.hidePopover();
      return { pillText, scopeText, selected, joinedTargets: !!joined };
    })()`);
    check(
      "scenes: target membership uses the Fixtures scope pill, not concatenated target text",
      !!sceneScopeFlow && sceneScopeFlow.joinedTargets === false && sceneScopeFlow.pillText === "Office",
      JSON.stringify(sceneScopeFlow)
    );
    check(
      "scenes: scope pill opens and lists the scene's targets",
      !!sceneScopeFlow && /Office/.test(sceneScopeFlow.scopeText || ""),
      JSON.stringify(sceneScopeFlow)
    );
    check(
      "scenes: opening the scope pill does not also select the row",
      !!sceneScopeFlow && sceneScopeFlow.selected === false,
      JSON.stringify(sceneScopeFlow)
    );

    // 5d. User feedback (2026-09-16): on a narrow scene row the swatch is
    // the single most useful way to recognize a scene at a glance, so it
    // must be the LAST thing the row hides — location (scope pill) and the
    // ready/disabled counts drop first, not the swatch.
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    await sleep(150);
    const mobileSwatchFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const row = await waitFor(() => [...root().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "twilight"));
      if (!row) return { fail: "twilight row not found" };
      await new Promise((r) => setTimeout(r, 60));
      const sr = dom(row);
      const visible = (el) => !!el && getComputedStyle(el).display !== "none" && el.getBoundingClientRect().width > 0;
      const band = sr.querySelector("ss-swatch-band");
      return {
        rowWidth: sr.querySelector(".row").getBoundingClientRect().width,
        swatchVisible: visible(band),
        scopeVisible: visible(sr.querySelector("ss-scope-control")),
        readyVisible: visible(sr.querySelector(".ready")),
      };
    })()`);
    await send("Emulation.clearDeviceMetricsOverride");
    await sleep(120);
    check(
      "scenes (mobile, 390px): a narrow row keeps the swatch visible",
      !!mobileSwatchFlow && mobileSwatchFlow.swatchVisible === true,
      JSON.stringify(mobileSwatchFlow)
    );
    check(
      "scenes (mobile, 390px): location (scope pill) and the ready/disabled counts hide BEFORE the swatch",
      !!mobileSwatchFlow && mobileSwatchFlow.scopeVisible === false && mobileSwatchFlow.readyVisible === false,
      JSON.stringify(mobileSwatchFlow)
    );

    // 5e. User feedback (2026-09-16): a fixed-px column budget silently
    // overflowed <ss-panel>'s overflow:hidden at ordinary desktop widths,
    // clipping the swatch/ready-counts/actions entirely invisible without
    // any visible scrollbar. Pin "no clipping" across the widths that
    // actually occur (compact mode, right at the breakpoint on both sides,
    // and full mode) so a future fixed-px regression is caught immediately.
    // Same pass also fixed a second bug the no-clipping check can't see:
    // <ss-action-menu>'s own :host is flex-wrap:wrap, so a too-narrow
    // actions column silently wrapped a dynamic scene's 2 primary buttons
    // (apply+play) onto a second line while a static scene's single button
    // fit on one — the SAME list then showed visibly uneven row heights
    // depending on scene type. Assert every row in a mixed static+dynamic
    // catalog reports the identical height.
    await send("Emulation.setDeviceMetricsOverride", { width: 700, height: 900, deviceScaleFactor: 1, mobile: false });
    await sleep(150);
    const noClipFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const rows = await waitFor(() => {
        const rs = [...root().querySelectorAll("ss-scene-row")];
        return rs.length >= 2 ? rs : null;
      });
      if (!rows) return { fail: "fewer than 2 scene rows found" };
      const row = rows[0];
      const widths = [400, 560, 700, 712, 720, 850, 1100];
      const clipResults = {};
      for (const w of widths) {
        row.style.width = w + "px";
        await new Promise((r) => requestAnimationFrame(r));
        const rowEl = dom(row).querySelector(".row");
        clipResults[w] = {
          boxWidth: rowEl.getBoundingClientRect().width,
          scrollWidth: rowEl.scrollWidth,
        };
      }
      row.style.width = "";
      // Row-height uniformity: a static scene (single primary button) next
      // to a dynamic one (two primary buttons) must report the same height
      // — neither one's action buttons may wrap onto a second line while
      // the other's don't, at either a narrow (compact) or wide (full)
      // container width.
      const heightsByWidth = {};
      for (const w of [400, 900]) {
        row.style.width = "";
        for (const r of rows) r.style.width = w + "px";
        await new Promise((res) => requestAnimationFrame(res));
        heightsByWidth[w] = rows.map((r) => ({
          scene: r.scene && r.scene.name,
          dynamic: !!(r.summary && r.summary.dynamic),
          height: dom(r).querySelector(".row").getBoundingClientRect().height,
        }));
      }
      for (const r of rows) r.style.width = "";
      const twilight = rows.find((r) => r.scene && r.scene.id === "twilight") || rows[0];
      const visible = (el) => !!el && getComputedStyle(el).display !== "none" && el.getBoundingClientRect().width > 0;
      const layoutAt = {};
      for (const w of [400, 700, 1100]) {
        twilight.style.width = w + "px";
        const rowEl = () => dom(twilight).querySelector(".row");
        const t0 = Date.now();
        while (Date.now() - t0 < 800) {
          const cols = getComputedStyle(rowEl()).gridTemplateColumns.trim().split(/\s+/);
          const want = w <= 560 ? 3 : 5;
          if (cols.length === want) break;
          await new Promise((res) => setTimeout(res, 20));
        }
        const sr = dom(twilight);
        const glyph = sr.querySelector(".name ss-status-glyph");
        const glyphLabelEl = glyph ? dom(glyph).querySelector(".label") : null;
        layoutAt[w] = {
          nameWidth: sr.querySelector(".name .text").getBoundingClientRect().width,
          swatchWidth: sr.querySelector("ss-swatch-band").getBoundingClientRect().width,
          glyphLabel: glyph ? String(glyph.label || "") : null,
          glyphText: glyphLabelEl ? glyphLabelEl.textContent.trim() : "",
          scopeVisible: visible(sr.querySelector("ss-scope-control")),
          readyVisible: visible(sr.querySelector(".ready")),
          gutter: !!sr.querySelector(".gutter"),
          columns: getComputedStyle(sr.querySelector(".row")).gridTemplateColumns,
        };
      }
      twilight.style.width = "";
      return { clipResults, heightsByWidth, layoutAt };
    })()`);
    await send("Emulation.clearDeviceMetricsOverride");
    await sleep(120);
    const clipResults = noClipFlow && !noClipFlow.fail ? noClipFlow.clipResults : null;
    const clippedWidths = clipResults
      ? Object.entries(clipResults).filter(([, r]) => r.scrollWidth > r.boxWidth + 1).map(([w]) => w)
      : ["eval-failed"];
    check(
      "scenes: a row never overflows its own container at any shipped width (400-1100px)",
      clippedWidths.length === 0,
      JSON.stringify({ clippedWidths, noClipFlow })
    );
    const heightsByWidth = noClipFlow && !noClipFlow.fail ? noClipFlow.heightsByWidth : null;
    const unevenAt = heightsByWidth
      ? Object.entries(heightsByWidth)
          .filter(([, rowsAtWidth]) => new Set(rowsAtWidth.map((r) => r.height)).size > 1)
          .map(([w]) => w)
      : ["eval-failed"];
    check(
      "scenes: static and dynamic scene rows report the SAME height (no hidden action-button wrap)",
      unevenAt.length === 0,
      JSON.stringify({ unevenAt, heightsByWidth })
    );
    const layoutAt = noClipFlow && !noClipFlow.fail ? noClipFlow.layoutAt : null;
    check(
      "scenes: healthy rows never spell out 'ready' (status dot only)",
      !!layoutAt &&
        layoutAt[700] &&
        layoutAt[700].glyphLabel === "" &&
        layoutAt[700].glyphText === "",
      JSON.stringify(layoutAt)
    );
    check(
      "scenes: leftover width goes to name and swatch (no empty gutter track)",
      !!layoutAt && layoutAt[400] && layoutAt[400].gutter === false && layoutAt[1100].gutter === false,
      JSON.stringify(layoutAt)
    );
    check(
      "scenes (compact 400px): the swatch grows past the old fixed 116px chip",
      !!layoutAt && layoutAt[400] && layoutAt[400].swatchWidth > 116,
      JSON.stringify(layoutAt && layoutAt[400])
    );
    check(
      "scenes (wide 1100px): the name is not capped at 200px",
      !!layoutAt && layoutAt[1100] && layoutAt[1100].nameWidth > 200,
      JSON.stringify(layoutAt && layoutAt[1100])
    );
    check(
      "scenes (wide 1100px): the swatch grows with leftover width",
      !!layoutAt && layoutAt[1100] && layoutAt[1100].swatchWidth > 160,
      JSON.stringify(layoutAt && layoutAt[1100])
    );
    check(
      "scenes (wide 1100px): the scope pill stays visible once there is room",
      !!layoutAt && layoutAt[1100] && layoutAt[1100].scopeVisible === true,
      JSON.stringify(layoutAt && layoutAt[1100])
    );
    check(
      "scenes (compact 700px): the name keeps its room once the live chips condense",
      !!layoutAt && layoutAt[700] && layoutAt[700].nameWidth > 180,
      JSON.stringify(layoutAt && layoutAt[700])
    );

    // 6. Scene overflow menu (post-R5 visual polish): opens without being
    //    clipped by the row list's own overflow:hidden, routes the exact
    //    same scene-action payload as the old flat icon row, doesn't also
    //    select the scene row, and closes itself after use.
    const overflowFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("mixed-fidelity");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const root = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const row = await waitFor(() => [...root().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "twilight"));
      if (!row) return { fail: "twilight row not found" };
      let captured = null;
      row.addEventListener("scene-action", (e) => { captured = e.detail; });
      const om = dom(row).querySelector("ss-overflow-menu");
      if (!om) return { fail: "no ss-overflow-menu on the scene row" };
      dom(om).querySelector(".trigger").click();
      await waitFor(() => dom(om).querySelector("[popover]").matches(":popover-open"));
      const popoverRect = dom(om).querySelector("[popover]").getBoundingClientRect();
      const archiveBtn = [...dom(om).querySelectorAll(".item")].find((b) => b.textContent.trim() === "Archive");
      if (!archiveBtn) return { fail: "no Archive item in the overflow menu" };
      archiveBtn.click();
      await waitFor(() => captured !== null);
      const stillOpen = dom(om).querySelector("[popover]").matches(":popover-open");
      return { popoverWidth: popoverRect.width, captured, stillOpen, rowSelected: row.selected === true };
    })()`);
    check(
      "scenes: overflow menu opens with real (non-clipped) size",
      !!overflowFlow && overflowFlow.popoverWidth > 0,
      JSON.stringify(overflowFlow)
    );
    check(
      "scenes: overflow menu's Archive item routes the same scene-action payload",
      !!overflowFlow && !!overflowFlow.captured && overflowFlow.captured.action === "archive" && overflowFlow.captured.scene_id === "twilight",
      JSON.stringify(overflowFlow && overflowFlow.captured)
    );
    check(
      "scenes: using the overflow menu does not also select the row",
      !!overflowFlow && overflowFlow.rowSelected === false,
      JSON.stringify(overflowFlow)
    );
    check(
      "scenes: overflow menu closes itself after an item is activated",
      !!overflowFlow && overflowFlow.stillOpen === false,
      JSON.stringify(overflowFlow)
    );

    // 7. Scene Builder journey (Pass 2 plan §8, real user path): New Scene ->
    //    controls -> Preview -> Save -> Edit -> Save Changes -> dirty guard.
    const builderFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const builder = () => root().querySelector("ss-view-scene-builder");
      const broot = () => dom(builder());
      const setNativeValue = (input, value) => {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      };
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const scenesRoot = () => dom(root().querySelector("ss-view-scenes"));
      const newBtn = await waitFor(() => scenesRoot().querySelector("#new-scene"));
      if (!newBtn) return { fail: "New Scene button missing on Scenes" };
      newBtn.click();
      if (!(await waitFor(() => builder() && broot().querySelector("#builder-name")))) {
        return { fail: "builder did not open with a name field" };
      }
      const emptySaveDisabled = broot().querySelector("#builder-save").disabled;
      // Targets: both canonical groups are offered (Builder-expansion §1).
      const groupLabels = [...broot().querySelectorAll(".target-group .group-label")].map((el) => el.textContent.trim());
      const paintedIds = [...broot().querySelectorAll(".tid")].map((el) => el.textContent.trim());
      const wledCluster = broot().querySelector("details.target-cluster");
      const officeName = [...broot().querySelectorAll(".target-eco .name")].map((el) => el.textContent.trim());
      const wledOpen = wledCluster ? wledCluster.open : null;
      // name + target (mixed: one declared group + one individual fixture)
      setNativeValue(broot().querySelector("#builder-name"), "Browser Glow");
      const target = broot().querySelector("input.builder-target[value='office']");
      if (!target) return { fail: "no office target checkbox" };
      target.click();
      await waitFor(() => store.state.builder.draft.target_ids.includes("office"));
      const fixtureTarget = broot().querySelector("input.builder-target[value='lamp']");
      if (!fixtureTarget) return { fail: "no individual-fixture target checkbox" };
      fixtureTarget.click();
      await waitFor(() => store.state.builder.draft.target_ids.includes("lamp"));
      // Scene Defaults (§2): explicit brightness + color, then dynamic motion.
      // New drafts already seed brightness; only click the set-toggle when unset.
      if (typeof store.state.builder.draft.default_state.brightness !== "number") {
        broot().querySelector(".builder-state-set-brightness").click();
        await waitFor(() => typeof store.state.builder.draft.default_state.brightness === "number");
      }
      setNativeValue(broot().querySelector("#builder-default-brightness-num"), "42");
      await waitFor(() => store.state.builder.draft.default_state.brightness === 42);
      broot().querySelector(".builder-state-set-color").click();
      await waitFor(() => typeof store.state.builder.draft.default_state.color === "string");
      setNativeValue(broot().querySelector(".state-color-hex[data-scope='default']"), "#abcdef");
      await waitFor(() => store.state.builder.draft.default_state.color === "#abcdef");
      const dyn = broot().querySelector("input.builder-motion-dynamic");
      dyn.checked = true;
      dyn.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.motion.mode === "palette_cycle");
      // Palette is a Dynamic-only panel; add two colors and reorder.
      await waitFor(() => broot().querySelector("#builder-add-color"));
      broot().querySelector("#builder-add-color").click();
      broot().querySelector("#builder-add-color").click();
      await waitFor(() => store.state.builder.draft.palette.length === 2);
      const hex0 = broot().querySelector(".builder-palette-hex[data-index='0']");
      const hex1 = broot().querySelector(".builder-palette-hex[data-index='1']");
      setNativeValue(hex0, "#112233");
      setNativeValue(hex1, "#445566");
      const upBtn = broot().querySelector(".builder-move-down[data-index='0']");
      if (!upBtn) return { fail: "move-down control missing on palette row 1" };
      upBtn.click();
      await waitFor(() => store.state.builder.draft.palette[0] === "#445566");
      setNativeValue(broot().querySelector("#builder-speed"), "0.65");
      await waitFor(() => store.state.builder.draft.motion.speed === 0.65);
      setNativeValue(broot().querySelector("#builder-palette-brightness-num"), "55");
      await waitFor(() => store.state.builder.draft.brightness === 55);
      const paletteOnDynamic = !!broot().querySelector("#builder-palette");
      const stat = broot().querySelector("input.builder-motion-static");
      stat.checked = true;
      stat.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.motion.mode === "static");
      const assignList = await waitFor(() => broot().querySelector(".paint-use"));
      const defaultColorHidden = !broot().querySelector(".builder-state-set-color[data-scope='default']");
      // Segments stay collapsed by default, before any color row is expanded.
      const segsHiddenAtStart = !broot().querySelector(".assign-row[data-fixture='wled_seg_0']");
      // Unified design (single-row palette): a plain fixture's picker only
      // exists once its color row's "N lights" toggle is expanded, but a
      // MIXED cluster (segments disagreeing, the common case before anyone
      // pins them together) lives in the always-visible "Other lights" tail
      // instead — check current DOM first, then try each color in turn.
      const openColorContaining = async (sel) => {
        const already = broot().querySelector(sel);
        if (already) return already;
        for (const toggle of [...broot().querySelectorAll(".paint-use")]) {
          toggle.click();
          const found = await waitFor(() => broot().querySelector(sel), 800);
          if (found) return found;
        }
        return null;
      };
      const lampChip = await openColorContaining(".builder-assign-chip[data-fixture='lamp']");
      if (!lampChip) return { fail: "lamp assign control missing" };
      lampChip.click();
      const lampRowOpen = await waitFor(() => broot().querySelector(".assign-row[data-fixture='lamp'].open"));
      const paint0 = await waitFor(() => {
        const swatch = broot().querySelector(".builder-paint-swatch[data-fixture='lamp'][data-index='0']");
        return swatch && swatch.getBoundingClientRect().height >= 20 ? swatch : null;
      });
      if (!paint0) {
        const hidden = broot().querySelector(".builder-paint-swatch[data-fixture='lamp'][data-index='0']");
        return {
          fail: "lamp palette color 0 not visible after opening the light",
          lampRowOpen: !!lampRowOpen,
          paintCount: broot().querySelectorAll(".builder-paint-swatch[data-fixture='lamp']").length,
          hiddenHeight: hidden ? hidden.getBoundingClientRect().height : null,
        };
      }
      paint0.click();
      await waitFor(() => (store.state.builder.draft.fixture_states.lamp || {}).palette_index === 0);
      const paintedLamp = (store.state.builder.draft.fixture_states.lamp || {}).palette_index === 0;
      // Pinning lamp can reshuffle which slot auto/spread fixtures resolve to
      // (spread recomputes over the remaining unpinned candidates), so the
      // WLED cluster may no longer be under the color row we had open.
      const wledAssign = await openColorContaining(".assign-cluster");
      const wledChip = wledAssign && wledAssign.querySelector(".builder-assign-chip");
      if (!wledChip) return { fail: "WLED cluster assign control missing", paintedLamp, segsHiddenAtStart };
      wledChip.click();
      const clusterPaint = await waitFor(() => {
        const swatch = wledAssign.querySelector(".builder-paint-swatch[data-index='1']");
        return swatch && swatch.getBoundingClientRect().height >= 20 ? swatch : null;
      });
      if (!clusterPaint) {
        return { fail: "WLED cluster palette color 1 not visible after opening the fixture", paintedLamp, segsHiddenAtStart };
      }
      clusterPaint.click();
      await waitFor(() => (store.state.builder.draft.fixture_states.wled_seg_0 || {}).palette_index === 1);
      const wledIds = ["wled_seg_0", "wled_seg_1", "wled_seg_2", "wled_seg_3", "wled_seg_4", "wled_seg_5"];
      const clusterPinnedAll = wledIds.every((id) => (store.state.builder.draft.fixture_states[id] || {}).palette_index === 1);
      // Now that every segment agrees, the cluster is no longer "mixed" and
      // moves from "Other lights" into color index 1's own bucket — the
      // pre-pin reference is stale, re-locate it before touching its fold.
      const wledAssignAfterPin = await openColorContaining(".assign-cluster");
      const fold = wledAssignAfterPin && wledAssignAfterPin.querySelector(".builder-assign-fold");
      if (!fold) return { fail: "WLED segment accordion control missing", clusterPinnedAll, segsHiddenAtStart };
      const clusterMainRow = wledAssignAfterPin.querySelector(".assign-row");
      fold.click();
      const seg0 = await waitFor(() => broot().querySelector(".assign-row[data-fixture='wled_seg_0']"));
      const clusterAligned =
        !!seg0 &&
        !!clusterMainRow &&
        Math.abs(seg0.getBoundingClientRect().left - clusterMainRow.getBoundingClientRect().left) < 1 &&
        Math.abs(seg0.getBoundingClientRect().right - clusterMainRow.getBoundingClientRect().right) < 1;
      const spreadBtn = broot().querySelector("#builder-spread-palette");
      if (spreadBtn) spreadBtn.click();
      await waitFor(() => (store.state.builder.draft.fixture_states.lamp || {}).palette_index == null);
      const spreadCleared = (store.state.builder.draft.fixture_states.lamp || {}).palette_index == null;
      dyn.checked = true;
      dyn.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.motion.mode === "palette_cycle");
      // preview (server-authoritative) without catalog mutation
      const catalogBefore = store.state.scenes.scenes.length;
      broot().querySelector("#builder-preview").click();
      const panel = await waitFor(() => broot().querySelector("#builder-preview-panel #builder-preview-head"));
      if (!panel) return { fail: "preview panel did not render", previewError: store.state.builder && store.state.builder.previewError };
      const plan = store.state.builder.preview.render_plan;
      const hasReduction =
        (plan.fixture_plans || []).some((p) => p.fidelity === "approximate" || p.fidelity === "unsupported") ||
        (plan.skipped_fixture_ids || []).length > 0;
      // §7: an unqualified green headline is only allowed for a clean plan.
      const previewToneHonest = hasReduction ? !panel.classList.contains("ok") : panel.classList.contains("ok");
      const previewText = broot().querySelector("#builder-preview-panel").textContent;
      const catalogUnchanged = store.state.scenes.scenes.length === catalogBefore;
      const proposedId = store.state.builder.preview && store.state.builder.preview.scene ? store.state.builder.preview.scene.id : null;
      // corrective pass B: any draft change immediately invalidates the
      // preview — the old panel must vanish and the stale hint must appear.
      setNativeValue(broot().querySelector("#builder-default-brightness-num"), "43");
      await waitFor(() => store.state.builder.preview === null && store.state.builder.previewStale === true);
      const previewInvalidated = store.state.builder.preview === null && store.state.builder.previewStale === true;
      const staleHintShown = !!broot().querySelector("#builder-preview-stale");
      const oldPanelGone = !broot().querySelector("#builder-preview-panel #builder-preview-head");
      // Per-fixture override (§3): add one, edit a supported field.
      const addSelect = broot().querySelector("#builder-override-add");
      if (!addSelect) return { fail: "override picker missing" };
      const overrideFixture = [...addSelect.options].map((o) => o.value).filter(Boolean)[0];
      if (!overrideFixture) return { fail: "no fixture available to override" };
      addSelect.value = overrideFixture;
      addSelect.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => !!(store.state.builder.draft.fixture_states || {})[overrideFixture]);
      const overrideRoot = () => broot().querySelector(".override[data-fixture='" + overrideFixture + "']");
      overrideRoot().querySelector("summary").click();
      const ovOn = overrideRoot().querySelector(".state-on");
      ovOn.value = "off";
      ovOn.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.fixture_states[overrideFixture].on === false);
      overrideRoot().querySelector(".state-brightness-num").value = "";
      setNativeValue(overrideRoot().querySelector(".state-brightness-num"), "33");
      await waitFor(() => store.state.builder.draft.fixture_states[overrideFixture].brightness === 33);
      // re-preview produces current server-authoritative results again
      broot().querySelector("#builder-preview").click();
      const panelAgain = await waitFor(() => broot().querySelector("#builder-preview-panel #builder-preview-head"));
      const rePreviewOk =
        !!panelAgain && store.state.builder.previewStale === false && !broot().querySelector("#builder-preview-stale");
      // save
      broot().querySelector("#builder-save").click();
      const savedRow = await waitFor(() =>
        [...scenesRoot().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "browser_glow")
      );
      const savedDoc = store.state.scenes.scenes.find((s) => s.id === "browser_glow") || null;
      const backOnScenes = store.state.view === "scenes" && store.state.builder === null;
      const selected = store.state.selection && store.state.selection.type === "scene" && store.state.selection.id === "browser_glow";
      // §8: a newly authored scene gets backend-authoritative row fidelity.
      const rowFidelity = await waitFor(() => {
        const cached = store.sceneFidelityFor("browser_glow");
        return cached && cached.status === "ok" && cached.planned > 0 ? cached : null;
      });
      // edit: overflow menu -> Edit
      if (!savedRow) return { fail: "saved scene row not found after save" };
      const om = dom(savedRow).querySelector("ss-overflow-menu");
      dom(om).querySelector(".trigger").click();
      await waitFor(() => dom(om).querySelector("[popover]").matches(":popover-open"));
      const editBtn = [...dom(om).querySelectorAll(".item")].find((b) => b.textContent.trim() === "Edit");
      if (!editBtn) return { fail: "Edit item missing in overflow menu" };
      editBtn.click();
      if (
        !(await waitFor(() => store.state.view === "scene_builder" && store.state.builder.mode === "edit" && broot() && broot().querySelector("#builder-name")))
      ) {
        return { fail: "Edit did not open the builder in edit mode" };
      }
      const editPrepopulated =
        broot().querySelector("#builder-name").value === "Browser Glow" &&
        store.state.builder.dirty === false;
      const editIdShown = (broot().querySelector(".head .scene-id") || { textContent: "" }).textContent.includes("browser_glow");
      setNativeValue(broot().querySelector("#builder-palette-brightness-num"), "80");
      broot().querySelector("#builder-save").click();
      const updatedDoc = await waitFor(() => {
        const doc = store.state.scenes.scenes.find((s) => s.id === "browser_glow");
        return doc && doc.brightness === 80 ? doc : null;
      });
      const stillBackOnScenes = store.state.view === "scenes" && store.state.builder === null;
      // dirty guard: start a new draft, touch it, try to leave
      scenesRoot().querySelector("#new-scene").click();
      await waitFor(() => store.state.view === "scene_builder" && broot() && broot().querySelector("#builder-name"));
      setNativeValue(broot().querySelector("#builder-name"), "Abandoned Draft");
      root().querySelectorAll("nav.tabs button").forEach((b) => {
        if (b.textContent.trim() === "Overview") b.click();
      });
      await waitFor(() => !!root().querySelector("#builder-exit-leave"));
      const guardVisible = !!root().querySelector(".exit-confirm");
      const stillInBuilder = store.state.view === "scene_builder";
      const leaveBtn = root().querySelector("#builder-exit-leave");
      if (leaveBtn) leaveBtn.click();
      await waitFor(() => store.state.view === "overview" && store.state.builder === null);
      const guardReleased = store.state.view === "overview" && store.state.builder === null;
      const abandoned = store.state.scenes.scenes.some((s) => s.id === "abandoned_draft");
      return {
        emptySaveDisabled,
        groupLabels,
        paintedIds,
        wledCluster: !!wledCluster,
        wledOpen,
        officeName,
        previewOk: !!panel,
        paletteOnDynamic,
        assignList: !!assignList,
        defaultColorHidden,
        paintedLamp,
        segsHiddenAtStart,
        clusterPinnedAll,
        clusterAligned,
        spreadCleared,
        previewToneHonest,
        hasReduction,
        previewText: previewText.slice(0, 160),
        catalogUnchanged,
        proposedId,
        previewInvalidated,
        staleHintShown,
        oldPanelGone,
        rePreviewOk,
        overrideFixture,
        overrideOn: savedDoc && savedDoc.fixture_states && savedDoc.fixture_states[overrideFixture]
          ? savedDoc.fixture_states[overrideFixture].on
          : null,
        overrideBrightness: savedDoc && savedDoc.fixture_states && savedDoc.fixture_states[overrideFixture]
          ? savedDoc.fixture_states[overrideFixture].brightness
          : null,
        rowFidelity: rowFidelity || null,
        savedDefaults: savedDoc ? savedDoc.default_state : null,
        savedBrightness: savedDoc ? savedDoc.brightness : null,
        backOnScenes,
        selected,
        savedName: savedDoc ? savedDoc.name : null,
        savedTargets: savedDoc ? savedDoc.target_ids : null,
        savedPalette: savedDoc ? savedDoc.palette : null,
        savedMotion: savedDoc && savedDoc.motion ? savedDoc.motion.mode : null,
        editPrepopulated,
        editIdShown,
        updatedBrightness: updatedDoc ? updatedDoc.brightness : null,
        stillBackOnScenes,
        guardVisible,
        stillInBuilder,
        guardReleased,
        abandoned,
      };
    })()`);
    check("builder: New Scene opens the Builder; an empty draft cannot be saved", !!builderFlow && builderFlow.emptySaveDisabled === true, JSON.stringify(builderFlow));
    check(
      "builder §1: targets are rooms, HA ecosystem groups, and collapsible fixtures without underscore ids",
      !!builderFlow &&
        deepEqualList(builderFlow.groupLabels, ["Rooms", "Home Assistant groups", "Fixtures"]) &&
        Array.isArray(builderFlow.paintedIds) &&
        builderFlow.paintedIds.length === 0 &&
        builderFlow.wledCluster === true &&
        builderFlow.wledOpen === false &&
        Array.isArray(builderFlow.officeName) &&
        builderFlow.officeName.includes("Office Lights"),
      JSON.stringify(builderFlow && {
        groupLabels: builderFlow.groupLabels,
        paintedIds: builderFlow.paintedIds,
        wledCluster: builderFlow.wledCluster,
        wledOpen: builderFlow.wledOpen,
        officeName: builderFlow.officeName,
      })
    );
    check(
      "builder: preview renders the server-authoritative fidelity panel without catalog mutation",
      !!builderFlow && builderFlow.previewOk === true && builderFlow.catalogUnchanged === true && builderFlow.proposedId === "browser_glow",
      JSON.stringify(builderFlow && builderFlow.previewText)
    );
    check(
      "builder §7: the preview headline is honest — an unqualified green only for a clean render plan",
      !!builderFlow && builderFlow.previewToneHonest === true,
      JSON.stringify(builderFlow && { hasReduction: builderFlow.hasReduction, previewText: builderFlow.previewText })
    );
    check(
      "builder: a draft change invalidates the previous preview (panel cleared, stale hint shown) until re-previewed",
      !!builderFlow && builderFlow.previewInvalidated === true && builderFlow.staleHintShown === true && builderFlow.oldPanelGone === true && builderFlow.rePreviewOk === true,
      JSON.stringify(builderFlow && { previewInvalidated: builderFlow.previewInvalidated, staleHintShown: builderFlow.staleHintShown, oldPanelGone: builderFlow.oldPanelGone, rePreviewOk: builderFlow.rePreviewOk })
    );
    check(
      "builder §2+§3: scene defaults AND a per-fixture override persist through Save",
      !!builderFlow &&
        builderFlow.savedDefaults &&
        builderFlow.savedDefaults.brightness === 43 &&
        builderFlow.savedDefaults.color === "#abcdef" &&
        builderFlow.overrideBrightness === 33 &&
        builderFlow.overrideOn === false,
      JSON.stringify(builderFlow && { savedDefaults: builderFlow.savedDefaults, overrideFixture: builderFlow.overrideFixture, overrideBrightness: builderFlow.overrideBrightness, overrideOn: builderFlow.overrideOn })
    );
    check(
      "builder §8: a newly authored scene immediately receives backend-authoritative row fidelity",
      !!builderFlow && !!builderFlow.rowFidelity && builderFlow.rowFidelity.status === "ok" && builderFlow.rowFidelity.planned > 0,
      JSON.stringify(builderFlow && builderFlow.rowFidelity)
    );
    check(
      "builder: Save Scene persists, returns to Scenes, and selects the saved scene",
      !!builderFlow && builderFlow.backOnScenes === true && builderFlow.selected === true && builderFlow.savedName === "Browser Glow",
      JSON.stringify(builderFlow)
    );
    check(
      "builder: Static shows palette assignment; painting pins a slot and Spread clears it",
      !!builderFlow &&
        builderFlow.paletteOnDynamic === true &&
        builderFlow.assignList === true &&
        builderFlow.defaultColorHidden === true &&
        builderFlow.paintedLamp === true &&
        builderFlow.spreadCleared === true,
      JSON.stringify(builderFlow && {
        paletteOnDynamic: builderFlow.paletteOnDynamic,
        assignList: builderFlow.assignList,
        defaultColorHidden: builderFlow.defaultColorHidden,
        paintedLamp: builderFlow.paintedLamp,
        spreadCleared: builderFlow.spreadCleared,
      })
    );
    check(
      "builder: WLED assignment is one fixture picker with a closed segment accordion",
      !!builderFlow &&
        builderFlow.segsHiddenAtStart === true &&
        builderFlow.clusterPinnedAll === true &&
        builderFlow.clusterAligned === true,
      JSON.stringify(builderFlow && {
        segsHiddenAtStart: builderFlow.segsHiddenAtStart,
        clusterPinnedAll: builderFlow.clusterPinnedAll,
        clusterAligned: builderFlow.clusterAligned,
        fail: builderFlow.fail,
      })
    );
    check(
      "builder: saved intent carries mixed targets, ordered palette, dynamic motion, and palette brightness",
      !!builderFlow &&
        deepEqualList(builderFlow.savedTargets, ["office", "lamp"]) &&
        deepEqualList(builderFlow.savedPalette, ["#445566", "#112233"]) &&
        builderFlow.savedMotion === "palette_cycle" &&
        builderFlow.savedBrightness === 55,
      JSON.stringify(builderFlow && { savedTargets: builderFlow.savedTargets, savedPalette: builderFlow.savedPalette, savedBrightness: builderFlow.savedBrightness })
    );
    check(
      "builder: Edit pre-populates the canonical intent and shows the immutable id",
      !!builderFlow && builderFlow.editPrepopulated === true && builderFlow.editIdShown === true,
      JSON.stringify(builderFlow)
    );
    check(
      "builder: Save Changes updates the catalog with the id stable",
      !!builderFlow && builderFlow.updatedBrightness === 80 && builderFlow.stillBackOnScenes === true,
      JSON.stringify(builderFlow)
    );
    check(
      "builder: leaving a dirty draft requires the in-app confirmation, Leave discards it",
      !!builderFlow && builderFlow.guardVisible === true && builderFlow.stillInBuilder === true && builderFlow.guardReleased === true && builderFlow.abandoned === false,
      JSON.stringify(builderFlow)
    );

    // 7b. Rename: the overflow menu's Rename item must open the row's OWN
    //     inline editor, and the submitted name must persist via scene.rename.
    const renameFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("mixed-fidelity");
      store.setView("scenes");
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const scenesRoot = () => dom(dom(app()).querySelector("ss-view-scenes"));
      const twilightRow = () =>
        [...scenesRoot().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === "twilight");
      const row = await waitFor(() => twilightRow() && dom(twilightRow()).querySelector("ss-overflow-menu"));
      if (!row) return { fail: "twilight row or overflow menu missing" };
      // ss-overflow-menu keeps its trigger/items in its own shadow root.
      const openMenu = async (rowEl) => {
        const om = dom(rowEl).querySelector("ss-overflow-menu");
        dom(om).querySelector(".trigger").click();
        await waitFor(() => dom(om).querySelector("[popover]").matches(":popover-open"));
        return om;
      };
      let om = await openMenu(twilightRow());
      const renameItem = [...dom(om).querySelectorAll(".item")].find((b) => b.textContent.trim() === "Rename");
      if (!renameItem) return { fail: "no Rename item in the overflow menu" };
      renameItem.click();
      const editor = await waitFor(() => dom(twilightRow()).querySelector(".rename input"));
      if (!editor) return { fail: "inline rename editor did not appear" };
      editor.value = "Twilight Renamed";
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      const renameSpan = dom(twilightRow()).querySelector(".rename");
      [...renameSpan.querySelectorAll("button")].find((b) => b.textContent.trim() === "Save").click();
      const renamed = await waitFor(() => {
        const doc = store.state.scenes.scenes.find((s) => s.id === "twilight");
        const freshRow = twilightRow();
        return doc && doc.name === "Twilight Renamed" && freshRow && freshRow.scene.name === "Twilight Renamed" ? doc : null;
      });
      const notice = store.state.notice ? store.state.notice.text : "";
      // restore the original name through the same seam to leave the scenario clean
      const freshRow = twilightRow();
      if (freshRow && dom(freshRow).querySelector("ss-overflow-menu")) {
        om = await openMenu(freshRow);
        const renameItem2 = [...dom(om).querySelectorAll(".item")].find((b) => b.textContent.trim() === "Rename");
        renameItem2.click();
        const editor2 = await waitFor(() => dom(freshRow).querySelector(".rename input"));
        if (editor2) {
          editor2.value = "Twilight";
          editor2.dispatchEvent(new Event("input", { bubbles: true }));
          const span2 = dom(freshRow).querySelector(".rename");
          [...span2.querySelectorAll("button")].find((b) => b.textContent.trim() === "Save").click();
          await waitFor(() => {
            const doc = store.state.scenes.scenes.find((s) => s.id === "twilight");
            return doc && doc.name === "Twilight";
          });
        }
      }
      const restored = (store.state.scenes.scenes.find((s) => s.id === "twilight") || {}).name;
      return { renamedOk: !!renamed, notice, restored };
    })()`);
    check(
      "rename: the overflow Rename item opens the inline editor and scene.rename persists the new name",
      !!renameFlow && renameFlow.renamedOk === true,
      JSON.stringify(renameFlow)
    );
    check(
      "rename: the catalog reflects the rename and the original name is restored through the same seam",
      !!renameFlow && renameFlow.restored === "Twilight",
      JSON.stringify(renameFlow)
    );

    // 7bb. Duplicate / Save-as-New (Builder-expansion §4) + advanced-content
    //      preservation (§3): duplicate an ADVANCED scene, edit one supported
    //      override field, and prove the advanced content + original survive.
    const duplicateFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const setNativeValue = (input, value) => {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      };
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const scenesRoot = () => dom(root().querySelector("ss-view-scenes"));
      const rowFor = (id) =>
        [...scenesRoot().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === id);
      const originalBefore = JSON.parse(JSON.stringify(store.state.scenes.scenes.find((s) => s.id === "twilight")));
      const row = await waitFor(() => rowFor("twilight") && dom(rowFor("twilight")).querySelector("ss-overflow-menu"));
      if (!row) return { fail: "twilight row or overflow menu missing" };
      const om = dom(rowFor("twilight")).querySelector("ss-overflow-menu");
      dom(om).querySelector(".trigger").click();
      await waitFor(() => dom(om).querySelector("[popover]").matches(":popover-open"));
      const dupItem = [...dom(om).querySelectorAll(".item")].find((b) => b.textContent.trim() === "Duplicate");
      if (!dupItem) return { fail: "no Duplicate item in the overflow menu" };
      dupItem.click();
      const builder = () => root().querySelector("ss-view-scene-builder");
      const broot = () => dom(builder());
      if (!(await waitFor(() => store.state.builder && store.state.builder.mode === "duplicate" && broot().querySelector("#builder-name")))) {
        return { fail: "Duplicate did not open the Builder in duplicate mode" };
      }
      const draft = store.state.builder.draft;
      const sourceStamp = draft.metadata || {};
      const sourceIdDropped = !draft.id;
      const provenanceDropped = !sourceStamp.migrated_from_v1 && !sourceStamp.archived_at;
      const intentCarried =
        Object.keys(draft.fixture_states || {}).length === Object.keys(originalBefore.fixture_states || {}).length;
      const gradientBefore = draft.fixture_states.g_strip && draft.fixture_states.g_strip.gradient
        ? [...draft.fixture_states.g_strip.gradient]
        : null;
      const saveLabel = (broot().querySelector("#builder-save") || {}).textContent || "";
      // Edit ONE supported field on an override that also carries advanced content.
      const overrideEl = broot().querySelector(".override[data-fixture='g_strip']");
      if (!overrideEl) return { fail: "g_strip override not rendered in the duplicate draft" };
      overrideEl.querySelector("summary").click();
      setNativeValue(overrideEl.querySelector(".state-brightness-num"), "37");
      await waitFor(() => store.state.builder.draft.fixture_states.g_strip.brightness === 37);
      const gradientAfter = store.state.builder.draft.fixture_states.g_strip.gradient || null;
      const gradientPreserved =
        gradientBefore && gradientAfter && gradientBefore.length === gradientAfter.length
          ? gradientBefore.every((c, i) => c === gradientAfter[i])
          : false;
      // Preview -> candidate id -> Save New.
      broot().querySelector("#builder-preview").click();
      const head = await waitFor(() => broot().querySelector("#builder-preview-head"));
      if (!head) return { fail: "duplicate preview did not render" };
      const candidateId = store.state.builder.preview && store.state.builder.preview.scene ? store.state.builder.preview.scene.id : null;
      broot().querySelector("#builder-save").click();
      const created = await waitFor(() => {
        const doc = store.state.scenes.scenes.find((s) => s.id === "twilight_copy");
        return doc || null;
      });
      const originalAfter = store.state.scenes.scenes.find((s) => s.id === "twilight");
      const originalUntouched = JSON.stringify(originalAfter) === JSON.stringify(originalBefore);
      const backOnScenes = store.state.view === "scenes" && store.state.builder === null;
      return {
        fail: created ? null : "duplicated scene not found in the catalog",
        sourceIdDropped,
        provenanceDropped,
        intentCarried,
        saveLabel: saveLabel.trim(),
        gradientPreserved,
        candidateId,
        createdId: created ? created.id : null,
        duplicatedFrom: created && created.metadata ? created.metadata.duplicated_from : null,
        createdFixtureStates: created && created.fixture_states ? Object.keys(created.fixture_states).length : 0,
        createdBrightness: created && created.fixture_states && created.fixture_states.g_strip ? created.fixture_states.g_strip.brightness : null,
        originalUntouched,
        backOnScenes,
      };
    })()`);
    check(
      "duplicate: the overflow Duplicate action opens the Builder with the source INTENT and no source identity",
      !!duplicateFlow &&
        duplicateFlow.sourceIdDropped === true &&
        duplicateFlow.provenanceDropped === true &&
        duplicateFlow.intentCarried === true &&
        duplicateFlow.saveLabel === "Save New",
      JSON.stringify(duplicateFlow)
    );
    check(
      "duplicate §3: editing one supported override field preserves that fixture's advanced content (gradient)",
      !!duplicateFlow && duplicateFlow.gradientPreserved === true,
      JSON.stringify(duplicateFlow)
    );
    check(
      "duplicate §4: Preview derives the candidate id and Save New creates a backend-authoritative scene",
      !!duplicateFlow &&
        duplicateFlow.candidateId === "twilight_copy" &&
        duplicateFlow.createdId === "twilight_copy" &&
        duplicateFlow.duplicatedFrom === "twilight" &&
        duplicateFlow.backOnScenes === true,
      JSON.stringify(duplicateFlow)
    );
    check(
      "duplicate §4: advanced intent round-trips to the new scene and the original stays untouched",
      !!duplicateFlow &&
        duplicateFlow.createdFixtureStates === 12 &&
        duplicateFlow.createdBrightness === 37 &&
        duplicateFlow.originalUntouched === true,
      JSON.stringify(duplicateFlow)
    );

    // 7bc. Orphan overrides (§3): retargeting a scene must not strand
    //      overrides the user cannot remove — the Builder offers an explicit
    //      per-override cleanup for fixture_states entries outside the
    //      selected targets.
    const orphanFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      await store.setScenario("all-healthy");
      store.setView("scenes");
      await store.openBuilder({ sceneId: "twilight" });
      await waitFor(() => store.state.builder && store.state.builder.mode === "edit");
      const broot = () => dom(root().querySelector("ss-view-scene-builder"));
      await waitFor(() => broot() && broot().querySelector("#builder-name"));
      const overridesBefore = Object.keys(store.state.builder.draft.fixture_states || {}).length;
      // Retarget: drop the office GROUP, keep one individual fixture. Every
      // other fixture_states entry becomes an orphan for the current targets.
      const group = broot().querySelector("input.builder-target[value='office']");
      group.checked = false;
      group.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => !store.state.builder.draft.target_ids.includes("office"));
      const lamp = broot().querySelector("input.builder-target[value='lamp']");
      lamp.checked = true;
      lamp.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.target_ids.includes("lamp"));
      const orphanRoot = await waitFor(() => broot().querySelector("#builder-orphan-overrides"));
      const buttons = orphanRoot ? [...orphanRoot.querySelectorAll(".builder-orphan-remove")] : [];
      const orphanIds = buttons.map((b) => b.dataset.fixture);
      const gstrip = buttons.find((b) => b.dataset.fixture === "g_strip");
      const actionLabel = gstrip ? (gstrip.getAttribute("aria-label") || "").length > 0 : false;
      if (gstrip) gstrip.click();
      const removed = await waitFor(() => !(store.state.builder.draft.fixture_states || {}).g_strip);
      const orphanCountAfter = broot().querySelectorAll("#builder-orphan-overrides .builder-orphan-remove").length;
      const savedNameUnchanged = (store.state.scenes.scenes.find((s) => s.id === "twilight") || {}).name;
      store.closeBuilder();
      await store.setView("scenes");
      return {
        orphanRendered: !!orphanRoot,
        orphanCount: orphanIds.length,
        orphanHasGstrip: orphanIds.includes("g_strip"),
        actionLabel,
        removedFromDraft: !!removed,
        orphanCountAfter,
        overridesBefore,
        savedNameUnchanged,
      };
    })()`);
    check(
      "builder §3: overrides stranded by a target change are listed with an explicit Remove action",
      !!orphanFlow &&
        orphanFlow.orphanRendered === true &&
        orphanFlow.orphanCount === orphanFlow.overridesBefore - 1 &&
        orphanFlow.orphanHasGstrip === true &&
        orphanFlow.actionLabel === true,
      JSON.stringify(orphanFlow)
    );
    check(
      "builder §3: removing an orphan override updates the draft without touching the saved scene",
      !!orphanFlow && orphanFlow.removedFromDraft === true && orphanFlow.orphanCountAfter === orphanFlow.orphanCount - 1,
      JSON.stringify(orphanFlow)
    );

    // 7bd. Scene rows never paint fidelity bucket counts. Preview failures
    //      stay in the Fidelity cache / inspector — they must not dump
    //      "6 nat · 8 approx" (or "fidelity unavailable") onto the catalog row.
    const fidelityFailFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      await store.setScenario("all-healthy");
      store.setView("scenes");
      const scenesRoot = () => dom(root().querySelector("ss-view-scenes"));
      const rowFor = (id) => [...scenesRoot().querySelectorAll("ss-scene-row")].find((r) => r.scene && r.scene.id === id);
      await waitFor(() => rowFor("twilight"));
      const rowCopy = (id) => {
        const row = rowFor(id);
        if (!row) return { text: "", hasFid: false };
        const sr = dom(row);
        return { text: (sr.textContent || "").replace(/\\s+/g, " ").trim(), hasFid: !!sr.querySelector(".fid") };
      };
      const looksLikeBuckets = (t) => /\\b\\d+\\s+(nat|eq|approx|unsup)\\b/.test(t) || /fidelity unavailable/i.test(t);
      await waitFor(() => {
        const cached = store.sceneFidelityFor("twilight");
        return cached && cached.status === "ok" && cached.planned > 0 ? cached : null;
      });
      const ok = rowCopy("twilight");
      const originalEntry = store.sceneFidelityFor("twilight");
      store.state.sceneFidelity["twilight"] = { status: "unavailable", reason: "probe boom" };
      store.select({ type: "scene", id: "twilight" });
      await new Promise((r) => setTimeout(r, 120));
      const bad = rowCopy("twilight");
      const cacheStatus = (store.sceneFidelityFor("twilight") || {}).status;
      store.state.sceneFidelity["twilight"] = originalEntry;
      store.select({ type: "scene", id: "twilight" });
      return {
        okHasFid: ok.hasFid,
        okHasBuckets: looksLikeBuckets(ok.text),
        badHasFid: bad.hasFid,
        badHasBuckets: looksLikeBuckets(bad.text),
        cacheStatus,
      };
    })()`);
    check(
      "scenes: rows never show nat/eq/approx/unsup bucket counts",
      !!fidelityFailFlow && fidelityFailFlow.okHasFid === false && fidelityFailFlow.okHasBuckets === false,
      JSON.stringify(fidelityFailFlow)
    );
    check(
      "scenes: a failed preview does not dump fidelity copy onto the catalog row",
      !!fidelityFailFlow &&
        fidelityFailFlow.badHasFid === false &&
        fidelityFailFlow.badHasBuckets === false &&
        fidelityFailFlow.cacheStatus === "unavailable",
      JSON.stringify(fidelityFailFlow)
    );

    // 7bd. Canonical validation surfacing (§7): a preview-time validation
    //      failure must land on the field that caused it (and clear when the
    //      input is corrected), and a failed preview must never leave the
    //      previous panel on screen as if it still described the draft.
    const fieldErrorFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const setV = (input, value) => {
        input.value = value;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      };
      await store.setScenario("all-healthy");
      store.setView("scenes");
      await store.openBuilder({});
      await waitFor(() => store.state.builder && store.state.builder.mode === "create");
      const broot = () => dom(root().querySelector("ss-view-scene-builder"));
      await waitFor(() => broot() && broot().querySelector("#builder-name"));
      setV(broot().querySelector("#builder-name"), "Field Error Probe");
      const target = broot().querySelector("input.builder-target[value='office']");
      target.checked = true;
      target.dispatchEvent(new Event("change", { bubbles: true }));
      await waitFor(() => store.state.builder.draft.target_ids.includes("office"));
      broot().querySelector(".builder-state-set-color").click();
      await waitFor(() => typeof store.state.builder.draft.default_state.color === "string");
      const hexInput = () => broot().querySelector(".state-color-hex[data-scope='default']");
      setV(hexInput(), "#zzzzzz");
      await waitFor(() => store.state.builder.draft.default_state.color === "#zzzzzz");
      broot().querySelector("#builder-preview").click();
      await waitFor(() => store.state.builder.previewError);
      const fieldError = broot().querySelector("#builder-default-state-error");
      const marked = hexInput().classList.contains("invalid");
      // The panel that may still exist is the ERROR panel (same ids by
      // design); what matters is that no server plan is left in state and the
      // headline is not a "ready" one.
      const previewCleared = store.state.builder.preview === null;
      const headAfterFailure = broot().querySelector("#builder-preview-head");
      const errorOnlyHeadline =
        !!headAfterFailure && headAfterFailure.classList.contains("bad") && !/ready/i.test(headAfterFailure.textContent);
      setV(hexInput(), "#112233");
      await waitFor(() => !store.state.builder.previewError);
      const cleared = !broot().querySelector("#builder-default-state-error");
      const unmarked = !hexInput().classList.contains("invalid");
      const staleHintAfterCorrection = !!broot().querySelector("#builder-preview-stale");
      store.closeBuilder();
      await store.setView("scenes");
      return {
        fieldErrorShown: !!fieldError,
        fieldErrorText: fieldError ? fieldError.textContent.trim() : "",
        marked,
        previewCleared,
        errorOnlyHeadline,
        cleared,
        unmarked,
        staleHintAfterCorrection,
      };
    })()`);
    check(
      "builder §7: a canonical validation error is surfaced on the offending field and clears when corrected",
      !!fieldErrorFlow &&
        fieldErrorFlow.fieldErrorShown === true &&
        /color/.test(fieldErrorFlow.fieldErrorText) &&
        fieldErrorFlow.marked === true &&
        fieldErrorFlow.cleared === true &&
        fieldErrorFlow.unmarked === true &&
        fieldErrorFlow.staleHintAfterCorrection === true,
      JSON.stringify(fieldErrorFlow)
    );
    check(
      "builder §7: a failed preview clears the previous server plan and shows only a bad headline",
      !!fieldErrorFlow && fieldErrorFlow.previewCleared === true && fieldErrorFlow.errorOnlyHeadline === true,
      JSON.stringify(fieldErrorFlow)
    );

    // 7c. Read-only runtime policy: the Builder stays REACHABLE for
    //     observational authoring (scene.preview_draft), while persistence
    //     affordances are visibly blocked.
    const builderRo = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 5000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 60));
        }
        return null;
      };
      const realRuntime = store.state.status.runtime;
      store.state.status = {
        ...store.state.status,
        runtime: { mode: "read_only", read_only: true, provider_writes_blocked: true, allowed_commands: ["scene.preview_draft", "scene.preview", "scene.apply"] },
      };
      await store.setView("scenes");
      const scenesRoot = () => dom(root().querySelector("ss-view-scenes"));
      const newBtn = await waitFor(() => scenesRoot() && scenesRoot().querySelector("#new-scene"));
      const newDisabled = newBtn ? newBtn.disabled : null;
      if (newBtn && !newBtn.disabled) newBtn.click();
      const brootWait = await waitFor(
        () => store.state.view === "scene_builder" && root().querySelector("ss-view-scene-builder") && dom(root().querySelector("ss-view-scene-builder")).querySelector("#builder-save")
      );
      const broot = () => dom(root().querySelector("ss-view-scene-builder"));
      // Give the draft valid name/target data so Save's disabled state can
      // only come from the read-only policy, not missing fields.
      store.patchBuilderDraft({ name: "RO Draft", target_ids: ["office"] });
      await new Promise((r) => setTimeout(r, 60));
      const saveDisabled = broot() && broot().querySelector("#builder-save") ? broot().querySelector("#builder-save").disabled : null;
      const saveTitle = broot() && broot().querySelector("#builder-save") ? broot().querySelector("#builder-save").title : "";
      const previewEnabled = broot() && broot().querySelector("#builder-preview") ? !broot().querySelector("#builder-preview").disabled : null;
      const catalogBefore = store.state.scenes.scenes.length;
      let previewOk = false;
      let previewToneHonest = false;
      if (previewEnabled) {
        broot().querySelector("#builder-preview").click();
        previewOk = !!(await waitFor(() => broot().querySelector("#builder-preview-panel #builder-preview-head")));
        if (previewOk) {
          const head = broot().querySelector("#builder-preview-head");
          const plan = store.state.builder.preview.render_plan;
          const hasReduction =
            (plan.fixture_plans || []).some((p) => p.fidelity === "approximate" || p.fidelity === "unsupported") ||
            (plan.skipped_fixture_ids || []).length > 0;
          previewToneHonest = hasReduction ? !head.classList.contains("ok") : head.classList.contains("ok");
        }
      }
      const catalogUnchanged = store.state.scenes.scenes.length === catalogBefore;
      store.state.status = { ...store.state.status, runtime: realRuntime };
      store.closeBuilder();
      await store.setView("scenes");
      return { newDisabled, builderOpened: !!brootWait, saveDisabled, saveTitle, previewEnabled, previewOk, previewToneHonest, catalogUnchanged };
    })()`);
    check(
      "builder read-only: New Scene entry stays available (observational preview_draft)",
      !!builderRo && builderRo.newDisabled === false && builderRo.builderOpened === true,
      JSON.stringify(builderRo)
    );
    check(
      "builder read-only: Save is visibly blocked while Preview works without any catalog mutation",
      !!builderRo && builderRo.saveDisabled === true && builderRo.saveTitle.length > 0 && builderRo.previewEnabled === true && builderRo.previewOk === true && builderRo.previewToneHonest === true && builderRo.catalogUnchanged === true,
      JSON.stringify(builderRo)
    );

    // 7.9 First-run setup flow (portable pass): the empty-registry scenario
    // must land on the Setup view, and the rendered adopt → target flow must
    // actually mutate the registry through the store (mock engine).
    const setupFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 6000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 50));
        }
        return null;
      };
      // Land via the shell's auto-switch: overview + empty install -> setup.
      store.setView("overview");
      await store.setScenario("empty-registry");
      const landed = await waitFor(() => {
        const view = root().querySelector("ss-view-setup");
        if (!view) return null;
        const current = root().querySelector('nav.tabs button[aria-current="page"]');
        return current && current.textContent.trim() === "Setup" ? view : null;
      });
      if (!landed) return { landed: false };
      const viewRoot = () => dom(root().querySelector("ss-view-setup"));
      const tabs = [...root().querySelectorAll("nav.tabs button")].map((b) => b.textContent.trim());
      const intro = viewRoot().querySelector('[data-ss="setup-intro"]');
      const modeText = intro ? intro.textContent : "";
      const providerChips = viewRoot().querySelectorAll(".provider").length;
      // Adopt the first unbound observation through the rendered form.
      const firstRow = () => viewRoot().querySelector('[data-ss="adopt-button"]');
      const row = () => firstRow() && firstRow().closest('[data-ss="setup-observation"]');
      const fixturesBefore = store.state.fixtures.fixtures.length;
      let adopted = false;
      if (row()) {
        const domRow = row();
        const idInput = domRow.querySelector('[data-ss="adopt-id"]');
        const nameInput = domRow.querySelector('[data-ss="adopt-name"]');
        idInput.value = "first_light";
        idInput.dispatchEvent(new Event("input", { bubbles: true }));
        nameInput.value = "First Light";
        nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise((r) => setTimeout(r, 30));
        row().querySelector('[data-ss="adopt-button"]').click();
        adopted = await waitFor(() => store.state.fixtures.fixtures.length === fixturesBefore + 1);
      }
      // Create a target with the adopted fixture as a member.
      const nameInput = viewRoot().querySelector('[data-ss="target-name"]');
      let targetCreated = false;
      if (nameInput && adopted) {
        nameInput.value = "First Room";
        nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        const checkbox = viewRoot().querySelector('[data-ss="target-member-checkbox"]');
        if (checkbox) {
          checkbox.click();
          await new Promise((r) => setTimeout(r, 30));
        }
        viewRoot().querySelector('[data-ss="target-create-button"]').click();
        targetCreated = await waitFor(() => {
          const rows = [...viewRoot().querySelectorAll('[data-ss="setup-target"]')];
          return rows.some((r) => /1 member\\(s\\)/.test(r.textContent));
        });
      }
      const summary = viewRoot().querySelector('[data-ss="setup-registry-summary"]');
      const yaml = viewRoot().querySelector("pre.yaml");
      return {
        landed: true,
        tabs,
        setupTabCurrent: true,
        introMentionsAdmin: /registry_admin/.test(modeText),
        providerChips,
        adopted: !!adopted,
        targetCreated: !!targetCreated,
        summaryText: summary ? summary.textContent.replace(/\\s+/g, " ").trim() : "",
        yamlShowsUnlock: !!yaml && /registry_admin: false/.test(yaml.textContent) && /read_only: false/.test(yaml.textContent),
      };
    })()`);
    check(
      "setup: empty install auto-lands on the Setup view with the Setup tab current",
      !!setupFlow && setupFlow.landed === true && setupFlow.setupTabCurrent === true,
      JSON.stringify(setupFlow || {})
    );
    check(
      "setup: intro names registry_admin and provider health renders",
      !!setupFlow && setupFlow.introMentionsAdmin === true && setupFlow.providerChips >= 1,
      JSON.stringify(setupFlow || {})
    );
    check(
      "setup: rendered adopt form creates a fixture through the store",
      !!setupFlow && setupFlow.adopted === true,
      JSON.stringify(setupFlow || {})
    );
    check(
      "setup: rendered target form creates a target with membership",
      !!setupFlow && setupFlow.targetCreated === true,
      JSON.stringify(setupFlow || {})
    );
    check(
      "setup: registry summary counts and the exit-registry_admin yaml are honest",
      !!setupFlow && /1 fixture\(s\)/.test(setupFlow.summaryText) && /1 target\(s\)/.test(setupFlow.summaryText) && setupFlow.yamlShowsUnlock === true,
      JSON.stringify(setupFlow || {})
    );

    // 7.9.1 A mature install must NOT offer the Setup tab (restore first).
    const matureNav = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      await store.setScenario("all-healthy");
      store.setView("overview");
      await app().updateComplete;
      return {
        tabs: [...dom(app()).querySelectorAll("nav.tabs button")].map((b) => b.textContent.trim()),
      };
    })()`);
    check(
      "setup: a mature install shows no Setup tab",
      !!matureNav && Array.isArray(matureNav.tabs) && !matureNav.tabs.includes("Setup"),
      JSON.stringify(matureNav || {})
    );

    // 7.9.1b Helper contract (corrective cleanup): Setup availability means
    // EMPTY registry OR registry_admin — the empty branch must hold
    // INDEPENDENTLY of runtime mode (the #setupAvailable -> #isEmptyInstall
    // call passed `status` where application state was expected, silently
    // disabling exactly this branch). Asserted through the observable nav.
    const helperContract = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const tabs = () => [...root().querySelectorAll("nav.tabs button")].map((b) => b.textContent.trim());
      const withStatus = async (patch) => {
        Object.assign(store.state.status, patch);
        app().requestUpdate();
        await app().updateComplete;
      };
      await store.setScenario("all-healthy");
      store.setView("overview");
      // populated + normal: Setup hidden
      await withStatus({ runtime: { ...(store.state.status.runtime || {}), mode: "normal" } });
      const populatedNormalTabs = tabs();
      // EMPTY registry with a NON-admin mode: Setup must STILL be offered
      await withStatus({
        fixtures: { ...(store.state.status.fixtures || {}), total: 0 },
        runtime: { ...(store.state.status.runtime || {}), mode: "normal" },
      });
      const emptyNormalTabs = tabs();
      await store.setScenario("all-healthy");
      store.setView("overview");
      return { populatedNormalTabs, emptyNormalTabs };
    })()`);
    check(
      "setup: an empty registry satisfies Setup availability independently of runtime mode",
      !!helperContract && Array.isArray(helperContract.emptyNormalTabs) && helperContract.emptyNormalTabs.includes("Setup"),
      JSON.stringify(helperContract || {})
    );
    check(
      "setup: a populated normal-mode install still hides Setup (helper contract other half)",
      !!helperContract && Array.isArray(helperContract.populatedNormalTabs) && !helperContract.populatedNormalTabs.includes("Setup"),
      JSON.stringify(helperContract || {})
    );

    // 7.9.2 Resumable bootstrap (corrective pass): adopt exactly ONE fixture,
    // navigate away, and prove Setup is STILL reachable — a partially
    // populated registry in registry_admin must not strand the operator.
    // The nav derives from the backend runtime mode (re-read on every
    // reload/reconnect/new initialization), so the mode-derived tab presence
    // here is the reload-equivalent assertion; the mock client cannot carry
    // state across a literal page reload.
    const resumeFlow = await evaluate(`(async () => {
      const app = () => document.querySelector("ss-app");
      const store = app().store;
      const root = () => dom(app());
      const waitFor = async (fn, ms = 8000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < ms) {
          try { const v = fn(); if (v) return v; } catch {}
          await new Promise((r) => setTimeout(r, 50));
        }
        return null;
      };
      store.setView("overview");
      await store.setScenario("empty-registry");
      // The shell's ONE-TIME auto-land was consumed by the earlier 7.9
      // section; a returning operator reaches Setup through the visible tab.
      store.setView("setup");
      const setupView = () => root().querySelector("ss-view-setup");
      const ready = await waitFor(() => setupView() && dom(setupView()).querySelector('[data-ss="adopt-button"]'));
      if (!ready) return { failed: "setup view or adopt form did not render" };
      // step 2 of the flow: run discovery through the store command
      await store.sendCommand({ command: "discovery.run" });
      await waitFor(() => dom(setupView()).querySelector('[data-ss="setup-discovery-summary"]'));
      // adopt exactly one fixture through the rendered form
      const adoptButton = dom(setupView()).querySelector('[data-ss="adopt-button"]');
      if (!adoptButton) return { failed: "no adopt button after discovery" };
      const domRow = adoptButton.closest('[data-ss="setup-observation"]');
      const idInput = domRow.querySelector('[data-ss="adopt-id"]');
      idInput.value = "resumed_light";
      idInput.dispatchEvent(new Event("input", { bubbles: true }));
      await new Promise((r) => setTimeout(r, 30));
      dom(setupView()).querySelector('[data-ss="adopt-button"]').click();
      const adopted = await waitFor(() => store.state.fixtures.fixtures.length === 1);
      // the operator moves on to another view
      store.setView("overview");
      await app().updateComplete;
      const tabsAfterLeave = [...root().querySelectorAll("nav.tabs button")].map((b) => b.textContent.trim());
      const runtimeMode = store.state.status.runtime.mode;
      // come back through the visible Setup tab like an operator would
      const setupTab = [...root().querySelectorAll("nav.tabs button")].find((b) => b.textContent.trim() === "Setup");
      if (setupTab) setupTab.click();
      const backOnSetup = await waitFor(() => root().querySelector("ss-view-setup"));
      // complete the bootstrap: create a target with the adopted fixture
      const viewRoot = () => dom(root().querySelector("ss-view-setup"));
      const nameInput = viewRoot().querySelector('[data-ss="target-name"]');
      if (!nameInput) return { failed: "target form missing on resumed setup view" };
      nameInput.value = "Resume Room";
      nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      const checkbox = viewRoot().querySelector('[data-ss="target-member-checkbox"]');
      if (checkbox) {
        checkbox.click();
        await new Promise((r) => setTimeout(r, 30));
      }
      viewRoot().querySelector('[data-ss="target-create-button"]').click();
      const target = await waitFor(() => (store.state.fixtures.targets || []).find((t) => t.name === "Resume Room"));
      const memberOk = !!(adopted && target && (store.state.fixtures.fixtures[0].groups || []).includes(target.id));
      return {
        adopted: !!adopted,
        runtimeMode,
        tabsAfterLeave,
        setupStillReachable: tabsAfterLeave.includes("Setup") && !!backOnSetup,
        targetCreated: !!target,
        memberOk,
        fixturesTotal: store.state.fixtures.fixtures.length,
      };
    })()`);
    check(
      "setup: one adopted fixture later, registry_admin keeps Setup reachable (resumable bootstrap)",
      !!resumeFlow && resumeFlow.adopted === true && resumeFlow.runtimeMode === "registry_admin" &&
        resumeFlow.setupStillReachable === true,
      JSON.stringify(resumeFlow || {})
    );
    check(
      "setup: target.create completes the bootstrap on the partially populated registry",
      !!resumeFlow && resumeFlow.targetCreated === true && resumeFlow.memberOk === true,
      JSON.stringify(resumeFlow || {})
    );
    // restore the default scenario for the responsive section
    await evaluate(`(async () => {
      const store = document.querySelector("ss-app").store;
      store.setView("overview");
      await store.setScenario("all-healthy");
      return true;
    })()`);

    // 8. Responsive validation (Builder-expansion §9): at each shipped width
    //    the Builder must not ADD page-level horizontal overflow, its panels
    //    and primary actions must stay visible, and icon-only buttons must
    //    keep accessible names. Baseline is the Scenes view at the same width
    //    so a pre-existing shell overflow is never misattributed to the Builder.
    const responsiveWidths = [
      [1440, 900],
      [700, 900],
      [390, 844],
    ];
    const responsive = {};
    for (const [width, height] of responsiveWidths) {
      await send("Emulation.setDeviceMetricsOverride", {
        width,
        height,
        deviceScaleFactor: 1,
        mobile: false,
      });
      await sleep(150);
      responsive[width] = await evaluate(`(async () => {
        const app = () => document.querySelector("ss-app");
        const store = app().store;
        const root = () => dom(app());
        const waitFor = async (fn, ms = 6000) => {
          const t0 = Date.now();
          while (Date.now() - t0 < ms) {
            try { const v = fn(); if (v) return v; } catch {}
            await new Promise((r) => setTimeout(r, 50));
          }
          return null;
        };
        const overflow = () => {
          const doc = document.documentElement;
          return doc.scrollWidth - doc.clientWidth;
        };
        await store.setScenario("all-healthy");
        store.setView("scenes");
        await waitFor(() => root().querySelector("ss-view-scenes"));
        await new Promise((r) => setTimeout(r, 60));
        const scenesOverflow = overflow();
        await store.openBuilder({ sceneId: "aurora_flow" });
        await waitFor(() => store.state.builder && root().querySelector("ss-view-scene-builder"));
        const broot = () => dom(root().querySelector("ss-view-scene-builder"));
        await waitFor(() => broot() && broot().querySelector("#builder-save"));
        await new Promise((r) => setTimeout(r, 60));
        const builderOverflow = overflow();
        const visible = (el) => {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };
        const unnamed = [...broot().querySelectorAll("button")].filter(
          (b) => !(b.getAttribute("aria-label") || b.textContent || "").trim()
        ).length;
        const rootEl = broot().querySelector(":scope > .section, :scope > div");
        const result = {
          scenesOverflow,
          builderOverflow,
          builderScrollBox: rootEl ? rootEl.scrollWidth - rootEl.clientWidth : 0,
          name: visible(broot().querySelector("#builder-name")),
          save: visible(broot().querySelector("#builder-save")),
          preview: visible(broot().querySelector("#builder-preview")),
          cancel: visible(broot().querySelector("#builder-cancel")),
          targets: broot().querySelectorAll("input.builder-target").length,
          defaults: visible(broot().querySelector("#builder-default-brightness-num")),
          overrides: visible(broot().querySelector("#builder-overrides")),
          palettePreview: !!broot().querySelector("#builder-palette-preview"),
          unnamedButtons: unnamed,
        };
        store.closeBuilder();
        await store.setView("scenes");
        return result;
      })()`);
    }
    await send("Emulation.clearDeviceMetricsOverride");
    await sleep(120);
    for (const [width] of responsiveWidths) {
      const r = responsive[width] || {};
      check(
        `responsive ${width}: the Builder adds no page-level horizontal overflow`,
        typeof r.builderOverflow === "number" &&
          typeof r.scenesOverflow === "number" &&
          r.builderOverflow <= r.scenesOverflow + 1,
        JSON.stringify({ scenesOverflow: r.scenesOverflow, builderOverflow: r.builderOverflow })
      );
      check(
        `responsive ${width}: Builder hierarchy + primary actions stay visible and reachable`,
        r.name === true &&
          r.save === true &&
          r.preview === true &&
          r.cancel === true &&
          r.targets > 0 &&
          r.defaults === true &&
          r.overrides === true &&
          r.palettePreview === true,
        JSON.stringify(r)
      );
      check(
        `responsive ${width}: every Builder button keeps an accessible name`,
        r.unnamedButtons === 0,
        JSON.stringify({ unnamedButtons: r.unnamedButtons })
      );
    }
    // Reduced-motion preference is preserved in the shipped bundle (Lit
    // component styles live in the JS chunk, not the extracted CSS asset).
    const assetNames = readdirSync(join(DIST, "assets"));
    const bundleText = assetNames.map((name) => readFileSync(join(DIST, "assets", name), "utf8")).join("\n");
    check(
      "responsive: the shipped bundle still honors prefers-reduced-motion",
      bundleText.includes("prefers-reduced-motion"),
      assetNames.join(", ")
    );
    check(
      "responsive: Builder controls ship a visible keyboard focus style",
      bundleText.includes(":focus-visible") && bundleText.includes("ss-view-scene-builder"),
      assetNames.join(", ")
    );

    check("browser: zero console errors across the whole run", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 400));

    console.log(`\n${passed} passed, ${failed} failed`);
    if (failed > 0) process.exitCode = 1;
  } finally {
    try {
      if (ws) ws.close();
    } catch {}
    try {
      chrome.child.kill();
    } catch {}
    await sleep(200);
    try {
      rmSync(chrome.userDataDir, { recursive: true, force: true });
    } catch {}
    server.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
