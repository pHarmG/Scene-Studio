/** Runtime administration over the independent supervisor; no scene commands. */
export const executionLabels = {
  downloading: "Downloading…", verifying: "Verifying…", staged: "Installing…",
  activating: "Installing…", restarting: "Restarting…", reconnecting: "Waiting for Scene Studio…",
  verifying_new_build: "Verifying update…", succeeded: "Update complete", rollback: "Rolling back…",
  failed: "Update failed",
};
export const updateRunning = (status) => status && !["idle", "succeeded", "failed"].includes(status.state);

export async function followUpdate(client, target, changed, { timeout = 300000, interval = 1000,
  now = () => Date.now(), sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) } = {}) {
  const deadline = now() + timeout;
  while (now() < deadline) {
    try {
      const status = await client.getUpdateStatus();
      if (status.target_version && status.target_version !== target) {
        return changed({ state: "failed", recovery_required: true, message: "A different server update is running. Reopen System to inspect it." });
      }
      if (status.state === "failed") return changed(status);
      if (status.state === "succeeded") {
        changed({ ...status, state: "verifying_new_build" });
        const live = await (client.getUpdateBuild ? client.getUpdateBuild() : client.getStatus());
        if (live.engine?.ok && live.product?.build?.version === target) return changed(status);
        if (live.engine?.ok && live.product?.build?.version === status.installed_version) {
          return changed({ state: "failed", rolled_back: true, message: "The previous build is running; the update was rolled back." });
        }
      } else {
        changed(status.state === "idle" ? { state: "reconnecting", target_version: target,
          message: "Waiting for the update executor…" } : status);
      }
    } catch {
      changed({ state: "reconnecting", target_version: target, message: "Waiting for Scene Studio after restart…" });
    }
    await sleep(interval);
  }
  return changed({ state: "failed", target_version: target, recovery_required: true,
    message: "Update status timed out. The server may still be recovering. Reopen System to check; do not start another update." });
}
