"""`redactit clip` against this machine's real clipboard. Skipped where there is none to
open (CI runners, sandboxes, headless sessions) and until models are installed.

The tests overwrite the clipboard. The text that was on it is put back afterwards;
anything else (an image, copied files) is lost.
"""

import ctypes
import sys

import keyring
import pytest
from redactit import cli, models
from redactit.hosts import clipboard

try:
    models.path_for("gliner/model.onnx")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

import hostkit  # noqa: E402

TEXT = "Call Priya Okafor at priya.okafor@corp.local about card 4111 1111 1111 1111."  # synthetic
VALUES = ("Priya", "Okafor", "corp.local", "4111")


@pytest.fixture
def board():
    try:
        board = clipboard.system_clipboard()
        before = board.read()
    except clipboard.ClipboardError as e:
        pytest.skip(f"no usable clipboard here ({e})")
    yield board
    if before.text and not before.concealed:
        try:
            board.write(before.text, board.read().version)
        except clipboard.ClipboardError:
            pass


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """An in-memory keychain and data folder: the test never touches the real vault."""
    previous = keyring.get_keyring()
    keyring.set_keyring(hostkit.MemoryKeyring())
    monkeypatch.setenv("REDACTIT_DATA_DIR", str(tmp_path / "data"))
    yield
    keyring.set_keyring(previous)


def test_clip_redacts_the_clipboard_in_place(board, cli_env, capsys):
    board.write(TEXT, board.read().version)
    assert cli.main(["clip"]) == 0
    out = board.read().text
    assert not [v for v in VALUES if v in out] and "[PERSON_1]" in out and "[EMAIL_1]" in out
    err = capsys.readouterr().err
    assert err.startswith("redactit clip: clipboard redacted: ") and err.count("\n") == 1
    assert not [v for v in VALUES if v in err]


@pytest.mark.skipif(sys.platform != "win32", reason="sets a Windows clipboard format")
def test_clip_leaves_a_concealed_item_alone(board, cli_env, capsys):
    """A password manager's copy, as KeePass and others mark it: no engine, no change.
    Run in the isolated keychain and data folder all the same: if the concealment check
    ever regressed, the engine it loaded would otherwise open the real vault."""
    secret = "Pr1ya-Okafor-hunter2"  # synthetic
    exclude = board.user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing")
    with board._open():
        board.user32.EmptyClipboard()
        for fmt, data in ((clipboard.CF_UNICODETEXT, (secret + "\0").encode("utf-16-le")), (exclude, b"\0\0\0\0")):
            handle = board.kernel32.GlobalAlloc(clipboard.GMEM_MOVEABLE, len(data))
            ctypes.memmove(board.kernel32.GlobalLock(handle), data, len(data))
            board.kernel32.GlobalUnlock(handle)
            assert board.user32.SetClipboardData(fmt, handle)
    sequence = board.user32.GetClipboardSequenceNumber()

    assert cli.main(["clip"]) == clipboard.SKIPPED
    assert board.user32.GetClipboardSequenceNumber() == sequence  # not written to
    with board._open():
        assert board._text() == secret
    err = capsys.readouterr().err
    assert "concealed" in err and "hunter2" not in err and "Okafor" not in err
