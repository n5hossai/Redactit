"""Unit tests for the seeded corpus generator.

WHY these checks and no others: the leak harness trusts the generator's
determinism and its manifest contract; OCR-based readability is checked
by the leak test's pass-through run, not here, to keep this
suite fast and offline.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pypdfium2 as pdfium
import pytest

import generate as gen
from corpus_media import FACES

SEED = 42
PER_VARIANT = 1


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    out = tmp_path_factory.mktemp("corpus")
    manifest = gen.generate(seed=SEED, out=out, per_variant=PER_VARIANT)
    return out, manifest


def test_determinism(tmp_path):
    out_a, out_b = tmp_path / "a", tmp_path / "b"
    gen.generate(seed=SEED, out=out_a, per_variant=PER_VARIANT)
    gen.generate(seed=SEED, out=out_b, per_variant=PER_VARIANT)

    assert (out_a / "manifest.json").read_bytes() == (out_b / "manifest.json").read_bytes()
    assert (out_a / "company_terms.txt").read_bytes() == (out_b / "company_terms.txt").read_bytes()

    files_a = sorted(p.relative_to(out_a) for p in out_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(out_b) for p in out_b.rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        assert (out_a / rel).read_bytes() == (out_b / rel).read_bytes(), rel


def test_docx_manifest_values_present(corpus):
    out, manifest = corpus
    for doc in manifest["documents"]:
        if doc["format"] != "docx":
            continue
        path = out / doc["file"]
        with zipfile.ZipFile(path) as zf:
            assert zf.testzip() is None
            xml_names = [n for n in zf.namelist() if n.endswith((".xml", ".rels"))]
            parts = {n: zf.read(n) for n in xml_names}
            for data in parts.values():
                ET.fromstring(data)  # every part must be well-formed XML
            raw = "".join(data.decode("utf-8") for data in parts.values())
            doc_xml = parts["word/document.xml"].decode("utf-8")

        joined_runs = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", doc_xml, re.S))
        for e in doc["seeded"]:
            haystack = joined_runs if e["location"] == "split_runs" else raw
            assert e["value"] in haystack, (doc["file"], e)


def test_pdf_text_layer(corpus):
    out, manifest = corpus
    for doc in manifest["documents"]:
        if doc["format"] != "pdf":
            continue
        pdf = pdfium.PdfDocument(str(out / doc["file"]))
        text = pdf[0].get_textpage().get_text_range()

        if doc["variant"] == "scanned":
            assert text == ""
            continue

        norm_text = _norm(text)
        for e in doc["seeded"]:
            if e["location"] in ("text_layer", "split_lines"):
                assert _norm(e["value"]) in norm_text, (doc["file"], e)


def test_validators_on_generated_values(corpus):
    _, manifest = corpus
    seen = {"CREDIT_CARD": 0, "CA_SIN": 0, "IBAN": 0}
    for doc in manifest["documents"]:
        for e in doc["seeded"]:
            if e["entity_type"] == "CREDIT_CARD":
                digits = re.sub(r"[ -]", "", e["value"])
                assert gen.luhn_ok(digits), e
                seen["CREDIT_CARD"] += 1
            elif e["entity_type"] == "CA_SIN":
                assert gen.luhn_ok(e["value"]), e
                seen["CA_SIN"] += 1
            elif e["entity_type"] == "IBAN":
                assert gen.iban_mod97_ok(e["value"]), e
                seen["IBAN"] += 1
    assert all(count > 0 for count in seen.values()), seen


def test_manifest_schema(corpus):
    out, manifest = corpus
    assert manifest["seed"] == SEED
    assert manifest["schema"] == 1
    seen_ids = set()
    for doc in manifest["documents"]:
        assert (out / doc["file"]).is_file()
        assert "/" in doc["file"] and "\\" not in doc["file"]
        for e in doc["seeded"]:
            assert e["id"] not in seen_ids
            seen_ids.add(e["id"])
    assert (out / "company_terms.txt").is_file()


def test_every_image_seeds_one_face_from_the_fixtures(corpus):
    _, manifest = corpus
    fixtures = {p.name for p in FACES.glob("*.jpg")}
    images = [d for d in manifest["documents"] if d["format"] in ("png", "jpg")]
    assert images and fixtures
    for doc in images:
        faces = [(e["location"], e["value"] in fixtures) for e in doc["seeded"] if e["entity_type"] == "FACE"]
        assert faces == [("face", True)], doc["file"]
