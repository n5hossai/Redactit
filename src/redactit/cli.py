"""Command-line entry point. Subcommands (redact, verify, clip, watch,
setup-models) land in later phases; this only proves the entry point works.
"""

import argparse

from redactit import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redactit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(f"redactit {__version__}")
    return 0
