"""A missing or tampered model must stop the engine, never be used anyway."""

import pytest
from redactit import models


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("REDACTIT_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(models, "LOCK", {"m/weights.bin": {"url": "https://example.invalid", "sha256": "0" * 64}})
    return tmp_path / "m" / "weights.bin"


def test_missing_model_fails_closed(pinned):
    with pytest.raises(models.ModelError, match="setup-models"):
        models.path_for("m/weights.bin")


def test_tampered_model_fails_closed(pinned):
    pinned.parent.mkdir()
    pinned.write_bytes(b"not the pinned weights")
    with pytest.raises(models.ModelError, match="SHA-256"):
        models.path_for("m/weights.bin")
