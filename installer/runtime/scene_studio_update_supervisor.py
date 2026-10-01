"""Independent protocol-1 AppDaemon update companion. Never imports Scene Studio.

See docs/installation/IN_APP_UPDATES.md. This installed file and its trust module
are deliberately excluded from the replacement set. Persist intent BEFORE
activation/restart so initialize() can recover even when the backend cannot load.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.request import Request, urlopen
import uuid

from scene_studio_release import TrustError, digest, retrieve_verified, stable_key

TERMINAL = {"idle", "succeeded", "failed"}
MESSAGES = {
    "idle": "No update is running.", "downloading": "Downloading release…",
    "verifying": "Verifying release…", "staged": "Release staged.",
    "activating": "Installing Scene Studio…", "restarting": "Restarting AppDaemon…",
    "verifying_new_build": "Waiting for Scene Studio; verifying update…",
    "succeeded": "Update installed and verified.", "rollback": "Restoring previous build…",
    "failed": "Update failed. Active files were not changed.",
}
IDENTITY = ("version", "source_sha", "source_tree_sha256", "channel", "dirty", "tag")
# Keep this witness across AppDaemon module/app reloads in the SAME interpreter.
# Container PID values can be reused, so PID alone cannot prove a restart.
if not hasattr(os, "_scene_studio_update_process"):
    os._scene_studio_update_process = uuid.uuid4().hex


class Cancelled(Exception):
    pass


def fsync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def tree_hashes(root):
    result = {}
    for path in Path(root).rglob("*"):
        if path.is_symlink():
            raise TrustError("Product symlinks are not supported.")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(root).as_posix()] = digest(path)
    return result


def durable_copy(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for path in target.rglob("*"):
        if path.is_file():
            with path.open("r+b" if os.name == "nt" else "rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
        fsync_directory(path)
    fsync_directory(target)
    fsync_directory(target.parent)
    if tree_hashes(source) != tree_hashes(target):
        raise TrustError("Staged or backup product verification failed.")


class Runner:
    def __init__(self, config_root, probe, restart, ready=lambda: True, retrieve=retrieve_verified,
                 timeout=120, cancelled=None, preserved_paths=(), require_restart=False, process_token=None):
        self.root = Path(config_root).absolute()
        self.products = [self.root / "apps/scene_studio", self.root / "www/scene_studio"]
        self.state_root = self.root / "scene_studio_updates"
        self.probe, self.restart, self.ready, self.retrieve = probe, restart, ready, retrieve
        self.timeout = timeout
        self.require_restart = require_restart
        self.process_token = process_token or os._scene_studio_update_process
        self.preserved_paths = [Path(p).resolve() for p in preserved_paths]
        self.cancelled = cancelled or threading.Event()
        self.mutex = threading.Lock()
        self.lock_file = None
        self.worker = None
        # Reject symlink ancestors before even creating the journal/lock.
        for path in [*self.products, self.state_root]:
            if any(p.is_symlink() for p in [path, *path.parents]):
                raise TrustError("Update paths must not contain symlinks.")
        self.state_root.mkdir(mode=0o700, exist_ok=True)
        self.journal = self.state_root / "transaction.json"
        self.record = self.read_record()

    def read_record(self):
        if self.journal.exists():
            return json.loads(self.journal.read_text(encoding="utf-8"))
        return {"state": "idle"}

    def save(self, state=None, **values):
        if state:
            self.record["state"] = state
        self.record.update(values)
        temp = self.journal.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(self.record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.journal)
        fsync_directory(self.state_root)

    def status(self):
        # Explicit allowlist; journal internals and exceptions never leave runner.
        record = self.read_record()
        return {**{k: record[k] for k in ("state", "target_version", "installed_version", "rolled_back", "recovery_required") if k in record},
                "message": record.get("message", MESSAGES.get(record["state"], "Recovery required."))}

    def acquire(self):
        stream = (self.state_root / "execution.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.lock_file = stream
        return True

    def release(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def preflight(self):
        if not self.ready():
            raise TrustError("AppDaemon restart service is unavailable.")
        for path in self.products:
            for preserved in self.preserved_paths:
                if preserved == path or path in preserved.parents or preserved in path.parents:
                    raise TrustError("User data overlaps a product replacement path.")
            if not path.is_dir() or not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
                raise TrustError("Installed product paths are not writable.")
            tree_hashes(path)
            if path.stat().st_dev != self.state_root.stat().st_dev:
                raise TrustError("Products and recovery journal must share a filesystem.")
        current = self.probe()
        if not current.get("engine", {}).get("ok"):
            raise TrustError("Installed Scene Studio is not healthy.")
        stable_key(current["product"]["build"]["version"])
        return current

    def start(self, body, background=True):
        if not isinstance(body, dict) or set(body) != {"target_version"}:
            raise TrustError("Only an explicit target_version is accepted.")
        target = body["target_version"]
        stable_key(target)
        with self.mutex:
            if self.lock_file or not self.acquire():
                raise TrustError("An update is already running.")
            try:
                self.record = self.read_record()
                if self.record["state"] not in TERMINAL or self.record.get("recovery_required"):
                    raise TrustError("A pending update requires recovery.")
                current = self.preflight()
                old = current["product"]["build"]
                if stable_key(target) <= stable_key(old["version"]):
                    raise TrustError("Target must be newer than the installed version.")
                self.record = {"state": "downloading", "id": uuid.uuid4().hex,
                               "target_version": target, "installed_version": old["version"],
                               "old_build": {k: old.get(k) for k in IDENTITY},
                               "old_mode": current["runtime"]["mode"], "activation_intent": False}
                self.save()
            except Exception:
                self.release()
                raise
            if background:
                self.worker = threading.Thread(target=self.execute, daemon=True)
                self.worker.start()
            else:
                self.execute()
        return self.status()

    def transaction_root(self):
        ident = self.record["id"]
        if not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            raise TrustError("Invalid recovery transaction.")
        return self.state_root / ident

    def execute(self):
        try:
            work = self.transaction_root()
            work.mkdir(mode=0o700)
            root, build = self.retrieve(self.record["target_version"], work / "release",
                                        progress=lambda state: self.save(state))
            self.check_cancelled()
            self.save(new_build={k: build[k] for k in IDENTITY})
            sources = [root / "backend/src/scene_studio", root / "workbench/dist"]
            self.record["hashes"] = []
            self.record["old_hashes"] = []
            for i, (source, live) in enumerate(zip(sources, self.products)):
                durable_copy(source, work / f"stage-{i}")
                durable_copy(live, work / f"backup-{i}")
                self.record["hashes"].append(tree_hashes(source))
                self.record["old_hashes"].append(tree_hashes(live))
            self.save("staged")
            self.check_cancelled()
            # Catch a changed runtime/other deployment before mutation.
            current = self.preflight()
            if any(current["product"]["build"].get(k) != self.record["old_build"].get(k) for k in IDENTITY):
                raise TrustError("Installed build changed during staging.")
            for i, live in enumerate(self.products):
                if tree_hashes(live) != self.record["old_hashes"][i]:
                    raise TrustError("Installed product changed during staging.")
            self.save("activating", activation_intent=True)
            for i, live in enumerate(self.products):
                os.replace(live, work / f"previous-{i}")
                fsync_directory(live.parent)
                os.replace(work / f"stage-{i}", live)
                fsync_directory(live.parent)
                if tree_hashes(live) != self.record["hashes"][i]:
                    raise TrustError("Activated product hash mismatch.")
            self.request_restart("restarting")
            self.verify_new()
        except Cancelled:
            pass  # persisted intent is recovered by the next supervisor instance
        except Exception:
            self.fail_or_rollback()
        finally:
            self.release()

    def wait_healthy(self, expected, hashes):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.check_cancelled()
            try:
                current = self.probe()
                build = current.get("product", {}).get("build", {})
                if (current.get("engine", {}).get("ok")
                        and current.get("runtime", {}).get("mode") == self.record["old_mode"]
                        and all(build.get(k) == expected.get(k) for k in IDENTITY)
                        and all(tree_hashes(p) == h for p, h in zip(self.products, hashes))):
                    return
            except Exception:
                pass  # restart disconnect/failed import is expected until bounded timeout
            self.cancelled.wait(min(1, max(0, deadline - time.monotonic())))
        raise TrustError("Scene Studio health verification timed out.")

    def verify_new(self):
        self.save("verifying_new_build")
        self.wait_healthy(self.record["new_build"], self.record["hashes"])
        self.save("succeeded", activation_intent=False, recovery_required=False)

    def wait_for_restart(self):
        # Never commit a production update in the interpreter that activated it.
        # A rejected/lost restart request keeps the watchdog alive until timeout;
        # a real shutdown cancels it and a NEW process resumes the journal.
        deadline = self.record.get("restart_deadline", time.time() + self.timeout)
        while time.time() < deadline:
            self.check_cancelled()
            self.cancelled.wait(min(1, max(0, deadline - time.time())))
        raise TrustError("AppDaemon restart did not complete in time.")

    def request_restart(self, state, **values):
        self.save(state, restart_origin=self.process_token, restart_deadline=time.time() + self.timeout, **values)
        try:
            self.restart()
        except Exception:
            pass  # the restart can break its own service-call connection
        if self.require_restart:
            self.wait_for_restart()

    def rollback(self):
        work = self.transaction_root()
        self.save("rollback")
        for i, live in enumerate(self.products):
            backup = work / f"backup-{i}"
            if tree_hashes(backup) != self.record["old_hashes"][i]:
                raise TrustError("Recovery backup hash mismatch.")
            # Idempotent recovery even if interrupted between the two renames.
            if live.exists() and tree_hashes(live) == self.record["old_hashes"][i]:
                continue
            restore = work / f"restore-{i}"
            if restore.exists():
                shutil.rmtree(restore)
            durable_copy(backup, restore)
            if live.exists():
                failed = work / f"failed-{i}-{uuid.uuid4().hex}"
                os.replace(live, failed)
                fsync_directory(live.parent)
            os.replace(restore, live)
            fsync_directory(live.parent)
        self.request_restart("rollback", rollback_restart=True)
        self.verify_old()

    def verify_old(self):
        self.wait_healthy(self.record["old_build"], self.record["old_hashes"])
        self.save("failed", rolled_back=True, recovery_required=False, activation_intent=False,
                  message="Update failed; the previous healthy build was restored.")

    def fail_or_rollback(self):
        if not self.record.get("activation_intent"):
            self.save("failed", message="Release verification or staging failed. Active files were not changed.")
            return
        try:
            self.rollback()
        except Cancelled:
            pass
        except Exception:
            self.save("failed", recovery_required=True,
                      message="Update and automatic recovery failed. Retained backups require external recovery.")

    def recover(self):
        # A companion reload can initialize before its old worker observes
        # terminate(). Wait for that OS lock rather than abandoning recovery.
        deadline = time.monotonic() + self.timeout
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            with self.mutex:
                if not self.lock_file and self.acquire():
                    break
            self.cancelled.wait(1)
        else:
            return
        with self.mutex:
            try:
                self.record = self.read_record()
                state = self.record["state"]
                if state in TERMINAL:
                    return
                if state in ("restarting", "verifying_new_build"):
                    try:
                        if self.require_restart and self.record.get("restart_origin") == self.process_token:
                            self.wait_for_restart()
                        self.verify_new()
                    except Cancelled:
                        raise
                    except Exception:
                        self.rollback()
                elif state == "rollback":
                    if self.record.get("rollback_restart"):
                        if self.require_restart and self.record.get("restart_origin") == self.process_token:
                            self.wait_for_restart()
                        self.verify_old()
                    else:
                        self.rollback()
                elif self.record.get("activation_intent"):
                    self.rollback()
                else:
                    self.save("failed", message="Update interrupted before activation. Active files were not changed.")
            except Cancelled:
                pass
            except Exception:
                self.save("failed", recovery_required=True,
                          message="Automatic recovery failed. Retained backups require external recovery.")
            finally:
                self.release()


def api_status(api_url):
    request = Request(api_url + "/api/appdaemon/scene_studio_api",
                      data=json.dumps({"method": "GET", "path": "/status"}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        envelope = json.loads(response.read(2 * 1024 * 1024))
    if envelope.get("status") != 200:
        raise TrustError("Scene Studio API unavailable.")
    body = envelope["body"]
    # Verify the served static surface as well as on-disk hashes.
    with urlopen(api_url + "/local/scene_studio/build-info.json", timeout=5) as response:
        served = json.loads(response.read(16384))
    if any(served.get(k) != body.get("product", {}).get("build", {}).get(k) for k in IDENTITY):
        raise TrustError("Served Workbench build mismatch.")
    return body


try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:
    hass = None  # local focused tests need no AppDaemon installation


class SceneStudioUpdateSupervisor(hass.Hass if hass else object):
    def initialize(self):
        self.stop_event = threading.Event()
        slug = self.args.get("addon_slug", "")
        if not slug or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in slug):
            raise TrustError("A valid AppDaemon addon_slug is required.")
        api_url = self.args.get("api_url", "http://127.0.0.1:5050").rstrip("/")
        self.runner = Runner(self.args.get("config_root", "/config"),
                             probe=lambda: api_status(api_url),
                             restart=lambda: self.call_service("hassio/addon_restart", addon=slug, timeout=20),
                             ready=lambda: any(s.get("domain") == "hassio" and s.get("service") == "addon_restart"
                                               for s in self.list_services(self.namespace)),
                             preserved_paths=[self.args["store_root"]],
                             require_restart=True,
                             cancelled=self.stop_event)
        self.register_endpoint(self.endpoint, "scene_studio_update_api")
        # Worker recovery holds the OS lock, not an AppDaemon callback/thread.
        threading.Thread(target=self.runner.recover, daemon=True).start()

    def terminate(self):
        self.stop_event.set()

    def endpoint(self, args, **kwargs):
        try:
            if not isinstance(args, dict) or set(args) - {"method", "path", "body"}:
                raise TrustError("An update RPC envelope is required.")
            request = kwargs.get("request")
            if request is not None and request.content_type != "application/json":
                raise TrustError("Update requests require application/json.")
            path = args.get("path")
            method = args.get("method")
            if path == "/update/status" and method == "GET":
                payload, code = self.runner.status(), 200
            elif path == "/update" and method == "POST":
                payload, code = self.runner.start(args.get("body")), 202
            else:
                payload, code = {"message": "Unknown update route."}, 404
        except TrustError as exc:
            payload, code = {"message": str(exc)}, 409
        except Exception:
            payload, code = {"message": "Update executor unavailable."}, 503
        return {"status": code, "body": payload}, 200
