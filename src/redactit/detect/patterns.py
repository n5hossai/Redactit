"""Pattern recognizers for IDs, contact details, dates of birth and addresses.

Scores encode certainty: a checksum or fixed national format scores high; shapes that
also fit innocent text (bare 9-digit numbers, plain dates) score low and only clear a
threshold when a cue word or table header says what they are (see registry.py).
"""

from __future__ import annotations

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer

MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
DASH = r"[\s.\-–—]"  # space, dot, hyphen, en dash, em dash


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Presidio's own CreditCardRecognizer has no branch for Mastercard's 2016+ BIN range
# (2221-2720); its Luhn check (validate_result) still applies to this extra pattern, so
# passing it alongside PATTERNS to CreditCardRecognizer(patterns=...) is enough.
MASTERCARD_2_SERIES = Pattern(
    "Credit card (Mastercard 2-series)",
    r"\b2(?:22[1-9]|2[3-9]\d|[3-6]\d{2}|7[01]\d|720)[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
    0.3,
)


def _recognizer(entity: str, patterns: list[Pattern], flags=re.MULTILINE) -> PatternRecognizer:
    return PatternRecognizer(supported_entity=entity, patterns=patterns, global_regex_flags=flags)


class CaSinRecognizer(PatternRecognizer):
    """Canadian SIN: 9 digits, optional space/dash grouping, Luhn check digit.

    The first digit is never 0 or 8 (those ranges are reserved and never issued), so the
    regex excludes them up front; `invalidate_result` then drops anything that fails Luhn.
    """

    PATTERNS = [Pattern("CA_SIN", r"\b[1-79]\d{2}[- ]?\d{3}[- ]?\d{3}\b", 0.4)]

    def __init__(self) -> None:
        super().__init__(supported_entity="CA_SIN", patterns=self.PATTERNS, global_regex_flags=re.MULTILINE)

    def invalidate_result(self, pattern_text: str) -> bool:
        return not _luhn_ok(re.sub(r"[- ]", "", pattern_text))


class UsSsnRecognizer(PatternRecognizer):
    """US SSN with any mix of space, dot or dash separators, or none.

    Area 000, 666 and 9xx, group 00 and serial 0000 are never issued. A bare 9-digit run
    fits too much else (passport, account and SIN numbers), so it scores low and needs a
    cue such as "SSN" to clear a threshold.
    """

    PATTERNS = [
        Pattern("SSN separated", r"\b\d{3}[\s.-]\d{2}[\s.-]\d{4}\b", 0.6),
        Pattern("SSN bare", r"\b\d{9}\b", 0.2),
    ]

    def __init__(self) -> None:
        super().__init__(supported_entity="US_SSN", patterns=self.PATTERNS, global_regex_flags=re.MULTILINE)

    def invalidate_result(self, pattern_text: str) -> bool:
        d = re.sub(r"\D", "", pattern_text)
        return d[:3] in ("000", "666") or d[0] == "9" or d[3:5] == "00" or d[5:] == "0000"


# UK National Insurance number: excluded prefixes and letters per HMRC, suffix A-D. The
# format is specific enough to score high in any case and with any grouping.
UK_NINO = _recognizer("UK_NINO", [Pattern(
    "UK NINO",
    r"\b(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z](?:[\s-]?\d{2}){3}[\s-]?[A-D]\b",
    0.85,
)], re.MULTILINE | re.IGNORECASE)

# CA (2 letters + 6 digits), UK/GB (1 letter + 8 digits) and US (9 digits) passports. Only
# the lettered form scores above zero on its own; the "passport" cue does the rest.
PASSPORT = _recognizer("PASSPORT", [
    Pattern("Passport lettered", r"\b[A-Z]{1,2}[\s-]?\d{6,8}\b", 0.35),
    Pattern("Passport bare digits", r"\b\d{9}\b", 0.05),
], re.MULTILINE | re.IGNORECASE)

# Most dates are not birth dates, so every format starts low; a "born"/"DOB" cue or a DOB
# column header lifts it (registry.py).
DATE_OF_BIRTH = _recognizer("DATE_OF_BIRTH", [
    Pattern("Date ISO or Y/M/D", r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b", 0.15),
    Pattern("Date D/M/Y", r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b", 0.15),
    Pattern("Date Month D, Y", rf"\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", 0.15),
    Pattern("Date D Month Y", rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{MONTH},?\s+\d{{4}}\b", 0.15),
], re.MULTILINE | re.IGNORECASE)

# Our own email pattern: Presidio's checks the domain against the public suffix list, so
# addresses on internal domains (corp.local, acme.internal) scored 0 and survived.
EMAIL = _recognizer("EMAIL", [Pattern(
    "Email", r"(?<![\w.+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w-])", 0.95,
)])

# Phones by shape: an optional country/trunk prefix, an area code in brackets or followed
# by a separator, a 6-8 digit subscriber part and an optional extension. Replaces
# Presidio's phonenumbers recogniser, which at the leniency synthetic (often unassigned)
# numbers need also matched dates, card groups and ZIP codes. Overlaps with cards and
# SINs are merged by the policy, so over-matching a card group costs nothing.
PHONE = _recognizer("PHONE", [
    Pattern(
        "Phone with area code",
        rf"(?<![\w+])(?:\+\d{{1,3}}{DASH}?|00\d{{1,3}}{DASH}?|1{DASH})?(?:\(0\)\s?)?"
        rf"(?:\(\d{{2,5}}\){DASH}?|\d{{2,5}}{DASH})\d{{3,4}}{DASH}?\d{{3,4}}"
        r"(?:\s?(?:x|ext\.?|#)\s?\d{1,6})?(?!\w)",
        0.8,
    ),
    # International numbers grouped in pairs or triples, e.g. +33 6 12 34 56 78.
    Pattern("Phone international", rf"(?<![\w+])\+\d{{1,3}}(?:{DASH}?\d{{1,4}}){{3,6}}(?!\w)", 0.8),
    Pattern("Phone E.164", r"(?<![\w+])\+\d{10,14}(?!\w)", 0.8),
    # Bare 10-11 digits are usually a phone; other long numbers (account ids, timestamps)
    # get masked as PHONE too, which is the safe direction to be wrong in.
    Pattern("Phone bare digits", r"(?<![\w+])\d{10,11}(?!\w)", 0.6),
    Pattern("Phone local 7 digits", rf"(?<![\w+-])\d{{3}}[-.–]\d{{4}}(?![\w-])", 0.5),
], re.IGNORECASE)

# Addresses the name model misses: street lines with a known street type, then up to four
# comma-separated parts (city, region, postcode, country). Over-reaching into the rest of
# a sentence redacts too much, which is the safe side.
_STREET_TYPE = (r"(?:Street|St|Road|Rd|Lane|Ln|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Court|Ct|Way|Place|Pl"
                r"|Square|Sq|Crescent|Cres|Terrace|Close|Parkway|Pkwy|Highway|Hwy|Row|Walk|Gardens|Grove"
                r"|Mews|Circle|Cir|Trail|Park|Hill|Green)")
_UNIT = r"(?:(?:Flat|Apt\.?|Apartment|Unit|Suite)\s*[\w-]+,?\s+)?"
_TAIL = r"(?:,[^\S\n]*[^,\n.;:!?]{2,40}){0,4}"
ADDRESS = _recognizer("ADDRESS", [
    Pattern("Street, number first",
            rf"\b{_UNIT}\d{{1,5}}[A-Za-z]?,?\s+(?:[A-Z][\w'-]*\s+){{0,4}}{_STREET_TYPE}\b\.?{_TAIL}", 0.75),
    Pattern("Street, type first (FR/ES/IT)",
            r"\b\d{1,5}[A-Za-z]?,?\s+(?:Rue|Avenue|Boulevard|Bd|Place|Chemin|All[ée]e|Impasse|Quai|Via|Viale"
            rf"|Calle|Avenida|Plaza)(?:\s+[\w'-]+){{1,6}}{_TAIL}", 0.75),
    # US military mail: ship, PSC box or unit, then APO/FPO/DPO + AA/AE/AP + ZIP.
    Pattern("Military address",
            r"\b(?:(?:USNS|USNV|USS|USCGC)\s+[A-Z][\w'-]*(?:\s[A-Z][\w'-]*)?|PSC\s+\d{4},?\s+Box\s+\d{4}"
            r"|Unit\s+\d{4},?\s+Box\s+\d{4})[,\n]\s*(?:APO|FPO|DPO)\s+(?:AA|AE|AP)\s+\d{5}\b", 0.9),
    # Postcodes on their own: enough to locate a person to a street in the UK and Canada.
    Pattern("UK postcode", r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[ABD-HJLNP-UW-Z]{2}\b", 0.55),
    Pattern("CA postal code", r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b", 0.55),
])
