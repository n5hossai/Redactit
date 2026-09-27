"""GLiNER on onnxruntime finds names and addresses without torch. Skipped until models are installed."""

import sys

import pytest
from redactit import models

try:
    MODEL, TOKENIZER = models.path_for("gliner/model.onnx"), models.path_for("gliner/tokenizer.json")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

from redactit.detect.ner import GlinerNer  # noqa: E402


@pytest.fixture(scope="module")
def ner():
    return GlinerNer(MODEL, TOKENIZER)


def test_finds_people_and_addresses(ner):
    text = "Call Priya Okafor about 217 Davis Point Apt. 431, Philipport, VT 68945. Siobhan O'Brien signed."
    found = {(s.entity_type, text[s.start:s.end]) for s in ner.detect(text)}
    assert ("PERSON", "Priya Okafor") in found
    assert ("PERSON", "Siobhan O'Brien") in found
    assert ("ADDRESS", "217 Davis Point Apt. 431, Philipport, VT 68945") in found


def test_long_text_is_windowed_without_losing_entities(ner):
    filler = "The quarterly figures were discussed at length. " * 60  # ~420 words, three windows
    text = filler + "Signed by Priya Okafor. " + filler
    assert any(text[s.start:s.end] == "Priya Okafor" for s in ner.detect(text))


def test_runs_without_torch():
    assert "torch" not in sys.modules


def test_a_long_base64_blob_is_fast_and_bounded(ner):
    import base64
    import os
    import time

    blob = base64.b64encode(os.urandom(9000)).decode()  # ~12 KB, the case that once hung
    start = time.perf_counter()
    found = ner.detect(f"Attachment: {blob}\nSigned by Priya Okafor.")
    assert time.perf_counter() - start < 30
    assert any(s.entity_type == "PERSON" for s in found)


def test_windows_respect_the_token_budget():
    from redactit.detect.ner import LONG_WORD_TOKENS, _windows

    lens = [3] * 500 + [LONG_WORD_TOKENS + 1] + [2] * 50
    for a, b in _windows(lens, budget=100):
        assert sum(lens[a:b]) <= 100 and all(n <= LONG_WORD_TOKENS for n in lens[a:b])
    covered = {i for a, b in _windows(lens, 100) for i in range(a, b)}
    assert covered == set(range(len(lens))) - {500}
