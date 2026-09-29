"""Acceptance for the leak harness: on unredacted inputs it must report every seeded value.

If the harness reported zero survivors for a redactor that changed nothing, every later
"zero leaks" result would be meaningless. CI covers the text formats here; the OCR
formats are slower and run through tests/leak/run.py for each phase report.
"""

import json

import generate as gen
import pytest
import run as leak
from corpus_media import FACES
from PIL import Image
from redactit.models import ModelError, path_for

FAST = {"txt", "md", "docx"}


def test_pass_through_redactor_leaks_every_seeded_value(tmp_path):
    manifest = gen.generate(seed=7, out=tmp_path, per_variant=1)
    seeded = sum(len(d["seeded"]) for d in manifest["documents"] if d["format"] in FAST)

    result = leak.run(corpus=tmp_path, outputs=tmp_path, spans_file=None, formats=FAST)

    assert seeded > 0
    assert len(result["survivors"]) == seeded
    assert not result["missing_outputs"]


def test_a_format_typo_is_an_error_not_an_empty_pass(tmp_path):
    gen.generate(seed=7, out=tmp_path, per_variant=1)
    with pytest.raises(ValueError, match="not in the manifest"):
        leak.run(corpus=tmp_path, outputs=tmp_path, spans_file=None, formats={"TXT"})


def test_output_type_comes_from_content_not_suffix(tmp_path):
    from PIL import Image

    Image.new("RGB", (8, 8)).save(tmp_path / "a.webp")
    assert leak._open_image((tmp_path / "a.webp").read_bytes()) is not None
    (tmp_path / "b.bin").write_bytes(b"\x00\x01 not a known format")
    with pytest.raises(ValueError, match="unrecognised output type"):
        leak.extract(tmp_path / "b.bin")


def test_zip_members_are_extracted_not_skipped(tmp_path):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("notes/readme.md", "call Priya Okafor")
        z.writestr("blob.bin", b"\x00\x01 opaque")
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="blob.bin: unrecognised output type"):
        leak.extract(tmp_path / "a.zip")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("notes/readme.md", "call Priya Okafor")
    (tmp_path / "b.zip").write_bytes(buf.getvalue())
    sources, _ = leak.extract(tmp_path / "b.zip")
    assert "Priya Okafor" in sources["notes/readme.md/text"]


@pytest.fixture
def no_ocr(monkeypatch):
    monkeypatch.setattr(leak, "_ocr", lambda img, rotations: "")


@pytest.mark.parametrize(("scores", "expected"), [([0.9], ["face present"]), ([], [])])
def test_a_face_in_an_output_image_is_a_problem(monkeypatch, no_ocr, scores, expected):
    monkeypatch.setattr(leak, "_face_scores", lambda img: scores)
    problems: list[str] = []
    leak._image_text(Image.new("RGB", (8, 8)), problems)
    assert problems == expected


def test_a_missing_face_model_fails_loudly_instead_of_skipping_the_check(monkeypatch, tmp_path, no_ocr):
    monkeypatch.setenv("REDACTIT_MODEL_DIR", str(tmp_path))
    with pytest.raises(ModelError, match="setup-models"):
        leak._image_text(Image.new("RGB", (8, 8)), [])


@pytest.mark.parametrize(("problems", "removed"), [([], 1), (["face present"], 0)])
def test_a_face_entry_is_removed_only_when_no_face_is_left(monkeypatch, tmp_path, problems, removed):
    doc = {"file": "png/a.png", "format": "png", "variant": "plain",
           "seeded": [{"id": "d0000-s01", "entity_type": "FACE", "value": "man_2.jpg", "location": "face"}]}
    (tmp_path / "manifest.json").write_text(json.dumps({"seed": 1, "schema": 1, "documents": [doc]}), encoding="utf-8")
    (tmp_path / "png").mkdir()
    (tmp_path / "png" / "a.png").write_bytes(b"")
    monkeypatch.setattr(leak, "extract", lambda path: ({"bytes": "man_2.jpg"}, problems))

    result = leak.run(tmp_path, tmp_path, spans_file=None)

    assert result["recall"]["png/FACE"] == {"seeded": 1, "removed": removed}
    assert [s["found_in"] for s in result["survivors"]] == ([["face present"]] if problems else [])
    assert [v["problem"] for v in result["violations"]] == problems


def test_every_face_fixture_is_detected():
    try:
        path_for(leak.FACE_MODEL)
    except (ModelError, KeyError):  # KeyError: file present but not in models.lock.json
        pytest.skip("YuNet not installed and pinned; run `redactit setup-models`")
    scores = {}
    for path in sorted(FACES.glob("*.jpg")):
        with Image.open(path) as img:
            scores[path.name] = max(leak._face_scores(img), default=0)
    assert scores and {n: s for n, s in scores.items() if s < 0.8} == {}
