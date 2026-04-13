"""
Claude Code session tracker.

Two data sources for awareness of what Claude Code has been doing:

1. **Live session file** — reads the most recent .jsonl file from the
   mounted Claude Code projects directory for up-to-the-minute context.
   No sync, no storage, truth always fresh.

2. **SQLite rolling buffer** — Claude Code Stop hooks POST each completed
   turn to /api/claude_code/turn, which logs it here. Gives proactive
   awareness without re-parsing the session file each time.

Both sources are used by the `check_claude_activity` Jess tool and the
code_agent prompt builder.

**Security model:** all file reads are constrained to a single configured
root (`CLAUDE_PROJECTS_PATH`, defaults to `/root/.claude/projects` inside
the container). Any caller-supplied path — from a Stop hook, tool call,
or REST endpoint — is rejected if it does not resolve (via `os.path.realpath`)
under that root. The hook's `transcript_path` field is never trusted
directly; we use `session_id` (validated as a UUID-like string) to locate
the file ourselves.
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

# File-editing tools that indicate Claude Code touched a file
_EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "Update"}

# UUID-like session IDs only (prevents shell/path injection via the hook)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")

# Hard cap on session file size we're willing to read. Real session files
# are typically a few MB; anything dramatically larger is either adversarial
# or a bug — reading it would risk memory exhaustion.
_MAX_SESSION_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def _is_reasonable_size(filepath: str) -> bool:
    """Return True iff `filepath` exists and is within the size cap."""
    try:
        return os.path.getsize(filepath) <= _MAX_SESSION_FILE_BYTES
    except OSError:
        return False


def _claude_projects_root() -> str:
    """Return the container-side Claude Code projects directory.

    Reads from the CLAUDE_PROJECTS_PATH env var, falling back to the
    conventional container mount point. This is the ONLY trusted root —
    all session file reads must resolve under this path.
    """
    return os.environ.get("CLAUDE_PROJECTS_PATH", "/root/.claude/projects")


def _resolve_under_root(path: str) -> Optional[str]:
    """Return an absolute realpath iff it stays under the projects root.

    Returns None if the path escapes the root, is missing, or is malformed.
    Uses `os.path.realpath` to defeat symlink + ../ traversal attempts.
    """
    if not path:
        return None
    root = os.path.realpath(_claude_projects_root())
    try:
        resolved = os.path.realpath(path)
    except (ValueError, OSError):
        return None
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def _most_recent_jsonl(directory: str) -> Optional[str]:
    """Return the most recently modified .jsonl file in `directory` (recursive)."""
    try:
        files = glob.glob(os.path.join(directory, "**", "*.jsonl"), recursive=True)
    except Exception:
        return None
    if not files:
        return None
    try:
        return max(files, key=os.path.getmtime)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session file parsing (live reads, no DB)
# ---------------------------------------------------------------------------


def _find_latest_session_file(project_dir: Optional[str] = None) -> Optional[str]:
    """Find the most recently modified session .jsonl under the projects root.

    If `project_dir` is provided, it is validated to be under the configured
    root and, if valid, scoped to that subdirectory. Otherwise all projects
    under the root are scanned and the globally most-recent file wins.

    Unlike the previous implementation, this does NOT use `os.getcwd()` —
    the container's CWD (`/app`) is unrelated to the Claude Code project
    encoding (`-opt-helios-gateway-mvp`). Path resolution is anchored to
    a fixed configured root instead.
    """
    root = _claude_projects_root()
    if not os.path.isdir(root):
        return None

    scope = root
    if project_dir:
        resolved = _resolve_under_root(project_dir)
        if resolved and os.path.isdir(resolved):
            scope = resolved
        else:
            logger.debug("[CC_TRACKER] project_dir outside root, scanning root: %s", project_dir)

    return _most_recent_jsonl(scope)


def _find_session_file_by_id(session_id: str) -> Optional[str]:
    """Locate a session .jsonl by its UUID-like id under the projects root.

    Validates the id matches a safe character set before interpolating it
    into a glob pattern (prevents injection via `*`, `/`, `..`, etc.).
    """
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return None

    root = _claude_projects_root()
    if not os.path.isdir(root):
        return None

    try:
        matches = glob.glob(
            os.path.join(root, "**", f"{session_id}.jsonl"),
            recursive=True,
        )
    except Exception:
        return None
    if not matches:
        return None

    # Defense in depth: realpath check on the result
    return _resolve_under_root(matches[0])


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
    if not _is_reasonable_size(filepath):
        logger.warning("[CC_TRACKER] Session file exceeds size cap, skipping: %s", filepath)
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

    The hook POSTs JSON from Claude Code with keys including `session_id` and
    `transcript_path`. The path field is **ignored entirely** — Claude Code
    runs on the host and sends host-absolute paths that don't exist inside
    the container, and accepting arbitrary paths from the hook would allow
    an attacker with a valid API token (or a prompt-injected LLM) to read
    arbitrary files into the palace via this endpoint.

    Instead, we use `session_id` (validated as UUID-like) to locate the
    file ourselves, constrained to the configured projects root.
    """
    session_id = str(payload.get("session_id") or "").strip()

    # Preferred path: resolve session file from UUID under our root
    if session_id:
        session_file = _find_session_file_by_id(session_id)
        if session_file:
            try:
                turns = _parse_session_file_for_hook(session_file)
                if turns:
                    latest = turns[-1]
                    latest["session_id"] = session_id
                    latest["project"] = _detect_project_from_path(session_file)
                    return log_claude_code_turn(latest)
            except Exception as e:
                logger.warning("[CC_TRACKER] Failed to parse session %s: %s", session_id, e)

    # Fallback: store whatever the hook sent directly (no file read).
    # We defensively coerce list fields and cap string lengths so a malformed
    # payload can't blow up state_store.
    def _as_list(value: Any) -> List[Any]:
        return value if isinstance(value, list) else []

    turn = {
        "timestamp": payload.get("timestamp") or datetime.now().isoformat(),
        "session_id": session_id,
        "project": str(payload.get("project") or "")[:200],
        "turn_type": str(payload.get("turn_type") or "assistant")[:20],
        "content": str(payload.get("content") or "")[:10000],
        "tool_uses": _as_list(payload.get("tool_uses")),
        "files_touched": _as_list(payload.get("files_touched")),
        "commit_hash": str(payload.get("commit_hash") or "")[:64],
    }
    return log_claude_code_turn(turn)


def _parse_session_file_for_hook(filepath: str, max_turns: int = 2) -> List[Dict[str, Any]]:
    """Parse a session file and return the last max_turns assistant turns."""
    if not _is_reasonable_size(filepath):
        logger.warning("[CC_TRACKER] Hook session file exceeds size cap, skipping: %s", filepath)
        return []
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
