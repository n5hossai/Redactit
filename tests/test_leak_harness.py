"""Acceptance for the leak harness: on unredacted inputs it must report every seeded value.

If the harness reported zero survivors for a redactor that changed nothing, every later
"zero leaks" result would be meaningless. CI covers the text formats here; the OCR
formats are slower and run through tests/leak/run.py for each phase report.
"""

import generate as gen
import pytest
import run as leak

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
