#!/usr/bin/env python3
"""Fake remote Home Assistant host for the portable-install acceptance tests.

Emulates — WITHOUT any real network beyond localhost — the exact remote
surface the Scene Studio deployers and the portable installer use:

* ``ssh <host> <command>``: a stateful fake remote filesystem plus a
  mini-interpreter for the specific shell vocabulary the deployers emit
  (test/echo/stat/install/mv/chown/rm/find/sed/sort/grep/cut/sha256sum/
  tar/python3/base64|tee/curl/uname/hostname, if/then/else/fi, ``&&``,
  ``||``, pipelines, ``> /dev/null`` redirects, and the Workbench
  ``served_hash=$(...)`` probe). Every invocation is appended to
  ``calls.log`` (json lines) so tests can prove what ran — and that
  read-only paths never wrote.
* ``--serve-api PORT``: the HA REST boundary (GET ``/api/config``, POST
  ``/api/services/hassio/addon_restart``). A restart makes the fake Scene
  Studio API "installed" only when the backend tree exists in the fake FS;
  the reported runtime mode is ``apps_mode`` from the state dir (pre-seed
  ``normal`` for upgrade scenarios, ``registry_admin`` for fresh installs).

State layout (env FAKE_HA_STATE):
    fs/          fake remote filesystem root (absolute paths map under it)
    apps_mode    runtime mode a present backend would report after restart
    calls.log    json-lines record of every ssh invocation

This is TEST SUPPORT ONLY: it lives under tests/ and never ships in the
distribution bundle.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

ADDON_ROOT = "/addon_configs/fake_addon"


class CommandError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeHost:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.fs = state_dir / "fs"
        self.fs.mkdir(parents=True, exist_ok=True)
        self.cwd = self.fs

    # -- paths -------------------------------------------------------------

    def resolve(self, remote_path: str) -> Path:
        cleaned = remote_path.strip()
        base = self.fs
        if not cleaned.startswith("/"):
            base = self.cwd
        cleaned = cleaned.lstrip("/").replace("\\", "/")
        parts = [part for part in cleaned.split("/") if part not in ("", ".")]
        for part in parts:
            if part == "..":
                raise CommandError(2, f"fake-ssh: unsafe path: {remote_path}")
        return base.joinpath(*parts) if parts else base

    def log_call(self, argv: list[str], stdin: bytes, exit_code: int, stdout: str) -> None:
        record = {"argv": argv, "stdin_bytes": len(stdin), "exit": exit_code, "stdout": stdout[:400]}
        with open(self.state_dir / "calls.log", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def apps_mode(self) -> str:
        path = self.state_dir / "apps_mode"
        return path.read_text(encoding="utf-8").strip() if path.exists() else "registry_admin"

    # -- statement splitting ------------------------------------------------

    def split_statements(self, script: str) -> list[str]:
        """Split on top-level ``;`` while if/then/else/fi blocks stay atomic."""
        statements: list[str] = []
        current: list[str] = []
        quote = None
        depth_if = 0
        for char in script:
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
                current.append(char)
                continue
            if char == ";" and depth_if == 0:
                statements.append("".join(current).strip())
                current = []
                continue
            current.append(char)
        statements.append("".join(current).strip())

        # Reassemble if/then/else/fi blocks: the scanner split at every
        # top-level `;`, so an if-block arrives as fragments. Depth delta is
        # counted per FRAGMENT (the merged string would double-count).
        merged: list[str] = []
        pending = ""
        depth = 0
        for fragment in statements:
            if not fragment:
                continue
            words = _split_words(fragment)
            depth += sum(1 for word in words if word == "if") - sum(1 for word in words if word == "fi")
            if depth > 0:
                pending = f"{pending}; {fragment}" if pending else fragment
                continue
            depth = 0
            statement = f"{pending}; {fragment}" if pending else fragment
            pending = ""
            if statement in ("set -eu", "set -e"):
                continue
            merged.append(statement)
        if pending:
            merged.append(pending)
        return [item for item in merged if item]

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, script: str, stdin: bytes = b"") -> tuple[int, str]:
        self.cwd = self.fs
        outputs: list[str] = []
        for statement in self.split_statements(script):
            code, out = self.run_statement(statement, stdin)
            if code != 0:
                return code, "\n".join(outputs + ([out] if out else []))
            if out:
                outputs.append(out)
        return 0, "\n".join(outputs)

    def run_statement(self, statement: str, stdin: bytes) -> tuple[int, str]:
        statement = statement.strip()
        if not statement:
            return 0, ""
        if statement.startswith("if "):
            return self.run_if(statement)
        # && and || share bash's equal precedence, left-associative:
        # `a && b || c` runs c when a fails. Evaluate the chain in order.
        if re.search(r" (&&|\|\|) ", statement):
            segments = re.split(r" (&&|\|\|) ", statement)
            code, out = self.run_statement(segments[0], stdin)
            index = 1
            while index < len(segments) - 1:
                op = segments[index]
                if (op == "&&" and code == 0) or (op == "||" and code != 0):
                    code_r, out_r = self.run_statement(segments[index + 1], stdin)
                    out = out + ("\n" if out and out_r else "") + out_r
                    code = code_r
                # otherwise: short-circuit; code/out carry forward
                index += 2
            return code, out
        if "|" in statement and "$(" not in statement:
            stages = [stage.strip() for stage in statement.split("|")]
            code, data = self.run_simple(stages[0], stdin)
            for stage in stages[1:]:
                if code != 0:
                    return code, ""
                payload = data if isinstance(data, bytes) else data.encode("utf-8")
                code, data = self.run_simple(stage, payload)
            if code == 0:
                return code, data.decode("utf-8", "replace") if isinstance(data, bytes) else data
            return code, ""
        return self.run_simple(statement, stdin)

    def run_if(self, statement: str) -> tuple[int, str]:
        words = _split_words(statement)
        try:
            then_index = words.index("then")
            fi_index = _rindex(words, "fi")
        except ValueError:
            raise CommandError(2, f"fake-ssh: cannot parse if-statement: {statement[:200]}")
        cond = " ".join(words[1:then_index]).strip().rstrip(";").strip()
        tail = words[then_index + 1 : fi_index]
        else_index = _index_of(tail, "else")
        then_part = " ".join(tail if else_index == -1 else tail[:else_index]).strip().rstrip(";").strip()
        else_part = "" if else_index == -1 else " ".join(tail[else_index + 1 :]).strip().rstrip(";").strip()
        code, _ = self.run_statement(cond, b"")
        branch = then_part if code == 0 else else_part
        if not branch:
            return 0, ""
        return self.evaluate(branch)

    def run_simple(self, command: str, stdin: bytes) -> tuple[int, str]:
        command = _strip_redirects(command.strip())
        if not command:
            return 0, ""
        try:
            return self.dispatch(command, stdin)
        except CommandError as exc:
            return exc.code, exc.message

    def dispatch(self, command: str, stdin: bytes) -> tuple[int, str]:
        if command.startswith("sudo "):
            command = command[len("sudo ") :]
        if command.startswith("cd "):
            parts = shlex.split(command)
            self.cwd = self.resolve(parts[1])
            return 0, ""
        if command.startswith("echo "):
            body = command[len("echo ") :].strip()
            body = body.replace("$(hostname)", "fake-ha")
            if len(body) >= 2 and body[0] == body[-1] and body[0] in ("'", '"'):
                body = body[1:-1]
            return 0, body
        if command == "uname -srmo":
            return 0, "Linux 6.1.0-fake aarch64 GNU/Linux"
        if command == "python3 --version":
            return 0, "Python 3.12.13"
        if command.startswith("test "):
            return self.cmd_test(command)
        if command.startswith("stat "):
            parts = shlex.split(command)
            if not self.resolve(parts[-1]).exists():
                raise CommandError(1, "fake-ssh: stat target missing")
            return 0, "1000:1000"
        if command.startswith("install -d"):
            parts = shlex.split(command)
            self.resolve(parts[-1]).mkdir(parents=True, exist_ok=True)
            return 0, ""
        if command.startswith("mv "):
            parts = shlex.split(command)[1:]
            if len(parts) != 2:
                raise CommandError(2, "fake-ssh: mv needs two args")
            src, dst = self.resolve(parts[0]), self.resolve(parts[1])
            if not src.exists():
                raise CommandError(1, "fake-ssh: mv source missing")
            if dst.exists():
                raise CommandError(1, "fake-ssh: mv destination exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return 0, ""
        if command.startswith("chown -R "):
            return 0, ""  # ownership is implicit in the fake fs
        if command.startswith("rm -f "):
            parts = shlex.split(command)[1:]
            target = self.resolve(parts[0])
            if target.is_file():
                target.unlink()
            return 0, ""
        if command.startswith("rm -rf "):
            parts = shlex.split(command)[1:]
            target = self.resolve(parts[0])
            if target == self.fs:
                raise CommandError(1, "fake-ssh: refusing to remove fs root")
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            return 0, ""
        if command.startswith("find "):
            return self.cmd_find(command)
        if command.startswith("sed "):
            lines = stdin.decode("utf-8", "replace").splitlines()
            return 0, "\n".join(line[2:] if line.startswith("./") else line for line in lines if line)
        if command == "sort":
            lines = sorted(stdin.decode("utf-8", "replace").splitlines())
            return 0, "\n".join(line for line in lines if line)
        if command.startswith("grep "):
            parts = shlex.split(command)
            needle = parts[-1]
            if len(needle) >= 2 and needle[0] in ("'", '"'):
                needle = needle[1:-1]
            data = stdin.decode("utf-8", "replace")
            matched = [line for line in data.splitlines() if needle in line]
            if matched:
                return 0, "\n".join(matched)
            raise CommandError(1, "")
        if command.startswith("cut "):
            data = stdin.decode("utf-8", "replace").strip()
            return 0, data.split(" ")[0] if data else ""
        if command.startswith("sha256sum "):
            parts = shlex.split(command)[1:]
            target = self.resolve(parts[0])
            if not target.is_file():
                raise CommandError(1, "fake-ssh: sha256sum target missing")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            return 0, f"{digest}  {parts[0]}"
        if command.startswith("tar czf "):
            parts = shlex.split(command)  # tar czf <archive> <member>
            archive = self.resolve(parts[2])
            member = parts[3]
            base = self.resolve(member)
            listing: list[str] = []
            if base.is_dir():
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        listing.append(f"{member.rstrip('/')}/{path.relative_to(base).as_posix()}")
            elif base.is_file():
                listing.append(member)
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text("\n".join(listing), encoding="utf-8")
            return 0, ""
        if command.startswith("tar tzf "):
            parts = shlex.split(command)  # tar tzf <archive>
            archive = self.resolve(parts[2])
            if not archive.is_file():
                raise CommandError(2, "fake-ssh: tar archive missing")
            return 0, archive.read_text(encoding="utf-8")
        if command.startswith("python3 -c "):
            parts = shlex.split(command[len("python3 -c ") :])
            directory = self.resolve(parts[1])
            if not directory.is_dir():
                raise CommandError(1, "fake-ssh: ast-parse target missing")
            try:
                for path in sorted(directory.rglob("*.py")):
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                raise CommandError(1, f"SyntaxError: {exc}")
            return 0, ""
        if command == "base64 -d":
            # pipeline stage: decode stdin, pass bytes downstream
            try:
                return 0, base64.b64decode(stdin)
            except Exception as exc:
                raise CommandError(1, f"fake-ssh: base64 decode failed: {exc}")
        if command == "base64 -w0":
            # pipeline stage: encode stdin as one unwrapped base64 line
            return 0, base64.b64encode(stdin).decode("ascii")
        if command.startswith("tee "):
            parts = shlex.split(command)[1:]
            target = self.resolve(parts[0])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(stdin)
            return 0, ""
        if command.startswith("ls "):
            parts = shlex.split(command)
            directory = self.resolve(parts[-1])
            if not directory.is_dir():
                raise CommandError(2, f"fake-ssh: ls target missing: {parts[-1]}")
            names = sorted(item.name for item in directory.iterdir())
            return 0, "\n".join(names)
        if command.startswith("cat "):
            parts = shlex.split(command)[1:]
            target = self.resolve(parts[0])
            if not target.is_file():
                raise CommandError(1, f"fake-ssh: cat target missing: {parts[0]}")
            return 0, target.read_text(encoding="utf-8", errors="replace")
        if command.startswith("cp -a "):
            parts = shlex.split(command)[1:]  # ["-a", <src>, <dst>]
            if len(parts) != 3:
                raise CommandError(2, "fake-ssh: cp -a needs src and dst")
            src, dst = self.resolve(parts[1]), self.resolve(parts[2])
            if not src.exists():
                raise CommandError(1, "fake-ssh: cp source missing")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            return 0, ""
        if command.startswith("chmod "):
            return 0, ""  # permissions are implicit in the fake fs
        if command.startswith("curl "):
            return self.cmd_curl(command)
        raise CommandError(2, f"fake-ssh: unsupported command: {command[:200]}")

    def cmd_test(self, command: str) -> tuple[int, str]:
        words = shlex.split(command)[1:]
        negate = False
        if words and words[0] == "!":
            negate = True
            words = words[1:]
        if len(words) != 2 or words[0] not in ("-d", "-f", "-e"):
            raise CommandError(2, f"fake-ssh: unsupported test: {command}")
        target = self.resolve(words[1])
        if words[0] == "-d":
            result = target.is_dir()
        elif words[0] == "-f":
            result = target.is_file()
        else:
            result = target.exists()
        if negate:
            result = not result
        return (0, "") if result else (1, "")

    def cmd_find(self, command: str) -> tuple[int, str]:
        words = shlex.split(command)
        name_filter = None
        index = 0
        while index < len(words):
            if words[index] == "-name":
                name_filter = words[index + 1]
            index += 1
        pattern = "*" if name_filter in (None, "*") else name_filter
        if "-o" in words and "build-info.json" in words:
            pattern = "*"
        results = []
        for path in sorted(self.cwd.rglob(pattern)):
            if not path.is_file():
                continue
            if "-o" in words and "build-info.json" in words and path.suffix != ".py" and path.name != "build-info.json":
                continue
            results.append("./" + path.relative_to(self.cwd).as_posix())
        return 0, "\n".join(results)

    def cmd_curl(self, command: str) -> tuple[int, str]:
        # The only curl target in this vocabulary is the Scene Studio API.
        runtime = self.api_state()
        if runtime is None:
            # curl exit 7: connection failed — the "absent" signal.
            raise CommandError(7, "")
        return 0, json.dumps({"status": 200, "body": {"runtime": runtime}})

    def api_state(self) -> dict | None:
        # Discover whichever addon config root carries a Scene Studio backend
        # (the wizard may select any candidate root, not the default one).
        backend = None
        addon_configs = self.resolve("/addon_configs")
        if addon_configs.is_dir():
            for candidate in sorted(addon_configs.iterdir()):
                marker = candidate / "apps" / "scene_studio" / "appdaemon_adapter" / "adapter.py"
                if marker.is_file():
                    backend = candidate / "apps" / "scene_studio"
                    break
        if backend is None:
            return None
        mode = self.apps_mode()
        return {
            "mode": mode,
            "read_only": mode == "read_only",
            "provider_writes_blocked": mode in ("read_only", "registry_admin"),
            "allowed_commands": [],
        }

    def served_index_probe(self, expected_hash: str) -> tuple[int, str]:
        # Discover whichever addon config root serves the Workbench (the
        # wizard may select any candidate root, not the default one).
        index = None
        addon_configs = self.resolve("/addon_configs")
        if addon_configs.is_dir():
            for candidate in sorted(addon_configs.iterdir()):
                candidate_index = candidate / "www" / "scene_studio" / "index.html"
                if candidate_index.is_file():
                    index = candidate_index
                    break
        if index is None:
            raise CommandError(1, "fake-ssh: served index missing")
        actual = hashlib.sha256(index.read_bytes()).hexdigest()
        if actual != expected_hash:
            raise CommandError(1, f"hash mismatch: served {actual} expected {expected_hash}")
        if "Scene Studio Workbench" not in index.read_text(encoding="utf-8", errors="replace"):
            raise CommandError(1, "served page missing Workbench marker")
        return 0, ""


def _strip_redirects(command: str) -> str:
    return re.sub(r">>\s*/dev/null\s*2>&1$|>\s*/dev/null\s*2>&1$|2>&1$|>\s*/dev/null$|2>/dev/null$", "", command).strip()


def _split_words(text: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []
    quote = None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char.isspace() or char == ";":
            if current:
                words.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        words.append("".join(current))
    return words


def _index_of(words: list[str], keyword: str) -> int:
    for index, word in enumerate(words):
        if word == keyword:
            return index
    return -1


def _rindex(words: list[str], keyword: str) -> int:
    for index in range(len(words) - 1, -1, -1):
        if words[index] == keyword:
            return index
    raise ValueError(keyword)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def parse_ssh_argv(argv: list[str]) -> tuple[str | None, str]:
    """Parse an ssh command line: options, then destination, then command.

    Supports the forms the deployers and the wizard emit: ``ssh <host> <cmd>``
    and ``ssh -p <port> -o <opt>... <user@host> <cmd>``.
    """
    host = None
    command_parts: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-p", "-P"):
            index += 2  # skip the port value
            continue
        if arg == "-o":
            index += 2  # skip the option value
            continue
        if arg.startswith("-"):
            index += 1
            continue
        if host is None:
            host = arg
            index += 1
            continue
        command_parts = argv[index:]
        break
    return host, " ".join(command_parts)


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--serve-api":
        serve_api(int(sys.argv[2]), Path(sys.argv[3]))
        return 0

    state_dir = Path(os.environ["FAKE_HA_STATE"])
    argv = sys.argv[1:]
    host_alias, command = parse_ssh_argv(argv)
    stdin = b"" if sys.stdin.isatty() else sys.stdin.buffer.read()

    # Unreachable-target simulation: the wizard must fail cleanly before any
    # mutation when ssh cannot connect.
    if (state_dir / "ssh_unreachable").exists():
        return 255

    host = FakeHost(state_dir)

    # Deliberate-staging-failure simulation (config rollback test): staging
    # writes for the backend deployer fail while config writes to
    # /addon_configs still succeed.
    failing_staging = (state_dir / "fail_backend_staging").exists()

    def staging_targeted(statement: str) -> bool:
        return "scene-studio-backend-" in statement and (
            "install -d" in statement or "base64" in statement or "tee " in statement
        )

    if failing_staging and staging_targeted(command):
        host.log_call(argv, stdin, 1, "fake-ssh: simulated staging failure")
        print("fake-ssh: simulated staging failure")
        return 1

    # The Workbench served-hash probe is the one $(...) shape in the
    # vocabulary; handle it before the generic interpreter. The embedded
    # quotes may be stripped by argument marshalling, so accept both shapes.
    if "served_hash=$(curl" in command:
        match = re.search(r'test +\$?served_hash += +"?([0-9a-f]{64})"?', command)
        expected = match.group(1) if match else ""
        try:
            code, out = host.served_index_probe(expected)
        except CommandError as exc:
            code, out = exc.code, exc.message
        host.log_call(argv, stdin, code, out)
        if out:
            print(out)
        return code

    try:
        code, out = host.evaluate(command, stdin)
    except CommandError as exc:
        code, out = exc.code, exc.message
    host.log_call(argv, stdin, code, out)
    if out:
        print(out)
    return code


def serve_api(port: int, state_dir: Path) -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    host = FakeHost(state_dir)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/api/config":
                self._send(200, {"version": "2026.9.10"})
                return
            self._send(404, {"message": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path == "/api/services/hassio/addon_restart":
                # A restart makes the fake API "installed" exactly when the
                # backend tree exists; apps_mode decides the reported mode.
                self._send(200, {"result": "ok", "data": {}})
                return
            if self.path == "/api/appdaemon/scene_studio_api":
                # The AppDaemon named-endpoint envelope the wizard's final
                # health check probes (read-only /status).
                self._send(200, {
                    "status": 200,
                    "body": {"runtime": {"mode": host.apps_mode(), "allowed_commands": []}},
                })
                return
            self._send(404, {"message": "not found"})

        def log_message(self, *args):  # silence
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
