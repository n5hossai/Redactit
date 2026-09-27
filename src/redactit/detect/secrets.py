"""API keys and tokens. Vendor prefixes (AKIA, ghp_, glpat-, ...) are structural
certainties and score high; a bare 40-character AWS secret looks like any base64 run, so
it scores low and needs a cue such as "secret" (registry.py).
"""

from __future__ import annotations

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer

# A private-key block ends at its END line, or at the end of the text when the END line was
# cut off: a truncated key is still a key. Not at a blank line: encrypted PEM and PGP
# armour put one between their headers and the key body.
_KEY_END = r"(?:-----END {kind}-----|\Z)"

PATTERNS = [
    Pattern("AWS access key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", 0.85),
    Pattern("AWS secret key (weak)", r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])", 0.3),
    Pattern("GitHub token", r"\bgh[opsur]_[A-Za-z0-9]{36,}\b", 0.85),
    Pattern("GitHub fine-grained PAT", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", 0.85),
    Pattern("GitLab token", r"\bglpat-[A-Za-z0-9_-]{20,}\b", 0.85),
    Pattern("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}\b", 0.85),
    Pattern("Slack token", r"\bxox[abprs]-\d{6,14}-\d{6,14}-[A-Za-z0-9]{20,40}\b", 0.85),
    Pattern("Stripe live key", r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]{16,}\b", 0.85),
    Pattern("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b", 0.85),
    # OpenAI/Anthropic style; the underscore in Stripe's sk_live_ keeps the two apart.
    Pattern("OpenAI/Anthropic key", r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b", 0.85),
    Pattern("JWT", r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", 0.85),
    Pattern("PEM private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?" + _KEY_END.format(kind="[A-Z ]*PRIVATE KEY"), 0.9),
    Pattern("PGP private key", r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?" + _KEY_END.format(kind="PGP PRIVATE KEY BLOCK"), 0.9),
]


class ApiKeyRecognizer(PatternRecognizer):
    """Vendor tokens, JWTs, AWS keys and whole private-key blocks, all as API_KEY."""

    def __init__(self) -> None:
        # DOTALL so a key block spans lines; lazy .*? stops at the first END line or the end.
        super().__init__(supported_entity="API_KEY", patterns=PATTERNS, global_regex_flags=re.MULTILINE | re.DOTALL)
