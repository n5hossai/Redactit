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
