import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { exec } from "node:child_process";
import { build } from "esbuild";

const __filename = fileURLToPath(import.meta.url);
const cardRoot = path.dirname(__filename);
// URL space root: /scene-studio-card/<path> maps onto home-assistant/scene-studio-card/<path>.
const serveRoot = path.join(cardRoot, "..");

const args = new Set(process.argv.slice(2));
const noOpen = args.has("--no-open");
const shutdownArg = [...args].find((arg) => arg.startsWith("--shutdown-ms="));
const shutdownMs = shutdownArg ? Number.parseInt(shutdownArg.split("=")[1] ?? "", 10) : null;

const host = "127.0.0.1";
const port = 4175;
const previewPath = "/scene-studio-card/preview/index.html";

const contentTypes = new Map([
  [".html", "text/html; charset=utf-8"],
  [".js", "text/javascript; charset=utf-8"],
  [".mjs", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"],
  [".json", "application/json; charset=utf-8"],
  [".svg", "image/svg+xml"],
  [".png", "image/png"],
]);

function openUrl(url) {
  const command =
    process.platform === "win32"
      ? `start "" "${url}"`
      : process.platform === "darwin"
        ? `open "${url}"`
        : `xdg-open "${url}"`;

  exec(command, (error) => {
    if (error) {
      console.warn(`Preview server is running, but the browser could not be opened automatically: ${error.message}`);
    }
  });
}

function resolveRequestPath(requestUrl) {
  const pathname = new URL(requestUrl, `http://${host}:${port}`).pathname;
  const normalizedPath = pathname === "/" ? previewPath : pathname;
  const rel = normalizedPath.replace(/^\//, "");
  const filePath = path.resolve(serveRoot, rel);
  const resolvedRoot = path.resolve(serveRoot);
  if (!filePath.startsWith(resolvedRoot)) {
    return null;
  }
  return filePath;
}

async function buildCard() {
  await build({
    entryPoints: ["src/scene-studio-card.ts"],
    outfile: "dist/scene-studio-card.js",
    bundle: true,
    format: "esm",
    target: "es2020",
    sourcemap: false,
    minify: false,
    absWorkingDir: cardRoot,
  });
}

await buildCard();

const server = createServer(async (request, response) => {
  const filePath = resolveRequestPath(request.url ?? previewPath);
  if (!filePath) {
    response.writeHead(403).end("Forbidden");
    return;
  }

  try {
    const file = await readFile(filePath);
    const contentType = contentTypes.get(path.extname(filePath).toLowerCase()) ?? "application/octet-stream";
    response.writeHead(200, { "Content-Type": contentType, "Cache-Control": "no-store" });
    response.end(file);
  } catch {
    response.writeHead(404).end("Not found");
  }
});

await new Promise((resolve) => server.listen(port, host, resolve));

const previewUrl = `http://${host}:${port}${previewPath}`;
console.log(`Preview ready: ${previewUrl}`);

if (!noOpen) {
  openUrl(previewUrl);
}

if (shutdownMs !== null && shutdownMs >= 0) {
  setTimeout(() => {
    server.close(() => process.exit(0));
  }, shutdownMs);
}
