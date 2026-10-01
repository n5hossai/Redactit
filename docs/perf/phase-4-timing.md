# Phase 4 timing: the speed plan, measured

Measured on 2026-10-01 with `tests/bench/timing.py`, on the same machine and inputs as
[phase-3-timing.md](phase-3-timing.md):
- **Machine:** Ryzen 7 7840HS, 8 cores, 15 GB RAM, Windows 11, CPU only.
- **Raw numbers:** [`phase-4/timing.json`](phase-4/timing.json).
- **New in this run:** each image is also timed at a new size on every repeat, as real
  screenshots arrive. Phase 3 only re-timed the same image, which hid the cost of a size the
  engine had not seen.

![Phase 3 against Phase 4](phase-4/impact.svg)

## Summary

| Input | Phase 3 | Phase 4 (same size) | Phase 4 (new size) | Peak memory, 3 → 4 |
|---|---:|---:|---:|---:|
| Cold start to a loaded engine | 6.1 s | **3.2 s** | | |
| Paste, 2 KB | 0.62 s | **0.51 s** | | 1.2 → 1.2 GB |
| Paste, 20 KB | 6.8 s | **5.3-6.5 s*** | | 1.2 → 1.2 GB |
| Paste, 200 KB | 72 s | **53 s** | | 1.8 → 1.8 GB |
| Word, 10 pages | 9.6 s | **7.9 s** | | 1.2 → 1.2 GB |
| PDF, 1 digital page | 11.8 s | **7.0 s** | | 1.9 → 2.1 GB |
| PDF, 1 scanned page | 10.2 s | **5.6 s** | | 1.9 → 2.1 GB |
| PDF, 10 digital pages | 116 s | **70 s** | | 1.9 → 2.2 GB |
| Screenshot, 1280 x 720 | 5.1 s | **3.5 s** | 3.6 s | 1.6 → 2.0 GB |
| Screenshot, 1920 x 1080 | 9.5 s | **6.5 s** | 6.6 s | 1.6 → 2.1 GB |
| Screenshot, 3840 x 2160 | 38 s | **25 s** | 25 s | 2.8 → 3.5 GB |
| Phone photo, 12 MP | 15 s | **12 s** | 14 s | 3.3 → 3.5 GB |

\* The full run measured 6.5 s, steady across all 5 repeats. A run of that input alone
measured 5.25 s, and the difference sat in the name model, so it reflects the machine's
state, not the code.

What changed, by decision in [speed-plan.md](speed-plan.md):
- **Cold start halved (decision 4):** model hashing now runs while the imports load, and
  costs nothing on the critical path.
- **OCR (decision 2):**
  - **Memory pool:** with it on, a new image size costs about the same as a repeat (6.6
    against 6.5 s at 1080p). Before, a new 720p took 7.5 s against 4.3 s.
  - **Gate:** the turned passes are skipped only when nothing is left to read, and never at
    dial 5.
  - **Peak memory:** the pool raises it. Views past 9 MP read without the pool, so every
    measured peak stays at or under 3.5 GB. With the pool, a 12 MP photo peaked at 5.7 GB.
- **Patterns and the name model (decision 3):** the pattern stage on a 200 KB paste went from
  about 19 s to 3 s, and the name model now runs one thread per physical core.

![How long each input takes](phase-4/speed.svg)

![Where the time goes](phase-4/stages.svg)

The name model is now nearly all of a paste's time (92-95%). It stays at full precision,
because INT8 lost recall (decision 3), so long pastes remain multi-second and get a
progress state.

![Cold start](phase-4/cold-start.svg)

![Peak memory](phase-4/memory.svg)

## Leak safety of these changes

[leak-reports/phase-4-speed.md](../leak-reports/phase-4-speed.md):
- **Sweeps:** 0 survivors and 0 violations at dial 3 (5 seeds, 1,110 values) and at dial 5
  (2 seeds).
- **Precision:** unchanged on every format.
- **Text output:** all 190 redacted text files are byte-identical before and after.

## Reproduce

```
py -3.12 -m uv run --group leak python tests/bench/timing.py --out docs/perf/phase-4/timing.json
py -3.12 -m uv run python tests/bench/charts.py docs/perf/phase-4/timing.json docs/perf/phase-4 docs/perf/phase-4/compare.json
```
