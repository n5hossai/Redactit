"""The native host's message schema, chunk reassembly and install config, without a process.

Round trips through a real host process are in tests/test_host.py.
"""

import base64
import json
import os
import queue
import struct
import threading
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from redactit.detect.registry import PatternTimeout
from redactit.hosts import native
from redactit.hosts.native import RAW_CHUNK, Job, Reject, parse
from redactit.models import ModelError

TEMPLATE = Path(__file__).resolve().parents[2] / "installers" / "host-manifest.json"
EXTENSION = "chrome-extension://" + "abcdefghijklmnop" * 2 + "/"
SECRET = "Priya Okafor"  # must never come back in an error


def body(msg) -> bytes:
    return json.dumps(msg).encode("utf-8")


def header(rid="r1", size=10, **extra):
    return {"type": "redact_text", "id": rid, "scope": "claude.ai/chat/1", "site": "claude.ai", "size": size,
            "total": native.chunk_count(size), **extra}


def chunk(seq, data: bytes, total, rid="r1"):
    return {"type": "chunk", "id": rid, "seq": seq, "total": total, "data": base64.b64encode(data).decode("ascii")}


def rejected(raw: bytes) -> Reject:
    with pytest.raises(Reject) as info:
        parse(raw)
    assert SECRET not in info.value.message and SECRET not in str(info.value.rid)
    return info.value


@pytest.mark.parametrize("msg", [
    {"type": "ping", "id": "p1"},
    {"type": "cancel", "id": "r1"},
    header(),
    {**header(), "type": "redact_file", "kind": "pdf"},
    chunk(0, b"abc", 1),
])
def test_every_message_type_parses(msg):
    assert parse(body(msg)) == msg


@pytest.mark.parametrize(("raw", "code"), [
    (b"", "bad_json"),
    (b"{not json", "bad_json"),
    (b"\xff\xfe{}", "bad_json"),  # not UTF-8
    (b"[1, 2]", "bad_json"),  # not an object
    (b'{"type": "ping", "id": "a", "id": "b"}', "bad_json"),  # duplicate key
    (b'{"type": "ping", "id": NaN}', "bad_json"),
    (b"[" * 100_000 + b"]" * 100_000, "bad_json"),  # nested too deep to parse
], ids=["empty", "not-json", "not-utf8", "not-object", "duplicate-key", "nan", "too-deep"])
def test_malformed_json_is_refused(raw, code):
    assert rejected(raw).code == code


@pytest.mark.parametrize(("msg", "detail"), [
    ({"type": "exfiltrate", "id": "u1", "text": SECRET}, "unknown message type"),
    ({"type": ["ping"], "id": "u1"}, "unknown message type"),
    ({"type": "ping", "id": "u1", "note": SECRET}, "unknown or missing fields"),
    ({"type": "ping"}, "unknown or missing fields"),
    ({**header(), "kind": "pdf"}, "unknown or missing fields"),  # kind belongs to redact_file only
    ({**header(), "size": True}, "field 'size' is not valid"),
    ({**header(), "size": 10.0}, "field 'size' is not valid"),
    ({**header(), "size": -1}, "field 'size' is not valid"),
    ({**header(), "total": 0}, "field 'total' is not valid"),
    ({**header(), "site": "https://claude.ai/chat"}, "field 'site' is not valid"),
    ({**header(), "scope": SECRET}, "field 'scope' is not valid"),
    ({**header(), "type": "redact_file", "kind": "exe"}, "field 'kind' is not valid"),
    ({**chunk(0, b"", 1), "data": "A" * (native.CHUNK + 4)}, "field 'data' is not valid"),
])
def test_schema_violations_are_refused_without_echoing_values(msg, detail):
    rej = rejected(body(msg))
    assert (rej.code, rej.message) == ("bad_message", detail)


def test_an_id_that_could_carry_content_is_not_echoed():
    rej = rejected(body({"type": "exfiltrate", "id": SECRET}))
    assert rej.rid is None


def test_chunks_reassemble_exactly():
    data = bytes(range(256)) * (RAW_CHUNK // 256 * 2 + 3)  # two full chunks and a partial one
    job = Job.start(header(size=len(data)))
    pieces = [data[i:i + RAW_CHUNK] for i in range(0, len(data), RAW_CHUNK)]
    done = [job.add(parse(body(chunk(seq, p, len(pieces))))) for seq, p in enumerate(pieces)]
    assert done == [False, False, True] and job.payload() == data


def test_an_empty_payload_is_one_empty_chunk():
    job = Job.start(header(size=0))
    assert job.total == 1 and job.add(chunk(0, b"", 1)) and job.payload() == b""


@pytest.mark.parametrize("bad", [
    chunk(1, b"x" * RAW_CHUNK, 2),  # wrong sequence number
    chunk(0, b"x" * RAW_CHUNK, 3),  # wrong total
    chunk(0, b"x" * 10, 2),  # a short chunk before the last
])
def test_out_of_order_or_wrong_size_chunks_are_refused(bad):
    job = Job.start(header(size=RAW_CHUNK + 5))
    with pytest.raises(Reject) as info:
        job.add(bad)
    assert (info.value.code, info.value.rid) == ("bad_sequence", "r1")


def test_a_last_chunk_longer_than_declared_is_refused():
    job = Job.start(header(size=5))
    with pytest.raises(Reject, match="bad_sequence"):
        job.add(chunk(0, b"123456", 1))


def test_a_total_that_does_not_follow_from_the_size_is_refused():
    with pytest.raises(Reject, match="bad_sequence"):
        Job.start({**header(size=10), "total": 2})


def test_chunk_data_must_be_base64():
    job = Job.start(header(size=3))
    with pytest.raises(Reject) as info:
        job.add({**chunk(0, b"abc", 1), "data": "not base64!"})
    assert info.value.code == "bad_message"


def test_the_installed_manifest_is_the_only_source_of_the_allowed_origin(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"allowed_origins": [EXTENSION]}), encoding="utf-8")
    assert native.allowed_origins(manifest) == {EXTENSION}


@pytest.mark.parametrize("origins", [
    [],
    [EXTENSION, "chrome-extension://" + "b" * 32 + "/"],  # a second caller
    ["chrome-extension://*/"],
    ["https://claude.ai/"],
    "chrome-extension://" + "a" * 32 + "/",  # not a list
])
def test_a_manifest_must_allow_exactly_one_extension(tmp_path, origins):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"allowed_origins": origins}), encoding="utf-8")
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(manifest)


def test_the_unfilled_template_is_refused():
    """An installer that forgot to write the real ID gets a host that serves nobody."""
    assert json.loads(TEMPLATE.read_text(encoding="utf-8"))["type"] == "stdio"
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(TEMPLATE)


def test_a_missing_manifest_is_refused(tmp_path):
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(tmp_path / "missing.json")


def test_frames_to_the_extension_stay_under_chromes_limit():
    largest = native._frame({"type": "chunk", "id": "x" * 64, "seq": 10**6, "total": 10**6,
                             "data": base64.b64encode(b"\xff" * RAW_CHUNK).decode("ascii")})
    assert native.CHUNK <= len(largest) <= native.TO_EXTENSION_MAX and len(largest) <= native.MAX_FRAME


# --- The host in this process, over pipes, with a stand-in engine ---------------------------
# Every way the engine can fail must end each request with one error and no result.


class StubEngine:
    """Stands in for pipeline.Engine; each stage can be made to fail."""

    def __init__(self, *, text_fails=None, images_fail=None, redact_raises=None, review_mode="low_confidence_only",
                 flags=(False,)):
        self.text_fails, self.images_fail, self.redact_raises = text_fails, images_fail, redact_raises
        self.policy = SimpleNamespace(review=SimpleNamespace(mode=review_mode))
        self.flags = flags  # needs_review of each decision a redact call returns
        self.sites = []

    def warm_text(self):
        if self.text_fails:
            raise self.text_fails

    def warm_images(self):
        if self.images_fail:
            raise self.images_fail

    def redact(self, text, scope, *, site=None, **_kwargs):
        self.sites.append(site)
        if self.redact_raises:
            raise self.redact_raises
        return SimpleNamespace(text=text.replace(SECRET, "[PERSON_1]"),
                               decisions=[SimpleNamespace(needs_review=f) for f in self.flags])


class Wire:
    """A native.Host on its own threads, spoken to through two pipes as Chrome would."""

    def __init__(self, load_engine):
        self._to_host, self._sender = os.pipe()
        self._receiver, self._from_host = os.pipe()
        self.host = native.Host(self._to_host, self._from_host, load_engine, idle_seconds=600)
        self.received: list[dict] = []
        self._routes = defaultdict(queue.Queue)
        self._served = threading.Thread(target=self.host.run, daemon=True)
        self._served.start()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while (head := native._read_exact(self._receiver, 4)) is not None:
            msg = json.loads(native._read_exact(self._receiver, struct.unpack("=I", head)[0]))
            self.received.append(msg)
            self._routes[msg.get("id")].put(msg)

    def send(self, msg):
        native._write_all(self._sender, native._frame(msg))

    def request(self, rid, kind, payload: bytes, site="claude.ai"):
        types = {"text": "redact_text"}
        header = {"type": types.get(kind, "redact_file"), "id": rid, "scope": "s1",
                  "site": site, "size": len(payload), "total": 1}
        if kind not in types:
            header["kind"] = kind
        self.send(header)
        self.send(chunk(0, payload, 1, rid))

    def next(self, rid, timeout=30):
        return self._routes[rid].get(timeout=timeout)

    def outcome(self, rid):
        """The first error or result for `rid`, passing over progress frames."""
        while (msg := self.next(rid))["type"] == "progress":
            pass
        return msg

    def close(self):
        os.close(self._sender)  # end of input, as when Chrome closes the port
        self._served.join(10)
        for fd in (self._from_host, self._to_host, self._receiver):
            os.close(fd)
        everything = json.dumps(self.received)
        assert SECRET not in everything and "Okafor" not in everything


@pytest.fixture
def wire():
    wires = []

    def start(load_engine):
        wires.append(Wire(load_engine))
        return wires[-1]

    yield start
    for w in wires:
        w.close()


PAYLOADS = {"text": f"Call {SECRET}.".encode(), "txt": f"Call {SECRET}.".encode(),
            "pdf": b"%PDF-1.7 " + SECRET.encode(), "image": b"\x89PNG " + SECRET.encode()}


def _cannot_load():
    raise RuntimeError(f"model choked on {SECRET}")


@pytest.mark.parametrize("load", [
    _cannot_load,
    lambda: StubEngine(text_fails=ModelError("gliner/model.onnx is not installed; run `redactit setup-models` again")),
], ids=["engine cannot be built", "text warm-up fails"])
def test_text_detection_that_cannot_load_refuses_every_request(wire, load):
    w = wire(load)
    assert w.next(None)["state"] == "warming"
    announced = w.next(None)
    assert (announced["type"], announced["code"]) == ("error", "engine_unavailable")
    assert w.next(None)["state"] == "unavailable"
    for rid, kind in enumerate(PAYLOADS):
        w.request(f"r{rid}", kind, PAYLOADS[kind])
        answer = w.outcome(f"r{rid}")
        assert (answer["type"], answer["code"]) == ("error", "engine_unavailable")
    w.send({"type": "ping", "id": "p1"})
    assert w.next("p1")["state"] == "unavailable"
    assert not [m for m in w.received if m["type"] == "result"]


def test_ocr_that_cannot_load_still_serves_text_and_refuses_pdfs_and_images(wire):
    engine = StubEngine(images_fail=RuntimeError(f"OCR choked on {SECRET}"))
    w = wire(lambda: engine)
    assert [w.next(None)["state"] for _ in range(2)] == ["warming", "ready-text"]
    announced = w.next(None)
    assert (announced["type"], announced["code"]) == ("error", "engine_unavailable")
    for kind in PAYLOADS:
        w.request(kind, kind, PAYLOADS[kind])
    for kind in ("pdf", "image"):
        answer = w.outcome(kind)
        assert (answer["type"], answer["code"]) == ("error", "engine_unavailable")
    for kind in ("text", "txt"):
        answer = w.outcome(kind)
        assert answer["type"] == "result"
        parts = [w.next(kind) for _ in range(answer["total"])]
        assert base64.b64decode(parts[0]["data"]) == b"Call [PERSON_1]."
    w.send({"type": "ping", "id": "p1"})
    assert w.next("p1")["state"] == "ready-text"  # never ready-all


def test_each_requests_site_reaches_its_redaction(wire, monkeypatch):
    from redactit.formats import image, pdf

    sites = []
    monkeypatch.setattr(pdf, "redact_pdf",
                        lambda data, engine, scope, *, site, **kw: sites.append(("pdf", site)) or (b"%PDF", "## Page 1\n"))
    monkeypatch.setattr(image, "redact_image",
                        lambda data, engine, scope, *, site, **kw: sites.append(("image", site)) or (b"PNG", ".png", ""))
    engine = StubEngine()
    w = wire(lambda: engine)
    requests = [("text", "claude.ai"), ("txt", "chatgpt.com"), ("pdf", "gemini.google.com"), ("image", "claude.ai")]
    for rid, (kind, site) in enumerate(requests):
        w.request(f"s{rid}", kind, PAYLOADS[kind], site=site)
        assert w.outcome(f"s{rid}")["type"] == "result"
    assert engine.sites == ["claude.ai", "chatgpt.com"]
    assert sites == [("pdf", "gemini.google.com"), ("image", "claude.ai")]


def test_a_detection_timeout_is_its_own_error_and_returns_nothing(wire):
    w = wire(lambda: StubEngine(redact_raises=PatternTimeout("a detection pattern timed out; nothing was redacted")))
    w.request("t1", "text", PAYLOADS["text"])
    answer = w.outcome("t1")
    assert (answer["type"], answer["code"]) == ("error", "detection_timeout")
    assert answer["message"] == native.MESSAGES["detection_timeout"]
    w.send({"type": "ping", "id": "p1"})
    w.next("p1")  # answered after the request, so nothing more is coming for it
    assert not [m for m in w.received if m.get("id") == "t1" and m["type"] in ("result", "chunk")]


# --- Protocol 2: review signal and re-mapping ----------------------------------------------

@pytest.mark.parametrize(("policy_mode", "announced"), [("always", "always"),
                                                        ("low_confidence_only", "low_confidence")])
def test_status_carries_the_policys_review_mode_once_the_engine_loads(wire, policy_mode, announced):
    w = wire(lambda: StubEngine(review_mode=policy_mode))
    statuses = [w.next(None) for _ in range(3)]
    assert [(s["state"], s["review_mode"]) for s in statuses] == [
        ("warming", None), ("ready-text", announced), ("ready-all", announced)]
    assert all(s["protocol"] == 2 for s in statuses)
    w.send({"type": "ping", "id": "p1"})
    assert w.next("p1")["review_mode"] == announced


@pytest.mark.parametrize(("flags", "review"), [
    ((), {"needed": False, "count": 0}),
    ((False, False), {"needed": False, "count": 0}),
    ((True, False, True), {"needed": True, "count": 2}),
])
def test_a_result_counts_the_decisions_that_need_review(wire, flags, review):
    w = wire(lambda: StubEngine(flags=flags))
    w.request("t1", "text", PAYLOADS["text"])
    assert w.outcome("t1")["review"] == review


def test_a_files_review_count_covers_every_engine_call_the_format_makes(wire, monkeypatch):
    from redactit.formats import pdf

    def two_pages(data, engine, scope, *, site, **kw):
        for page in ("page one", "page two"):
            engine.redact(page, scope, site=site)  # as redact_pdf does per page and per image
        return b"%PDF", "## Page 1\n"

    monkeypatch.setattr(pdf, "redact_pdf", two_pages)
    w = wire(lambda: StubEngine(flags=(True, False)))
    w.request("f1", "pdf", PAYLOADS["pdf"])
    assert w.outcome("f1")["review"] == {"needed": True, "count": 2}
