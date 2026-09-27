"""Pseudonymizer labels and apply()'s right-to-left splice of policy decisions."""

from __future__ import annotations

from redactit.pseudonym import Pseudonymizer, apply
from redactit.types import Decision, Span
from redactit.vault import Vault

KEY = b"\x02" * 32


def _vault(tmp_path) -> Vault:
    return Vault(tmp_path / "vault.db", KEY)


def test_same_normalised_value_gets_the_same_label(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    assert pz.label("PERSON", "Priya Okafor") == "[PERSON_1]"
    assert pz.label("PERSON", "  priya   OKAFOR ") == "[PERSON_1]"
    assert pz.label("PERSON", "Jordan Page") == "[PERSON_2]"


def test_counters_are_independent_per_entity_type(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    assert pz.label("PERSON", "Priya Okafor") == "[PERSON_1]"
    assert pz.label("EMAIL", "priya@example.com") == "[EMAIL_1]"


def test_different_scopes_are_independent(tmp_path):
    vault = _vault(tmp_path)
    a = Pseudonymizer(vault, scope="chat-a")
    b = Pseudonymizer(vault, scope="chat-b")
    assert a.label("PERSON", "Priya Okafor") == "[PERSON_1]"
    assert b.label("PERSON", "Jordan Page") == "[PERSON_1]"
    assert a.label("PERSON", "Jordan Page") == "[PERSON_2]"


def _decision(start, end, entity_type, action) -> Decision:
    span = Span(start, end, entity_type, 0.9, "gliner")
    return Decision(span=span, action=action, rule_id=f"entities.{entity_type}", reason="x")


def test_apply_pseudonymize(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "Contact Priya Okafor now"
    decisions = [_decision(8, 20, "PERSON", "pseudonymize")]
    assert apply(text, decisions, pz) == "Contact [PERSON_1] now"


def test_apply_mask_keeps_separators(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "Call 555-123-4567 today"
    decisions = [_decision(5, 17, "PHONE", "mask")]
    assert apply(text, decisions, pz) == "Call ***-***-**** today"


def test_apply_strike_and_box_fill_with_full_blocks(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "secret: abc123"
    strike = apply(text, [_decision(8, 14, "API_KEY", "strike")], pz)
    box = apply(text, [_decision(8, 14, "API_KEY", "box")], pz)
    assert strike == box == "secret: ██████"


def test_apply_omit_removes_the_value(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "note: abc123 end"
    decisions = [_decision(6, 12, "API_KEY", "omit")]
    assert apply(text, decisions, pz) == "note:  end"


def test_apply_is_right_to_left_so_earlier_offsets_stay_valid(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "Priya Okafor emailed priya@example.com"
    decisions = [
        _decision(0, 12, "PERSON", "pseudonymize"),
        _decision(21, 38, "EMAIL", "pseudonymize"),
    ]
    assert apply(text, decisions, pz) == "[PERSON_1] emailed [EMAIL_1]"
