"""Model files, pinned by SHA-256 and verified on every load.

Two kinds of pin live in models.lock.json: files `redactit setup-models` downloads (a `url`
at a pinned revision) and files that ship inside an installed package (a `package` and a
`path` inside it, such as RapidOCR's three ONNX files). Both are hashed in full on every
load; there is no hash cache, so bit rot or a swap that keeps the timestamp is still caught.
"""

import hashlib
import importlib.util
import json
import os
import sys
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import platformdirs

from redactit.types import RedactitError

LOCK = json.loads(Path(__file__).with_name("models.lock.json").read_text(encoding="utf-8"))
TEXT_MODELS = ("gliner/model.onnx", "gliner/tokenizer.json")
OCR_MODELS = ("rapidocr/det.onnx", "rapidocr/cls.onnx", "rapidocr/rec.onnx")
# Windows can hold a file open so nobody else may write, rename or delete it until it is
# loaded. Elsewhere there is no such lock, so the file is hashed again after loading.
HOLDS = sys.platform == "win32"
_HASH_CHUNK = 64 << 20


class ModelError(RedactitError, RuntimeError):
    pass


def model_dir() -> Path:
    default = platformdirs.user_data_path("redactit", appauthor=False) / "models"
    return Path(os.environ.get("REDACTIT_MODEL_DIR") or default)


def local_path(name: str) -> Path:
    """Where a pinned file lives: the model folder, or inside the package that ships it."""
    pin = LOCK[name]
    if "package" in pin:
        return _package_root(pin["package"]) / pin["path"]
    return model_dir() / name


def verify(names) -> "Verification":
    """Start hashing `names` in a background thread and return at once.

    Use the result as a context manager around the code that loads the files: entering
    waits for the hashes and raises ModelError on any mismatch, and the files cannot change
    until the block ends (THREAT_MODEL T15). Start it before slow imports to hide the hash.
    """
    return Verification(names)


def path_for(name: str) -> Path:
    """Check one pinned file now and return its path, or fail closed.

    The file could still change between this check and its use, so loaders use `verify`;
    this is for checking that a model is installed.
    """
    path = local_path(name)
    with _open_held(path, name) as f:
        _check(f, name)
    return path


class Verification:
    """Pinned files hashed in a thread and held unchanged until the `with` block ends."""

    def __init__(self, names) -> None:
        self._names = tuple(names)
        self._files: list = []
        self._paths: dict[str, Path] = {}
        self._error: ModelError | None = None
        self._thread = threading.Thread(target=self._hash_all, name="redactit-verify", daemon=True)
        self._thread.start()

    def _hash_all(self) -> None:
        name = ""
        try:
            for name in self._names:
                path = local_path(name)
                f = _open_held(path, name)
                self._files.append(f)
                _check(f, name)
                self._paths[name] = path
        except ModelError as e:
            self._error = e
        except Exception as e:  # noqa: BLE001 - surfaced to the loader as a refusal, never swallowed
            self._error = ModelError(f"{name} could not be verified ({type(e).__name__})")

    def __enter__(self) -> dict[str, Path]:
        self._thread.join()
        if self._error:
            self._close()
            raise self._error
        return dict(self._paths)

    def __exit__(self, exc_type, *_exc) -> None:
        try:
            if exc_type is None and not HOLDS:
                # No lock outside Windows: hash again now that the loader holds its own copy,
                # and refuse it if the file changed while it was being loaded.
                for name, path in self._paths.items():
                    if _sha256(path) != LOCK[name]["sha256"]:
                        raise ModelError(f"{name} changed while it was being loaded; {_remedy(name)}")
        finally:
            self._close()

    def close(self) -> None:
        """Release the files without loading them, for a caller that fails before its
        `with` block. Waits for the hashing thread, which may still be opening files."""
        self._thread.join()
        self._close()

    def _close(self) -> None:
        for f in self._files:
            f.close()
        self._files.clear()


@contextmanager
def released_on_error(verification: Verification | None):
    """Close `verification` if the block raises: a bad policy, a keychain error or a
    failed import before the loader's `with`. Otherwise Windows keeps the model files
    locked against writes and deletes, `redactit setup-models` included, for as long as
    the process lives. Closing twice is harmless."""
    try:
        yield
    except BaseException:
        if verification is not None:
            verification.close()
        raise


def fetch_all() -> list[str]:
    """Download every missing or mismatched file. The only network code in Redactit.

    Files that ship inside a package are never downloaded; `check_bundled` verifies them.
    """
    fetched = []
    for name, pin in LOCK.items():
        if "url" not in pin:
            continue
        path = model_dir() / name
        if path.is_file() and _sha256(path) == pin["sha256"]:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(pin["url"]) as resp, part.open("wb") as out:  # noqa: S310 (pinned https URL)
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        if _sha256(part) != pin["sha256"]:
            part.unlink()
            raise ModelError(f"{name}: downloaded file does not match its pinned SHA-256")
        part.replace(path)
        fetched.append(name)
    return fetched


def check_bundled() -> list[str]:
    """Verify the pinned files that ship inside installed packages; returns their names."""
    names = [name for name, pin in LOCK.items() if "package" in pin]
    for name in names:
        path_for(name)
    return names


def _check(f, name: str) -> None:
    if _digest(f) != LOCK[name]["sha256"]:
        raise ModelError(f"{name} does not match its pinned SHA-256; {_remedy(name)}")


def _remedy(name: str) -> str:
    if "package" in LOCK[name]:
        return f"reinstall Redactit to restore {LOCK[name]['package']}"
    return "run `redactit setup-models` again"


def _package_root(package: str) -> Path:
    # find_spec locates the package without importing it (RapidOCR's import pulls in OpenCV).
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise ModelError(f"{package} is not installed; reinstall Redactit")
    return Path(next(iter(spec.submodule_search_locations)))


def _open_held(path: Path, name: str):
    """Open for reading; on Windows also deny every other writer, renamer and deleter.

    The deny lasts while the handle is open, so the bytes a loader reads by path are the
    bytes hashed here. Opening fails if another process already has the file open for
    writing, which is refused like a mismatch.
    """
    if not HOLDS:
        try:
            return path.open("rb")
        except FileNotFoundError:
            raise ModelError(f"{name} is not installed; {_remedy(name)}") from None
        except OSError as e:  # a folder in its place, no permission
            raise ModelError(f"{name} could not be opened ({type(e).__name__})") from None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    generic_read, file_share_read, open_existing, sequential_scan = 0x80000000, 0x1, 3, 0x08000000
    handle = kernel32.CreateFileW(str(path), generic_read, file_share_read, None, open_existing, sequential_scan, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        error = ctypes.get_last_error()
        if error in (2, 3):  # ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND
            raise ModelError(f"{name} is not installed; {_remedy(name)}")
        if error == 32:  # ERROR_SHARING_VIOLATION: someone holds it open for writing
            raise ModelError(f"{name} is open for writing by another program; refusing to load it")
        raise ModelError(f"{name} could not be opened (Windows error {error})")
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY), "rb", buffering=0)


def _digest(f) -> str:
    # Large reads keep the thread in hashlib and the OS, which both release the GIL, so the
    # hash really runs beside the imports.
    h = hashlib.sha256()
    while chunk := f.read(_HASH_CHUNK):
        h.update(chunk)
    return h.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as f:
        return _digest(f)
