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
from redactit.pipeline import Engine
from redactit.policy import DEFAULT_POLICY, load_policy
from redactit.vault import Vault
from run import normalize

SUPPORTED = {"txt", "md", "docx"}
# Word documents are redacted as Markdown; everything else keeps its format.
READERS = {"docx": (lambda p: docx_to_markdown(p.read_bytes()), ".md")}


def redact_corpus(corpus: Path, out: Path, dial: int, formats: set[str] = SUPPORTED) -> None:
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
            read, suffix = READERS.get(doc["format"], (lambda p: p.read_text(encoding="utf-8"), None))
            text = read(corpus / doc["file"])
            result = engine.redact(text, scope=doc["file"], file_type=doc["format"])
            dst = out / doc["file"] if suffix is None else (out / doc["file"]).with_suffix(suffix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(result.text, encoding="utf-8")
            spans += [
                {"file": doc["file"], "entity_type": d.span.entity_type,
                 "value_digest": hashlib.sha256(normalize(text[d.span.start:d.span.end]).encode()).hexdigest()}
                for d in result.decisions
            ]
        (out / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dial", type=int, default=3)
    args = ap.parse_args()
    redact_corpus(args.corpus, args.out, args.dial)
