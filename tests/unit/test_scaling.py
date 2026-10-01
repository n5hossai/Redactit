"""The pattern stage on large pastes: each speed fix gives Presidio's own output, and the time
grows with the text, not with its square."""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import generate as gen
import pytest
import regex
from presidio_analyzer import AnalysisExplanation, RecognizerResult
from presidio_analyzer.context_aware_enhancers import LemmaContextAwareEnhancer
from presidio_analyzer.nlp_engine import NlpArtifacts
from presidio_analyzer.predefined_recognizers import CreditCardRecognizer

from redactit.detect import scaling
from redactit.detect.patterns import ADDRESS, AnchoredPattern, _Anchored
from redactit.detect.registry import Detector

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
import timing  # noqa: E402  the timing benchmark's prose: a name, email, phone and card per paragraph

ANCHORED = [p for p in ADDRESS.patterns if isinstance(p, AnchoredPattern)]


@pytest.fixture(scope="module")
def det() -> Detector:
    return Detector(company_terms=["Project Bluefinch"])


@pytest.fixture(scope="module")
def prose() -> dict[str, str]:
    return {size: timing.build_input(f"text_{size}")[1] for size in ("20kb", "200kb")}


def _best(fn, repeats: int = 3) -> float:
    """The fastest of a few runs: a pause from another process only ever adds time."""
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def _digits_and_commas(size: int) -> str:
    """Long runs of short numbers and commas: the shape that makes address rules backtrack."""
    rng, parts, total = random.Random(1), [], 0
    while total < size:
        parts.append("".join(rng.choice("0123456789") for _ in range(rng.randint(1, 6))) + rng.choice([",", ", ", ",\n"]))
        total += len(parts[-1])
    return "".join(parts)[:size]


# --- Same output as Presidio -------------------------------------------------------------


def test_de_duplication_keeps_what_presidio_keeps_in_its_order():
    rng = random.Random(3)
    types = ["PHONE", "CREDIT_CARD", "US_SSN", "PASSPORT"]
    for _ in range(300):
        results = []
        for _ in range(rng.randrange(120)):
            start = rng.randrange(300)
            results.append(RecognizerResult(rng.choice(types), start, start + rng.randrange(1, 40),
                                            rng.choice([0.0, 0.35, 0.6, 1.0])))
        results += [RecognizerResult(r.entity_type, r.start, r.end, r.score) for r in results[::3]]  # exact repeats
        kept = scaling.remove_duplicates(results)
        assert [id(r) for r in kept] == [id(r) for r in scaling.PRESIDIO_REMOVE_DUPLICATES(results)]


def test_the_context_pass_scores_what_presidios_scores(det):
    analyzer = det._analyzer
    text = ("Payment card 4111 1111 1111 1111 on file. Visa debit 4012888888881881, iban GB82 WEST 1234 "
            "5698 7654 32 for the transaction.\n\n  Unrelated words here; the cardholder paid by credit card. ") * 3
    artifacts = analyzer.nlp_engine.process_text(text, "en")
    card = next(r for r in analyzer.registry.recognizers if isinstance(r, CreditCardRecognizer))
    meta = {RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: card.id, RecognizerResult.RECOGNIZER_NAME_KEY: card.name}
    # A weak card match starting at every character of every token, so every way of finding
    # a match's token is tried.
    raw = [RecognizerResult("CREDIT_CARD", i, i + 1, 0.3, AnalysisExplanation(card.name, 0.3), dict(meta))
           for token in artifacts.tokens for i in range(token.idx, token.idx + len(token))]
    recognizers = analyzer.registry.get_recognizers(language="en", all_fields=True)
    ours = scaling.LinearContextEnhancer().enhance_using_context(text, raw, artifacts, recognizers)
    theirs = LemmaContextAwareEnhancer().enhance_using_context(text, raw, artifacts, recognizers)
    assert [(r.start, r.score) for r in ours] == [(r.start, r.score) for r in theirs]
    assert {round(r.score, 6) for r in ours} == {0.3, 0.65}  # some raised by a cue, some not: the comparison means something


def _addressy(rng: random.Random, vf) -> str:
    seps = [", ", ",", ",\n", ",      \n\n   ", "\n", " ", "-\n", "\r\n", "; ", ". ", ".", "|", "\t,\t"]
    pieces = [rng.choice([vf.address, vf.fake.city, vf.filler_sentence,
                          lambda: rng.choice(["SW1A 1AA", "K1A 0B1", "SKR3P1B2", "M5V3L9", "CA 90210", "Vl 77725",
                                              "NY 10001-1234", "TX75001", "EC1A1BB", "AB12 3CD"]),
                          lambda: "".join(rng.choice("ab Cd,1 2\n-.") for _ in range(rng.randint(1, 60)))])()
              + rng.choice(seps) for _ in range(rng.randint(1, 12))]
    return "".join(pieces)


def test_anchored_address_rules_match_exactly_what_the_plain_rules_match():
    rng, vf = random.Random(7), gen.ValueFactory(7)
    plain = {p.name: regex.compile(p.regex, ADDRESS.global_regex_flags) for p in ANCHORED}
    found = 0
    for _ in range(400):
        text = _addressy(rng, vf)
        for p in ANCHORED:
            expected = [m.span() for m in plain[p.name].finditer(text)]
            assert [m.span() for m in p.compiled_regex.finditer(text)] == expected, (p.name, text)
            found += len(expected)
    assert found > 400  # the texts are full of addresses, so the comparison means something


def test_presidio_keeps_the_anchored_search(det):
    """Presidio recompiles a pattern, as a plain and slow regex, when its flags differ from the recognizer's."""
    det.detect("1 Main Street, Springfield, IL 62704")
    assert [p.name for p in ANCHORED] == ["UK postcode", "CA postal code", "US state and ZIP"]
    assert all(isinstance(p.compiled_regex, _Anchored) for p in ANCHORED)


def test_spacy_never_downloads_a_missing_model(det):
    assert det._analyzer.nlp_engine.auto_download is False


# --- Time ---------------------------------------------------------------------------------
# Each limit is a ratio, which a slower machine does not change, or an absolute cap far above
# what an 8-core laptop needs, so a slow CI runner passes and a return to the old cost fails.


def test_the_address_rules_take_a_second_or_less_on_a_200_kb_paste(prose):
    # 0.04-0.06 s on the laptop; 7 s before the rules were anchored on their postcode or ZIP.
    assert _best(lambda: ADDRESS.analyze(prose["200kb"], ["ADDRESS"])) <= 1.0


def test_the_pattern_stage_grows_with_the_text_not_its_square(det, prose):
    # 200 KB against 20 KB: 10x if linear (fixed costs make it less), 23x before the fixes.
    # The 20 KB time is the mean over the ten 20 KB pieces of the same paste, timed right
    # after the whole paste, so a busy machine slows both sides of a ratio alike; the best
    # of three ratios is kept, since a quadratic cost would show in all three.
    text = prose["200kb"]
    pieces = [text[i:i + 20_000] for i in range(0, len(text), 20_000)]
    ratios = [_best(lambda: det.detect(text), 1) / (_best(lambda: [det.detect(p) for p in pieces], 1) / len(pieces))
              for _ in range(3)]
    assert min(ratios) <= 12, ratios


def test_digit_and_comma_runs_take_bounded_time(det):
    text = _digits_and_commas(200_000)
    assert _best(lambda: ADDRESS.analyze(text, ["ADDRESS"])) <= 1.0  # 0.13 s on the laptop; 6 s before
    # Every recognizer and Presidio's passes, without spaCy's tokens: spaCy is linear but slow
    # on 80,000 number tokens, and it is not what backtracked. 0.4 s on the laptop.
    no_tokens = NlpArtifacts([], [], [], [], det._analyzer.nlp_engine, "en")
    assert _best(lambda: det._analyzer.analyze(text=text, language="en", nlp_artifacts=no_tokens), 1) <= 10
