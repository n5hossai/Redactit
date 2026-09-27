"""Model files: fetched only by `redactit setup-models`, pinned by SHA-256, verified on every load."""

import hashlib
import json
import os
import urllib.request
from pathlib import Path

import platformdirs

from redactit.types import RedactitError

LOCK = json.loads(Path(__file__).with_name("models.lock.json").read_text(encoding="utf-8"))


class ModelError(RedactitError, RuntimeError):
    pass


def model_dir() -> Path:
    default = platformdirs.user_data_path("redactit", appauthor=False) / "models"
    return Path(os.environ.get("REDACTIT_MODEL_DIR") or default)


def path_for(name: str) -> Path:
    """Return the verified local path of a pinned file, or fail closed.

    Hashing on every load means a swapped or corrupted model is refused, not quietly used
    to miss entities (THREAT_MODEL T15).
    """
    path = model_dir() / name
    if not path.is_file():
        raise ModelError(f"{name} is not installed; run `redactit setup-models`")
    if _sha256(path) != LOCK[name]["sha256"]:
        raise ModelError(f"{name} does not match its pinned SHA-256; run `redactit setup-models` again")
    return path


def fetch_all() -> list[str]:
    """Download every missing or mismatched file. The only network code in Redactit."""
    fetched = []
    for name, pin in LOCK.items():
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


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
