"""
Tests for claude_code_tracker.py and the claude_code_turns section of state_store.py.

Tests pure/near-pure functions directly. State-store tests use an in-memory SQLite
database via monkeypatching DB_PATH. Tracker tests mock state_store to avoid I/O.
"""

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import check helpers
# ---------------------------------------------------------------------------


def _can_import_tracker():
    try:
        import orchestrator.claude_code_tracker  # noqa: F401
        return True
    except (ImportError, ModuleNotFoundError):
        return False


def _can_import_state_store():
    try:
        import orchestrator.state_store  # noqa: F401
        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_tracker = pytest.mark.skipif(
    not _can_import_tracker(),
    reason="claude_code_tracker requires full orchestrator dependencies",
)

_skip_no_state_store = pytest.mark.skipif(
    not _can_import_state_store(),
    reason="state_store requires orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CLAUDE_CODE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_code_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    turn_type TEXT NOT NULL,
    content TEXT,
    tool_uses TEXT,
    files_touched TEXT,
    commit_hash TEXT
);
"""


def _make_in_memory_db():
    """Return a fresh in-memory SQLite path (shared-cache URI)."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Initialize the table
    conn = sqlite3.connect(path)
    conn.executescript(_CLAUDE_CODE_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _make_jsonl_entry(role, text=None, tools=None, timestamp=None):
    """Build a minimal .jsonl session entry dict."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in (tools or []):
        content.append(tool)
    return {
        "type": role,
        "timestamp": timestamp or "2026-04-12T18:32:28.964Z",
        "uuid": "test-uuid-1234",
        "message": {
            "role": role,
            "content": content,
        },
        "sessionId": "session-abc123",
    }


# ---------------------------------------------------------------------------
# Tests: _normalize_turn
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestNormalizeTurn:
    """Tests for _normalize_turn() — the core entry parser."""

    def setup_method(self):
        from orchestrator.claude_code_tracker import _normalize_turn
        self._normalize_turn = _normalize_turn

    def test_user_text_entry(self):
        entry = _make_jsonl_entry("user", text="Hello, please help me")
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert result["turn_type"] == "user"
        assert "Hello" in result["content"]
        assert result["tool_uses"] == []
        assert result["files_touched"] == []

    def test_assistant_text_entry(self):
        entry = _make_jsonl_entry("assistant", text="Sure, I can help with that!")
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert result["turn_type"] == "assistant"
        assert "Sure" in result["content"]

    def test_assistant_with_edit_tool_extracts_file_path(self):
        tools = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/opt/helios/gateway_mvp/foo.py"}}]
        entry = _make_jsonl_entry("assistant", text="Editing the file", tools=tools)
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert "Edit" in result["tool_uses"]
        assert "/opt/helios/gateway_mvp/foo.py" in result["files_touched"]

    def test_write_tool_extracts_file_path(self):
        tools = [{"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/newfile.py"}}]
        entry = _make_jsonl_entry("assistant", text="Writing new file", tools=tools)
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert "Write" in result["tool_uses"]
        assert "/tmp/newfile.py" in result["files_touched"]

    def test_multiedit_tool_extracts_path(self):
        tools = [{"type": "tool_use", "name": "MultiEdit", "input": {"file_path": "/tmp/multi.py"}}]
        entry = _make_jsonl_entry("assistant", text="Multi-editing", tools=tools)
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert "MultiEdit" in result["tool_uses"]
        assert "/tmp/multi.py" in result["files_touched"]

    def test_thinking_only_returns_none(self):
        """A thinking-only entry has no text or tool_uses — should return None."""
        entry = {
            "type": "assistant",
            "timestamp": "2026-04-12T18:32:28.964Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "Let me reason..."}],
            },
        }
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is None

    def test_empty_content_and_no_tools_returns_none(self):
        entry = {
            "type": "assistant",
            "timestamp": "2026-04-12T18:32:28.964Z",
            "message": {"role": "assistant", "content": []},
        }
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is None

    def test_non_dict_message_returns_none(self):
        entry = {"type": "assistant", "timestamp": "2026-04-12T18:32:28.964Z", "message": "not a dict"}
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is None

    def test_session_id_extracted_from_entry(self):
        entry = _make_jsonl_entry("assistant", text="Hello")
        entry["sessionId"] = "my-session-id"
        result = self._normalize_turn(entry, "/fake/path/my-session-id.jsonl")
        assert result is not None
        assert result["session_id"] == "my-session-id"

    def test_session_id_falls_back_to_filename(self):
        entry = _make_jsonl_entry("assistant", text="Hello")
        entry.pop("sessionId", None)
        result = self._normalize_turn(entry, "/fake/path/uuid-abc123.jsonl")
        assert result is not None
        assert result["session_id"] == "uuid-abc123"

    def test_content_capped_at_2000_chars(self):
        long_text = "x" * 5000
        entry = _make_jsonl_entry("assistant", text=long_text)
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert len(result["content"]) <= 2000

    def test_tool_without_file_path_still_recorded(self):
        tools = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
        entry = _make_jsonl_entry("assistant", text="Running bash", tools=tools)
        result = self._normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert "Bash" in result["tool_uses"]
        assert result["files_touched"] == []


# ---------------------------------------------------------------------------
# Tests: _find_latest_session_file
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestFindLatestSessionFile:
    """Tests for _find_latest_session_file() — all tests set CLAUDE_PROJECTS_PATH
    so the security check passes for tmp_path-based fixtures.
    """

    def setup_method(self):
        from orchestrator.claude_code_tracker import _find_latest_session_file
        self._find_latest_session_file = _find_latest_session_file

    def test_empty_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        result = self._find_latest_session_file(str(tmp_path))
        assert result is None

    def test_single_file_returned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        f = tmp_path / "session-abc.jsonl"
        f.write_text('{"type":"assistant"}\n')
        result = self._find_latest_session_file(str(tmp_path))
        assert result == str(f)

    def test_most_recent_file_returned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        f1 = tmp_path / "old-session.jsonl"
        f1.write_text('{"type":"user"}\n')
        time.sleep(0.05)  # ensure mtime difference
        f2 = tmp_path / "new-session.jsonl"
        f2.write_text('{"type":"assistant"}\n')
        result = self._find_latest_session_file(str(tmp_path))
        assert result == str(f2)

    def test_no_project_dir_scans_root(self, tmp_path, monkeypatch):
        """When no project_dir is provided, the whole root is scanned."""
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session = proj / "session-xyz.jsonl"
        session.write_text('{"type":"assistant"}\n')

        result = self._find_latest_session_file()  # no project_dir
        assert result == str(session)

    def test_nonexistent_project_dir_falls_back_to_root(self, tmp_path, monkeypatch):
        """When project_dir doesn't exist, fall back to scanning the full root."""
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session = proj / "session-xyz.jsonl"
        session.write_text('{"type":"assistant"}\n')

        result = self._find_latest_session_file(str(tmp_path / "no-such-dir"))
        assert result == str(session)

    def test_project_dir_outside_root_falls_back_to_root(self, tmp_path, monkeypatch):
        """A project_dir that escapes the root is rejected; scan falls back to root."""
        root = tmp_path / "projects"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(root))

        # Session inside the root
        good_session = root / "-test" / "session.jsonl"
        good_session.parent.mkdir()
        good_session.write_text('{"type":"assistant"}\n')
        # Session OUTSIDE the root — must never be returned
        bad_session = outside / "evil.jsonl"
        bad_session.write_text('{"type":"assistant"}\n')

        result = self._find_latest_session_file(str(outside))
        assert result == str(good_session)
        assert result != str(bad_session)

    def test_no_projects_at_all_returns_none(self, tmp_path, monkeypatch):
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(root))
        result = self._find_latest_session_file(str(tmp_path / "no-such-dir"))
        assert result is None

    def test_root_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path / "does-not-exist"))
        result = self._find_latest_session_file()
        assert result is None


# ---------------------------------------------------------------------------
# Tests: get_current_session_turns
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestGetCurrentSessionTurns:
    """Tests for get_current_session_turns() — live file parsing.

    All tests set CLAUDE_PROJECTS_PATH to tmp_path so the security check
    on project_dir passes.
    """

    def setup_method(self):
        from orchestrator.claude_code_tracker import get_current_session_turns
        self._get_current_session_turns = get_current_session_turns

    @pytest.fixture(autouse=True)
    def _set_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

    def test_returns_last_n_turns(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        lines = []
        for i in range(10):
            entry = _make_jsonl_entry("assistant", text=f"Turn {i}")
            lines.append(json.dumps(entry))
        session_file.write_text("\n".join(lines))

        result = self._get_current_session_turns(n=3, project_dir=str(tmp_path))
        assert len(result) == 3
        # Last 3 are turns 7, 8, 9
        assert "Turn 9" in result[-1]["content"]

    def test_handles_malformed_json_lines_gracefully(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        good_entry = json.dumps(_make_jsonl_entry("assistant", text="Good turn"))
        session_file.write_text(f"{{bad json line\n{good_entry}\n")

        result = self._get_current_session_turns(n=10, project_dir=str(tmp_path))
        assert len(result) == 1
        assert "Good turn" in result[0]["content"]

    def test_skips_non_message_entry_types(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        entries = [
            json.dumps({"type": "permission-mode", "data": "allow"}),
            json.dumps({"type": "file-history-snapshot", "files": []}),
            json.dumps({"type": "attachment", "content": "..."}),
            json.dumps({"type": "tool_result", "output": "done"}),
            json.dumps(_make_jsonl_entry("assistant", text="Real turn")),
        ]
        session_file.write_text("\n".join(entries))

        result = self._get_current_session_turns(n=10, project_dir=str(tmp_path))
        assert len(result) == 1
        assert "Real turn" in result[0]["content"]

    def test_returns_empty_list_when_no_file(self, tmp_path):
        result = self._get_current_session_turns(n=5, project_dir=str(tmp_path / "no-such"))
        assert result == []

    def test_oversized_file_is_skipped(self, tmp_path, monkeypatch):
        """Files over the size cap are refused (memory-exhaustion guard)."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="should not be read")))

        # Mock os.path.getsize to report a huge size
        import orchestrator.claude_code_tracker as tracker
        monkeypatch.setattr(
            "os.path.getsize",
            lambda p: 100 * 1024 * 1024 if str(p) == str(session_file) else os.stat(p).st_size,
        )
        result = self._get_current_session_turns(n=5, project_dir=str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# Tests: _detect_project_from_path
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestDetectProjectFromPath:
    """Tests for _detect_project_from_path()."""

    def setup_method(self):
        from orchestrator.claude_code_tracker import _detect_project_from_path
        self._detect_project_from_path = _detect_project_from_path

    def test_standard_path_extracts_last_two_segments(self):
        path = "/home/user/.claude/projects/-opt-helios-gateway-mvp/uuid.jsonl"
        result = self._detect_project_from_path(path)
        # segments of "opt-helios-gateway-mvp" split by - → last 2 = "gateway_mvp"
        assert result == "gateway_mvp"

    def test_edge_case_one_segment(self):
        path = "/home/user/.claude/projects/-singleword/uuid.jsonl"
        result = self._detect_project_from_path(path)
        # "singleword".split("-") = ["singleword"] → one segment → segments[0]
        assert result == "singleword"

    def test_no_matching_segment_returns_empty(self):
        path = "/tmp/uuid.jsonl"
        result = self._detect_project_from_path(path)
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: state_store claude_code_turns functions (in-memory SQLite)
# ---------------------------------------------------------------------------


@_skip_no_state_store
class TestStateStoreClaudeCodeTurns:
    """Tests for log_claude_code_turn, get_claude_code_turns, get_claude_code_files_touched,
    and cleanup_old_claude_code_turns using a real in-memory SQLite DB.
    """

    @pytest.fixture(autouse=True)
    def _patch_db_path(self, tmp_path, monkeypatch):
        import orchestrator.state_store as ss
        db_path = str(tmp_path / "test_state.db")
        monkeypatch.setattr(ss, "DB_PATH", db_path)
        # Initialize the schema
        from orchestrator.db import init_db
        init_db(db_path, _CLAUDE_CODE_SCHEMA)
        self.db_path = db_path

    def _now_iso(self, delta_minutes=0):
        return (datetime.now() + timedelta(minutes=delta_minutes)).isoformat()

    def _make_turn(self, content="hello", project="test_proj", files=None, tools=None, delta_minutes=0):
        return {
            "timestamp": self._now_iso(delta_minutes),
            "session_id": "sess-001",
            "project": project,
            "turn_type": "assistant",
            "content": content,
            "tool_uses": tools or [],
            "files_touched": files or [],
            "commit_hash": "",
        }

    def test_log_and_retrieve_turn(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns
        turn = self._make_turn(content="wrote some code")
        row_id = log_claude_code_turn(turn)
        assert isinstance(row_id, int)
        assert row_id > 0

        turns = get_claude_code_turns(since_minutes=5)
        assert len(turns) == 1
        assert turns[0]["content"] == "wrote some code"

    def test_time_filter_cuts_off_old_rows(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns
        old_turn = self._make_turn(content="ancient history", delta_minutes=-200)
        recent_turn = self._make_turn(content="just now")
        log_claude_code_turn(old_turn)
        log_claude_code_turn(recent_turn)

        turns = get_claude_code_turns(since_minutes=60)
        contents = [t["content"] for t in turns]
        assert "just now" in contents
        assert "ancient history" not in contents

    def test_project_filter(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns
        log_claude_code_turn(self._make_turn(content="project A work", project="proj_a"))
        log_claude_code_turn(self._make_turn(content="project B work", project="proj_b"))

        turns_a = get_claude_code_turns(since_minutes=60, project="proj_a")
        assert all(t["project"] == "proj_a" for t in turns_a)
        assert len(turns_a) == 1

    def test_limit_respected(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns
        for i in range(10):
            log_claude_code_turn(self._make_turn(content=f"turn {i}"))
        turns = get_claude_code_turns(since_minutes=60, limit=3)
        assert len(turns) == 3

    def test_tool_uses_and_files_decoded_as_lists(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns
        turn = self._make_turn(
            files=["/opt/helios/gateway_mvp/foo.py", "/opt/helios/gateway_mvp/bar.py"],
            tools=["Edit", "Bash"],
        )
        log_claude_code_turn(turn)
        turns = get_claude_code_turns(since_minutes=5)
        assert isinstance(turns[0]["files_touched"], list)
        assert "/opt/helios/gateway_mvp/foo.py" in turns[0]["files_touched"]
        assert isinstance(turns[0]["tool_uses"], list)
        assert "Edit" in turns[0]["tool_uses"]

    def test_get_files_touched_deduplicates(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_files_touched
        log_claude_code_turn(self._make_turn(files=["/a/foo.py", "/a/bar.py"]))
        log_claude_code_turn(self._make_turn(files=["/a/foo.py", "/a/baz.py"]))

        files = get_claude_code_files_touched(since_minutes=60)
        assert files.count("/a/foo.py") == 1
        assert "/a/bar.py" in files
        assert "/a/baz.py" in files

    def test_cleanup_deletes_old_turns(self):
        from orchestrator.state_store import log_claude_code_turn, get_claude_code_turns, cleanup_old_claude_code_turns
        old_turn = self._make_turn(content="very old", delta_minutes=-60 * 24 * 10)  # 10 days ago
        recent_turn = self._make_turn(content="recent")
        log_claude_code_turn(old_turn)
        log_claude_code_turn(recent_turn)

        deleted = cleanup_old_claude_code_turns(days=7)
        assert deleted >= 1

        # Recent turn must still be there
        turns = get_claude_code_turns(since_minutes=60 * 24 * 30)
        contents = [t["content"] for t in turns]
        assert "recent" in contents
        assert "very old" not in contents


# ---------------------------------------------------------------------------
# Tests: get_recent_activity_summary and get_files_touched (tracker level)
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestActivitySummaryAndFilesTouched:
    """Tests for get_recent_activity_summary() and get_files_touched() — fallback logic."""

    def test_summary_uses_buffer_when_populated(self):
        import orchestrator.claude_code_tracker as tracker
        mock_turns = [
            {
                "timestamp": "2026-04-12T18:30:00",
                "turn_type": "assistant",
                "content": "I edited the config file",
                "tool_uses": ["Edit"],
                "files_touched": ["/opt/helios/gateway_mvp/config.py"],
            }
        ]
        with patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=mock_turns):
            summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert "config.py" in summary
        assert "Edit" in summary

    def test_summary_falls_back_to_live_file_when_buffer_empty(self, tmp_path):
        import orchestrator.claude_code_tracker as tracker
        # Write a minimal session file
        session_file = tmp_path / "session.jsonl"
        entry = _make_jsonl_entry("assistant", text="Working on fallback feature")
        session_file.write_text(json.dumps(entry) + "\n")

        with patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=[]):
            with patch("orchestrator.claude_code_tracker._find_latest_session_file", return_value=str(session_file)):
                summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert "fallback feature" in summary

    def test_summary_returns_empty_string_when_both_sources_empty(self):
        import orchestrator.claude_code_tracker as tracker
        with patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=[]):
            with patch("orchestrator.claude_code_tracker.get_current_session_turns", return_value=[]):
                summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert summary == ""

    def test_get_files_touched_uses_buffer_when_populated(self):
        import orchestrator.claude_code_tracker as tracker
        with patch(
            "orchestrator.claude_code_tracker.get_claude_code_files_touched",
            return_value=["/opt/helios/gateway_mvp/foo.py"],
        ):
            files = tracker.get_files_touched(minutes_back=60)
        assert "/opt/helios/gateway_mvp/foo.py" in files

    def test_get_files_touched_falls_back_to_live_file(self, tmp_path):
        import orchestrator.claude_code_tracker as tracker
        session_file = tmp_path / "session.jsonl"
        tools = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/live/path/module.py"}}]
        entry = _make_jsonl_entry("assistant", text="editing", tools=tools)
        session_file.write_text(json.dumps(entry) + "\n")

        with patch("orchestrator.claude_code_tracker.get_claude_code_files_touched", return_value=[]):
            with patch("orchestrator.claude_code_tracker._find_latest_session_file", return_value=str(session_file)):
                files = tracker.get_files_touched(minutes_back=60)
        assert "/live/path/module.py" in files


# ---------------------------------------------------------------------------
# Tests: log_turn_from_hook (buffer ingestion)
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestLogTurnFromHook:
    """Tests for log_turn_from_hook() — hook payload ingestion.

    Security-critical behavior: transcript_path is IGNORED entirely. The
    function uses session_id (UUID-validated) to locate the file itself,
    constrained to CLAUDE_PROJECTS_PATH. These tests exercise both the
    session-id-based path and the direct-payload fallback, and verify
    that adversarial payloads never read files outside the root.
    """

    def test_session_id_resolves_file_under_root(self, tmp_path, monkeypatch):
        """Valid UUID session_id + matching file → parsed and logged."""
        import orchestrator.claude_code_tracker as tracker
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session_id = "b59382fb-b98a-404a-a732-c74e20c69073"
        session_file = proj / f"{session_id}.jsonl"
        entry = _make_jsonl_entry("assistant", text="Committed new changes")
        session_file.write_text(json.dumps(entry) + "\n")

        payload = {"session_id": session_id, "transcript_path": "/ignored"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=7) as mock_log:
            result = tracker.log_turn_from_hook(payload)

        assert result == 7
        call_args = mock_log.call_args[0][0]
        assert "Committed new changes" in call_args["content"]
        assert call_args["session_id"] == session_id

    def test_direct_payload_fallback_when_session_id_missing(self):
        """No session_id → falls through to direct payload with coercion."""
        import orchestrator.claude_code_tracker as tracker
        payload = {
            "project": "gateway_mvp",
            "turn_type": "assistant",
            "content": "Fixed the bug",
            "tool_uses": ["Edit"],
            "files_touched": ["/opt/helios/gateway_mvp/fix.py"],
            "commit_hash": "abc123",
        }
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=42) as mock_log:
            result = tracker.log_turn_from_hook(payload)
        assert result == 42
        call_args = mock_log.call_args[0][0]
        assert call_args["content"] == "Fixed the bug"
        assert call_args["tool_uses"] == ["Edit"]

    def test_direct_payload_fallback_when_session_file_missing(self, tmp_path, monkeypatch):
        """Valid session_id but no matching file → falls through to direct payload."""
        import orchestrator.claude_code_tracker as tracker
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

        payload = {
            "session_id": "abcdefgh-ijkl-mnop-qrst-uvwxyz123456",
            "content": "fallback content",
        }
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=5) as mock_log:
            result = tracker.log_turn_from_hook(payload)
        assert result == 5
        call_args = mock_log.call_args[0][0]
        assert call_args["content"] == "fallback content"

    def test_path_traversal_session_id_rejected(self, tmp_path, monkeypatch):
        """A session_id with path-traversal chars is rejected by the regex;
        function falls through to the direct-payload branch without reading
        any file outside the root.
        """
        import orchestrator.claude_code_tracker as tracker
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

        # Plant a valid session file under the root that we must NOT read
        proj = tmp_path / "-real"
        proj.mkdir()
        real_file = proj / "realuuid.jsonl"
        real_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="secret data")))

        payload = {"session_id": "../../etc/passwd", "content": "attacker payload"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=99) as mock_log:
            result = tracker.log_turn_from_hook(payload)

        assert result == 99
        call_args = mock_log.call_args[0][0]
        # Should use direct payload, NOT the secret real file
        assert call_args["content"] == "attacker payload"
        assert "secret data" not in call_args["content"]

    def test_glob_injection_session_id_rejected(self):
        """A session_id containing a glob wildcard is rejected by the regex."""
        import orchestrator.claude_code_tracker as tracker
        payload = {"session_id": "*", "content": "attacker payload"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=1) as mock_log:
            tracker.log_turn_from_hook(payload)
        call_args = mock_log.call_args[0][0]
        # Should use direct payload fallback, not try to glob
        assert call_args["content"] == "attacker payload"

    def test_non_list_tool_uses_coerced_safely(self):
        """A payload with non-list tool_uses should not crash; coerces to []."""
        import orchestrator.claude_code_tracker as tracker
        payload = {
            "content": "safe content",
            "tool_uses": "not a list",  # adversarial type
            "files_touched": {"also": "not a list"},
        }
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=1) as mock_log:
            tracker.log_turn_from_hook(payload)
        call_args = mock_log.call_args[0][0]
        assert call_args["tool_uses"] == []
        assert call_args["files_touched"] == []


# ---------------------------------------------------------------------------
# Tests: security helpers (_resolve_under_root, _find_session_file_by_id,
# _is_reasonable_size)
# ---------------------------------------------------------------------------


@_skip_no_tracker
class TestSecurityHelpers:
    """Tests for the path-safety and file-size helpers introduced to close
    review blockers around path traversal and memory exhaustion.
    """

    def test_resolve_under_root_accepts_path_inside_root(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _resolve_under_root

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        inside = tmp_path / "subdir"
        inside.mkdir()
        assert _resolve_under_root(str(inside)) == str(inside.resolve())

    def test_resolve_under_root_accepts_root_itself(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _resolve_under_root

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _resolve_under_root(str(tmp_path)) == str(tmp_path.resolve())

    def test_resolve_under_root_rejects_etc_passwd(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _resolve_under_root

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _resolve_under_root("/etc/passwd") is None

    def test_resolve_under_root_rejects_dotdot_traversal(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _resolve_under_root

        # Put the root a couple levels deep so .. actually escapes
        root = tmp_path / "a" / "b" / "projects"
        root.mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(root))

        traversal = str(root / ".." / ".." / ".." / "etc")
        # realpath on the traversal resolves it outside the root
        assert _resolve_under_root(traversal) is None

    def test_resolve_under_root_rejects_empty_string(self):
        from orchestrator.claude_code_tracker import _resolve_under_root
        assert _resolve_under_root("") is None

    def test_resolve_under_root_rejects_none(self):
        from orchestrator.claude_code_tracker import _resolve_under_root
        assert _resolve_under_root(None) is None

    def test_find_session_file_by_id_valid_uuid(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        proj = tmp_path / "-test"
        proj.mkdir()
        session_id = "b59382fb-b98a-404a-a732-c74e20c69073"
        session_file = proj / f"{session_id}.jsonl"
        session_file.write_text("{}\n")

        result = _find_session_file_by_id(session_id)
        assert result == str(session_file.resolve())

    def test_find_session_file_by_id_rejects_path_traversal(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _find_session_file_by_id("../../etc/passwd") is None

    def test_find_session_file_by_id_rejects_glob_wildcard(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _find_session_file_by_id("*") is None

    def test_find_session_file_by_id_rejects_empty(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _find_session_file_by_id("") is None

    def test_find_session_file_by_id_rejects_short_id(self, tmp_path, monkeypatch):
        """Regex requires at least 8 chars to reject trivial strings."""
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _find_session_file_by_id("short") is None

    def test_find_session_file_by_id_returns_none_when_no_match(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _find_session_file_by_id

        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        # No files exist, but the id is valid
        assert _find_session_file_by_id("valid-session-1234") is None

    def test_is_reasonable_size_accepts_small_file(self, tmp_path):
        from orchestrator.claude_code_tracker import _is_reasonable_size

        f = tmp_path / "tiny.jsonl"
        f.write_text("hello\n")
        assert _is_reasonable_size(str(f)) is True

    def test_is_reasonable_size_rejects_missing_file(self):
        from orchestrator.claude_code_tracker import _is_reasonable_size
        assert _is_reasonable_size("/no/such/file.jsonl") is False

    def test_is_reasonable_size_rejects_oversized(self, tmp_path, monkeypatch):
        from orchestrator.claude_code_tracker import _is_reasonable_size

        f = tmp_path / "fake-huge.jsonl"
        f.write_text("x")
        # Mock getsize to report 100MB (over the 50MB cap)
        monkeypatch.setattr("os.path.getsize", lambda p: 100 * 1024 * 1024)
        assert _is_reasonable_size(str(f)) is False
