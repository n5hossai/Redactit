"""Acceptance for the leak harness: on unredacted inputs it must report every seeded value.

If the harness reported zero survivors for a redactor that changed nothing, every later
"zero leaks" result would be meaningless. CI covers the text formats here; the OCR
formats are slower and run through tests/leak/run.py for each phase report.
"""

import importlib.util
import sys
from pathlib import Path

TESTS = Path(__file__).parent
sys.path.insert(0, str(TESTS / "corpus"))
import generate as gen  # noqa: E402

_spec = importlib.util.spec_from_file_location("leak_run", TESTS / "leak" / "run.py")
leak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak)

FAST = {"txt", "md", "docx"}


def test_pass_through_redactor_leaks_every_seeded_value(tmp_path):
    manifest = gen.generate(seed=7, out=tmp_path, per_variant=1)
    seeded = sum(len(d["seeded"]) for d in manifest["documents"] if d["format"] in FAST)

    result = leak.run(corpus=tmp_path, outputs=tmp_path, spans_file=None, formats=FAST)

    assert seeded > 0
    assert len(result["survivors"]) == seeded
    assert not result["missing_outputs"]
