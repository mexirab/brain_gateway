"""Tests for orchestrator/jobs_training_corpus.py drain pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from orchestrator import jobs_training_corpus as jtc
from orchestrator.jobs_training_corpus import (
    Record,
    _fingerprint,
    _load_existing_ids,
    drain_cc_sessions,
    drain_owui,
    drain_state_store,
    run_drain,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_owui_db(path: Path, conversations: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE chat (id VARCHAR(255) PRIMARY KEY, user_id VARCHAR(255), "
        "title TEXT, archived INTEGER, created_at DATETIME, updated_at DATETIME, "
        "chat JSON, meta JSON)"
    )
    for conv in conversations:
        conn.execute(
            "INSERT INTO chat (id, user_id, title, archived, created_at, updated_at, chat, meta) "
            "VALUES (?, 'u', ?, 0, '2026-04-15', '2026-04-15', ?, '{}')",
            (conv["id"], conv.get("title", "t"), json.dumps(conv["chat"])),
        )
    conn.commit()
    conn.close()


def _make_state_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE chat_messages ("
        "id INTEGER PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT, "
        "routing TEXT, announcement_type TEXT, created_at TEXT)"
    )
    for conv_id, role, content, created_at in rows:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?)",
            (conv_id, role, content, created_at),
        )
    conn.commit()
    conn.close()


def _make_cc_jsonl(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# drain_owui
# ---------------------------------------------------------------------------


class TestDrainOwui:
    def test_missing_db_yields_nothing(self, tmp_path):
        assert list(drain_owui(tmp_path / "nope.db")) == []

    def test_extracts_user_and_assistant_turns(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {
                "id": "c1",
                "chat": {
                    "messages": [
                        {"role": "user", "content": "hello jess", "timestamp": 1775000000},
                        {"role": "assistant", "content": "hi nadim", "timestamp": 1775000001},
                    ],
                },
            }
        ])
        records = list(drain_owui(db))
        assert len(records) == 2
        assert {r.role for r in records} == {"user", "assistant"}
        assert all(r.source == "owui" for r in records)
        assert all(r.session_id == "owui:c1" for r in records)
        # Timestamps converted from unix epoch
        assert all("2026" in r.ts or "2025" in r.ts for r in records)

    def test_falls_back_to_history_messages(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {
                "id": "c2",
                "chat": {
                    "history": {
                        "messages": {
                            "m1": {"role": "user", "content": "from history", "timestamp": 1775000000},
                        },
                    },
                },
            }
        ])
        records = list(drain_owui(db))
        assert len(records) == 1
        assert records[0].text == "from history"

    def test_skips_short_content(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {"id": "c3", "chat": {"messages": [{"role": "user", "content": "a"}]}},
        ])
        assert list(drain_owui(db)) == []

    def test_skips_non_user_assistant_roles(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {"id": "c4", "chat": {"messages": [{"role": "system", "content": "ignored system prompt"}]}},
        ])
        assert list(drain_owui(db)) == []


# ---------------------------------------------------------------------------
# drain_state_store
# ---------------------------------------------------------------------------


class TestDrainStateStore:
    def test_missing_db_yields_nothing(self, tmp_path):
        assert list(drain_state_store(tmp_path / "nope.db")) == []

    def test_extracts_messages(self, tmp_path):
        db = tmp_path / "state.db"
        _make_state_db(db, [
            ("conv1", "user", "test question", "2026-04-15T10:00:00"),
            ("conv1", "assistant", "test answer", "2026-04-15T10:00:01"),
            ("conv1", "tool", "should be skipped", "2026-04-15T10:00:02"),
        ])
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
        _make_cc_jsonl(cc / "session1.jsonl", [
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
        ])
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
        _make_cc_jsonl(cc / "s.jsonl", [
            # Dropped: harness noise dumps
            {"type": "user", "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}},
            {"type": "user", "message": {"role": "user", "content": "<command-name>/foo</command-name>"}},
            {"type": "user", "message": {"role": "user", "content": "<bash-stdout>output</bash-stdout>"}},
            # Kept: legitimate user prose that happens to start with a bracket
            {"type": "user", "message": {"role": "user", "content": "[urgent] remind me to send the draft"}},
            {"type": "user", "message": {"role": "user", "content": "<Component> element rendering question"}},
        ])
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
        _make_cc_jsonl(cc / "s.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "here's the key: sk-abcdefghijklmnopqrstuvwxyz1234567890"}},
            {"type": "user", "message": {"role": "user", "content": "clean question about the weather"}},
        ])
        records = list(drain_cc_sessions(cc))
        assert len(records) == 1
        assert "weather" in records[0].text

    def test_private_key_block_dropped(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(cc / "s.jsonl", [
            {"type": "user", "message": {"role": "user",
             "content": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg..."}},
        ])
        assert list(drain_cc_sessions(cc)) == []

    def test_fernet_ciphertext_dropped(self, tmp_path):
        cc = tmp_path / "cc"
        cc.mkdir()
        _make_cc_jsonl(cc / "s.jsonl", [
            {"type": "user", "message": {"role": "user",
             "content": "gAAAAABp1isCb4iFivPOd1nFVdlQLPaDosUURSOvLOyf5OS1kkmOaTqsA0CauO35RzKAmntr"}},
        ])
        assert list(drain_cc_sessions(cc)) == []

    def test_bearer_token_dropped_in_owui(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {"id": "c1", "chat": {"messages": [
                {"role": "user", "content": "Authorization: Bearer eyJhbGc1234567890abcdefghij.foobar.baz"},
                {"role": "user", "content": "actually fine message"},
            ]}},
        ])
        records = list(drain_owui(db))
        assert len(records) == 1
        assert "fine message" in records[0].text


class TestOwuiListContent:
    """OWUI multimodal messages put content in a list of parts."""

    def test_list_of_parts_joined(self, tmp_path):
        db = tmp_path / "webui.db"
        _make_owui_db(db, [
            {"id": "c1", "chat": {"messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "first part"},
                    {"type": "image", "url": "..."},
                    {"type": "text", "text": "second part"},
                ]},
            ]}},
        ])
        records = list(drain_owui(db))
        assert len(records) == 1
        assert "first part" in records[0].text
        assert "second part" in records[0].text


class TestSameTextCrossSource:
    """Same text in two sources stays as two records — source is in the hash."""

    def test_cross_source_not_deduped(self, tmp_path):
        out = tmp_path / "training_corpus"
        owui = tmp_path / "webui.db"
        state = tmp_path / "state.db"
        no_cc = tmp_path / "nope"

        _make_owui_db(owui, [
            {"id": "c1", "chat": {"messages": [{"role": "user", "content": "same exact question"}]}},
        ])
        _make_state_db(state, [
            ("conv1", "user", "same exact question", "2026-04-15T10:00:00"),
        ])

        counts = run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=no_cc)
        assert counts["owui"] == 1
        assert counts["state_store"] == 1


# ---------------------------------------------------------------------------
# run_drain — end-to-end + idempotency
# ---------------------------------------------------------------------------


class TestRunDrain:
    def _setup(self, tmp_path):
        owui = tmp_path / "webui.db"
        state = tmp_path / "state.db"
        cc = tmp_path / "cc"
        out = tmp_path / "training_corpus"
        cc.mkdir()

        _make_owui_db(owui, [
            {"id": "c1", "chat": {"messages": [
                {"role": "user", "content": "owui question one"},
                {"role": "assistant", "content": "owui answer one"},
            ]}},
        ])
        _make_state_db(state, [
            ("ss1", "user", "state store question", "2026-04-15T10:00:00"),
        ])
        _make_cc_jsonl(cc / "s.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "claude code question"}},
        ])
        return out, owui, state, cc

    def test_writes_all_sources_once(self, tmp_path):
        out, owui, state, cc = self._setup(tmp_path)
        counts = run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=cc)
        assert counts == {"owui": 2, "state_store": 1, "cc_session": 1}

        # Monthly file should exist with 4 lines
        files = list(out.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 4

        # Every record has required fields
        for line in lines:
            rec = json.loads(line)
            assert set(rec.keys()) == {"id", "ts", "source", "session_id", "role", "text"}

    def test_second_run_is_idempotent(self, tmp_path):
        out, owui, state, cc = self._setup(tmp_path)
        run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=cc)
        counts2 = run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=cc)
        # No new records on re-run
        assert counts2 == {"owui": 0, "state_store": 0, "cc_session": 0}
        # File still has just 4 lines
        files = list(out.glob("*.jsonl"))
        lines = files[0].read_text().strip().split("\n")
        assert len(lines) == 4

    def test_new_turn_appended_on_later_run(self, tmp_path):
        out, owui, state, cc = self._setup(tmp_path)
        run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=cc)

        # Add a new state_store message and re-run
        conn = sqlite3.connect(str(state))
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES ('ss1', 'user', 'brand new question', '2026-04-15T11:00:00')"
        )
        conn.commit()
        conn.close()

        counts2 = run_drain(out_dir=out, owui_db=owui, state_db=state, cc_dir=cc)
        assert counts2["state_store"] == 1
        assert counts2["owui"] == 0

    def test_dedup_uses_content_hash(self, tmp_path):
        out, _, state, _ = self._setup(tmp_path)
        # No OWUI or CC — state_store only
        no_owui = tmp_path / "nope1.db"
        no_cc = tmp_path / "nope2"

        # Run, then duplicate the exact same content with whitespace differences
        run_drain(out_dir=out, owui_db=no_owui, state_db=state, cc_dir=no_cc)
        conn = sqlite3.connect(str(state))
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) "
            "VALUES ('ss1', 'user', '  state store question  ', '2026-04-15T12:00:00')"
        )
        conn.commit()
        conn.close()
        counts2 = run_drain(out_dir=out, owui_db=no_owui, state_db=state, cc_dir=no_cc)
        assert counts2["state_store"] == 0  # whitespace-normalised dupe


class TestFingerprint:
    def test_stable_across_whitespace(self):
        a = _fingerprint("owui", "s1", "user", "Hello World")
        b = _fingerprint("owui", "s1", "user", "hello  world")
        assert a == b

    def test_differs_by_source(self):
        a = _fingerprint("owui", "s1", "user", "hi")
        b = _fingerprint("state_store", "s1", "user", "hi")
        assert a != b
