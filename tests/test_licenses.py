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
    # Runtime from Phase 3. Its GPL mentions are ICU's autoconf macros (GPL with the
    # Autoconf exception, build scripts only) and the LLVM exception clause naming GPLv2.
    "pypdfium2": "BSD-3/Apache-2.0; bundled notices mention GPL only in build-script and exception text",
    # Runtime, via spaCy/Presidio/tokenizers; accepted by the owner. Used unmodified, and
    # MPL/LGPL obligations attach only to changes in their own files.
    "certifi": "MPL-2.0: CA bundle pulled in by requests/httpx; the offline engine never uses it",
    "setuptools": "MIT, vendoring autocommand (LGPL-3) and validate-pyproject files (MPL-2.0); spaCy needs it",
    "typing-extensions": "PSF-2.0: permissive; required by most typed libraries",
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
    """Text of every license file shipped in the wheel.

    Matching by name alone misses vendored notices such as pypdfium2's BUILD_LICENSES/*.txt,
    so everything under the PEP 639 `.dist-info/licenses/` folder is read as well.
    """
    names = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)[^/]*$|\.dist-info/licenses/", re.I)
    files = [f for f in dist.files or [] if names.search(f.as_posix())]
    return " ".join(f.locate().read_text(errors="ignore") for f in files)


def test_installed_dependencies_are_permissively_licensed():
    failures = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        key = re.sub(r"[-_.]+", "-", name).lower()  # PEP 503: typing_extensions == typing-extensions
        if key in FLAGGED or key == "redactit":
            continue
        declared = _declared(dist)
        if not ALLOWED.search(declared) or COPYLEFT.search(declared):
            failures.append(f"{name}: declares {declared!r}")
        elif COPYLEFT.search(_bundled(dist)):
            failures.append(f"{name}: bundles a copyleft license file")
    assert not failures, "Non-permissive dependencies:\n" + "\n".join(failures)
