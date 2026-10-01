# Phase 3 timing: how fast each format redacts

Measured on 2026-09-30 with `tests/bench/timing.py`. Raw numbers are in
[`phase-3-timing.json`](phase-3-timing.json).

- **Machine:** AMD Ryzen 7 7840HS (8 cores, 16 threads), 15 GB RAM, Windows 11, CPU only.
- **Setup:** Python 3.12, default policy at dial 3, synthetic inputs only.
- **Method:** each input ran in a fresh process, with the engine loaded and one untimed
  warm-up call before the timed runs. That is what a native host that stays open between
  requests would see.

## Summary

| Input | Typical (median) | First file after a start | Verdict for the extension |
|---|---:|---:|---|
| Paste, 200 B | 0.07 s | 0.10 s | Instant |
| Paste, 2 KB | 0.62 s | 0.75 s | Fine, under 1 s |
| Paste, 20 KB | 6.8 s | 8.7 s | Too slow for a paste |
| Paste, 200 KB (a log) | 72 s | 81 s | Far too slow |
| Word, 1 / 10 / 50 pages | 0.92 / 9.6 / 51 s | 1.1 / 11 / 57 s | About 1 s per page |
| PDF, 1 digital page | 12 s | 17 s | Too slow |
| PDF, 1 scanned page | 10 s | 16 s | Too slow |
| PDF, 4 / 10 digital pages | 46 / 116 s | 63 / 156 s | About 11 s per page |
| Screenshot, 1280 x 720 | 5.1 s | 9.3 s | Slow |
| Screenshot, 1920 x 1080 | 9.5 s | 16 s | Too slow |
| Screenshot, 2560 x 1440 | 18 s | 27 s | Too slow |
| Screenshot, 3840 x 2160 | 38 s | 55 s | Far too slow |
| Photo with face and text, 1 MP | 2.3 s | 4.2 s | Acceptable |
| Phone photo, 12 MP | 15 s | 18 s | Too slow |

The verdicts use the usual response-time limits:
- **Under 1 s**, the user's flow is not broken.
- **1 to 10 s**, they notice the wait but stay on the task.
- **Over 10 s**, attention drifts, so the user needs progress and a way to cancel.

![How long each input takes](speed.svg)

## Where the time goes

![Where the time goes](stages.svg)

- **Text and Word:** the name and address model takes 73-82% of the time, and the
  patterns most of the rest. Both grow linearly with length, at about 0.35 s per KB of
  text, so a 200 KB log takes over a minute.
- **Screenshots:**
  - The upright OCR pass is 61-69% of the time, and the turned passes another 12-19%.
  - Time grows with pixels: 5 s at 0.9 MP, 38 s at 8.3 MP.
  - These screenshots are dense with text, as chat screenshots are.
- **PDFs:**
  - About 11 s per page, digital or scanned. OCR of the 200 DPI page is 73-88% of that.
  - On a digital page the name model runs twice, once on the text layer and once on the
    OCR text: 2.1 s.
- **Phone photo:** the turned passes are 67% of the time. On a textured background
  nothing reads upright with confidence, so the turned passes cover the whole photo.

![How time grows with input size](scaling.svg)

## Cold start and memory

![Cold start](cold-start.svg)

- **Load:** 6.1 s from launching the engine to a loaded engine.
  - Python start 1.6 s
  - imports 2.2 s
  - name model 1.5 s
  - model hash check 0.55 s
  - Presidio and spaCy 0.34 s
- **First file:** the first file after a start is slower than the typical run while the OCR
  models load and the runtime warms up. It costs 2 s more for a small photo, 17 s for a 4K
  screenshot, and 40 s for a 10-page PDF.
- **Implication:** if the native host started per request, every request would pay all
  of this.

![Peak memory](memory.svg)

- **At rest:** the loaded engine holds about 940 MB.
- **Peak:**
  - 1.2 GB for short pastes and Word files; 1.8 GB for a 200 KB paste;
  - 1.6-1.9 GB for PDFs and screenshots;
  - 2.8 GB for a 4K screenshot; 3.3 GB for a 12 MP photo.

## Output size and Chrome's 1 MB message cap

![Output size](output-size.svg)

A native host can send at most 1 MB per message to the extension. Some outputs exceed that:
- PDFs come out at about 0.55 MB per page, so 2 pages or more go over 1 MB.
- A 12 MP photo comes out at 4.4 MB.

The Phase 4 host must send outputs in chunks, as docs/PLAN.md risk 5 planned.

## Reproduce

```
py -3.12 -m uv run --group leak python tests/bench/timing.py --out docs/perf/phase-3-timing.json
py -3.12 -m uv run python tests/bench/charts.py docs/perf/phase-3-timing.json docs/perf
```

The full run takes about 20 minutes. Keep the machine otherwise idle while it runs.
