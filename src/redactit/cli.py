"""Command-line interface: `redactit redact`, `redactit setup-models`."""

import argparse
import os
import sys
import uuid
from pathlib import Path

import platformdirs

from redactit import __version__

SUFFIXES = {".txt": "text", ".md": "text", ".docx": "docx", ".pdf": "pdf",
            ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}


def _paths() -> dict[str, Path]:
    # The user's own data may move (REDACTIT_DATA_DIR, e.g. for tests); the admin policy
    # path never follows the environment (see managed.py).
    user_data = Path(os.environ.get("REDACTIT_DATA_DIR") or platformdirs.user_data_path("redactit", appauthor=False))
    return {
        "policy": platformdirs.user_config_path("redactit", appauthor=False) / "policy.yaml",
        "vault": user_data / "vault.db",
        "audit": user_data / "audit.jsonl",
    }


def _redact(args: argparse.Namespace) -> int:
    from redactit import safety

    safety.block_network()  # before any detector or model code is imported and run
    from redactit.audit import AuditLog
    from redactit.formats.docx import docx_to_markdown
    from redactit.formats.image import redact_image
    from redactit.formats.pdf import redact_pdf
    from redactit.managed import assert_admin_owned, managed_policy_path
    from redactit.pipeline import Engine
    from redactit.policy import load_policy
    from redactit.vault import Vault

    paths = _paths()
    user_policy = args.policy or (paths["policy"] if paths["policy"].is_file() else None)
    managed = managed_policy_path()
    if managed.is_file():
        assert_admin_owned(managed)
    else:
        managed = None
    engine = Engine(load_policy(user_policy, managed), Vault.open(paths["vault"]), AuditLog(paths["audit"]))
    scope = args.scope or uuid.uuid4().hex  # a fresh scope per call unless the caller links runs
    args.out.mkdir(parents=True, exist_ok=True)
    seen = set()
    for i, src in enumerate(args.paths, 1):
        kind = SUFFIXES.get(src.suffix.lower())
        if kind is None:
            print(f"skipped {i}/{len(args.paths)}: {src.suffix or 'no extension'} is not supported", file=sys.stderr)
            continue
        if src.name.lower() in seen:  # a/notes.md and b/notes.md would both write out/notes.md
            print(f"skipped {i}/{len(args.paths)}: an earlier input has the same name", file=sys.stderr)
            continue
        seen.add(src.name.lower())
        # A changed format is appended to the full name ("notes.docx.md", "scan.pdf.md",
        # "photo.webp.png"), so notes.docx can never overwrite notes.md from the same folder.
        dst = args.out / src.name
        if kind == "pdf":  # rebuilt from pixels, plus the redacted page text as Markdown
            pdf, markdown = redact_pdf(src.read_bytes(), engine, scope)
            dst.write_bytes(pdf)
            dst.with_name(dst.name + ".md").write_text(markdown, encoding="utf-8")
        elif kind == "image":  # JPEG stays JPEG; every other format becomes PNG
            image, suffix, _ = redact_image(src.read_bytes(), engine, scope)
            same = src.suffix.lower() in ((".jpg", ".jpeg") if suffix == ".jpg" else (suffix,))
            (dst if same else dst.with_name(dst.name + suffix)).write_bytes(image)
        else:  # Word documents come out as Markdown; txt and md keep their format
            text = docx_to_markdown(src.read_bytes()) if kind == "docx" else src.read_text(encoding="utf-8")
            result = engine.redact(text, scope, file_type=src.suffix[1:].lower(), site=args.site)
            (dst.with_name(dst.name + ".md") if kind == "docx" else dst).write_text(result.text, encoding="utf-8")
        print(f"redacted {i}/{len(args.paths)}")
    return 0


def _setup_models(_args: argparse.Namespace) -> int:
    from redactit import models

    fetched = models.fetch_all()
    print(f"models ready ({len(fetched)} downloaded, {len(models.LOCK) - len(fetched)} already present)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redactit", description="Local-first redaction before text reaches an LLM.")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    sub = parser.add_subparsers(dest="command")

    redact = sub.add_parser("redact", help="redact text, Markdown, Word, PDF and image files")
    redact.add_argument("paths", nargs="+", type=Path)
    redact.add_argument("--out", type=Path, required=True, help="output folder")
    redact.add_argument("--policy", type=Path, help="policy file (default: user policy if present)")
    redact.add_argument("--scope", help="reuse pseudonyms across calls that share this name")
    redact.add_argument("--site", help="apply this site's policy rules, e.g. chatgpt.com")
    redact.set_defaults(run=_redact)

    setup = sub.add_parser("setup-models", help="download and verify the pinned models (needs network once)")
    setup.set_defaults(run=_setup_models)

    args = parser.parse_args(argv)
    if args.version:
        print(f"redactit {__version__}")
        return 0
    if not args.command:
        parser.print_help()
        return 2
    try:
        return args.run(args)
    except Exception as exc:  # noqa: BLE001
        # Only our own errors print their message; another library's could quote the input.
        from redactit.types import RedactitError

        msg = str(exc) if isinstance(exc, RedactitError) else f"{type(exc).__name__} (details withheld: may contain input text)"
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
