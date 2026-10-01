import { build } from "esbuild";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.dirname(__filename);

await build({
  entryPoints: ["src/scene-studio-card.ts"],
  outfile: "dist/scene-studio-card.js",
  bundle: true,
  format: "esm",
  target: "es2020",
  sourcemap: false,
  minify: false,
  absWorkingDir: repoRoot,
});
