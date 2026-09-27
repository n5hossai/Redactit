"""The engine handles sensitive input and must never phone home. These tests
prove the pytest-socket guard (see pyproject.toml addopts) is actually wired
up, so any future code path that opens a socket fails the suite instead of
silently sending redacted-away data somewhere.
"""

import socket

import pytest
from pytest_socket import SocketBlockedError

from redactit.cli import main


def test_socket_guard_is_active():
    """Canary: if --disable-socket were ever dropped from addopts, this is
    the test that would notice -- every other offline claim depends on it."""
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_cli_version_runs_fully_offline():
    assert main(["--version"]) == 0
