# Speed plan for the extension

**Status: proposed, pending the owner's approval.** Built on the measurements in
[phase-3-timing.md](phase-3-timing.md) and on follow-up experiments. Every change here
either gives identical output, or kept the leak sweep at 0 survivors (seeds 7 and 99,
all formats, dial 3, 168 values each).

![What the proposed decisions change](impact.svg)

## The five decisions

| # | Decision | Effect (basis) | Safety check |
|---|---|---|---|
| 1 | Keep one warm host per browser profile | First paste 6.7 s → 0.72 s (measured) | Same detection code; requests wait while loading and fail closed |
| 2 | OCR: turn on onnxruntime's memory arena; skip turned passes when nothing is left to read | New 720p 7.5 → 4.3 s, new 1080p 13.2 → 7.7 s OCR (measured); sweep 101 → 60 s per seed | 0 survivors; identical precision; gate tightened after review, re-sweep before commit |
| 3 | Keep the full-precision name model; take only changes with identical output | 2 KB 0.62 → ~0.5 s, 20 KB 6.8 → ~5.3 s (estimated) | Identical scores; INT8 rejected on recall |
| 4 | Verify every model on every load, no hash cache; overlap the hash with imports | Cold start 6.1 → ~4.5-5 s (estimated) | Closes two gaps: check-then-load, and unhashed OCR models |
| 5 | No model inside the browser for the MVP | Avoids a second engine to keep leak-free | Phones stay unsupported for now |

### 1. Keep one warm host per browser profile

- **Problem:** cold start is 6.1 s.
  - Python start 1.6 s;
  - imports 2.2 s;
  - name model 1.5 s;
  - hash check 0.55 s;
  - spaCy 0.34 s.

  A first paste on a cold host takes about 6.7 s; on a warm one, 0.62 s. Moving data between the browser and the host costs nothing that matters: 0.03 ms for 2 KB, 10 ms for 2 MB.
- **Proposal:**
  - **When it starts:** on the first real keystroke, paste or drop in the chat composer. Both sites appear to focus the composer on page load, so "on focus" would amount to "on page load".
  - **Opt-in setting:** "Keep Redactit ready" starts it as soon as an AI site loads.
  - **Load order:** text detection first (ready 4.4 s after launch); OCR warms in the background (ready 8.8 s).
  - **Idle exit:** the host exits after 30 idle minutes.
  - **Launch:** the host starts the base Python directly, which saves 0.3-1.1 s over the venv launcher.
- **While warming:** the extension holds the paste and shows "warming up". It never sends raw text, and blocks the paste after 30 s.
- **Cost:** about 0.94 GB of memory with text ready, 1.08 GB with everything warm. Peaks reach 1.9 GB on a PDF page and 3.3 GB on a 12 MP photo. An idle host uses memory, not battery.

### 2. OCR: memory arena on, and a gate on the turned passes

- **Problem:** OCR is 70-85% of image and PDF time.
  - RapidOCR switches onnxruntime's memory arena off, so every new image size reallocates.
  - The benchmark re-timed the same image, so it understated this: a new 720p takes 7.5 s against 4.3 s on a repeat.
  - The three turned passes cost 0.6 s on 720p, 3.0 s per PDF page and 7.0 s at 4K, and usually find nothing.
- **Proposal:**
  - **Arena:** turn it on, and trim it when idle (it holds memory after use).
  - **Gate:** skip the turned passes only when the upright pass read at least one line confidently and left nothing unread. A page with no upright boxes still gets every pass.
  - **Strict dial:** dial 5 always runs every pass.
- **Measured:**
  - The gate fired on 30 of 44 views.
  - 48 of 48 rotated stress values (90/180/270°, 14-32 px, black and grey) kept it open.
  - Read lines with the arena on were identical.
- **Before it is committed:** the tightened gate needs one more leak sweep. The 48 stress cases and a scanned page with only sideways text join the corpus permanently.
- **Not now:**
  - **Skipping OCR on digital PDF pages:** the corpus has no outlined-font or broken-text-layer pages to prove it safe.
  - **GPU via DirectML:** a download, and a non-MIT license.

### 3. Name model: keep full precision

- **Rejected, INT8:** it is 2.27× faster, but 49 of 300 names and 40 of 150 addresses fell below the dial-3 threshold (median name score 0.742 → 0.603).
- **Rejected, the vendor's lighter model:** F1 75.5% against 80.99%.
- **Taken:** only changes with identical output:
  - threads set to the physical core count (8 threads is fastest; 16 doubles the time);
  - two windows in flight;
  - the slim spaCy engine (identical spans, pattern stage 17-28% faster).
- **Fix in Phase 4:** Presidio's de-duplication and context passes grow faster than the text (about 6 s at 200 KB, near 0 at 20 KB). The address pattern itself is linear (about 24 ms per KB). Add a timing test:
  - the address recognizer takes 1.0 s or less on 200 KB;
  - the 200 KB / 20 KB time ratio stays at 12× or less;
  - an adversarial input of long digit-and-comma runs passes.
- **Large pastes stay slow:** about 5 s at 20 KB. They need a progress state, and the owner may set a size cap.

### 4. Model verification

- **Rejected:** a hash cache keyed on file size and modification time. Bit rot, or a swap that keeps the time, would pass silently.
- **Rejected:** reading the model once and loading those bytes. onnxruntime keeps its own copy, so it costs +633 MB resident even with the buffer freed (measured).
- **Rejected:** onnxruntime's pre-optimised format. Two conversions gave different SHA-256 values, so it cannot be pinned.
- **Proposal:**
  - hash in a background thread during the 2.2 s of imports;
  - on Windows, lock each model file against writes and deletes until it is loaded;
  - elsewhere, re-hash after loading and refuse on a mismatch;
  - pin RapidOCR's three model files in `models.lock.json`; they are not hashed at runtime today, a gap against T15.
- **Remaining risk:** a local process with write access to the user's model folder could still race a swap on non-Windows systems. Such a process could already change the installed packages.

### 5. No model in the browser for the MVP

- **The host is not the bottleneck:** moving data to it costs milliseconds. It is needed anyway for the CLI, folder watcher, clipboard, PDF and OCR.
- **Why not in the browser:**
  - **No threads:** the extension's service worker has none, and the model on one thread is 3.7× slower (measured natively).
  - **Permission:** a hidden extension page needs `offscreen`, which is outside the allowed permissions.
  - **Size:** the model is 665 MB at full precision, shipped with every update.
  - **Duplication:** a browser engine would need JavaScript ports of Presidio, PDFium and the OCR post-processing, which means a second engine to keep leak-free.
- **Revisit:** only for platforms with no native host (Safari on iOS).

## Other machines and browsers

![The same cases on other machines](devices.svg)

| Machine (Geekbench 6 multi-core) | Paste, 2 KB | Screenshot, 1080p | PDF page | RAM |
|---|---:|---:|---:|---|
| This machine, Ryzen 7 7840HS (11,871), **measured** | 0.65 s | 9.6 s | 11.7 s | 15 GB: fine |
| Same machine at 4 / 2 / 1 threads, **measured** | 0.75 / 0.93* / 1.7 s | 10.7 / 12.8 / 15.7 s | 12.6 / 15.9 / 20.0 s | |
| Apple M2 (10,062), projected | ~0.8 s | ~11 s | ~14 s | fine |
| Mid-range laptop, Ryzen 5 7530U (5,679), projected | ~1.4 s | ~20 s | ~25 s | fine |
| Budget laptop, Core i5-1135G7 (5,373), projected | ~1.4 s | ~21 s | ~26 s | 8 GB: tight |
| Mid-range phone, Snapdragon 7s Gen 2 (2,987) | not estimated | not estimated | not estimated | risky |

\* The name model alone at 2 threads; the end-to-end figure was noisy.

These are today's times, before decisions 2 and 3. Projections scale the 8-thread
times by multi-core score. They are pessimistic for OCR, which gains only 1.65× from 1 to
8 threads. Scores come from the Geekbench browser and CPU-Monkey listings (sources in
[speed-plan.json](speed-plan.json)).

| Browser | Works? | How |
|---|---|---|
| Chrome desktop | Yes | MV3 extension + native host; numbers as measured |
| Edge desktop | Yes | Same code, separate host registration; same numbers |
| Firefox desktop | Yes, with porting | Native messaging exists; event pages instead of service workers, sidebar instead of side panel |
| Safari macOS | Hard | Native messaging only through an app extension, so the engine ships inside a sandboxed app |
| Chrome Android | No | No extensions |
| Safari iOS | Only with an in-browser model | The one case where decision 5 would be revisited |

On a budget laptop a 1080p screenshot would still take 10-15 s after decision 2. Slow
machines therefore need the drop zone's visible progress, not only faster code.

## Review points

Each decision was challenged before it was proposed:
- **Warm host:** starting at every page load costs about 1 GB per profile even for people who never paste. Resolved: start on the first keystroke, paste or drop; page-load start is opt-in.
- **OCR gate:** a page whose only text is sideways could get no upright boxes and skip the turned passes. Resolved: zero boxes means every pass runs, dial 5 never skips, the stress cases become corpus variants, and the gate is re-swept before commit.
- **Pattern stage:** checked for regex backtracking. The address pattern is linear; the cost is in Presidio's passes, now with a timing test target.
- **Model loading:** "read once, load bytes" was measured with the buffer freed. It still holds +633 MB, so file locking and re-hashing were chosen instead.
- **Benchmark method:** re-timing the same image hid the reallocation cost. Phase 4's benchmark times a new image size on every repeat and reports both figures.

## What the owner needs to decide

1. Accept about 1 GB of memory per browser profile while the host is warm, with a 30-minute idle exit (decision 1).
2. Starting on the first keystroke, paste or drop as the default, with "Keep Redactit ready" (start at page load) as an opt-in (decision 1).
3. Approve the OCR gate for dials 3-4, with dial 5 always running every pass (decision 2).
4. Large pastes: keep them slow with a progress state, or set a size cap (decision 3).
5. Confirm no in-browser model and no phone support in the MVP (decision 5).
