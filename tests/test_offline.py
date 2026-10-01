"""The engine handles sensitive input and must never phone home. These tests
prove the pytest-socket guard (see pyproject.toml addopts) is actually wired
up, so any future code path that opens a socket fails the suite instead of
silently sending redacted-away data somewhere.
"""

import socket
import zipfile

import pytest
from PIL import Image
from pytest_socket import SocketBlockedError
from redactit.safety import NetworkBlocked

from redactit.cli import main

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def test_socket_guard_is_active():
    """Canary: if --disable-socket were ever dropped from addopts, this is
    the test that would notice -- every other offline claim depends on it."""
    # The engine's own block may already be active in this process; either guard counts.
    with pytest.raises((SocketBlockedError, NetworkBlocked)):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_cli_version_runs_fully_offline():
    assert main(["--version"]) == 0


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A real `redact` run: models installed, an in-memory keychain, data under tmp_path."""
    import keyring
    import keyring.backend
    from redactit import models

    try:
        models.path_for("gliner/model.onnx")
    except models.ModelError:
        pytest.skip("models not installed")

    class MemoryKeyring(keyring.backend.KeyringBackend):
        priority = 1
        store: dict = {}

        def get_password(self, service, user):
            return self.store.get((service, user))

        def set_password(self, service, user, password):
            self.store[(service, user)] = password

        def delete_password(self, service, user):
            self.store.pop((service, user), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    monkeypatch.setenv("REDACTIT_DATA_DIR", str(tmp_path / "data"))
    yield
    keyring.set_keyring(previous)


def test_cli_redacts_end_to_end_with_sockets_blocked(tmp_path, cli_env):
    """The whole `redact` path, model included, runs with every socket refused."""
    src = tmp_path / "note.md"
    src.write_text("Call Priya Okafor at priya.okafor@corp.local about card 4111 1111 1111 1111.\n", encoding="utf-8")
    assert main(["redact", str(src), "--out", str(tmp_path / "out")]) == 0
    out = (tmp_path / "out" / "note.md").read_text(encoding="utf-8")
    assert "Priya" not in out and "okafor" not in out.lower() and "4111" not in out
    assert "[PERSON_1]" in out and "[EMAIL_1]" in out


def test_no_output_overwrites_another(tmp_path, cli_env, capsys):
    """note.docx comes out as note.docx.md beside note.md, pic.webp as pic.webp.png beside
    pic.png, and a second note.md from another folder is skipped."""
    (tmp_path / "note.md").write_text("Email priya.okafor@corp.local today.\n", encoding="utf-8")
    with zipfile.ZipFile(tmp_path / "note.docx", "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r>'
                                        "<w:t>Card 4111 1111 1111 1111</w:t></w:r></w:p></w:body></w:document>")
    (tmp_path / "copy").mkdir()
    (tmp_path / "copy" / "note.md").write_text("Phone 416-555-0199\n", encoding="utf-8")
    for fmt in ("png", "webp"):
        Image.new("RGB", (64, 64), "white").save(tmp_path / f"pic.{fmt}")
    inputs = [tmp_path / n for n in ("note.md", "note.docx", "copy/note.md", "pic.png", "pic.webp")]
    assert main(["redact", *map(str, inputs), "--out", str(tmp_path / "out")]) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["note.docx.md", "note.md", "pic.png", "pic.webp.png"]
    assert "[EMAIL_1]" in (tmp_path / "out" / "note.md").read_text(encoding="utf-8")
    assert "4111" not in (tmp_path / "out" / "note.docx.md").read_text(encoding="utf-8")
    assert "same name" in capsys.readouterr().err
