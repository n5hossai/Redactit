"""Redact the synthetic corpus with the real engine, for the leak test.

    uv run python tests/leak/redact_corpus.py --corpus tests/corpus/out --out tests/leak/redacted --dial 3
    uv run python tests/leak/run.py --corpus tests/corpus/out --outputs tests/leak/redacted \
        --spans tests/leak/redacted/spans.jsonl

Writes outputs mirroring the corpus layout, spans.jsonl (value digests, for precision) and
the engine's own audit.jsonl, which the leak test scans for seeded values as well.
"""

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

import yaml
from redactit.audit import AuditLog
from redactit.formats.docx import docx_to_markdown
from redactit.formats.image import redact_image
from redactit.formats.pdf import redact_pdf
from redactit.pipeline import Engine
from redactit.policy import DEFAULT_POLICY, load_policy
from redactit.vault import Vault
from run import normalize

TEXT_FORMATS = {"txt", "md", "docx"}
OCR_FORMATS = {"pdf", "png", "jpg"}  # slow: every page and image is OCR'd several times
SUPPORTED = TEXT_FORMATS | OCR_FORMATS


class _Recorder:
    """Passes everything through to the engine, noting each redacted value for precision."""

    def __init__(self, engine: Engine) -> None:
        self._engine, self.values = engine, []

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def redact(self, text, *args, **kwargs):
        result = self._engine.redact(text, *args, **kwargs)
        self.values += [text[d.span.start:d.span.end] for d in result.decisions]
        return result


def _write(corpus: Path, out: Path, doc: dict, engine) -> None:
    """Redact one corpus file the way the CLI would, writing every output next to its path."""
    src, dst = corpus / doc["file"], out / doc["file"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    fmt, scope = doc["format"], doc["file"]
    if fmt == "pdf":
        pdf, markdown = redact_pdf(src.read_bytes(), engine, scope)
        dst.write_bytes(pdf)
        dst.with_name(dst.name + ".md").write_text(markdown, encoding="utf-8")
    elif fmt in ("png", "jpg"):
        image, suffix, text = redact_image(src.read_bytes(), engine, scope)
        dst.with_suffix(suffix).write_bytes(image)
        dst.with_name(dst.name + ".md").write_text(text, encoding="utf-8")
    else:  # Word documents are redacted as Markdown; txt and md keep their format
        text = docx_to_markdown(src.read_bytes()) if fmt == "docx" else src.read_text(encoding="utf-8")
        target = dst.with_name(dst.name + ".md") if fmt == "docx" else dst
        target.write_text(engine.redact(text, scope=scope, file_type=fmt).text, encoding="utf-8")


def redact_corpus(corpus: Path, out: Path, dial: int, formats: set[str] = TEXT_FORMATS) -> None:
    out.mkdir(parents=True, exist_ok=True)
    policy = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    policy["dial"]["position"] = dial
    policy["custom_terms"]["files"] = [str(corpus / "company_terms.txt")]
    with tempfile.TemporaryDirectory() as tmp, Vault(Path(tmp) / "vault.db", os.urandom(32)) as vault:
        # A throwaway vault key: the test never touches the real keychain.
        (Path(tmp) / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
        engine = Engine(load_policy(Path(tmp) / "policy.yaml"), vault, AuditLog(out / "audit.jsonl"))
        spans = []
        manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
        for doc in (d for d in manifest["documents"] if d["format"] in formats):
            recorder = _Recorder(engine)
            _write(corpus, out, doc, recorder)
            spans += [{"file": doc["file"], "value_digest": hashlib.sha256(normalize(v).encode()).hexdigest()}
                      for v in recorder.values]
        (out / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dial", type=int, default=3)
    ap.add_argument("--formats", default=",".join(sorted(SUPPORTED)), help="comma-separated subset")
    args = ap.parse_args()
    redact_corpus(args.corpus, args.out, args.dial, set(args.formats.split(",")))
