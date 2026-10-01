"""Pattern recognizers for IDs, contact details, dates of birth and addresses.

Scores encode certainty: a checksum or fixed national format scores high; shapes that
also fit innocent text (bare 9-digit numbers, plain dates) score low and only clear a
threshold when a cue word or table header says what they are (see registry.py).
"""

from __future__ import annotations

import regex as re
from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer, RecognizerResult

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


class CardDigitsRecognizer(PatternRecognizer):
    """Card numbers Presidio's word-anchored patterns miss: broken across one line break (PDF
    text layers and OCR wrap mid-number) or glued to a label because OCR dropped the space
    ("Card4111..."). Luhn and a 13-19 digit length still apply.
    """

    PATTERNS = [
        Pattern("Card across a line break", r"(?<!\d)(?:\d[ -]?){4,15}\r?\n[^\S\n]*(?:\d[ -]?){2,15}\d(?!\d)", 0.8),
        Pattern("Card glued to text", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", 0.8),
    ]

    def __init__(self) -> None:
        super().__init__(supported_entity="CREDIT_CARD", patterns=self.PATTERNS, name="card_digits_pattern")

    def validate_result(self, pattern_text: str) -> bool:
        digits = re.sub(r"\D", "", pattern_text)
        return 13 <= len(digits) <= 19 and _luhn_ok(digits)


# Official IBAN length per country (SWIFT IBAN registry).
IBAN_LENGTHS = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}


def _iban_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


class GluedIbanRecognizer(EntityRecognizer):
    """IBANs glued to the text before them ("IBANGB69VMDF..."), which OCR produces and
    Presidio's word-anchored pattern misses. Every position is tried at its country's exact
    length and must pass mod-97, so a false match is roughly a 1-in-97 chance per candidate
    that already has a valid country code, two check digits and the right length.
    """

    def __init__(self) -> None:
        super().__init__(supported_entities=["IBAN"], name="glued_iban")

    def load(self) -> None:
        pass

    def analyze(self, text: str, entities, nlp_artifacts=None) -> list[RecognizerResult]:
        results = []
        for m in re.finditer(r"(?=([A-Z]{2})\d{2})", text):
            end = _take_iban_chars(text, m.start(), IBAN_LENGTHS.get(m.group(1), 0))
            if end:
                # A checksum pass is certain. An IBAN-shaped string that fails it is most often
                # an OCR misread (O for 0), and IBAN is locked, so it is masked either way.
                score = 1.0 if _iban_ok(text[m.start():end].replace(" ", "")) else 0.6
                results.append(RecognizerResult("IBAN", m.start(), end, score,
                                                recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: self.name}))
        return results


def _take_iban_chars(text: str, start: int, n: int) -> int:
    """End of the n-th capital letter or digit from `start`, with single spaces allowed between
    them (printed IBANs group by four: "GB82 WEST 1234 ..."); 0 if anything else comes first."""
    count, i = 0, start
    while n and i < len(text) and count < n:
        ch = text[i]
        if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            count += 1
        elif ch != " " or text[i - 1] == " ":
            return 0
        i += 1
    return i if n and count == n else 0


def _recognizer(entity: str, patterns: list[Pattern], flags=re.MULTILINE) -> PatternRecognizer:
    # A distinct name per entity, so the audit log can tell which recogniser fired.
    return PatternRecognizer(supported_entity=entity, patterns=patterns, global_regex_flags=flags,
                             name=f"{entity.lower()}_pattern")


class CaSinRecognizer(PatternRecognizer):
    """Canadian SIN: 9 digits, optional space/dash grouping, Luhn check digit.

    The first digit is never 0 or 8 (those ranges are reserved and never issued), so the
    regex excludes them up front; `validate_result` keeps only numbers that pass Luhn, which
    also raises their score to 1.0, marking them as checksum-confirmed.
    """

    # Digit lookarounds instead of \b: OCR glues labels to values ("SIN046454286").
    PATTERNS = [Pattern("CA_SIN", r"(?<!\d)[1-79]\d{2}[- ]?\d{3}[- ]?\d{3}(?!\d)", 0.4)]

    def __init__(self) -> None:
        super().__init__(supported_entity="CA_SIN", patterns=self.PATTERNS, global_regex_flags=re.MULTILINE)

    def validate_result(self, pattern_text: str) -> bool:
        return _luhn_ok(re.sub(r"[- ]", "", pattern_text))


class UsSsnRecognizer(PatternRecognizer):
    """US SSN with any mix of space, dot or dash separators, or none.

    Area 000, 666 and 9xx, group 00 and serial 0000 are never issued. A bare 9-digit run
    fits too much else (passport, account and SIN numbers), so it scores low and needs a
    cue such as "SSN" to clear a threshold.
    """

    PATTERNS = [
        Pattern("SSN separated", r"(?<!\d)\d{3}[\s.-]\d{2}[\s.-]\d{4}(?!\d)", 0.6),
        # 0.35 clears the locked-type threshold: an unlabelled 9-digit run is masked, since
        # it may be an SSN, passport or account number and over-redaction is the safe side.
        Pattern("SSN bare", r"(?<!\d)\d{9}(?!\d)", 0.35),
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
    r"(?<!\d)(?!BG|GB|KN|NK|NT|TN|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z](?:[\s-]?\d{2}){3}[\s-]?[A-D]\b",
    0.85,
)], re.MULTILINE | re.IGNORECASE)

# CA (2 letters + 6 digits), UK/GB (1 letter + 8 digits) and US (9 digits) passports. Only
# the lettered form scores above zero on its own; the "passport" cue does the rest.
PASSPORT = _recognizer("PASSPORT", [
    # No boundary before the letters: OCR glues "PP CF581535" into "PPCF581535".
    # Capital letters may be glued to a preceding word; lower-case ones must start a word, or
    # "Deadline 20240315" loses "ne" and becomes "Deadli** ********".
    Pattern("Passport lettered", r"(?:(?<!\d)(?-i:[A-Z]{1,2})|\b(?-i:[a-z]{1,2}))[\s-]?\d{6,8}(?!\d)", 0.35),
    Pattern("Passport bare digits", r"\b\d{9}\b", 0.35),  # same reasoning as a bare SSN
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
    # \w in domain labels too: internationalised domains (müller-bau.de) are still addresses.
    "Email", r"(?<![\w.+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)+(?![\w-])", 0.95,
)])

# Phones by shape: an optional country/trunk prefix, an area code in brackets or followed
# by a separator, a 6-8 digit subscriber part and an optional extension. Replaces
# Presidio's phonenumbers recogniser, which at the leniency synthetic (often unassigned)
# numbers need also matched dates, card groups and ZIP codes. Overlaps with cards and
# SINs are merged by the policy, so over-matching a card group costs nothing.
PHONE = _recognizer("PHONE", [
    Pattern(
        "Phone with area code",
        # Digit lookarounds, not word ones: OCR glues labels to numbers ("Phone5551234567").
        rf"(?<![\d+])(?:\+\d{{1,3}}{DASH}?|00\d{{1,3}}{DASH}?|1{DASH})?(?:\(0\)\s?)?"
        rf"(?:\(\d{{2,5}}\){DASH}?|\d{{2,5}}{DASH})\d{{3,4}}{DASH}?\d{{3,4}}"
        r"(?:\s?(?:x|ext\.?|#)\s?\d{1,6})?(?!\d)",
        0.8,
    ),
    # International numbers grouped in pairs or triples, e.g. +33 6 12 34 56 78.
    Pattern("Phone international", rf"(?<![\d+])\+\d{{1,3}}(?:{DASH}?\d{{1,4}}){{3,6}}(?!\d)", 0.8),
    Pattern("Phone E.164", r"(?<![\d+])\+\d{10,14}(?!\d)", 0.8),
    # Bare 10-11 digits are usually a phone; other long numbers (account ids, timestamps)
    # get masked as PHONE too, which is the safe direction to be wrong in.
    Pattern("Phone bare digits", r"(?<![\d+])\d{10,11}(?!\d)", 0.6),
    Pattern("Phone local 7 digits", rf"(?<![\w+-])\d{{3}}[-.–]\d{{4}}(?![\w-])", 0.5),
], re.IGNORECASE)

# Addresses the name model misses: street lines with a known street type, then up to four
# comma-separated parts (city, region, postcode, country). Over-reaching into the rest of
# a sentence redacts too much, which is the safe side.
_STREET_TYPE = (r"(?:Street|St|Road|Rd|Lane|Ln|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Court|Ct|Way|Place|Pl"
                r"|Square|Sq|Crescent|Cres|Terrace|Close|Parkway|Pkwy|Highway|Hwy|Row|Walk|Gardens|Grove"
                r"|Mews|Circle|Cir|Trail|Park|Hill|Green)")
_UNIT = r"(?:(?:Flat|Apt\.?|Apartment|Unit|Suite)\s*[\w-]+,?\s+)?"
_UNIT_AFTER = r"(?:,?[^\S\n]*(?:Apt|Apartment|Suite|Ste|Unit|Flat|#)\.?[^\S\n]*[\w-]+)?"
_TAIL = _UNIT_AFTER + r"(?:,[^\S\n]*[^,\n.;:!?]{2,40}){0,4}"
# Up to three comma-separated parts before a postcode; a part may end a line, since PDFs
# and hard-wrapped text break addresses after a comma ("2 Josh Plains," / "Vanessafort, S6 5WJ").
# A part may continue across one line break only mid-word ("Va" / "nessafort", or a hyphen
# break): a new line that starts a new word, like "Card: ...", must not join the address.
# A full stop ends a part only before a space or line end, so "Apt.044" (OCR) stays inside.
_CHAR = r"(?:[^,\n.;:!?|]|\.(?=\S))"
_PART_CHARS = 40  # characters in a part, on each side of a line break
_PART = rf"{_CHAR}{{0,{_PART_CHARS}}}(?:(?:(?<=[A-Za-z])\r?\n(?=[a-z])|-\r?\n){_CHAR}{{0,{_PART_CHARS}}})?"
_PART_MAX = 2 * _PART_CHARS + 3  # the longest part: two runs joined by "-\r\n"
_PARTS_MAX = 4  # parts in any rule below, each followed by at most a comma and one run of spaces
_BEFORE = rf"(?:{_PART},\s*){{0,3}}(?:{_PART}[^\S\n]?)?"  # OCR may glue the last part on ("SKR3P1B2")
_SPACES = re.compile(r"\s+")


class AnchoredPattern(Pattern):
    """`before + core`, searched only near where `core` occurs, with the matches finditer gives.

    The postcode and ZIP rules start with up to four free-text parts, so the regex engine
    tried them at every character and backtracked through each 40-character run: 7 s of
    a 200 KB paste with no address in it. Every match ends its parts where its core matches,
    and the parts are at most _PARTS_MAX x (_PART_MAX + a comma + the text's longest run of
    spaces) long. So only starts that close to a core are tried, left to right and resuming
    after each match as finditer does, with the full pattern, and the matches are the same.
    """

    def __init__(self, name: str, before: str, core: str, score: float, flags: int) -> None:
        super().__init__(name, before + core, score)
        # Presidio reuses a compiled pattern when the recognizer's flags match these.
        self.compiled_regex, self.compiled_with_flags = _Anchored(before + core, core, flags), flags


class _Anchored:
    def __init__(self, full: str, core: str, flags: int) -> None:
        self.full, self.core = re.compile(full, flags), re.compile(f"(?={core})", flags)

    def finditer(self, text: str, timeout=None):
        """Matches of the full pattern, as `full.finditer(text)` returns them. No timeout: Presidio
        skips a pattern whose search times out, which would let an address through."""
        cores = [m.start() for m in self.core.finditer(text)]  # every position a core matches at
        if not cores:
            return
        spaces = max((len(m.group()) for m in _SPACES.finditer(text)), default=0)
        reach = _PARTS_MAX * (_PART_MAX + 1 + spaces)
        pos = 0  # where finditer would resume
        for core in cores:
            pos = max(pos, core - reach)
            while pos <= core:
                match = self.full.match(text, pos)  # lookbehinds still see the text before pos
                if match:
                    yield match
                    pos = max(match.end(), pos + 1)
                else:
                    pos += 1
_D4 = r"\d(?:\s*\d){3}"  # four digits, possibly broken by a line wrap
# US states, DC, territories and military "states", with the letters OCR confuses them with
# ("VI" read as "Vl"): the ZIP rule below must still fire on a misread code.
_US_STATE = "|".join(code.replace("I", "[Il1]").replace("O", "[O0]") for code in (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY AS GU MP PR VI UM AA AE AP").split())
ADDRESS = _recognizer("ADDRESS", [
    Pattern("Street, number first",
            rf"\b{_UNIT}\d{{1,5}}[A-Za-z]?,?\s+(?:[A-Z][\w'-]*\s+){{0,4}}{_STREET_TYPE}\b\.?{_TAIL}", 0.75),
    Pattern("Street, type first (FR/ES/IT)",
            r"\b\d{1,5}[A-Za-z]?,?\s+(?:Rue|Avenue|Boulevard|Bd|Place|Chemin|All[ée]e|Impasse|Quai|Via|Viale"
            rf"|Calle|Avenida|Plaza)(?:\s+[\w'-]+){{1,6}}{_TAIL}", 0.75),
    # US military mail: ship, PSC box or unit, then APO/FPO/DPO + AA/AE/AP + ZIP. Digit groups
    # may break across a line ("Box 47" / "75, APO ..."), as PDF text layers wrap mid-number,
    # and every space may be missing, as OCR drops them ("PSC0489,Box6499,APOAE87326").
    Pattern("Military address",
            rf"\b(?:(?:USNS|USNV|USS|USCGC)\s*[A-Z][\w'-]*(?:\s[A-Z][\w'-]*)?|PSC\s*{_D4},?\s*Box\s*{_D4}"
            rf"|Unit\s*{_D4},?\s*Box\s*{_D4})\s*[,\n]\s*(?:APO|FPO|DPO)\s*(?:AA|AE|AP)\s*\d(?:\s*\d){{4}}(?!\d)", 0.9),
    Pattern("PO box", rf"\b(?:P\.?\s?O\.?\s?Box|Post\s+Office\s+Box)\s+\d+{_TAIL}", 0.75),
    Pattern("Numbered unit", rf"\b(?:Unit|Suite|Flat|Apartment)\s+\d+[A-Za-z]?{_TAIL}", 0.6),
    # A postcode locates a person to a street in the UK and Canada, so it is redacted with
    # up to three comma-separated parts before it (building, street, town) on its line.
    # Digit lookarounds, not \b: OCR glues the province or town to the code ("SKR3P1B2").
    AnchoredPattern("UK postcode", _BEFORE, r"(?<!\d)[A-Z]{1,2}\d[A-Z\d]?\s*\d[ABD-HJLNP-UW-Z]{2}(?![a-z0-9])",
                    0.55, re.MULTILINE),
    AnchoredPattern("CA postal code", _BEFORE,
                    r"(?<!\d)[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d(?!\d)", 0.55, re.MULTILINE),
    # A US ZIP alone covers thousands of people, so the rule needs "town, ST 12345" after at
    # least one comma-separated part; the street line in front of it is taken too.
    AnchoredPattern("US state and ZIP", rf"(?:{_PART},\s*){{1,3}}",
                    rf"(?<![A-Za-z])(?:{_US_STATE})\s*\d{{5}}(?:-\d{{4}})?(?!\d)", 0.55, re.MULTILINE),
])
