"""PDF redaction: every page is rebuilt from pixels, so no text layer, metadata, form,
attachment or script from the input can survive into the output.

Sensitive text is found twice: in the text layer (exact character boxes) and by the image
pipeline on the rendered page (scanned pages, text inside embedded images, outlined fonts).
"""

from __future__ import annotations

import io

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from redactit.formats.image import Box, find_boxes, paint
from redactit.types import RedactitError

DPI = 200  # enough for OCR to read 8 pt text; the output keeps the page's physical size
MAX_PAGES = 500  # a crafted file stops here instead of rendering forever
LINE_TOLERANCE = 0.5  # chars whose vertical centres differ by less than this share of a height are one line


class PdfError(RedactitError, ValueError):
    pass


def redact_pdf(data: bytes, engine, scope: str, *, destination: str = "cli") -> tuple[bytes, str]:
    """(image-only PDF bytes, redacted Markdown of each page's text)."""
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise PdfError(f"unreadable or encrypted PDF ({type(e).__name__})") from e
    if len(pdf) > MAX_PAGES:
        raise PdfError(f"refusing a PDF with more than {MAX_PAGES} pages")
    pages, markdown = [], []
    for number, page in enumerate(pdf, 1):
        scale = DPI / 72
        image = page.render(scale=scale).to_pil().convert("RGB")
        layer_boxes, layer_text = _text_layer(page, engine, scope, scale, destination)
        pixel_boxes, ocr_text = find_boxes(image, engine, scope, file_type="pdf", destination=destination)
        pages.append(paint(image, layer_boxes + pixel_boxes))
        markdown.append(f"## Page {number}\n\n{layer_text.strip() or ocr_text.strip()}")
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=pages[1:], resolution=DPI)
    return out.getvalue(), "\n\n".join(markdown) + "\n"


def _text_layer(page, engine, scope: str, scale: float, destination: str) -> tuple[list[Box], str]:
    """Boxes over sensitive text-layer characters, and the redacted text of the layer."""
    textpage = page.get_textpage()
    count = textpage.count_chars()
    if count == 0:
        return [], ""
    # One string character per PDFium character, so a span maps straight back to char boxes.
    text = "".join(chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i) or 32) for i in range(count))
    result = engine.redact(text, scope, file_type="pdf", destination=destination)
    height = page.get_height()
    boxes = []
    for decision, replacement in zip(result.decisions, result.replacements):
        label = replacement if decision.action == "pseudonymize" else None
        for left, top, right, bottom in _line_rects(textpage, decision.span.start, decision.span.end):
            # PDF points (origin bottom-left) to raster pixels (origin top-left).
            x0, y0, x1, y1 = left * scale, (height - top) * scale, right * scale, (height - bottom) * scale
            boxes.append(Box(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), label))
    return boxes, result.text


def _line_rects(textpage, start: int, end: int) -> list[tuple[float, float, float, float]]:
    """Union of character boxes in [start, end), one rectangle per visual line."""
    rects: list[list[float]] = []
    for i in range(start, end):
        left, bottom, right, top = textpage.get_charbox(i, loose=True)
        if right <= left or top <= bottom:
            continue  # generated line breaks and spaces have no box
        last = rects[-1] if rects else None
        if last and abs((last[1] + last[3]) - (top + bottom)) / 2 < LINE_TOLERANCE * (top - bottom):
            last[0], last[1], last[2], last[3] = min(last[0], left), max(last[1], top), max(last[2], right), min(last[3], bottom)
        else:
            rects.append([left, top, right, bottom])
    return [tuple(r) for r in rects]
