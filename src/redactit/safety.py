"""Runtime guards: the engine process cannot open network connections."""

import logging
import socket

_NETWORK_FAMILIES = {socket.AF_INET, socket.AF_INET6}


class NetworkBlocked(RuntimeError):
    pass


class _GuardedSocket(socket.socket):
    def __init__(self, family=socket.AF_INET, *args, **kwargs):
        # Unix sockets stay allowed: the Linux keychain talks to D-Bus over one.
        if family in _NETWORK_FAMILIES:
            raise NetworkBlocked("Redactit's engine never uses the network")
        super().__init__(family, *args, **kwargs)


def _no_dns(*_args, **_kwargs):
    raise NetworkBlocked("Redactit's engine never resolves hostnames")


def block_network() -> None:
    """Refuse IP sockets and DNS for the rest of the process.

    Defence in depth behind "no network code": a dependency that tries to phone home
    (a telemetry hook, a suffix-list refresh) fails loudly instead of sending data.
    """
    socket.socket = _GuardedSocket
    socket.getaddrinfo = _no_dns
    # Libraries that log at DEBUG may include the text they analysed.
    logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)
