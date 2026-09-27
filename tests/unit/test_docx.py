"""Word files: every visible part is extracted, properties are dropped, hostile files are refused."""

import io
import json
import zipfile

import generate as gen
import pytest
from redactit.formats.docx import DocxError, docx_to_markdown


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("docx")
    manifest = gen.generate(seed=11, out=root, per_variant=1)
    return root, [d for d in manifest["documents"] if d["format"] == "docx"]


def test_every_visible_part_is_extracted_and_properties_are_dropped(corpus):
    root, docs = corpus
    for doc in docs:
        md = docx_to_markdown((root / doc["file"]).read_bytes())
        for s in doc["seeded"]:
            if s["location"] == "core_props":
                assert s["value"] not in md, "document properties must never reach the output"
            else:
                assert s["value"] in md, (doc["file"], s["location"])


def _docx(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


def test_an_xml_entity_bomb_is_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><d>&b;</d>'
    with pytest.raises(DocxError):
        docx_to_markdown(_docx({"word/document.xml": bomb}))


def test_a_zip_bomb_is_refused(monkeypatch):
    from redactit.formats import docx

    monkeypatch.setattr(docx, "MAX_UNCOMPRESSED", 1000)
    with pytest.raises(DocxError, match="oversized"):
        docx_to_markdown(_docx({"word/document.xml": "<d>" + "x" * 5000 + "</d>"}))


def test_files_that_are_not_word_documents_are_refused():
    with pytest.raises(DocxError):
        docx_to_markdown(b"plain text, not a zip")
    with pytest.raises(DocxError, match="document.xml"):
        docx_to_markdown(_docx({"word/styles.xml": "<s/>"}))
