"""Every installed distribution must be MIT, Apache-2.0, BSD or ISC (see
docs/PLAN.md #3/#8) unless explicitly flagged below. This fails the build
the moment a copyleft dependency (GPL/LGPL/AGPL/MPL) sneaks in, instead of
that being discovered at release time.
"""

import importlib.metadata as metadata
import re

# Distributions with a human-reviewed exception. One line each: SPDX id(s)
# plus why the license is acceptable despite not being a literal MIT/
# Apache-2.0/BSD/ISC match. Never add an entry here to work around a real
# copyleft dependency -- only for licenses that are permissive in substance.
FLAGGED = {
    "pillow": "MIT-CMU -- historical PIL license, MIT-equivalent permissive terms",
    "defusedxml": "PSF-2.0 -- permissive; standard XML entity-attack guard for DOCX parsing",
    "numpy": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 -- aggregate of "
    "permissive/public-domain pieces (transitive via rapidocr-onnxruntime, test-only)",
}

# Not third-party runtime code we ship; excluded per the task brief.
IGNORE = {"redactit", "pip", "setuptools", "wheel", "uv"}

# tqdm's metadata literally reads "MPL-2.0 AND MIT", but tqdm's own LICENCE
# file grants a *choice* of either license, not both at once -- we exercise
# the MIT option, so this is not a real copyleft conflict despite the "AND".
DUAL_CHOICE = {"tqdm": "MIT"}

ALLOWED = re.compile(r"\b(MIT|Apache[- ]?2\.0|BSD|ISC)\b", re.I)
COPYLEFT = re.compile(r"\b(AGPL|LGPL|GPL|MPL)\b", re.I)


def _permissive(clause: str) -> bool:
    """A single license clause (no boolean operators left) is allowed."""
    return bool(ALLOWED.search(clause)) and not COPYLEFT.search(clause)


def _license_ok(text: str) -> bool:
    """Evaluate an SPDX-ish boolean expression: OR only needs one permissive
    option (a licensee's choice); AND needs every part permissive, since all
    of them apply at once to the combined work."""
    or_parts = re.split(r"\bOR\b", text, flags=re.I)
    if len(or_parts) > 1:
        return any(_license_ok(p) for p in or_parts)
    and_parts = re.split(r"\bAND\b", text, flags=re.I)
    if len(and_parts) > 1:
        return all(_license_ok(p) for p in and_parts)
    return _permissive(text)


def _license_text(dist: metadata.Distribution) -> str | None:
    """License-Expression (SPDX) first, then the free-text License field,
    then classifiers -- most dists only populate one of the three."""
    meta = dist.metadata
    classifiers = "; ".join(c for c in meta.get_all("Classifier") or [] if c.startswith("License"))
    return meta.get("License-Expression") or meta.get("License") or classifiers or None


def test_installed_dependencies_are_permissively_licensed():
    failures = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        key = name.lower()
        if key in IGNORE or key in FLAGGED:
            continue
        if key in DUAL_CHOICE:
            assert _permissive(DUAL_CHOICE[key]), f"{name}: recorded dual-choice license is not permissive"
            continue
        text = _license_text(dist)
        if not text or not _license_ok(text):
            failures.append(f"{name}: {text!r}")
    assert not failures, "Non-permissive or unlicensed dependencies:\n" + "\n".join(failures)
