"""Images: OCR text, faces and barcodes are filled, and the output carries no metadata."""

import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import run as leak
import zxingcpp
from PIL import Image, ImageDraw, ImageFont, ImageOps
from redactit import models, ocr
from redactit.formats import image
from redactit.formats.image import Box, ImageError, find_boxes, paint, redact_image

try:
    models.path_for("gliner/model.onnx")
    models.path_for(image.YUNET)
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

from redactit.audit import AuditLog  # noqa: E402
from redactit.pipeline import Engine, Result  # noqa: E402
from redactit.policy import load_policy  # noqa: E402
from redactit.types import Decision, Span  # noqa: E402
from redactit.vault import Vault  # noqa: E402

FONT = ImageFont.load_default(size=32)
FACE_FIXTURES = sorted(
    p for p in (Path(__file__).parent.parent / "fixtures" / "faces").glob("*")
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
)
IBAN = "GB82 WEST 1234 5698 7654 32"
SEEDED = [
    ("PERSON", "Priya Okafor"),
    ("EMAIL", "priya.okafor@northwind.com"),
    ("CREDIT_CARD", "4111 1111 1111 1111"),
    ("IBAN", IBAN),
    ("EMAIL", "bob.jones@example.org"),  # only in the QR code
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("image")
    with Vault(tmp / "vault.db", os.urandom(32)) as vault:
        yield Engine(load_policy(None), vault, AuditLog(tmp / "audit.jsonl")), tmp / "audit.jsonl"


def _text_tile(text: str, angle: int = 0) -> Image.Image:
    tile = Image.new("RGB", (FONT.getbbox(text)[2] + 20, 56), "white")
    ImageDraw.Draw(tile).text((10, 8), text, font=FONT, fill="black")
    return tile.rotate(angle, expand=True, fillcolor="white")


def _qr(payload: str) -> Image.Image:
    bitmap = zxingcpp.write_barcode_to_image(zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode), scale=6)
    h, w = bitmap.shape
    return Image.frombuffer("L", (w, h), bytes(bitmap), "raw", "L", 0, 1).convert("RGB")


def _png(img: Image.Image, **save) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", **save)
    return out.getvalue()


def _seeded_png() -> bytes:
    img = Image.new("RGB", (1000, 820), "white")
    draw = ImageDraw.Draw(img)
    for y, line in enumerate(["Name: Priya Okafor", "Email: priya.okafor@northwind.com", "Card: 4111 1111 1111 1111"]):
        draw.text((40, 40 + y * 60), line, font=FONT, fill="black")
    img.paste(_text_tile(f"IBAN {IBAN}", 90), (900, 40))
    img.paste(_qr("bob.jones@example.org"), (40, 300))
    exif = img.getexif()
    exif[0x013B] = "Priya Okafor"  # Artist
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (43.0, 38.0, 0.0), "W", (79.0, 23.0, 0.0)
    return _png(img, exif=exif)


def _verify(data: bytes):
    """The leak harness's independent read of an output: which seeded values survive, plus metadata problems."""
    problems: list[str] = []
    with Image.open(io.BytesIO(data)) as img:
        sources = leak._image_text(img, problems)
    compact = {k: leak.normalize(v) for k, v in sources.items()}
    spaced = {k: leak.words(v) for k, v in sources.items()}
    return {v: leak.found_in(t, v, compact, spaced) for t, v in SEEDED}, sources, problems


@pytest.fixture(scope="module")
def seeded(engine):
    data = _seeded_png()
    out, suffix, text = redact_image(data, engine[0], "t")
    return data, out, suffix, text


def test_no_seeded_value_survives_in_the_output(seeded):
    data, out, suffix, _ = seeded
    assert suffix == ".png"
    assert all(_verify(data)[0].values()), "the OCR the check relies on must see every value in the input"
    survivors, sources, problems = _verify(out)
    assert not {v: hits for v, hits in survivors.items() if hits}
    assert sources["barcode"] == ""
    assert problems == []


def test_metadata_never_carries_over(seeded):
    data, out, _, _ = seeded
    with Image.open(io.BytesIO(data)) as before, Image.open(io.BytesIO(out)) as after:
        assert before.getexif().get_ifd(0x8825), "the input must carry GPS for this test to mean anything"
        assert not after.getexif() and not after.getexif().get_ifd(0x8825)
        assert not after.info.get("exif") and not getattr(after, "text", {}) and "icc_profile" not in after.info
    assert b"Okafor" not in out and not any(chunk in out for chunk in (b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"iCCP"))


def test_a_pseudonymized_name_shows_its_label(seeded):
    _, out, _, text = seeded
    assert "[PERSON_1]" in text and "Okafor" not in text
    with Image.open(io.BytesIO(out)) as img:
        assert "PERSON" in leak._ocr(img, rotations=(0,)).upper()


def test_reading_direction_is_right_at_every_orientation():
    img = Image.new("RGB", (1000, 900), "white")
    for angle, at in {0: (40, 40), 90: (900, 100), 180: (300, 780), 270: (800, 100)}.items():
        img.paste(_text_tile(f"IBAN {IBAN}", angle), at)
    lines = [ln for ln in ocr.read_lines(img) if len(ln.text) > 20]

    def heading(line):  # which way the first edge of the reading-order quad points, one of the four axes
        (x0, y0), (x1, y1) = line.quad[:2]
        return tuple(int(np.sign(d)) if abs(d) > 300 else 0 for d in (x1 - x0, y1 - y0))

    assert len(lines) == 4 and {heading(ln) for ln in lines} == {(1, 0), (0, -1), (-1, 0), (0, 1)}


def test_a_thin_strip_is_read_and_its_quads_stay_on_the_strip():
    strip = Image.new("RGB", (500, 44), "white")
    ImageDraw.Draw(strip).text((20, 2), "Priya Okafor", font=FONT, fill="black")
    (line,) = ocr.read_lines(strip)  # one read, not fragments from the turned passes
    x0, y0, x1, y1 = ocr._bbox(line.quad)
    assert "Okafor" in line.text and x0 < 40 and x1 > 150 and -8 <= y0 and y1 <= 52


@pytest.mark.parametrize("size", [(3840, 2160), (9000, 5000), (4097, 300)])
def test_large_images_are_read_in_overlapping_tiles_that_cover_every_pixel(size):
    tiles = [(x, y, *t.size) for (x, y), t in ocr._tiles(Image.new("RGB", size))]
    assert all(w <= ocr.MAX_SIDE and h <= ocr.MAX_SIDE for *_, w, h in tiles)
    for axis in (0, 1):  # along each axis: from 0 to the far edge, neighbours sharing at least TILE_OVERLAP
        spans = sorted({(t[axis], t[axis] + t[axis + 2]) for t in tiles})
        assert spans[0][0] == 0 and spans[-1][1] == size[axis]
        assert all(a_end - b_start >= ocr.TILE_OVERLAP for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def test_merge_takes_whole_lines_over_fragments_and_spaces_over_glue():
    def line(text, x0, x1, top=0, slant=0):  # a box `slant` pixels lower at its right end
        return ocr.Line(text, ((x0, top), (x1, top + slant), (x1, top + slant + 38), (x0, top + 38)))

    whole = line("Contact Priya Okafor", 0, 200)
    words = [line("Contact", 0, 70), line("Priya", 80, 130), line("Okafor", 140, 200)]
    assert ocr._merge([(1.0, w) for w in words] + [(0.9, whole)]) == [whole]
    spaced, glued = line("IBAN GB82", 0, 80), line("IBANGB82", 0, 80)
    assert ocr._merge([(1.0, glued), (0.97, spaced)]) == [spaced]
    above, below = line("first", 0, 600, 0, 63), line("second", 0, 600, 50, 63)  # tilted: bounding boxes overlap
    assert len(ocr._merge([(1.0, above), (1.0, below)])) == 2


def test_lines_come_back_top_to_bottom_and_left_to_right():
    def line(text, x, y):
        return ocr.Line(text, ((x, y), (x + 50, y), (x + 50, y + 20), (x, y + 20)))

    first, second, third = line("a", 200, 3), line("b", 10, 40), line("c", 120, 42)
    assert ocr._reading_order([third, first, second]) == [first, second, third]


def test_lines_of_a_sideways_page_come_back_in_its_own_reading_order():
    def column(text, x):  # read top to bottom, glyph tops facing right: a page turned 90 degrees
        return ocr.Line(text, ((x + 20, 0), (x + 20, 200), (x, 200), (x, 0)))

    first, second, third = column("a", 300), column("b", 250), column("c", 200)
    assert ocr._reading_order([third, first, second]) == [first, second, third]


def test_padding_follows_the_text_height_even_on_an_upright_box_over_sideways_text():
    grown = image._grow(((100, 0), (120, 0), (120, 400), (100, 400)))  # 20 px wide, 400 px long
    assert [round(x) for x, _ in grown] == [97, 123, 123, 97]  # 15% of 20 px, not of 400 px


def test_char_quad_interpolates_along_the_reading_direction():
    across = ocr.Line("abcd", ((0, 0), (40, 0), (40, 10), (0, 10)))
    assert ocr.char_quad(across, 1, 3) == ((10, 0), (30, 0), (30, 10), (10, 10))
    down = ocr.Line("abcd", ((10, 0), (10, 40), (0, 40), (0, 0)))  # text turned a quarter: reads downwards
    assert ocr.char_quad(down, 0, 2) == ((10, 0), (10, 20), (0, 20), (0, 0))


def test_char_quad_prefers_measured_positions_and_clamps():
    uneven = ocr.Line("ab", ((0, 0), (40, 0), (40, 10), (0, 10)), cuts=(0.0, 0.25, 1.0))
    assert ocr.char_quad(uneven, 1, 2) == ((10, 0), (40, 0), (40, 10), (10, 10))
    assert ocr.char_quad(uneven, -3, 9) == uneven.quad


def test_a_span_over_two_lines_gets_a_box_on_each(monkeypatch):
    lines = [
        ocr.Line("Ship to Priya", ((0, 0), (130, 0), (130, 20), (0, 20))),
        ocr.Line("Okafor today", ((0, 30), (120, 30), (120, 50), (0, 50))),
    ]
    text = "\n".join(ln.text for ln in lines)
    span = Span(text.index("Priya"), text.index(" today"), "PERSON", 0.9, "test")
    decision = Decision(span, "pseudonymize", "entities.PERSON", "test")
    engine = SimpleNamespace(
        policy=SimpleNamespace(entities={}, effective_dial=lambda: 3), audit=None,
        redact=lambda t, scope, **kw: Result("", [decision], ["[PERSON_1]"]),
    )
    monkeypatch.setattr(ocr, "read_lines", lambda img, dial: lines)
    boxes, _ = find_boxes(Image.new("RGB", (200, 100)), engine, "t")
    assert boxes == [
        Box(ocr.char_quad(lines[0], 8, 13), "[PERSON_1]"),
        Box(ocr.char_quad(lines[1], 0, 6), "[PERSON_1]"),
    ]
    decision = Decision(span, "mask", "entities.PERSON", "test")  # only a pseudonym is printed on the box
    engine.redact = lambda t, scope, **kw: Result("", [decision], ["*****"])
    assert {b.label for b in find_boxes(Image.new("RGB", (200, 100)), engine, "t")[0]} == {None}


def test_masking_prints_the_label_once_and_keeps_later_characters_in_place():
    line = ocr.Line("Contact Priya Okafor now", ((0, 0), (240, 0), (240, 10), (0, 10)))  # 10 px per character
    name, surname = ((80, 0), (200, 0), (200, 10), (80, 10)), ((140, 0), (200, 0), (200, 10), (140, 10))
    labelled = image._mask(line, [Box(name, "[PERSON_1]")])
    assert labelled.text == "Contact [PERSON_1] now"
    start = labelled.text.index("now")
    assert [round(p[0]) for p in ocr.char_quad(labelled, start, start + 3)[:2]] == [210, 240]
    assert image._mask(line, [Box(surname, None)]).text == "Contact Priya ██████ now"


def test_text_under_a_box_the_caller_fills_is_masked_in_the_text(engine):
    """A PDF's text-layer boxes are passed in, so its Markdown never shows what its page hides."""
    img = Image.new("RGB", (900, 80), "white")
    ImageDraw.Draw(img).text((20, 20), "Case ref XJ-4410 closed", font=FONT, fill="black")
    (line,) = ocr.read_lines(img)
    start = line.text.index("XJ")
    _, text = find_boxes(img, engine[0], "t", covered=[Box(ocr.char_quad(line, start, start + 7), None)])
    assert "XJ" not in text and "4410" not in text and "█" in text and "Case ref" in text and "closed" in text


def test_paint_fills_padded_boxes_on_a_copy_and_labels_them():
    img = Image.new("RGB", (300, 100), "white")
    quad = ((50, 40), (250, 40), (250, 60), (50, 60))
    plain = paint(img, [Box(quad, None)])
    assert img.getpixel((100, 50)) == (255, 255, 255)  # the input is untouched
    black, white = (0, 0, 0), (255, 255, 255)
    assert plain.getpixel((100, 50)) == black and plain.getpixel((48, 50)) == black  # padded by 15% of the height
    assert plain.getpixel((44, 50)) == white and plain.getpixel((100, 10)) == white
    labelled = paint(img, [Box(quad, "[PERSON_1]")])
    assert labelled.crop((50, 40, 250, 60)).getextrema() == ((0, 255), (0, 255), (0, 255))


def test_other_formats_become_png_and_transparency_is_flattened(engine):
    bmp = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(bmp, "BMP")
    _, suffix, _ = redact_image(bmp.getvalue(), engine[0], "t")
    assert suffix == ".png"
    data, suffix, _ = redact_image(_png(Image.new("RGBA", (64, 64), (255, 0, 0, 0))), engine[0], "t")
    with Image.open(io.BytesIO(data)) as img:
        assert img.mode == "RGB" and img.getpixel((5, 5)) == (255, 255, 255)


def test_jpeg_stays_jpeg_with_orientation_applied_and_no_metadata(engine):
    img = Image.new("RGB", (200, 100), "white")
    exif = img.getexif()
    exif[0x0112], exif[0x013B] = 6, "Priya Okafor"  # rotated a quarter turn; Artist
    src = io.BytesIO()
    img.save(src, "JPEG", exif=exif)
    data, suffix, text = redact_image(src.getvalue(), engine[0], "t")
    with Image.open(io.BytesIO(data)) as out:
        assert (suffix, out.format, out.size, text) == (".jpg", "JPEG", (100, 200), "")
        assert not out.getexif() and not out.info.get("exif")
    assert b"Okafor" not in data


def test_oversized_and_unreadable_files_are_refused(engine):
    huge = _png(Image.new("1", (7072, 7072)))  # 50.01 megapixels, a few KB on disk
    with pytest.raises(ImageError, match="megapixels"):
        redact_image(huge, engine[0], "t")
    with pytest.raises(ImageError, match="readable"):
        redact_image(b"not an image", engine[0], "t")


def test_a_barcode_is_audited_without_its_payload(engine):
    eng, audit = engine
    img = Image.new("RGB", (400, 400), "white")
    img.paste(_qr("carol.white@example.net"), (100, 100))
    data, _, _ = redact_image(_png(img), eng, "t")
    event = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert event["event"] == "redaction" and event["decisions"] == []
    assert event["entity_counts"] == {"BARCODE": 1, "FACE": 0}
    assert "carol" not in audit.read_text(encoding="utf-8") and b"carol" not in data
    with Image.open(io.BytesIO(data)) as out:
        assert zxingcpp.read_barcodes(out) == []


def _faces(data: bytes) -> int:
    """How many faces YuNet finds in an encoded image, after the orientation the redactor applies."""
    import cv2

    with Image.open(io.BytesIO(data)) as img:
        rgb = ImageOps.exif_transpose(img).convert("RGB")
    detector = cv2.FaceDetectorYN.create(str(models.path_for(image.YUNET)), "", rgb.size, image.FACE_SCORE)
    _, found = detector.detect(np.ascontiguousarray(np.asarray(rgb)[..., ::-1]))
    return 0 if found is None else len(found)


@pytest.mark.parametrize("path", FACE_FIXTURES, ids=lambda p: p.name)  # no fixtures: the test is skipped
def test_every_detected_face_is_covered(path, engine):
    data = path.read_bytes()
    if not _faces(data):
        pytest.skip("YuNet finds no face in this fixture")
    out, _, _ = redact_image(data, engine[0], "t")
    assert _faces(out) == 0


def test_formats_outside_the_allowlist_are_refused(engine):
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    with pytest.raises(ImageError):
        redact_image(eps, engine, "s")
