/** Backend-owned identity for live connections; Vite identity for mock builds. */
export const localBuild = typeof __SCENE_STUDIO_BUILD__ === "undefined"
  ? { version: null, channel: "local", short_sha: null, dirty: true }
  : __SCENE_STUDIO_BUILD__;

export function buildLabel(build) {
  if (!build?.version) return "Build unknown";
  return `v${build.version} • ${build.channel || "unknown"} ${build.short_sha || "unknown"}${build.dirty ? " (modified)" : ""}`;
}

export const updateLabels = {
  unchecked: "Not checked", checking: "Checking…", current: "Current",
  available: "Update available", unavailable: "Check unavailable", error: "Error",
};
