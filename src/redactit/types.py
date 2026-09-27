"""Data passed between pipeline stages: detectors emit Spans, the policy turns them into Decisions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Span:
    """A detected entity at [start, end) of the analysed text.

    `score` is in [0, 1]. `validated` means a checksum or format rule confirmed it (Luhn,
    mod-97, SIN rules), which lets locked types apply at every dial position.
    """

    start: int
    end: int
    entity_type: str
    score: float
    detector: str
    validated: bool = False


@dataclass(frozen=True)
class Decision:
    """What to do with one Span, and why.

    `reason` is built from the rule, detector, score and dial only. It must never contain
    the matched text, because it is written to the audit log.
    """

    span: Span
    action: str  # pseudonymize | mask | strike | omit | box
    rule_id: str
    reason: str
    needs_review: bool = False
