"""Builds the Presidio analyzer once; `Detector.detect()` only runs it, never reloads it."""

from __future__ import annotations

import re
from dataclasses import replace

from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import CreditCardRecognizer, IbanRecognizer

from redactit.types import Span

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


class Detector:
    """`Detector(company_terms=[...])` builds the analyzer once (the slow part);
    `detect(text)` returns `Span`s in Redactit's entity vocabulary.
    """

    def __init__(self, company_terms: list[str] | None = None) -> None:
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()
        recognizers = [
            # Luhn-checked, plus the Mastercard 2-series range Presidio's regex lacks.
            CreditCardRecognizer(patterns=CreditCardRecognizer.PATTERNS + [MASTERCARD_2_SERIES]),
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
        self._analyzer = AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=["en"])

    def detect(self, text: str) -> list[Span]:
        results = self._analyzer.analyze(text=text, language="en")
        return [_with_context(_to_span(result), text) for result in results]


# A cue word just before a weak match, or in its Markdown table column's header, makes it a
# confident one. Presidio's own enhancer compares single lemmas (so "date of birth" never
# matches) and adds only 0.35, which left a labelled birth date below every threshold.
_CONTEXT = {
    "DATE_OF_BIRTH": re.compile(r"\b(born|dob|d\.o\.b|birth\s*date|date\s+of\s+birth|birthday)\b", re.I),
    "PASSPORT": re.compile(r"\bpassport\b", re.I),
    "PHONE": re.compile(r"\b(phone|tel|telephone|mobile|cell|call|fax|contacts?|reach|text|sms|number)\b", re.I),
    "US_SSN": re.compile(r"\b(ssn|social\s+security)\b", re.I),
    # No word boundaries: the cue usually sits inside an identifier (aws_secret_access_key).
    "API_KEY": re.compile(r"secret|password|passwd|token|api_?key", re.I),
}
CONTEXT_WINDOW, CONTEXT_SCORE = 80, 0.85


def _with_context(span: Span, text: str) -> Span:
    cue = _CONTEXT.get(span.entity_type)
    if cue and span.score < CONTEXT_SCORE and (
        cue.search(text[max(0, span.start - CONTEXT_WINDOW):span.start]) or cue.search(_column_header(text, span.start))
    ):
        return replace(span, score=CONTEXT_SCORE, detector=span.detector + ".context")
    return span


def _column_header(text: str, pos: int) -> str:
    """The header cell above `pos` when it sits in a Markdown table row, else ""."""
    row_start = text.rfind("\n", 0, pos) + 1
    rows = text[:row_start].split("\n")[:-1]
    if not text[row_start:pos].lstrip().startswith("|"):
        return ""
    header = text[row_start:text.find("\n", pos) if "\n" in text[pos:] else len(text)]
    for row in reversed(rows):  # walk up to the table's first row
        if not row.lstrip().startswith("|"):
            break
        header = row
    column = text.count("|", row_start, pos)
    cells = header.split("|")
    return cells[column] if column < len(cells) else ""


def _to_span(result: RecognizerResult) -> Span:
    meta = result.recognition_metadata or {}
    return Span(
        start=result.start,
        end=result.end,
        entity_type=result.entity_type,
        score=result.score,
        detector=meta.get(RecognizerResult.RECOGNIZER_NAME_KEY, ""),
        validated=result.entity_type in _CHECKSUM_TYPES
        or (result.entity_type in _STRUCTURAL_TYPES and result.score >= STRUCTURAL_SCORE),
    )
