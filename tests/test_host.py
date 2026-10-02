"""Round trips through the native host: a real process, started the way Chrome starts it,
spoken to in Chrome's frames (docs/PLAN.md §7, THREAT_MODEL T8, T9 and T10).

One host serves most tests, in file order; tests that end a host start their own.
"""

import io
import json
import os
import re
import struct
import time
import zipfile

import pytest
from redactit import models

try:
    models.path_for("gliner/model.onnx")
    models.path_for("yunet/face_detection_yunet_2023mar.onnx")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

import hostkit  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from redactit.audit import AuditLog  # noqa: E402
from redactit.cli import effective_policy  # noqa: E402
from redactit.formats.docx import docx_to_markdown  # noqa: E402
from redactit.formats.image import redact_image  # noqa: E402
from redactit.formats.pdf import redact_pdf  # noqa: E402
from redactit.hosts import native  # noqa: E402
from redactit.pipeline import Engine  # noqa: E402
from redactit.vault import Vault  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

# Synthetic values; none of them may come back unredacted, in an error, on stderr or in the audit log.
NAME, EMAIL, CARD = "Priya Okafor", "priya.okafor@northwind.com", "4111 1111 1111 1111"
SECRETS = [NAME, "Okafor", EMAIL, CARD, CARD.replace(" ", "")]
PASTE = f"Please email {NAME} at {EMAIL}; she paid with card {CARD}."
LINES = [f"Patient: {NAME}", f"Email: {EMAIL}", f"Card: {CARD}"]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    h = hostkit.Host(tmp_path_factory.mktemp("host"))
    h.request("early", "text", PASTE.encode("utf-8"), scope="early")  # before the host can be ready
    yield h
    h.close()


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """The engine the host runs, called directly, to compare outputs with."""
    tmp = tmp_path_factory.mktemp("direct")
    with Vault(tmp / "vault.db", os.urandom(32)) as vault:
        yield Engine(effective_policy(), vault, AuditLog(tmp / "audit.jsonl"))


def _big_pdf() -> bytes:
    """One page of values above a photo-like noise image: about 3 MB, so 8 chunks in."""
    rng = np.random.default_rng(7)
    noise = Image.fromarray((205 + rng.normal(0, 22, (1500, 1800, 3))).clip(0, 255).astype("uint8"))
    jpeg, out = io.BytesIO(), io.BytesIO()
    noise.save(jpeg, "JPEG", quality=97)
    c = canvas.Canvas(out, pagesize=letter)
    c.setFont("Helvetica", 12)
    for i, line in enumerate(LINES):
        c.drawString(72, 720 - 20 * i, line)
    c.drawImage(ImageReader(io.BytesIO(jpeg.getvalue())), 72, 72, width=468, height=420)
    c.save()
    return out.getvalue()


def _big_image() -> bytes:
    """A 1700x1300 PNG whose noise does not compress: about 5 MB each way."""
    rng = np.random.default_rng(11)
    img = Image.fromarray((215 + rng.normal(0, 18, (1300, 1700, 3))).clip(0, 255).astype("uint8"))
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=36)
    for i, line in enumerate(LINES):
        draw.rectangle((90, 90 + 80 * i, 1100, 150 + 80 * i), fill="white")
        draw.text((100, 100 + 80 * i), line, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _small_pages(n: int) -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(216, 72))
    for i in range(n):
        c.setFont("Helvetica", 12)
        c.drawString(12, 30, f"Page {i + 1}: {NAME}")
        c.showPage()
    c.save()
    return out.getvalue()


def _docx(text: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{text}</w:t>'
                                        "</w:r></w:p></w:body></w:document>")
    return out.getvalue()


def _without_save_stamps(pdf: bytes) -> bytes:
    # PDFium stamps every save with the time and a random trailer /ID, so two direct calls
    # differ there too; nothing else may.
    pdf = re.sub(rb"/CreationDate\(D:\d{14}[^)]*\)", b"/CreationDate()", pdf)
    return re.sub(rb"/ID\[<[0-9A-F]+><[0-9A-F]+>\]", b"/ID[]", pdf)


# --- lifecycle --------------------------------------------------------------------------

def test_warming_then_ready_and_an_early_request_waits(host, engine):
    out = host.outcome("early")
    host.wait_status("ready-all")
    statuses = [m for m in host.messages if m["type"] == "status" and m["id"] is None]
    assert [s["state"] for s in statuses] == ["warming", "ready-text", "ready-all"]
    assert statuses[1]["_n"] < out["result"]["_n"]  # answered after text detection was ready
    assert out["parts"]["text"].decode("utf-8") == engine.redact(PASTE, "early").text


def test_ping_reports_the_state_and_protocol(host):
    status = host.ping("p1")
    assert (status["type"], status["state"], status["protocol"]) == ("status", "ready-all", native.PROTOCOL)
    assert status["review_mode"] == native.REVIEW_MODES[effective_policy().review.mode]


def test_a_result_reports_how_many_decisions_need_review(host, engine):
    host.request("rv", "text", PASTE.encode("utf-8"), scope="review")
    expected = sum(d.needs_review for d in engine.redact(PASTE, "review").decisions)
    assert host.outcome("rv")["result"]["review"] == {"needed": expected > 0, "count": expected}


# --- round trips ------------------------------------------------------------------------

def test_text_round_trip_matches_the_engine(host, engine):
    host.request("t1", "text", PASTE.encode("utf-8"), scope="round-trip")
    out = host.outcome("t1")
    text = out["parts"]["text"].decode("utf-8")
    assert text == engine.redact(PASTE, "round-trip").text and NAME not in text
    assert out["result"]["parts"] == [{"name": "text", "media_type": "text/plain", "size": len(text.encode())}]
    assert [p["stage"] for p in out["progress"]] == ["queued", "redacting"]


@pytest.mark.parametrize(("kind", "data", "media"), [
    ("md", f"# Notes\n\n{PASTE}\n".encode("utf-8"), "text/markdown"),
    ("docx", _docx(PASTE), "text/markdown"),
], ids=["md", "docx"])
def test_text_files_round_trip(host, engine, kind, data, media):
    host.request(f"f-{kind}", kind, data, scope=f"file-{kind}")
    out = host.outcome(f"f-{kind}")
    expected = engine.redact(docx_to_markdown(data) if kind == "docx" else data.decode("utf-8"), f"file-{kind}").text
    assert out["parts"]["text"].decode("utf-8") == expected and EMAIL not in expected
    assert out["result"]["parts"][0]["media_type"] == media


def test_a_multi_megabyte_pdf_is_chunked_both_ways_and_matches_the_engine(host, engine):
    data = _big_pdf()
    assert native.chunk_count(len(data)) >= 6
    host.request("pdf", "pdf", data, scope="pdf")
    out = host.outcome("pdf")
    pdf, markdown = redact_pdf(data, engine, "pdf")
    assert out["result"]["total"] >= 2  # more than one chunk back
    assert _without_save_stamps(out["parts"]["file"]) == _without_save_stamps(pdf)
    assert out["parts"]["text"] == markdown.encode("utf-8")
    assert NAME not in markdown and "[PERSON_1]" in markdown
    assert [(p.get("page"), p.get("pages")) for p in out["progress"]] == [(None, None), (None, None), (1, 1)]


def test_a_large_image_is_chunked_both_ways_and_matches_the_engine(host, engine):
    data = _big_image()
    host.request("img", "image", data, scope="img")
    out = host.outcome("img")
    image, suffix, _ = redact_image(data, engine, "img")
    assert native.chunk_count(len(data)) >= 10 and out["result"]["total"] >= 10
    assert out["parts"]["file"] == image and suffix == ".png"
    assert out["result"]["parts"] == [{"name": "file", "media_type": "image/png", "size": len(image)}]


def test_requests_are_answered_in_arrival_order(host):
    ids = ["q1", "q2", "q3"]
    host.request("q0", "image", _big_image(), scope="queue")  # slow, and first in line
    for rid in ids:
        host.request(rid, "text", f"Note {rid} for {NAME}.".encode("utf-8"), scope="queue")
    answers = [host.outcome(rid) for rid in ["q0", *ids]]
    order = [a["result"]["_n"] for a in answers]
    assert order == sorted(order)


# --- cancel -----------------------------------------------------------------------------

def test_cancel_while_running_and_while_queued(host):
    pages = 8
    host.request("long", "pdf", _small_pages(pages), scope="cancel")
    host.request("behind", "text", PASTE.encode("utf-8"), scope="cancel")
    while (msg := host.next("long"))["type"] != "progress" or "page" not in msg:
        pass  # the first page has started
    host.send({"type": "cancel", "id": "behind"})
    assert host.error("behind")["code"] == "cancelled"  # straight away, while the PDF still runs
    host.send({"type": "cancel", "id": "long"})
    out = host.outcome("long")
    assert out["error"]["code"] == "cancelled"
    assert len([p for p in out["progress"] if "page" in p]) < pages - 1  # stopped between pages


def test_cancel_while_uploading_drops_the_rest(host):
    data = _big_pdf()
    host.request("up", "pdf", data, chunks=2)
    host.send({"type": "cancel", "id": "up"})
    assert host.error("up")["code"] == "cancelled"
    total = native.chunk_count(len(data))
    for seq in range(2, total):  # chunks already in flight when the cancel was sent
        host.send({"type": "chunk", "id": "up", "seq": seq, "total": total, "data": ""})
    assert host.ping("after-cancel")["type"] == "status"
    assert not [m for m in host.messages if m.get("id") == "up" and m.get("code") != "cancelled"]


# --- malformed input (T9) ---------------------------------------------------------------

@pytest.mark.parametrize(("raw", "rid", "code"), [
    (hostkit.frame(b"{not json " + NAME.encode()), None, "bad_json"),
    (hostkit.frame(b""), None, "bad_json"),  # a zero length
    (hostkit.frame(json.dumps({"type": "exfiltrate", "id": "u1", "text": NAME}).encode()), "u1", "bad_message"),
    (hostkit.frame(json.dumps({"type": "ping", "id": "u2", "note": NAME}).encode()), "u2", "bad_message"),
    (hostkit.frame(b"x" * (native.MAX_FRAME + 1)), None, "message_too_large"),
], ids=["bad-json", "zero-length", "unknown-type", "unknown-field", "oversized"])
def test_malformed_frames_get_a_clean_error_and_the_host_carries_on(host, raw, rid, code):
    host.send_raw(raw)
    error = host.error(rid)
    assert error["code"] == code and NAME not in error["message"]
    assert host.ping("alive")["state"] == "ready-all"


def test_a_wrong_sequence_number_drops_the_request(host):
    data = b"x" * (native.RAW_CHUNK + 10)
    host.request("seq", "text", data, chunks=1)
    host.send({"type": "chunk", "id": "seq", "seq": 5, "total": 2, "data": ""})
    assert host.error("seq")["code"] == "bad_sequence"
    host.send({"type": "chunk", "id": "seq", "seq": 1, "total": 2, "data": ""})  # ignored now
    assert host.ping("alive-seq")["type"] == "status"


@pytest.mark.parametrize(("msg", "code"), [
    ({"type": "chunk", "id": "nobody", "seq": 0, "total": 1, "data": ""}, "unknown_request"),
    ({"type": "cancel", "id": "nobody"}, "unknown_request"),
    ({"type": "redact_file", "id": "big", "scope": "s", "site": "claude.ai", "kind": "pdf",
      "size": native.MAX_PAYLOAD + 1, "total": native.chunk_count(native.MAX_PAYLOAD + 1)}, "too_large"),
])
def test_requests_the_host_cannot_serve_are_refused(host, msg, code):
    host.send(msg)
    assert host.error(msg["id"])["code"] == code


def test_too_many_requests_in_flight_are_refused_and_nothing_is_lost(host):
    rids = [f"open{i}" for i in range(native.MAX_IN_FLIGHT)]
    for rid in rids:  # uploads started and never finished
        host.request(rid, "text", b"x" * 10, chunks=0)
    host.request("one-too-many", "text", b"x" * 10, chunks=0)
    assert host.error("one-too-many")["code"] == "busy"
    host.request(rids[0], "text", b"x" * 10, chunks=0)
    assert host.error(rids[0])["code"] == "duplicate_request"
    for rid in rids:
        host.send({"type": "cancel", "id": rid})
        assert host.error(rid)["code"] == "cancelled"
    host.request("room-again", "text", PASTE.encode("utf-8"))
    assert "result" in host.outcome("room-again")


def test_nothing_sent_leaks_into_errors_stderr_or_the_audit_log(host):
    """Runs last on the shared host: every value sent above, in requests and malformed
    frames alike, is checked against everything the host wrote."""
    host.request("last", "text", PASTE.encode("utf-8"))
    host.outcome("last")
    assert host.close() == 0  # stdin closed, as on disconnect: a clean exit
    errors = json.dumps([m for m in host.messages if m["type"] in ("error", "status", "progress")])
    stderr, audit = host.stderr(), host.audit_path.read_text(encoding="utf-8")
    for where, text in {"errors": errors, "stderr": stderr, "audit": audit}.items():
        assert not [s for s in SECRETS if s in text], where
    assert "redaction" in audit and "model_verified" in audit


# --- hosts of their own -----------------------------------------------------------------

def test_a_caller_other_than_the_allowed_extension_is_refused(tmp_path):
    h = hostkit.Host(tmp_path, origin="chrome-extension://" + "p" * 32 + "/")
    assert h.wait_exit(timeout=30) == 1
    assert [m["code"] for m in h.messages] == ["origin_refused"]
    assert not h.audit_path.exists()  # refused before the engine loaded


def test_an_unfilled_manifest_template_serves_nobody(tmp_path):
    h = hostkit.Host(tmp_path, manifest=hostkit.REPO / "installers" / "host-manifest.json")
    assert h.wait_exit(timeout=30) == 1
    assert [m["code"] for m in h.messages] == ["bad_config"]


def test_an_impossible_length_closes_the_host_cleanly(tmp_path):
    h = hostkit.Host(tmp_path)
    h.send_raw(struct.pack("=I", 0xFFFFFFFF) + b"{}")
    assert h.wait_exit(timeout=60) == 1
    assert [m.get("code") for m in h.messages if m["type"] == "error"] == ["bad_frame"]
    assert "Traceback" not in h.stderr()


def test_the_host_exits_after_the_idle_timeout_but_not_while_warming(tmp_path):
    h = hostkit.Host(tmp_path, idle_seconds=1)  # far shorter than warming up takes
    h.wait_status("ready-all")
    ready = time.monotonic()
    assert h.wait_exit(timeout=30) == 0
    assert time.monotonic() - ready < 10
