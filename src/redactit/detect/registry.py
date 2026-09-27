"""Builds the Presidio analyzer once; `Detector.detect()` only runs it, never reloads it."""

from __future__ import annotations

import tldextract
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    PhoneRecognizer,
    UkNinoRecognizer,
    UsSsnRecognizer,
)

from redactit.types import Span

from .dictionary import CompanyTermRecognizer
from .patterns import MASTERCARD_2_SERIES, CaSinRecognizer, DateOfBirthRecognizer, PassportRecognizer
from .secrets import ApiKeyRecognizer

# Entity types whose recognizer only ever returns a result after a checksum or an exact
# structural/format rule passed (never a bare guess) -- every Span of these types is
# validated. EMAIL, PHONE and DATE_OF_BIRTH have no such rule and stay unvalidated.
_VALIDATED_TYPES = {
    "CREDIT_CARD",
    "IBAN",
    "CA_SIN",
    "US_SSN",
    "UK_NINO",
    "PASSPORT",
    "API_KEY",
    "COMPANY_TERM",
}

_SUPPORTED_PHONE_REGIONS = ("US", "CA", "GB")


def _offline_email_recognizer() -> EmailRecognizer:
    """EmailRecognizer validates domains via tldextract, which fetches the public suffix
    list over the network on a cold cache. Point it at the bundled snapshot only, so
    every call -- cold cache or not -- stays local. Re-armed per Detector so a test can
    set TLDEXTRACT_CACHE to a fresh directory and see it take effect immediately.
    """
    tldextract.extract = tldextract.TLDExtract(suffix_list_urls=())
    return EmailRecognizer(supported_entity="EMAIL")


class Detector:
    """Public entry point: `Detector(company_terms=[...])` builds the analyzer once (the
    slow part); `detect(text)` returns `Span`s in redactit's own entity vocabulary.
    """

    def __init__(self, company_terms: list[str] | None = None) -> None:
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()

        recognizers = [
            # Luhn-checked; already named CREDIT_CARD. Extended with the Mastercard
            # 2-series pattern Presidio's own regex is missing (see patterns.py).
            CreditCardRecognizer(patterns=CreditCardRecognizer.PATTERNS + [MASTERCARD_2_SERIES]),
            _offline_email_recognizer(),
            IbanRecognizer(supported_entity="IBAN"),  # mod-97 checked
            PhoneRecognizer(
                supported_entity="PHONE", supported_regions=_SUPPORTED_PHONE_REGIONS, leniency=0
            ),
            UsSsnRecognizer(),  # already named US_SSN
            UkNinoRecognizer(),  # already named UK_NINO; prefix rules built in
            CaSinRecognizer(),
            PassportRecognizer(),
            DateOfBirthRecognizer(),
            ApiKeyRecognizer(),
        ]
        if company_terms:
            recognizers.append(CompanyTermRecognizer(company_terms))

        # Built from exactly this list, never `load_predefined_recognizers`, so no
        # SpacyRecognizer is ever added and spaCy NER emits nothing on its own --
        # PERSON and ADDRESS are the lead's GLiNER model's job, not ours.
        registry = RecognizerRegistry(recognizers=recognizers, supported_languages=["en"])
        self._analyzer = AnalyzerEngine(
            registry=registry, nlp_engine=nlp_engine, supported_languages=["en"]
        )

    def detect(self, text: str) -> list[Span]:
        results = self._analyzer.analyze(text=text, language="en")
        return [_to_span(result) for result in results]


def _to_span(result: RecognizerResult) -> Span:
    detector = ""
    if result.recognition_metadata:
        detector = result.recognition_metadata.get(RecognizerResult.RECOGNIZER_NAME_KEY, "")
    return Span(
        start=result.start,
        end=result.end,
        entity_type=result.entity_type,
        score=result.score,
        detector=detector,
        validated=result.entity_type in _VALIDATED_TYPES,
    )
