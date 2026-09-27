"""Runtime guards: the engine process cannot open network connections."""

import logging
import socket

_AF_UNIX = getattr(socket, "AF_UNIX", None)
_DNS_CALLS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo")


class NetworkBlocked(RuntimeError):
    pass


class _GuardedSocket(socket.socket):
    def __init__(self, family=-1, *args, **kwargs):
        # Allow-list, not block-list: family -1 (auto-detect) and any future family would
        # otherwise slip through. Unix sockets stay open for the Linux keychain's D-Bus.
        if family != _AF_UNIX or _AF_UNIX is None:
            raise NetworkBlocked("Redactit's engine never uses the network")
        super().__init__(family, *args, **kwargs)


def _no_dns(*_args, **_kwargs):
    raise NetworkBlocked("Redactit's engine never resolves hostnames")


def block_network() -> None:
    """Refuse every non-Unix socket and every DNS lookup for the rest of the process.

    Defence in depth behind "no network code": a dependency that tries to phone home
    (a telemetry hook, a suffix-list refresh) fails loudly instead of sending data.
    Idempotent; `Engine` calls it, so every interface and the leak test run behind it.
    """
    socket.socket = _GuardedSocket
    for name in _DNS_CALLS:
        setattr(socket, name, _no_dns)
    # Libraries that log at DEBUG may include the text they analysed.
    logging.getLogger("presidio-analyzer").setLevel(logging.WARNING)
