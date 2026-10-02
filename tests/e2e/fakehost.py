"""A stand-in native host that misbehaves on purpose, for the fail-closed browser tests.

Started by Chrome through a test launcher as `fakehost.py <mode> <pid file> <log file>`.
It speaks the real host's framing (src/redactit/hosts/native.py) and never redacts:

  warming       says `warming` and never gets further
  hang          gets ready, takes requests, then never answers
  engine-error  fails to load its engine, as a host without its models would
  garbage       answers a request with a frame the protocol does not allow
  v1            speaks protocol 1, as a host older than the extension would
  review-always, review-low, review-low-clear, review-off
                answer every request with REDACTED, under that review mode, with one
                decision marked for review (none for review-low-clear)

It writes its process id, so a test can kill it, and logs the type and id of every
frame it receives (never their data), so a test can see a cancel arrive. Standard
library only: it runs under `-I -S`, like the real host's launcher.
"""

import base64
import json
import os
import struct
import sys


def send(msg: dict) -> None:
    body = json.dumps(msg).encode("ascii")
    sys.stdout.buffer.write(struct.pack("=I", len(body)) + body)
    sys.stdout.buffer.flush()


REDACTED = b"Please email [PERSON_1] at [EMAIL_1]."
REVIEW = {"review-always": ("always", 0), "review-low": ("low_confidence", 1),
          "review-low-clear": ("low_confidence", 0), "review-off": ("off", 1)}
MODE = None


def status(state: str) -> None:
    if MODE == "v1":
        send({"type": "status", "id": None, "state": state, "version": "0.0.9", "protocol": 1})
        return
    loaded = state not in ("warming", "unavailable")  # as the real host: null until the engine has loaded
    review_mode = REVIEW.get(MODE, ("low_confidence", 0))[0] if loaded else None
    send({"type": "status", "id": None, "state": state, "version": "0.1.0", "protocol": 2,
          "review_mode": review_mode})


def answer(rid: str) -> None:
    count = REVIEW[MODE][1]
    send({"type": "progress", "id": rid, "stage": "redacting"})
    send({"type": "result", "id": rid, "size": len(REDACTED), "total": 1,
          "parts": [{"name": "text", "media_type": "text/plain", "size": len(REDACTED)}],
          "review": {"needed": count > 0, "count": count}})
    send({"type": "chunk", "id": rid, "seq": 0, "total": 1, "data": base64.b64encode(REDACTED).decode("ascii")})


def frames():
    while (head := sys.stdin.buffer.read(4)) and len(head) == 4:
        yield json.loads(sys.stdin.buffer.read(struct.unpack("=I", head)[0]))


def main() -> None:
    global MODE
    mode, pid_file, log_file = sys.argv[1:4]
    MODE = mode
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
    elif mode != "warming":
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
                          "parts": [{"name": "text", "media_type": "text/plain", "size": 4}],
                          "review": {"needed": False, "count": 0}})
                elif mode in REVIEW:
                    answer(rid)


if __name__ == "__main__":
    main()
