"""Folder watcher behaviour that needs no models: settling, not redoing work, the private
temp folder (THREAT_MODEL T11) and what reaches the log."""

import os
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from redactit.cli import output_name, output_suffixes, redact_file
from redactit.detect.registry import PatternTimeout
from redactit.hosts import watcher
from redactit.hosts.watcher import TEMP_PREFIX, Watcher
from redactit.types import RedactitError

SETTLE = 0.4
VALUES = ("Priya", "Okafor", "4111")  # synthetic; none may reach the log


def wait_for(condition, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


class Recorder:
    """Stands in for cli.redact_file: names outputs the same way and notes every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, name, data):
        self.calls.append((name, data))
        return [(output_name(name, s), f"redacted {len(data)} bytes\n") for s in output_suffixes(name)]


@contextmanager
def running(inbox, outbox, convert, settle=SETTLE):
    """A watcher on a background thread, stopped (and its temp folder removed) on exit."""
    log, errors, stop = [], [], threading.Event()
    w = Watcher(inbox, outbox, settle=settle, poll=0.02, log=log.append)

    def target():
        try:
            w.run(convert, stop)
        except BaseException as e:  # noqa: BLE001 - reported by the assert below
            errors.append(e)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    wait_for(lambda: any(m.startswith("watching") for m in log) or errors)
    try:
        yield w, log
    finally:
        stop.set()
        thread.join(15)
    assert not errors and not thread.is_alive()


def temp_folders(outbox):
    return [p for p in outbox.iterdir() if p.name.startswith(TEMP_PREFIX)]


@pytest.fixture
def boxes(tmp_path):
    return tmp_path / "inbox", tmp_path / "outbox"


def test_the_outbox_cannot_be_the_inbox(tmp_path):
    with pytest.raises(RedactitError, match="different folder"):
        Watcher(tmp_path / "in", tmp_path / "x" / ".." / "in")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows spellings of one folder")
@pytest.mark.parametrize("exists", [False, True], ids=["created by run", "already there"])
@pytest.mark.parametrize("spell", [lambda p: "\\\\?\\" + str(p), lambda p: str(p).swapcase()],
                         ids=["extended-length prefix", "case only"])
def test_the_outbox_cannot_be_the_inbox_under_another_spelling(tmp_path, spell, exists):
    box = tmp_path / "box"
    if exists:
        box.mkdir()
        (box / "notes.txt").write_bytes(b"original")
    stop = threading.Event()
    stop.set()  # one pass only, if it starts at all
    with pytest.raises(RedactitError, match="different folder"):
        Watcher(spell(box), box, settle=0, poll=0.01, log=lambda _m: None).run(Recorder(), stop)
    left = [p.name for p in box.iterdir()] if box.exists() else []  # refused before or after creating it
    assert left == (["notes.txt"] if exists else [])  # no temp folder, no output
    if exists:
        assert (box / "notes.txt").read_bytes() == b"original"


@pytest.mark.parametrize("layout", ["outbox inside the inbox", "inbox inside the outbox"])
def test_one_folder_inside_the_other_leaves_originals_alone(tmp_path, layout):
    """Only the inbox's top level is watched and outputs are bare names, so nesting is safe."""
    if layout == "outbox inside the inbox":
        inbox, outbox = tmp_path / "in", tmp_path / "in" / "out"
    else:
        inbox, outbox = tmp_path / "out" / "in", tmp_path / "out"
    rec = Recorder()
    with running(inbox, outbox, rec):
        (inbox / "notes.txt").write_bytes(b"original")
        wait_for(lambda: (outbox / "notes.txt").exists())
        time.sleep(SETTLE * 3)  # an output read back as an input would show up as a second call
    assert rec.calls == [("notes.txt", b"original")]
    assert (inbox / "notes.txt").read_bytes() == b"original"


def test_a_half_written_file_is_not_read_early(boxes):
    inbox, outbox = boxes
    rec = Recorder()
    with running(inbox, outbox, rec, settle=0.6):
        with open(inbox / "notes.txt", "wb") as f:  # a slow copy: a piece every 0.25 s for 1.5 s
            for _ in range(6):
                f.write(b"x" * 1000)
                f.flush()
                time.sleep(0.25)
                assert rec.calls == []
        wait_for(lambda: (outbox / "notes.txt").exists())
    assert rec.calls == [("notes.txt", b"x" * 6000)]


def test_a_file_is_not_redone_until_it_changes(boxes):
    inbox, outbox = boxes
    inbox.mkdir()
    src = inbox / "notes.txt"
    src.write_bytes(b"one")
    rec = Recorder()
    with running(inbox, outbox, rec) as (w, _):
        wait_for(lambda: len(rec.calls) == 1)
        src.read_bytes()  # someone reads it: an event, but the same version
        w.saw(src)
        time.sleep(SETTLE * 3)
        assert len(rec.calls) == 1
        with open(src, "ab") as f:  # edited while watched: redone
            f.write(b" two")
        wait_for(lambda: len(rec.calls) == 2)
    with running(inbox, outbox, rec):  # a restart finds every output newer than its input
        time.sleep(SETTLE * 3)
    assert len(rec.calls) == 2
    time.sleep(0.05)
    with open(src, "ab") as f:  # edited while stopped: redone at the next start
        f.write(b" three")
    with running(inbox, outbox, rec):
        wait_for(lambda: len(rec.calls) == 3)
    assert [data for _, data in rec.calls] == [b"one", b"one two", b"one two three"]


def assert_private(folder):
    if sys.platform == "win32":
        # mkdtemp's owner-only ACL is set explicitly and protected: no entry is inherited
        # from the outbox, which icacls would mark "(I)".
        out = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, check=True).stdout
        acl = out.replace(str(folder), "")  # the path itself contains "Users"
        assert "(I)" not in acl and "Everyone" not in acl and "Users" not in acl
        assert "OWNER RIGHTS:(OI)(CI)(F)" in acl
    else:
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_outputs_are_written_privately_then_renamed_and_the_temp_folder_goes(boxes):
    inbox, outbox = boxes
    seen_in_temp = []

    def convert(name, data):
        seen_in_temp.append(sorted(p.name for p in w.temp.iterdir()))
        return [("scan.pdf", b"%PDF-redacted"), ("scan.pdf.md", "## Page 1\n")]

    with running(inbox, outbox, convert) as (w, log):
        temp = w.temp
        assert temp.parent == outbox.resolve() and temp_folders(outbox) == [temp]
        assert_private(temp)
        (inbox / "scan.pdf").write_bytes(b"%PDF-original")
        wait_for(lambda: (outbox / "scan.pdf.md").exists())
        assert list(temp.iterdir()) == []  # empty after every file, not only at the end
    assert not temp.exists() and temp_folders(outbox) == []
    assert sorted(p.name for p in outbox.iterdir()) == ["scan.pdf", "scan.pdf.md"]
    assert (outbox / "scan.pdf").read_bytes() == b"%PDF-redacted"
    assert (inbox / "scan.pdf").read_bytes() == b"%PDF-original"
    assert seen_in_temp == [[]] and "redacted a .pdf file (2 outputs)" in log


def test_a_failed_write_leaves_no_temp_file(boxes, monkeypatch):
    inbox, outbox = boxes
    real_replace, calls = os.replace, []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError(28, "No space left on device", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(watcher.os, "replace", flaky_replace)
    with running(inbox, outbox, Recorder()) as (w, log):
        (inbox / "scan.pdf").write_bytes(b"%PDF-original")
        wait_for(lambda: any(m.startswith("failed") for m in log))
        assert list(w.temp.iterdir()) == []
    assert temp_folders(outbox) == []
    assert "failed on a .pdf file: OSError (details withheld: may contain input text)" in log


def test_ctrl_c_in_the_middle_of_a_write_leaves_nothing_behind(boxes, monkeypatch):
    inbox, outbox = boxes
    inbox.mkdir()
    (inbox / "notes.txt").write_bytes(b"original")
    w = Watcher(inbox, outbox, settle=0.05, poll=0.02, log=lambda _m: None)
    partial = []

    def interrupted(_fd):  # Ctrl+C lands after the bytes are written, before the rename
        partial.extend(w.temp.iterdir())
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        w.run(Recorder())
    assert len(partial) == 1 and not partial[0].exists()
    assert list(outbox.iterdir()) == []  # no temp folder, no partial output
    assert (inbox / "notes.txt").read_bytes() == b"original"


LINK_SKIPPED = "skipped a .txt file: it is a link, which is not followed"


def test_a_symlink_out_of_the_inbox_is_not_followed(boxes):
    inbox, outbox = boxes
    inbox.mkdir()
    outside = inbox.parent / "elsewhere.txt"
    outside.write_bytes(b"a file outside the inbox")
    try:
        os.symlink(outside, inbox / "link.txt")
    except OSError:  # Windows needs Developer Mode or an elevated process for this
        pytest.skip("symlinks cannot be created here")
    rec = Recorder()
    with running(inbox, outbox, rec) as (_, log):
        wait_for(lambda: LINK_SKIPPED in log)
    assert rec.calls == [] and list(outbox.iterdir()) == []


SYMLINK, MOUNT_POINT, CLOUD_6 = 0xA000000C, 0xA0000003, 0x9000601A  # IO_REPARSE_TAG_* values


def test_link_decision():
    """The decision behind the test above, with stat results stood in, so it runs where no
    symlink can be made. A reparse point is a link only when its tag names another path."""
    regular, reparse = stat.S_IFREG | 0o600, 0x400 | 0x20

    def st(**fields):
        return SimpleNamespace(st_mode=regular, **fields)

    assert not watcher._is_link(st())
    assert not watcher._is_link(st(st_file_attributes=0x20, st_reparse_tag=0))  # archive bit only
    assert watcher._is_link(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))
    assert watcher._is_link(st(st_file_attributes=reparse, st_reparse_tag=SYMLINK))
    assert watcher._is_link(st(st_file_attributes=reparse, st_reparse_tag=MOUNT_POINT))  # a junction
    assert not watcher._is_link(st(st_file_attributes=reparse, st_reparse_tag=CLOUD_6))  # a OneDrive placeholder
    assert not watcher._is_link(st(st_file_attributes=0x20, st_reparse_tag=SYMLINK))  # a tag without the attribute


def _reparse_point(monkeypatch, name: str, tag: int) -> list:
    """Make the inbox entry `name` report itself as a reparse point with `tag`; returns
    the paths the watcher opens."""
    real_lstat, real_open, opened = os.lstat, os.open, []

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.path.basename(path) != name:
            return st
        fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_file_attributes": 0x400, "st_reparse_tag": tag})

    monkeypatch.setattr(watcher.os, "lstat", lstat)
    monkeypatch.setattr(watcher.os, "open", lambda path, *a, **k: opened.append(path) or real_open(path, *a, **k))
    return opened


@pytest.mark.parametrize("tag", [SYMLINK, MOUNT_POINT], ids=["symlink", "mount point"])
def test_a_reparse_point_naming_another_path_is_skipped_unread(boxes, monkeypatch, tag):
    inbox, outbox = boxes
    inbox.mkdir()
    (inbox / "link.txt").write_bytes(b"what a link would lead to")
    opened = _reparse_point(monkeypatch, "link.txt", tag)
    rec = Recorder()
    with running(inbox, outbox, rec) as (_, log):
        wait_for(lambda: LINK_SKIPPED in log)
    assert rec.calls == [] and not [p for p in opened if os.path.basename(p) == "link.txt"]


def test_a_cloud_placeholder_is_read_like_any_file(boxes, monkeypatch):
    inbox, outbox = boxes
    inbox.mkdir()
    (inbox / "notes.txt").write_bytes(b"synced from the cloud")
    _reparse_point(monkeypatch, "notes.txt", CLOUD_6)
    rec = Recorder()
    with running(inbox, outbox, rec) as (_, log):
        wait_for(lambda: (outbox / "notes.txt").exists())
    assert rec.calls == [("notes.txt", b"synced from the cloud")] and LINK_SKIPPED not in log


def test_a_file_swapped_after_it_settled_is_not_read(boxes, monkeypatch):
    """The opened file's ID must match the version that settled; a link or another file
    put in its place meanwhile has another one. Opened without following a final symlink
    where the OS can refuse one."""
    inbox, outbox = boxes
    inbox.mkdir()
    (inbox / "notes.txt").write_bytes(b"original")
    real_open, real_fstat, flags = os.open, os.fstat, []

    def fstat(fd):
        st = real_fstat(fd)
        fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_ino": st.st_ino + 1})  # always another file

    def opener(path, mode, *args, **kwargs):
        flags.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(watcher.os, "fstat", fstat)
    monkeypatch.setattr(watcher.os, "open", opener)
    rec = Recorder()
    with running(inbox, outbox, rec):
        wait_for(lambda: len(flags) >= 2)  # opened, refused, settled again and opened again
    assert rec.calls == [] and list(outbox.iterdir()) == []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    assert all(f & (os.O_WRONLY | os.O_RDWR) == 0 and f & nofollow == nofollow for f in flags)


class FakeEngine:
    def redact(self, text, scope, **_kwargs):
        return SimpleNamespace(text=text.replace("Priya Okafor", "[PERSON_1]"))


def test_bad_files_are_logged_without_their_names_or_content(boxes):
    """Unsupported, corrupt and undecodable files get a reason and the watcher carries on.
    Neither a value nor a file name reaches the log: names can be sensitive too."""
    inbox, outbox = boxes
    inbox.mkdir()
    files = {
        "Priya Okafor 4111.xyz": b"Priya Okafor 4111 1111 1111 1111",
        "report.4111111111111111": b"Priya Okafor",
        "broken.pdf": b"not a PDF: Priya Okafor",
        "broken.docx": b"not a zip: Priya Okafor",
        "latin.txt": "Priya Okafor, caf\xe9".encode("latin-1"),
        ".hidden.txt": b"Priya Okafor",
        "~$notes.docx": b"Word's lock file",
        "good.txt": b"Call Priya Okafor.",
    }
    for name, data in files.items():
        (inbox / name).write_bytes(data)
    convert = lambda name, data: redact_file(name, data, FakeEngine(), "scope", destination="outbox")  # noqa: E731
    with running(inbox, outbox, convert) as (_, log):
        wait_for(lambda: len(log) >= 7)
        time.sleep(SETTLE * 2)  # the hidden and lock files must stay silent
    events = sorted(log[1:])
    assert events == sorted([
        "skipped a file of an unsupported type",
        "skipped a file of an unsupported type",
        "skipped a .pdf file: unreadable or encrypted PDF (PdfiumError)",
        "skipped a .docx file: not a Word document: the file is not a zip package",
        "skipped a .txt file: the text is not valid UTF-8",
        "redacted a .txt file (1 output)",
    ])
    text = "\n".join(log)
    assert not [v for v in (*VALUES, "broken", "latin", "good", "hidden", "report") if v in text]
    assert sorted(p.name for p in outbox.iterdir()) == ["good.txt"]
    assert (outbox / "good.txt").read_text(encoding="utf-8") == "Call [PERSON_1]."
    assert {p.name: p.read_bytes() for p in inbox.iterdir()} == files  # originals untouched


def test_two_inputs_with_one_output_name_never_overwrite_each_other(boxes):
    """notes.docx and notes.docx.md both write notes.docx.md: the later one is skipped."""
    inbox, outbox = boxes
    rec = Recorder()
    with running(inbox, outbox, rec) as (_, log):
        (inbox / "notes.docx").write_bytes(b"first")
        wait_for(lambda: (outbox / "notes.docx.md").exists())
        (inbox / "notes.docx.md").write_bytes(b"second, later")
        wait_for(lambda: len(log) >= 3)
    assert log[1:] == ["redacted a .docx file (1 output)",
                       "skipped a .md file: another input in the inbox has an output of the same name"]
    assert (outbox / "notes.docx.md").read_text(encoding="utf-8") == "redacted 5 bytes\n"


def test_an_output_name_from_an_earlier_run_stays_with_its_input(boxes):
    inbox, outbox = boxes
    rec = Recorder()
    with running(inbox, outbox, rec):
        (inbox / "scan.pdf").write_bytes(b"first")
        wait_for(lambda: (outbox / "scan.pdf.md").exists())
    with running(inbox, outbox, rec) as (_, log):  # scan.pdf is done, but its outputs stay claimed
        (inbox / "scan.pdf.md").write_bytes(b"second, later")
        wait_for(lambda: len(log) >= 2)
    assert log[1:] == ["skipped a .md file: another input in the inbox has an output of the same name"]
    assert (outbox / "scan.pdf.md").read_text(encoding="utf-8") == "redacted 5 bytes\n"


def test_a_detection_timeout_skips_the_file_and_writes_nothing(boxes):
    class TimingOut:
        def redact(self, *_args, **_kwargs):
            raise PatternTimeout("a detection pattern timed out; nothing was redacted")

    inbox, outbox = boxes
    convert = lambda name, data: redact_file(name, data, TimingOut(), "scope", destination="outbox")  # noqa: E731
    with running(inbox, outbox, convert) as (_, log):
        (inbox / "notes.txt").write_bytes(b"Call Priya Okafor.")
        wait_for(lambda: len(log) >= 2)
    assert log[1:] == ["skipped a .txt file: a detection pattern timed out; nothing was redacted"]
    assert list(outbox.iterdir()) == []


def test_retention_is_undecided_so_nothing_is_deleted():
    assert watcher.OUTBOX_RETENTION_DAYS is None
