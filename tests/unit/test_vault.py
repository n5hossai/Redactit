"""Vault: HMAC-indexed, AES-GCM-encrypted pseudonym mapping store."""

from __future__ import annotations

import keyring
import keyring.backend
import keyring.errors
import pytest

from redactit.vault import Vault

KEY = b"\x01" * 32


class _FakeBackend(keyring.backend.KeyringBackend):
    """Tiny in-memory keyring backend, just enough to exercise Vault.open."""

    priority = 1

    def __init__(self):
        super().__init__()
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, password):
        self._store[(service, username)] = password


class _BrokenBackend(keyring.backend.KeyringBackend):
    """Simulates "no usable keyring backend" so Vault.open must fail closed."""

    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend available")

    def set_password(self, service, username, password):
        raise keyring.errors.NoKeyringError("no backend available")


@pytest.fixture
def restore_keyring():
    original = keyring.get_keyring()
    yield
    keyring.set_keyring(original)


def test_number_for_dedups_normalised_value_and_counts_per_type(tmp_path):
    vault = Vault(tmp_path / "vault.db", KEY)
    assert vault.number_for("scope1", "PERSON", "Priya Okafor") == 1
    assert vault.number_for("scope1", "PERSON", "  priya   OKAFOR ") == 1
    assert vault.number_for("scope1", "PERSON", "Jordan Page") == 2
    assert vault.number_for("scope1", "EMAIL", "priya@example.com") == 1


def test_different_scopes_are_independent(tmp_path):
    vault = Vault(tmp_path / "vault.db", KEY)
    assert vault.number_for("scope-a", "PERSON", "Priya Okafor") == 1
    assert vault.number_for("scope-b", "PERSON", "Priya Okafor") == 1
    assert vault.value_for("scope-a", "PERSON", 1) == "Priya Okafor"
    assert vault.value_for("scope-b", "PERSON", 1) == "Priya Okafor"


def test_value_for_round_trips_the_first_seen_casing(tmp_path):
    vault = Vault(tmp_path / "vault.db", KEY)
    vault.number_for("scope1", "PERSON", "Priya Okafor")
    assert vault.value_for("scope1", "PERSON", 1) == "Priya Okafor"


def test_value_for_missing_entry_returns_none(tmp_path):
    vault = Vault(tmp_path / "vault.db", KEY)
    assert vault.value_for("scope1", "PERSON", 99) is None


def test_purge_removes_only_expired_entries(tmp_path):
    vault = Vault(tmp_path / "vault.db", KEY)
    vault.number_for("scope1", "PERSON", "Priya Okafor")  # fresh
    vault.number_for("scope1", "PERSON", "Jordan Page")  # will be backdated
    vault._conn.execute("UPDATE entries SET created_at = 0 WHERE n = 2")
    vault._conn.commit()

    removed = vault.purge(retention_days=30)  # backdated entry is epoch 0, fresh one is "now"

    assert removed == 1
    assert vault.value_for("scope1", "PERSON", 1) == "Priya Okafor"
    assert vault.value_for("scope1", "PERSON", 2) is None


def test_purge_now_accepts_a_datetime_and_keeps_recent_entries(tmp_path):
    from datetime import datetime, timezone

    vault = Vault(tmp_path / "vault.db", KEY)
    vault.number_for("scope1", "PERSON", "Priya Okafor")

    removed = vault.purge(retention_days=30, now=datetime.now(timezone.utc))

    assert removed == 0
    assert vault.value_for("scope1", "PERSON", 1) == "Priya Okafor"


def test_db_file_contains_no_plaintext_value_or_scope(tmp_path):
    path = tmp_path / "vault.db"
    vault = Vault(path, KEY)
    vault.number_for("my-secret-chat-scope", "PERSON", "Priya Okafor")

    raw = path.read_bytes()
    assert b"my-secret-chat-scope" not in raw
    assert b"Priya Okafor" not in raw
    assert b"priya okafor" not in raw.lower()


def test_open_creates_and_reuses_a_key_via_the_keyring(tmp_path, restore_keyring):
    keyring.set_keyring(_FakeBackend())
    vault = Vault.open(tmp_path / "vault.db")
    n = vault.number_for("scope1", "PERSON", "Priya Okafor")

    # A second open() must read back the same key, not mint a new one.
    vault2 = Vault.open(tmp_path / "vault.db")
    assert vault2.value_for("scope1", "PERSON", n) == "Priya Okafor"


def test_open_fails_closed_with_no_usable_backend(tmp_path, restore_keyring):
    keyring.set_keyring(_BrokenBackend())
    with pytest.raises(RuntimeError):
        Vault.open(tmp_path / "vault.db")


def test_hidden_characters_do_not_split_one_person_into_two(tmp_path):
    v = Vault(tmp_path / "v.db", KEY)
    assert v.number_for("chat", "PERSON", "Pri­ya Okafor") == v.number_for("chat", "PERSON", "Priya​ Okafor")


def test_a_ciphertext_moved_to_another_row_fails_to_decrypt(tmp_path):
    from cryptography.exceptions import InvalidTag

    v = Vault(tmp_path / "v.db", KEY)
    v.number_for("chat", "PERSON", "Priya Okafor")
    v.number_for("chat", "PERSON", "Dmitri Volkov")
    first = v._conn.execute("SELECT ciphertext FROM entries WHERE n=1").fetchone()[0]
    v._conn.execute("UPDATE entries SET ciphertext=? WHERE n=2", (first,))
    with pytest.raises(InvalidTag):
        v.value_for("chat", "PERSON", 2)
