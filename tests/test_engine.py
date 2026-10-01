"""Engine behaviour that needs the real model. Skipped until models are installed."""

import json
import os

import pytest
from redactit import models

try:
    models.path_for("gliner/model.onnx")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

from redactit.audit import AuditLog  # noqa: E402
from redactit.pipeline import Engine  # noqa: E402
from redactit.policy import load_policy  # noqa: E402
from redactit.vault import Vault  # noqa: E402


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("engine")
    with Vault(tmp / "vault.db", os.urandom(32)) as vault:
        yield Engine(load_policy(None), vault, AuditLog(tmp / "audit.jsonl")), tmp / "audit.jsonl"


def test_hidden_characters_do_not_hide_values(engine):
    text = "Pri­ya Okafor paid with 4111​ 1111 1111 1111."
    out = engine[0].redact(text, scope="t").text
    assert "Okafor" not in out and "1111" not in out and " paid with " in out


def test_startup_is_audited_without_values(engine):
    events = [json.loads(line)["event"] for line in engine[1].read_text(encoding="utf-8").splitlines()]
    assert events[:3] == ["engine_start", "policy_loaded", "model_verified"] and "vault_purge" in events


def test_warming_up_records_only_the_models_it_verified(engine):
    """Warm-up runs the detectors on synthetic input: no vault entry, no redaction event."""
    eng, audit = engine
    rows = lambda: eng.vault._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]  # noqa: E731
    before, logged = rows(), len(audit.read_text(encoding="utf-8").splitlines())
    eng.warm_text()
    eng.warm_images()
    new = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()[logged:]]
    assert rows() == before
    assert [(e["event"], e["model"], e["source"]) for e in new] == [
        ("model_verified", "rapidocr.det.onnx", "package"),
        ("model_verified", "rapidocr.cls.onnx", "package"),
        ("model_verified", "rapidocr.rec.onnx", "package"),
        ("model_verified", "yunet.face_detection_yunet_2023mar.onnx", "download"),
    ]
