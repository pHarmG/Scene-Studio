#!/usr/bin/env node
/**
 * Functional contract check for the Scene Studio compact card (HA control-
 * card rework plan §13) — the parts a static screenshot can't prove:
 *
 *   1. scene type/action mapping (static->apply, dynamic idle->start,
 *      active->pause, paused->resume) fires the exact expected command;
 *   2. lifecycle commands for a live session use the exact backend-issued
 *      session_id, never a scene_id;
 *   3. plural-session safety: 2+ live sessions never sends an implicit
 *      lifecycle command from the primary action, and each session in the
 *      drawer is addressed independently (the sibling stays untouched);
 *   4. read-only/blocked policy visibly disables the primary action instead
 *      of waiting for an avoidable failure;
 *   5. a command failure surfaces the error text and does not leave the
 *      button claiming a state change (busy clears, no optimistic flip);
 *   0. element registration: `scene-studio-card` is the canonical element
 *      (Lovelace type `custom:scene-studio-card`) and the compatibility
 *      alias `test-bench-scene-controls-card` resolves to the same card
 *      class (its constructor directly extends SceneStudioCard and
 *      alias-created elements are SceneStudioCard instances), so unmigrated
 *      dashboards keep working.
 *
 * Uses the same mock (preview/mock-projection.mjs) the visual review does,
 * through review/interaction.html, which exposes every fired command
 * verbatim on window.__firedCommands. Playwright's CSS engine pierces open
 * shadow roots (Lit's default), so ordinary selectors reach the card's
 * internal buttons directly.
 */
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname, extname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
// URL space root: /scene-studio-card/<path> maps onto home-assistant/scene-studio-card/<path>.
const SERVE_ROOT = join(ROOT, "..");
const PORT = 4177;

const MIME = { ".html": "text/html; charset=utf-8", ".js": "application/javascript", ".mjs": "application/javascript" };

function resolvePath(pathname) {
  const rel = pathname.replace(/^\//, "");
  return join(SERVE_ROOT, rel);
}

function createStaticServer() {
  return createServer((req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
  const filePath = resolvePath(pathname);
  if (!filePath.startsWith(SERVE_ROOT) || !existsSync(filePath)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, { "Content-Type": MIME[extname(filePath)] ?? "application/octet-stream" });
    res.end(readFileSync(filePath));
  });
}

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

async function main() {
  if (!existsSync(join(ROOT, "dist", "scene-studio-card.js"))) {
    console.error("Run 'npm run build' first.");
    process.exit(1);
  }

  let playwright;
  try {
    playwright = await import("playwright");
  } catch {
    console.error("Playwright not installed. Run: npx playwright install chromium");
    process.exit(1);
  }

  const server = createStaticServer();
  await new Promise((resolve) => server.listen(PORT, "127.0.0.1", resolve));
  const browser = await playwright.chromium.launch({ headless: true, args: ["--no-sandbox"] });

  const openScenario = async (id) => {
    const page = await browser.newPage({ viewport: { width: 500, height: 700 } });
    page.on("pageerror", (e) => console.error(`  [pageerror ${id}]`, e.message));
    page.on("console", (m) => {
      if (m.type() === "error") console.error(`  [console.error ${id}]`, m.text());
    });
    const url = `http://127.0.0.1:${PORT}/scene-studio-card/review/interaction.html?scenario=${id}`;
    // The lightweight static server below occasionally drops a rapid,
    // back-to-back connection on Windows (ERR_CONNECTION_FAILED with no
    // page error) — a local-harness flake, not a card defect. Retry the
    // navigation a couple of times before failing the check.
    let lastError;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        await page.goto(url);
        lastError = null;
        break;
      } catch (err) {
        lastError = err;
        await new Promise((r) => setTimeout(r, 200));
      }
    }
    if (lastError) throw lastError;
    await page.waitForFunction(() => window.__cardReady === true, { timeout: 10000 });
    return page;
  };

  try {
    // 0. element registration: the canonical `scene-studio-card` element
    //    (Lovelace type `custom:scene-studio-card`) resolves, and the
    //    compatibility alias `test-bench-scene-controls-card` resolves to
    //    the same card class — a registry binds one constructor to one
    //    name, so the alias is a direct subclass of SceneStudioCard and an
    //    alias-created element upgrades as a genuine SceneStudioCard.
    {
      const page = await openScenario("static-selected");
      const reg = await page.evaluate(() => {
        const primary = window.__elementRegistration?.primary ?? null;
        const alias = window.__elementRegistration?.alias ?? null;
        const aliasInstance = document.createElement("test-bench-scene-controls-card");
        return {
          primaryDefined: typeof primary === "function",
          aliasDefined: typeof alias === "function",
          aliasExtendsPrimary: primary !== null && alias !== null && Object.getPrototypeOf(alias) === primary,
          aliasInstanceUpgrades: primary !== null && aliasInstance instanceof primary,
        };
      });
      check(
        "custom:scene-studio-card resolves (customElements.get returns the constructor)",
        reg.primaryDefined,
        JSON.stringify(reg),
      );
      check(
        "compatibility alias test-bench-scene-controls-card resolves to the same card class (extends SceneStudioCard; alias element is a SceneStudioCard instance)",
        reg.aliasDefined && reg.aliasExtendsPrimary && reg.aliasInstanceUpgrades,
        JSON.stringify(reg),
      );
      await page.close();
    }

    // 1. static + idle -> Apply -> scene.apply
    {
      const page = await openScenario("static-selected");
      await page.click(".controlRow .actionButton.accent");
      const fired = await page.evaluate(() => window.__firedCommands);
      check("static + idle -> Apply fires scene.apply with the exact scene_id", fired.length === 1 && fired[0].command === "scene.apply" && fired[0].scene_id === "twilight", JSON.stringify(fired));
      await page.close();
    }

    // 2. dynamic + no session -> the primary action is Apply (bolt), exactly
    //    like static scenes; dynamic playback moved into the overflow menu.
    {
      const page = await openScenario("dynamic-idle");
      await page.click(".controlRow .actionButton.accent");
      let fired = await page.evaluate(() => window.__firedCommands);
      check(
        "dynamic + idle -> primary action is Apply (scene.apply), never an implicit Play",
        fired.length === 1 && fired[0].command === "scene.apply" && fired[0].scene_id === "aurora_flow",
        JSON.stringify(fired),
      );
      // The dynamic alternative lives in the overflow menu, gated by policy.
      await page.click(".overflowTrigger");
      await page.waitForTimeout(200);
      const playItem = page.locator(".overflowMenu .overflowItem", { hasText: "Play dynamically" });
      check("dynamic + idle -> overflow offers 'Play dynamically'", (await playItem.count()) === 1);
      await playItem.click();
      await page.waitForTimeout(200);
      fired = await page.evaluate(() => window.__firedCommands);
      check(
        "overflow 'Play dynamically' fires playback.start with the scene_id",
        fired.length === 2 && fired[1].command === "playback.start" && fired[1].scene_id === "aurora_flow",
        JSON.stringify(fired),
      );
      await page.close();
    }

    // 2b. static + idle -> the overflow carries no 'Play dynamically' item.
    {
      const page = await openScenario("static-selected");
      await page.click(".overflowTrigger");
      await page.waitForTimeout(200);
      const playItems = await page.locator(".overflowMenu .overflowItem", { hasText: "Play dynamically" }).count();
      check("static + idle -> overflow has no 'Play dynamically' item", playItems === 0, String(playItems));
      await page.close();
    }

    // 3. dynamic + active -> Pause -> playback.pause(exact session_id)
    {
      const page = await openScenario("dynamic-active");
      await page.click(".controlRow .actionButton.accent");
      const fired = await page.evaluate(() => window.__firedCommands);
      check(
        "dynamic + active -> Pause fires playback.pause with the exact session_id (never scene_id)",
        fired.length === 1 && fired[0].command === "playback.pause" && fired[0].session_id === "sess-1" && fired[0].scene_id === undefined,
        JSON.stringify(fired),
      );
      await page.close();
    }

    // 4. dynamic + paused -> Resume -> playback.resume(exact session_id)
    {
      const page = await openScenario("dynamic-paused");
      await page.click(".controlRow .actionButton.accent");
      const fired = await page.evaluate(() => window.__firedCommands);
      check(
        "dynamic + paused -> Resume fires playback.resume with the exact session_id",
        fired.length === 1 && fired[0].command === "playback.resume" && fired[0].session_id === "sess-1",
        JSON.stringify(fired),
      );
      await page.close();
    }

    // 5. orphaned session -> primary action is Stop only (never Pause/Resume)
    {
      const page = await openScenario("dynamic-orphaned");
      // Direct child of .controlRow — the primary action, never the nested
      // overflow trigger (also an unclassed .actionButton one level deeper).
      await page.click(".controlRow > .actionButton");
      const fired = await page.evaluate(() => window.__firedCommands);
      check(
        "orphaned session -> primary action is Stop, addressed by session_id",
        fired.length === 1 && fired[0].command === "playback.stop" && fired[0].session_id === "sess-1",
        JSON.stringify(fired),
      );
      await page.close();
    }

    // 6. plural-session safety: primary action on 2 live sessions never
    //    fires a lifecycle command — it opens the drawer instead.
    {
      const page = await openScenario("multi-session");
      await page.click(".controlRow .actionButton.accent");
      let fired = await page.evaluate(() => window.__firedCommands);
      check("2 live sessions: clicking the primary action fires NO command (opens the drawer)", fired.length === 0, JSON.stringify(fired));
      const drawerVisible = await page.locator(".sessionRow").count();
      check("2 live sessions: the drawer is now showing both sessions", drawerVisible === 2, String(drawerVisible));

      // Pausing the FIRST (active) session must never touch the second.
      await page.locator(".sessionRow", { hasText: "Playing" }).locator(".actionButton").first().click();
      fired = await page.evaluate(() => window.__firedCommands);
      check(
        "2 live sessions: pausing one session addresses exactly that session_id",
        fired.length === 1 && fired[0].command === "playback.pause" && fired[0].session_id === "sess-1",
        JSON.stringify(fired),
      );
      await page.close();
    }

    // 7. read-only/blocked policy visibly disables the primary action
    {
      const page = await openScenario("read-only");
      const disabled = await page.locator(".controlRow .actionButton.accent").getAttribute("disabled");
      check("read_only mode: the primary action is disabled, not just going to fail", disabled !== null, `disabled attr = ${disabled}`);
      await page.close();
    }
    {
      const page = await openScenario("provider-writes-blocked");
      const disabled = await page.locator(".controlRow .actionButton.accent").getAttribute("disabled");
      check("registry_admin (provider_writes_blocked): Apply is disabled truthfully", disabled !== null, `disabled attr = ${disabled}`);
      const archiveEnabled = await page.evaluate(() => {
        const overflowTrigger = document.querySelector("scene-studio-card").shadowRoot.querySelectorAll(".controlRow .actionButton")[1];
        overflowTrigger.click();
        return true;
      });
      check("registry_admin: overflow trigger still opens (archive stays available per policy)", archiveEnabled === true);
      await page.close();
    }

    // 8. command failure never claims a state change; the button returns to
    //    non-busy and the error banner explains what happened. The mock
    //    mirrors the lossy production transport (no `ok` on a rejection),
    //    so this also proves the card's defensive ack parsing.
    {
      const page = await openScenario("command-failure");
      await page.click(".controlRow .actionButton.accent");
      await page.waitForSelector(".errorBanner", { timeout: 3000 });
      const stillBusy = await page.locator(".controlRow .actionButton.busy").count();
      check("command failure: the button leaves the busy state (no stuck spinner)", stillBusy === 0, String(stillBusy));
      const bannerText = await page.locator(".errorBanner").innerText();
      check("command failure: the error banner surfaces the backend's reason", bannerText.includes("Simulated failure"), bannerText);
      await page.close();
    }

    // 8b. slow-but-successful command: the timeout releases the buttons and
    //     shows the no-response banner, then the late ack reconciles it away
    //     (success clears the banner; the command is never re-sent).
    {
      const page = await openScenario("command-delayed");
      await page.click(".controlRow .actionButton.accent");
      await page.waitForSelector(".errorBanner", { timeout: 3000 });
      const bannerText = await page.locator(".errorBanner").innerText();
      check("delayed ack: the timeout shows the no-response banner", bannerText.includes("No response"), bannerText);
      const busyAfterTimeout = await page.locator(".controlRow .actionButton.busy").count();
      check("delayed ack: buttons are released after the timeout", busyAfterTimeout === 0, String(busyAfterTimeout));
      await page.waitForFunction(() => !document.querySelector("scene-studio-card").shadowRoot.querySelector(".errorBanner"), { timeout: 5000 });
      const fired = await page.evaluate(() => window.__firedCommands);
      check("delayed ack: the late ack clears the banner without re-sending", fired.length === 1, JSON.stringify(fired));
      const applied = await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        return card.hass.states["sensor.scene_studio_ui"].attributes.current?.scene_id;
      });
      check("delayed ack: the command actually applied (current scene updated)", applied === "twilight", String(applied));
      await page.close();
    }

    // 9. live brightness trim: dragging the palette strip commits ONE
    //    light.turn_on (or turn_off at 0) to the projection-resolved light
    //    entities, and a plain tap commits nothing.
    {
      const page = await openScenario("dynamic-active");
      const slider = page.locator(".brightnessPill");
      check("dynamic-active: the brightness pill is a live slider", (await slider.count()) === 1 && (await slider.getAttribute("role")) === "slider");
      await slider.scrollIntoViewIfNeeded();
      const before = await slider.getAttribute("aria-valuenow");
      const box = await slider.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 4 });
      await page.mouse.up();
      await page.waitForTimeout(650);
      const calls = await page.evaluate(() => window.__firedServiceCalls);
      const expected = Math.min(100, Number(before) + Math.round((60 / box.width) * 100));
      check(
        "dragging the pill commits exactly one light.turn_on with resolved entities",
        calls.length === 1 &&
          calls[0].domain === "light" &&
          calls[0].service === "turn_on" &&
          calls[0].data.brightness_pct === expected &&
          Array.isArray(calls[0].data.entity_id) &&
          calls[0].data.entity_id.includes("light.hue_g_strip"),
        JSON.stringify(calls),
      );
      // The draft clears once hass echoes the value: valuenow returns to live.
      const now = await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        return card.shadowRoot.querySelector(".brightnessPill")?.getAttribute("aria-valuenow");
      });
      check("after commit the slider reads back the live brightness", Number(now) === expected, `${now} vs ${expected}`);

      // Tap (no drag) commits nothing and does not open the scene picker.
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(200);
      const afterTap = await page.evaluate(() => window.__firedServiceCalls);
      check("tapping the pill commits nothing", afterTap.length === 1, JSON.stringify(afterTap));
      const pickerRows = await page.locator(".panelRow").count();
      check("tapping the pill does not open the scene picker", pickerRows === 0, String(pickerRows));
      await page.close();
    }

    // 10. keyboard slider semantics: arrows adjust, debounce collapses to one
    //     commit; Enter toggles the lights off.
    {
      const page = await openScenario("static-selected");
      const slider = page.locator(".brightnessPill");
      await slider.focus();
      await slider.press("ArrowUp");
      await slider.press("ArrowUp");
      await page.waitForTimeout(650);
      const calls = await page.evaluate(() => window.__firedServiceCalls);
      check(
        "two arrow presses commit one debounced light.turn_on",
        calls.length === 1 && calls[0].service === "turn_on" && typeof calls[0].data.brightness_pct === "number",
        JSON.stringify(calls),
      );
      await slider.press("Enter");
      await page.waitForTimeout(250);
      const calls2 = await page.evaluate(() => window.__firedServiceCalls);
      check(
        "Enter toggles the scene lights off (turn_off, no brightness)",
        calls2.length === 2 && calls2[1].service === "turn_off" && calls2[1].data.brightness_pct === undefined,
        JSON.stringify(calls2),
      );
      await page.close();
    }

    // 10b. the trim survives cleared runtime state: a static scene with no
    //     live sessions and `current: null` (an overnight automation took
    //     over, or AppDaemon restarted) still shows the pill and still
    //     commits, because scene.target_ids resolve the lights (bridge v3).
    {
      const page = await openScenario("static-no-runtime");
      const slider = page.locator(".brightnessPill");
      check(
        "static scene with cleared current: the brightness pill is still rendered",
        (await slider.count()) === 1,
      );
      await slider.focus();
      await slider.press("ArrowUp");
      await page.waitForTimeout(650);
      const calls = await page.evaluate(() => window.__firedServiceCalls);
      check(
        "static scene with cleared current: ArrowUp still commits light.turn_on to the scene's lights",
        calls.length === 1 &&
          calls[0].service === "turn_on" &&
          calls[0].data.entity_id.includes("light.hue_g_strip"),
        JSON.stringify(calls),
      );
      await page.close();
    }

    // 10c. an unreachable light must not cap the trim: the live reading
    //     excludes unavailable entities. Live 2026-09-30: 13 lights at 100%
    //     plus one dead entity averaged to 93%, so 100% was unreachable.
    {
      const page = await openScenario("static-selected");
      await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        for (const id of ["light.hue_g_strip", "light.lamp"]) {
          card.hass.states[id] = { state: "on", attributes: { friendly_name: id, brightness: 255 } };
        }
        card.hass = { ...card.hass };
      });
      const now = await page.locator(".brightnessPill").getAttribute("aria-valuenow");
      check(
        "every reachable light at full: the pill reads 100%, not the dead-light-diluted mean",
        now === "100",
        String(now),
      );
      await page.locator(".brightnessPill").focus();
      await page.keyboard.press("ArrowDown");
      await page.waitForTimeout(650);
      const calls = await page.evaluate(() => window.__firedServiceCalls);
      check(
        "the trim still addresses the dead light so it rejoins the scene when it recovers",
        calls.length === 1 &&
          calls[0].service === "turn_on" &&
          calls[0].data.brightness_pct === 95 &&
          calls[0].data.entity_id.includes("light.custom_gradient"),
        JSON.stringify(calls),
      );
      await page.close();
    }

    // 11. overflow popover: top layer (never clipped by ha-card) and flips
    //     above the trigger when the viewport bottom is closer than the menu.
    {
      const page = await openScenario("provider-writes-blocked");
      // Pin the trigger 8px above the viewport bottom edge: a genuine
      // no-room-below embed, the image-2 clipping case.
      await page.setViewportSize({ width: 500, height: 360 });
      await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        const rect = card.shadowRoot.querySelector(".overflowTrigger").getBoundingClientRect();
        window.scrollBy(0, rect.bottom - window.innerHeight + 8);
      });
      await page.waitForTimeout(200);
      await page.click(".overflowTrigger");
      await page.waitForTimeout(250);
      const menuState = await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        const sr = card.shadowRoot;
        const menu = sr.querySelector(".overflowMenu");
        const box = menu.getBoundingClientRect();
        const trigger = sr.querySelector(".overflowTrigger").getBoundingClientRect();
        // document.elementFromPoint retargets open-shadow hits to the host;
        // the shadow root's own hit-test sees the top-layer menu.
        const hit = sr.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
        const insideMenu = hit ? menu === hit || menu.contains(hit) : false;
        return {
          open: menu.matches(":popover-open"),
          box: { top: box.top, bottom: box.bottom, height: box.height },
          triggerTop: trigger.top,
          insideMenu,
          viewport: window.innerHeight,
        };
      });
      check("overflow menu opens as a native popover", menuState.open === true);
      check(
        "overflow menu fits the viewport (nothing runs off the embed edge)",
        menuState.box.bottom <= menuState.viewport + 1 && menuState.box.top >= -1,
        JSON.stringify(menuState.box),
      );
      check(
        "overflow menu flips above the trigger near the bottom edge",
        menuState.box.top < menuState.triggerTop,
        `menu.top=${menuState.box.top} trigger.top=${menuState.triggerTop}`,
      );
      check("overflow menu is on the top layer (hit-test reaches its items)", menuState.insideMenu === true);
      // Scrolling the dashboard closes it (fixed menu must not detach).
      await page.mouse.wheel(0, -120);
      await page.waitForTimeout(250);
      const stillOpen = await page.evaluate(() => {
        const card = document.querySelector("scene-studio-card");
        return card.shadowRoot.querySelector(".overflowMenu").matches(":popover-open");
      });
      check("scrolling the page closes the popover (no detached menu)", stillOpen === false);
      await page.close();
    }

    console.log(`\n${passed} passed, ${failed} failed`);
    if (failed > 0) process.exitCode = 1;
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
