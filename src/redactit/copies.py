"""Redactit's own redacted copies: which outputs it wrote, and deleting them once they expire.

Redacted copies are kept as long as the pseudonym vault's entries, then deleted: the
policy's vault.retention_days, 30 days by default (the owner's decision of 2026-10-02,
docs/PLAN.md §12), so a shorter admin or user setting shortens both. The folder watcher
records every output it publishes and purges at start-up and about once an hour. The rules:

- Only a recorded file is ever deleted. `--outbox` can name any folder, including one that
  holds the user's own files, and those were never recorded.
- A recorded copy is deleted only while it is still the file Redactit wrote: the same file
  ID, size and mtime. One the user edited, or replaced with a file of their own, is theirs
  now; it is dropped from the index and left alone.
- A link, or a reparse point naming another path, is never followed or deleted (the
  watcher's link rule), and nothing in the watcher's current inbox is touched.
- The index lives in the user's data folder, never in the outbox, where it would look like
  an output and travel with the folder. It is replaced atomically, never edited in place;
  if it cannot be read, nothing is deleted.
- Results and the audit event carry counts and exception types, never a file's name: a
  name such as "<person> passport.png" is sensitive on its own.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from redactit.hosts.watcher import _fingerprint, _is_link

if TYPE_CHECKING:
    from redactit.audit import AuditLog

_VERSION = 1
_ID_FIELDS = ("dev", "ino", "size", "mtime_ns")  # in the order of watcher._fingerprint
_FIELDS = {"path", "written", *_ID_FIELDS}
_PURGED, _DROPPED, _KEPT = "purged", "dropped", "kept"


@dataclass(frozen=True)
class Purge:
    """What one purge did. `problem` says why it stopped short, never naming a file."""

    purged: int = 0  # expired copies deleted
    dropped: int = 0  # entries let go: the file is gone, changed, a link, or in the inbox
    kept: int = 0  # not expired yet, or not checkable or deletable right now
    readable: bool = True
    problem: str | None = None


class Copies:
    """The index of redacted copies at `index`, and the purge that deletes expired ones.

    `retention_days` has no default here: it comes from the loaded policy, so the vault's
    setting is the one place it is set.
    """

    def __init__(self, index: Path, *, retention_days: int,
                 clock: Callable[[], float] = time.time, audit: AuditLog | None = None) -> None:
        self.index, self.retention_days, self.clock, self.audit = Path(index), retention_days, clock, audit

    def record(self, written: Iterable[tuple[Path, os.stat_result]]) -> None:
        """Add outputs just written, each with an lstat taken once it was in place.

        Raises OSError if the index cannot be read or replaced; the outputs are then not
        recorded, and stay until the user removes them.
        """
        now = self.clock()
        new = [{"path": os.path.abspath(path), "written": now, **dict(zip(_ID_FIELDS, _fingerprint(st), strict=True))}
               for path, st in written]
        if not new:
            return
        try:
            entries = self._load()
        except ValueError:
            # Corrupt, not merely unreadable for now: start again. The copies it listed are
            # never deleted, which is the safe way to lose them.
            entries = []
        # A new output under an existing name replaced that file, so its old entry goes.
        names = {os.path.normcase(e["path"]) for e in new}
        self._save([e for e in entries if os.path.normcase(e["path"]) not in names] + new)

    def purge(self, inbox: Path | None = None) -> Purge:
        """Delete recorded copies older than the retention period that are still as written.

        Entries that are no longer Redactit's copies are dropped and their files left alone.
        Nothing whose folder is `inbox` is touched. Writes one audit event of counts.
        """
        try:
            entries = self._load()
        except (OSError, ValueError) as e:
            result = Purge(readable=False,
                           problem=f"deleted nothing: the index of redacted copies cannot be read ({type(e).__name__})")
        else:
            cutoff = self.clock() - self.retention_days * 86400
            outcomes = [self._expire(entry, cutoff, inbox) for entry in entries]
            problem = None
            if any(o != _KEPT for o in outcomes):
                try:
                    self._save([e for e, o in zip(entries, outcomes, strict=True) if o == _KEPT])
                except OSError as e:  # the deleted and dropped entries are dropped next time: their files are gone or changed
                    problem = f"the index of redacted copies could not be updated ({type(e).__name__})"
            result = Purge(outcomes.count(_PURGED), outcomes.count(_DROPPED), outcomes.count(_KEPT), problem=problem)
        if self.audit is not None:
            self.audit.write("copies_purge", purged_count=result.purged, dropped_count=result.dropped,
                             kept_count=result.kept, retention_days=self.retention_days,
                             index_readable=result.readable)
        return result

    def _expire(self, entry: dict[str, Any], cutoff: float, inbox: Path | None) -> str:
        path = Path(entry["path"])
        if inbox is not None and _same_folder(path.parent, inbox):
            return _DROPPED  # an earlier outbox now watched as the inbox: its files are inputs, only ever read
        try:
            st = os.lstat(path)  # the entry itself, never what a link names
        except FileNotFoundError:
            return _DROPPED  # removed or moved by the user
        except OSError:
            return _KEPT  # cannot be checked now; checked again next time
        if _is_link(st) or not stat.S_ISREG(st.st_mode) or _fingerprint(st) != tuple(entry[k] for k in _ID_FIELDS):
            return _DROPPED  # no longer the file Redactit wrote
        if entry["written"] >= cutoff:
            return _KEPT
        try:
            # Removes the name checked above. Unlinking a link would remove the link, never
            # what it names, so even a swap after the check cannot delete through one.
            os.unlink(path)
        except FileNotFoundError:
            return _DROPPED
        except OSError:
            return _KEPT  # e.g. open in another program on Windows; tried again next time
        return _PURGED

    def _load(self) -> list[dict[str, Any]]:
        """The recorded copies, or [] if none were ever recorded. Raises ValueError for an
        index that is not exactly what _save writes, and OSError for one that cannot be read:
        every entry is checked, so a damaged index deletes nothing rather than a guess."""
        try:
            raw = self.index.read_bytes()
        except FileNotFoundError:
            return []
        data = json.loads(raw)
        if not (isinstance(data, dict) and set(data) == {"version", "copies"} and data["version"] == _VERSION
                and isinstance(data["copies"], list)):
            raise ValueError("not an index of redacted copies")
        for entry in data["copies"]:
            if not (isinstance(entry, dict) and set(entry) == _FIELDS and isinstance(entry["path"], str)
                    and _is_number(entry["written"]) and all(_is_int(entry[k]) for k in _ID_FIELDS)):
                raise ValueError("not an index of redacted copies")
        return data["copies"]

    def _save(self, entries: list[dict[str, Any]]) -> None:
        """Replace the index in one rename, so a crash leaves the old index or the new one."""
        folder = self.index.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)  # a new data folder is private, as the vault's
        fd, tmp = tempfile.mkstemp(prefix=".copies-", suffix=".tmp", dir=folder)  # 0600, beside the index
        try:
            with open(fd, "w", encoding="utf-8") as f:
                json.dump({"version": _VERSION, "copies": entries}, f)
                f.flush()
                os.fsync(f.fileno())  # the rename must not land before the data does
            os.replace(tmp, self.index)
        finally:
            Path(tmp).unlink(missing_ok=True)  # already gone once renamed; this catches every failure
        if os.name == "posix":  # and the rename itself must survive a crash
            dir_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)


def _same_folder(a: Path, b: Path) -> bool:
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:
        return os.path.samefile(a, b)  # other spellings of one folder, such as \\?\ on Windows
    except OSError:
        return False


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
