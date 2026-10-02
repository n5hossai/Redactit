"""Pseudonymizer labels, and each action's replacement spliced into the text."""

from __future__ import annotations

from redactit.pseudonym import Pseudonymizer, remap, replacements, splice
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


def apply(text, decisions, pz) -> str:
    return splice(text, decisions, replacements(text, decisions, pz))


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


def test_every_offset_indexes_the_original_text(tmp_path):
    pz = Pseudonymizer(_vault(tmp_path), scope="chat-1")
    text = "Priya Okafor emailed priya@example.com"
    decisions = [
        _decision(0, 12, "PERSON", "pseudonymize"),
        _decision(21, 38, "EMAIL", "pseudonymize"),
    ]
    assert apply(text, decisions, pz) == "[PERSON_1] emailed [EMAIL_1]"


def test_pseudonyms_number_in_reading_order(tmp_path):
    text = "Ann met Bob."
    ds = [Decision(Span(0, 3, "PERSON", 0.9, "t"), "pseudonymize", "entities.PERSON", "r"),
          Decision(Span(8, 11, "PERSON", 0.9, "t"), "pseudonymize", "entities.PERSON", "r")]
    with Vault(tmp_path / "v.db", b"\x02" * 32) as v:
        assert apply(text, ds, Pseudonymizer(v, "s")) == "[PERSON_1] met [PERSON_2]."


def test_remap_restores_only_this_scopes_labels(tmp_path):
    vault = _vault(tmp_path)
    pz = Pseudonymizer(vault, scope="chat-1")
    assert pz.label("PERSON", "Priya Okafor") == "[PERSON_1]"
    assert pz.label("CREDIT_CARD", "4111 1111 1111 1111") == "[CREDIT_CARD_1]"
    Pseudonymizer(vault, scope="chat-2").label("PERSON", "Jordan Page")
    reply = "Ask [PERSON_1] about [CREDIT_CARD_1]; not [PERSON_2], [person_1] or [PERSON_01]."
    text, seen, restored = remap(reply, vault, "chat-1")
    assert text == "Ask Priya Okafor about 4111 1111 1111 1111; not [PERSON_2], [person_1] or [PERSON_01]."
    assert (seen, dict(restored)) == (3, {"PERSON": 1, "CREDIT_CARD": 1})  # [PERSON_2] seen, not this chat's
    assert remap(reply, vault, "chat-2")[0].startswith("Ask Jordan Page about [CREDIT_CARD_1]")
