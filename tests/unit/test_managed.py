"""The admin policy's location cannot be redirected, and a user-owned file is never trusted as one."""

import pytest
from redactit.managed import assert_admin_owned, managed_policy_path
from redactit.policy import PolicyError


def test_environment_cannot_move_the_admin_policy(monkeypatch, tmp_path):
    before = managed_policy_path()
    monkeypatch.setenv("WIN_PD_OVERRIDE_COMMON_APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path))
    monkeypatch.setenv("ProgramData", str(tmp_path))
    assert managed_policy_path() == before


def test_a_policy_file_the_user_owns_is_refused(tmp_path):
    forged = tmp_path / "policy.yaml"
    forged.write_text("dial: {admin_floor: 1}\n", encoding="utf-8")
    with pytest.raises(PolicyError):
        assert_admin_owned(forged)
