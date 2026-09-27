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


def apply(text: str, decisions: list[Decision], pz: Pseudonymizer) -> str:
    """Apply each decision's action to `text`. Right-to-left so earlier offsets stay valid."""
    for d in sorted(decisions, key=lambda d: d.span.start, reverse=True):
        start, end = d.span.start, d.span.end
        original = text[start:end]
        if d.action == "pseudonymize":
            replacement = pz.label(d.span.entity_type, original)
        elif d.action == "mask":
            replacement = "".join("*" if c.isalnum() else c for c in original)
        elif d.action in ("strike", "box"):
            replacement = "█" * len(original)
        elif d.action == "omit":
            replacement = ""
        else:
            raise ValueError(f"unknown action: {d.action}")
        text = text[:start] + replacement + text[end:]
    return text
