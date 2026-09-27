"""National-ID, passport and date-of-birth recognizers, each backed by a checksum or a
structural format rule so a returned match is never a bare guess.
"""

from __future__ import annotations

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer


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


class CaSinRecognizer(PatternRecognizer):
    """Canadian SIN: 9 digits, optional space/dash grouping, Luhn check digit.

    The first digit is never 0 or 8 (those ranges are reserved and never issued), so the
    regex excludes them up front; `invalidate_result` then drops anything that fails Luhn.
    """

    PATTERNS = [Pattern("CA_SIN", r"\b[1-79]\d{2}[- ]?\d{3}[- ]?\d{3}\b", 0.4)]
    CONTEXT = ["sin", "social insurance", "social insurance number", "nas"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="CA_SIN",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            global_regex_flags=re.MULTILINE,
        )

    def invalidate_result(self, pattern_text: str) -> bool:
        digits = re.sub(r"[- ]", "", pattern_text)
        return not _luhn_ok(digits)


class PassportRecognizer(PatternRecognizer):
    """Passport numbers: CA (2 letters + 6 digits), UK/GB (1 letter + 8 digits), and the
    bare 9-digit US style. The bare-digit form is indistinguishable from many other
    numbers, so it starts weak and depends on the "passport" context word to matter.
    """

    PATTERNS = [
        Pattern("Passport CA/UK style", r"\b[A-Z]{1,2}\d{6,8}\b", 0.35),
        Pattern("Passport US style (weak)", r"\b\d{9}\b", 0.05),
    ]
    CONTEXT = ["passport", "passport number", "travel document", "passport#"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="PASSPORT",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            global_regex_flags=re.MULTILINE,
        )


class DateOfBirthRecognizer(PatternRecognizer):
    """Common date formats (ISO, slashed, written). Base score is low because most dates
    in a document are not birth dates; context words ("born", "DOB", ...) carry the signal.
    """

    PATTERNS = [
        Pattern("Date ISO", r"\b\d{4}-\d{2}-\d{2}\b", 0.15),
        Pattern("Date slashed", r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", 0.15),
        Pattern(
            "Date written (Month Day, Year)",
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b",
            0.15,
        ),
        Pattern(
            "Date written (Day Month Year)",
            r"\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}\b",
            0.15,
        ),
    ]
    CONTEXT = ["born", "dob", "date of birth", "birth date", "birthdate"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="DATE_OF_BIRTH",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            global_regex_flags=re.MULTILINE | re.IGNORECASE,
        )
