#!/usr/bin/env python3
"""Seeded synthetic corpus for the Redactit leak test.

WHY determinism matters: the leak harness re-runs this generator in CI; a
byte-identical corpus for the same --seed keeps every leak report
comparable run to run. All randomness therefore comes from
`random.Random(seed)` and `Faker.seed_instance(seed)` -- never wall-clock
time (see ValueFactory.dob, which pins the date range instead of using
Faker's default "now"-relative age window).

Usage: py -3.12 -m uv run python tests/corpus/generate.py --seed 1234 --out tests/corpus/out --per-variant 2
"""
from __future__ import annotations

import argparse
import json
import random
import string
import sys
from datetime import date
from pathlib import Path

from faker import Faker

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus_docx import DOCX_VARIANTS, build_docx
from corpus_media import PDF_VARIANTS, build_image, build_pdf, build_screenshot

LOCALES = ["en_US", "en_CA", "en_GB"]

# Invented, license-clean org names and codenames; company_terms.txt lists
# whichever of these actually end up in a document.
COMPANY_POOL = [
    "Project Bluefinch",
    "Northwind Analytica",
    "Cedar Hollow Partners",
    "Project Amber Cove",
    "Silver Ridge Robotics",
    "Granite Bay Logistics",
    "Project Indigo Peak",
    "Harborlight Dynamics",
    "Project Slate Orchard",
    "Fenwick Meridian Group",
]

# ---------------------------------------------------------------------------
# Validators (also used by tests/unit/test_corpus.py)
# ---------------------------------------------------------------------------


def luhn_check_digit(digits: str) -> str:
    """The check digit that makes `digits + check` pass Luhn."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


_LETTER_VAL = {c: str(10 + i) for i, c in enumerate(string.ascii_uppercase)}


def _iban_numeric(s: str) -> str:
    return "".join(_LETTER_VAL.get(c, c) for c in s)


def iban_check_digits(country: str, bban: str) -> str:
    remainder = int(_iban_numeric(bban + country + "00")) % 97
    return f"{98 - remainder:02d}"


def iban_mod97_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    return int(_iban_numeric(rearranged)) % 97 == 1


# UK NINO prefix rules: neither letter is D/F/I/Q/U/V, the second is also
# never O, and a handful of two-letter combinations are reserved.
_NINO_FIRST_BAD = set("DFIQUV")
_NINO_SECOND_BAD = set("DFIOQUV")
_NINO_PREFIX_BAD = {"BG", "GB", "KN", "NK", "NT", "TN", "ZZ"}
_NINO_LETTERS = "ABCEHJKLMNPRSTWXYZ"


# ---------------------------------------------------------------------------
# Value factory
# ---------------------------------------------------------------------------


class ValueFactory:
    """Deterministic PII-shaped values, seeded once per corpus run."""

    def __init__(self, seed: int):
        self.fake = Faker(LOCALES)
        self.fake.seed_instance(seed)
        self.rng = random.Random(seed)
        self.used_terms: list[str] = []

    def person(self) -> str:
        return self.fake.name()

    def email(self) -> str:
        return self.fake.email()

    def phone(self) -> str:
        return self.fake.phone_number()

    def address(self) -> str:
        return self.fake.address().replace("\n", ", ")

    def dob(self) -> str:
        # fixed range, never relative to "today" -- see module docstring
        d = self.fake.date_between(start_date=date(1950, 1, 1), end_date=date(2005, 12, 31))
        return d.isoformat()

    def credit_card(self, style: str | None = None) -> str:
        bin_prefix = self.rng.choice(["4", "51", "2221", "37"])
        length = 15 if bin_prefix == "37" else 16
        body = bin_prefix + "".join(str(self.rng.randint(0, 9)) for _ in range(length - len(bin_prefix) - 1))
        number = body + luhn_check_digit(body)
        style = style or self.rng.choice(["none", "spaces", "dashes"])
        if style == "spaces":
            return " ".join(number[i : i + 4] for i in range(0, len(number), 4))
        if style == "dashes":
            return "-".join(number[i : i + 4] for i in range(0, len(number), 4))
        return number

    def iban(self) -> str:
        bank = "".join(self.rng.choice(string.ascii_uppercase) for _ in range(4))
        sort_code = "".join(str(self.rng.randint(0, 9)) for _ in range(6))
        account = "".join(str(self.rng.randint(0, 9)) for _ in range(8))
        bban = bank + sort_code + account
        return f"GB{iban_check_digits('GB', bban)}{bban}"

    def ca_sin(self) -> str:
        # Real SINs never start with 0 or 8, so neither may synthetic ones.
        body = self.rng.choice("1234567" "9") + "".join(str(self.rng.randint(0, 9)) for _ in range(7))
        return body + luhn_check_digit(body)

    def us_ssn(self) -> str:
        area = self.rng.choice([a for a in range(1, 900) if a != 666])
        group = self.rng.randint(1, 99)
        serial = self.rng.randint(1, 9999)
        return f"{area:03d}-{group:02d}-{serial:04d}"

    def uk_nino(self) -> str:
        while True:
            a, b = self.rng.choice(_NINO_LETTERS), self.rng.choice(_NINO_LETTERS)
            if a in _NINO_FIRST_BAD or b in _NINO_SECOND_BAD or a + b in _NINO_PREFIX_BAD:
                continue
            break
        digits = "".join(str(self.rng.randint(0, 9)) for _ in range(6))
        return f"{a}{b}{digits}{self.rng.choice('ABCD')}"

    def passport(self) -> str:
        style = self.rng.choice(["ca", "us", "gb"])
        if style == "ca":
            letters = "".join(self.rng.choice(string.ascii_uppercase) for _ in range(2))
            return letters + "".join(str(self.rng.randint(0, 9)) for _ in range(6))
        if style == "us":
            return "".join(str(self.rng.randint(0, 9)) for _ in range(9))
        letter = self.rng.choice(string.ascii_uppercase)
        return letter + "".join(str(self.rng.randint(0, 9)) for _ in range(8))

    def api_key(self, kind: str | None = None) -> str:
        kind = kind or self.rng.choice(["aws", "github", "slack", "pem"])
        alnum = string.ascii_letters + string.digits
        if kind == "aws":
            return "AKIA" + "".join(self.rng.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        if kind == "github":
            return "ghp_" + "".join(self.rng.choice(alnum) for _ in range(36))
        if kind == "slack":
            g1 = "".join(str(self.rng.randint(0, 9)) for _ in range(10))
            g2 = "".join(str(self.rng.randint(0, 9)) for _ in range(10))
            g3 = "".join(self.rng.choice(alnum) for _ in range(24))
            return f"xoxb-{g1}-{g2}-{g3}"
        # pem: a fake private-key block, random base64-shaped body
        b64 = string.ascii_letters + string.digits + "+/"
        lines = ["".join(self.rng.choice(b64) for _ in range(64)) for _ in range(4)]
        return "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----"

    def company_term(self) -> str:
        term = self.rng.choice(COMPANY_POOL)
        if term not in self.used_terms:
            self.used_terms.append(term)
        return term

    def filler_sentence(self) -> str:
        return self.fake.sentence()

    def filler_paragraph(self) -> str:
        return self.fake.paragraph(nb_sentences=3)


# ---------------------------------------------------------------------------
# Text / Markdown builders
# ---------------------------------------------------------------------------


def build_txt(vf: ValueFactory, path: Path) -> list[dict]:
    entries: list[dict] = []

    def seed(entity_type: str, value: str) -> str:
        entries.append({"entity_type": entity_type, "value": value, "location": "body"})
        return value

    term = seed("COMPANY_TERM", vf.company_term())
    person = seed("PERSON", vf.person())
    email = seed("EMAIL", vf.email())
    phone = seed("PHONE", vf.phone())
    address = seed("ADDRESS", vf.address())
    dob = seed("DATE_OF_BIRTH", vf.dob())
    card = seed("CREDIT_CARD", vf.credit_card())
    iban = seed("IBAN", vf.iban())
    ssn = seed("US_SSN", vf.us_ssn())
    nino = seed("UK_NINO", vf.uk_nino())
    passport = seed("PASSPORT", vf.passport())
    # A name wrapped onto the next line, as in hard-wrapped plain text.
    reviewer = vf.person()
    entries.append({"entity_type": "PERSON", "value": reviewer, "location": "split_lines"})
    wrapped = reviewer.replace(" ", "\n", 1)

    text = (
        f"Subject: {term} account update\n\n"
        f"{vf.filler_paragraph()}\n\n"
        f"Please contact {person} at {email} or {phone} about the account.\n"
        f"Billing address: {address}.\n"
        f"Date of birth on file: {dob}. Card on file: {card}.\n"
        f"Refunds go to IBAN {iban}. SSN {ssn}, NI number {nino}, passport {passport}.\n\n"
        f"{vf.filler_paragraph()}\n\nReviewed and approved by {wrapped}.\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return entries


def build_md(vf: ValueFactory, path: Path) -> list[dict]:
    entries: list[dict] = []

    def seed(entity_type: str, value: str) -> str:
        entries.append({"entity_type": entity_type, "value": value, "location": "body"})
        return value

    term = seed("COMPANY_TERM", vf.company_term())
    p1 = seed("PERSON", vf.person())
    e1 = seed("EMAIL", vf.email())
    p2 = seed("PERSON", vf.person())
    ph2 = seed("PHONE", vf.phone())
    sin = seed("CA_SIN", vf.ca_sin())
    card = seed("CREDIT_CARD", vf.credit_card())
    key = seed("API_KEY", vf.api_key())

    text = (
        f"# {term} status report\n\n"
        f"## Summary\n\n{vf.filler_paragraph()}\n\n"
        f"## Contacts\n\n"
        f"- {p1} -- {e1}\n"
        f"- {p2} -- {ph2}\n\n"
        f"## Accounts\n\n"
        f"| Name | SIN | Card |\n"
        f"|---|---|---|\n"
        f"| {p1} | {sin} | {card} |\n\n"
        f"## Notes\n\n{vf.filler_paragraph()}\n\n"
        f'```bash\nexport API_KEY="{key}"\n```\n'
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return entries


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# (format, variant, extension, builder(vf, path) -> entries)
_PLAN = (
    [("txt", "plain", "txt", build_txt), ("md", "plain", "md", build_md)]
    + [("docx", v, "docx", lambda vf, path, v=v: build_docx(v, vf, path)) for v in DOCX_VARIANTS]
    + [("pdf", v, "pdf", lambda vf, path, v=v: build_pdf(v, vf, path)) for v in PDF_VARIANTS]
    + [("png", "plain", "png", lambda vf, path: build_image("png", vf, path))]
    + [("png", "screenshot_4k", "png", build_screenshot)]
    + [("jpg", "plain", "jpg", lambda vf, path: build_image("jpg", vf, path))]
)


def generate(seed: int, out: Path, per_variant: int) -> dict:
    vf = ValueFactory(seed)
    documents = []
    doc_idx = 0
    for fmt, variant, ext, builder in _PLAN:
        for i in range(per_variant):
            rel_file = f"{fmt}/{variant}_{i:03d}.{ext}"
            doc_path = out / rel_file
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            entries = builder(vf, doc_path)
            for k, entry in enumerate(entries, start=1):
                entry["id"] = f"d{doc_idx:04d}-s{k:02d}"
            documents.append(
                {"file": rel_file, "format": fmt, "variant": variant, "seeded": entries}
            )
            doc_idx += 1

    manifest = {"seed": seed, "schema": 1, "documents": documents}
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n"
    )
    (out / "company_terms.txt").write_text(
        "\n".join(vf.used_terms) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--per-variant", type=int, default=2)
    args = parser.parse_args()
    manifest = generate(args.seed, args.out, args.per_variant)
    print(f"wrote {len(manifest['documents'])} documents to {args.out}")


if __name__ == "__main__":
    main()
