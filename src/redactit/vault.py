"""Encrypted pseudonym-mapping store. Never writes a plaintext value or scope to disk
(docs/THREAT_MODEL.md T12): only keyed HMAC digests and AES-256-GCM ciphertext land in
the SQLite file, and the key itself lives only in the OS keychain.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import keyring
import keyring.errors
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from redactit.types import RedactitError

_SERVICE = "redactit"
_USER = "vault-key"


class VaultError(RedactitError, RuntimeError):
    pass


def _subkey(key: bytes, purpose: bytes) -> bytes:
    # Separate keys for encryption and lookup digests, so neither use can weaken the other.
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"redactit vault " + purpose).derive(key)


def _normalise(value: str) -> str:
    """One identity per value: case, spacing, Unicode variants and hidden characters
    (soft hyphens, zero-width spaces) must not split one person into two pseudonyms."""
    visible = "".join(c for c in unicodedata.normalize("NFKC", value) if unicodedata.category(c) != "Cf")
    return " ".join(visible.casefold().split())


class Vault:
    """SQLite-backed store of scope -> entity -> counter -> encrypted value."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) != 32:
            raise VaultError("vault key must be 32 bytes")
        self._aes = AESGCM(_subkey(key, b"aes"))
        self._mac = _subkey(key, b"hmac")
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)  # user-only: the vault never trusts other local processes
        # check_same_thread=False: the native host reads messages on its own thread.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                scope_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                n INTEGER NOT NULL,
                lookup TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (scope_id, entity_type, lookup)
            )"""
        )
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_n ON entries(scope_id, entity_type, n)")
        self._conn.commit()

    @classmethod
    def open(cls, path: Path) -> "Vault":
        """Read (or create) the 32-byte key in the OS keychain. Fails closed: never
        falls back to a key on disk if no keyring backend is usable."""
        try:
            if keyring.get_password(_SERVICE, _USER) is None:
                keyring.set_password(_SERVICE, _USER, os.urandom(32).hex())
            # Read back rather than keep our own copy: if two processes created a key at
            # once, both must end up using whichever one the keychain kept.
            key = bytes.fromhex(keyring.get_password(_SERVICE, _USER))
        except keyring.errors.KeyringError as e:
            raise VaultError("no usable OS keychain found; refusing to store the vault key on disk") from e
        return cls(path, key)

    def _mac_of(self, msg: str) -> str:
        return hmac.new(self._mac, msg.encode("utf-8"), hashlib.sha256).hexdigest()

    def number_for(self, scope: str, entity_type: str, value: str) -> int:
        """Get or create the counter for this (scope, type, normalised value)."""
        scope_id = self._mac_of(scope)
        lookup = self._mac_of(f"{scope}|{entity_type}|{_normalise(value)}")
        row = self._conn.execute(
            "SELECT n FROM entries WHERE scope_id=? AND entity_type=? AND lookup=?", (scope_id, entity_type, lookup)
        ).fetchone()
        if row is not None:
            return row[0]
        max_n = self._conn.execute(
            "SELECT MAX(n) FROM entries WHERE scope_id=? AND entity_type=?", (scope_id, entity_type)
        ).fetchone()[0]
        n = (max_n or 0) + 1
        nonce = os.urandom(12)
        ciphertext = nonce + self._aes.encrypt(nonce, value.encode("utf-8"), _row_id(scope_id, entity_type, n))
        self._conn.execute(
            "INSERT INTO entries (scope_id, entity_type, n, lookup, ciphertext, created_at) VALUES (?,?,?,?,?,?)",
            (scope_id, entity_type, n, lookup, ciphertext, time.time()),
        )
        self._conn.commit()
        return n

    def value_for(self, scope: str, entity_type: str, n: int) -> str | None:
        scope_id = self._mac_of(scope)
        row = self._conn.execute(
            "SELECT ciphertext FROM entries WHERE scope_id=? AND entity_type=? AND n=?", (scope_id, entity_type, n)
        ).fetchone()
        if row is None:
            return None
        nonce, ct = row[0][:12], row[0][12:]
        return self._aes.decrypt(nonce, ct, _row_id(scope_id, entity_type, n)).decode("utf-8")

    def purge(self, retention_days: int, now: float | datetime | None = None) -> int:
        """Delete entries older than `retention_days`; returns the number removed."""
        now_ts = now.timestamp() if isinstance(now, datetime) else (time.time() if now is None else float(now))
        cur = self._conn.execute("DELETE FROM entries WHERE created_at < ?", (now_ts - retention_days * 86400,))
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _row_id(scope_id: str, entity_type: str, n: int) -> bytes:
    # Associated data binds each ciphertext to its row: one swapped into another row fails.
    return f"{scope_id}|{entity_type}|{n}".encode()
