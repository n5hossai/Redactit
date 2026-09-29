"""PDF redaction: every page is rebuilt from pixels, so no text layer, metadata, form,
attachment or script from the input can survive into the output.

Sensitive text is found twice: in the text layer (exact character boxes) and by the image
pipeline on the rendered page (scanned pages, text inside embedded images, outlined fonts).
"""

from __future__ import annotations

import ctypes
import io

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from redactit.formats.image import Box, find_boxes, paint
from redactit.types import RedactitError

DPI = 200  # enough for OCR to read 8 pt text; the output keeps the page's physical size
MAX_PAGES = 500  # a crafted file stops here instead of rendering forever
# A1 at 200 DPI is ~31 MP. A 1 KB file can declare a 5000 pt page (193 MP, ~580 MB), so pages
# past this are refused rather than rendered.
MAX_PAGE_PIXELS = 64_000_000
LINE_TOLERANCE = 0.5  # chars whose vertical centres differ by less than this share of a height are one line
JPEG_QUALITY = 90


class PdfError(RedactitError, ValueError):
    pass


def redact_pdf(data: bytes, engine, scope: str, *, destination: str = "cli") -> tuple[bytes, str]:
    """(image-only PDF bytes, redacted Markdown of the text visible on each page)."""
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise PdfError(f"unreadable or encrypted PDF ({type(e).__name__})") from e
    if len(pdf) > MAX_PAGES:
        raise PdfError(f"refusing a PDF with more than {MAX_PAGES} pages")
    out, markdown = pdfium.PdfDocument.new(), []
    for number, page in enumerate(pdf, 1):
        width, height = page.get_size()  # points, after the page's own rotation
        scale = DPI / 72
        if width * height * scale * scale > MAX_PAGE_PIXELS:
            raise PdfError(f"page {number} is too large to render safely")
        image = page.render(scale=scale).to_pil().convert("RGB")
        layer_boxes = _text_layer(page, image.size, engine, scope, destination)
        pixel_boxes, visible_text = find_boxes(image, engine, scope, file_type="pdf", destination=destination)
        _append_page(out, paint(image, layer_boxes + pixel_boxes), width, height)
        # The Markdown comes from what the page shows, not the text layer: text under a drawn
        # box or in white on white is invisible on the page and must not reappear here.
        markdown.append(f"## Page {number}\n\n{visible_text.strip()}")
    buffer = io.BytesIO()
    out.save(buffer)
    return buffer.getvalue(), "\n\n".join(markdown) + "\n"


def _append_page(doc, image, width: float, height: float) -> None:
    """Add one image-only page. Pages are stored as JPEG as they are made, so a long document
    never holds every decoded page in memory at once."""
    jpeg = io.BytesIO()
    image.save(jpeg, "JPEG", quality=JPEG_QUALITY)
    jpeg.seek(0)
    page = doc.new_page(width, height)
    obj = pdfium.PdfImage.new(doc)
    obj.load_jpeg(jpeg, inline=False)
    obj.set_matrix(pdfium.PdfMatrix().scale(width, height))
    page.insert_obj(obj)
    page.gen_content()


def _text_layer(page, size: tuple[int, int], engine, scope: str, destination: str) -> list[Box]:
    """Boxes over sensitive text-layer characters, in rendered-page pixels."""
    textpage = page.get_textpage()
    count = textpage.count_chars()
    if count == 0:
        return []
    # One string character per PDFium character, so a span maps straight back to char boxes.
    # Emoji arrive as surrogate halves, which spaCy cannot encode; U+FFFD keeps the alignment.
    codes = (pdfium_c.FPDFText_GetUnicode(textpage.raw, i) for i in range(count))
    text = "".join("�" if 0xD800 <= c <= 0xDFFF else chr(c or 32) for c in codes)
    result = engine.redact(text, scope, file_type="pdf", destination=destination)
    to_pixels = _page_to_pixels(page, size)
    boxes = []
    for decision, replacement in zip(result.decisions, result.replacements):
        label = replacement if decision.action == "pseudonymize" else None
        for left, top, right, bottom in _line_rects(textpage, decision.span.start, decision.span.end):
            corners = [to_pixels(x, y) for x in (left, right) for y in (top, bottom)]
            xs, ys = [p[0] for p in corners], [p[1] for p in corners]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            boxes.append(Box(((x0, y0), (x1, y0), (x1, y1), (x0, y1)), label))
    return boxes


def _page_to_pixels(page, size: tuple[int, int]):
    """PDF user space to rendered pixels through PDFium itself, so /Rotate, the CropBox origin
    and any page transform are applied exactly as in the render. A hand-written flip once put
    every box on a rotated page onto empty paper, leaving the text visible."""
    width, height = size

    def convert(x: float, y: float) -> tuple[int, int]:
        dx, dy = ctypes.c_int(), ctypes.c_int()
        pdfium_c.FPDF_PageToDevice(page.raw, 0, 0, width, height, 0, x, y, ctypes.byref(dx), ctypes.byref(dy))
        return dx.value, dy.value

    return convert


def _line_rects(textpage, start: int, end: int) -> list[tuple[float, float, float, float]]:
    """Union of character boxes in [start, end), one rectangle per visual line (page space)."""
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
