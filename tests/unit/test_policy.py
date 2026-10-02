"""Policy loading, tighten-only merge, dial thresholds and decide()."""

from __future__ import annotations

from pathlib import Path

import pytest

from redactit.policy import Policy, PolicyError, load_policy
from redactit.types import Span

EXAMPLE = Path(__file__).resolve().parents[2] / "src" / "redactit" / "policy.default.yaml"


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_the_shipped_example_policy():
    policy = load_policy(user=EXAMPLE)
    assert policy.entities["CREDIT_CARD"].locked is True
    assert policy.entities["PERSON"].action == "pseudonymize"
    assert policy.effective_dial() == 3


def test_bad_key_error_names_the_field(tmp_path):
    bad = write(tmp_path / "user.yaml", "dial:\n  postion: 3\n")  # typo'd key
    with pytest.raises(ValueError, match="postion"):
        load_policy(user=bad)


def test_bad_action_error_names_the_value(tmp_path):
    bad = write(tmp_path / "user.yaml", "entities:\n  PERSON: {action: shred}\n")
    with pytest.raises(ValueError, match="entities.PERSON"):
        load_policy(user=bad)


def test_admin_floor_raises_a_lower_user_position(tmp_path):
    managed = write(tmp_path / "managed.yaml", "dial: {position: 4, admin_floor: 4}\n")
    user = write(tmp_path / "user.yaml", "dial: {position: 1}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.effective_dial() == 4


def test_user_may_raise_above_the_floor(tmp_path):
    managed = write(tmp_path / "managed.yaml", "dial: {admin_floor: 2}\n")
    user = write(tmp_path / "user.yaml", "dial: {position: 5}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.effective_dial() == 5


def test_locked_type_cannot_be_unlocked_by_user(tmp_path):
    managed = write(tmp_path / "managed.yaml", "entities:\n  CREDIT_CARD: {action: mask, locked: true}\n")
    user = write(tmp_path / "user.yaml", "entities:\n  CREDIT_CARD: {action: mask, locked: false}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.entities["CREDIT_CARD"].locked is True


def test_enabled_type_cannot_be_disabled_by_user(tmp_path):
    managed = write(tmp_path / "managed.yaml", "entities:\n  PERSON: {action: pseudonymize, enabled: true}\n")
    user = write(tmp_path / "user.yaml", "entities:\n  PERSON: {action: pseudonymize, enabled: false}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.entities["PERSON"].enabled is True


def test_user_may_enable_a_type_managed_left_disabled(tmp_path):
    managed = write(tmp_path / "managed.yaml", "entities:\n  DATE_OF_BIRTH: {action: mask, enabled: false}\n")
    user = write(tmp_path / "user.yaml", "entities:\n  DATE_OF_BIRTH: {action: mask, enabled: true}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.entities["DATE_OF_BIRTH"].enabled is True


def test_user_may_lock_a_type_managed_left_unlocked(tmp_path):
    managed = write(tmp_path / "managed.yaml", "entities:\n  PERSON: {action: pseudonymize}\n")
    user = write(tmp_path / "user.yaml", "entities:\n  PERSON: {action: pseudonymize, locked: true}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.entities["PERSON"].locked is True


def test_site_dial_raises_but_never_lowers(tmp_path):
    managed = write(tmp_path / "managed.yaml", "dial: {admin_floor: 1}\nsites:\n  chatgpt.com: {dial: 4}\n")
    user = write(tmp_path / "user.yaml", "dial: {position: 2}\nsites:\n  chatgpt.com: {dial: 1}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.effective_dial("chatgpt.com") == 4
    assert policy.effective_dial("other.example") == 2


def test_custom_terms_are_unioned_but_only_the_admin_may_allowlist(tmp_path):
    managed = write(tmp_path / "managed.yaml", "custom_terms: {terms: [ProjectFalcon]}\nallowlist: [Redactit]\n")
    user = write(tmp_path / "user.yaml", "custom_terms: {terms: [Nimbus]}\nallowlist: [Priya Okafor]\n")
    policy = load_policy(user=user, managed=managed)
    assert set(policy.custom_terms.terms) == {"ProjectFalcon", "Nimbus"}
    assert policy.allowlist == ["Redactit"]


def test_missing_custom_terms_file_fails_closed(tmp_path):
    user = write(tmp_path / "user.yaml", "custom_terms: {files: [clients.txt]}\n")
    with pytest.raises(PolicyError, match="not found"):
        load_policy(user).company_terms()


def test_admin_dial_position_holds_when_the_user_file_leaves_it_alone(tmp_path):
    managed = write(tmp_path / "managed.yaml", "dial: {position: 5}\n")
    user = write(tmp_path / "user.yaml", "custom_terms: {terms: [Nimbus]}\n")
    assert load_policy(user, managed).effective_dial() == 5


def test_company_terms_reads_listed_files(tmp_path):
    terms_file = write(tmp_path / "terms.txt", "Acme Corp\nProjectFalcon\n")
    user = write(tmp_path / "user.yaml", f"custom_terms: {{files: [{terms_file.name}], terms: [Nimbus]}}\n")
    policy = load_policy(user=user)
    assert set(policy.company_terms()) == {"Acme Corp", "ProjectFalcon", "Nimbus"}


def _policy(**entities) -> Policy:
    return Policy(entities=entities)


def test_decide_drops_disabled_type():
    policy = _policy(PERSON={"action": "pseudonymize", "enabled": False})
    span = Span(0, 5, "PERSON", 0.99, "gliner")
    assert policy.decide("Priya", [span]) == []


def test_decide_drops_below_threshold():
    policy = _policy(PERSON={"action": "pseudonymize"})
    span = Span(0, 5, "PERSON", 0.10, "gliner")  # dial 3 threshold for model types: 0.50
    assert policy.decide("Priya", [span]) == []


def test_decide_drops_allowlisted_value():
    policy = Policy(entities={"PERSON": {"action": "pseudonymize"}}, allowlist=["priya okafor"])
    span = Span(0, 12, "PERSON", 0.99, "gliner")
    assert policy.decide("Priya Okafor", [span]) == []


def test_allowlist_never_applies_to_locked_type():
    policy = Policy(
        entities={"CREDIT_CARD": {"action": "mask", "locked": True}},
        allowlist=["4111111111111111"],
    )
    span = Span(0, 16, "CREDIT_CARD", 0.99, "luhn", validated=True)
    decisions = policy.decide("4111111111111111", [span])
    assert len(decisions) == 1


def test_validated_locked_span_always_passes():
    policy = _policy(CREDIT_CARD={"action": "mask", "locked": True})
    span = Span(0, 5, "CREDIT_CARD", 0.01, "luhn", validated=True)
    decisions = policy.decide("41111", [span])
    assert len(decisions) == 1


def test_reason_never_contains_matched_text():
    policy = _policy(PERSON={"action": "pseudonymize"})
    span = Span(0, 12, "PERSON", 0.87, "gliner")
    decisions = policy.decide("Priya Okafor", [span])
    assert decisions[0].reason == "gliner score=0.87 dial=3 threshold=0.50"
    assert "Priya" not in decisions[0].reason
    assert decisions[0].rule_id == "entities.PERSON"


def test_equal_overlap_prefers_validated_then_score():
    policy = _policy(PERSON={"action": "pseudonymize"}, COMPANY_TERM={"action": "pseudonymize"})
    validated = Span(0, 12, "PERSON", 0.70, "validator", validated=True)
    higher_score = Span(0, 12, "COMPANY_TERM", 0.99, "gliner")
    decisions = policy.decide("Priya Okafor", [validated, higher_score])
    assert [d.span for d in decisions] == [validated]


def test_a_full_tie_names_the_same_type_whatever_order_the_spans_arrive_in():
    policy = _policy(PASSPORT={"action": "mask"}, US_SSN={"action": "mask"})
    passport = Span(0, 9, "PASSPORT", 0.85, "passport_pattern")
    ssn = Span(0, 9, "US_SSN", 0.85, "us_ssn_pattern")
    first = policy.decide("123456789", [passport, ssn])
    second = policy.decide("123456789", [ssn, passport])
    assert [d.span.entity_type for d in first] == [d.span.entity_type for d in second] == ["US_SSN"]


def test_overlapping_spans_redact_their_union():
    """A validated company term inside an email must not leave the email's local part behind."""
    policy = _policy(EMAIL={"action": "pseudonymize"}, COMPANY_TERM={"action": "pseudonymize"})
    text = "priya.okafor@northwind.com"
    email = Span(0, len(text), "EMAIL", 0.95, "email")
    term = Span(13, 22, "COMPANY_TERM", 1.0, "dictionary", validated=True)
    [decision] = policy.decide(text, [email, term])
    assert (decision.span.start, decision.span.end, decision.span.entity_type) == (0, len(text), "EMAIL")


def test_decisions_are_non_overlapping_and_sorted_by_start():
    policy = _policy(PERSON={"action": "pseudonymize"}, EMAIL={"action": "pseudonymize"})
    person = Span(6, 18, "PERSON", 0.99, "gliner")  # "Priya Okafor"
    email = Span(0, 5, "EMAIL", 0.99, "gliner")  # "email"
    decisions = policy.decide("email Priya Okafor", [person, email])
    assert [d.span.start for d in decisions] == [0, 6]


def test_needs_review_always_mode():
    policy = Policy(entities={"PERSON": {"action": "pseudonymize"}}, review={"mode": "always"})
    span = Span(0, 12, "PERSON", 0.99, "gliner", validated=True)
    decisions = policy.decide("Priya Okafor", [span])
    assert decisions[0].needs_review is True


def test_needs_review_low_confidence_only():
    policy = _policy(PERSON={"action": "pseudonymize"})
    close_to_threshold = Span(0, 12, "PERSON", 0.60, "gliner")  # < 0.50 + 0.15
    confident = Span(0, 12, "PERSON", 0.95, "gliner")
    assert policy.decide("Priya Okafor", [close_to_threshold])[0].needs_review is True
    assert policy.decide("Priya Okafor", [confident])[0].needs_review is False


def test_review_mode_is_tighten_only(tmp_path):
    managed = write(tmp_path / "managed.yaml", "review: {mode: always}\n")
    user = write(tmp_path / "user.yaml", "review: {mode: low_confidence_only}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.review.mode == "always"


def test_vault_retention_takes_the_stricter_value(tmp_path):
    managed = write(tmp_path / "managed.yaml", "vault: {retention_days: 30}\n")
    user = write(tmp_path / "user.yaml", "vault: {retention_days: 7}\n")
    policy = load_policy(user=user, managed=managed)
    assert policy.vault.retention_days == 7


def test_no_policy_files_still_redacts_everything_by_default():
    policy = load_policy(None)
    assert policy.entities["PERSON"].action == "pseudonymize"
    assert policy.entities["CREDIT_CARD"].locked


def test_admin_may_loosen_a_default_but_a_user_may_not(tmp_path):
    managed = tmp_path / "managed.yaml"
    managed.write_text("entities:\n  DATE_OF_BIRTH: {enabled: false}\n", encoding="utf-8")
    assert not load_policy(None, managed).entities["DATE_OF_BIRTH"].enabled
    user = tmp_path / "user.yaml"
    user.write_text("entities:\n  PERSON: {enabled: false}\n", encoding="utf-8")
    assert load_policy(user).entities["PERSON"].enabled
