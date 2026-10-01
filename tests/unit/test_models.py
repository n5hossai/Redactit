"""A missing, tampered or swapped model must stop the engine, never be used anyway (T15)."""

import hashlib
import os
import sys
import threading

import pytest
from PIL import Image
from redactit import models

GOOD = b"the pinned weights"


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    monkeypatch.setenv("REDACTIT_MODEL_DIR", str(tmp_path))
    monkeypatch.setattr(models, "LOCK", {"m/weights.bin": {"url": "https://example.invalid", "sha256": "0" * 64}})
    return tmp_path / "m" / "weights.bin"


@pytest.fixture
def good(tmp_path, monkeypatch):
    """A pinned file whose content matches its pin."""
    monkeypatch.setenv("REDACTIT_MODEL_DIR", str(tmp_path))
    pin = {"url": "https://example.invalid", "sha256": hashlib.sha256(GOOD).hexdigest()}
    monkeypatch.setattr(models, "LOCK", {"m/weights.bin": pin})
    path = tmp_path / "m" / "weights.bin"
    path.parent.mkdir()
    path.write_bytes(GOOD)
    return path


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    """Write a file where a real pin expects it: the model folder and RapidOCR's package
    folder both point into tmp_path, so the installed files (which uv hard-links from its
    cache) are never touched."""
    monkeypatch.setenv("REDACTIT_MODEL_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(models, "_package_root", lambda _package: tmp_path / "package")

    def write(name: str, content: bytes):
        path = models.local_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    return write


def test_missing_model_fails_closed(pinned):
    with pytest.raises(models.ModelError, match="setup-models"):
        models.path_for("m/weights.bin")


def test_tampered_model_fails_closed(pinned):
    pinned.parent.mkdir()
    pinned.write_bytes(b"not the pinned weights")
    with pytest.raises(models.ModelError, match="SHA-256"):
        models.path_for("m/weights.bin")


@pytest.mark.parametrize("name", ["gliner/model.onnx", "gliner/tokenizer.json", "yunet/face_detection_yunet_2023mar.onnx",
                                  *models.OCR_MODELS])
def test_every_real_pin_refuses_a_tampered_file(stand_in, name):
    stand_in(name, b"tampered weights")
    with pytest.raises(models.ModelError, match="SHA-256"), models.verify([name]):
        pytest.fail("a tampered file reached the loader")


def test_ocr_builds_only_from_verified_files(stand_in):
    from redactit import ocr

    for name in models.OCR_MODELS:
        stand_in(name, b"tampered weights")
    ocr._engine.cache_clear()
    try:
        with pytest.raises(models.ModelError, match="rapidocr/det.onnx"):
            ocr._engine()
    finally:
        ocr._engine.cache_clear()  # the next OCR use rebuilds from the real files


def test_face_detection_refuses_a_tampered_model(stand_in):
    from redactit.formats import image

    stand_in(image.YUNET, b"tampered weights")
    with pytest.raises(models.ModelError, match="yunet"):
        image._faces(Image.new("RGB", (64, 64), "white"))


def test_bundled_rapidocr_models_match_their_pins():
    assert models.check_bundled() == list(models.OCR_MODELS)


def test_setup_never_downloads_bundled_files(monkeypatch):
    monkeypatch.setattr(models, "LOCK", {k: v for k, v in models.LOCK.items() if "package" in v})
    monkeypatch.setattr(models.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("tried to download"))
    assert models.fetch_all() == []


def test_hashing_runs_in_the_background(good, monkeypatch):
    release, digest = threading.Event(), models._digest
    monkeypatch.setattr(models, "_digest", lambda f: (release.wait(10), digest(f))[1])
    pending = models.verify(["m/weights.bin"])  # returns although the hash cannot finish yet
    release.set()
    with pending as paths:
        assert paths["m/weights.bin"] == good


@pytest.mark.skipif(sys.platform != "win32", reason="Windows holds the file; elsewhere it is hashed again")
def test_held_file_cannot_be_changed_until_loaded(good):
    other = good.with_name("other.bin")
    other.write_bytes(b"a swapped-in model")
    with models.verify(["m/weights.bin"]):
        with pytest.raises(PermissionError):
            good.open("r+b")
        with pytest.raises(PermissionError):
            os.replace(other, good)
        with pytest.raises(PermissionError):
            good.unlink()
    good.open("r+b").close()  # released once the block ends
    assert good.read_bytes() == GOOD


def test_change_during_load_is_caught_by_the_second_hash(good, monkeypatch):
    monkeypatch.setattr(models, "HOLDS", False)  # as on macOS and Linux
    with pytest.raises(models.ModelError, match="changed while"), models.verify(["m/weights.bin"]) as paths:
        paths["m/weights.bin"].write_bytes(b"swapped in while the loader read it")


def test_unchanged_file_passes_the_second_hash(good, monkeypatch):
    monkeypatch.setattr(models, "HOLDS", False)
    with models.verify(["m/weights.bin"]) as paths:
        assert paths["m/weights.bin"].read_bytes() == GOOD
