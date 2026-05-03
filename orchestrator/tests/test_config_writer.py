"""
Tests for orchestrator/config_writer.py.

Covers:
1. atomic_write_yaml happy path — file content matches input dict.
2. Atomicity — os.replace failure leaves original file intact AND tmpfile cleaned up.
3. Recursive serialization — nested dicts/lists round-trip via yaml.safe_load.
4. _redact recursively masks credential-shaped keys (case-insensitive) in dicts
   and list-of-dicts; preserves unrelated keys.
5. log_config_change inserts a row into config_changes with the right panel
   and redacted diff; failure mode: get_db raising must NOT raise upward.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# atomic_write_yaml
# ---------------------------------------------------------------------------


def test_atomic_write_yaml_happy_path(tmp_path: Path):
    from orchestrator.config_writer import atomic_write_yaml

    target = tmp_path / "out.yaml"
    payload = {"a": 1, "b": "two", "c": [1, 2, 3]}
    atomic_write_yaml(target, payload)

    assert target.exists()
    with open(target) as f:
        loaded = yaml.safe_load(f)
    assert loaded == payload


def test_atomic_write_yaml_creates_parent_dirs(tmp_path: Path):
    from orchestrator.config_writer import atomic_write_yaml

    target = tmp_path / "nested" / "deep" / "out.yaml"
    atomic_write_yaml(target, {"x": 1})
    assert target.exists()


def test_atomic_write_yaml_failure_leaves_original_intact_and_cleans_tmp(tmp_path: Path):
    """If os.replace raises, the original file must be untouched and the
    tempfile must be removed (no `.out.yaml.<...>.tmp` litter)."""
    from orchestrator import config_writer

    target = tmp_path / "out.yaml"
    # Seed an existing file we don't want clobbered.
    original_content = {"original": "value"}
    with open(target, "w") as f:
        yaml.safe_dump(original_content, f)

    def _boom(*_args, **_kwargs):
        raise OSError("simulated replace failure")

    with patch.object(config_writer.os, "replace", side_effect=_boom):
        with pytest.raises(OSError, match="simulated replace failure"):
            config_writer.atomic_write_yaml(target, {"new": "data"})

    # Original file unchanged.
    with open(target) as f:
        assert yaml.safe_load(f) == original_content

    # No tmpfile siblings left behind.
    siblings = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    leftover_tmps = [s for s in siblings if s.startswith(".out.yaml.") and s.endswith(".tmp")]
    assert leftover_tmps == [], f"leftover tempfiles: {leftover_tmps}"


def test_atomic_write_yaml_round_trip_nested_structures(tmp_path: Path):
    """Nested dicts and lists round-trip cleanly."""
    from orchestrator.config_writer import atomic_write_yaml

    target = tmp_path / "complex.yaml"
    payload = {
        "categories": {
            "water": {
                "enabled": True,
                "interval_minutes": 90,
                "active_hours": {"start": "09:00", "end": "21:00"},
            },
            "meds": {
                "enabled": True,
                "times": ["08:00", "20:00"],
            },
        },
        "quiet_hours": {
            "start": "22:00",
            "end": "07:00",
            "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        },
        "list_of_dicts": [{"a": 1}, {"b": [2, 3]}],
    }
    atomic_write_yaml(target, payload)

    with open(target) as f:
        loaded = yaml.safe_load(f)
    assert loaded == payload


# ---------------------------------------------------------------------------
# _redact
# ---------------------------------------------------------------------------


def test_redact_masks_credential_keys_case_insensitive():
    from orchestrator.config_writer import _redact

    inp = {
        "Token": "abc",
        "API_KEY": "xyz",
        "secret": "shh",
        "Password": "p",
        "Auth": "bearer-thing",
        "user_name": "Nadim",
        "count": 5,
    }
    out = _redact(inp)
    assert out["Token"] == "***REDACTED***"
    assert out["API_KEY"] == "***REDACTED***"
    assert out["secret"] == "***REDACTED***"
    assert out["Password"] == "***REDACTED***"
    assert out["Auth"] == "***REDACTED***"
    # Unrelated keys preserved.
    assert out["user_name"] == "Nadim"
    assert out["count"] == 5


def test_redact_handles_nested_and_list_of_dicts():
    from orchestrator.config_writer import _redact

    inp = {
        "outer": {
            "api_key": "secret-key",
            "nested": {"password": "p", "ok_key": "ok"},
        },
        "list": [
            {"token": "t1", "name": "first"},
            {"name": "second", "secret": "s2"},
        ],
        "scalar_list": [1, 2, 3],
    }
    out = _redact(inp)
    assert out["outer"]["api_key"] == "***REDACTED***"
    assert out["outer"]["nested"]["password"] == "***REDACTED***"
    assert out["outer"]["nested"]["ok_key"] == "ok"
    assert out["list"][0]["token"] == "***REDACTED***"
    assert out["list"][0]["name"] == "first"
    assert out["list"][1]["secret"] == "***REDACTED***"
    assert out["list"][1]["name"] == "second"
    assert out["scalar_list"] == [1, 2, 3]


def test_redact_passes_through_scalars():
    from orchestrator.config_writer import _redact

    assert _redact("plain") == "plain"
    assert _redact(42) == 42
    assert _redact(None) is None
    assert _redact(True) is True


# ---------------------------------------------------------------------------
# log_config_change
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_state_db(tmp_path, monkeypatch):
    """Point state_store.DB_PATH at a fresh DB and init schema."""
    from orchestrator import state_store

    db_path = str(tmp_path / "config_changes.db")
    monkeypatch.setattr(state_store, "DB_PATH", db_path)
    state_store.init_db()
    return db_path


def test_log_config_change_inserts_row_with_redacted_diff(isolated_state_db):
    from orchestrator import state_store
    from orchestrator.config_writer import log_config_change

    before = {"api_key": "OLDSECRET", "tone": "warm"}
    after = {"api_key": "NEWSECRET", "tone": "direct"}
    log_config_change("identity", before, after, changed_by="test")

    with state_store.get_db() as conn:
        rows = conn.execute(
            "SELECT panel, diff_json, changed_by FROM config_changes WHERE panel = 'identity'"
        ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["panel"] == "identity"
    assert row["changed_by"] == "test"

    diff = json.loads(row["diff_json"])
    # Redacted keys masked.
    assert diff["before"]["api_key"] == "***REDACTED***"
    assert diff["after"]["api_key"] == "***REDACTED***"
    # Unrelated keys preserved.
    assert diff["before"]["tone"] == "warm"
    assert diff["after"]["tone"] == "direct"


def test_log_config_change_swallows_db_failures(monkeypatch, caplog):
    """If get_db raises, log_config_change must log a warning but NOT raise.
    Audit drift must never break the user's settings save."""
    from orchestrator import config_writer

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated DB explosion")

    # Patch the symbol that config_writer imports (lazy import inside the func)
    monkeypatch.setattr("orchestrator.state_store.get_db", _boom)

    # Must NOT raise.
    with caplog.at_level("WARNING"):
        config_writer.log_config_change("identity", {"a": 1}, {"a": 2})

    assert any("Failed to log audit row" in rec.message for rec in caplog.records)
