"""Command-line interface: `redactit redact`, `redactit setup-models`."""

import argparse
import sys
import uuid
from pathlib import Path

import platformdirs

from redactit import __version__

TEXT_SUFFIXES = {".txt", ".md"}


def _paths() -> dict[str, Path]:
    user_data = platformdirs.user_data_path("redactit", appauthor=False)
    return {
        "policy": platformdirs.user_config_path("redactit", appauthor=False) / "policy.yaml",
        # Admin-writable only, e.g. %ProgramData%\redactit or /etc/xdg/redactit.
        "managed": platformdirs.site_config_path("redactit", appauthor=False) / "policy.yaml",
        "vault": user_data / "vault.db",
        "audit": user_data / "audit.jsonl",
    }


def _redact(args: argparse.Namespace) -> int:
    from redactit import safety

    safety.block_network()  # before any detector or model code is imported and run
    from redactit.audit import AuditLog
    from redactit.pipeline import Engine
    from redactit.policy import load_policy
    from redactit.vault import Vault

    paths = _paths()
    user_policy = args.policy or (paths["policy"] if paths["policy"].is_file() else None)
    managed = paths["managed"] if paths["managed"].is_file() else None
    engine = Engine(load_policy(user_policy, managed), Vault.open(paths["vault"]), AuditLog(paths["audit"]))
    scope = args.scope or uuid.uuid4().hex  # a fresh scope per call unless the caller links runs
    args.out.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(args.paths, 1):
        if src.suffix.lower() not in TEXT_SUFFIXES:
            print(f"skipped {i}/{len(args.paths)}: {src.suffix or 'no extension'} is not supported yet", file=sys.stderr)
            continue
        result = engine.redact(src.read_text(encoding="utf-8"), scope, file_type=src.suffix[1:].lower(), site=args.site)
        (args.out / src.name).write_text(result.text, encoding="utf-8")
        print(f"redacted {i}/{len(args.paths)}: {len(result.decisions)} items")
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

    redact = sub.add_parser("redact", help="redact text or Markdown files")
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
        # Only our own message types reach the terminal; others could quote the input text.
        from redactit.models import ModelError

        msg = str(exc) if isinstance(exc, ModelError) else f"{type(exc).__name__} (details withheld: may contain input text)"
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
