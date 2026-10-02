"""Consistent "[TYPE_N]" labels per chat scope, and applying a Policy's decisions to text."""

from __future__ import annotations

import re
from collections import Counter

from redactit.types import Decision
from redactit.vault import Vault

# A label as `Pseudonymizer.label` writes it: "[PERSON_1]", "[CREDIT_CARD_12]".
LABEL = re.compile(r"\[([A-Z](?:[A-Z_]{0,30}[A-Z])?)_([1-9][0-9]{0,8})\]")


class Pseudonymizer:
    """Same value (case-folded, whitespace-collapsed) in the same scope gets the same label.

    Normalisation and per-(scope, type) counters live in the vault, so different
    scopes stay independent and the mapping survives across calls.
    """

    def __init__(self, vault: Vault, scope: str) -> None:
        self._vault = vault
        self._scope = scope

    def label(self, entity_type: str, value: str) -> str:
        n = self._vault.number_for(self._scope, entity_type, value)
        return f"[{entity_type}_{n}]"


def replacements(text: str, decisions: list[Decision], pz: Pseudonymizer) -> list[str]:
    """The replacement for each decision. `decisions` must be sorted by start (the engine
    sorts them once): labels are numbered in that order, so [PERSON_1] is the first person
    mentioned. Images and PDFs label their boxes with these."""
    return [_replacement(d, text[d.span.start:d.span.end], pz) for d in decisions]


def splice(text: str, decisions: list[Decision], subs: list[str]) -> str:
    """`text` with each decision's span (sorted by start, not overlapping) replaced by its substitute."""
    out, pos = [], 0
    for d, sub in zip(decisions, subs):
        out.append(text[pos:d.span.start] + sub)
        pos = d.span.end
    return "".join(out) + text[pos:]


def remap(text: str, vault: Vault, scope: str) -> tuple[str, int, Counter]:
    """`text` with each of this scope's labels replaced by the value it stands for.

    Only labels the vault holds for `scope` are replaced; any other "[TYPE_N]" (another
    chat's, a purged one, or one the AI made up) stays as it is. Returns the text, the
    number of labels seen, and how many were restored per type, for the audit log.
    """
    restored: Counter = Counter()
    seen = 0

    def real(m: re.Match) -> str:
        nonlocal seen
        seen += 1
        value = vault.value_for(scope, m.group(1), int(m.group(2)))
        if value is None:
            return m.group(0)
        restored[m.group(1)] += 1
        return value

    return LABEL.sub(real, text), seen, restored


def _replacement(d: Decision, original: str, pz: Pseudonymizer) -> str:
    if d.action == "pseudonymize":
        return pz.label(d.span.entity_type, original)
    if d.action == "mask":
        return "".join("*" if c.isalnum() else c for c in original)
    if d.action in ("strike", "box"):
        return "█" * len(original)
    if d.action == "omit":
        return ""
    raise ValueError(f"unknown action: {d.action}")
