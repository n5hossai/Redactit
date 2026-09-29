"""OCR text lines with pixel quads, read at four orientations and two scales, then merged."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from itertools import accumulate

import numpy as np
from PIL import Image, ImageDraw

Quad = tuple[tuple[float, float], ...]

TURNS = (1, 2, 3)  # quarter turns tried after the upright read, so text at any orientation reads upright once
# RapidOCR shrinks anything longer than MAX_SIDE before reading it: at 2000 px a 4K screenshot's
# 14 px text became unreadable and survived. Images past MAX_SIDE are read in overlapping tiles.
MAX_SIDE, TILE_OVERLAP = 4096, 512
UPSCALE, UPSCALE_SIDE = 2.0, 2000  # the upscale pass is for small images only; it stops at UPSCALE_SIDE
MIN_UPSCALE = 1.25  # a smaller gain is not worth another pass
CONFIDENT = 0.9  # a read this sure of itself is not read again at another orientation or scale
ASPECT = 4  # RapidOCR scales a thin image to a 736 px short side: slow at 8:1, and its resize fails near 100:1


@dataclass(frozen=True)
class Line:
    text: str
    quad: Quad  # 4 corners in image pixels, reading order: TL, TR, BR, BL
    # Where each character starts along the line, as len(text) + 1 fractions of its length.
    # Without them characters are assumed evenly spaced, which misses glyph edges in
    # proportional fonts and wherever the recognizer dropped a space.
    cuts: tuple[float, ...] = ()


@cache
def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(max_side_len=MAX_SIDE)


def read_lines(img: Image.Image) -> list[Line]:
    """Every text line in the image, including text turned 90, 180 or 270 degrees and small text."""
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    found = [(score, _shift(line, x, y)) for (x, y), tile in _tiles(rgb) for score, line in _read_view(tile)]
    return _reading_order(_merge(found))


def _tiles(img: Image.Image):
    """(offset, crop) pieces no longer than MAX_SIDE, overlapping so a line cut by one edge is whole in the next."""
    step = MAX_SIDE - TILE_OVERLAP
    starts = lambda size: sorted({*range(0, max(size - MAX_SIDE, 0), step), max(size - MAX_SIDE, 0)})  # noqa: E731
    for y in starts(img.height):
        for x in starts(img.width):
            yield (x, y), img.crop((x, y, min(x + MAX_SIDE, img.width), min(y + MAX_SIDE, img.height)))


def _shift(line: Line, dx: float, dy: float) -> Line:
    return Line(line.text, tuple((x + dx, y + dy) for x, y in line.quad), line.cuts)


def _read_view(rgb: Image.Image) -> list[tuple[float, Line]]:
    found = _read(rgb, 0, 1.0)
    # The other passes only need what this one could not read, so upright lines it read with
    # confidence are painted out: reading every line again at every orientation and scale is
    # most of the time. Turned lines stay, because a wrong read of one can look confident.
    sure = [line for score, line in found if score >= CONFIDENT and len(line.text) >= 3 and _upright(line)]
    left = _erase(rgb, sure)
    found += [line for turns in TURNS for line in _read(left, turns, 1.0)]
    scale = min(UPSCALE, UPSCALE_SIDE / max(rgb.size))
    if scale >= MIN_UPSCALE:  # only the upright read is upscaled: rotated small text is rare
        big = left.resize((round(rgb.width * scale), round(rgb.height * scale)), Image.Resampling.BICUBIC)
        found += _read(big, 0, scale)
    return found


def _upright(line: Line) -> bool:
    (x0, y0), (x1, y1) = line.quad[:2]
    return x1 - x0 > abs(y1 - y0)


def _erase(img: Image.Image, lines: list[Line]) -> Image.Image:
    """A copy with each line's quad filled in the corner's colour."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    for line in lines:
        draw.polygon(line.quad, fill=img.getpixel((0, 0)))
    return out


def _read(view: Image.Image, turns: int, scale: float) -> list[tuple[float, Line]]:
    """(score, line) for every line RapidOCR reads in `view` turned `turns` quarter turns, in view pixels / scale."""
    padded, (dx, dy) = _pad(view)
    pixels = np.ascontiguousarray(np.rot90(np.asarray(padded), turns)[..., ::-1])  # RapidOCR expects BGR
    # Without the angle classifier a line only reads right when it is upright, so its quad
    # is in reading order and no duplicate can come back with the corners reversed.
    result, _ = _engine()(pixels, use_cls=False, return_word_box=True)
    out = []
    for box, text, score, chars, *_ in result or []:
        quad = tuple((float(x), float(y)) for x, y in box)
        if _is_vertical(quad):  # RapidOCR turns tall crops a quarter turn: they read top to bottom
            quad = quad[1:] + quad[:1]
        cuts = _cuts(quad, chars) if len(chars) == len(text) else ()
        quad = (_unturn(p, turns, padded.size) for p in quad)
        out.append((float(score), Line(text, tuple(((x - dx) / scale, (y - dy) / scale) for x, y in quad), cuts)))
    return out


def _pad(view: Image.Image) -> tuple[Image.Image, tuple[int, int]]:
    """The view on a canvas no thinner than 1:ASPECT (in the corner's colour), and where it sits."""
    w, h = view.size
    pw, ph = max(w, h // ASPECT), max(h, w // ASPECT)
    if (pw, ph) == (w, h):
        return view, (0, 0)
    canvas = Image.new("RGB", (pw, ph), view.getpixel((0, 0)))
    offset = ((pw - w) // 2, (ph - h) // 2)
    canvas.paste(view, offset)
    return canvas, offset


def _cuts(quad: Quad, chars: list) -> tuple[float, ...]:
    """Boundaries halfway between the centres of neighbouring character boxes, projected on the line."""
    (x0, y0), (x1, y1) = quad[0], quad[1]
    dx, dy = x1 - x0, y1 - y0
    span = dx * dx + dy * dy or 1.0
    at = [(sum(x - x0 for x, _ in c) / 4 * dx + sum(y - y0 for _, y in c) / 4 * dy) / span for c in chars]
    bounds = [0.0, *((a + b) / 2 for a, b in zip(at, at[1:])), 1.0]
    return tuple(accumulate((min(max(b, 0.0), 1.0) for b in bounds), max))


def _is_vertical(quad: Quad) -> bool:
    w = max(math.dist(quad[0], quad[1]), math.dist(quad[3], quad[2]))
    h = max(math.dist(quad[0], quad[3]), math.dist(quad[1], quad[2]))
    return h >= 1.5 * w


def _unturn(p: tuple[float, float], turns: int, size: tuple[int, int]) -> tuple[float, float]:
    """Map a point of the view turned `turns` quarter turns back to the unturned view."""
    (x, y), (w, h) = p, size
    return [(x, y), (w - y, x), (w - x, h - y), (y, h - x)][turns]


def _bbox(quad: Quad) -> tuple[float, float, float, float]:
    xs, ys = zip(*quad)
    return min(xs), min(ys), max(xs), max(ys)


def _merge(found: list[tuple[float, Line]]) -> list[Line]:
    """One read per line. Passes and scales read most lines several times, some split into words.

    Reads are taken by expected correct characters (score x length), so a whole line beats its
    fragments, and a read that dropped a space, which hides values from the detectors
    ("IBANGB82..." matches nothing), loses to one that kept it. Two reads are the same line when
    the centre of one lies inside the other, which holds for tilted lines where boxes overlap.
    """
    kept, boxes, centres = [], np.empty((len(found), 4)), np.empty((len(found), 2))
    for _, line in sorted(found, key=lambda f: -f[0] * len(f[1].text)):
        box = _bbox(line.quad)
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        n, b, c = len(kept), boxes[: len(kept)], centres[: len(kept)]
        near = ((b[:, 0] <= centre[0]) & (centre[0] <= b[:, 2]) & (b[:, 1] <= centre[1]) & (centre[1] <= b[:, 3])) | (
            (box[0] <= c[:, 0]) & (c[:, 0] <= box[2]) & (box[1] <= c[:, 1]) & (c[:, 1] <= box[3]))
        if any(_inside(centre, kept[j].quad) or _inside(c[j], line.quad) for j in np.flatnonzero(near)):
            continue
        boxes[n], centres[n] = box, centre
        kept.append(line)
    return kept


def _inside(p: tuple[float, float], quad: Quad) -> bool:
    """Whether the point is in the convex quad, whichever way round its corners run."""
    cross = [(b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) for a, b in zip(quad, quad[1:] + quad[:1])]
    return all(v >= 0 for v in cross) or all(v <= 0 for v in cross)


def _reading_order(lines: list[Line]) -> list[Line]:
    """Top to bottom, and left to right within a row, so wrapped values stay adjacent."""
    def centre(line):
        x0, y0, x1, y1 = _bbox(line.quad)
        return (x0 + x1) / 2, (y0 + y1) / 2

    rows: list[tuple[float, float, list[Line]]] = []  # (centre y, half height, lines)
    for line in sorted(lines, key=lambda l: centre(l)[1]):
        cy, half = centre(line)[1], math.dist(line.quad[0], line.quad[3]) / 2
        if rows and abs(cy - rows[-1][0]) < rows[-1][1]:
            rows[-1][2].append(line)
        else:
            rows.append((cy, half, [line]))
    return [line for *_, row in rows for line in sorted(row, key=lambda l: centre(l)[0])]


def char_quad(line: Line, start: int, end: int) -> Quad:
    """Corners of characters [start, end), by linear interpolation along the reading direction."""
    n = len(line.text)
    cuts = line.cuts if len(line.cuts) == n + 1 else [i / max(n, 1) for i in range(n + 1)]
    a, b = cuts[min(max(start, 0), n)], cuts[min(max(end, 0), n)]
    p0, p1, p2, p3 = line.quad

    def lerp(p, q, t):
        return p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t

    return lerp(p0, p1, a), lerp(p0, p1, b), lerp(p3, p2, b), lerp(p3, p2, a)
