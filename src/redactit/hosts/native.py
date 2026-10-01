"""Chrome native-messaging host: the browser extension's only way into the engine.

Chrome starts this process when the extension connects, passes the caller's origin
("chrome-extension://<id>/") as an argument, and speaks its framing on stdin and stdout:
a 4-byte native-endian length, then that many bytes of UTF-8 JSON. stdout carries
protocol frames and nothing else.

Extension to host                                           Host to extension
  ping        {id}                                            status    {id|null, state, version, protocol}
  redact_text {id, scope, site, size, total} + chunks         progress  {id, stage[, page, pages]}
  redact_file {id, scope, site, kind, size, total} + chunks   result    {id, size, total, parts} + chunks
  chunk       {id, seq, total, data}                          error     {id|null, code, message}
  cancel      {id}

Every message is a JSON object with a `type` and exactly the fields above. A payload (text
as UTF-8, a file as it is) travels as base64 chunks of RAW_CHUNK bytes numbered from 0;
every chunk but the last is full, so `total` follows from `size`. A chunk out of order, of
the wrong size or under the wrong total drops its request. A result's parts (a PDF and its
Markdown, say) are concatenated, chunked the same way, and split again by their sizes.

`state` goes warming -> ready-text -> ready-all (or unavailable if the engine cannot
load). Requests are answered in arrival order; one that arrives while warming waits. An
error's message is fixed text or a RedactitError's, so it never quotes the input.

An error's `code` is one of MESSAGES' keys and stays stable across versions. A request
that fails is answered with exactly one error and no result. Among them: engine_unavailable
when the engine, or for PDFs and images OCR, could not start; bad_input when the payload
cannot be read as its kind; detection_timeout when a detection pattern ran past its time
limit, so the input was not fully checked; internal for anything else.

Threads, not asyncio: on Windows asyncio needs a socket pair, which the engine's network
block refuses. One thread reads frames, one loads the engine (text first, then OCR), one
works through the queue, and one ends the process after IDLE_SECONDS without work.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from redactit import __version__

PROTOCOL = 1
CHUNK = 512 * 1024  # base64 characters per chunk: a frame stays near 512 KiB, half Chrome's 1 MB cap to the extension
RAW_CHUNK = CHUNK // 4 * 3  # the payload bytes those characters carry (384 KiB)
MAX_FRAME = CHUNK + 4096  # one chunk and its envelope: the largest frame a well-formed extension sends
TO_EXTENSION_MAX = 1024 * 1024  # Chrome's cap on one message to the extension
STREAM_MAX = 64 * 2**20  # Chrome's cap the other way: a longer length means the stream itself is corrupt
# One paste or file, as large as Chrome would pass in one message. A 500-page scan or a
# 50 MP photo as JPEG fits; pastes get no smaller cap (docs/perf/speed-plan.md, decision 3).
MAX_PAYLOAD = 64 * 2**20
# Requests received and not yet answered: a multi-file drop plus pastes behind it. Inputs
# held at once stay within two of the largest, small beside the warm engine's ~1 GB.
MAX_IN_FLIGHT = 16
MAX_BUFFERED = 2 * MAX_PAYLOAD
IDLE_SECONDS = 30 * 60

KINDS = ("txt", "md", "docx", "pdf", "image")
_NEEDS_IMAGES = {"pdf", "image"}  # these wait for OCR; text and Word documents do not

MESSAGES = {
    "bad_frame": "the message length is impossible; the connection is closing",
    "message_too_large": f"a message may be at most {MAX_FRAME} bytes; it was skipped",
    "bad_json": "the message is not a UTF-8 JSON object",
    "bad_message": "the message does not match the protocol",
    "bad_sequence": "a chunk was out of order, the wrong size or under the wrong total; the request is dropped",
    "unknown_request": "no request with this id is in progress",
    "duplicate_request": "a request with this id is already in progress",
    "too_large": f"the payload is larger than {MAX_PAYLOAD // 2**20} MiB",
    "busy": f"too many requests in progress (at most {MAX_IN_FLIGHT} and {MAX_BUFFERED // 2**20} MiB)",
    "cancelled": "the request was cancelled",
    "bad_input": "the input could not be read",
    "detection_timeout": "a detection pattern ran too long, so the input was not fully checked; nothing was redacted",
    "engine_unavailable": "the redaction engine could not start",
    "internal": "internal error (details withheld: they may contain input text)",
    "origin_refused": "this caller is not allowed to use Redactit",
    "bad_config": "the host's installed manifest is missing or invalid",
}

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A chat's scope comes from its URL ("claude.ai/chat/<uuid>"); the vault stores only its HMAC.
_SCOPE = re.compile(r"[A-Za-z0-9_.:/-]{1,256}")
_HOSTNAME = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+")
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}/")


def _text(pattern: re.Pattern):
    return lambda v: isinstance(v, str) and pattern.fullmatch(v) is not None


def _count(low: int):
    return lambda v: type(v) is int and v >= low  # not bool, not float


_REQUEST = {"id": _text(_ID), "scope": _text(_SCOPE), "site": _text(_HOSTNAME), "size": _count(0), "total": _count(1)}
SCHEMA = {
    "ping": {"id": _text(_ID)},
    "cancel": {"id": _text(_ID)},
    "redact_text": _REQUEST,
    "redact_file": {**_REQUEST, "kind": lambda v: isinstance(v, str) and v in KINDS},
    "chunk": {"id": _text(_ID), "seq": _count(0), "total": _count(1),
              "data": lambda v: isinstance(v, str) and len(v) <= CHUNK},
}


class Reject(Exception):
    """A refusal answered with an error frame. `message` never quotes the input."""

    def __init__(self, code: str, message: str | None = None, rid: str | None = None):
        super().__init__(code)
        self.code, self.message, self.rid = code, message or MESSAGES[code], rid


class _Cancelled(Exception):
    pass


def parse(body: bytes) -> dict:
    """One message checked against SCHEMA, or Reject."""
    try:
        msg = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise Reject("bad_json") from None
    if not isinstance(msg, dict):
        raise Reject("bad_json")
    rid = msg.get("id") if _text(_ID)(msg.get("id")) else None  # echoed only if it is a well-formed id
    fields = SCHEMA.get(msg.get("type")) if isinstance(msg.get("type"), str) else None
    if fields is None:
        raise Reject("bad_message", "unknown message type", rid)
    if msg.keys() != {"type", *fields}:
        raise Reject("bad_message", "unknown or missing fields", rid)
    for name, valid in fields.items():
        if not valid(msg[name]):
            raise Reject("bad_message", f"field {name!r} is not valid", rid)
    return msg


def _unique_keys(pairs):
    d = dict(pairs)
    if len(d) != len(pairs):  # {"size": 1, "size": 9} must not mean whichever came last
        raise ValueError("duplicate key")
    return d


def _no_constant(_name):
    raise ValueError("NaN and Infinity are not JSON")


def chunk_count(size: int) -> int:
    return max(1, -(-size // RAW_CHUNK))


@dataclass(eq=False)
class Job:
    """One request: its chunks while they arrive, then its place in the queue."""

    id: str
    kind: str  # "text" for redact_text, else one of KINDS
    scope: str
    site: str
    size: int
    total: int
    chunks: list[bytes] = field(default_factory=list)
    received: int = 0
    cancelled: bool = False

    @classmethod
    def start(cls, msg: dict) -> "Job":
        job = cls(msg["id"], msg.get("kind", "text"), msg["scope"], msg["site"], msg["size"], msg["total"])
        if job.total != chunk_count(job.size):
            raise Reject("bad_sequence", rid=job.id)
        return job

    def add(self, msg: dict) -> bool:
        """Append one chunk; True once the payload is complete."""
        if msg["seq"] != len(self.chunks) or msg["total"] != self.total:
            raise Reject("bad_sequence", rid=self.id)
        try:
            data = base64.b64decode(msg["data"], validate=True)
        except ValueError:  # binascii.Error, or a non-ASCII character
            raise Reject("bad_message", "field 'data' is not base64", self.id) from None
        if len(data) != min(RAW_CHUNK, self.size - self.received):
            raise Reject("bad_sequence", rid=self.id)
        self.chunks.append(data)
        self.received += len(data)
        return len(self.chunks) == self.total

    def payload(self) -> bytes:
        data = b"".join(self.chunks)
        self.chunks.clear()  # one copy is enough
        return data


class Host:
    """The protocol over two file descriptors, with the engine built by `load_engine`."""

    def __init__(self, rfd: int, wfd: int, load_engine, *, idle_seconds: float = IDLE_SECONDS):
        self._rfd, self._wfd, self._load_engine, self._idle = rfd, wfd, load_engine, idle_seconds
        self._cond = threading.Condition()  # guards everything below; taken before _send_lock, never after
        self._send_lock = threading.Lock()
        self._state = "warming"
        self._warming = True  # until both load stages have finished or failed
        self._engine = None
        self._failed: Reject | None = None  # text stage failed: every request gets this
        self._image_failure: Reject | None = None  # OCR stage failed: PDFs and images get this
        self._uploads: dict[str, Job] = {}
        self._queue: deque[Job] = deque()
        self._running: Job | None = None
        self._buffered = 0
        self._dropped: deque[str] = deque(maxlen=64)  # refused or cancelled uploads whose chunks may still arrive
        self._last_active = time.monotonic()
        self._done = threading.Event()
        self._exit_code = 0

    def run(self) -> int:
        """Serve until the extension disconnects, the stream breaks, or the host idles out."""
        self._status(None)
        for target in (self._read_frames, self._load, self._work, self._watch_idle):
            name = "redactit-host-" + target.__name__.strip("_").replace("_", "-")
            threading.Thread(target=self._guard, args=(target,), name=name, daemon=True).start()
        self._done.wait()
        return self._exit_code

    def _guard(self, target) -> None:
        try:
            target()
        except BaseException as e:  # noqa: BLE001 - a dead thread would leave requests unanswered
            _diag(f"internal error in {threading.current_thread().name} ({type(e).__name__})")
            try:
                self._error(Reject("internal"))
            finally:
                self._stop(1)

    def _stop(self, code: int) -> None:
        if not self._done.is_set():
            self._exit_code = code
            self._done.set()

    # --- sending -------------------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        body = json.dumps(msg, separators=(",", ":")).encode("ascii")
        if len(body) > TO_EXTENSION_MAX:  # chunks are sized so this cannot happen; Chrome would drop the port
            raise RuntimeError("a frame to the extension is over Chrome's limit")
        with self._send_lock:
            try:
                _write_all(self._wfd, struct.pack("=I", len(body)) + body)
            except OSError:  # the extension is gone
                self._stop(0)
        self._last_active = time.monotonic()

    def _status(self, rid: str | None) -> None:
        self._send({"type": "status", "id": rid, "state": self._state, "version": __version__, "protocol": PROTOCOL})

    def _progress(self, rid: str, stage: str, page: int | None = None, pages: int | None = None) -> None:
        msg = {"type": "progress", "id": rid, "stage": stage}
        if page is not None:
            msg.update(page=page, pages=pages)
        self._send(msg)

    def _error(self, rej: Reject) -> None:
        self._send({"type": "error", "id": rej.rid, "code": rej.code, "message": rej.message})

    def _result(self, rid: str, parts: list[tuple[str, str, bytes]]) -> None:
        blob = b"".join(data for *_, data in parts)
        total = chunk_count(len(blob))
        self._send({"type": "result", "id": rid, "size": len(blob), "total": total,
                    "parts": [{"name": name, "media_type": media, "size": len(data)} for name, media, data in parts]})
        for seq in range(total):
            data = base64.b64encode(blob[seq * RAW_CHUNK:(seq + 1) * RAW_CHUNK]).decode("ascii")
            self._send({"type": "chunk", "id": rid, "seq": seq, "total": total, "data": data})

    # --- reading -------------------------------------------------------------------------

    def _read_frames(self) -> None:
        while not self._done.is_set():
            head = _read_exact(self._rfd, 4)
            if head is None:
                return self._stop(0)  # Chrome closed the port
            self._last_active = time.monotonic()
            (length,) = struct.unpack("=I", head)
            if length > STREAM_MAX:  # nothing after this can be trusted to start on a frame
                self._error(Reject("bad_frame"))
                return self._stop(1)
            if length > MAX_FRAME:  # skipped whole, so the next frame is still found
                if not _skip(self._rfd, length):
                    return self._stop(0)
                self._error(Reject("message_too_large"))
                continue
            body = _read_exact(self._rfd, length)
            if body is None:
                return self._stop(0)
            try:
                self._handle(parse(body))
            except Reject as rej:
                self._error(rej)

    def _handle(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "ping":
            with self._cond:
                self._status(msg["id"])
        elif kind == "cancel":
            self._cancel(msg["id"])
        elif kind == "chunk":
            self._chunk(msg)
        else:
            self._begin(msg)

    def _begin(self, msg: dict) -> None:
        rid = msg["id"]
        with self._cond:
            if rid in self._uploads or self._find(rid) or (self._running and self._running.id == rid):
                raise Reject("duplicate_request", rid=rid)
            try:
                if msg["size"] > MAX_PAYLOAD:
                    raise Reject("too_large", rid=rid)
                in_flight = len(self._uploads) + len(self._queue) + (self._running is not None)
                if in_flight >= MAX_IN_FLIGHT or self._buffered + msg["size"] > MAX_BUFFERED:
                    raise Reject("busy", rid=rid)
                job = Job.start(msg)
            except Reject:
                self._dropped.append(rid)  # its chunks are already on the way
                raise
            self._uploads[rid] = job
            self._buffered += job.size

    def _chunk(self, msg: dict) -> None:
        rid = msg["id"]
        with self._cond:
            job = self._uploads.get(rid)
            if job is None:
                if rid in self._dropped:
                    return
                raise Reject("unknown_request", rid=rid)
            try:
                complete = job.add(msg)
            except Reject:
                self._drop_upload(rid)
                raise
            if complete:
                del self._uploads[rid]
                self._progress(rid, "queued")  # before the worker can see it, so stages arrive in order
                self._queue.append(job)
                self._cond.notify_all()

    def _cancel(self, rid: str) -> None:
        with self._cond:
            if rid in self._uploads:
                self._drop_upload(rid)
            elif job := self._find(rid):
                self._queue.remove(job)
                self._buffered -= job.size
                self._dropped.append(rid)
            elif self._running and self._running.id == rid:
                self._running.cancelled = True  # the worker answers at its next check
                return
            elif rid in self._dropped:
                return  # already refused or cancelled
            else:
                raise Reject("unknown_request", rid=rid)
            self._error(Reject("cancelled", rid=rid))

    def _find(self, rid: str) -> Job | None:
        return next((job for job in self._queue if job.id == rid), None)

    def _drop_upload(self, rid: str) -> None:
        self._buffered -= self._uploads.pop(rid).size
        self._dropped.append(rid)

    # --- loading -------------------------------------------------------------------------

    def _load(self) -> None:
        """Text detection first, announced as soon as it works; then OCR, while pastes run."""
        try:
            engine = self._load_engine()
            engine.warm_text()
        except Exception as e:  # noqa: BLE001 - fail closed: every request is answered with it
            failure = _load_failure(e, "text detection")
            with self._cond:
                self._failed, self._state, self._warming = failure, "unavailable", False
                self._error(failure)
                self._status(None)
                self._cond.notify_all()
            return
        with self._cond:
            self._engine, self._state = engine, "ready-text"
            self._status(None)  # sent before the worker can take a request, so it arrives first
            self._cond.notify_all()
        try:
            engine.warm_images()
        except Exception as e:  # noqa: BLE001 - text keeps working; PDFs and images get the error
            failure = _load_failure(e, "OCR")
            with self._cond:
                self._image_failure, self._warming = failure, False
                self._error(failure)
                self._cond.notify_all()
            return
        with self._cond:
            self._state, self._warming = "ready-all", False
            self._status(None)
            self._cond.notify_all()

    # --- working -------------------------------------------------------------------------

    def _work(self) -> None:
        while True:
            with self._cond:
                while not (self._queue and self._can_start(self._queue[0])):
                    self._cond.wait()
                job = self._running = self._queue.popleft()
            try:
                self._process(job)
            finally:
                with self._cond:
                    self._running = None
                    self._buffered -= job.size
                    self._cond.notify_all()
                self._last_active = time.monotonic()

    def _can_start(self, job: Job) -> bool:
        if self._failed:
            return True  # answered with the failure straight away
        if job.kind in _NEEDS_IMAGES:
            return self._state == "ready-all" or self._image_failure is not None
        return self._state in ("ready-text", "ready-all")

    def _process(self, job: Job) -> None:
        failure = self._failed or (self._image_failure if job.kind in _NEEDS_IMAGES else None)
        if failure:
            return self._error(Reject(failure.code, failure.message, job.id))
        self._progress(job.id, "redacting")
        try:
            parts = self._redact(job)
            if job.cancelled:
                raise _Cancelled
        except _Cancelled:
            return self._error(Reject("cancelled", rid=job.id))
        except Exception as e:  # noqa: BLE001 - fail closed: an error frame, never the input
            return self._error(_request_failure(e, job.id))
        self._result(job.id, parts)

    def _redact(self, job: Job) -> list[tuple[str, str, bytes]]:
        """(part name, media type, bytes) for each output. Text parts are UTF-8."""
        engine, data, where = self._engine, job.payload(), {"destination": job.site}
        if job.kind in ("text", "txt", "md", "docx"):
            if job.kind == "docx":
                from redactit.formats.docx import docx_to_markdown

                text, file_type = docx_to_markdown(data), "docx"
            else:
                text, file_type = data.decode("utf-8"), "paste" if job.kind == "text" else job.kind
            out = engine.redact(text, job.scope, file_type=file_type, site=job.site, **where).text
            media = "text/plain" if job.kind in ("text", "txt") else "text/markdown"  # Word comes out as Markdown
            return [("text", media, out.encode("utf-8"))]
        if job.kind == "pdf":
            from redactit.formats.pdf import redact_pdf

            def page(number: int, pages: int) -> None:
                if job.cancelled:
                    raise _Cancelled  # stops between pages instead of finishing a long document
                self._progress(job.id, "redacting", number, pages)

            pdf, markdown = redact_pdf(data, engine, job.scope, site=job.site, progress=page, **where)
            return [("file", "application/pdf", pdf), ("text", "text/markdown", markdown.encode("utf-8"))]
        from redactit.formats.image import redact_image

        image, suffix, _ = redact_image(data, engine, job.scope, site=job.site, **where)
        return [("file", "image/jpeg" if suffix == ".jpg" else "image/png", image)]

    def _watch_idle(self) -> None:
        step = min(1.0, self._idle / 4)
        while not self._done.wait(step):
            with self._cond:
                busy = self._warming or self._uploads or self._queue or self._running is not None
            if not busy and time.monotonic() - self._last_active >= self._idle:
                return self._stop(0)


def _load_failure(exc: Exception, stage: str) -> Reject:
    from redactit.types import RedactitError

    _diag(f"{stage} could not start ({type(exc).__name__})")
    # Our own errors explain themselves ("run `redactit setup-models`"); others may quote anything.
    return Reject("engine_unavailable", str(exc) if isinstance(exc, RedactitError) else None)


def _request_failure(exc: Exception, rid: str) -> Reject:
    from redactit.detect.registry import PatternTimeout
    from redactit.models import ModelError
    from redactit.types import RedactitError
    from redactit.vault import VaultError

    if isinstance(exc, (ModelError, VaultError)):
        return Reject("engine_unavailable", str(exc), rid)
    if isinstance(exc, PatternTimeout):  # the input was fine; the engine could not finish checking it
        return Reject("detection_timeout", rid=rid)
    if isinstance(exc, RedactitError):  # an unreadable PDF, image or Word file
        return Reject("bad_input", str(exc), rid)
    if isinstance(exc, UnicodeDecodeError):
        return Reject("bad_input", "the text is not valid UTF-8", rid)
    _diag(f"a request failed ({type(exc).__name__})")
    return Reject("internal", rid=rid)


def _read_exact(fd: int, n: int) -> bytes | None:
    """Exactly n bytes, or None at end of input."""
    buf = bytearray()
    while len(buf) < n:
        piece = os.read(fd, min(n - len(buf), 1 << 20))
        if not piece:
            return None
        buf += piece
    return bytes(buf)


def _skip(fd: int, n: int) -> bool:
    while n:
        piece = os.read(fd, min(n, 1 << 20))
        if not piece:
            return False
        n -= len(piece)
    return True


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# --- process setup ---------------------------------------------------------------------

_DIAG = None  # the real stderr, for the host's own one-line diagnostics


def _diag(message: str) -> None:
    """One line on stderr from the host itself: fixed text and type names, never input."""
    stream = _DIAG or sys.__stderr__
    try:
        stream.write(f"redactit host: {message}\n")
        stream.flush()
    except (AttributeError, OSError, ValueError):  # no stderr at all is fine
        pass


class _Withheld(io.TextIOBase):
    """sys.stdout and sys.stderr for everything but the host itself.

    What a library prints can quote the input (a warning about the text it choked on, a
    traceback), and Chrome may log a host's stderr, so only the fact is kept.
    """

    encoding = "utf-8"

    def __init__(self) -> None:
        self._last = 0.0

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        now = time.monotonic()
        if s.strip() and now - self._last >= 1.0:  # one note per burst, not one per line
            self._last = now
            _diag("a library message was withheld (it may quote input)")
        return len(s)


def _claim_stdio() -> tuple[int, int]:
    """Private copies of stdin and stdout for the protocol; the standard ones are replaced.

    stdout becomes stderr, so a stray print from Python or native code cannot corrupt a
    frame. stdin becomes the null device because on Windows a read waiting on a pipe
    blocks every other call on that pipe, including the GetFileType a DLL's C runtime makes
    on the standard handles as it loads: numpy's import hung while the reader waited.
    """
    global _DIAG
    rfd, wfd = os.dup(0), os.dup(1)
    for fd, mode in ((0, os.O_RDONLY), (1, os.O_WRONLY)):
        null = os.open(os.devnull, mode)
        os.dup2(null, fd)
        os.close(null)
    try:
        os.dup2(2, 1)
    except OSError:  # started without a stderr: stdout stays the null device
        pass
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        # Text mode would turn \n into \r\n and stop reading at a 0x1A byte inside a length.
        for fd in (rfd, wfd):
            msvcrt.setmode(fd, os.O_BINARY)
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        for fd, std in ((0, -10), (1, -11)):  # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, as DLLs look them up
            kernel32.SetStdHandle(std & 0xFFFFFFFF, msvcrt.get_osfhandle(fd))
    _DIAG = sys.__stderr__
    sys.stdout = sys.stderr = _Withheld()
    sys.excepthook = lambda kind, *_: _diag(f"internal error ({kind.__name__})")
    threading.excepthook = lambda args: _diag(f"internal error in a thread ({args.exc_type.__name__})")
    return rfd, wfd


def allowed_origins(manifest: Path) -> set[str]:
    """The caller the installed host manifest allows: the same list Chrome enforces (T8).

    Read from the file the installer wrote, never from the environment, which whoever
    starts the process controls. Exactly one well-formed extension ID, so the template's
    placeholder, or a second caller added later, is refused.
    """
    try:
        origins = json.loads(manifest.read_text(encoding="utf-8"))["allowed_origins"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Reject("bad_config") from None
    if not (isinstance(origins, list) and len(origins) == 1 and _text(_EXTENSION_ORIGIN)(origins[0])):
        raise Reject("bad_config")
    return set(origins)


def _open_engine():
    from redactit import models

    pending = models.verify(models.TEXT_MODELS)  # the hash runs while the engine's imports below do
    with models.released_on_error(pending):
        from redactit.cli import open_engine
    return open_engine(verified=pending)  # releases it too if the engine cannot be built


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("origin", help="the caller, as Chrome passes it: chrome-extension://<id>/")
    parser.add_argument("--manifest", type=Path, required=True,
                        help="the installed host manifest; its one allowed origin is the only caller served")
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS,
                        help="exit after this long with no requests (default: 30 minutes)")
    parser.add_argument("--parent-window", help=argparse.SUPPRESS)  # Chrome adds it on Windows; unused


def serve(args: argparse.Namespace) -> int:
    """Run the host for one Chrome connection, then end the process.

    Ends with os._exit: a redaction still running in native code when Chrome disconnects
    cannot be interrupted, and nothing is lost by not waiting (the vault and audit log
    commit every write; there are no temp files).
    """
    rfd, out = _claim_stdio()
    try:
        allowed = allowed_origins(args.manifest)
        if args.origin not in allowed:
            raise Reject("origin_refused")
    except Reject as rej:
        _diag(rej.message)
        _write_all(out, _frame({"type": "error", "id": None, "code": rej.code, "message": rej.message}))
        _exit(1)
    from redactit import safety

    safety.block_network()  # before any engine code is imported
    _exit(Host(rfd, out, _open_engine, idle_seconds=max(args.idle_seconds, 0.1)).run())


def _frame(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":")).encode("ascii")
    return struct.pack("=I", len(body)) + body


def _exit(code: int) -> None:
    try:
        sys.__stderr__.flush()
    except (AttributeError, OSError, ValueError):
        pass
    os._exit(code)


def main(argv: list[str] | None = None) -> int:
    """Entry point for a launcher that starts the interpreter directly (see docs/PLAN.md §7)."""
    parser = argparse.ArgumentParser(prog="redactit host", description="Redactit's Chrome native-messaging host.")
    add_arguments(parser)
    return serve(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
