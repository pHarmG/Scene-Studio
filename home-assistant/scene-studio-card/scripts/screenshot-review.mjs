import { createServer } from "http";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "fs";
import { join, dirname, extname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, "..");
// URL space root: /scene-studio-card/<path> maps onto home-assistant/scene-studio-card/<path>.
const SERVE_ROOT = join(ROOT, "..");
const DEFAULT_OUT_DIR = "review-artifacts";
const DEFAULT_PORT = 4176;

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript",
  ".mjs": "application/javascript",
  ".json": "application/json",
  ".png": "image/png",
};

function parseArgs() {
  const outDir = process.argv.find((a) => a.startsWith("--out-dir="))?.split("=")[1] ?? DEFAULT_OUT_DIR;
  const port = parseInt(process.argv.find((a) => a.startsWith("--port="))?.split("=")[1] ?? DEFAULT_PORT, 10);
  return { outDir, port };
}

function resolveRepoPublicPath(serveRoot, pathname) {
  const rel = pathname.replace(/^\//, "");
  return join(serveRoot, rel);
}

function createStaticServer(serveRoot) {
  return createServer((req, res) => {
    let pathname = new URL(req.url, "http://localhost").pathname;
    if (pathname === "/") pathname = "/scene-studio-card/review/index.html";
    const filePath = resolveRepoPublicPath(serveRoot, pathname);
    if (!filePath.startsWith(serveRoot)) {
      res.writeHead(403);
      res.end();
      return;
    }
    if (!existsSync(filePath)) {
      res.writeHead(404);
      res.end();
      return;
    }
    res.writeHead(200, { "Content-Type": MIME[extname(filePath)] ?? "application/octet-stream" });
    res.end(readFileSync(filePath));
  });
}

function runId() {
  return new Date().toISOString().replace(/[:.]/g, "").slice(0, 15);
}

async function main() {
  const { outDir, port } = parseArgs();
  if (!existsSync(join(ROOT, "dist", "scene-studio-card.js"))) {
    console.error("Run 'npm run build' first.");
    process.exit(1);
  }

  const artifactRoot = join(ROOT, outDir, runId());
  mkdirSync(artifactRoot, { recursive: true });

  const server = createStaticServer(SERVE_ROOT);
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));

  let playwright;
  try {
    playwright = await import("playwright");
  } catch {
    console.error("Playwright not installed. Run: npx playwright install chromium");
    process.exit(1);
  }

  const browser = await playwright.chromium.launch({ headless: true, args: ["--no-sandbox"] });
  const page = await browser.newPage({ viewport: { width: 1400, height: 1200 }, deviceScaleFactor: 1 });

  try {
    await page.goto(`http://127.0.0.1:${port}/scene-studio-card/review/index.html`, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.__tbsc_review_ready === true, { timeout: 15000 });

    const scenarios = await page.evaluate(() => window.__tbsc_review_scenarios ?? []);
    const manifest = [];

    for (const scenario of scenarios) {
      const dir = join(artifactRoot, scenario.id);
      mkdirSync(dir, { recursive: true });
      const locator = page.locator(`#${scenario.elementId}`);
      await locator.waitFor({ state: "visible" });
      await locator.screenshot({ path: join(dir, "screenshot.png") });

      const summary = {
        id: scenario.id,
        widthPx: scenario.meta?.widthPx ?? 0,
        screenshotPath: `${scenario.id}/screenshot.png`,
      };

      writeFileSync(join(dir, "summary.json"), JSON.stringify(summary, null, 2), "utf8");
      manifest.push(summary);
    }

    writeFileSync(join(artifactRoot, "manifest.json"), JSON.stringify({ runAt: new Date().toISOString(), scenarios: manifest }, null, 2), "utf8");
    writeFileSync(
      join(artifactRoot, "index.html"),
      `<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Scene Studio Card Review</title><style>body{font:14px/1.5 "Segoe UI",sans-serif;background:#111827;color:#e5e7eb;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{background:#1f2937;border:1px solid #374151;border-radius:12px;padding:12px}img{width:100%;height:auto;border-radius:10px;display:block}a{color:#93c5fd}</style></head><body><h1>Scene Studio Card Review</h1><div class="grid">${manifest.map((scenario) => `<div class="card"><h2>${scenario.id}</h2><p>${scenario.widthPx}px</p><a href="${scenario.screenshotPath}"><img src="${scenario.screenshotPath}" alt="${scenario.id}"></a></div>`).join("")}</div></body></html>`,
      "utf8",
    );
    console.log(`Review artifacts written to: ${artifactRoot}`);
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
