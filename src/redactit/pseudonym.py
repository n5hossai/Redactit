"""Consistent "[TYPE_N]" labels per chat scope, and applying a Policy's decisions to text."""

from __future__ import annotations

from redactit.types import Decision
from redactit.vault import Vault


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
    """The replacement for each decision (sorted by start), computed in reading order so
    [PERSON_1] is the first person mentioned. Images and PDFs label their boxes with these."""
    return [_replacement(d, text[d.span.start:d.span.end], pz) for d in sorted(decisions, key=lambda d: d.span.start)]


def splice(text: str, decisions: list[Decision], subs: list[str]) -> str:
    """`text` with each decision's span (sorted by start) replaced by its substitute."""
    out, pos = [], 0
    for d, sub in zip(sorted(decisions, key=lambda d: d.span.start), subs):
        out.append(text[pos:d.span.start] + sub)
        pos = d.span.end
    return "".join(out) + text[pos:]


def apply(text: str, decisions: list[Decision], pz: Pseudonymizer) -> str:
    """Apply each decision's action to `text`."""
    return splice(text, decisions, replacements(text, decisions, pz))


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
