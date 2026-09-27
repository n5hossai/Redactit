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
