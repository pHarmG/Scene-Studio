import { defineConfig } from "vite";

// Relative assets: the SPA is served from a sub-path (/local/scene_studio/)
// by the AppDaemon static route, so absolute /assets/... URLs would 404.
//
// Dev-only live-data proxy: the "direct"/"appdaemon" HTTP clients
// (src/api.js) fetch an ABSOLUTE base URL. In production the Workbench is
// served BY the same AppDaemon addon, so it's same-origin and no proxy is
// involved. During local development, pointing the Workbench's System
// drawer -> Connection -> "Live URL" at a real device's host directly hits
// the browser's CORS policy — the device's API has no
// Access-Control-Allow-Origin header and never needs one. Proxying
// /api/scene_studio and /api/appdaemon through the dev server itself
// sidesteps that: set the System drawer's Live URL to THIS dev server's own
// origin (e.g. http://localhost:5173) instead of the device's — the browser
// then talks only to same-origin `npm run dev`, which forwards server-side
// (Node, not the browser, so CORS never applies) to the real device.
//
// There is deliberately NO hardcoded default device. Set SCENE_STUDIO_LIVE_HOST
// to the AppDaemon origin to proxy live data, e.g.:
//   SCENE_STUDIO_LIVE_HOST=http://appdaemon.local:5050 npm run dev
// Without it the dev server starts in mock mode (the proxies are omitted, so
// any live-transport attempt fails fast instead of silently reaching some
// other household's device).
const liveHost = process.env.SCENE_STUDIO_LIVE_HOST;
if (liveHost) {
  console.log(`[scene-studio-workbench] live-data proxy target: ${liveHost}`);
} else {
  console.log(
    "[scene-studio-workbench] SCENE_STUDIO_LIVE_HOST not set: dev server runs in mock mode " +
      "(set it to an AppDaemon origin to proxy live data).",
  );
}

export default defineConfig({
  base: "./",
  server: {
    port: process.env.PORT ? Number(process.env.PORT) : 5173,
    ...(liveHost
      ? {
          proxy: {
            "/api/scene_studio": { target: liveHost, changeOrigin: true },
            "/api/appdaemon": { target: liveHost, changeOrigin: true },
          },
        }
      : {}),
  },
});
