"""Charts for the timing report: SVG files that follow the viewer's light or dark theme.

    py -3.12 -m uv run python tests/bench/charts.py docs/perf/phase-3-timing.json docs/perf

Hand-written SVG, so the report needs no plotting library (and no new license to vet).
Colours are the categorical slots of a colour-blind-checked palette, in its fixed order.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

W = 760
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
LIGHT = {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
         "axis": "#c3c2b7", "band": "#f3f2ee", "band2": "#ebeae4",
         "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a", "s4": "#eda100", "s5": "#e87ba4", "s6": "#008300",
         "s7": "#4a3aa7", "other": "#c3c2b7"}
DARK = {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
        "axis": "#383835", "band": "#222221", "band2": "#2a2a28",
        "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70", "s4": "#c98500", "s5": "#d55181", "s6": "#008300",
        "s7": "#9085e9", "other": "#52514e"}
STAGES = [  # (label, colour, source stages)
    ("OCR, upright", "s1", ["OCR, upright pass"]),
    ("OCR, turned text", "s2", ["OCR, turned passes"]),
    ("OCR, small text (2x)", "s3", ["OCR, 2x pass for small text"]),
    ("Name and address model", "s4", ["Name and address model"]),
    ("Patterns and checksums", "s5", ["Patterns and checksums"]),
    ("Faces and barcodes", "s6", ["Faces", "Barcodes"]),
    ("Render, paint, write", "s7", ["Render page", "Paint boxes", "Write page", "Word to Markdown"]),
    ("Other", "other", ["Other"]),
]
GROUPS = [("text", "Paste"), ("docx", "Word"), ("pdf", "PDF"), ("image", "Image")]
MB = 2**20
CHROME_CAP = 1 * MB  # Chrome's limit on one native message from the host to the extension


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def secs(s: float) -> str:
    return f"{s:.2f} s" if s < 1 else f"{s:.1f} s" if s < 10 else f"{s:.0f} s"


def text_width(s: str, size: int = 12) -> float:
    return len(s) * size * 0.55  # close enough for a system sans; used only to keep labels apart


class Svg:
    def __init__(self, height: int, title: str, desc: str) -> None:
        self.h, self.parts = height, []
        rules = lambda c: "".join(f"--{k}:{v};" for k, v in c.items())  # noqa: E731
        self.head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" viewBox="0 0 {W} {height}" '
            f'role="img" aria-labelledby="t d"><title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>'
            f"<style>svg{{{rules(LIGHT)}}}@media (prefers-color-scheme: dark){{svg{{{rules(DARK)}}}}}"
            f"text{{font-family:{FONT};font-size:12px;fill:var(--ink2)}}.h{{font-size:15px;font-weight:600;fill:var(--ink)}}"
            ".sub{fill:var(--muted)}.v{fill:var(--ink);font-variant-numeric:tabular-nums}.m{fill:var(--muted);font-size:11px}"
            "</style>"
            f'<rect width="{W}" height="{height}" rx="8" fill="var(--surface)"/>'
        )

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, cls="", anchor="start", weight=None) -> None:
        w = f' font-weight="{weight}"' if weight else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" class="{cls}"{w}>{esc(s)}</text>')

    def bar(self, x, y, w, h, colour, round_end=True) -> None:
        """A horizontal bar, square at its baseline and rounded 4 px at its data end."""
        if w <= 0:
            return
        r = min(4, w / 2, h / 2) if round_end else 0
        self.add(f'<path d="M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}'
                 f'a{r},{r} 0 0 1 -{r},{r}h-{w - r:.1f}z" fill="var(--{colour})"/>')

    def line(self, x1, y1, x2, y2, colour="grid", width=1) -> None:
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="var(--{colour})" '
                 f'stroke-width="{width}" stroke-linecap="round"/>')

    def dot(self, x, y, colour, hollow=False) -> None:
        fill = "var(--surface)" if hollow else f"var(--{colour})"
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{fill}" stroke="var(--{colour if hollow else "surface"})" '
                 f'stroke-width="2"/>')

    def save(self, path: Path) -> None:
        path.write_text(self.head + "".join(self.parts) + "</svg>\n", encoding="utf-8")


def nice_ticks(vmax: float) -> list[float]:
    """0 and two or three round steps (1, 2 or 5 times a power of ten) reaching past `vmax`."""
    raw = vmax / 2.5
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    return [step * i for i in range(math.ceil(vmax / step) + 1)]


def ordered(report: dict) -> list[tuple[str, list[dict]]]:
    rows = report["scenarios"]
    return [(name, [s for s in rows if s["group"] == g]) for g, name in GROUPS if any(s["group"] == g for s in rows)]


def speed(report: dict, out: Path) -> None:
    """Dot plot on a log scale: the typical time per input, and the first file after a start."""
    groups = ordered(report)
    rows = sum(len(g) for _, g in groups)
    top, row_h, left, right = 96, 24, 238, W - 70
    height = top + rows * row_h + len(groups) * 18 + 44
    svg = Svg(height, "How long each input takes to redact",
              "Median time per input on a log scale, with the slower first file after the engine starts.")
    svg.text(20, 30, "How long each input takes to redact", "h")
    svg.text(20, 50, "Warm engine, median of repeated runs; the ring is the first file after the engine starts", "sub")
    lo, hi = math.log10(0.03), math.log10(300)
    x = lambda s: left + (math.log10(max(s, 0.03)) - lo) / (hi - lo) * (right - left)  # noqa: E731
    bottom = height - 40
    for a, b, label, fill in ((0.03, 1, "under 1 s: flow unbroken", "band"), (1, 10, "1-10 s: a noticeable wait", "band2"),
                              (10, 300, "over 10 s: attention drifts", "band")):
        svg.add(f'<rect x="{x(a):.1f}" y="{top - 22}" width="{x(b) - x(a):.1f}" height="{bottom - top + 22}" '
                f'fill="var(--{fill})"/>')
        svg.text((x(a) + x(b)) / 2, top - 8, label, "m", "middle")
    for t, label in ((0.1, "0.1 s"), (1, "1 s"), (10, "10 s"), (60, "1 min")):
        svg.line(x(t), top - 22, x(t), bottom, "grid")
        svg.text(x(t), bottom + 16, label, "m", "middle")
    svg.line(left, bottom, right, bottom, "axis")
    y = top + 4
    for name, scenarios in groups:
        svg.text(20, y + 8, name, "v", weight=600)
        y += 18
        for s in scenarios:
            cy = y + row_h / 2 - 4
            svg.text(32, cy + 4, s["label"])
            svg.line(x(s["min_s"]), cy, x(s["max_s"]), cy, "s1", 2)
            first_slower = s["warmup_s"] - s["median_s"] >= 0.5  # a ring only where the first file costs more
            if first_slower:
                svg.dot(x(s["warmup_s"]), cy, "s2", hollow=True)
            svg.dot(x(s["median_s"]), cy, "s1")
            label_x = max(x(s["max_s"]), x(s["warmup_s"]) if first_slower else 0) + 10
            svg.text(min(label_x, W - 52), cy + 4, secs(s["median_s"]), "v")
            y += row_h
    svg.dot(25, 70, "s1")
    svg.text(35, 74, "Typical run (line: fastest to slowest)", "sub")
    svg.dot(265, 70, "s2", hollow=True)
    svg.text(275, 74, "First file after the engine starts", "sub")
    svg.save(out / "speed.svg")


def stages(report: dict, out: Path) -> None:
    """Share of each input's time by stage, as 100% stacked bars with the total at the end."""
    groups = ordered(report)
    rows = sum(len(g) for _, g in groups)
    top, row_h, bar_h, left, right = 112, 24, 14, 238, W - 64
    height = top + rows * row_h + len(groups) * 18 + 24
    svg = Svg(height, "Where the time goes", "Share of each input's redaction time spent in each stage.")
    svg.text(20, 30, "Where the time goes", "h")
    svg.text(20, 50, "Share of each input's time by stage; the total is at the right", "sub")
    lx, ly = 20, 72
    for label, colour, _ in STAGES:  # legend, wrapped into rows
        w = 16 + text_width(label, 11) + 18
        if lx + w > W - 20:
            lx, ly = 20, ly + 18
        svg.add(f'<rect x="{lx}" y="{ly - 9}" width="10" height="10" rx="2" fill="var(--{colour})"/>')
        svg.text(lx + 15, ly, label, "m")
        lx += w
    y = top
    for name, scenarios in groups:
        svg.text(20, y + 8, name, "v", weight=600)
        y += 18
        for s in scenarios:
            cy = y + (row_h - bar_h) / 2
            svg.text(32, cy + bar_h - 3, s["label"])
            total = sum(s["stages_s"].values()) or 1
            xs = left
            parts = [(c, sum(s["stages_s"].get(k, 0) for k in keys)) for _, c, keys in STAGES]
            parts = [(c, v) for c, v in parts if v / total * (right - left) >= 1]
            for i, (colour, v) in enumerate(parts):
                w = v / total * (right - left)
                svg.bar(xs, cy, max(w - 2, 0.5), bar_h, colour, round_end=i == len(parts) - 1)
                xs += w
            svg.text(right + 8, cy + bar_h - 3, secs(s["median_s"]), "v")
            y += row_h
    svg.save(out / "stages.svg")


def cold_start(report: dict, out: Path) -> None:
    """From launching the engine to its first redaction, by stage."""
    cold = report["cold_start"]
    images = [s for s in report["scenarios"] if s["group"] == "image"]
    first_ocr = min((s["warmup_s"] - s["median_s"] for s in images), default=0)
    order = ["Python start", "Imports", "Model hash check", "Name model load", "Presidio and spaCy load",
             "Vault, policy and audit"]
    parts = [(k, cold["stages_s"].get(k, 0.0)) for k in order] + [("OCR models, on the first image", first_ocr)]
    colours = ["other", "s1", "s2", "s3", "s4", "s5", "s7"]
    total = sum(v for _, v in parts)
    left, right, top = 20, W - 20, 78
    height = top + 40 + 22 * ((len(parts) + 1) // 2) + 20
    svg = Svg(height, "Cold start", "Seconds from launching the engine to its first redaction, by stage.")
    svg.text(20, 30, f"Cold start: {secs(cold['median_s'])} to a loaded engine, "
                     f"{secs(cold['median_s'] + first_ocr)} to a first image", "h")
    svg.text(20, 50, "What a native host pays once if it stays open, or on every request if it does not", "sub")
    xs = left
    for i, ((label, v), colour) in enumerate(zip(parts, colours)):
        w = v / total * (right - left)
        svg.bar(xs, top, max(w - 2, 0.5), 18, colour, round_end=i == len(parts) - 1)
        xs += w
    for i, ((label, v), colour) in enumerate(zip(parts, colours)):
        cx, cy = left + (i % 2) * 370, top + 48 + (i // 2) * 22
        svg.add(f'<rect x="{cx}" y="{cy - 9}" width="10" height="10" rx="2" fill="var(--{colour})"/>')
        svg.text(cx + 16, cy, label)
        svg.text(cx + 330, cy, secs(v), "v", "end")
    svg.save(out / "cold-start.svg")


def scaling(report: dict, out: Path) -> None:
    """Three small multiples: paste size, pages, and screenshot pixels against time."""
    by_id = {s["id"]: s for s in report["scenarios"]}
    panels = [
        ("Paste size", [("Paste", "s1", [(by_id[i]["input"]["bytes"] / 1000, by_id[i]["median_s"])
                                         for i in ("text_200b", "text_2kb", "text_20kb", "text_200kb") if i in by_id])],
         "KB (log)", True),
        ("Pages", [("Word", "s2", [(by_id[i]["input"]["pages"], by_id[i]["median_s"])
                                   for i in ("docx_1p", "docx_10p", "docx_50p") if i in by_id]),
                   ("PDF", "s3", [(by_id[i]["input"]["pages"], by_id[i]["median_s"])
                                  for i in ("pdf_digital_1p", "pdf_digital_4p", "pdf_digital_10p") if i in by_id])],
         "pages", False),
        ("Screenshot size", [("Screenshot", "s1", [(by_id[i]["input"]["pixels"] / 1e6, by_id[i]["median_s"])
                                                   for i in ("img_720p", "img_1080p", "img_1440p", "img_4k")
                                                   if i in by_id])],
         "megapixels", False),
    ]
    height, pw, gap, top, ph = 310, 200, 50, 84, 160
    svg = Svg(height, "How time grows with input size",
              "Median seconds against paste size, page count and screenshot megapixels.")
    svg.text(20, 30, "How time grows with input size", "h")
    svg.text(20, 50, "Median seconds; every axis starts at zero except paste size", "sub")
    for n, (title, series, unit, log_x) in enumerate(panels):
        x0 = 48 + n * (pw + gap)
        pts = [p for _, _, ps in series for p in ps]
        if not pts:
            continue
        xmax = max(p[0] for p in pts)
        ticks = nice_ticks(max(p[1] for p in pts))
        ymax = ticks[-1]
        lo = min(p[0] for p in pts)
        fx = ((lambda v: x0 + (math.log10(v) - math.log10(lo)) / (math.log10(xmax) - math.log10(lo) or 1) * pw)
              if log_x else (lambda v: x0 + v / xmax * pw))
        fy = lambda v: top + ph - v / ymax * ph  # noqa: E731
        svg.text(x0, top - 12, title, "v", weight=600)
        for yv in ticks:
            svg.line(x0, fy(yv), x0 + pw, fy(yv), "grid" if yv else "axis")
            svg.text(x0 - 4, fy(yv) + 4, f"{yv:g} s" if yv else "0", "m", "end")
        last = -99.0
        for v in sorted({p[0] for p in pts}):  # label the measured sizes, skipping any that would touch
            if fx(v) - last >= 22:
                svg.text(fx(v), top + ph + 16, f"{v:g}" if v == int(v) else f"{v:.1f}", "m", "middle")
                last = fx(v)
        svg.text(x0 + pw, top + ph + 30, unit, "m", "end")
        for label, colour, ps in series:
            if len(ps) > 1:
                path = " ".join(f"{'M' if i == 0 else 'L'}{fx(a):.1f},{fy(b):.1f}" for i, (a, b) in enumerate(ps))
                svg.add(f'<path d="{path}" fill="none" stroke="var(--{colour})" stroke-width="2" '
                        'stroke-linejoin="round" stroke-linecap="round"/>')
            for a, b in ps:
                svg.dot(fx(a), fy(b), colour)
            a, b = ps[-1]
            svg.text(fx(a) - 6, fy(b) - 10, f"{label} {secs(b)}", "v", "end")
    svg.save(out / "scaling.svg")


def sizes(report: dict, out: Path) -> None:
    """Output size per file against Chrome's 1 MB native-message cap, and peak memory."""
    files = [s for s in report["scenarios"] if s["group"] in ("pdf", "image")]
    resting = sorted(s["memory_after_load_mb"] for s in report["scenarios"])[len(report["scenarios"]) // 2]
    for name, title, sub, value, rule, rule_label in (
        ("output-size.svg", "Output size against Chrome's 1 MB message cap",
         "Redacted file plus its Markdown; past the line, the native host must send it in chunks",
         lambda s: s["output_bytes"] / MB, CHROME_CAP / MB, "1 MB cap"),
        ("memory.svg", "Peak memory per input", "Working set of the engine process, model loaded",
         lambda s: s["memory_peak_mb"], resting, "engine loaded"),
    ):
        rows = files if name == "output-size.svg" else report["scenarios"]
        top, row_h, left, right = 84, 22, 238, W - 80
        height = top + len(rows) * row_h + 34
        svg = Svg(height, title, sub)
        svg.text(20, 30, title, "h")
        svg.text(20, 50, sub, "sub")
        vmax = max([value(s) for s in rows] + [rule or 0]) * 1.08 or 1
        fx = lambda v: left + v / vmax * (right - left)  # noqa: E731
        for i, s in enumerate(rows):
            cy = top + i * row_h
            svg.text(20, cy + 12, s["label"])
            svg.bar(left, cy + 2, fx(value(s)) - left, 14, "s1")
            v = value(s)
            svg.text(fx(v) + 6, cy + 13, f"{v:.1f} MB" if v < 100 else f"{v:,.0f} MB", "v")
        bottom = top + len(rows) * row_h
        svg.line(left, top - 4, left, bottom, "axis")
        if rule:
            svg.line(fx(rule), top - 8, fx(rule), bottom, "s2", 2)
            svg.text(fx(rule) + 4, top - 10, f"{rule_label} ({rule:,.0f} MB)" if rule > 10 else rule_label, "m")
        svg.save(out / name)


def main() -> None:
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for chart in (speed, stages, cold_start, scaling, sizes):
        chart(report, out)
    print(sorted(p.name for p in out.glob("*.svg")))


if __name__ == "__main__":
    main()
