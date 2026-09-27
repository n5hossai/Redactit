"""The engine's own network block, independent of the test suite's socket guard."""

import socket

import pytest
from redactit import safety


def test_block_network_refuses_ip_sockets_and_dns(monkeypatch):
    monkeypatch.setattr(socket, "socket", socket.socket)  # restored after the test
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    safety.block_network()
    with pytest.raises(safety.NetworkBlocked):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(safety.NetworkBlocked):
        socket.getaddrinfo("example.com", 443)
