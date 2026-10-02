"""PDFs come back as pixels only: no text layer, no values, the page text as redacted Markdown."""

import io
import os

import pytest
from redactit import models

try:
    models.path_for("gliner/model.onnx")
    models.path_for("yunet/face_detection_yunet_2023mar.onnx")
except (models.ModelError, KeyError):
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

import pypdfium2 as pdfium  # noqa: E402
import run as leak  # noqa: E402
from redactit.formats import pdf as pdf_format  # noqa: E402
from redactit.formats.pdf import PdfError, redact_pdf  # noqa: E402
from redactit.pipeline import Engine  # noqa: E402
from redactit.policy import load_policy  # noqa: E402
from redactit.vault import Vault  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

VALUES = ["Priya Okafor", "priya.okafor@corp.local", "4111 1111 1111 1111"]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with Vault(tmp_path_factory.mktemp("pdf") / "v.db", os.urandom(32)) as vault:
        yield Engine(load_policy(None), vault)


def _pdf(pages: list[list[str]]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setAuthor("Priya Okafor")  # document metadata must not survive either
    for lines in pages:
        for i, line in enumerate(lines):
            c.drawString(72, 720 - 24 * i, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def test_a_digital_pdf_comes_back_as_pixels_without_values(engine):
    data = _pdf([["Contact Priya Okafor at priya.okafor@corp.local.", "Card on file: 4111 1111 1111 1111"]])
    out, markdown = redact_pdf(data, engine, scope="t")
    doc = pdfium.PdfDocument(out)
    assert "".join(p.get_textpage().get_text_range() for p in doc).strip() == ""
    assert not {k: v for k, v in doc.get_metadata_dict().items() if v and k not in leak.HARMLESS_PDF_META}
    seen = leak.normalize(leak._ocr(doc[0].render(scale=300 / 72).to_pil(), rotations=(0,)))
    assert not [v for v in VALUES if leak.normalize(v) in seen]
    assert "[PERSON_1]" in markdown and not [v for v in VALUES if v in markdown]


def test_unreadable_input_is_refused(engine):
    with pytest.raises(PdfError):
        redact_pdf(b"%PDF-1.7 truncated", engine, scope="t")


def test_page_count_is_capped(engine, monkeypatch):
    monkeypatch.setattr(pdf_format, "MAX_PAGES", 1)
    with pytest.raises(PdfError, match="pages"):
        redact_pdf(_pdf([["one"], ["two"]]), engine, scope="t")


def _page(draw, rotation=0, cropbox=None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    if rotation:
        c.setPageRotation(rotation)
    if cropbox:
        c.setCropBox(cropbox)
    draw(c)
    c.showPage()
    c.save()
    return buf.getvalue()


def _visible(out: bytes) -> str:
    doc = pdfium.PdfDocument(out)
    return leak.normalize(" ".join(leak._ocr(p.render(scale=300 / 72).to_pil(), rotations=(0, 90, 270)) for p in doc))


@pytest.mark.parametrize("rotation, cropbox", [(90, None), (270, None), (0, (100, 100, 500, 800))],
                         ids=["rotated 90", "rotated 270", "cropped"])
def test_boxes_land_on_the_text_of_rotated_and_cropped_pages(engine, rotation, cropbox):
    def draw(c):
        c.setFont("Helvetica", 11)
        c.drawString(150, 500, "Contact Priya Okafor at priya.okafor@corp.local")
    out, _ = redact_pdf(_page(draw, rotation, cropbox), engine, scope="t")
    seen = _visible(out)
    assert "okafor" not in seen and "priya" not in seen


def test_text_hidden_under_a_drawn_box_does_not_reach_the_markdown(engine):
    def draw(c):
        c.drawString(72, 700, "Case notes for review")
        c.drawString(72, 650, "Secret client Dmitri Volkov")
        c.rect(60, 640, 400, 25, fill=1)  # a cosmetic "redaction" drawn over the text
    _, markdown = redact_pdf(_page(draw), engine, scope="t")
    assert "Volkov" not in markdown and "Case notes" in markdown


def test_oversized_pages_are_refused(engine):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(9000, 9000))  # ~780 MP at 200 DPI, from a tiny file
    c.drawString(72, 72, "x")
    c.save()
    with pytest.raises(PdfError, match="too large"):
        redact_pdf(buf.getvalue(), engine, scope="t")


def test_a_sites_own_rules_reach_the_text_layer_and_ocr(monkeypatch):
    """`site` must reach the text layer's detection, OCR's dial and the OCR text's detection."""
    from types import SimpleNamespace

    from redactit import ocr
    from redactit.pipeline import Result

    seen = []
    engine = SimpleNamespace(
        policy=SimpleNamespace(entities={}, effective_dial=lambda site=None: seen.append(("dial", site)) or 3),
        audit=None,
        redact=lambda text, scope, **kw: seen.append((kw["file_type"], kw["site"])) or Result(text, [], []),
    )
    line = ocr.Line("Contact", ((0, 0), (70, 0), (70, 10), (0, 10)))
    monkeypatch.setattr(ocr, "read_lines", lambda img, dial: [line])
    redact_pdf(_pdf([["Contact Priya Okafor."]]), engine, scope="t", site="chatgpt.com")
    assert seen == [("pdf", "chatgpt.com"), ("dial", "chatgpt.com"), ("pdf", "chatgpt.com")]


def test_the_redacted_pdf_does_not_say_when_it_was_redacted(engine):
    out, _ = redact_pdf(_pdf([["Nothing sensitive here."]]), engine, scope="t")
    assert pdfium.PdfDocument(out).get_metadata_dict().get("CreationDate") == "D:19700101000000+00'00'"
