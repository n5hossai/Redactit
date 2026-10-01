"""Test support for the native host: starts it as Chrome would and speaks its frames.

The host runs in its own process, launched the way docs/PLAN.md §7 has the installer do
it: the base interpreter in isolated mode, with the venv's site-packages added by hand.
Its vault key lives in an in-memory keyring, never the real OS keychain.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import queue
import struct
import subprocess
import sys
import sysconfig
import threading
from collections import defaultdict
from pathlib import Path

import keyring.backend

REPO = Path(__file__).resolve().parent.parent
EXTENSION = "chrome-extension://" + "abcdefghijklmnop" * 2 + "/"  # a well-formed ID; no real extension
# What an installed launcher runs: no PYTHONPATH, no user site, no base site-packages.
LAUNCH = ("import site, sys; site.addsitedir(sys.argv.pop(1)); sys.path.insert(0, sys.argv.pop(1)); "
          "from redactit.hosts.native import main; main()")


class MemoryKeyring(keyring.backend.KeyringBackend):
    """Selected in the host process through PYTHON_KEYRING_BACKEND."""

    priority = 1
    _store: dict = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


def write_manifest(path: Path, origins: list[str]) -> Path:
    path.write_text(json.dumps({"name": "com.redactit.host", "description": "test", "path": "unused",
                                "type": "stdio", "allowed_origins": origins}), encoding="utf-8")
    return path


def frame(body: bytes) -> bytes:
    return struct.pack("=I", len(body)) + body


class Host:
    """One host process. Every message it sends is kept, in order, and routed by id."""

    def __init__(self, tmp: Path, *, origin: str = EXTENSION, idle_seconds: float = 600,
                 manifest: Path | None = None):
        from redactit.hosts import native

        self.native = native
        manifest = manifest or write_manifest(tmp / "host-manifest.json", [EXTENSION])
        env = {**os.environ, "REDACTIT_DATA_DIR": str(tmp / "data"),
               "PYTHON_KEYRING_BACKEND": "hostkit.MemoryKeyring"}
        self.stderr_path = tmp / "host-stderr.txt"
        self.audit_path = tmp / "data" / "audit.jsonl"
        cmd = [sys._base_executable, "-I", "-S", "-c", LAUNCH, sysconfig.get_path("purelib"), str(Path(__file__).parent),
               "--manifest", str(manifest), origin, "--idle-seconds", str(idle_seconds), "--parent-window=0"]
        with self.stderr_path.open("wb") as err:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, cwd=REPO, env=env)
        self.messages: list[dict] = []  # everything received, in arrival order
        self._routes: dict = defaultdict(queue.Queue)
        self._routes_lock = threading.Lock()
        self._ended = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        out = self.proc.stdout
        while (head := out.read(4)) and len(head) == 4:
            msg = json.loads(out.read(struct.unpack("=I", head)[0]))
            msg["_n"] = len(self.messages)  # arrival position, for ordering checks
            self.messages.append(msg)
            self._route(msg.get("id")).put(msg)
        self._ended.set()

    # --- sending ---

    def send_raw(self, data: bytes) -> None:
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, msg: dict) -> None:
        self.send_raw(frame(json.dumps(msg).encode("utf-8")))

    def request(self, rid: str, kind: str, payload: bytes, *, scope: str = "test-scope", site: str = "claude.ai",
                chunks: int | None = None) -> None:
        """Header plus chunks; `chunks` stops early to leave an upload unfinished."""
        native = self.native
        total = native.chunk_count(len(payload))
        header = {"type": "redact_text" if kind == "text" else "redact_file", "id": rid, "scope": scope,
                  "site": site, "size": len(payload), "total": total}
        if kind != "text":
            header["kind"] = kind
        self.send(header)
        for seq in range(total if chunks is None else chunks):
            piece = payload[seq * native.RAW_CHUNK:(seq + 1) * native.RAW_CHUNK]
            self.send({"type": "chunk", "id": rid, "seq": seq, "total": total,
                       "data": base64.b64encode(piece).decode("ascii")})

    # --- receiving ---

    def _route(self, rid: str | None) -> queue.Queue:
        with self._routes_lock:
            return self._routes[rid]

    def next(self, rid: str | None, timeout: float = 120) -> dict:
        return self._route(rid).get(timeout=timeout)

    def error(self, rid: str | None, timeout: float = 60) -> dict:
        """The next error for `rid`, passing over status and progress frames."""
        while True:
            msg = self.next(rid, timeout)
            if msg["type"] == "error":
                return msg

    def ping(self, rid: str, timeout: float = 60) -> dict:
        self.send({"type": "ping", "id": rid})
        return self.next(rid, timeout)

    def wait_status(self, state: str, timeout: float = 180) -> dict:
        while True:
            msg = self.next(None, timeout)
            if msg["type"] == "status" and msg["state"] == state:
                return msg

    def outcome(self, rid: str, timeout: float = 300) -> dict:
        """{"error": msg} or {"result": header, "parts": {name: bytes}}, plus "progress" seen."""
        progress = []
        while True:
            msg = self.next(rid, timeout)
            if msg["type"] == "progress":
                progress.append(msg)
            elif msg["type"] == "error":
                return {"error": msg, "progress": progress}
            elif msg["type"] == "result":
                chunks = [self.next(rid, timeout) for _ in range(msg["total"])]
                assert [c["seq"] for c in chunks] == list(range(msg["total"]))
                assert all(c["type"] == "chunk" and c["total"] == msg["total"] for c in chunks)
                blob = b"".join(base64.b64decode(c["data"]) for c in chunks)
                assert len(blob) == msg["size"]
                parts, at = {}, itertools.accumulate([0] + [p["size"] for p in msg["parts"]])
                bounds = list(at)
                for p, start, end in zip(msg["parts"], bounds, bounds[1:]):
                    parts[p["name"]] = blob[start:end]
                return {"result": msg, "parts": parts, "progress": progress, "chunks": chunks}

    def wait_exit(self, timeout: float) -> int:
        return self.proc.wait(timeout=timeout)

    def close(self) -> int:
        """Close stdin as Chrome does on disconnect; the host must exit on its own."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=30)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")
