"""
Claude Code session tracker.

Two data sources for awareness of what Claude Code has been doing:

1. **Live session file** — reads the most recent .jsonl file from
   ~/.claude/projects/<project>/ for up-to-the-minute context. No sync,
   no storage, truth always fresh.

2. **SQLite rolling buffer** — Claude Code Stop hooks POST each completed
   turn to /api/claude_code/turn, which logs it here. Gives proactive
   awareness without re-parsing the session file each time.

Both sources are used by the `check_claude_activity` Jess tool and the
code_agent prompt builder.
"""

import glob
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator.state_store import (
    get_claude_code_files_touched,
    get_claude_code_turns,
    log_claude_code_turn,
)

logger = logging.getLogger(__name__)

# Default location for Claude Code session files on the host
_DEFAULT_SESSION_ROOT = os.path.expanduser("~/.claude/projects")

# File-editing tools that indicate Claude Code touched a file
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "Update"}


# ---------------------------------------------------------------------------
# Session file parsing (live reads, no DB)
# ---------------------------------------------------------------------------


def _find_latest_session_file(project_dir: Optional[str] = None) -> Optional[str]:
    """Find the most recently modified session .jsonl file for a project.

    If project_dir is None, searches the current project directory by
    matching the current working directory to Claude Code's path-encoded
    project name (e.g. /opt/helios/gateway_mvp -> -opt-helios-gateway-mvp).
    """
    if project_dir is None:
        # Derive from current working directory
        cwd = os.getcwd()
        encoded = cwd.replace("/", "-")
        project_dir = os.path.join(_DEFAULT_SESSION_ROOT, encoded)

    if not os.path.isdir(project_dir):
        # Fall back to scanning all projects
        all_projects = sorted(
            glob.glob(os.path.join(_DEFAULT_SESSION_ROOT, "*")),
            key=os.path.getmtime,
            reverse=True,
        )
        if not all_projects:
            return None
        project_dir = all_projects[0]

    session_files = sorted(
        glob.glob(os.path.join(project_dir, "**", "*.jsonl"), recursive=True),
        key=os.path.getmtime,
        reverse=True,
    )
    return session_files[0] if session_files else None


def get_current_session_turns(n: int = 10, project_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Parse the most recent live session file and return the last N turn pairs.

    Returns a list of dicts with: timestamp, turn_type, content, tool_uses, files_touched.
    This is the freshest view — reads directly from disk each call.

    Claude Code session .jsonl format:
      Each line is an entry with a top-level `type` field. Entry types include
      "user", "assistant", "attachment", "permission-mode", "file-history-snapshot",
      "tool_result", etc. User and assistant entries have a nested `message` dict
      with `role` and `content` (where content is a list of parts: text, thinking,
      tool_use, tool_result).
    """
    filepath = _find_latest_session_file(project_dir)
    if not filepath:
        return []

    turns: List[Dict[str, Any]] = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                if entry_type not in ("user", "assistant"):
                    continue

                turn = _normalize_turn(entry, filepath)
                if turn:
                    turns.append(turn)
    except Exception as e:
        logger.warning("[CC_TRACKER] Failed to read %s: %s", filepath, e)
        return []

    # Return the last N turns
    return turns[-n:] if n > 0 else turns


def _normalize_turn(entry: Dict, filepath: str) -> Optional[Dict[str, Any]]:
    """Normalize a raw session entry into a flat turn dict.

    Real session format nests the actual message under `message`:
      {type: "assistant", timestamp: "...", message: {role, content: [...]}, ...}
    Content is a list of parts with types: text, thinking, tool_use, tool_result.
    """
    entry_type = entry.get("type", "")
    message = entry.get("message") or {}
    if not isinstance(message, dict):
        return None

    content = message.get("content", "")
    tool_uses: List[str] = []
    files_touched: List[str] = []
    text_parts: List[str] = []

    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                text_parts.append(part.get("text", ""))
            elif ptype == "thinking":
                # Skip thinking blocks — internal reasoning, not user-facing
                continue
            elif ptype == "tool_use":
                tool_name = part.get("name", "")
                if tool_name:
                    tool_uses.append(tool_name)
                # Extract file path from file-editing tools
                if tool_name in _EDIT_TOOLS:
                    tool_input = part.get("input", {})
                    if isinstance(tool_input, dict):
                        path = tool_input.get("file_path") or tool_input.get("path")
                        if path:
                            files_touched.append(path)
    elif isinstance(content, str):
        text_parts.append(content)

    text_content = "\n".join(t for t in text_parts if t).strip()

    if not text_content and not tool_uses:
        return None

    return {
        "timestamp": entry.get("timestamp", ""),
        "session_id": entry.get("sessionId") or os.path.basename(filepath).replace(".jsonl", ""),
        "turn_type": entry_type,
        "content": text_content[:2000],  # cap
        "tool_uses": tool_uses,
        "files_touched": files_touched,
    }


# ---------------------------------------------------------------------------
# Buffer (Stop hook) ingestion
# ---------------------------------------------------------------------------


def log_turn_from_hook(payload: Dict[str, Any]) -> int:
    """Accept a turn payload from the Stop hook and store it in the buffer.

    The hook POSTs JSON from Claude Code with keys like `transcript_path`,
    `session_id`, etc. This function normalizes and stores it.
    """
    # If transcript path is provided, re-parse the latest turn from it
    transcript_path = payload.get("transcript_path", "")
    if transcript_path and os.path.exists(transcript_path):
        try:
            # Read the last assistant turn from the transcript
            turns_from_file = _parse_session_file_for_hook(transcript_path)
            if turns_from_file:
                latest = turns_from_file[-1]
                latest["session_id"] = payload.get("session_id") or latest.get("session_id", "")
                latest["project"] = _detect_project_from_path(transcript_path)
                return log_claude_code_turn(latest)
        except Exception as e:
            logger.warning("[CC_TRACKER] Failed to parse transcript %s: %s", transcript_path, e)

    # Fallback: use whatever the hook sent directly
    turn = {
        "timestamp": payload.get("timestamp") or datetime.now().isoformat(),
        "session_id": payload.get("session_id", ""),
        "project": payload.get("project", ""),
        "turn_type": payload.get("turn_type", "assistant"),
        "content": payload.get("content", ""),
        "tool_uses": payload.get("tool_uses", []),
        "files_touched": payload.get("files_touched", []),
        "commit_hash": payload.get("commit_hash", ""),
    }
    return log_claude_code_turn(turn)


def _parse_session_file_for_hook(filepath: str, max_turns: int = 2) -> List[Dict[str, Any]]:
    """Parse a session file and return the last max_turns assistant turns."""
    turns: List[Dict[str, Any]] = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            turn = _normalize_turn(entry, filepath)
            if turn:
                turns.append(turn)
    return turns[-max_turns:] if turns else []


def _detect_project_from_path(filepath: str) -> str:
    """Extract project name from a Claude Code session file path.

    Paths look like: ~/.claude/projects/-opt-helios-gateway-mvp/uuid.jsonl
    """
    parts = filepath.split(os.sep)
    for part in parts:
        if part.startswith("-") and len(part) > 5:
            segments = part.strip("-").split("-")
            if segments:
                return "_".join(segments[-2:]) if len(segments) > 1 else segments[0]
    return ""


# ---------------------------------------------------------------------------
# Summary generation (for LLM context injection)
# ---------------------------------------------------------------------------


def get_recent_activity_summary(minutes_back: int = 120, max_chars: int = 1500) -> str:
    """Generate a compact summary of recent Claude Code activity.

    Used by:
    - `check_claude_activity` Jess tool (when action=recent)
    - code_agent system prompt (automatic context injection)

    Pulls from the SQLite buffer first, falls back to live session file if empty.
    """
    turns = get_claude_code_turns(since_minutes=minutes_back, limit=20)

    # If buffer is empty, fall back to live session file
    if not turns:
        live_turns = get_current_session_turns(n=10)
        if not live_turns:
            return ""
        turns = live_turns

    # Build summary
    lines = [f"Recent Claude Code activity (last {minutes_back} min):"]
    files_seen = set()
    for turn in turns:
        ts = (turn.get("timestamp") or "")[:16]
        turn_type = turn.get("turn_type", "")
        content = (turn.get("content") or "")[:300]
        tool_uses = turn.get("tool_uses") or []
        files = turn.get("files_touched") or []

        if turn_type == "assistant":
            prefix = f"  [{ts}]"
            if tool_uses:
                tool_summary = ", ".join(t for t in tool_uses[:5])
                lines.append(f"{prefix} tools: {tool_summary}")
            if files:
                for f in files:
                    if f not in files_seen:
                        files_seen.add(f)
                        lines.append(f"{prefix} edited: {f}")
            if content:
                # First sentence or first 200 chars
                first_line = content.split("\n")[0][:200]
                lines.append(f"{prefix} said: {first_line}")

    # Add files-touched summary
    if files_seen:
        lines.append(f"\nFiles touched ({len(files_seen)}): {', '.join(list(files_seen)[:10])}")

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n  [...truncated]"
    return summary


def get_files_touched(minutes_back: int = 60) -> List[str]:
    """Return a list of files Claude Code has edited in the time window.

    Checks the SQLite buffer first, then falls back to scanning the live
    session file if the buffer is empty (e.g., no Stop hook configured).
    """
    files = get_claude_code_files_touched(since_minutes=minutes_back)
    if files:
        return files

    # Fallback to live session file
    turns = get_current_session_turns(n=50)
    seen = set()
    result = []
    for turn in turns:
        for f in turn.get("files_touched", []) or []:
            if f and f not in seen:
                seen.add(f)
                result.append(f)
    return result
