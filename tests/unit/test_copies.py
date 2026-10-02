"""Redacted copies (redactit.copies): only a copy Redactit recorded, still as it wrote it,
is deleted, and only once it is older than the retention period. Time is a fake clock."""

import json
import os
from types import SimpleNamespace

import pytest
from redactit import copies
from redactit.audit import AuditLog
from redactit.copies import RETENTION_DAYS, Copies

DAY = 86400.0
START = 1_800_000_000.0
NAME = "Priya Okafor passport.txt"  # synthetic; a name that must never reach a result or the audit file


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


@pytest.fixture
def box(tmp_path):
    clock, data = Clock(), tmp_path / "data"
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    store = Copies(data / "copies.json", clock=clock, audit=AuditLog(data / "audit.jsonl"))
    return SimpleNamespace(copies=store, clock=clock, outbox=outbox, index=data / "copies.json",
                           audit=data / "audit.jsonl", tmp=tmp_path)


def written(box, name=NAME, data=b"[PERSON_1]'s passport\n"):
    """An output as the watcher leaves it: written, then recorded with its lstat."""
    path = box.outbox / name
    path.write_bytes(data)
    box.copies.record([(path, os.lstat(path))])
    return path


def recorded(box):
    return [entry["path"] for entry in json.loads(box.index.read_text(encoding="utf-8"))["copies"]]


def audit_events(box):
    return [json.loads(line) for line in box.audit.read_text(encoding="utf-8").splitlines()]


def test_retention_is_the_owners_30_days():
    assert RETENTION_DAYS == 30 and Copies(".").retention_days == 30


def test_a_copy_older_than_the_retention_period_is_deleted_and_a_newer_one_kept(box):
    old = written(box, "old.txt")
    box.clock.now += 2 * DAY
    new = written(box, "new.txt")
    box.clock.now = START + RETENTION_DAYS * DAY + 60  # old: a minute past 30 days; new: 28 days

    result = box.copies.purge()

    assert not old.exists() and new.exists()
    assert (result.purged, result.dropped, result.kept) == (1, 0, 1)
    assert recorded(box) == [str(new)]


def test_the_users_own_file_in_the_outbox_is_never_deleted(box):
    own = box.outbox / "my own notes.txt"
    own.write_bytes(b"the user's file, never recorded")
    os.utime(own, (0, 0))  # as old as a file can look
    copy = written(box)
    box.clock.now += 10 * 365 * DAY

    result = box.copies.purge()

    assert own.read_bytes() == b"the user's file, never recorded"
    assert not copy.exists() and result.purged == 1


def _edit(path):  # the same file, rewritten in place: same ID and size, a newer mtime
    st = os.stat(path)
    path.write_bytes(path.read_bytes().upper())
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def _replace(path):  # the user's own file put in its place: same size and mtime, another file ID
    st, theirs = os.stat(path), path.with_name("theirs.tmp")
    theirs.write_bytes(path.read_bytes().upper())
    os.utime(theirs, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(theirs, path)


@pytest.mark.parametrize("change", [_edit, _replace], ids=["edited", "replaced"])
def test_a_copy_the_user_edited_or_replaced_is_left_alone(box, change):
    copy = written(box)
    change(copy)
    after = copy.read_bytes()
    box.clock.now += 365 * DAY

    result = box.copies.purge()

    assert copy.read_bytes() == after
    assert (result.purged, result.dropped) == (0, 1) and recorded(box) == []  # theirs now, never checked again


def test_a_copy_replaced_by_a_symlink_is_not_followed_or_deleted(box):
    copy = written(box)
    target = box.tmp / "elsewhere.txt"
    target.write_bytes(b"a file the link names")
    copy.unlink()
    try:
        os.symlink(target, copy)
    except OSError:  # Windows needs Developer Mode or an elevated process for this
        pytest.skip("symlinks cannot be created here")
    box.clock.now += 365 * DAY

    result = box.copies.purge()

    assert target.read_bytes() == b"a file the link names" and os.path.islink(copy)
    assert (result.purged, result.dropped) == (0, 1)


SYMLINK, MOUNT_POINT = 0xA000000C, 0xA0000003  # IO_REPARSE_TAG_* values


@pytest.mark.parametrize("tag", [SYMLINK, MOUNT_POINT], ids=["symlink", "mount point"])
def test_a_reparse_point_naming_another_path_is_left_alone_even_with_a_matching_fingerprint(box, monkeypatch, tag):
    """The link rule on its own: the entry reports the recorded fingerprint, so only the
    rule keeps it. Stat results are stood in, so this runs where no link can be made."""
    copy = written(box)
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if os.path.basename(path) != NAME:
            return st
        fields = {k: getattr(st, k) for k in dir(st) if k.startswith("st_")}
        return SimpleNamespace(**{**fields, "st_file_attributes": 0x400, "st_reparse_tag": tag})

    monkeypatch.setattr(copies.os, "lstat", lstat)
    box.clock.now += 365 * DAY

    result = box.copies.purge()

    assert copy.exists() and (result.purged, result.dropped) == (0, 1)


def test_nothing_in_the_inbox_is_touched(box):
    copy = written(box)  # an earlier run's outbox, now watched as the inbox
    box.clock.now += 365 * DAY

    result = box.copies.purge(inbox=box.outbox)

    assert copy.exists() and (result.purged, result.dropped) == (0, 1)


def _half(box):
    whole = box.index.read_bytes()
    return whole[: len(whole) // 2]


@pytest.mark.parametrize("damage", [
    lambda box: _half(box),  # a write cut off part way
    lambda box: b"",
    lambda box: b"\xff\xfe not UTF-8",
    lambda box: json.dumps({"version": 1, "copies": [{"path": 5}]}).encode(),
    lambda box: json.dumps({"version": 2, "copies": []}).encode(),
    lambda box: json.dumps([str(box.outbox / NAME)]).encode(),
], ids=["half written", "empty", "not text", "bad entry", "unknown version", "not an index"])
def test_a_corrupt_index_deletes_nothing(box, damage):
    copy = written(box)
    box.index.write_bytes(damage(box))
    box.clock.now += 365 * DAY

    result = box.copies.purge()

    assert copy.exists()
    assert (result.purged, result.readable) == (0, False)
    assert result.problem.startswith("deleted nothing: the index of redacted copies cannot be read (")
    assert NAME not in result.problem


def test_an_unreadable_index_deletes_nothing(box):
    copy = written(box)
    box.index.unlink()
    box.index.mkdir()  # reading it fails with an OSError on every OS
    box.clock.now += 365 * DAY

    result = box.copies.purge()

    assert copy.exists() and not result.readable and "cannot be read" in result.problem


def test_a_failed_save_leaves_the_previous_index_whole(box, monkeypatch):
    first = written(box, "first.txt")
    second = box.outbox / "second.txt"
    second.write_bytes(b"redacted")

    def crash(_fd):  # the machine stops after part of the new index is written
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(copies.os, "fsync", crash)
    with pytest.raises(OSError):
        box.copies.record([(second, os.lstat(second))])
    monkeypatch.undo()

    assert recorded(box) == [str(first)]
    assert not [p for p in box.index.parent.iterdir() if p.suffix == ".tmp"]  # no partial index left beside it
    box.clock.now += 365 * DAY
    assert box.copies.purge().purged == 1 and not first.exists()


def test_a_corrupt_index_is_started_again_by_the_next_record(box):
    lost = written(box, "lost.txt")
    box.index.write_bytes(b"{")
    copy = written(box)
    box.clock.now += 365 * DAY

    box.copies.purge()

    assert lost.exists() and not copy.exists()  # an entry the damage lost is kept, never guessed at


def test_a_rewritten_output_replaces_its_entry(box):
    copy = written(box)
    box.clock.now += 20 * DAY
    written(box, data=b"[PERSON_1]'s passport, redacted again\n")
    box.clock.now += 20 * DAY  # 40 days after the first version, 20 after the second

    result = box.copies.purge()

    assert copy.exists() and (result.purged, result.kept) == (0, 1) and recorded(box) == [str(copy)]


def test_the_purge_audits_counts_only(box):
    written(box, "expired.txt")
    changed = written(box, "changed.txt")
    box.clock.now += 31 * DAY
    written(box)  # fresh
    changed.write_bytes(b"edited by the user")

    box.copies.purge()
    box.index.write_bytes(b"not json")
    box.copies.purge()

    events = audit_events(box)
    assert [{k: v for k, v in e.items() if k != "ts"} for e in events] == [
        {"event": "copies_purge", "purged_count": 1, "dropped_count": 1, "kept_count": 1,
         "retention_days": 30, "index_readable": True},
        {"event": "copies_purge", "purged_count": 0, "dropped_count": 0, "kept_count": 0,
         "retention_days": 30, "index_readable": False},
    ]
    text = box.audit.read_text(encoding="utf-8")
    assert not [v for v in ("Priya", "Okafor", "passport", "expired", "changed", box.tmp.name) if v in text]
