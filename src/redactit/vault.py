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
from datetime import datetime
from pathlib import Path

import keyring
import keyring.errors
import platformdirs
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_SERVICE = "redactit"
_USER = "vault-key"


def _hmac(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalise(value: str) -> str:
    return " ".join(value.casefold().split())


class Vault:
    """SQLite-backed store of scope -> entity -> counter -> encrypted value."""

    def __init__(self, path: Path, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("vault key must be 32 bytes")
        self._key = key
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(path.parent, 0o700)  # user-only: the vault never trusts other local processes
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
    def open(cls, path: Path | None = None) -> "Vault":
        """Read (or create) the 32-byte key in the OS keychain. Fails closed: never
        falls back to a key on disk if no keyring backend is usable."""
        if path is None:
            path = Path(platformdirs.user_data_dir(_SERVICE)) / "vault.db"
        try:
            stored = keyring.get_password(_SERVICE, _USER)
            if stored is None:
                key = os.urandom(32)
                keyring.set_password(_SERVICE, _USER, key.hex())
            else:
                key = bytes.fromhex(stored)
        except keyring.errors.KeyringError as e:
            raise RuntimeError(
                "no usable OS keychain backend found; refusing to store the vault key on disk"
            ) from e
        return cls(path, key)

    def number_for(self, scope: str, entity_type: str, value: str) -> int:
        """Get or create the counter for this (scope, type, normalised value)."""
        scope_id = _hmac(self._key, scope)
        lookup = _hmac(self._key, f"{scope}|{entity_type}|{_normalise(value)}")
        row = self._conn.execute(
            "SELECT n FROM entries WHERE scope_id=? AND entity_type=? AND lookup=?",
            (scope_id, entity_type, lookup),
        ).fetchone()
        if row is not None:
            return row[0]

        max_n = self._conn.execute(
            "SELECT MAX(n) FROM entries WHERE scope_id=? AND entity_type=?",
            (scope_id, entity_type),
        ).fetchone()[0]
        n = (max_n or 0) + 1

        nonce = os.urandom(12)
        ciphertext = nonce + AESGCM(self._key).encrypt(nonce, value.encode("utf-8"), None)
        self._conn.execute(
            "INSERT INTO entries (scope_id, entity_type, n, lookup, ciphertext, created_at) VALUES (?,?,?,?,?,?)",
            (scope_id, entity_type, n, lookup, ciphertext, time.time()),
        )
        self._conn.commit()
        return n

    def value_for(self, scope: str, entity_type: str, n: int) -> str | None:
        scope_id = _hmac(self._key, scope)
        row = self._conn.execute(
            "SELECT ciphertext FROM entries WHERE scope_id=? AND entity_type=? AND n=?",
            (scope_id, entity_type, n),
        ).fetchone()
        if row is None:
            return None
        nonce, ct = row[0][:12], row[0][12:]
        return AESGCM(self._key).decrypt(nonce, ct, None).decode("utf-8")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Vault":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def purge(self, retention_days: int, now: float | datetime | None = None) -> int:
        """Delete entries older than `retention_days`; returns the number removed."""
        if now is None:
            now_ts = time.time()
        elif isinstance(now, datetime):
            now_ts = now.timestamp()
        else:
            now_ts = float(now)
        cutoff = now_ts - retention_days * 86400
        cur = self._conn.execute("DELETE FROM entries WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cur.rowcount
