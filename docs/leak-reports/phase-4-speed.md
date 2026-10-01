# Phase 4 leak report: faster OCR and pattern stage

Decisions 2 and 3 of the [speed plan](../perf/speed-plan.md) are built:
- **OCR:** onnxruntime's memory arena is on and trimmed after each image or page. The
  three turned passes are skipped only when the upright pass read at least one line
  confidently and left no detected box unread. Dial 5 never skips them.
- **Pattern stage:** the slim spaCy engine; Presidio's de-duplication and context passes
  rewritten to scale with the text; the postcode and ZIP address rules searched only near
  a postcode or ZIP.
- **Threads:** the name model and OCR use one thread per physical core.

The corpus gained the cases that could defeat the gate:
- the 48 rotated stress pages: one value at 90, 180 or 270 degrees, 14-32 px, black or
  grey, alone or below 12 upright lines;
- a scanned PDF page whose only text runs sideways.

Every corpus holds all 48 stress pages, whatever `--per-variant` says. Before the sweep,
the verifier read all 270 new values of the five seeds in the unredacted inputs, so it can
see a survivor there.

The sweep ran as in [Phase 3](phase-3.md). There were five seeds (7, 99, 1234, 2024,
31337), two documents per variant plus the new pages. The real engine redacted each seed in
one process. The independent verifier then re-read every output: the text layer, 300 DPI
OCR at full size in three rotations, barcodes, metadata, faces and raw bytes. All values
and faces are synthetic.

## Result

| Dial | Formats | Planted values | Survivors | Metadata / text-layer / face violations | Precision |
|---|---|---:|---:|---:|---:|
| 3, five seeds | txt, md, docx | 360 | **0** | 0 | 385 / 396 = 0.972 |
| 3, five seeds | pdf, png, jpg | 750 | **0** | 0 | 611 / 662 = 0.923 |
| 5, seeds 7 and 99 | txt, md, docx | 144 | **0** | 0 | 153 / 163 = 0.939 |
| 5, seeds 7 and 99 | pdf, png, jpg | 300 | **0** | 0 | 244 / 289 = 0.844 |

Dial 3 ran on commit 7754081, and dial 5 on 2ca72b8. The later commit adds only the
folder watcher and clipboard work, which changes no detection or format code.

A confirmation sweep then ran on 1645ed9, the end of this phase, after the fixes that
apply a site's dial to OCR, name tied overlaps the same way in every run and stop a
redaction when a pattern times out. It used seeds 7 and 99 at dial 3, all formats:

| Dial | Formats | Planted values | Survivors | Metadata / text-layer / face violations | Precision |
|---|---|---:|---:|---:|---:|
| 3, seeds 7 and 99 | txt, md, docx | 144 | **0** | 0 | 153 / 159 = 0.962 |
| 3, seeds 7 and 99 | pdf, png, jpg | 300 | **0** | 0 | 244 / 266 = 0.917 |

Faces: 8 of 8 removed.

| Entity type (pdf, png, jpg, dial 3) | Planted | Removed |
|---|---:|---:|
| PERSON | 185 | 185 |
| EMAIL | 141 | 141 |
| PHONE | 135 | 135 |
| IBAN | 99 | 99 |
| CREDIT_CARD | 50 | 50 |
| ADDRESS | 30 | 30 |
| COMPANY_TERM | 30 | 30 |
| FACE | 20 | 20 |
| PASSPORT | 20 | 20 |
| CA_SIN | 10 | 10 |
| DATE_OF_BIRTH | 10 | 10 |
| US_SSN | 10 | 10 |
| API_KEY | 10 | 10 |

| Precision by format, dial 3 | Phase 3 documents | Added in Phase 4 |
|---|---:|---:|
| txt | 117 / 120 = 0.975 | |
| md | 88 / 90 = 0.978 | |
| docx | 180 / 186 = 0.968 | |
| pdf | 188 / 195 = 0.964 | 25 / 30 = 0.833 (sideways scans) |
| png | 105 / 120 = 0.875 | 234 / 247 = 0.947 (stress pages) |
| jpg | 59 / 70 = 0.843 | |

On the documents Phase 3 had, precision per format is exactly what Phase 3 reported.

## What the gate did

At dial 3 the gate skipped the turned passes on 12 of the 68 views per seed. It closed on
the upright PDF pages (digital, mixed, scanned, cropped, split lines) and the 4K
screenshots. It stayed open on:
- the rotated PDF pages;
- the photos with turned text;
- both sideways scans;
- all 48 stress pages in every seed.

On seed 7 and the benchmark's inputs, dial 3 and dial 5 read identical lines on all 74
views, including the 16 where the gate closed. So a closed gate dropped nothing there.

## Identical where it must be

- **Arena:** the read lines of 42 corpus and benchmark views were identical with the arena
  on and off: text, corners and character cuts at full float precision.
- **Pattern stage:** before and after, the redacted text corpus of five seeds at dials 3
  and 5 was byte-identical (190 files). So were every span, model score and decision on
  the benchmark prose (200 B to 200 KB) and on three digit-and-comma inputs. Tests pin each
  rewrite to Presidio's original: de-duplication, the context pass, and the anchored
  address rules on 400 address-heavy random texts.

## Speed

These timings are indicative: another builder used the machine throughout.

| | Phase 3 | Phase 4 |
|---|---:|---:|
| Redacting one seed's Phase 3 documents, all formats, engine load included | 101-102 s | 40-46 s |
| Redacting the 50 new documents per seed | | 100-137 s |
| Pattern stage, 200 KB paste | 24-27 s | 3 s |
| OCR of a new 720p screenshot (OCR only) | 5.9 s | 3.5 s |
| Memory of an OCR-only process after one 720p-1080p screenshot | | 0.17 GB (1.1-1.6 GB with the arena untrimmed) |

- **Pattern stage at 200 KB:** spaCy takes 2.6 s of the 3 s. The parts that grew faster
  than the text were profiled on a busy machine: de-duplication (6.7 s, now 0.02 s), the
  context pass (12.6 s, now 0.14 s) and the postcode and ZIP rules (7 s, now 0.04 s).
- **Dial-5 times:** 89 and 124 s for the Phase 3 documents. They ran next to the test
  suite, so they are not comparable.

## Misses found and fixed during this phase

None. Both sweeps found 0 survivors and 0 violations on their first run.

## Known gaps

- **What the gate cannot see.** The gate trusts upright text detection to box turned text,
  which it did for every stress value. A page with confident upright text and a turned value
  the detector never boxes would lose the turned passes at dials 3-4.
- **Address-heavy pastes are still slow in the address rules.** A 200 KB paste that is
  nothing but postcodes takes about 6.5 s there, as before: every position is near one.
- **spaCy is slow on number-heavy text.** 200 KB of digit runs is about 80,000 tokens, at
  5-12 s.

## Reproduce

The sweep drives these two commands per seed, then runs `run.py` once with
`--formats txt,md,docx` and once with `--formats pdf,png,jpg`:

```
py -3.12 -m uv run --group leak python tests/corpus/generate.py --seed 7 --out tests/corpus/out --per-variant 2
py -3.12 -m uv run --group leak python tests/leak/redact_corpus.py --corpus tests/corpus/out --out tests/leak/redacted --dial 3
py -3.12 -m uv run --group leak python tests/leak/run.py --corpus tests/corpus/out --outputs tests/leak/redacted --spans tests/leak/redacted/spans.jsonl --formats pdf,png,jpg
```

One seed of the OCR formats now takes about 10 minutes to redact and verify, because of the
50 new pages.
