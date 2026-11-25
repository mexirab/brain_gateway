"""
Tests for claude_code_tracker.py and the claude_code_turns section of state_store.py.

Tracker tests parse temp .jsonl session files and mock the state_store
writers; state-store tests run against a fresh real schema (``tmp_db``).
"""

import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import orchestrator.claude_code_tracker as tracker
from orchestrator.claude_code_tracker import (
    _detect_project_from_path,
    _find_latest_session_file,
    _find_session_file_by_id,
    _is_reasonable_size,
    _normalize_turn,
    _resolve_under_root,
    get_current_session_turns,
)
from orchestrator.state_store import (
    cleanup_old_claude_code_turns,
    get_claude_code_files_touched,
    get_claude_code_turns,
    log_claude_code_turn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jsonl_entry(role, text=None, tools=None, timestamp=None):
    """Build a minimal .jsonl session entry dict."""
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tool in tools or []:
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
# _normalize_turn
# ---------------------------------------------------------------------------


class TestNormalizeTurn:
    """Tests for _normalize_turn() — the core entry parser."""

    def test_user_text_entry(self):
        entry = _make_jsonl_entry("user", text="Hello, please help me")
        result = _normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert result["turn_type"] == "user"
        assert "Hello" in result["content"]
        assert result["tool_uses"] == []
        assert result["files_touched"] == []

    def test_assistant_text_entry(self):
        entry = _make_jsonl_entry("assistant", text="Sure, I can help with that!")
        result = _normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert result["turn_type"] == "assistant"
        assert "Sure" in result["content"]

    @pytest.mark.parametrize(
        "tool,path",
        [
            ("Edit", "/opt/gateway_mvp/foo.py"),
            ("Write", "/tmp/newfile.py"),
            ("MultiEdit", "/tmp/multi.py"),
        ],
    )
    def test_file_tool_extracts_file_path(self, tool, path):
        tools = [{"type": "tool_use", "name": tool, "input": {"file_path": path}}]
        entry = _make_jsonl_entry("assistant", text="Editing the file", tools=tools)
        result = _normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert tool in result["tool_uses"]
        assert path in result["files_touched"]

    @pytest.mark.parametrize(
        "message",
        [
            # A thinking-only entry has no text or tool_uses.
            pytest.param(
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "Let me reason..."}]},
                id="thinking-only",
            ),
            pytest.param({"role": "assistant", "content": []}, id="empty-content-no-tools"),
            pytest.param("not a dict", id="non-dict-message"),
        ],
    )
    def test_entry_without_text_or_tools_returns_none(self, message):
        entry = {"type": "assistant", "timestamp": "2026-04-12T18:32:28.964Z", "message": message}
        assert _normalize_turn(entry, "/fake/session.jsonl") is None

    def test_session_id_extracted_from_entry(self):
        entry = _make_jsonl_entry("assistant", text="Hello")
        entry["sessionId"] = "my-session-id"
        result = _normalize_turn(entry, "/fake/path/my-session-id.jsonl")
        assert result is not None
        assert result["session_id"] == "my-session-id"

    def test_session_id_falls_back_to_filename(self):
        entry = _make_jsonl_entry("assistant", text="Hello")
        entry.pop("sessionId", None)
        result = _normalize_turn(entry, "/fake/path/uuid-abc123.jsonl")
        assert result is not None
        assert result["session_id"] == "uuid-abc123"

    def test_content_capped_at_2000_chars(self):
        entry = _make_jsonl_entry("assistant", text="x" * 5000)
        result = _normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert len(result["content"]) <= 2000

    def test_tool_without_file_path_still_recorded(self):
        tools = [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]
        entry = _make_jsonl_entry("assistant", text="Running bash", tools=tools)
        result = _normalize_turn(entry, "/fake/session.jsonl")
        assert result is not None
        assert "Bash" in result["tool_uses"]
        assert result["files_touched"] == []


# ---------------------------------------------------------------------------
# _find_latest_session_file
# ---------------------------------------------------------------------------


class TestFindLatestSessionFile:
    """All tests set CLAUDE_PROJECTS_PATH so the security check passes for
    tmp_path-based fixtures."""

    def test_empty_dir_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        assert _find_latest_session_file(str(tmp_path)) is None

    def test_single_file_returned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        f = tmp_path / "session-abc.jsonl"
        f.write_text('{"type":"assistant"}\n')
        assert _find_latest_session_file(str(tmp_path)) == str(f)

    def test_most_recent_file_returned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        f1 = tmp_path / "old-session.jsonl"
        f1.write_text('{"type":"user"}\n')
        f2 = tmp_path / "new-session.jsonl"
        f2.write_text('{"type":"assistant"}\n')
        mtime = f2.stat().st_mtime
        os.utime(f1, (mtime - 60, mtime - 60))
        assert _find_latest_session_file(str(tmp_path)) == str(f2)

    def test_no_project_dir_scans_root(self, tmp_path, monkeypatch):
        """When no project_dir is provided, the whole root is scanned."""
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session = proj / "session-xyz.jsonl"
        session.write_text('{"type":"assistant"}\n')
        assert _find_latest_session_file() == str(session)

    def test_nonexistent_project_dir_falls_back_to_root(self, tmp_path, monkeypatch):
        """When project_dir doesn't exist, fall back to scanning the full root."""
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))
        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session = proj / "session-xyz.jsonl"
        session.write_text('{"type":"assistant"}\n')
        assert _find_latest_session_file(str(tmp_path / "no-such-dir")) == str(session)

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

        result = _find_latest_session_file(str(outside))
        assert result == str(good_session)
        assert result != str(bad_session)

    def test_no_projects_at_all_returns_none(self, tmp_path, monkeypatch):
        root = tmp_path / "projects"
        root.mkdir()
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(root))
        assert _find_latest_session_file(str(tmp_path / "no-such-dir")) is None

    def test_root_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path / "does-not-exist"))
        assert _find_latest_session_file() is None


# ---------------------------------------------------------------------------
# get_current_session_turns
# ---------------------------------------------------------------------------


class TestGetCurrentSessionTurns:
    """Live file parsing. CLAUDE_PROJECTS_PATH = tmp_path so the security check
    on project_dir passes."""

    @pytest.fixture(autouse=True)
    def _set_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

    def test_returns_last_n_turns(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        lines = [json.dumps(_make_jsonl_entry("assistant", text=f"Turn {i}")) for i in range(10)]
        session_file.write_text("\n".join(lines))

        result = get_current_session_turns(n=3, project_dir=str(tmp_path))
        assert len(result) == 3
        # Last 3 are turns 7, 8, 9
        assert "Turn 9" in result[-1]["content"]

    def test_handles_malformed_json_lines_gracefully(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        good_entry = json.dumps(_make_jsonl_entry("assistant", text="Good turn"))
        session_file.write_text(f"{{bad json line\n{good_entry}\n")

        result = get_current_session_turns(n=10, project_dir=str(tmp_path))
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

        result = get_current_session_turns(n=10, project_dir=str(tmp_path))
        assert len(result) == 1
        assert "Real turn" in result[0]["content"]

    def test_returns_empty_list_when_no_file(self, tmp_path):
        assert get_current_session_turns(n=5, project_dir=str(tmp_path / "no-such")) == []

    def test_oversized_file_is_skipped(self, tmp_path, monkeypatch):
        """Files over the size cap are refused (memory-exhaustion guard)."""
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="should not be read")))

        # Mock os.path.getsize to report a huge size
        monkeypatch.setattr(
            "os.path.getsize",
            lambda p: 100 * 1024 * 1024 if str(p) == str(session_file) else os.stat(p).st_size,
        )
        assert get_current_session_turns(n=5, project_dir=str(tmp_path)) == []


# ---------------------------------------------------------------------------
# _detect_project_from_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        # segments of "opt-helios-gateway-mvp" split by - → last 2 = "gateway_mvp"
        pytest.param("/home/user/.claude/projects/-opt-helios-gateway-mvp/uuid.jsonl", "gateway_mvp", id="last-two"),
        pytest.param("/home/user/.claude/projects/-singleword/uuid.jsonl", "singleword", id="one-segment"),
        pytest.param("/tmp/uuid.jsonl", "", id="no-projects-segment"),
    ],
)
def test_detect_project_from_path(path, expected):
    assert _detect_project_from_path(path) == expected


# ---------------------------------------------------------------------------
# state_store claude_code_turns functions (real schema via tmp_db)
# ---------------------------------------------------------------------------


def _make_turn(content="hello", project="test_proj", files=None, tools=None, delta_minutes=0):
    return {
        "timestamp": (datetime.now() + timedelta(minutes=delta_minutes)).isoformat(),
        "session_id": "sess-001",
        "project": project,
        "turn_type": "assistant",
        "content": content,
        "tool_uses": tools or [],
        "files_touched": files or [],
        "commit_hash": "",
    }


class TestStateStoreClaudeCodeTurns:
    def test_log_and_retrieve_turn(self, tmp_db):
        row_id = log_claude_code_turn(_make_turn(content="wrote some code"))
        assert isinstance(row_id, int)
        assert row_id > 0

        turns = get_claude_code_turns(since_minutes=5)
        assert len(turns) == 1
        assert turns[0]["content"] == "wrote some code"

    def test_time_filter_cuts_off_old_rows(self, tmp_db):
        log_claude_code_turn(_make_turn(content="ancient history", delta_minutes=-200))
        log_claude_code_turn(_make_turn(content="just now"))

        contents = [t["content"] for t in get_claude_code_turns(since_minutes=60)]
        assert "just now" in contents
        assert "ancient history" not in contents

    def test_project_filter(self, tmp_db):
        log_claude_code_turn(_make_turn(content="project A work", project="proj_a"))
        log_claude_code_turn(_make_turn(content="project B work", project="proj_b"))

        turns_a = get_claude_code_turns(since_minutes=60, project="proj_a")
        assert all(t["project"] == "proj_a" for t in turns_a)
        assert len(turns_a) == 1

    def test_limit_respected(self, tmp_db):
        for i in range(10):
            log_claude_code_turn(_make_turn(content=f"turn {i}"))
        assert len(get_claude_code_turns(since_minutes=60, limit=3)) == 3

    def test_tool_uses_and_files_decoded_as_lists(self, tmp_db):
        log_claude_code_turn(
            _make_turn(files=["/opt/gateway_mvp/foo.py", "/opt/gateway_mvp/bar.py"], tools=["Edit", "Bash"])
        )
        turns = get_claude_code_turns(since_minutes=5)
        assert isinstance(turns[0]["files_touched"], list)
        assert "/opt/gateway_mvp/foo.py" in turns[0]["files_touched"]
        assert isinstance(turns[0]["tool_uses"], list)
        assert "Edit" in turns[0]["tool_uses"]

    def test_get_files_touched_deduplicates(self, tmp_db):
        log_claude_code_turn(_make_turn(files=["/a/foo.py", "/a/bar.py"]))
        log_claude_code_turn(_make_turn(files=["/a/foo.py", "/a/baz.py"]))

        files = get_claude_code_files_touched(since_minutes=60)
        assert files.count("/a/foo.py") == 1
        assert "/a/bar.py" in files
        assert "/a/baz.py" in files

    def test_cleanup_deletes_old_turns(self, tmp_db):
        log_claude_code_turn(_make_turn(content="very old", delta_minutes=-60 * 24 * 10))  # 10 days ago
        log_claude_code_turn(_make_turn(content="recent"))

        assert cleanup_old_claude_code_turns(days=7) >= 1

        # Recent turn must still be there
        contents = [t["content"] for t in get_claude_code_turns(since_minutes=60 * 24 * 30)]
        assert "recent" in contents
        assert "very old" not in contents


# ---------------------------------------------------------------------------
# get_recent_activity_summary and get_files_touched (tracker level)
# ---------------------------------------------------------------------------


class TestActivitySummaryAndFilesTouched:
    """Fallback logic: the turn buffer first, else the live session file."""

    def test_summary_uses_buffer_when_populated(self):
        mock_turns = [
            {
                "timestamp": "2026-04-12T18:30:00",
                "turn_type": "assistant",
                "content": "I edited the config file",
                "tool_uses": ["Edit"],
                "files_touched": ["/opt/gateway_mvp/config.py"],
            }
        ]
        with patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=mock_turns):
            summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert "config.py" in summary
        assert "Edit" in summary

    def test_summary_falls_back_to_live_file_when_buffer_empty(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="Working on fallback feature")) + "\n")

        with (
            patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=[]),
            patch("orchestrator.claude_code_tracker._find_latest_session_file", return_value=str(session_file)),
        ):
            summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert "fallback feature" in summary

    def test_summary_returns_empty_string_when_both_sources_empty(self):
        with (
            patch("orchestrator.claude_code_tracker.get_claude_code_turns", return_value=[]),
            patch("orchestrator.claude_code_tracker.get_current_session_turns", return_value=[]),
        ):
            summary = tracker.get_recent_activity_summary(minutes_back=60)
        assert summary == ""

    def test_get_files_touched_uses_buffer_when_populated(self):
        with patch(
            "orchestrator.claude_code_tracker.get_claude_code_files_touched",
            return_value=["/opt/gateway_mvp/foo.py"],
        ):
            files = tracker.get_files_touched(minutes_back=60)
        assert "/opt/gateway_mvp/foo.py" in files

    def test_get_files_touched_falls_back_to_live_file(self, tmp_path):
        session_file = tmp_path / "session.jsonl"
        tools = [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/live/path/module.py"}}]
        session_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="editing", tools=tools)) + "\n")

        with (
            patch("orchestrator.claude_code_tracker.get_claude_code_files_touched", return_value=[]),
            patch("orchestrator.claude_code_tracker._find_latest_session_file", return_value=str(session_file)),
        ):
            files = tracker.get_files_touched(minutes_back=60)
        assert "/live/path/module.py" in files


# ---------------------------------------------------------------------------
# log_turn_from_hook (buffer ingestion)
# ---------------------------------------------------------------------------


class TestLogTurnFromHook:
    """Security-critical behavior: transcript_path is IGNORED entirely. The
    function uses session_id (UUID-validated) to locate the file itself,
    constrained to CLAUDE_PROJECTS_PATH. These tests exercise both the
    session-id-based path and the direct-payload fallback, and verify
    that adversarial payloads never read files outside the root.
    """

    def test_session_id_resolves_file_under_root(self, tmp_path, monkeypatch):
        """Valid UUID session_id + matching file → parsed and logged."""
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

        proj = tmp_path / "-opt-helios-gateway-mvp"
        proj.mkdir()
        session_id = "b59382fb-b98a-404a-a732-c74e20c69073"
        session_file = proj / f"{session_id}.jsonl"
        session_file.write_text(json.dumps(_make_jsonl_entry("assistant", text="Committed new changes")) + "\n")

        payload = {"session_id": session_id, "transcript_path": "/ignored"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=7) as mock_log:
            result = tracker.log_turn_from_hook(payload)

        assert result == 7
        call_args = mock_log.call_args[0][0]
        assert "Committed new changes" in call_args["content"]
        assert call_args["session_id"] == session_id

    def test_direct_payload_fallback_when_session_id_missing(self):
        """No session_id → falls through to direct payload with coercion."""
        payload = {
            "project": "gateway_mvp",
            "turn_type": "assistant",
            "content": "Fixed the bug",
            "tool_uses": ["Edit"],
            "files_touched": ["/opt/gateway_mvp/fix.py"],
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
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

        payload = {"session_id": "abcdefgh-ijkl-mnop-qrst-uvwxyz123456", "content": "fallback content"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=5) as mock_log:
            result = tracker.log_turn_from_hook(payload)
        assert result == 5
        assert mock_log.call_args[0][0]["content"] == "fallback content"

    def test_path_traversal_session_id_rejected(self, tmp_path, monkeypatch):
        """A session_id with path-traversal chars is rejected by the regex;
        function falls through to the direct-payload branch without reading
        any file outside the root.
        """
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
        payload = {"session_id": "*", "content": "attacker payload"}
        with patch("orchestrator.claude_code_tracker.log_claude_code_turn", return_value=1) as mock_log:
            tracker.log_turn_from_hook(payload)
        # Should use direct payload fallback, not try to glob
        assert mock_log.call_args[0][0]["content"] == "attacker payload"

    def test_non_list_tool_uses_coerced_safely(self):
        """A payload with non-list tool_uses should not crash; coerces to []."""
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
# Security helpers: path safety and file size (review blockers around path
# traversal and memory exhaustion)
# ---------------------------------------------------------------------------


class TestResolveUnderRoot:
    @pytest.fixture
    def root(self, tmp_path, monkeypatch):
        # A couple of levels deep so ".." can actually escape it.
        root = tmp_path / "a" / "b" / "projects"
        (root / "subdir").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(root))
        return root

    @pytest.mark.parametrize("rel", [pytest.param("subdir", id="inside"), pytest.param("", id="root-itself")])
    def test_accepts_paths_under_root(self, root, rel):
        path = root / rel if rel else root
        assert _resolve_under_root(str(path)) == str(path.resolve())

    @pytest.mark.parametrize(
        "make_path",
        [
            pytest.param(lambda root: "/etc/passwd", id="etc-passwd"),
            # realpath on the traversal resolves it outside the root
            pytest.param(lambda root: str(root / ".." / ".." / ".." / "etc"), id="dotdot-traversal"),
            pytest.param(lambda root: "", id="empty"),
            pytest.param(lambda root: None, id="none"),
        ],
    )
    def test_rejects(self, root, make_path):
        assert _resolve_under_root(make_path(root)) is None


class TestFindSessionFileById:
    @pytest.fixture(autouse=True)
    def _set_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECTS_PATH", str(tmp_path))

    def test_valid_uuid(self, tmp_path):
        proj = tmp_path / "-test"
        proj.mkdir()
        session_id = "b59382fb-b98a-404a-a732-c74e20c69073"
        session_file = proj / f"{session_id}.jsonl"
        session_file.write_text("{}\n")
        assert _find_session_file_by_id(session_id) == str(session_file.resolve())

    @pytest.mark.parametrize(
        "session_id",
        [
            pytest.param("../../etc/passwd", id="path-traversal"),
            pytest.param("*", id="glob-wildcard"),
            pytest.param("", id="empty"),
            # The regex requires at least 8 chars to reject trivial strings.
            pytest.param("short", id="too-short"),
            # Valid id, but no such file.
            pytest.param("valid-session-1234", id="no-match"),
        ],
    )
    def test_returns_none(self, session_id):
        assert _find_session_file_by_id(session_id) is None


class TestIsReasonableSize:
    def test_accepts_small_file(self, tmp_path):
        f = tmp_path / "tiny.jsonl"
        f.write_text("hello\n")
        assert _is_reasonable_size(str(f)) is True

    def test_rejects_missing_file(self):
        assert _is_reasonable_size("/no/such/file.jsonl") is False

    def test_rejects_oversized(self, tmp_path, monkeypatch):
        f = tmp_path / "fake-huge.jsonl"
        f.write_text("x")
        # Mock getsize to report 100MB (over the 50MB cap)
        monkeypatch.setattr("os.path.getsize", lambda p: 100 * 1024 * 1024)
        assert _is_reasonable_size(str(f)) is False
