"""Images: sensitive OCR text, faces and barcodes are filled on a copy of the pixels, which
are then re-encoded from scratch so no metadata can carry over (docs/PLAN.md section 5)."""

from __future__ import annotations

import io
import math
from bisect import bisect_right
from dataclasses import dataclass
from functools import cache
from itertools import accumulate

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from redactit import models, ocr
from redactit.types import RedactitError

MAX_PIXELS = 50_000_000  # decompression bombs: a small file can decode to gigabytes
YUNET = "yunet/face_detection_yunet_2023mar.onnx"
FACE_SCORE = 0.5  # below YuNet's 0.9 default: a missed face leaks, a spurious box costs nothing
FACE_MAX_SIDE = 3200  # YuNet needs ~70 bytes per pixel, so larger photos are shrunk for detection only
PAD = 0.15  # share of the box height added on every side, so glyph edges never peek out
MIN_LABEL_PX = 8  # smaller than this the label is unreadable, so the box stays plain

Quad = ocr.Quad


class ImageError(RedactitError, ValueError):
    pass


@dataclass(frozen=True)
class Box:
    quad: Quad
    label: str | None  # replacement to print on the box, e.g. "[PERSON_1]"; None = plain fill


def find_boxes(img: Image.Image, engine, scope: str, *, covered: list[Box] = (), file_type: str = "png",
               destination: str = "cli") -> tuple[list[Box], str]:
    """Boxes to fill (sensitive OCR text, faces, barcodes) and the redacted OCR text.

    `covered` are boxes the caller fills anyway (a PDF's text-layer finds). OCR characters
    under them are masked before detection, so the text never shows what the picture hides:
    OCR once read a boxed birth date as "Dateof birth:1951-04-07", and without its cue the
    date was not found again.
    """
    rgb = img if img.mode == "RGB" else img.convert("RGB")  # no copy of a 50 MP photo
    lines = [_mask(line, covered) for line in ocr.read_lines(rgb)] if covered else ocr.read_lines(rgb)
    boxes, redacted = [], ""
    if lines:
        text = "\n".join(line.text for line in lines)
        result = engine.redact(text, scope, file_type=file_type, destination=destination)
        starts = list(accumulate((len(line.text) + 1 for line in lines), initial=0))
        for d, replacement in zip(result.decisions, result.replacements):
            label = replacement if d.action == "pseudonymize" else None
            i = bisect_right(starts, d.span.start) - 1
            while i < len(lines) and starts[i] < d.span.end:  # a span over two lines gets a box per line
                lo, hi = max(d.span.start - starts[i], 0), min(d.span.end - starts[i], len(lines[i].text))
                if lo < hi:
                    boxes.append(Box(ocr.char_quad(lines[i], lo, hi), label))
                i += 1
        redacted = result.text
    faces = _faces(rgb) if _enabled(engine, "FACE") else []
    codes = _barcodes(rgb) if _enabled(engine, "BARCODE") else []
    if engine.audit and (faces or codes):  # engine.redact audited the text; these have no text
        engine.audit.write("redaction", file_type=file_type, destination=destination,
                           dial=engine.policy.effective_dial(),
                           entity_counts={"FACE": len(faces), "BARCODE": len(codes)}, decisions=[])
    return boxes + faces + codes, redacted


def paint(img: Image.Image, boxes: list[Box]) -> Image.Image:
    """A new RGB image with every box filled black (labelled in white when it has a label)."""
    out = img.convert("RGB")  # a copy: the input is never touched
    draw = ImageDraw.Draw(out)
    for box in boxes:
        quad = _grow(box.quad)
        draw.polygon(quad, fill=(0, 0, 0))
        if box.label:
            _write_label(out, quad, box.label)
    return out


def redact_image(data: bytes, engine, scope: str, *, destination: str = "cli") -> tuple[bytes, str, str]:
    """(re-encoded image bytes, output suffix like ".png", redacted OCR text)"""
    img, fmt = _load(data)
    suffix = ".jpg" if fmt == "JPEG" else ".png"
    boxes, text = find_boxes(img, engine, scope, file_type=suffix[1:], destination=destination)
    painted = paint(img, boxes)
    # Raw pixels only: nothing of the input's EXIF, GPS, ICC, XMP or text chunks can reach the encoder.
    clean = Image.frombytes("RGB", painted.size, painted.tobytes())
    out = io.BytesIO()
    clean.save(out, fmt, **({"quality": 90} if fmt == "JPEG" else {}))
    return out.getvalue(), suffix, text


def _load(data: bytes) -> tuple[Image.Image, str]:
    """Decode with the EXIF orientation applied, as RGB, plus the format to write it back in."""
    try:
        # An allowlist, not content sniffing alone: EPS would run Ghostscript (AGPL) through a
        # temp file, and WMF/EMF render through the OS. JPEG also opens phones' MPO files.
        Image.init()  # registers WEBP and TIFF, which the allowlist lookup needs
        img = Image.open(io.BytesIO(data), formats=("PNG", "JPEG", "WEBP", "BMP", "GIF", "TIFF"))
    except (OSError, Image.DecompressionBombError):
        raise ImageError("the file is not a readable image") from None
    if img.width * img.height > MAX_PIXELS:
        raise ImageError(f"the image is {img.width}x{img.height}; the limit is {MAX_PIXELS // 1_000_000} megapixels")
    fmt = "JPEG" if img.format in ("JPEG", "MPO") else "PNG"  # MPO is a phone's multi-picture JPEG
    try:
        img = ImageOps.exif_transpose(img)
        if "A" in img.getbands() or "transparency" in img.info:  # show what a viewer sees, not the hidden colour
            rgba = img.convert("RGBA")
            img = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        return (img if img.mode == "RGB" else img.convert("RGB")), fmt
    except (OSError, ValueError, SyntaxError):
        raise ImageError("the file is not a readable image") from None


def _mask(line: ocr.Line, boxes: list[Box]) -> ocr.Line:
    """`line` with the characters under each (padded) box replaced by the box's label, or by
    "█" when it has none. Each replacement is spread over the width it replaces, so the
    characters after it keep their positions for the boxes found in this text later."""
    quads, text, n = [_grow(b.quad) for b in boxes], line.text, len(line.text)

    def owner(i: int) -> int | None:
        q = ocr.char_quad(line, i, i + 1)
        centre = sum(p[0] for p in q) / 4, sum(p[1] for p in q) / 4
        return next((k for k, quad in enumerate(quads) if ocr._inside(centre, quad)), None)

    owners = [None if c.isspace() else owner(i) for i, c in enumerate(text)]
    for i in range(1, n - 1):  # a space inside one box is part of it: "Priya Okafor" gets one label
        if owners[i] is None and text[i].isspace():
            after = next((o for o, c in zip(owners[i + 1:], text[i + 1:]) if not c.isspace()), None)
            if owners[i - 1] is not None and owners[i - 1] == after:
                owners[i] = after
    if not any(o is not None for o in owners):
        return line
    cuts = line.cuts if len(line.cuts) == n + 1 else [i / max(n, 1) for i in range(n + 1)]
    out, out_cuts, i = [], [cuts[0]], 0
    while i < n:
        j = i + 1
        while owners[i] is not None and j < n and owners[j] == owners[i]:
            j += 1
        piece = text[i] if owners[i] is None else (boxes[owners[i]].label or "█" * (j - i))
        out.append(piece)
        out_cuts += [cuts[i] + (cuts[j] - cuts[i]) * (t + 1) / len(piece) for t in range(len(piece))]
        i = j
    return ocr.Line("".join(out), line.quad, tuple(out_cuts))


def _enabled(engine, entity_type: str) -> bool:
    cfg = engine.policy.entities.get(entity_type)
    return cfg is not None and (cfg.locked or cfg.enabled)


def _rect(x: float, y: float, w: float, h: float) -> Quad:
    return (x, y), (x + w, y), (x + w, y + h), (x, y + h)


def _faces(rgb: Image.Image) -> list[Box]:
    import cv2

    # Errors only: OpenCV 5 warns on every create() that DNN targets are ignored.
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    # Built per call, not cached: its buffers grow with the input and a 50 MP photo would keep gigabytes.
    scale = min(1.0, FACE_MAX_SIDE / max(rgb.size))
    small = rgb if scale == 1 else rgb.resize((round(rgb.width * scale), round(rgb.height * scale)))
    detector = cv2.FaceDetectorYN.create(str(models.path_for(YUNET)), "", small.size, FACE_SCORE)
    _, faces = detector.detect(np.ascontiguousarray(np.asarray(small)[..., ::-1]))  # OpenCV expects BGR
    return [Box(_rect(x / scale, y / scale, w / scale, h / scale), None) for x, y, w, h, *_ in
            (faces if faces is not None else [])]


def _barcodes(rgb: Image.Image) -> list[Box]:
    import zxingcpp

    corners = ("top_left", "top_right", "bottom_right", "bottom_left")
    return [Box(tuple((getattr(c.position, k).x, getattr(c.position, k).y) for k in corners), None)
            for c in zxingcpp.read_barcodes(rgb)]


def _grow(quad: Quad) -> Quad:
    """Move every side outwards by PAD of the box's shorter side (the text height), along the
    box's own axes. Not always p0 to p3: a text-layer box on a sideways page is upright in
    pixels, so that side is the line's length, and a padding from it covered the next lines."""
    p0, p1, p2, p3 = quad
    pad = PAD * min(math.dist(p0, p1), math.dist(p0, p3))

    def unit(p, q):
        d = math.dist(p, q) or 1.0
        return (q[0] - p[0]) / d * pad, (q[1] - p[1]) / d * pad

    (ux, uy), (vx, vy) = unit(p0, p1), unit(p0, p3)
    return ((p0[0] - ux - vx, p0[1] - uy - vy), (p1[0] + ux - vx, p1[1] + uy - vy),
            (p2[0] + ux + vx, p2[1] + uy + vy), (p3[0] - ux + vx, p3[1] - uy + vy))


@cache
def _font(size: int):
    return ImageFont.load_default(size=size)


def _write_label(canvas: Image.Image, quad: Quad, text: str) -> None:
    """White text fitted inside the box and turned to follow its reading direction."""
    (x0, y0), (x1, y1), *_ = quad
    w, h = math.dist(quad[0], quad[1]), math.dist(quad[0], quad[3])
    size = int(h * 0.75)
    while size >= MIN_LABEL_PX and _font(size).getlength(text) > w * 0.95:
        size -= 1
    if size < MIN_LABEL_PX:
        return
    tile = Image.new("L", (math.ceil(w), math.ceil(h)))
    ImageDraw.Draw(tile).text((w / 2, h / 2), text, fill=255, font=_font(size), anchor="mm")
    tile = tile.rotate(-math.degrees(math.atan2(y1 - y0, x1 - x0)), expand=True)
    cx, cy = sum(p[0] for p in quad) / 4, sum(p[1] for p in quad) / 4
    canvas.paste((255, 255, 255), (round(cx - tile.width / 2), round(cy - tile.height / 2)), tile)
