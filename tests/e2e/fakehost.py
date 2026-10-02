"""A stand-in native host that misbehaves on purpose, for the fail-closed browser tests.

Started by Chrome through a test launcher as `fakehost.py <mode> <pid file> <log file>`.
It speaks the real host's framing (src/redactit/hosts/native.py) and never redacts:

  warming       says `warming` and never gets further
  hang          gets ready, takes requests, then never answers
  engine-error  fails to load its engine, as a host without its models would
  garbage       answers a request with a frame the protocol does not allow

It writes its process id, so a test can kill it, and logs the type and id of every
frame it receives (never their data), so a test can see a cancel arrive. Standard
library only: it runs under `-I -S`, like the real host's launcher.
"""

import json
import os
import struct
import sys


def send(msg: dict) -> None:
    body = json.dumps(msg).encode("ascii")
    sys.stdout.buffer.write(struct.pack("=I", len(body)) + body)
    sys.stdout.buffer.flush()


def status(state: str) -> None:
    send({"type": "status", "id": None, "state": state, "version": "0.1.0", "protocol": 1})


def frames():
    while (head := sys.stdin.buffer.read(4)) and len(head) == 4:
        yield json.loads(sys.stdin.buffer.read(struct.unpack("=I", head)[0]))


def main() -> None:
    mode, pid_file, log_file = sys.argv[1:4]
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    with open(pid_file, "w", encoding="ascii") as f:
        f.write(str(os.getpid()))
    status("warming")
    if mode == "engine-error":
        send({"type": "error", "id": None, "code": "engine_unavailable",
              "message": "the redaction engine could not start"})
        status("unavailable")
    elif mode in ("hang", "garbage"):
        status("ready-text")
        status("ready-all")
    totals = {}
    with open(log_file, "a", encoding="utf-8") as log:
        for msg in frames():
            log.write(json.dumps({"type": msg.get("type"), "id": msg.get("id")}) + "\n")
            log.flush()
            if msg.get("type") in ("redact_text", "redact_file"):
                totals[msg["id"]] = msg["total"]
            elif msg.get("type") == "chunk" and msg["seq"] + 1 == totals.get(msg["id"]):
                rid = msg["id"]
                if mode == "engine-error":
                    send({"type": "error", "id": rid, "code": "engine_unavailable",
                          "message": "the redaction engine could not start"})
                elif mode == "hang":
                    send({"type": "progress", "id": rid, "stage": "queued"})
                    send({"type": "progress", "id": rid, "stage": "redacting"})
                elif mode == "garbage":
                    send({"type": "result", "id": rid, "size": 4, "total": 1, "note": "not in the protocol",
                          "parts": [{"name": "text", "media_type": "text/plain", "size": 4}]})


if __name__ == "__main__":
    main()
