"""Leak test: prove that no seeded value survives in redacted outputs.

    uv run python tests/leak/run.py --corpus tests/corpus/out --outputs <dir>

<dir> mirrors the corpus layout. Every file under <dir> whose path starts with an input's
path minus its extension counts as that input's output: `docx/a.docx` -> `docx/a.md`.
Pointing --outputs at the corpus itself runs the "redactor that changes nothing" and must
report 0% recall; that run proves the harness can see leaks.

The verifier is deliberately independent of the redactor: it renders PDFs at 300 DPI,
OCRs images at two scales and three rotations, decodes barcodes, dumps metadata, and also
searches raw bytes. A leak only one of these paths can see still counts.

Exit code 1 if any seeded value (or an identifying part of one) survives, if an output
still carries metadata or a PDF text layer, or if nothing was checked at all.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import Path

FUZZY_MIN_LEN = 8  # shorter values need an exact hit; 1 edit on "12345" is too loose
TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv"}
# Honorifics and suffixes are not identifying on their own, so they are not name parts.
NOT_NAME_PARTS = {"mr", "mrs", "ms", "miss", "dr", "jr", "sr", "md", "phd", "dds", "dvm", "ii", "iii", "iv"}
# OCR confuses these pairs; folding both sides stops a misread digit from hiding a leak.
OCR_FOLD = str.maketrans("oilsb", "01158")
# PDF info keys that name the producing tool or a time, never the document's subject.
HARMLESS_PDF_META = {"Producer", "CreationDate", "ModDate"}


def _canon(text: str) -> str:
    """Decode HTML entities and fold Unicode variants (NFKC) before any comparison.

    "O&#39;Brien", decomposed accents and fullwidth digits must match their plain forms.
    """
    return unicodedata.normalize("NFKC", html.unescape(text)).casefold()


def normalize(value: str) -> str:
    """Keep letters and digits only, so "4111 1111" and an OCR'd "41111111" compare equal.

    Must match the engine's normalisation for --spans digests.
    """
    return re.sub(r"[\W_]+", "", _canon(value))


def words(text: str) -> str:
    """Space-separated tokens with padding, for whole-word matching of short parts."""
    return " " + re.sub(r"[\W_]+", " ", _canon(text)).strip() + " "


def parts(entity_type: str, value: str) -> list[str]:
    """Pieces that identify someone on their own.

    A redactor that replaces only the first name ("[PERSON_1] Okafor"), only the house
    number, or only a PEM key's armour lines has still leaked, so each piece is checked.
    """
    if entity_type == "PERSON":
        return [t for t in words(value).split() if len(t) >= 3 and t not in NOT_NAME_PARTS]
    if entity_type == "ADDRESS":
        pieces = (re.sub(r"^\d+\w*\s+", "", p.strip()) for p in value.split(","))
        return [p for p in pieces if len(normalize(p)) >= 5]
    if entity_type == "API_KEY" and "-----BEGIN" in value:
        return [line for line in value.splitlines() if line and not line.startswith("-----")]
    return []


def fuzzy_contains(needle: str, hay: str, k: int = 1) -> bool:
    """True if `needle` occurs in `hay` with at most k edits.

    Pigeonhole prefilter: with k edits, at least one of k+1 pieces of the needle appears
    verbatim, so the O(n*m) edit-distance scan only runs on windows around those hits.
    """
    if needle in hay:
        return True
    if len(needle) < FUZZY_MIN_LEN:
        return False
    size = len(needle) // (k + 1)
    for i in range(k + 1):
        piece = needle[i * size: (i + 1) * size if i < k else None]
        start = hay.find(piece)
        while start != -1:
            window = hay[max(0, start - len(needle) - k): start + len(needle) + k]
            if _edit_distance_substring(needle, window) <= k:
                return True
            start = hay.find(piece, start + 1)
    return False


def _edit_distance_substring(p: str, t: str) -> int:
    """Smallest edit distance between p and any substring of t (Sellers' algorithm)."""
    prev = [0] * (len(t) + 1)  # free start anywhere in t
    for i, pc in enumerate(p, 1):
        cur = [i] + [0] * len(t)
        for j, tc in enumerate(t, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (pc != tc))
        prev = cur
    return min(prev)


def survives(value: str, source: str, hay: str) -> bool:
    """OCR sources get one edit per 10 characters and look-alike folding; others get one edit."""
    needle = normalize(value)
    if source.endswith("_ocr"):
        return fuzzy_contains(needle.translate(OCR_FOLD), hay.translate(OCR_FOLD), max(1, len(needle) // 10))
    return fuzzy_contains(needle, hay)


def found_in(entity_type: str, value: str, compact: dict[str, str], spaced: dict[str, str]) -> list[str]:
    """Sources where the whole value (fuzzy) or an identifying part (whole word) survives.

    Parts are never matched against raw bytes: short names like "Page" or "Max" occur by
    chance in binary data ("/Type /Page" is in every PDF), which would fail a correct redactor.
    """
    hits = [src for src, hay in compact.items() if survives(value, src, hay)]
    return hits + [
        f"{src} (part: {p})"
        for p in parts(entity_type, value)
        for src, hay in spaced.items()
        if not src.endswith(("/bytes", ":bytes")) and words(p) in hay
    ]


# --- extractors -----------------------------------------------------------------------

def extract(path: Path) -> tuple[dict[str, str], list[str]]:
    """Return ({source_name: text}, [metadata problems]) for one output file."""
    problems: list[str] = []
    return _extract(path.read_bytes(), path.name, problems), problems


def _extract(data: bytes, name: str, problems: list[str]) -> dict[str, str]:
    """Sniff the type from content, not the suffix: an image saved as .webp must still be
    OCR'd, and an unknown type is an error, never "plain text"."""
    # Raw bytes catch metadata and uncompressed streams no parser was asked about.
    sources = {"bytes": data.decode("latin-1") + data.decode("utf-16-le", "ignore")}
    if data.startswith(b"%PDF"):
        sources.update(_pdf_text(data, problems))
    elif zipfile.is_zipfile(io.BytesIO(data)):
        sources.update(_zip_text(data, problems))
    elif (img := _open_image(data)) is not None:
        with img:
            sources.update(_image_text(img, problems))
    elif Path(name).suffix.lower() in TEXT_SUFFIXES:
        sources["text"] = data.decode("utf-8", "replace")
    else:
        raise ValueError(f"{name}: unrecognised output type; refusing to score it as text")
    return sources


def _open_image(data: bytes):
    from PIL import Image, UnidentifiedImageError

    try:
        return Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        return None


def _zip_text(data: bytes, problems: list[str]) -> dict[str, str]:
    """XML parts as text plus attribute values (comment and revision authors live in
    attributes); every other member is extracted recursively, so an image zipped up is
    still OCR'd rather than skipped."""
    sources, xml_parts = {}, []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            member = z.read(name)
            if name.endswith((".xml", ".rels")):
                xml = member.decode("utf-8", "replace")
                xml_parts.append(re.sub(r"<[^>]+>", "", xml))  # adjacent runs join up
                xml_parts.extend(re.findall(r'="([^"]*)"', xml))
            elif not name.endswith("/"):
                sources.update({f"{name}/{k}": v for k, v in _extract(member, name, problems).items()})
    sources["ooxml"] = " ".join(xml_parts)
    return sources


def _pdf_text(data: bytes, problems: list[str]) -> dict[str, str]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    layer, ocr = [], []
    for page in pdf:
        layer.append(page.get_textpage().get_text_range())
        ocr.append(_ocr(page.render(scale=300 / 72).to_pil(), rotations=(0,)))
    meta = {k: v for k, v in pdf.get_metadata_dict().items() if v and k not in HARMLESS_PDF_META}
    # Redacted PDFs are rebuilt from images, so any text layer or subject metadata means
    # the rebuild did not happen, even when no seeded value is in it.
    if "".join(layer).strip():
        problems.append("pdf text layer present")
    if meta:
        problems.append(f"pdf metadata present: {sorted(meta)}")
    return {"pdf_text_layer": "\n".join(layer), "pdf_ocr": "\n".join(ocr), "pdf_meta": " ".join(meta.values())}


def _image_text(img, problems: list[str]) -> dict[str, str]:
    import zxingcpp

    exif = img.getexif()
    meta = [str(v) for v in exif.values()] + [str(v) for v in exif.get_ifd(0x8825).values()]  # + GPS IFD
    meta += [str(v) for v in getattr(img, "text", {}).values()]  # PNG text chunks
    if img.info.get("comment"):
        meta.append(str(img.info["comment"]))  # JPEG COM marker
    # Images are re-encoded from pixels, so any EXIF, GPS or text chunk means the re-encode
    # failed, even when no seeded value is in it (GPS coordinates are never seeded).
    if meta:
        problems.append("image metadata present")
    # 2x upscale helps the detector with small or thin glyphs that 1x misses.
    big = img.resize((img.width * 2, img.height * 2))
    return {
        "image_ocr": _ocr(img, rotations=(0, 90, 270)) + "\n" + _ocr(big, rotations=(0,)),
        "barcode": " ".join(r.text for r in zxingcpp.read_barcodes(img)),
        "image_meta": " ".join(meta),
    }


_OCR = None


def _ocr(img, rotations) -> str:
    """RapidOCR at each rotation; its angle classifier only handles 180-degree flips."""
    global _OCR
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    _OCR = _OCR or RapidOCR()
    lines = []
    for angle in rotations:
        result, _ = _OCR(np.asarray(img.convert("RGB").rotate(angle, expand=True)))
        lines += [text for _box, text, _score in result or []]
    return "\n".join(lines)


# --- scoring -----------------------------------------------------------------------------

def outputs_for(input_rel: str, outputs: Path) -> list[Path]:
    stem = (outputs / input_rel).with_suffix("")
    return sorted(p for p in stem.parent.glob(stem.name + ".*") if p.is_file())


def run(corpus: Path, outputs: Path, spans_file: Path | None, formats: set[str] | None = None) -> dict:
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    if formats:  # CI runs the fast text formats only; OCR formats run locally
        unknown = formats - {d["format"] for d in manifest["documents"]}
        if unknown:  # a typo must not silently filter everything out
            raise ValueError(f"formats not in the manifest: {sorted(unknown)}")
        manifest["documents"] = [d for d in manifest["documents"] if d["format"] in formats]
    per = defaultdict(lambda: {"seeded": 0, "removed": 0})
    survivors, missing, violations = [], [], []
    for doc in manifest["documents"]:
        outs = outputs_for(doc["file"], outputs)
        if not outs:
            missing.append(doc["file"])  # no output means nothing was checked: fail loud
        compact, spaced = {}, {}
        for o in outs:
            sources, problems = extract(o)
            violations += [{"file": doc["file"], "output": o.name, "problem": p} for p in problems]
            for src, text in sources.items():
                compact[f"{o.name}:{src}"], spaced[f"{o.name}:{src}"] = normalize(text), words(text)
        for s in doc["seeded"]:
            key = (doc["format"], s["entity_type"])
            per[key]["seeded"] += 1
            hits = found_in(s["entity_type"], s["value"], compact, spaced)
            if hits or not outs:
                survivors.append({**s, "file": doc["file"], "found_in": hits or ["<no output>"]})
            else:
                per[key]["removed"] += 1
    return {
        "recall": {f"{f}/{e}": v for (f, e), v in sorted(per.items())},
        "precision": _precision(manifest, spans_file),
        "survivors": survivors,
        "violations": violations,
        "missing_outputs": missing,
    }


def _precision(manifest: dict, spans_file: Path | None) -> dict | None:
    """Share of redacted spans whose exact value was a seeded value, per format.

    Stricter than the overlap definition in docs/PLAN.md §9: a span that covers only part
    of a seeded value counts as a false positive here.

    Needs the engine's test-only span dump: JSONL of {file, entity_type, value_digest},
    digest = sha256(normalize(value)). Unsalted digests of IDs are reversible, which is why
    that dump exists only for synthetic data and never in normal operation.
    """
    if not spans_file:
        return None
    seeded = {
        d["file"]: {hashlib.sha256(normalize(s["value"]).encode()).hexdigest() for s in d["seeded"]}
        for d in manifest["documents"]
    }
    fmt = {d["file"]: d["format"] for d in manifest["documents"]}
    tally = defaultdict(lambda: [0, 0])  # format -> [true positives, all spans]
    for line in spans_file.read_text(encoding="utf-8").splitlines():
        span = json.loads(line)
        if span["file"] not in fmt:  # filtered out by --formats
            continue
        t = tally[fmt[span["file"]]]
        t[0] += span["value_digest"] in seeded[span["file"]]
        t[1] += 1
    return {f: {"true_positive": tp, "redacted": n, "precision": round(tp / n, 4)} for f, (tp, n) in tally.items()}


def write_report(result: dict, report_dir: Path) -> str:
    rows = ["| Format / entity | Seeded | Removed | Recall |", "|---|---:|---:|---:|"]
    for key, v in result["recall"].items():
        rows.append(f"| {key} | {v['seeded']} | {v['removed']} | {v['removed'] / v['seeded']:.0%} |")
    total = sum(v["seeded"] for v in result["recall"].values())
    lines = [
        "# Leak report", "",
        f"Survivors: **{len(result['survivors'])}** of {total} seeded values.",
        f"Metadata / text-layer violations: **{len(result['violations'])}**.", "",
        *rows, "",
        "Precision: " + ("not measured (no --spans file)" if result["precision"] is None
                         else json.dumps(result["precision"])),
    ]
    if result["missing_outputs"]:
        lines += ["", f"Inputs with no output: {len(result['missing_outputs'])}"]
    md = "\n".join(lines) + "\n"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "report.md").write_text(md, encoding="utf-8")
    # Synthetic values only, so the full survivor list is safe to keep for debugging.
    (report_dir / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--outputs", type=Path, required=True)
    ap.add_argument("--spans", type=Path, help="engine span dump for precision")
    ap.add_argument("--formats", help="comma-separated subset, e.g. txt,md,docx")
    ap.add_argument("--report", type=Path, default=Path("tests/leak/out"))
    args = ap.parse_args(argv)
    formats = {f.strip().lower() for f in args.formats.split(",")} if args.formats else None
    result = run(args.corpus, args.outputs, args.spans, formats)
    print(write_report(result, args.report))
    checked = sum(v["seeded"] for v in result["recall"].values())
    return 1 if result["survivors"] or result["violations"] or result["missing_outputs"] or not checked else 0


if __name__ == "__main__":
    sys.exit(main())
