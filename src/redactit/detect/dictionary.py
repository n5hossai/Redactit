"""Exact company/project-codename matching: case-insensitive, whole-word, longest term wins."""

from __future__ import annotations

from presidio_analyzer import PatternRecognizer


class CompanyTermRecognizer(PatternRecognizer):
    """Deny-list recognizer for the caller's own company and project codenames.

    Terms are sorted longest-first: Presidio's deny-list regex is one alternation, and an
    alternation takes whichever branch matches first, so the longest term must come first
    or a shorter term that is its prefix would win instead.
    """

    def __init__(self, company_terms: list[str]) -> None:
        terms = sorted(dict.fromkeys(company_terms), key=len, reverse=True)
        super().__init__(supported_entity="COMPANY_TERM", deny_list=terms, deny_list_score=1.0)
