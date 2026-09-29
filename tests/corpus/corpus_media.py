"""PDF and image (PNG/JPG) builders for the seeded corpus.

WHY Pillow + reportlab by hand: the leak harness must prove it can see
PII inside rasterised pages, embedded images, rotated/low-contrast text,
QR payloads and EXIF -- none of that comes from a single high-level API.
"""
from __future__ import annotations

from pathlib import Path

import reportlab.rl_config as rl_config

rl_config.invariant = 1  # deterministic PDF bytes: no timestamps, fixed object ids
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from PIL import Image, ImageDraw, ImageFont
import PIL.ExifTags as ExifTags
import zxingcpp

PDF_VARIANTS = ["digital", "split_lines", "scanned", "mixed", "rotated", "cropped"]

FONT_SIZE = 32  # capitals render ~23 px tall, lowercase ~18 px: RapidOCR reads both reliably
_FONT = ImageFont.load_default(size=FONT_SIZE)
LOW_CONTRAST_GRAY = (140, 140, 140)  # on white: contrast ratio ~3.36, still >=3:1
FACES = Path(__file__).resolve().parents[1] / "fixtures" / "faces"
FACE_SIZE = 280  # small enough for the free bottom-right corner, large enough for YuNet


def _seed(entries: list[dict], entity_type: str, value: str, location: str) -> str:
    entries.append({"entity_type": entity_type, "value": value, "location": location})
    return value


# ---------------------------------------------------------------------------
# PDF variants
# ---------------------------------------------------------------------------

def build_pdf(variant: str, vf, path: Path) -> list[dict]:
    entries: list[dict] = []
    c = rl_canvas.Canvas(str(path), pagesize=LETTER)
    width, height = LETTER

    if variant == "digital":
        person = _seed(entries, "PERSON", vf.person(), "text_layer")
        email = _seed(entries, "EMAIL", vf.email(), "text_layer")
        dob = _seed(entries, "DATE_OF_BIRTH", vf.dob(), "text_layer")
        term = _seed(entries, "COMPANY_TERM", vf.company_term(), "text_layer")
        c.setFont("Helvetica", 12)
        lines = [
            f"{term} client record",
            vf.filler_sentence(),
            f"Name: {person}",
            f"Email: {email}",
            f"Date of birth: {dob}",
            vf.filler_sentence(),
        ]
        y = height - 72
        for line in lines:
            c.drawString(72, y, line)
            y -= 18

    elif variant == "split_lines":
        card = _seed(entries, "CREDIT_CARD", vf.credit_card(style="none"), "split_lines")
        address = _seed(entries, "ADDRESS", vf.address(), "split_lines")
        c.setFont("Helvetica", 12)
        c.drawString(72, height - 72, vf.filler_sentence())
        # wrap the card mid-digit-run across two lines, as a real paragraph would
        mid = len(card) // 2
        c.drawString(72, height - 100, f"Card on file: {card[:mid]}")
        c.drawString(72, height - 118, card[mid:])
        mid_a = len(address) // 2
        c.drawString(72, height - 146, f"Address: {address[:mid_a]}")
        c.drawString(72, height - 164, address[mid_a:])

    elif variant == "scanned":
        person = _seed(entries, "PERSON", vf.person(), "page_image")
        phone = _seed(entries, "PHONE", vf.phone(), "page_image")
        ssn = _seed(entries, "US_SSN", vf.us_ssn(), "page_image")
        page_img = _text_page_image(
            [vf.filler_sentence(), f"Name: {person}", f"Phone: {phone}", f"SSN: {ssn}"],
            size=(int(width), int(height)),
        )
        c.drawImage(ImageReader(page_img), 0, 0, width=width, height=height)

    elif variant == "mixed":
        person = _seed(entries, "PERSON", vf.person(), "text_layer")
        email = _seed(entries, "EMAIL", vf.email(), "text_layer")
        c.setFont("Helvetica", 12)
        c.drawString(72, height - 72, vf.filler_sentence())
        c.drawString(72, height - 90, f"Name: {person}")
        c.drawString(72, height - 108, f"Email: {email}")
        # the embedded photo holds OTHER values than the text layer above
        img_person = _seed(entries, "PERSON", vf.person(), "embedded_image")
        img_card = _seed(entries, "CREDIT_CARD", vf.credit_card(), "embedded_image")
        embedded = _text_page_image(
            [f"Signed: {img_person}", f"Card: {img_card}"], size=(500, 160)
        )
        c.drawImage(ImageReader(embedded), 72, height - 320, width=250, height=80)

    elif variant == "rotated":
        # /Rotate turns the page when shown: boxes placed in unrotated page space land beside the text.
        c.setPageRotation(vf.rng.choice([90, 270]))
        person = _seed(entries, "PERSON", vf.person(), "text_layer")
        phone = _seed(entries, "PHONE", vf.phone(), "text_layer")
        email = _seed(entries, "EMAIL", vf.email(), "text_layer")
        c.setFont("Helvetica", 12)
        for i, line in enumerate([vf.filler_sentence(), f"Name: {person}", f"Phone: {phone}", f"Email: {email}"]):
            c.drawString(72, height - 72 - 18 * i, line)

    elif variant == "cropped":
        # A CropBox shows only part of the page, so page space and shown pixels differ by its origin.
        c.setCropBox((100, 100, width - 60, height - 60))
        person = _seed(entries, "PERSON", vf.person(), "text_layer")
        sin = _seed(entries, "CA_SIN", vf.ca_sin(), "text_layer")
        iban = _seed(entries, "IBAN", vf.iban(), "text_layer")
        c.setFont("Helvetica", 12)
        for i, line in enumerate([vf.filler_sentence(), f"Employee: {person}", f"SIN: {sin}", f"Payroll IBAN: {iban}"]):
            c.drawString(160, height - 140 - 18 * i, line)

    c.showPage()
    c.save()
    return entries


def _text_page_image(lines: list[str], size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    y = 20
    for line in lines:
        d.text((20, y), line, font=_FONT, fill="black")
        y += FONT_SIZE + 12
    return img


# ---------------------------------------------------------------------------
# PNG / JPG variant
# ---------------------------------------------------------------------------

def _rotated_block(text: str, angle: int, pad: int = 24) -> Image.Image:
    bbox = _FONT.getbbox(text)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tmp = Image.new("RGB", (w + pad * 2, h + pad * 2), "white")
    ImageDraw.Draw(tmp).text((pad, pad), text, font=_FONT, fill="black")
    return tmp.rotate(angle, expand=True, fillcolor="white")


def _qr_image(payload: str, scale: int = 6) -> Image.Image:
    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode)
    bitmap = zxingcpp.write_barcode_to_image(barcode, scale=scale)
    h, w = bitmap.shape  # avoid a numpy dependency: read via the buffer protocol
    return Image.frombuffer("L", (w, h), bytes(bitmap), "raw", "L", 0, 1).convert("RGB")


SCREEN = (3840, 2160)
SCREEN_FONT = ImageFont.load_default(size=14)  # desktop UI text at 100% scaling
SCREEN_BG, SCREEN_PANEL, SCREEN_TEXT = (32, 33, 36), (48, 49, 54), (225, 225, 225)


def build_screenshot(vf, path: Path) -> list[dict]:
    """A dark-mode 4K chat window with small text spread to every edge.

    Read whole, it only works at full size: shrunk to 2000 px, 14 px text drops below what
    the OCR detector can see, so this catches any reader (redactor or verifier) that shrinks.
    """
    entries: list[dict] = []
    img = Image.new("RGB", SCREEN, SCREEN_BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 420, SCREEN[1]), fill=SCREEN_PANEL)  # the conversation list
    person = _seed(entries, "PERSON", vf.person(), "screenshot")
    d.text((24, 60), f"Chat with {person}", font=SCREEN_FONT, fill=SCREEN_TEXT)
    lines = [
        vf.filler_sentence(),
        f"My email is {_seed(entries, 'EMAIL', vf.email(), 'screenshot')}",
        f"Call me on {_seed(entries, 'PHONE', vf.phone(), 'screenshot')}",
        vf.filler_sentence(),
    ]
    for i, line in enumerate(lines):
        d.text((480, 120 + 36 * i), line, font=SCREEN_FONT, fill=SCREEN_TEXT)
    card = _seed(entries, "CREDIT_CARD", vf.credit_card(), "screenshot")
    d.text((SCREEN[0] - 520, SCREEN[1] // 2), f"Card: {card}", font=SCREEN_FONT, fill=SCREEN_TEXT)
    key = _seed(entries, "API_KEY", vf.api_key(vf.rng.choice(["aws", "github", "slack"])), "screenshot")  # one line: no PEM
    d.text((480, SCREEN[1] - 60), f"export TOKEN={key}", font=SCREEN_FONT, fill=SCREEN_TEXT)
    img.save(path, format="PNG")
    return entries


def build_image(fmt: str, vf, path: Path) -> list[dict]:
    """fmt is 'png' or 'jpg'; both share layout, EXIF support differs only in file save."""
    entries: list[dict] = []
    canvas_w, canvas_h = 1000, 900
    img = Image.new("RGB", (canvas_w, canvas_h), "white")
    d = ImageDraw.Draw(img)

    person = _seed(entries, "PERSON", vf.person(), "page_image")
    phone = _seed(entries, "PHONE", vf.phone(), "page_image")
    address = _seed(entries, "ADDRESS", vf.address(), "page_image")
    term = _seed(entries, "COMPANY_TERM", vf.company_term(), "page_image")

    y = 20
    for line in (
        f"{term} visitor log",
        f"Name: {person}",
        f"Phone: {phone}",
        f"Address: {address}",
    ):
        d.text((20, y), line, font=_FONT, fill="black")
        y += FONT_SIZE + 14

    # low-contrast line, still >=3:1 against white
    card = _seed(entries, "CREDIT_CARD", vf.credit_card(), "low_contrast")
    d.text((20, y), f"Card: {card}", font=_FONT, fill=LOW_CONTRAST_GRAY)
    y += FONT_SIZE + 20

    # small-angle rotated line
    small_val = _seed(entries, "IBAN", vf.iban(), "rotated")
    small_block = _rotated_block(f"IBAN {small_val}", 6)
    img.paste(small_block, (20, y))

    # 90-degree rotated line, placed to the right of the small-angle block
    ninety_val = _seed(entries, "PASSPORT", vf.passport(), "rotated")
    ninety_block = _rotated_block(f"PP {ninety_val}", 90)
    img.paste(ninety_block, (20 + small_block.width + 40, y))
    y += max(small_block.height, ninety_block.height) + 20

    # QR payload: email + phone, decodable back to the same text
    qr_email = _seed(entries, "EMAIL", vf.email(), "qr_payload")
    qr_phone = _seed(entries, "PHONE", vf.phone(), "qr_payload")
    qr_img = _qr_image(f"{qr_email} {qr_phone}")
    img.paste(qr_img, (20, y))

    # A synthetic face in the bottom-right corner, clear of every block above.
    face = vf.rng.choice(sorted(FACES.glob("*.jpg")))
    with Image.open(face) as photo:
        img.paste(photo.convert("RGB").resize((FACE_SIZE, FACE_SIZE)), (canvas_w - FACE_SIZE - 20, canvas_h - FACE_SIZE - 20))
    _seed(entries, "FACE", face.name, "face")

    # EXIF: Artist=PERSON, ImageDescription=EMAIL, plus a GPS IFD for realism
    exif_person = _seed(entries, "PERSON", vf.person(), "exif")
    exif_email = _seed(entries, "EMAIL", vf.email(), "exif")
    exif = img.getexif()
    exif[ExifTags.Base.Artist] = exif_person
    exif[ExifTags.Base.ImageDescription] = exif_email
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (43.0, 38.0, 0.0)
    gps[ExifTags.GPS.GPSLongitudeRef] = "W"
    gps[ExifTags.GPS.GPSLongitude] = (79.0, 23.0, 0.0)

    if fmt == "jpg":
        img.save(path, format="JPEG", quality=90, exif=exif)
    else:
        img.save(path, format="PNG", exif=exif)
    return entries
