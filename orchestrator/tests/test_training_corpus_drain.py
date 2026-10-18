"""Tests for orchestrator/jobs_training_corpus.py drain pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator.jobs_training_corpus import (
    _fingerprint,
    drain_cc_sessions,
    drain_state_store,
    run_drain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE chat_messages ("
        "id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT, "
        "routing TEXT, announcement_type TEXT, created_at TEXT)"
    )
    for conv_id, role, content, created_at in rows:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, created_at),
        )
    conn.commit()
    conn.close()


def _make_cc_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# drain_state_store
# ---------------------------------------------------------------------------


class TestDrainStateStore:
    def test_missing_db_yields_nothing(self, tmp_path):
        assert list(drain_state_store(tmp_path / "nope.db")) == []

    def test_extracts_messages(self, tmp_path):
        db = tmp_path / "state.db"
        _make_state_db(
            db,
            [
                ("conv1", "user", "test question", "2026-04-15T10:00:00"),
                ("conv1", "assistant", "test answer", "2026-04-15T10:00:01"),
                ("conv1", "tool", "should be skipped", "2026-04-15T10:00:02"),
            ],
        )
        records = list(drain_state_store(db))
        assert len(records) == 2
        assert {r.role for r in records} == {"user", "assistant"}


# ---------------------------------------------------------------------------
# drain_cc_sessions
# ---------------------------------------------------------------------------


class TestDrainCcSessions:
    def test_missing_dir_yields_nothing(self, tmp_path):
        assert list(drain_cc_sessions(tmp_path / "nope")) == []

    def test_extracts_user_turns_skips_tool_results(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(
            cc / "session1.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": "a real question from the user"}},
                # Tool result — should be skipped
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "stdout blob"}],
                    },
                },
                # Assistant turn — should be skipped (cc drain is user-only)
                {"type": "assistant", "message": {"role": "assistant", "content": "reply"}},
                # Short turn — dropped (under MIN_LEN=2)
                {"type": "user", "message": {"role": "user", "content": "x"}},
            ],
        )
        records = list(drain_cc_sessions(cc))
        assert len(records) == 1
        assert records[0].role == "user"
        assert records[0].source == "cc_session"
        assert "real question" in records[0].text

    def test_drops_specific_noise_prefixes_not_all_brackets(self, tmp_path):
        """System-reminder and command-output noise is dropped, but legitimate
        bracketed prose is kept — tightening of the prior over-broad filter."""
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                # Dropped: harness noise dumps
                {"type": "user", "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
                {"type": "user", "message": {"role": "user", "content": "<command-name>/foo</command-name>"}},
                {"type": "user", "message": {"role": "user", "content": "<bash-stdout>output</bash-stdout>"}},
                # Kept: legitimate user prose that happens to start with a bracket
                {"type": "user", "message": {"role": "user", "content": "[urgent] remind me to send the draft"}},
                {"type": "user", "message": {"role": "user", "content": "<Component> element rendering question"}},
            ],
        )
        records = list(drain_cc_sessions(cc))
        assert len(records) == 2
        assert all("remind me" in r.text or "element rendering" in r.text for r in records)

    def test_skips_oversize_jsonl_lines(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        fp = cc / "s.jsonl"
        # One good line, one oversized line (>2 MB), one good line
        good1 = json.dumps({"type": "user", "message": {"role": "user", "content": "first question"}})
        huge = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 3_000_000}})
        good2 = json.dumps({"type": "user", "message": {"role": "user", "content": "third question"}})
        fp.write_text(good1 + "\n" + huge + "\n" + good2 + "\n")
        records = list(drain_cc_sessions(cc))
        texts = {r.text for r in records}
        assert "first question" in texts
        assert "third question" in texts
        assert not any("x" * 100 in r.text for r in records)

    def test_skips_malformed_json_line(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        fp = cc / "s.jsonl"
        good = json.dumps({"type": "user", "message": {"role": "user", "content": "valid question"}})
        fp.write_text(good + "\nnot json at all\n" + good + "\n")
        # Two valid records, dedup collapses to 1
        records = list(drain_cc_sessions(cc))
        assert len(records) == 2  # drain is a raw iterator — run_drain does dedup


class TestSecretFilter:
    """Secrets must never reach the JSONL. Drop the whole record on match."""

    def test_openai_key_dropped(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "here's the key: sk-abcdefghijklmnopqrstuvwxyz1234567890"},
                },
                {"type": "user", "message": {"role": "user", "content": "clean question about the weather"}},
            ],
        )
        records = list(drain_cc_sessions(cc))
        assert len(records) == 1
        assert "weather" in records[0].text

    def test_private_key_block_dropped(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg..."},
                },
            ],
        )
        assert list(drain_cc_sessions(cc)) == []

    def test_fernet_ciphertext_dropped(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "gAAAAABp1isCb4iFivPOd1nFVdlQLPaDosUURSOvLOyf5OS1kkmOaTqsA0CauO35RzKAmntr",
                    },
                },
            ],
        )
        assert list(drain_cc_sessions(cc)) == []

    def test_bearer_token_dropped_in_state_store(self, tmp_path):
        db = tmp_path / "state.db"
        _make_state_db(
            db,
            [
                ("c1", "user", "Authorization: Bearer eyJhbGc1234567890abcdefghij.foobar.baz", "2026-04-15T10:00:00"),
                ("c1", "user", "actually fine message", "2026-04-15T10:00:01"),
            ],
        )
        records = list(drain_state_store(db))
        assert len(records) == 1
        assert "fine message" in records[0].text


class TestSameTextCrossSource:
    """Same text in two sources stays as two records — source is in the hash."""

    def test_cross_source_not_deduped(self, tmp_path):
        out = tmp_path / "training_corpus"
        state = tmp_path / "state.db"
        cc = tmp_path / "cc"
        cc.mkdir()

        _make_state_db(
            state,
            [
                ("conv1", "user", "same exact question", "2026-04-15T10:00:00"),
            ],
        )
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": "same exact question"}},
            ],
        )

        counts = run_drain(out_dir=out, state_db=state, cc_dir=cc)
        assert counts["state_store"] == 1
        assert counts["cc_session"] == 1


# ---------------------------------------------------------------------------
# run_drain — end-to-end + idempotency
# ---------------------------------------------------------------------------


class TestRunDrain:
    def _setup(self, tmp_path):
        state = tmp_path / "state.db"
        cc = tmp_path / "cc"
        out = tmp_path / "training_corpus"
        cc.mkdir()

        _make_state_db(
            state,
            [
                ("ss1", "user", "state store question", "2026-04-15T10:00:00"),
                ("ss1", "assistant", "state store answer", "2026-04-15T10:00:01"),
            ],
        )
        _make_cc_jsonl(
            cc / "s.jsonl",
            [
                {"type": "user", "message": {"role": "user", "content": "claude code question"}},
            ],
        )
        return out, state, cc

    def test_writes_all_sources_once(self, tmp_path):
        out, state, cc = self._setup(tmp_path)
        counts = run_drain(out_dir=out, state_db=state, cc_dir=cc)
        assert counts == {"state_store": 2, "cc_session": 1}

        # Monthly file should exist with 3 lines
        files = list(out.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 3

        # Every record has required fields
        for line in lines:
            rec = json.loads(line)
            assert set(rec.keys()) == {"id", "ts", "source", "session_id", "role", "text"}

    def test_second_run_is_idempotent(self, tmp_path):
        out, state, cc = self._setup(tmp_path)
        run_drain(out_dir=out, state_db=state, cc_dir=cc)
        counts2 = run_drain(out_dir=out, state_db=state, cc_dir=cc)
        # No new records on re-run
        assert counts2 == {"state_store": 0, "cc_session": 0}
        # File still has just 3 lines
        files = list(out.glob("*.jsonl"))
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 3

    def test_new_turn_appended_on_later_run(self, tmp_path):
        out, state, cc = self._setup(tmp_path)
        run_drain(out_dir=out, state_db=state, cc_dir=cc)

        # Add a new state_store message and re-run
        conn = sqlite3.connect(str(state))
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES ('ss1', 'user', 'brand new question', '2026-04-15T11:00:00')"
        )
        conn.commit()
        conn.close()

        counts2 = run_drain(out_dir=out, state_db=state, cc_dir=cc)
        assert counts2["state_store"] == 1
        assert counts2["cc_session"] == 0

    def test_dedup_uses_content_hash(self, tmp_path):
        out, state, _ = self._setup(tmp_path)
        # No CC — state_store only
        no_cc = tmp_path / "nope2"

        # Run, then duplicate the exact same content with whitespace differences
        run_drain(out_dir=out, state_db=state, cc_dir=no_cc)
        conn = sqlite3.connect(str(state))
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES ('ss1', 'user', '  state store question  ', '2026-04-15T12:00:00')"
        )
        conn.commit()
        conn.close()
        counts2 = run_drain(out_dir=out, state_db=state, cc_dir=no_cc)
        assert counts2["state_store"] == 0  # whitespace-normalised dupe


class TestFingerprint:
    def test_stable_across_whitespace(self):
        a = _fingerprint("state_store", "s1", "user", "Hello World")
        b = _fingerprint("state_store", "s1", "user", "hello  world")
        assert a == b

    def test_differs_by_source(self):
        a = _fingerprint("cc_session", "s1", "user", "hi")
        b = _fingerprint("state_store", "s1", "user", "hi")
        assert a != b


# ---------------------------------------------------------------------------
# Defaults + missing-source logging
# ---------------------------------------------------------------------------


class TestDefaultsAndMissingSources:
    def test_cc_dir_derived_from_claude_projects_path(self, monkeypatch):
        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc.settings, "training_corpus_cc_dir", "")
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", "/mnt/cc-projects")
        assert tc.default_cc_dir() == Path("/mnt/cc-projects/-home-labadmin-gateway-nerves")

    def test_cc_dir_falls_back_to_jess_home(self, monkeypatch):
        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc.settings, "training_corpus_cc_dir", "")
        monkeypatch.delenv("CLAUDE_PROJECTS_PATH", raising=False)
        assert tc.default_cc_dir() == Path("/home/jess/.claude/projects/-home-labadmin-gateway-nerves")

    def test_empty_claude_projects_path_falls_back(self, monkeypatch):
        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc.settings, "training_corpus_cc_dir", "")
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", "")
        assert tc.default_cc_dir() == Path("/home/jess/.claude/projects/-home-labadmin-gateway-nerves")

    def test_explicit_cc_dir_wins(self, monkeypatch):
        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc.settings, "training_corpus_cc_dir", "/custom/cc")
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", "/mnt/cc-projects")
        assert tc.default_cc_dir() == Path("/custom/cc")

    def test_no_helios_era_slug_in_defaults(self):
        from orchestrator.config import Settings

        assert "-opt-gateway-mvp" not in Settings.model_fields["training_corpus_cc_dir"].default

    def test_missing_sources_logged_once_cc_info_state_warning(self, tmp_path, monkeypatch, caplog):
        import logging

        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc, "_reported_missing", set())
        out = tmp_path / "out"
        kwargs = dict(
            out_dir=out,
            state_db=tmp_path / "absent-state.db",
            cc_dir=tmp_path / "absent-cc",
        )
        with caplog.at_level(logging.DEBUG, logger="orchestrator.jobs_training_corpus"):
            run_drain(**kwargs)
            run_drain(**kwargs)

        skipped = [r for r in caplog.records if r.levelno >= logging.INFO and "skipped" in r.getMessage()]
        # Once each across two runs. cc is optional → INFO; a missing
        # brain_state.db is a real fault → WARNING.
        assert len(skipped) == 2
        warnings = [r for r in skipped if r.levelno >= logging.WARNING]
        assert len(warnings) == 1 and "state_store" in warnings[0].getMessage()

    def test_source_that_returns_then_vanishes_is_reported_again(self, tmp_path, monkeypatch, caplog):
        import logging

        from orchestrator import jobs_training_corpus as tc

        monkeypatch.setattr(tc, "_reported_missing", set())
        cc = tmp_path / "cc"
        with caplog.at_level(logging.INFO, logger="orchestrator.jobs_training_corpus"):
            list(drain_cc_sessions(cc))  # missing → logged
            cc.mkdir()
            list(drain_cc_sessions(cc))  # present → re-armed
            cc.rmdir()
            list(drain_cc_sessions(cc))  # missing again → logged again
        assert len([r for r in caplog.records if "skipped" in r.getMessage()]) == 2


class TestOpenWebuiRetired:
    def test_legacy_owui_records_kept_and_deduped(self, tmp_path):
        out = tmp_path / "training_corpus"
        out.mkdir()
        state = tmp_path / "state.db"
        _make_state_db(state, [("ss1", "user", "state store question", "2026-04-15T10:00:00")])
        legacy_id = _fingerprint("owui", "owui:c1", "user", "old chat turn")
        legacy = {
            "id": legacy_id,
            "ts": "2026-04-01T00:00:00+00:00",
            "source": "owui",
            "session_id": "owui:c1",
            "role": "user",
            "text": "old chat turn",
        }
        legacy_file = out / "2026-04.jsonl"
        legacy_file.write_text(json.dumps(legacy) + "\n")

        counts = run_drain(out_dir=out, state_db=state, cc_dir=tmp_path / "nope")
        assert counts == {"state_store": 1, "cc_session": 0}
        assert json.loads(legacy_file.read_text().strip())["source"] == "owui"

    def test_no_owui_source_or_setting(self):
        from orchestrator import jobs_training_corpus as tc
        from orchestrator.config import Settings

        assert not hasattr(tc, "drain_owui")
        assert "training_corpus_owui_db" not in Settings.model_fields
