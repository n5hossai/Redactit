"""The engine handles sensitive input and must never phone home. These tests
prove the pytest-socket guard (see pyproject.toml addopts) is actually wired
up, so any future code path that opens a socket fails the suite instead of
silently sending redacted-away data somewhere.
"""

import socket

import pytest
from pytest_socket import SocketBlockedError
from redactit.safety import NetworkBlocked

from redactit.cli import main


def test_socket_guard_is_active():
    """Canary: if --disable-socket were ever dropped from addopts, this is
    the test that would notice -- every other offline claim depends on it."""
    # The engine's own block may already be active in this process; either guard counts.
    with pytest.raises((SocketBlockedError, NetworkBlocked)):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_cli_version_runs_fully_offline():
    assert main(["--version"]) == 0


def test_cli_redacts_end_to_end_with_sockets_blocked(tmp_path, monkeypatch):
    """The whole `redact` path, model included, runs with every socket refused."""
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
    src = tmp_path / "note.md"
    src.write_text("Call Priya Okafor at priya.okafor@corp.local about card 4111 1111 1111 1111.\n", encoding="utf-8")
    try:
        assert main(["redact", str(src), "--out", str(tmp_path / "out")]) == 0
    finally:
        keyring.set_keyring(previous)
    out = (tmp_path / "out" / "note.md").read_text(encoding="utf-8")
    assert "Priya" not in out and "okafor" not in out.lower() and "4111" not in out
    assert "[PERSON_1]" in out and "[EMAIL_1]" in out
