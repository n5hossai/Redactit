"""Clipboard redaction for an OS keyboard shortcut: `redactit clip`.

One run per keypress, never a monitor (THREAT_MODEL T16): read the clipboard's text, leave
an item a password manager marked concealed alone, redact the text with the engine, and
write the result back. One line goes to stderr, with counts and entity types only.

Exit status: 0 when the redacted text was written back; 3 when there was nothing to
redact (no text, or a concealed item); 1 when anything failed. Unless it is 0, the
clipboard was not touched.

Fail closed. Nothing is written until the whole text is redacted, and nothing is written
if the clipboard changed while the engine ran: the user copied something else, which the
older text must not replace. The write replaces every format on the clipboard with plain
text, because the program that copied the text usually left HTML or RTF copies of the
original beside it, and those were not checked.

Concealment markers, per OS. A concealed item's text is never read at all.

- Windows: the registered formats "ExcludeClipboardContentFromMonitorProcessing" and
  "Clipboard Viewer Ignore", or "CanIncludeInClipboardHistory" holding 0. Read through
  ctypes.
- macOS: the pasteboard type "org.nspasteboard.ConcealedType" (nspasteboard.org), through
  pyobjc.
- Linux: "x-kde-passwordManagerHint" holding "secret", read with wl-paste (Wayland) or
  xclip (X11), run as separate programs. GNOME and some Wayland compositors set no such
  hint, so there a copied password looks like any other text (THREAT_MODEL §5).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass

from redactit.types import RedactitError

SKIPPED = 3  # exit status: nothing to redact, clipboard untouched
CHANGED = "the clipboard changed while redacting; it was left as it is"


class ClipboardError(RedactitError):
    """The clipboard could not be read or written. The message never quotes its content."""


@dataclass(frozen=True)
class Item:
    text: str | None  # None when there is no text, or the item is concealed
    concealed: bool
    version: object  # differs once the clipboard changes: a sequence number, a change count or the content


def redact_clipboard(open_engine, clipboard=None, scope: str | None = None) -> int:
    """Read, check, redact and write back, as described above; returns the exit status.

    `open_engine` is called only once there is text to redact, so a concealed or empty
    clipboard never loads the models.
    """
    board = clipboard or system_clipboard()
    item = board.read()
    if item.concealed:
        _say("clipboard left unchanged: a password manager marked the item concealed")
        return SKIPPED
    if not item.text:
        _say("clipboard left unchanged: it holds no text")
        return SKIPPED
    result = open_engine().redact(item.text, scope or uuid.uuid4().hex, file_type="clip", destination="clipboard")
    # Written back even when nothing was found: it drops the unchecked HTML and RTF copies.
    board.write(result.text, item.version)
    counts = Counter(d.span.entity_type for d in result.decisions)
    total = sum(counts.values())
    detail = " (" + ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())) + ")" if counts else ""
    _say(f"clipboard redacted: {total} value{'' if total == 1 else 's'}{detail}")
    return 0


def _say(message: str) -> None:
    print(f"redactit clip: {message}", file=sys.stderr, flush=True)


def system_clipboard():
    if sys.platform == "win32":
        return Windows()
    if sys.platform == "darwin":
        return MacOS()
    return Linux()


# --- Windows -------------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3  # parent of a message-only window: never shown
OPEN_TRIES = 20  # 50 ms apart: another program may hold the clipboard for a moment


class Windows:
    """The Win32 clipboard through ctypes. `user32` and `kernel32` can be stand-ins (tests)."""

    CONCEALED = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")
    HISTORY = "CanIncludeInClipboardHistory"  # a DWORD; 0 keeps the item out of Windows' clipboard history

    def __init__(self, user32=None, kernel32=None) -> None:
        if user32 is None:
            user32, kernel32 = _win32()
        self.user32, self.kernel32 = user32, kernel32

    def read(self) -> Item:
        with self._open():
            concealed = any(self._has(name) for name in self.CONCEALED) or self._history_flag() == 0
            text = None if concealed else self._text()
            # Taken last, with the clipboard still open: reading a format its owner renders
            # on demand makes the owner write it, which may count as a change.
            return Item(text, concealed, self.user32.GetClipboardSequenceNumber())

    def write(self, text: str, version) -> None:
        data = (text + "\0").encode("utf-16-le", "surrogatepass")
        handle = self.kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("no memory for the redacted text; the clipboard was left as it is")
        try:
            # Filled before the clipboard is emptied, so emptying it is followed only by the hand-over.
            pointer = self.kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("no memory for the redacted text; the clipboard was left as it is")
            ctypes.memmove(pointer, data, len(data))
            self.kernel32.GlobalUnlock(handle)
            with self._open():
                if self.user32.GetClipboardSequenceNumber() != version:
                    raise ClipboardError(CHANGED)
                if not self.user32.EmptyClipboard():
                    raise ClipboardError("the clipboard could not be emptied; it was left as it is")
                if not self.user32.SetClipboardData(CF_UNICODETEXT, handle):
                    raise ClipboardError("the redacted text could not be written; the clipboard is now empty")
                handle = None  # the clipboard owns the memory now
        finally:
            if handle:
                self.kernel32.GlobalFree(handle)

    @contextmanager
    def _open(self):
        # A window of our own to own the clipboard: opened without one, EmptyClipboard leaves
        # it ownerless and SetClipboardData then fails (EmptyClipboard's documentation).
        window = self.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
        if not window:
            raise ClipboardError("no clipboard: a window could not be created (no desktop session?)")
        try:
            for _ in range(OPEN_TRIES):
                if self.user32.OpenClipboard(window):
                    break
                time.sleep(0.05)
            else:
                raise ClipboardError("the clipboard could not be opened (another program holds it, or access is denied)")
            try:
                yield
            finally:
                self.user32.CloseClipboard()
        finally:
            self.user32.DestroyWindow(window)

    def _format(self, name: str) -> int:
        """The ID of a registered format if the clipboard holds it, else 0."""
        fmt = self.user32.RegisterClipboardFormatW(name)
        return fmt if fmt and self.user32.IsClipboardFormatAvailable(fmt) else 0

    def _has(self, name: str) -> bool:
        return bool(self._format(name))

    def _history_flag(self) -> int | None:
        fmt = self._format(self.HISTORY)
        if not fmt:
            return None
        data = self._data(fmt)
        # Present but unreadable counts as 0: skipping a clipboard is cheaper than reading a secret.
        return int.from_bytes(data[:4], "little") if data and len(data) >= 4 else 0

    def _text(self) -> str | None:
        if not self.user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        data = self._data(CF_UNICODETEXT)
        if data is None:
            raise ClipboardError("the clipboard's text could not be read")
        text = data[:len(data) // 2 * 2].decode("utf-16-le", "surrogatepass")
        return text.split("\0", 1)[0]  # the memory block may be larger than the string in it

    def _data(self, fmt: int) -> bytes | None:
        handle = self.user32.GetClipboardData(fmt)
        if not handle:
            return None
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, self.kernel32.GlobalSize(handle))
        finally:
            self.kernel32.GlobalUnlock(handle)


def _win32():
    """user32 and kernel32 with their signatures declared: handles are pointer-sized, and
    ctypes' default int result would cut them in half on 64-bit Windows."""
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        (user32, "CreateWindowExW"): ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
        (user32, "DestroyWindow"): ([w.HWND], w.BOOL),
        (user32, "OpenClipboard"): ([w.HWND], w.BOOL),
        (user32, "CloseClipboard"): ([], w.BOOL),
        (user32, "EmptyClipboard"): ([], w.BOOL),
        (user32, "GetClipboardSequenceNumber"): ([], w.DWORD),
        (user32, "RegisterClipboardFormatW"): ([w.LPCWSTR], w.UINT),
        (user32, "IsClipboardFormatAvailable"): ([w.UINT], w.BOOL),
        (user32, "GetClipboardData"): ([w.UINT], w.HANDLE),
        (user32, "SetClipboardData"): ([w.UINT, w.HANDLE], w.HANDLE),
        (kernel32, "GlobalAlloc"): ([w.UINT, ctypes.c_size_t], w.HGLOBAL),
        (kernel32, "GlobalLock"): ([w.HGLOBAL], w.LPVOID),
        (kernel32, "GlobalUnlock"): ([w.HGLOBAL], w.BOOL),
        (kernel32, "GlobalSize"): ([w.HGLOBAL], ctypes.c_size_t),
        (kernel32, "GlobalFree"): ([w.HGLOBAL], w.HGLOBAL),
    }
    for (dll, name), (args, result) in signatures.items():
        function = getattr(dll, name)
        function.argtypes, function.restype = args, result
    return user32, kernel32


# --- macOS ---------------------------------------------------------------------------------

class MacOS:
    """NSPasteboard through pyobjc. `pasteboard` can be a stand-in (tests)."""

    CONCEALED = "org.nspasteboard.ConcealedType"

    def __init__(self, pasteboard=None, text_type: str = "public.utf8-plain-text") -> None:
        if pasteboard is None:
            try:
                from AppKit import NSPasteboard, NSPasteboardTypeString
            except ImportError:
                raise ClipboardError("pyobjc-framework-Cocoa is missing; reinstall Redactit") from None
            pasteboard, text_type = NSPasteboard.generalPasteboard(), NSPasteboardTypeString
        self.pasteboard, self.text_type = pasteboard, text_type

    def read(self) -> Item:
        # Counted first: any change after this point, even during this read, refuses the write.
        version = self.pasteboard.changeCount()
        if self.CONCEALED in list(self.pasteboard.types() or []):
            return Item(None, True, version)
        text = self.pasteboard.stringForType_(self.text_type)
        return Item(None if text is None else str(text), False, version)

    def write(self, text: str, version) -> None:
        if self.pasteboard.changeCount() != version:
            raise ClipboardError(CHANGED)
        self.pasteboard.clearContents()
        if not self.pasteboard.setString_forType_(text, self.text_type):
            raise ClipboardError("the redacted text could not be written; the clipboard is now empty")


# --- Linux ---------------------------------------------------------------------------------

TOOL_TIMEOUT = 5.0  # seconds; a clipboard owner that hangs must not hang the shortcut


class Linux:
    """wl-paste/wl-copy on Wayland, else xclip on X11, run as separate programs.
    `run`, `which` and `environ` can be stand-ins (tests)."""

    HINT = "x-kde-passwordManagerHint"
    TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain")  # preferred first; all read as UTF-8

    def __init__(self, run=subprocess.run, which=shutil.which, environ=os.environ) -> None:
        self._run = run
        if environ.get("WAYLAND_DISPLAY") and which("wl-paste") and which("wl-copy"):
            self._list = ["wl-paste", "--list-types"]
            self._get = lambda kind: ["wl-paste", "--no-newline", "--type", kind]
            self._put = ["wl-copy", "--type", "text/plain;charset=utf-8"]
        elif environ.get("DISPLAY") and which("xclip"):
            self._list = ["xclip", "-selection", "clipboard", "-o", "-t", "TARGETS"]
            self._get = lambda kind: ["xclip", "-selection", "clipboard", "-o", "-t", kind]
            self._put = ["xclip", "-selection", "clipboard", "-i", "-t", "UTF8_STRING"]
        else:
            raise ClipboardError("no clipboard tool: install wl-clipboard (Wayland) or xclip (X11)")

    def read(self) -> Item:
        snapshot = self._snapshot()
        _, concealed, data = snapshot
        if data is None:
            return Item(None, concealed, snapshot)
        try:
            return Item(data.decode("utf-8"), False, snapshot)
        except UnicodeDecodeError:
            raise ClipboardError("the clipboard's text is not UTF-8") from None

    def write(self, text: str, version) -> None:
        if self._snapshot() != version:  # no change counter here: the content itself is compared
            raise ClipboardError(CHANGED)
        # wl-copy and xclip stay behind to serve the clipboard, so their output goes to the
        # null device: a pipe would keep this process waiting for them to exit.
        try:
            proc = self._run(self._put, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, timeout=TOOL_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClipboardError(f"{self._put[0]} failed ({type(e).__name__})") from None
        if proc.returncode != 0:
            raise ClipboardError(f"{self._put[0]} could not write the redacted text")

    def _snapshot(self) -> tuple[tuple[str, ...], bool, bytes | None]:
        """(the clipboard's types, concealed, the text's bytes or None)."""
        listed = self._call(self._list)
        if listed is None:  # both tools fail on an empty clipboard
            return (), False, None
        types = tuple(line.strip() for line in listed.decode("utf-8", "replace").splitlines() if line.strip())
        if self.HINT in types:
            hint = self._call(self._get(self.HINT))
            if hint is None or hint.strip() == b"secret":  # unreadable counts as secret
                return types, True, None
        kind = next((t for t in self.TEXT_TYPES if t in types), None)
        if kind is None:
            return types, False, None
        data = self._call(self._get(kind))
        if data is None:
            raise ClipboardError("the clipboard's text could not be read")
        return types, False, data

    def _call(self, cmd: list[str]) -> bytes | None:
        try:
            proc = self._run(cmd, capture_output=True, timeout=TOOL_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ClipboardError(f"{cmd[0]} failed ({type(e).__name__})") from None
        return proc.stdout if proc.returncode == 0 else None

