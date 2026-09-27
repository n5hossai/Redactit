"""The leak harness's matcher decides what counts as a surviving value, so it gets its own tests."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("leak_run", Path(__file__).parents[1] / "leak" / "run.py")
leak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak)


def found(value: str, text: str) -> bool:
    return leak.fuzzy_contains(leak.normalize(value), leak.normalize(text))


def test_separators_and_line_breaks_do_not_hide_a_value():
    assert found("4111 1111 1111 1111", "card: 4111-1111\n1111 1111 thanks")


def test_one_ocr_error_still_counts_as_a_leak():
    assert found("Priya Okafor", "Contact Priya 0kafor today")


def test_two_errors_do_not_match():
    assert not found("Priya Okafor", "Contact Pria 0kafor today")


def test_short_values_need_an_exact_hit():
    assert found("AB12", "ref ab12")
    assert not found("AB12", "ref ab13")


def test_pseudonym_is_not_a_leak():
    assert not found("Priya Okafor", "Contact [PERSON_1] today")


def test_masked_card_keeping_last_four_is_not_a_full_leak():
    assert not found("4111 1111 1111 1234", "card **** **** **** 1234")
