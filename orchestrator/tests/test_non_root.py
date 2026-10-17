"""
Tests for the non-root orchestrator container (USER jess, uid 1000 default).

Covers the code-side consequences of dropping root:
- container-path defaults moved from /root/... to /home/jess/...
- auto_learn tolerates a key file it can read but not chmod (still root-owned
  from the root-container era)
- config_writer.match_parent_owner is a real no-op when not root

The host-side scripts/orchestrator_state_perms.sh needs docker compose, so it
is exercised by a separate host-run script, not here.
"""

from __future__ import annotations

import logging
import os

import pytest
from cryptography.fernet import Fernet

# ---------------------------------------------------------------------------
# Container-path defaults
# ---------------------------------------------------------------------------


def test_config_field_defaults_point_at_jess_home():
    from orchestrator.config import Settings

    fields = Settings.model_fields
    assert fields["model_ssh_key"].default == "/home/jess/.ssh/id_ed25519"
    assert fields["training_corpus_cc_dir"].default.startswith("/home/jess/.claude/projects/")


def test_settings_instance_defaults_without_env(monkeypatch):
    """With the env vars unset, a fresh Settings resolves to /home/jess paths."""
    from orchestrator.config import Settings

    for var in ("MODEL_SSH_KEY", "TRAINING_CORPUS_CC_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.model_ssh_key == "/home/jess/.ssh/id_ed25519"
    assert s.training_corpus_cc_dir.startswith("/home/jess/")
    assert "/root" not in s.training_corpus_cc_dir


def test_no_settings_default_mentions_root_home():
    """Guard: no Settings string default should still point at /root/."""
    from orchestrator.config import Settings

    offenders = [
        name
        for name, f in Settings.model_fields.items()
        if isinstance(f.default, str) and f.default.startswith("/root/")
    ]
    assert offenders == []


def test_claude_projects_root_default_when_env_unset(monkeypatch):
    from orchestrator import claude_code_tracker

    monkeypatch.delenv("CLAUDE_PROJECTS_PATH", raising=False)
    assert claude_code_tracker._claude_projects_root() == "/home/jess/.claude/projects"


def test_claude_projects_root_env_override(monkeypatch, tmp_path):
    from orchestrator import claude_code_tracker

    monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
    assert claude_code_tracker._claude_projects_root() == str(tmp_path)


# ---------------------------------------------------------------------------
# auto_learn key loading under a non-owner uid
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_cipher(monkeypatch, tmp_path):
    """Point auto_learn at a tmp key file with encryption on and no env key."""
    from orchestrator import auto_learn, shared

    key_file = tmp_path / "auto_learn.key"
    monkeypatch.setattr(auto_learn, "_KEY_FILE", str(key_file))
    monkeypatch.setattr(auto_learn, "_cipher", None)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPT", True)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPTION_KEY", "")
    yield auto_learn, key_file
    # monkeypatch restores _cipher=None afterwards, so the cached cipher
    # built from the tmp key never leaks into other tests.


def test_existing_key_chmod_permission_error_still_loads(fresh_cipher, monkeypatch, caplog):
    auto_learn, key_file = fresh_cipher
    key = Fernet.generate_key()
    key_file.write_bytes(key + b"\n")

    def deny(*a, **kw):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "chmod", deny)
    with caplog.at_level(logging.WARNING, logger="orchestrator.auto_learn"):
        cipher = auto_learn._get_cipher()

    assert cipher is not None
    # The cipher is the one from the file (not a freshly generated key).
    token = Fernet(key).encrypt(b"probe")
    assert cipher.decrypt(token) == b"probe"
    assert any(r.levelno == logging.WARNING and "chmod" in r.getMessage() for r in caplog.records), (
        "expected a WARNING about the chmod failure"
    )
    # The key file must be untouched (no regeneration on chmod failure).
    assert key_file.read_bytes().strip() == key


def test_existing_key_chmod_ok_no_warning(fresh_cipher, caplog):
    auto_learn, key_file = fresh_cipher
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    key_file.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="orchestrator.auto_learn"):
        cipher = auto_learn._get_cipher()

    assert cipher.decrypt(Fernet(key).encrypt(b"x")) == b"x"
    assert oct(key_file.stat().st_mode & 0o777) == oct(0o600)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_existing_key_other_oserror_still_propagates(fresh_cipher, monkeypatch):
    """Only PermissionError is tolerated; other OSErrors are not swallowed."""
    auto_learn, key_file = fresh_cipher
    key_file.write_bytes(Fernet.generate_key())

    def eio(*a, **kw):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(os, "chmod", eio)
    with pytest.raises(OSError):
        auto_learn._get_cipher()


def test_missing_key_generated_and_roundtrips(fresh_cipher):
    auto_learn, key_file = fresh_cipher
    assert not key_file.exists()
    cipher = auto_learn._get_cipher()
    assert key_file.exists()
    assert oct(key_file.stat().st_mode & 0o777) == oct(0o600)
    assert Fernet(key_file.read_bytes().strip()).decrypt(cipher.encrypt(b"y")) == b"y"


def test_cipher_is_cached(fresh_cipher):
    auto_learn, _ = fresh_cipher
    assert auto_learn._get_cipher() is auto_learn._get_cipher()


# ---------------------------------------------------------------------------
# match_parent_owner — real (unpatched) no-op when not root
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="only meaningful in the non-root image")
def test_match_parent_owner_real_noop_as_non_root(tmp_path):
    from orchestrator.config_writer import match_parent_owner

    f = tmp_path / "f"
    with open(f, "w") as fh:
        match_parent_owner(fh.fileno(), tmp_path)  # must not raise
    st = f.stat()
    assert (st.st_uid, st.st_gid) == (os.geteuid(), os.getegid())


@pytest.mark.skipif(os.geteuid() == 0, reason="only meaningful in the non-root image")
def test_match_parent_owner_noop_even_if_parent_owned_by_other(monkeypatch, tmp_path):
    """Non-root never calls fchown, even when the parent's owner differs."""
    from orchestrator import config_writer

    calls = []
    monkeypatch.setattr(os, "fchown", lambda *a: calls.append(a))
    fake_stat = os.stat_result((0o40755, 0, 0, 1, 4242, 4242, 0, 0, 0, 0))
    real_stat = os.stat
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **kw: fake_stat if str(p) == str(tmp_path) else real_stat(p, *a, **kw)
    )
    with open(tmp_path / "g", "w") as fh:
        config_writer.match_parent_owner(fh.fileno(), tmp_path)
    assert calls == []
