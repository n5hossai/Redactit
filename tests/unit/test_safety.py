"""The engine's own network block, independent of the test suite's socket guard."""

import socket

import pytest
from redactit import safety


@pytest.fixture
def blocked(monkeypatch):
    for name in ("socket", *safety._DNS_CALLS):
        monkeypatch.setattr(socket, name, getattr(socket, name))  # restored after the test
    safety.block_network()


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6, -1], ids=["ipv4", "ipv6", "auto"])
def test_every_non_unix_socket_is_refused(blocked, family):
    with pytest.raises(safety.NetworkBlocked):
        socket.socket(family, socket.SOCK_STREAM)


@pytest.mark.parametrize("call", ["getaddrinfo", "gethostbyname", "gethostbyname_ex", "getnameinfo"])
def test_every_dns_lookup_is_refused(blocked, call):
    args = (("93.184.216.34", 443), 0) if call == "getnameinfo" else ("example.com",)
    with pytest.raises(safety.NetworkBlocked):
        getattr(socket, call)(*args)
