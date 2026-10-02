// Installer UI only: exercise progressive disclosure in a real browser.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(process.argv[3]).href);
const url = process.argv[2];
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url);
  await page.getByRole("button", { name: "3 Detected setup" }).click();
  await page.getByRole("heading", { name: "Home Assistant + AppDaemon detected" }).waitFor();
  const advanced = page.locator("details").filter({ has: page.locator("summary", { hasText: "Configure advanced topology" }) });
  assert.equal(await advanced.getAttribute("open"), null);
  assert.equal(await page.getByText("Home Assistant filesystem host (SSH)", { exact: true }).isVisible(), false);
  await page.getByRole("button", { name: "Continue to lighting sources" }).click();
  await page.getByRole("heading", { name: /Lighting sources/ }).waitFor();
  await page.getByRole("button", { name: "3 Detected setup" }).click();
  await page.locator("summary", { hasText: "Configure advanced topology" }).click();
  assert.equal(await page.getByText("Home Assistant filesystem host (SSH)", { exact: true }).isVisible(), true);
  // Force only HTTP detection failure; this remains a read-only probe.
  await page.evaluate(async () => {
    const token = new URLSearchParams(location.search).get("token");
    const headers = { "X-Scene-Studio-Token": token, "Content-Type": "application/json" };
    await fetch("/api/answers", { method: "POST", headers, body: JSON.stringify({ appdaemon_http_url: "http://unreachable.example:5051" }) });
    await fetch("/api/probe", { method: "POST", headers, body: JSON.stringify({ names: ["topology"] }) });
  });
  await page.reload();
  await page.getByRole("button", { name: "3 Detected setup" }).click();
  await page.getByRole("heading", { name: "Automatic setup detection was incomplete." }).waitFor();
  assert.notEqual(await advanced.getAttribute("open"), null);
  assert.equal(await page.getByText("Home Assistant filesystem host (SSH)", { exact: true }).isVisible(), true);
  assert.deepEqual(errors, []);
  console.log("Installer browser topology checks passed");
} finally {
  await browser.close();
}
