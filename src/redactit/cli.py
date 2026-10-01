"""Command-line interface: `redactit redact`, `watch`, `clip`, `setup-models` and `host`."""

import argparse
import io
import os
import sys
import uuid
from pathlib import Path

import platformdirs

from redactit import __version__
from redactit.hosts import native

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


def effective_policy(path: Path | None = None):
    """The defaults, the admin's managed policy (refused unless admin-owned), then the user's
    policy file (or `path`), as every interface applies them."""
    from redactit.managed import assert_admin_owned, managed_policy_path
    from redactit.policy import load_policy

    default = _paths()["policy"]
    user_policy = path or (default if default.is_file() else None)
    managed = managed_policy_path()
    if managed.is_file():
        assert_admin_owned(managed)
    else:
        managed = None
    return load_policy(user_policy, managed)


def open_engine(policy: Path | None = None, verified=None):
    """The engine with the user's policy, keychain vault and audit log. `verified` is a
    `models.verify(...)` the caller started before its imports (see Engine); it is
    released here if the engine cannot be built."""
    from redactit import models

    with models.released_on_error(verified):
        from redactit.audit import AuditLog
        from redactit.pipeline import Engine
        from redactit.vault import Vault

        paths = _paths()
        return Engine(effective_policy(policy), Vault.open(paths["vault"]), AuditLog(paths["audit"]), verified=verified)


def output_name(name: str, suffix: str) -> str:
    """The output for input `name` in the format `suffix`. A changed format is appended to
    the full name ("notes.docx.md", "scan.pdf.md", "photo.webp.png"), so notes.docx can
    never overwrite notes.md from the same folder."""
    own = Path(name).suffix.lower()
    return name if own == suffix or (suffix == ".jpg" and own == ".jpeg") else name + suffix


def output_suffixes(name: str) -> tuple[str, ...]:
    """The formats `redact_file` writes for a supported `name`, in order. An image's is
    predicted from its suffix; a PNG named photo.jpg really comes out as photo.jpg.png."""
    own = Path(name).suffix.lower()
    kind = SUFFIXES[own]
    if kind == "pdf":  # rebuilt from pixels, plus the redacted page text as Markdown
        return ".pdf", ".md"
    if kind == "image":  # JPEG stays JPEG; every other format becomes PNG
        return (".jpg",) if own in (".jpg", ".jpeg") else (".png",)
    return (".md",) if kind == "docx" else (own,)  # Word comes out as Markdown; txt and md keep theirs


def redact_file(name: str, data: bytes, engine, scope: str, *, site: str | None = None,
                destination: str = "cli") -> list[tuple[str, str | bytes]]:
    """Redact one file called `name` (with a supported suffix) whose content is `data`.

    Returns each output's file name with its text (to be written as UTF-8 in text mode)
    or its bytes. `redactit redact` and the folder watcher both write exactly this, so
    the same input gets the same outputs under the same names from either.
    """
    from redactit.formats.docx import docx_to_markdown
    from redactit.formats.image import redact_image
    from redactit.formats.pdf import redact_pdf

    kind = SUFFIXES[Path(name).suffix.lower()]
    if kind == "image":
        image, suffix, _ = redact_image(data, engine, scope, destination=destination, site=site)
        return [(output_name(name, suffix), image)]  # the suffix comes from the decoded format
    if kind == "pdf":
        parts = redact_pdf(data, engine, scope, destination=destination, site=site)
    else:
        text = docx_to_markdown(data) if kind == "docx" else _decode(data)
        file_type = Path(name).suffix[1:].lower()
        parts = (engine.redact(text, scope, file_type=file_type, destination=destination, site=site).text,)
    return [(output_name(name, s), part) for s, part in zip(output_suffixes(name), parts, strict=True)]


def _decode(data: bytes) -> str:
    # Universal newlines, exactly as Path.read_text reads a file: the form the leak test proves.
    return io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read()


def _redact(args: argparse.Namespace) -> int:
    from redactit import models, safety

    safety.block_network()  # before any detector or model code is imported and run
    pending = models.verify(models.TEXT_MODELS)  # hashes in a thread while the imports below run
    with models.released_on_error(pending):
        from redactit.formats import docx, image, pdf  # noqa: F401 - loaded now, while the hash runs
    engine = open_engine(args.policy, pending)
    if any(SUFFIXES.get(src.suffix.lower()) in ("pdf", "image") for src in args.paths):
        engine.warm_images()  # verifies and loads OCR and faces up front, and audits it
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
        for name, content in redact_file(src.name, src.read_bytes(), engine, scope, site=args.site):
            if isinstance(content, str):
                (args.out / name).write_text(content, encoding="utf-8")
            else:
                (args.out / name).write_bytes(content)
        print(f"redacted {i}/{len(args.paths)}")
    return 0


def _watch(args: argparse.Namespace) -> int:
    from redactit import models, safety
    from redactit.hosts import watcher

    inbox, outbox = watcher.default_folders()
    watch = watcher.Watcher(args.inbox or inbox, args.outbox or outbox)  # refuses outbox == inbox before models load
    watcher.stop_on_signals()
    try:
        safety.block_network()
        pending = models.verify(models.TEXT_MODELS)
        with models.released_on_error(pending):
            from redactit.formats import docx, image, pdf  # noqa: F401 - loaded now, while the hash runs
        engine = open_engine(args.policy, pending)  # one engine for the whole run: the cold start is paid once
        engine.warm_text()
        engine.warm_images()  # now, so the first PDF or image dropped does not wait for OCR to load

        def convert(name: str, data: bytes) -> list[tuple[str, str | bytes]]:
            # A fresh pseudonym scope per file unless --scope links them: a watcher runs for
            # days, and its files go to different chats, which shared labels would link.
            return redact_file(name, data, engine, args.scope or uuid.uuid4().hex, destination="outbox")

        watch.run(convert)
    except KeyboardInterrupt:  # Ctrl+C is how a watcher stops; its temp folder is already gone
        print("redactit watch: stopped", file=sys.stderr)
    return 0


def _clip(args: argparse.Namespace) -> int:
    from redactit import models, safety
    from redactit.hosts import clipboard

    safety.block_network()

    def engine():  # loaded only once the clipboard turns out to hold text worth redacting
        pending = models.verify(models.TEXT_MODELS)  # hashes in a thread while open_engine's imports run
        return open_engine(args.policy, pending)

    return clipboard.redact_clipboard(engine, scope=args.scope)


def _setup_models(_args: argparse.Namespace) -> int:
    from redactit import models

    fetched = models.fetch_all()
    bundled = models.check_bundled()  # shipped inside packages: checked, never downloaded
    present = len(models.LOCK) - len(fetched) - len(bundled)
    print(f"models ready ({len(fetched)} downloaded, {present} already present, {len(bundled)} bundled and verified)")
    return 0


def _host(args: argparse.Namespace) -> int:
    return native.serve(args)  # ends the process itself


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

    watch = sub.add_parser("watch", help="redact every file dropped into an inbox folder into an outbox folder")
    watch.add_argument("--inbox", type=Path, help="folder to watch (default: inbox in Redactit's data folder)")
    watch.add_argument("--outbox", type=Path, help="folder for redacted files (default: outbox in Redactit's data folder)")
    watch.add_argument("--policy", type=Path, help="policy file (default: user policy if present)")
    watch.add_argument("--scope", help="reuse one set of pseudonyms for every file (default: a new set per file)")
    watch.set_defaults(run=_watch)

    clip = sub.add_parser("clip", help="redact the clipboard's text in place, once (an OS shortcut runs this)",
                          epilog="exit status: 0 written back, 3 nothing to redact (no text, or concealed), "
                                 "1 failed; unless 0, the clipboard is untouched")
    clip.add_argument("--policy", type=Path, help="policy file (default: user policy if present)")
    clip.add_argument("--scope", help="reuse pseudonyms across runs that share this name")
    clip.set_defaults(run=_clip)

    setup = sub.add_parser("setup-models", help="download and verify the pinned models (needs network once)")
    setup.set_defaults(run=_setup_models)

    host = sub.add_parser("host", help="serve the browser extension over Chrome native messaging (Chrome starts this)")
    native.add_arguments(host)
    host.set_defaults(run=_host)

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
