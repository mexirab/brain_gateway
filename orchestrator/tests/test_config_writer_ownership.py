"""
Tests for file ownership hand-off on atomic writes (config_writer.match_parent_owner).

The container runs as root, so mkstemp-based writes in the /app/data bind
mount used to land root:root 0600 — unreadable to the host's nightly backup.
match_parent_owner(fd, parent) fchowns the tmpfile to the parent dir's
owner before os.replace (no-op unless euid 0, never raises).

os.fchown / os.geteuid are monkeypatched — nothing is actually chowned.
Also covers is_first_boot() fail-closed, strict speaker validation, and the
auto_learn key-file ownership/mode.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def chown_spy(monkeypatch):
    """Pretend we're root and record fchown calls instead of performing them."""
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: calls.append((fd, uid, gid)))
    return calls


# ---------------------------------------------------------------------------
# match_parent_owner
# ---------------------------------------------------------------------------


def test_match_parent_owner_noop_when_not_root(monkeypatch, tmp_path):
    from orchestrator.config_writer import match_parent_owner

    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "fchown", lambda *a: calls.append(a))
    match_parent_owner(3, tmp_path)
    assert calls == []


def test_match_parent_owner_uses_parent_uid_gid(chown_spy, tmp_path):
    from orchestrator.config_writer import match_parent_owner

    st = os.stat(tmp_path)
    with open(tmp_path / "f", "w") as f:
        match_parent_owner(f.fileno(), tmp_path)
        assert chown_spy == [(f.fileno(), st.st_uid, st.st_gid)]


def test_match_parent_owner_swallows_oserror(monkeypatch, tmp_path):
    from orchestrator.config_writer import match_parent_owner

    def boom(*a):
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", boom)
    match_parent_owner(3, tmp_path)  # must not raise
    match_parent_owner(3, tmp_path / "does-not-exist")  # stat fails → swallowed


# ---------------------------------------------------------------------------
# Callers: fchown happens on the tmpfile fd BEFORE os.replace
# ---------------------------------------------------------------------------


def _order_spy(monkeypatch, module):
    """Record match_parent_owner and os.replace order within `module`."""
    events: list[tuple] = []
    real_replace = os.replace

    def fake_match(fd, parent):
        # fd must be a live, regular-file descriptor (the tmpfile).
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        events.append(("chown", Path(parent)))

    def spy_replace(src, dst):
        events.append(("replace", Path(src).name, Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(module, "match_parent_owner", fake_match)
    monkeypatch.setattr(os, "replace", spy_replace)
    return events


def test_atomic_write_yaml_chowns_before_replace(monkeypatch, tmp_path):
    from orchestrator import config_writer

    events = _order_spy(monkeypatch, config_writer)
    target = tmp_path / "x.yaml"
    config_writer.atomic_write_yaml(target, {"a": 1})

    assert [e[0] for e in events] == ["chown", "replace"]
    assert events[0][1] == tmp_path
    assert events[1][1].endswith(".tmp") and events[1][2] == target


def test_atomic_write_json_chowns_before_replace(monkeypatch, tmp_path):
    from orchestrator import routes_setup

    events = _order_spy(monkeypatch, routes_setup)
    target = tmp_path / "setup_state.json"
    routes_setup._atomic_write_json(str(target), {"setup_completed": True})

    assert [e[0] for e in events] == ["chown", "replace"]
    assert events[0][1] == tmp_path
    assert target.exists()


def test_setup_env_overrides_chowns_before_replace_and_is_0600(monkeypatch, tmp_path):
    from orchestrator import setup_env

    target = tmp_path / "setup_overrides.env"
    monkeypatch.setattr(setup_env, "_OVERRIDES_PATH", str(target))
    events = _order_spy(monkeypatch, setup_env)
    setup_env._atomic_write_overrides({"HA_URL": "http://ha.test:8123"})

    assert [e[0] for e in events] == ["chown", "replace"]
    assert events[0][1] == tmp_path
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert "HA_URL=" in target.read_text()


def test_atomic_write_yaml_real_chown_path_as_fake_root(chown_spy, tmp_path):
    """Integration: with the real match_parent_owner, fchown receives the parent's ids."""
    from orchestrator.config_writer import atomic_write_yaml

    atomic_write_yaml(tmp_path / "y.yaml", {"k": "v"})
    st = os.stat(tmp_path)
    assert len(chown_spy) == 1
    assert chown_spy[0][1:] == (st.st_uid, st.st_gid)


# ---------------------------------------------------------------------------
# auto_learn key creation
# ---------------------------------------------------------------------------


def test_auto_learn_key_creation_matches_owner_and_is_0600(monkeypatch, tmp_path):
    from orchestrator import auto_learn, config_writer, shared

    key_file = tmp_path / "sub" / "auto_learn.key"
    monkeypatch.setattr(auto_learn, "_KEY_FILE", str(key_file))
    monkeypatch.setattr(auto_learn, "_cipher", None)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPT", True, raising=False)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENCRYPTION_KEY", "", raising=False)

    calls: list[tuple[int, str]] = []

    def fake_match(fd, parent):
        # Mode is already restricted before any byte of the key is written.
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        calls.append((fd, str(parent)))

    monkeypatch.setattr(config_writer, "match_parent_owner", fake_match)

    cipher = auto_learn._get_cipher()

    assert cipher is not None
    assert len(calls) == 1 and calls[0][1] == str(key_file.parent)
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert len(key_file.read_bytes().strip()) == 44  # urlsafe-b64 Fernet key


# ---------------------------------------------------------------------------
# routes_setup.is_first_boot — fail closed on a corrupt state file
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_state(monkeypatch, tmp_path):
    from orchestrator import routes_setup

    path = tmp_path / "setup_state.json"
    monkeypatch.setattr(routes_setup, "_SETUP_STATE_PATH", str(path))
    return routes_setup, path


def test_first_boot_absent_file(setup_state):
    mod, _ = setup_state
    assert mod.is_first_boot() is True


def test_first_boot_completed(setup_state):
    mod, path = setup_state
    path.write_text('{"setup_completed": true}')
    assert mod.is_first_boot() is False


def test_first_boot_empty_object(setup_state):
    mod, path = setup_state
    path.write_text("{}")
    assert mod.is_first_boot() is True


@pytest.mark.parametrize("content", ["{garbage", "[1, 2]", ""])
def test_first_boot_corrupt_fails_closed(setup_state, content):
    mod, path = setup_state
    path.write_text(content)
    assert mod.is_first_boot() is False


# ---------------------------------------------------------------------------
# Strict speaker validation (Speakers panel + routines)
# ---------------------------------------------------------------------------

_GOOD = "media_player.a_1,media_player.b"
_BAD = [
    "media_player.x/../../config",
    "../config",
    "light.bedroom",
    "media_player.Kitchen",
    "MEDIA_PLAYER.kitchen",
    "media_player.",
    "media_player.a b",
    "media_player.a,light.b",
]


def test_announcement_routes_accepts_valid():
    from orchestrator.announcement_routes import _validate_speaker_string

    assert _validate_speaker_string(" media_player.a_1 , media_player.b ", "f") == _GOOD
    assert _validate_speaker_string("", "f") == ""


@pytest.mark.parametrize("bad", _BAD)
def test_announcement_routes_rejects_invalid(bad):
    from orchestrator.announcement_routes import _validate_speaker_string

    with pytest.raises(ValueError):
        _validate_speaker_string(bad, "routes.reminder")


def _routine(speaker):
    return {"routines": {"morning": {"speaker": speaker, "steps": [{"id": "x"}]}}}


def test_routines_accepts_valid_speaker():
    from orchestrator.routines_config import validate_routines

    validate_routines(_routine(_GOOD))
    validate_routines(_routine(""))


@pytest.mark.parametrize("bad", _BAD)
def test_routines_rejects_invalid_speaker(bad):
    from orchestrator.routines_config import validate_routines

    with pytest.raises(ValueError, match="speaker"):
        validate_routines(_routine(bad))
