"""AuditLog: a fixed field allowlist per event, so a raw value can never be logged."""

from __future__ import annotations

import json

import pytest

from redactit.audit import AuditLog


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_write_appends_one_valid_jsonl_record(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write("vault_purge", purged_count=3, retention_days=30)

    records = _lines(tmp_path / "audit.jsonl")
    assert len(records) == 1
    assert records[0]["event"] == "vault_purge"
    assert records[0]["purged_count"] == 3
    assert records[0]["retention_days"] == 30
    assert records[0]["ts"][4] == "-" and records[0]["ts"][-3] == ":"  # ISO 8601 with offset


def test_write_appends_multiple_lines(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write("engine_start", version="0.1.0")
    log.write("engine_start", version="0.1.0")
    assert len(_lines(tmp_path / "audit.jsonl")) == 2


def test_redaction_event_full_shape(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.write(
        "redaction",
        file_type="txt",
        destination="claude.ai",
        entity_counts={"PERSON": 2, "EMAIL": 1},
        dial=3,
        decisions=[
            {
                "entity_type": "PERSON",
                "detector": "gliner",
                "score": 0.87,
                "threshold": 0.65,
                "rule_id": "entities.person",
                "action": "pseudonymize",
                "validated": False,
                "needs_review": False,
            }
        ],
    )
    record = _lines(tmp_path / "audit.jsonl")[0]
    assert record["destination"] == "claude.ai"
    assert record["decisions"][0]["entity_type"] == "PERSON"


def test_unknown_event_raises(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="unknown event"):
        log.write("not_a_real_event", foo=1)


def test_unexpected_field_raises_naming_the_field(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="raw_value"):
        log.write("vault_purge", purged_count=1, retention_days=30, raw_value="oops")


def test_missing_field_raises(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="retention_days"):
        log.write("vault_purge", purged_count=1)


@pytest.mark.parametrize(
    ("event", "fields"),
    [
        ("model_verified", {"model": "someone@example.com", "source": "download", "revision": "r1", "sha256": "0" * 64}),
        ("model_verified", {"model": "gliner.model.onnx", "source": "Priya Okafor", "revision": "r1", "sha256": "0" * 64}),
        (
            "review_decision",
            {"rule_id": "entities.person", "action": "pseudonymize", "approved": "Priya Okafor"},
        ),
        ("upload_blocked", {"destination": "outbox", "reason_code": "the host timed out for John Smith"}),
    ],
)
def test_email_name_or_free_text_in_any_field_raises(tmp_path, event, fields):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        log.write(event, **fields)


def test_decision_with_bad_entity_type_shape_raises(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        log.write(
            "redaction",
            file_type="txt",
            destination="cli",
            entity_counts={},
            dial=3,
            decisions=[
                {
                    "entity_type": "John Smith",  # not a type name
                    "detector": "gliner",
                    "score": 0.9,
                    "threshold": 0.65,
                    "rule_id": "entities.person",
                    "action": "pseudonymize",
                    "validated": False,
                    "needs_review": False,
                }
            ],
        )


def test_destination_must_be_a_hostname_or_known_alias(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(ValueError):
        log.write("upload_blocked", destination="not a host", reason_code="timeout")
    log.write("upload_blocked", destination="claude.ai", reason_code="timeout")  # ok
    log.write("upload_blocked", destination="outbox", reason_code="host_down")  # ok
