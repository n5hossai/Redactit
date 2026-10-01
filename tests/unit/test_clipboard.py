"""`redactit clip` with every OS call replaced by a stand-in, so the concealment rules for
Windows, macOS and Linux run on any machine (THREAT_MODEL T16)."""

import ctypes
import itertools
import subprocess
from types import SimpleNamespace

import pytest
from redactit import cli, models
from redactit.detect.registry import PatternTimeout
from redactit.hosts import clipboard
from redactit.hosts.clipboard import CF_UNICODETEXT, SKIPPED, ClipboardError, Item, Linux, MacOS, Windows

SECRET = "Pr1ya-Okafor-hunter2"  # synthetic
TEXT = "Call Priya Okafor at priya.okafor@corp.local."


# --- Windows -------------------------------------------------------------------------------

class FakeWin32:
    """user32 and kernel32 over an in-memory clipboard of {format id: bytes}. Global memory
    is real ctypes buffers, so the backend's pointer reads and writes run as on Windows."""

    def __init__(self, formats: dict):
        self.ids: dict[str, int] = {}
        self.board = {self.RegisterClipboardFormatW(k) if isinstance(k, str) else k: v for k, v in formats.items()}
        self.sequence, self.is_open, self.open_fails = 7, False, 0
        self.blocks: dict[int, ctypes.Array] = {}  # every global memory block, by handle
        self.ours: set[int] = set()  # blocks the backend allocated and still owns: must end empty
        self._handles = itertools.count(100)
        self.read_formats: list[int] = []

    # user32
    def RegisterClipboardFormatW(self, name):
        return self.ids.setdefault(name, 0xC000 + len(self.ids))

    def CreateWindowExW(self, *_args):
        return 1

    def DestroyWindow(self, _hwnd):
        return 1

    def OpenClipboard(self, hwnd):
        assert hwnd and not self.is_open  # always with an owner window, never twice
        if self.open_fails:
            self.open_fails -= 1
            return 0
        self.is_open = True
        return 1

    def CloseClipboard(self):
        self.is_open = False
        return 1

    def IsClipboardFormatAvailable(self, fmt):
        assert self.is_open
        return fmt in self.board

    def GetClipboardData(self, fmt):
        assert self.is_open
        self.read_formats.append(fmt)
        return self._alloc(self.board[fmt]) if fmt in self.board else None

    def GetClipboardSequenceNumber(self):
        return self.sequence

    def EmptyClipboard(self):
        assert self.is_open
        self.board.clear()
        self.sequence += 1
        return 1

    def SetClipboardData(self, fmt, handle):
        assert self.is_open
        self.board[fmt] = self.blocks[handle].raw
        self.ours.discard(handle)  # the clipboard owns it now
        self.sequence += 1
        return handle

    # kernel32
    def GlobalAlloc(self, _flags, size):
        handle = self._alloc(b"\0" * size)
        self.ours.add(handle)
        return handle

    def GlobalLock(self, handle):
        return ctypes.addressof(self.blocks[handle])

    def GlobalUnlock(self, _handle):
        return 1

    def GlobalSize(self, handle):
        return len(self.blocks[handle])

    def GlobalFree(self, handle):
        self.ours.discard(handle)

    def _alloc(self, data: bytes) -> int:
        handle = next(self._handles)
        self.blocks[handle] = ctypes.create_string_buffer(data, len(data))
        return handle


def utf16(text: str, padding: int = 0) -> bytes:
    return (text + "\0").encode("utf-16-le") + b"\xab" * padding  # a block larger than its string


def windows(formats: dict) -> tuple[Windows, FakeWin32]:
    fake = FakeWin32(formats)
    return Windows(fake, fake), fake


@pytest.mark.parametrize("marker", [
    {"ExcludeClipboardContentFromMonitorProcessing": b"\0\0\0\0"},
    {"Clipboard Viewer Ignore": b""},
    {"CanIncludeInClipboardHistory": (0).to_bytes(4, "little")},
    {"CanIncludeInClipboardHistory": b""},  # present but unreadable: skipped, not read
], ids=["exclude from monitoring", "viewer ignore", "history 0", "history unreadable"])
def test_windows_skips_concealed_items_without_reading_them(marker):
    board, fake = windows({CF_UNICODETEXT: utf16(SECRET), **marker})
    item = board.read()
    assert item.concealed and item.text is None
    assert CF_UNICODETEXT not in fake.read_formats and not fake.is_open


def test_windows_reads_ordinary_text():
    board, fake = windows({CF_UNICODETEXT: utf16(TEXT + " \U0001F600", padding=6),
                           "CanIncludeInClipboardHistory": (1).to_bytes(4, "little"), "HTML Format": b"<b>x</b>"})
    assert board.read() == Item(TEXT + " \U0001F600", False, 7)
    board, _ = windows({"HTML Format": b"<b>x</b>"})
    assert board.read() == Item(None, False, 7)


def test_windows_write_replaces_every_format_with_the_text():
    board, fake = windows({CF_UNICODETEXT: utf16(TEXT), "HTML Format": b"<b>Priya</b>"})
    item = board.read()
    board.write("Call [PERSON_1].", item.version)
    assert fake.board == {CF_UNICODETEXT: utf16("Call [PERSON_1].")} and fake.ours == set()
    assert not fake.is_open


def test_windows_refuses_to_write_after_the_clipboard_changed():
    board, fake = windows({CF_UNICODETEXT: utf16(TEXT)})
    item = board.read()
    fake.sequence += 1  # the user copied something else meanwhile
    with pytest.raises(ClipboardError, match="changed"):
        board.write("Call [PERSON_1].", item.version)
    assert fake.board == {CF_UNICODETEXT: utf16(TEXT)} and fake.ours == set()  # untouched, nothing leaked


def test_windows_retries_a_busy_clipboard_then_gives_up(monkeypatch):
    monkeypatch.setattr(clipboard.time, "sleep", lambda _s: None)
    board, fake = windows({CF_UNICODETEXT: utf16(TEXT)})
    fake.open_fails = 3
    assert board.read().text == TEXT
    fake.open_fails = clipboard.OPEN_TRIES
    with pytest.raises(ClipboardError, match="could not be opened"):
        board.read()


# --- macOS ---------------------------------------------------------------------------------

class FakePasteboard:
    def __init__(self, items: dict):
        self.items, self.count, self.strings_read = dict(items), 41, []

    def changeCount(self):
        return self.count

    def types(self):
        return list(self.items)

    def stringForType_(self, kind):
        self.strings_read.append(kind)
        return self.items.get(kind)

    def clearContents(self):
        self.items.clear()
        self.count += 1
        return self.count

    def setString_forType_(self, text, kind):
        self.items[kind] = text
        return True


PLAIN = "public.utf8-plain-text"


def test_macos_skips_concealed_items_without_reading_them():
    pasteboard = FakePasteboard({PLAIN: SECRET, "org.nspasteboard.ConcealedType": ""})
    item = MacOS(pasteboard).read()
    assert item == Item(None, True, 41) and pasteboard.strings_read == []


def test_macos_reads_and_replaces_ordinary_text():
    pasteboard = FakePasteboard({PLAIN: TEXT, "public.html": "<b>Priya</b>", "org.nspasteboard.TransientType": ""})
    board = MacOS(pasteboard)
    item = board.read()
    assert item == Item(TEXT, False, 41)
    board.write("Call [PERSON_1].", item.version)
    assert pasteboard.items == {PLAIN: "Call [PERSON_1]."}
    assert MacOS(FakePasteboard({"public.png": b""})).read().text is None


def test_macos_refuses_to_write_after_the_clipboard_changed():
    pasteboard = FakePasteboard({PLAIN: TEXT})
    board = MacOS(pasteboard)
    item = board.read()
    pasteboard.count += 1
    with pytest.raises(ClipboardError, match="changed"):
        board.write("Call [PERSON_1].", item.version)
    assert pasteboard.items == {PLAIN: TEXT}


def test_macos_refuses_text_read_after_the_concealment_check_went_stale():
    pasteboard = FakePasteboard({PLAIN: TEXT})
    read = pasteboard.stringForType_

    def racing(kind):  # a password manager copies between the check and the read
        pasteboard.items = {PLAIN: SECRET, "org.nspasteboard.ConcealedType": ""}
        pasteboard.count += 1
        return read(kind)

    pasteboard.stringForType_ = racing
    with pytest.raises(ClipboardError, match="changed while it was read"):
        MacOS(pasteboard).read()


# --- Linux ---------------------------------------------------------------------------------

SESSIONS = {
    "wayland": ({"WAYLAND_DISPLAY": "wayland-0"}, {"wl-paste", "wl-copy"}),
    "x11": ({"DISPLAY": ":0"}, {"xclip"}),
}


class FakeTools:
    """wl-paste/wl-copy and xclip over an in-memory clipboard of {type: bytes}."""

    def __init__(self, board: dict):
        self.board, self.calls = dict(board), []

    def __call__(self, cmd, input=None, **kwargs):
        self.calls.append((cmd, kwargs))
        out, code = b"", 0
        if cmd in (["wl-paste", "--list-types"], ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]):
            out, code = "".join(f"{t}\n" for t in self.board).encode(), 0 if self.board else 1
        elif cmd[0] in ("wl-paste", "xclip") and "-i" not in cmd:
            out, code = (self.board[cmd[-1]], 0) if cmd[-1] in self.board else (b"", 1)
        else:  # wl-copy or xclip -i: takes the clipboard over with this one type
            assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
            self.board = {cmd[-1]: input}
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr=b"")

    def text_reads(self):
        return [cmd for cmd, _ in self.calls if cmd[0] in ("wl-paste", "xclip") and cmd[-1] in Linux.TEXT_TYPES]


def linux(session: str, board: dict) -> tuple[Linux, FakeTools]:
    environ, tools = SESSIONS[session]
    fake = FakeTools(board)
    return Linux(run=fake, which=lambda name: f"/usr/bin/{name}" if name in tools else None, environ=environ), fake


@pytest.mark.parametrize("session", SESSIONS)
def test_linux_skips_items_kde_marks_secret_without_reading_them(session):
    board, fake = linux(session, {"text/plain;charset=utf-8": SECRET.encode(), "x-kde-passwordManagerHint": b"secret"})
    assert board.read().concealed and fake.text_reads() == []
    board, fake = linux(session, {"UTF8_STRING": TEXT.encode(), "x-kde-passwordManagerHint": b"public"})
    assert board.read().text == TEXT


@pytest.mark.parametrize("session", SESSIONS)
def test_linux_reads_and_replaces_ordinary_text(session):
    board, fake = linux(session, {"text/html": b"<b>Priya</b>", "text/plain": TEXT.encode()})
    item = board.read()
    assert item.text == TEXT and not item.concealed
    board.write("Call [PERSON_1].", item.version)
    assert list(fake.board.values()) == [b"Call [PERSON_1]."]
    assert linux(session, {})[0].read() == Item(None, False, ((), False, None))  # empty clipboard
    assert linux(session, {"image/png": b"\x89PNG"})[0].read().text is None


@pytest.mark.parametrize("session", SESSIONS)
def test_linux_refuses_to_write_after_the_clipboard_changed(session):
    board, fake = linux(session, {"text/plain;charset=utf-8": TEXT.encode()})
    item = board.read()
    fake.board["text/plain;charset=utf-8"] = b"something newer"
    with pytest.raises(ClipboardError, match="changed"):
        board.write("Call [PERSON_1].", item.version)
    assert fake.board == {"text/plain;charset=utf-8": b"something newer"}


UTF8 = "text/plain;charset=utf-8"


@pytest.mark.parametrize("session", SESSIONS)
@pytest.mark.parametrize("before, after", [
    ({UTF8: TEXT.encode()}, {UTF8: SECRET.encode(), Linux.HINT: b"secret"}),
    ({UTF8: TEXT.encode(), Linux.HINT: b"public"}, {UTF8: SECRET.encode(), Linux.HINT: b"secret"}),
    ({UTF8: TEXT.encode()}, {UTF8: b"copied later", "text/html": b"<b>copied later</b>"}),
], ids=["now concealed", "same types, now concealed", "other types"])
def test_linux_refuses_text_read_after_the_concealment_check_went_stale(session, before, after):
    board, fake = linux(session, before)

    def racing(cmd, **kwargs):  # another owner takes the clipboard as the text is read
        result = fake(cmd, **kwargs)
        if cmd[0] in ("wl-paste", "xclip") and "-i" not in cmd and cmd[-1] == UTF8:
            fake.board = dict(after)
        return result

    board._run = racing
    with pytest.raises(ClipboardError, match="changed while it was read"):
        board.read()


def test_linux_without_a_clipboard_tool_fails_with_a_hint():
    with pytest.raises(ClipboardError, match="wl-clipboard"):
        Linux(run=None, which=lambda _name: None, environ={"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"})


# --- the command ---------------------------------------------------------------------------

class FakeBoard:
    def __init__(self, item: Item):
        self.item, self.written = item, None

    def read(self):
        return self.item

    def write(self, text, version):
        if version != self.item.version:
            raise ClipboardError(clipboard.CHANGED)
        self.written = text


class FakeEngine:
    def redact(self, text, scope, **kwargs):
        assert kwargs == {"file_type": "clip", "destination": "clipboard"}
        decisions = [SimpleNamespace(span=SimpleNamespace(entity_type=t)) for t in ("PERSON", "EMAIL", "PERSON")]
        return SimpleNamespace(text="Call [PERSON_1] at [EMAIL_1].", decisions=decisions)


def never():
    raise AssertionError("the engine must not load")


@pytest.mark.parametrize("item, message", [
    (Item(None, True, 1), "a password manager marked the item concealed"),
    (Item(None, False, 1), "it holds no text"),
    (Item("", False, 1), "it holds no text"),
])
def test_nothing_to_redact_leaves_the_clipboard_and_skips_the_engine(item, message, capsys):
    board = FakeBoard(item)
    assert clipboard.redact_clipboard(never, board) == SKIPPED
    assert board.written is None
    assert capsys.readouterr().err == f"redactit clip: clipboard left unchanged: {message}\n"


def test_redacted_text_is_written_back_and_only_counts_are_printed(capsys):
    board = FakeBoard(Item(TEXT, False, 1))
    assert clipboard.redact_clipboard(FakeEngine, board) == 0
    assert board.written == "Call [PERSON_1] at [EMAIL_1]."
    err = capsys.readouterr().err
    assert err == "redactit clip: clipboard redacted: 3 values (1 EMAIL, 2 PERSON)\n"


@pytest.fixture
def command(monkeypatch):
    """`redactit clip` through cli.main, with a stand-in clipboard and engine."""
    monkeypatch.setattr(models, "verify", lambda *_args: None)
    board = FakeBoard(Item(TEXT, False, 1))
    monkeypatch.setattr(clipboard, "system_clipboard", lambda: board)
    return board


def test_a_failing_engine_leaves_the_clipboard_untouched(command, monkeypatch, capsys):
    def broken(*_args):
        raise RuntimeError(f"model choked on {TEXT}")

    monkeypatch.setattr(cli, "open_engine", broken)
    assert cli.main(["clip"]) == 1
    assert command.written is None
    err = capsys.readouterr().err
    assert "RuntimeError (details withheld" in err and "Priya" not in err and "Okafor" not in err


def test_a_detection_timeout_leaves_the_clipboard_untouched(command, monkeypatch, capsys):
    class TimingOut:
        def redact(self, *_args, **_kwargs):
            raise PatternTimeout("a detection pattern timed out; nothing was redacted")

    monkeypatch.setattr(cli, "open_engine", lambda *_args: TimingOut())
    assert cli.main(["clip"]) == 1
    assert command.written is None
    assert capsys.readouterr().err == "error: a detection pattern timed out; nothing was redacted\n"


def test_a_clipboard_that_changed_meanwhile_is_left_alone(command, monkeypatch, capsys):
    def engine(*_args):
        command.item = Item("copied later", False, 2)  # the user copied something else meanwhile
        return FakeEngine()

    monkeypatch.setattr(cli, "open_engine", engine)
    assert cli.main(["clip"]) == 1
    assert command.written is None
    assert capsys.readouterr().err == f"error: {clipboard.CHANGED}\n"
