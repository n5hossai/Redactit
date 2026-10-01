"""Timing benchmark: how long each format takes to redact, where the time goes, and what it
costs in memory and output size. Synthetic inputs only.

    py -3.12 -m uv run --group leak python tests/bench/timing.py --out docs/perf/phase-3-timing.json

Every scenario runs in a fresh process: the engine loads, one untimed warm-up call runs
(it also loads the OCR models on first use), then the timed calls. That is what a native
host that stays open between requests would see. Cold start is timed on its own, from
launching Python to a loaded engine.

Stage times come from wrapping the leaf functions (OCR passes, the two detectors, faces,
barcodes, page render and write), so production code is not touched. Whatever the leaves
do not cover (decoding, encoding, policy, pseudonyms, audit) is reported as "other".
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "corpus"))  # the corpus value factory

SEED = 20260930
FACES = Path(__file__).resolve().parents[1] / "fixtures" / "faces"

# (id, label, group, repeats). Groups are what the extension would hand over: a paste, or an attachment.
SCENARIOS = [
    ("text_200b", "Paste, 200 B", "text", 10),
    ("text_2kb", "Paste, 2 KB", "text", 10),
    ("text_20kb", "Paste, 20 KB", "text", 5),
    ("text_200kb", "Paste, 200 KB (a log)", "text", 3),
    ("docx_1p", "Word, 1 page", "docx", 5),
    ("docx_10p", "Word, 10 pages", "docx", 3),
    ("docx_50p", "Word, 50 pages", "docx", 2),
    ("pdf_digital_1p", "PDF, 1 digital page", "pdf", 3),
    ("pdf_scanned_1p", "PDF, 1 scanned page", "pdf", 3),
    ("pdf_digital_4p", "PDF, 4 digital pages", "pdf", 2),
    ("pdf_digital_10p", "PDF, 10 digital pages", "pdf", 1),
    ("img_720p", "Screenshot, 1280 x 720", "image", 3),
    ("img_1080p", "Screenshot, 1920 x 1080", "image", 3),
    ("img_1440p", "Screenshot, 2560 x 1440", "image", 3),
    ("img_4k", "Screenshot, 3840 x 2160", "image", 3),
    ("photo_1mp", "Photo with face and text, 1 MP", "image", 3),
    ("photo_12mp", "Phone photo, 12 MP", "image", 2),
]
COLD_RUNS = 3
PAGE_CHARS = 3000  # about one page of prose


# --- inputs -------------------------------------------------------------------------------

def _vf():
    from generate import ValueFactory

    return ValueFactory(SEED)


def _prose(vf, size: int) -> str:
    """Prose with a name, email, phone and card in every paragraph, cut to `size` characters."""
    parts, total = [], 0
    while total < size:
        parts.append(f"{vf.filler_paragraph()} Contact {vf.person()} at {vf.email()} or {vf.phone()}; "
                     f"card {vf.credit_card()}.")
        total += len(parts[-1]) + 2
    return "\n\n".join(parts)[:size]


def _docx(vf, pages: int) -> bytes:
    from corpus_docx import _document_xml, _p

    body = "".join(_p(para) for para in _prose(vf, pages * PAGE_CHARS).split("\n\n"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", _document_xml(body))
    return buf.getvalue()


def _pdf(vf, pages: int, scanned: bool) -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf, (width, height) = io.BytesIO(), LETTER
    c = canvas.Canvas(buf, pagesize=LETTER)
    for _ in range(pages):
        lines = [line for para in _prose(vf, PAGE_CHARS).split("\n\n") for line in _wrap(para, 90)][:44]
        if scanned:  # a page image at 200 DPI, as a scanner makes it
            img = Image.new("RGB", (1700, 2200), "white")
            draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=30)
            for i, line in enumerate(lines):
                draw.text((150, 150 + 44 * i), line, font=font, fill="black")
            c.drawImage(ImageReader(img), 0, 0, width=width, height=height)
        else:
            c.setFont("Helvetica", 10)
            for i, line in enumerate(lines):
                c.drawString(72, height - 72 - 14.5 * i, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width) or [""]


def _screenshot(vf, size: tuple[int, int]) -> bytes:
    """A light chat window, 15 px UI text filling the message pane, as the user would capture it."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = size
    img = Image.new("RGB", size, (250, 250, 250))
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=15)
    draw.rectangle((0, 0, w // 5, h), fill=(236, 236, 240))  # the conversation list
    for i, y in enumerate(range(40, h - 40, 120)):
        draw.text((16, y), vf.person(), font=font, fill=(40, 40, 40))
    lines = [line for para in _prose(vf, 40 * PAGE_CHARS).split("\n\n") for line in _wrap(para, w // 12)]
    for i, y in enumerate(range(30, h - 30, 26)):
        draw.text((w // 5 + 40, y), lines[i % len(lines)], font=font, fill=(30, 30, 30))
    return _png(img)


def _photo(vf, size: tuple[int, int]) -> bytes:
    """A camera-like photo: textured background, a face, and a sign with a few words."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    rng = np.random.default_rng(SEED)
    w, h = size
    grad = np.linspace(90, 200, w, dtype=np.float32)[None, :, None] + rng.normal(0, 18, (h, w, 3))
    img = Image.fromarray(np.clip(grad, 0, 255).astype(np.uint8))
    face = Image.open(sorted(FACES.glob("*.jpg"))[0]).convert("RGB").resize((h // 3, h // 3))
    img.paste(face, (w // 8, h // 4))
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=max(24, h // 40))
    sx, sy = w // 2, h // 3
    draw.rectangle((sx, sy, sx + w // 3, sy + h // 5), fill="white")
    for i, text in enumerate([vf.person(), vf.phone(), vf.email()]):
        draw.text((sx + 20, sy + 20 + i * (h // 16)), text, font=font, fill="black")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def _png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def build_input(scenario: str) -> tuple[str, bytes | str, dict]:
    """(kind, data, description) for one scenario id."""
    vf = _vf()
    if scenario.startswith("text_"):
        size = {"text_200b": 200, "text_2kb": 2000, "text_20kb": 20_000, "text_200kb": 200_000}[scenario]
        text = _prose(vf, size)
        return "text", text, {"bytes": len(text.encode())}
    if scenario.startswith("docx_"):
        pages = int(scenario.split("_")[1].rstrip("p"))
        data = _docx(vf, pages)
        return "docx", data, {"bytes": len(data), "pages": pages}
    if scenario.startswith("pdf_"):
        _, kind, pages = scenario.split("_")
        data = _pdf(vf, int(pages.rstrip("p")), scanned=kind == "scanned")
        return "pdf", data, {"bytes": len(data), "pages": int(pages.rstrip("p"))}
    sizes = {"img_720p": (1280, 720), "img_1080p": (1920, 1080), "img_1440p": (2560, 1440),
             "img_4k": (3840, 2160), "photo_1mp": (1000, 900), "photo_12mp": (4000, 3000)}
    size = sizes[scenario]
    if scenario == "photo_1mp":
        from corpus_media import build_image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "photo.png"
            build_image("png", vf, path)
            data = path.read_bytes()
    else:
        data = _screenshot(vf, size) if scenario.startswith("img_") else _photo(vf, size)
    return "image", data, {"bytes": len(data), "pixels": size[0] * size[1]}


# --- measuring ----------------------------------------------------------------------------

class Stages:
    """Seconds spent in each wrapped function, summed per stage until reset."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)

    def wrap(self, owner, name: str, stage) -> None:
        fn = getattr(owner, name)

        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.seconds[stage(*args, **kwargs) if callable(stage) else stage] += time.perf_counter() - start

        setattr(owner, name, timed)

    def take(self) -> dict[str, float]:
        out, self.seconds = dict(self.seconds), defaultdict(float)
        return out


def _ocr_pass(view, turns, scale):
    return "OCR, 2x pass for small text" if scale > 1 else ("OCR, turned passes" if turns else "OCR, upright pass")


def memory_mb() -> tuple[float | None, float]:
    """(current, peak) resident memory of this process in MB."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

        c = Counters(cb=ctypes.sizeof(Counters))
        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE  # a 64-bit pseudo-handle; ctypes' default int truncates it
        info = ctypes.windll.psapi.GetProcessMemoryInfo
        info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not info(current(), ctypes.byref(c), c.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return c.WorkingSetSize / 2**20, c.PeakWorkingSetSize / 2**20
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return None, peak / (2**20 if sys.platform == "darwin" else 2**10)


def _engine(tmp: Path, verified=None):
    import yaml
    from redactit.audit import AuditLog
    from redactit.pipeline import Engine
    from redactit.policy import DEFAULT_POLICY, load_policy
    from redactit.vault import Vault

    policy = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))  # the defaults, at dial 3
    (tmp / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    vault = Vault(tmp / "vault.db", os.urandom(32))  # a throwaway key: never the real keychain
    return Engine(load_policy(tmp / "policy.yaml"), vault, AuditLog(tmp / "audit.jsonl"), verified=verified)


def run_scenario(scenario: str, repeats: int) -> dict:
    from redactit import ocr
    from redactit.formats import docx, image, pdf

    kind, data, described = build_input(scenario)
    with tempfile.TemporaryDirectory() as tmp:
        engine = _engine(Path(tmp))
        stages = Stages()
        stages.wrap(engine.detector, "detect", "Patterns and checksums")
        stages.wrap(engine.ner, "detect", "Name and address model")
        stages.wrap(ocr, "_read", _ocr_pass)
        stages.wrap(image, "_faces", "Faces")
        stages.wrap(image, "_barcodes", "Barcodes")
        for module in (image, pdf):
            stages.wrap(module, "paint", "Paint boxes")
        stages.wrap(pdf.pdfium.PdfPage, "render", "Render page")
        stages.wrap(pdf, "_append_page", "Write page")
        stages.wrap(docx, "docx_to_markdown", "Word to Markdown")

        def call() -> int:
            if kind == "text":
                return len(engine.redact(data, "bench", file_type="txt").text.encode())
            if kind == "docx":
                text = docx.docx_to_markdown(data)
                return len(engine.redact(text, "bench", file_type="docx").text.encode())
            if kind == "pdf":
                out, markdown = pdf.redact_pdf(data, engine, "bench")
                return len(out) + len(markdown.encode())
            out, _, text = image.redact_image(data, engine, "bench")
            return len(out) + len(text.encode())

        after_load = memory_mb()[0]
        start = time.perf_counter()
        call()  # warm-up: first OCR use loads its models
        warmup = time.perf_counter() - start
        stages.take()
        runs, per_stage, out_bytes = [], defaultdict(list), 0
        for _ in range(repeats):
            start = time.perf_counter()
            out_bytes = call()
            runs.append(time.perf_counter() - start)
            for stage, seconds in stages.take().items():
                per_stage[stage].append(seconds)
        current, peak = memory_mb()
        engine.vault.close()  # Windows cannot delete an open file
    stage_median = {s: statistics.median(v + [0.0] * (repeats - len(v))) for s, v in per_stage.items()}
    stage_median["Other"] = max(statistics.median(runs) - sum(stage_median.values()), 0.0)
    return {"id": scenario, "kind": kind, "input": described, "repeats": repeats, "warmup_s": warmup,
            "runs_s": runs, "median_s": statistics.median(runs), "min_s": min(runs), "max_s": max(runs),
            "stages_s": stage_median, "memory_after_load_mb": after_load, "memory_now_mb": current,
            "memory_peak_mb": peak, "output_bytes": out_bytes}


def run_cold() -> dict:
    """Imports and engine load in this fresh process, by stage."""
    marks = {"process_start": time.perf_counter()}
    from redactit import models

    pending = models.verify(models.TEXT_MODELS)  # as the interfaces do: hashed while the imports run
    from redactit import pipeline  # noqa: F401 (the import cost is what is measured)

    marks["imported"] = time.perf_counter()
    stages = Stages()
    # What the hash still costs after the imports, plus the second hash where files cannot be held.
    stages.wrap(models.Verification, "__enter__", "Model hash check")
    stages.wrap(models.Verification, "__exit__", "Model hash check")
    stages.wrap(pipeline, "Detector", "Presidio and spaCy load")
    stages.wrap(pipeline, "GlinerNer", "Name model load")
    with tempfile.TemporaryDirectory() as tmp:
        start = time.perf_counter()
        engine = _engine(Path(tmp), pending)
        load = time.perf_counter() - start
        engine.vault.close()  # Windows cannot delete an open file
    out = {"Imports": marks["imported"] - marks["process_start"], **stages.take()}
    out["Vault, policy and audit"] = max(load - sum(v for k, v in out.items() if k != "Imports"), 0.0)
    return {"stages_s": out, "memory_after_load_mb": memory_mb()[0]}


# --- driver -------------------------------------------------------------------------------

def _child(args: list[str]) -> tuple[dict, float]:
    start = time.perf_counter()
    proc = subprocess.Popen([sys.executable, __file__, *args], stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline()  # the result is printed before the process tears down
    wall = time.perf_counter() - start
    proc.wait()
    if proc.returncode or not line:
        raise SystemExit(f"benchmark child {args} failed ({proc.returncode})")
    return json.loads(line), wall


def _machine() -> dict:
    cpu = platform.processor()
    if sys.platform == "win32":
        try:
            cpu = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                                 capture_output=True, text=True, timeout=30).stdout.strip() or cpu
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"cpu": cpu, "logical_cpus": os.cpu_count(), "os": platform.platform(), "python": platform.python_version()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path)
    ap.add_argument("--scenario", help=argparse.SUPPRESS)
    ap.add_argument("--repeats", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--cold", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--only", help="comma-separated scenario ids")
    args = ap.parse_args()
    if args.cold:
        print(json.dumps(run_cold()), flush=True)
        return
    if args.scenario:
        print(json.dumps(run_scenario(args.scenario, args.repeats)), flush=True)
        return
    only = set(args.only.split(",")) if args.only else None
    cold = []
    for _ in range(COLD_RUNS):
        result, wall = _child(["--cold"])
        result["stages_s"]["Python start"] = max(wall - sum(result["stages_s"].values()), 0.0)
        cold.append(result | {"total_s": wall})
        print(f"cold start {wall:.1f} s", file=sys.stderr)
    scenarios = []
    for scenario, label, group, repeats in SCENARIOS:
        if only and scenario not in only:
            continue
        result, _ = _child(["--scenario", scenario, "--repeats", str(repeats)])
        scenarios.append(result | {"label": label, "group": group})
        print(f"{label}: median {result['median_s']:.2f} s", file=sys.stderr)
    stage_names = {s for c in cold for s in c["stages_s"]}
    report = {
        "date": date.today().isoformat(),
        "machine": _machine(),
        "cold_start": {"runs_s": [c["total_s"] for c in cold],
                       "median_s": statistics.median(c["total_s"] for c in cold),
                       "stages_s": {s: statistics.median(c["stages_s"].get(s, 0.0) for c in cold) for s in stage_names},
                       "memory_after_load_mb": statistics.median(c["memory_after_load_mb"] or 0 for c in cold)},
        "scenarios": scenarios,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
