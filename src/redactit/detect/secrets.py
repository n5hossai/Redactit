"""API keys and tokens: each pattern is a vendor's own fixed prefix, so a match is a
structural certainty rather than a guess -- scores stay high and case-sensitive.
"""

from __future__ import annotations

import regex as re
from presidio_analyzer import Pattern, PatternRecognizer

# One Pattern per key family. Prefixes are exact fingerprints (AKIA, ghp_, sk-, ...),
# so high base scores are warranted without waiting on context words.
PATTERNS = [
    Pattern("AWS access key", r"\bAKIA[A-Z0-9]{16}\b", 0.85),
    Pattern("GitHub token", r"\b(?:ghp|gho|ghs)_[A-Za-z0-9]{36,}\b", 0.85),
    Pattern("GitHub fine-grained PAT", r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", 0.85),
    Pattern("Slack token", r"\bxox[abprs]-\d{6,14}-\d{6,14}-[A-Za-z0-9]{20,40}\b", 0.85),
    Pattern("Stripe live key", r"\b(?:sk|pk|rk)_live_[A-Za-z0-9]{16,}\b", 0.85),
    Pattern("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b", 0.85),
    # OpenAI/Anthropic style; the underscore in Stripe's sk_live_ keeps the two apart.
    Pattern("OpenAI/Anthropic key", r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b", 0.85),
    Pattern("JWT", r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", 0.7),
    # Non-greedy DOTALL match so the span covers exactly one BEGIN..END block.
    Pattern("PEM private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", 0.9),
]

CONTEXT = ["key", "token", "secret", "api key", "credential", "password", "export"]


class ApiKeyRecognizer(PatternRecognizer):
    """Matches AWS, GitHub, Slack, Stripe, Google, OpenAI/Anthropic-style keys, JWTs, and
    whole PEM private-key blocks. All map to the single API_KEY entity.
    """

    def __init__(self) -> None:
        super().__init__(
            supported_entity="API_KEY",
            patterns=PATTERNS,
            context=CONTEXT,
            global_regex_flags=re.MULTILINE | re.DOTALL,
        )
