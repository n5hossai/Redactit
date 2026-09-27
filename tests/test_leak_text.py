"""Phase 2 acceptance: zero seeded values survive in text and Markdown, from the default
admin floor (the lowest dial a user can reach) to the tightest dial, and none reach the
audit log. Skipped until models are installed."""

import json

import generate as gen
import pytest
import run as leak
from redactit import models

try:
    models.path_for("gliner/model.onnx")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

from redact_corpus import SUPPORTED, redact_corpus  # noqa: E402


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    gen.generate(seed=7, out=root, per_variant=3)
    return root


@pytest.mark.parametrize("dial", [3, 5], ids=["admin floor", "tightest"])
def test_no_seeded_value_survives(corpus, tmp_path, dial):
    redact_corpus(corpus, tmp_path, dial)
    result = leak.run(corpus, tmp_path, tmp_path / "spans.jsonl", SUPPORTED)

    assert result["survivors"] == []
    assert result["violations"] == []
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    compact, spaced = {"audit:text": leak.normalize(audit)}, {"audit:text": leak.words(audit)}
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    leaked = [s["value"] for d in manifest["documents"] for s in d["seeded"]
              if leak.found_in(s["entity_type"], s["value"], compact, spaced)]
    assert leaked == []
