# Third-party notices

Redactit is MIT-licensed (see `LICENSE`). Every other dependency is MIT, Apache-2.0 or
BSD-style, and each is listed with its exact version in `uv.lock`.

The components below are the exceptions: they are under the LGPL (copyleft) or the MPL-2.0
(file-level copyleft). Redactit uses each one unmodified, as a separate library that you
can replace. `tests/test_licenses.py` fails if any other copyleft component appears, and
`docs/PLAN.md` section 8 records why these were accepted.

## FFmpeg, inside opencv-python-headless 5.0.0.93

- **Version.** FFmpeg 7.1 in the Windows x64 wheel. The wheel's notice files do not name a
  version, so it was read from `cv2/opencv_videoio_ffmpeg500_64.dll`, which reports libavcodec
  61.19.100 and libavformat 61.7.100: the values in the FFmpeg `n7.1` tag (7.1.x point releases
  report 61.19.101). The DLL's build configuration has no `--enable-gpl` or `--enable-nonfree`.
- **License.** LGPL-2.1-or-later. The OpenCV wheel itself is Apache-2.0.
- **License text.** https://github.com/FFmpeg/FFmpeg/blob/n7.1/COPYING.LGPLv2.1, also
  https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt and the wheel's
  `opencv_python_headless-5.0.0.93.dist-info/LICENSE-3RD-PARTY.txt`.
- **Source.** https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz (tag https://github.com/FFmpeg/FFmpeg/tree/n7.1).
  The wheel's own source is the opencv-python-headless 5.0.0.93 sdist (URL and SHA-256 in `uv.lock`).
- **Use.** Unmodified. FFmpeg is linked into a separate plugin DLL that OpenCV loads at run
  time, so replacing `opencv_videoio_ffmpeg500_64.dll` replaces FFmpeg. Redactit never
  decodes video.
- **Other platforms.** Not inspected. The opencv-python 5.0.0.93 Linux build files, shared by
  the headless wheels, pin FFmpeg 8.1.1
  (https://github.com/opencv/opencv-python/blob/93/docker/manylinux_2_28/Dockerfile_x86_64);
  the macOS build files name no FFmpeg version. The wheel's notice file also lists these
  LGPL libraries as redistributed on macOS: libgmp, libidn2, libunistring (LGPL-3.0);
  libbluray, libgnutls, libnettle, libhogweed, libintl, libmp3lame, libp11, librtmp, libsoxr,
  libtasn1 (LGPL-2.1). Their versions and sources are not recorded here. Qt 5 ships only in
  the non-headless wheels, which Redactit does not install.

## GEOS, inside shapely 2.1.2

- **Version.** GEOS 3.13.1, from `shapely.geos_version` and the wheel's `DELVEWHEEL` record
  (`geos-3.13.1`). Shapely's 2.1.2 release workflow sets `GEOS_VERSION` to 3.13.1 for every
  platform (https://github.com/shapely/shapely/blob/2.1.2/.github/workflows/release.yml).
- **License.** LGPL-2.1. Shapely itself is BSD-3-Clause.
- **License text.** https://github.com/libgeos/geos/blob/3.13.1/COPYING, also
  https://www.gnu.org/licenses/old-licenses/lgpl-2.1.txt and the wheel's
  `shapely-2.1.2.dist-info/licenses/LICENSE_GEOS`.
- **Source.** https://download.osgeo.org/geos/geos-3.13.1.tar.bz2 (tag https://github.com/libgeos/geos/tree/3.13.1).
- **Use.** Unmodified. GEOS ships as separate shared libraries (`shapely.libs/geos-*.dll` and
  `geos_c-*.dll` on Windows) that Shapely loads; replacing them replaces GEOS.

## certifi 2026.7.22

- **License.** MPL-2.0, covering the whole package including the CA bundle `cacert.pem`.
- **License text.** https://www.mozilla.org/en-US/MPL/2.0/, also the wheel's
  `certifi-2026.7.22.dist-info/licenses/LICENSE`.
- **Source.** https://github.com/certifi/python-certifi/tree/2026.07.22
- **Use.** Unmodified, an ordinary Python package pulled in by requests and httpx. Installing
  another certifi replaces it. The offline engine never calls it.

## tqdm 4.70.1

- **License.** MPL-2.0 AND MIT. Most files are MPL-2.0; older contributions stay MIT, as
  itemised in the package's `LICENCE`.
- **License text.** https://github.com/tqdm/tqdm/blob/v4.70.1/LICENCE, also
  https://www.mozilla.org/en-US/MPL/2.0/.
- **Source.** https://github.com/tqdm/tqdm/tree/v4.70.1
- **Use.** Unmodified, an ordinary Python package. Installing another tqdm replaces it.

## Files vendored inside setuptools 84.0.0

setuptools itself is MIT, and spaCy requires it. Two groups of vendored files are copyleft.
The exact copies are in the setuptools source at https://github.com/pypa/setuptools/tree/v84.0.0.

- **autocommand 2.2.2, LGPL-3.0**, in `setuptools/_vendor/autocommand/`.
  License text: https://www.gnu.org/licenses/lgpl-3.0.txt, also
  `setuptools/_vendor/autocommand-2.2.2.dist-info/LICENSE`.
  Upstream source: https://github.com/Lucretiel/autocommand/tree/2.2.2
- **validate-pyproject files, MPL-2.0**: `setuptools/config/setuptools.schema.json`,
  `setuptools/config/distutils.schema.json` and, in `setuptools/config/_validate_pyproject/`,
  `extra_validations.py`, `formats.py` and `error_reporting.py`, as declared in the two
  `NOTICE` files beside them. License text: https://www.mozilla.org/en-US/MPL/2.0/.
  Upstream project: https://github.com/abravalheri/validate-pyproject (the shipped copies
  are the ones in the setuptools tag above; the NOTICE files name no upstream version).
- **Use.** Unmodified. Installing a different setuptools replaces both.

## Reviewed, no copyleft obligation

Some wheels mention the GPL only under an exception or in build scripts that are not part
of the binary: numpy lists the GCC runtime library under the GCC Runtime Library Exception,
and pypdfium2 bundles ICU's autoconf macros. Details are in `docs/PLAN.md` section 8.
