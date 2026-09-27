"""Every installed distribution must be MIT, Apache-2.0, BSD or ISC (docs/PLAN.md §3, §8)
unless it is a reviewed exception below.

Checks both the declared license and the license files a wheel bundles: a package can
declare "Apache 2.0" while shipping an LGPL library inside it.
"""

import importlib.metadata as metadata
import re

# Reviewed exceptions, one reason each. Everything here is test-only unless it says
# otherwise; never add an entry to get past a real copyleft dependency in shipped code.
FLAGGED = {
    "pillow": "MIT-CMU: historical PIL license, MIT-equivalent terms",
    "defusedxml": "PSF-2.0: permissive; the standard XML entity-attack guard for DOCX",
    "numpy": "BSD/MIT/Zlib/CC0 parts plus the GCC runtime library exception; via rapidocr",
    "tqdm": "MPL-2.0 AND MIT: file-level copyleft on tqdm's own files, used unmodified; via rapidocr",
    "opencv-python": "Apache-2.0, but the wheel bundles FFmpeg (LGPL-2.1) as a separate DLL; via rapidocr",
    "shapely": "BSD-3, but the wheel bundles GEOS (LGPL-2.1) as a separate library; via rapidocr",
}

ALLOWED = re.compile(r"\b(MIT|Apache|BSD|ISC)\b", re.I)
# Any mention fails, even inside "X OR Y": a mis-parsed SPDX expression must never let a
# copyleft component through, so genuinely dual-licensed packages go through FLAGGED.
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|MPL)|GNU (LESSER |LIBRARY |AFFERO )?GENERAL PUBLIC", re.I)


def _declared(dist: metadata.Distribution) -> str:
    """License-Expression (SPDX) first, then the free-text License field, then classifiers."""
    meta = dist.metadata
    classifiers = "; ".join(c for c in meta.get_all("Classifier") or [] if c.startswith("License"))
    return meta.get("License-Expression") or meta.get("License") or classifiers


def _bundled(dist: metadata.Distribution) -> str:
    """Text of every license-like file shipped in the wheel."""
    names = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$", re.I)
    return " ".join(f.locate().read_text(errors="ignore") for f in dist.files or [] if names.search(str(f)))


def test_installed_dependencies_are_permissively_licensed():
    failures = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name.lower() in FLAGGED or name.lower() == "redactit":
            continue
        declared = _declared(dist)
        if not ALLOWED.search(declared) or COPYLEFT.search(declared):
            failures.append(f"{name}: declares {declared!r}")
        elif COPYLEFT.search(_bundled(dist)):
            failures.append(f"{name}: bundles a copyleft license file")
    assert not failures, "Non-permissive dependencies:\n" + "\n".join(failures)
