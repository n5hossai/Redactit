"""Leak test: prove that no seeded value survives in redacted outputs.

    uv run python tests/leak/run.py --corpus tests/corpus/out --outputs <dir>

<dir> mirrors the corpus layout. Every file under <dir> whose path starts with an input's
path minus its extension counts as that input's output: `docx/a.docx` -> `docx/a.md`.
Pointing --outputs at the corpus itself runs the "redactor that changes nothing" and must
report 0% recall; that run proves the harness can see leaks.

The verifier is deliberately independent of the redactor: it renders PDFs at 300 DPI,
OCRs images at two scales and three rotations, decodes barcodes, dumps metadata, and also
searches raw bytes. A leak only one of these paths can see still counts.

Exit code 1 if any seeded value survives.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

FUZZY_MIN_LEN = 8  # shorter values need an exact hit; 1 edit on "12345" is too loose
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def normalize(value: str) -> str:
    """Case-fold and drop everything but letters and digits.

    OCR drops spaces and the redactor may reflow lines, so "4111 1111" and "41111111"
    must compare equal. Must match the engine's normalisation for --spans digests.
    """
    return re.sub(r"[\W_]+", "", value.casefold())


def fuzzy_contains(needle: str, hay: str, k: int = 1) -> bool:
    """True if `needle` occurs in `hay` with at most k edits (k=1 here).

    Pigeonhole prefilter: with one edit, at least one half of the needle appears verbatim,
    so the O(n*m) edit-distance scan only runs on short windows around those hits.
    """
    if needle in hay:
        return True
    if len(needle) < FUZZY_MIN_LEN:
        return False
    mid = len(needle) // 2
    for half in (needle[:mid], needle[mid:]):
        start = hay.find(half)
        while start != -1:
            window = hay[max(0, start - len(needle) - k): start + len(needle) + k]
            if _edit_distance_substring(needle, window) <= k:
                return True
            start = hay.find(half, start + 1)
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


# --- extractors: each returns {source_name: text} -------------------------------------

def extract(path: Path) -> dict[str, str]:
    data = path.read_bytes()
    # Raw bytes catch metadata and uncompressed streams no parser was asked about.
    found = {"bytes": data.decode("latin-1") + data.decode("utf-16-le", "ignore")}
    suffix = path.suffix.lower()
    if suffix == ".docx":
        found["docx_xml"] = _docx_text(data)
    elif suffix == ".pdf":
        found.update(_pdf_text(data))
    elif suffix in IMAGE_SUFFIXES:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            found.update(_image_text(img))
    else:
        found["text"] = data.decode("utf-8", "replace")
    return found


def _docx_text(data: bytes) -> str:
    """Text nodes plus attribute values: comment and revision authors live in attributes."""
    parts = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if name.endswith((".xml", ".rels")):
                xml = z.read(name).decode("utf-8", "replace")
                parts.append(re.sub(r"<[^>]+>", "", xml))  # adjacent runs join up
                parts.extend(re.findall(r'="([^"]*)"', xml))
    return html.unescape(" ".join(parts))


def _pdf_text(data: bytes) -> dict[str, str]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    layer, ocr = [], []
    for page in pdf:
        layer.append(page.get_textpage().get_text_range())
        img = page.render(scale=300 / 72).to_pil()
        ocr.append(_ocr(img, rotations=(0,)))
    meta = " ".join(str(v) for v in pdf.get_metadata_dict().values())
    return {"pdf_text_layer": "\n".join(layer), "pdf_ocr": "\n".join(ocr), "pdf_meta": meta}


def _image_text(img) -> dict[str, str]:
    import zxingcpp

    exif = img.getexif()
    meta = [str(v) for v in exif.values()]
    meta += [str(v) for v in exif.get_ifd(0x8825).values()]  # GPS IFD
    meta += [str(v) for k, v in img.info.items() if k != "exif"]  # PNG text chunks
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
        manifest["documents"] = [d for d in manifest["documents"] if d["format"] in formats]
    per = defaultdict(lambda: {"seeded": 0, "removed": 0})
    survivors, missing = [], []
    for doc in manifest["documents"]:
        outs = outputs_for(doc["file"], outputs)
        if not outs:
            missing.append(doc["file"])  # no output means nothing was checked: fail loud
        sources = {f"{o.name}:{src}": normalize(text) for o in outs for src, text in extract(o).items()}
        for s in doc["seeded"]:
            key = (doc["format"], s["entity_type"])
            per[key]["seeded"] += 1
            hits = [src for src, hay in sources.items() if fuzzy_contains(normalize(s["value"]), hay)]
            if hits or not outs:
                survivors.append({**s, "file": doc["file"], "found_in": hits or ["<no output>"]})
            else:
                per[key]["removed"] += 1
    return {
        "recall": {f"{f}/{e}": v for (f, e), v in sorted(per.items())},
        "precision": _precision(manifest, spans_file),
        "survivors": survivors,
        "missing_outputs": missing,
    }


def _precision(manifest: dict, spans_file: Path | None) -> dict | None:
    """Share of redacted spans that hit a seeded value, per format.

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
        f"Survivors: **{len(result['survivors'])}** of {total} seeded values.", "",
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
    formats = set(args.formats.split(",")) if args.formats else None
    result = run(args.corpus, args.outputs, args.spans, formats)
    print(write_report(result, args.report))
    return 1 if result["survivors"] or result["missing_outputs"] else 0


if __name__ == "__main__":
    sys.exit(main())
