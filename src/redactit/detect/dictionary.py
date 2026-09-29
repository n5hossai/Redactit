"""The caller's own client names and project codenames, matched case-insensitively."""

from __future__ import annotations

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer

LONG_TERM = 8  # letters and digits; a codename this long does not occur inside other words by chance


class CompanyTermRecognizer(PatternRecognizer):
    """One pattern per term. Spaces inside a term may be missing, doubled or a line break,
    because OCR drops spaces ("ProjectSlateOrchard") and PDFs wrap lines. A long term may
    also touch the text around it (OCR glues labels to values); a short one must stand as a
    whole word, or "Acme" would match inside "Acmeville".
    """

    def __init__(self, company_terms: list[str]) -> None:
        terms = [t for t in dict.fromkeys(company_terms) if t.strip()]
        super().__init__(
            supported_entity="COMPANY_TERM",
            patterns=[Pattern(f"term {i}", _regex(t), 1.0) for i, t in enumerate(terms)],
            global_regex_flags=re.IGNORECASE | re.MULTILINE,
            name="company_term",
        )


def _regex(term: str) -> str:
    body = r"[\s_-]*".join(re.escape(word) for word in term.split())
    if sum(c.isalnum() for c in term) >= LONG_TERM:
        return body
    return rf"(?<![^\W_]){body}(?![^\W_])"
