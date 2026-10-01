"""Folder watcher: files dropped into an inbox come out redacted in an outbox.

`redactit watch` loads one engine for its whole run, so the models' cold start is paid
once. Each file goes through the same code as `redactit redact` (cli.redact_file) and
gets the same output names. The rules:

- The inbox is only ever read. Originals are never written, renamed or deleted, and the
  outbox may not be the inbox, where an output such as notes.txt would replace its input.
- A file is read only once its size and modification time have held still for
  SETTLE_SECONDS, so a file still being copied or downloaded is not redacted half-written.
- Each output is written to a private folder inside the outbox (0700, or an owner-only
  ACL on Windows) and renamed into place, so the outbox never shows a partial file. The
  folder is removed when the watcher stops, by error or Ctrl+C too (THREAT_MODEL T11).
- Log lines give the file type and a reason, never a file's name or content: a name such
  as "<person> passport.png" is sensitive on its own, and logs outlive the run.
- Only the inbox's top level is watched; hidden files and Word's "~$" lock files are not
  documents and are passed over.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import platformdirs
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from redactit.cli import SUFFIXES, output_name, output_suffixes
from redactit.hosts.native import MAX_PAYLOAD
from redactit.types import RedactitError

SETTLE_SECONDS = 2.0  # how long a file's size and mtime must hold before it is read
POLL_SECONDS = 0.25
MAX_FILE = MAX_PAYLOAD  # one file, as large as the browser extension may send one
TEMP_PREFIX = ".redactit-tmp-"

# How long redacted copies stay in the outbox is not decided yet (docs/PLAN.md §12). Until
# the owner decides, the watcher deletes nothing: None keeps every output until the user
# removes it. A purge, once chosen, runs from Watcher.run's loop, reads this value, and
# must never touch the inbox.
OUTBOX_RETENTION_DAYS: int | None = None

# (input file name, its bytes) -> each output's name and its text or bytes (cli.redact_file)
Convert = Callable[[str, bytes], list[tuple[str, str | bytes]]]


def default_folders() -> tuple[Path, Path]:
    """inbox/ and outbox/ in Redactit's data folder. Unlike the vault's location, these
    never follow REDACTIT_DATA_DIR: whoever sets a session's or a launcher's environment
    could otherwise send outputs, or read inputs, somewhere else. Flags are what the user
    typed or the installer wrote."""
    data = platformdirs.user_data_path("redactit", appauthor=False)
    return data / "inbox", data / "outbox"


def _log(message: str) -> None:
    print(f"redactit watch: {message}", file=sys.stderr, flush=True)


def _describe(name: str) -> str:
    """"a .pdf file", or a neutral phrase: an unknown suffix could itself be a value."""
    suffix = Path(name).suffix.lower()
    return f"a {suffix} file" if suffix in SUFFIXES else "a file of an unsupported type"


def _fingerprint(st: os.stat_result) -> tuple[int, int, int, int]:
    """Which version of a file this is: a replaced file has a new file ID, an edited one a new size or mtime."""
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _arrived_ns(st: os.stat_result) -> int:
    """When this version of the file appeared in the inbox, as near as the OS records it.

    Copying keeps the modification time (Explorer, cp -p), so an old file copied in over
    an earlier input of the same name would look older than that input's output. The
    creation time on Windows, and the inode change time elsewhere, move when it arrives.
    """
    other = st.st_birthtime_ns if sys.platform == "win32" else st.st_ctime_ns
    return max(st.st_mtime_ns, other)


def _private_dir(parent: Path) -> Path:
    """A new folder that only this user can open, on the outbox's volume so a rename
    into the outbox is atomic. mkdtemp makes it 0700; on Windows, Python 3.12.4 and later
    turn that mode into an owner-only ACL (CVE-2024-4030), where older versions left the
    parent's ACL in place."""
    if sys.platform == "win32" and sys.version_info < (3, 12, 4):
        raise RedactitError("the folder watcher needs Python 3.12.4 or later on Windows (private temporary files)")
    return Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=parent))


class Watcher:
    """Watches `inbox` and writes what `convert` returns for each settled file to `outbox`."""

    def __init__(self, inbox: Path, outbox: Path, *, settle: float = SETTLE_SECONDS,
                 poll: float = POLL_SECONDS, log: Callable[[str], None] = _log) -> None:
        self.inbox, self.outbox = Path(inbox).resolve(), Path(outbox).resolve()
        if self.inbox == self.outbox:
            raise RedactitError("the outbox must be a different folder from the inbox, or outputs would overwrite originals")
        self.settle, self.poll, self.log = settle, poll, log
        self.temp: Path | None = None  # the private folder outputs are written in; exists only while running
        self._lock = threading.Lock()  # guards _pending, which the observer's thread adds to
        self._pending: dict[Path, tuple[tuple[int, int], float] | None] = {}  # path -> ((size, mtime), stable since)
        # The version of each file already handled (redacted, skipped or failed), so the
        # events our own read or a virus scanner causes do not redo it, while a file that is
        # replaced or edited (a new fingerprint) is redone.
        self._done: dict[Path, tuple[int, int, int, int]] = {}
        self._unreadable: set[Path] = set()  # files whose "cannot be read yet" was logged once

    # --- lifecycle -------------------------------------------------------------------------

    def run(self, convert: Convert, stop: threading.Event | None = None) -> None:
        """Watch until `stop` is set. Ctrl+C propagates once the private folder is gone."""
        stop = stop or threading.Event()
        for folder in (self.inbox, self.outbox):  # a new one is private: the inbox holds originals
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.temp = _private_dir(self.outbox)
        observer = Observer()
        try:
            observer.schedule(_Events(self), str(self.inbox), recursive=False)
            observer.start()
            self._scan()  # after the observer starts, so a file that lands during the scan is not missed
            self.log(f"watching {self.inbox}, writing to {self.outbox} (Ctrl+C stops)")
            while not stop.is_set():
                self.step(convert)
                time.sleep(self.poll)  # unlike Event.wait, Ctrl+C interrupts it on Windows
        finally:
            observer.stop()
            if observer.is_alive():
                observer.join()
            self._remove_temp()

    def _remove_temp(self) -> None:
        # Every write deletes its own temp file in `finally`, so this is normally an empty
        # folder; rmtree also clears anything an interrupted write left (T11).
        try:
            shutil.rmtree(self.temp)
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log(f"could not remove the temporary folder {self.temp} ({type(e).__name__})")
        self.temp = None

    # --- events --------------------------------------------------------------------------

    def saw(self, path: Path) -> None:
        """A file was created, changed or moved in: check it once it settles."""
        if path.parent == self.inbox:
            with self._lock:
                self._pending.setdefault(path, None)

    def gone(self, path: Path) -> None:
        # Its _done entry may stay: a new file under the same name has a new fingerprint.
        with self._lock:
            self._pending.pop(path, None)

    def _scan(self) -> None:
        """Queue the files already in the inbox, except those whose outputs are all newer.

        The "newer" rule is what stops a restart from redoing the whole inbox: a file is
        taken as done when every output it should have exists and was written after the
        file last changed or arrived (_arrived_ns). An input edited or copied in since then
        is newer than an output, so it is redone. On Windows, a file can arrive with times
        older than its outputs (moved in from the same drive, or copied over an input of the
        same name); it is redone if it arrives while the watcher runs, where its new
        fingerprint decides, but not if it arrives while the watcher is stopped.
        """
        for entry in os.scandir(self.inbox):
            path = Path(entry.path)
            try:
                st = path.stat()  # os.stat, not entry.stat(): only it has the file ID on Windows
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and self._outputs_newer(path.name, st):
                self._done[path] = _fingerprint(st)
            else:
                self.saw(path)

    def _outputs_newer(self, name: str, st: os.stat_result) -> bool:
        if Path(name).suffix.lower() not in SUFFIXES:
            return False
        try:
            written = [(self.outbox / output_name(name, s)).stat().st_mtime_ns for s in output_suffixes(name)]
        except OSError:
            return False
        return min(written) >= _arrived_ns(st)

    # --- work ----------------------------------------------------------------------------

    def step(self, convert: Convert) -> None:
        """Handle every pending file whose size and mtime have held for `settle` seconds."""
        now = time.monotonic()
        with self._lock:
            pending = list(self._pending)
        for path in pending:
            try:
                st = path.stat()
            except FileNotFoundError:
                self.gone(path)
                continue
            except OSError:
                continue  # not statable right now; the next poll tries again
            if not stat.S_ISREG(st.st_mode):
                with self._lock:
                    self._pending.pop(path, None)
                continue
            signature = (st.st_size, st.st_mtime_ns)
            with self._lock:
                if path not in self._pending:  # deleted meanwhile
                    continue
                seen = self._pending[path]
                if seen is None or seen[0] != signature:  # new, or still being written: restart its clock
                    self._pending[path] = (signature, now)
                    continue
                if now - seen[1] < self.settle:
                    continue
                del self._pending[path]
            self._handle(path, st, convert)

    def _handle(self, path: Path, st: os.stat_result, convert: Convert) -> None:
        fingerprint = _fingerprint(st)
        if self._done.get(path) == fingerprint:
            return  # this version was already handled; the event came from a read or a scanner
        name, what = path.name, _describe(path.name)
        if name.startswith((".", "~$")):
            self._done[path] = fingerprint
            return
        if Path(name).suffix.lower() not in SUFFIXES:
            self.log(f"skipped {what}")
            self._done[path] = fingerprint
            return
        if st.st_size > MAX_FILE:
            self.log(f"skipped {what}: larger than {MAX_FILE // 2**20} MiB")
            self._done[path] = fingerprint
            return
        try:
            with path.open("rb") as f:  # read-only: the original is never written
                data = f.read()
            changed = _fingerprint(os.stat(path)) != fingerprint
        except OSError as e:  # e.g. the writing program still holds it exclusively on Windows
            if path not in self._unreadable:
                self.log(f"waiting: {what} cannot be read yet ({type(e).__name__})")
                self._unreadable.add(path)
            self.saw(path)
            return
        self._unreadable.discard(path)
        if changed:  # written to while it was read: wait for it to settle again
            self.saw(path)
            return
        try:
            outputs = convert(name, data)
            self._publish(outputs)
        except RedactitError as e:  # our own errors never quote the input
            self.log(f"skipped {what}: {e}")
        except UnicodeDecodeError:
            self.log(f"skipped {what}: the text is not valid UTF-8")
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the watcher
            self.log(f"failed on {what}: {type(e).__name__} (details withheld: may contain input text)")
        else:
            self.log(f"redacted {what} ({len(outputs)} output{'s' if len(outputs) > 1 else ''})")
        self._done[path] = fingerprint  # redone only once the file itself changes

    def _publish(self, outputs: list[tuple[str, str | bytes]]) -> None:
        """Write every output to the private folder, then rename each into the outbox.

        Nothing reaches the outbox until every output of the file is fully written, and a
        rename within one volume is atomic, so a reader never sees a partial output.
        """
        staged = []
        try:
            for name, content in outputs:
                tmp = self.temp / f"{uuid.uuid4().hex}.part"
                staged.append((tmp, self.outbox / name))
                # Text in text mode, as `redactit redact` writes it, so both give the same bytes.
                mode = {"mode": "x", "encoding": "utf-8"} if isinstance(content, str) else {"mode": "xb"}
                with open(tmp, **mode) as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())  # the rename must not land before the data does
            for tmp, dst in staged:
                os.replace(tmp, dst)
        finally:
            for tmp, _ in staged:
                tmp.unlink(missing_ok=True)  # already gone once renamed; this catches every failure


class _Events(FileSystemEventHandler):
    """Runs on watchdog's thread: it only queues paths; the main thread does the work."""

    def __init__(self, watcher: Watcher) -> None:
        self._watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        kind = event.event_type
        if kind in ("created", "modified", "closed"):
            self._watcher.saw(Path(os.fsdecode(event.src_path)))
        elif kind == "moved":  # a download renamed from x.pdf.part to x.pdf lands here
            self._watcher.gone(Path(os.fsdecode(event.src_path)))
            self._watcher.saw(Path(os.fsdecode(event.dest_path)))
        elif kind == "deleted":
            self._watcher.gone(Path(os.fsdecode(event.src_path)))
        # "opened" and "closed_no_write" (inotify) are reads, including our own: ignored.


def stop_on_signals() -> None:
    """Turn a service stop (SIGTERM) or Ctrl+Break (SIGBREAK, Windows) into Ctrl+C, so
    every way of stopping the watcher runs the cleanup that removes its temp folder."""

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), interrupt)
