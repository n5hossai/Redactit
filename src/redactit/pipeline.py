"""One engine behind every interface: detect -> decide -> apply -> audit."""

from collections import Counter
from dataclasses import dataclass

from redactit import models
from redactit.audit import AuditLog
from redactit.detect.ner import GlinerNer
from redactit.detect.registry import Detector
from redactit.policy import Policy
from redactit.pseudonym import Pseudonymizer, apply
from redactit.types import Decision
from redactit.vault import Vault


@dataclass(frozen=True)
class Result:
    text: str
    decisions: list[Decision]


class Engine:
    """Loads the analyzer and model once (seconds), then redacts many texts (milliseconds)."""

    def __init__(self, policy: Policy, vault: Vault, audit: AuditLog | None = None):
        self.policy, self.vault, self.audit = policy, vault, audit
        self.detector = Detector(company_terms=policy.company_terms())
        self.ner = GlinerNer(models.path_for("gliner/model.onnx"), models.path_for("gliner/tokenizer.json"))

    def redact(self, text: str, scope: str, *, file_type: str = "txt", destination: str = "cli",
               site: str | None = None) -> Result:
        """`scope` keeps pseudonyms consistent: one chat, one folder run, one CLI call."""
        decisions = self.policy.decide(text, self.detector.detect(text) + self.ner.detect(text), site)
        result = Result(apply(text, decisions, Pseudonymizer(self.vault, scope)), decisions)
        if self.audit:
            self.audit.write(
                "redaction",
                file_type=file_type,
                destination=destination,
                dial=self.policy.effective_dial(site),
                entity_counts=dict(Counter(d.span.entity_type for d in decisions)),
                decisions=[_audit_entry(d) for d in decisions],
            )
        return result


def _audit_entry(d: Decision) -> dict:
    # Structured fields only; the audit log refuses anything that looks like free text.
    s = d.span
    return {"entity_type": s.entity_type, "detector": s.detector.lower(), "score": round(s.score, 3),
            "threshold": d.threshold, "rule_id": d.rule_id, "action": d.action, "validated": s.validated,
            "needs_review": d.needs_review}
