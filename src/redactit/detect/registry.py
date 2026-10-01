"""Builds the Presidio analyzer once; `Detector.detect()` only runs it, never reloads it."""

from __future__ import annotations

import re
from dataclasses import replace

from presidio_analyzer import AnalyzerEngine, EntityRecognizer, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import SlimSpacyNlpEngine
from presidio_analyzer.predefined_recognizers import CreditCardRecognizer, IbanRecognizer

from redactit.types import Span

from . import scaling
from .dictionary import CompanyTermRecognizer
from .patterns import (
    ADDRESS,
    DATE_OF_BIRTH,
    EMAIL,
    MASTERCARD_2_SERIES,
    PASSPORT,
    PHONE,
    UK_NINO,
    CaSinRecognizer,
    CardDigitsRecognizer,
    GluedIbanRecognizer,
    UsSsnRecognizer,
)
from .secrets import ApiKeyRecognizer

# `validated` lets a locked type pass at any score and wins ties in overlap resolution, so
# it is reserved for matches a rule has confirmed: a checksum (Luhn, mod-97), an exact
# dictionary term, or a format specific enough to score 0.85+ on its own (NINO prefixes,
# vendor key prefixes). Bare-digit SSN and passport shapes are never validated.
_CHECKSUM_TYPES = {"CREDIT_CARD", "IBAN", "CA_SIN", "COMPANY_TERM"}
_STRUCTURAL_TYPES = {"UK_NINO", "API_KEY"}
STRUCTURAL_SCORE = 0.85

# Every recognizer and the analyzer call this static method by name; the replacement keeps
# Presidio's results and their order (scaling.py, tested against Presidio's own).
EntityRecognizer.remove_duplicates = staticmethod(scaling.remove_duplicates)


class Detector:
    """`Detector(company_terms=[...])` builds the analyzer once (the slow part);
    `detect(text)` returns `Span`s in Redactit's entity vocabulary.
    """

    def __init__(self, company_terms: list[str] | None = None) -> None:
        # Tokens, lemmas, stop words and punctuation are all Presidio reads from spaCy: no spaCy
        # recognizer is registered. The slim engine skips the parser and spaCy's own NER (spans
        # identical, pattern stage 17-28% faster) and never downloads a missing model.
        nlp_engine = SlimSpacyNlpEngine(models=[{"lang_code": "en", "model_name": "en_core_web_sm"}],
                                        auto_download=False)
        recognizers = [
            # Luhn-checked, plus the Mastercard 2-series range Presidio's regex lacks.
            CreditCardRecognizer(patterns=CreditCardRecognizer.PATTERNS + [MASTERCARD_2_SERIES]),
            CardDigitsRecognizer(),
            GluedIbanRecognizer(),
            IbanRecognizer(supported_entity="IBAN"),  # mod-97 checked
            CaSinRecognizer(),
            UsSsnRecognizer(),
            UK_NINO,
            PASSPORT,
            EMAIL,
            PHONE,
            DATE_OF_BIRTH,
            ADDRESS,
            ApiKeyRecognizer(),
        ]
        if company_terms:
            recognizers.append(CompanyTermRecognizer(company_terms))
        # Built from exactly this list, never `load_predefined_recognizers`, so spaCy's own
        # NER adds nothing: names and most addresses come from the GLiNER model (ner.py).
        registry = RecognizerRegistry(recognizers=recognizers, supported_languages=["en"])
        self._analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"],
                                        context_aware_enhancer=scaling.LinearContextEnhancer())

    def detect(self, text: str) -> list[Span]:
        results = self._analyzer.analyze(text=text, language="en")
        headers = _Headers(text)
        return [_with_context(_to_span(result), text, headers) for result in results]


# A cue word just before a weak match, or in its Markdown table column's header, makes it a
# confident one. Presidio's own enhancer compares single lemmas (so "date of birth" never
# matches) and adds only 0.35, which left a labelled birth date below every threshold.
_CONTEXT = {
    "DATE_OF_BIRTH": re.compile(r"\b(born|dob|d\.o\.b|birth\s*date|date\s*of\s*birth|birthday)\b", re.I),  # OCR: "Dateof"
    "PASSPORT": re.compile(r"\bpassport\b", re.I),
    "PHONE": re.compile(r"\b(phone|tel|telephone|mobile|cell|call|fax|contacts?|reach|text|sms|number)\b", re.I),
    "US_SSN": re.compile(r"\b(ssn|ss\s*(?:no|#|number)|soc(?:ial)?\.?\s*sec(?:urity)?)\b", re.I),
    # No word boundaries: the cue usually sits inside an identifier (aws_secret_access_key).
    "API_KEY": re.compile(r"secret|password|passwd|token|api_?key", re.I),
}
CONTEXT_WINDOW, CONTEXT_SCORE = 80, 0.85


def _with_context(span: Span, text: str, headers: "_Headers") -> Span:
    cue = _CONTEXT.get(span.entity_type)
    if cue and span.score < CONTEXT_SCORE and (
        cue.search(text[max(0, span.start - CONTEXT_WINDOW):span.start]) or cue.search(headers.column(span.start))
    ):
        return replace(span, score=CONTEXT_SCORE, detector=span.detector + ".context")
    return span


class _Headers:
    """Header cells of Markdown tables, with or without leading pipes, looked up per match.

    Each row's table header is memoised, so a table is walked once however many matches it
    holds: re-scanning the text above every match once cost 2.3 s on a 500 KB log.
    """

    def __init__(self, text: str) -> None:
        self.text, self._first_row = text, {}

    def column(self, pos: int) -> str:
        """The header cell of the column `pos` sits in, or "" outside a table."""
        start = self.text.rfind("\n", 0, pos) + 1
        if "|" not in self._line(start):
            return ""
        column = self.text[start:pos].strip().lstrip("|").count("|")
        cells = self._line(self._header_start(start)).strip().strip("|").split("|")
        return cells[column] if column < len(cells) else ""

    def _line(self, start: int) -> str:
        end = self.text.find("\n", start)
        return self.text[start:] if end == -1 else self.text[start:end]

    def _header_start(self, start: int) -> str:
        walked = []
        while start not in self._first_row:  # walk up while the line above is a table row
            walked.append(start)
            above = self.text.rfind("\n", 0, start - 1) + 1 if start else start
            if start == 0 or "|" not in self._line(above):
                self._first_row[start] = start
                break
            start = above
        for row in walked:
            self._first_row[row] = self._first_row[start]
        return self._first_row[start]


def _to_span(result: RecognizerResult) -> Span:
    meta = result.recognition_metadata or {}
    return Span(
        start=result.start,
        end=result.end,
        entity_type=result.entity_type,
        score=result.score,
        detector=meta.get(RecognizerResult.RECOGNIZER_NAME_KEY, ""),
        # A passed checksum raises a match to 1.0, so the score separates confirmed matches
        # from shapes that merely look like one (an OCR-misread IBAN scores 0.6).
        validated=result.entity_type in _CHECKSUM_TYPES | _STRUCTURAL_TYPES and result.score >= STRUCTURAL_SCORE,
    )
