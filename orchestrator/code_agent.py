"""
Code Agent — Jess's coding assistant for self-troubleshooting and development.

Delegates coding tasks to a separate coding-focused model (Qwen2.5-Coder-32B
on Saturn) with filesystem tools. Runs an inner agentic loop with read_file,
write_file, search_code, list_files, and run_command tools.

Read-only by default. Set apply_changes=true to allow file writes.
Git checkpoint is created before any write operation.
"""

import json
import logging
import re
import shlex
import subprocess
from pathlib import Path

import httpx

from orchestrator import shared
from orchestrator.metrics import CODE_AGENT_PREFLIGHT_FAILURES
from orchestrator.tool_registry import register_tool

logger = logging.getLogger(__name__)

# Short-timeout reachability probe for the code model. The model runs on Helios,
# which is powered down most of the time; connect must fail fast so preflight
# doesn't itself stall (see _code_model_reachable / handle_code_agent).
_PREFLIGHT_TIMEOUT = httpx.Timeout(4.0, connect=2.0)


async def _code_model_reachable(model_url: str) -> bool:
    """Return True if the code model's OpenAI-compatible endpoint answers quickly.

    Probes ``GET {model_url}/models`` with a short timeout. Any connection error,
    timeout, or 5xx is treated as unreachable so the caller can fail fast with an
    actionable message instead of entering the agent loop against a dead endpoint
    (where every one of up to CODE_AGENT_MAX_ROUNDS rounds would hang on connect).
    """
    url = f"{model_url.rstrip('/')}/models"
    try:
        # Reuse the shared pooled client (same idiom as the /models liveness
        # probe in api_routes.py); fall back to a one-off client only in the
        # unlikely case this runs before startup initialized it.
        if shared._http is not None:
            resp = await shared._http.get(url, timeout=_PREFLIGHT_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=_PREFLIGHT_TIMEOUT) as client:
                resp = await client.get(url)
        return resp.status_code < 500
    except Exception as e:
        logger.info("[CODE_AGENT] Preflight probe failed for %s: %s", url, e)
        return False

# Allowlisted commands for run_command, as argv-token prefixes. Commands are
# tokenized with shlex and executed WITHOUT a shell, so chaining (`;`, `&&`,
# `|`), redirection, and substitution have no effect — the old string-prefix
# check ran `shell=True`, and `cat foo; <anything>` sailed through on the
# "cat " prefix. `docker exec` was dropped: it is arbitrary root execution in
# any container, which defeats the point of an allowlist.
_COMMAND_ALLOWLIST: list[list[str]] = [
    ["python", "-m", "pytest"],
    ["pytest"],
    ["docker", "logs"],
    ["git", "diff"],
    ["git", "log"],
    ["git", "status"],
    ["git", "show"],
    ["cat"],
    ["head"],
    ["tail"],
    ["wc"],
    ["grep"],
    ["rg"],
    ["find"],
    ["ls"],
    ["systemctl", "status"],
    ["journalctl"],
    ["curl"],
]

# find can execute arbitrary binaries or delete files through these.
_FIND_BLOCKED_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"}

# curl is for GET-ing LAN debug endpoints only: no local file writes, no
# request bodies (which can read local files via @path), no method override.
_CURL_BLOCKED_FLAGS = {
    "-o",
    "-O",
    "--output",
    "--output-dir",
    "-T",
    "--upload-file",
    "-d",
    "--data",
    "--data-binary",
    "--data-raw",
    "--data-urlencode",
    "--json",
    "-F",
    "--form",
    "-K",
    "--config",
    "-X",
    "--request",
    "-u",
    "--user",
}
_CURL_URL_PREFIXES = ("http://localhost", "http://127.0.0.1", "http://10.0.0.")


def _command_rejection(argv: list[str]) -> str | None:
    """Return a rejection reason for an argv, or None if it is allowed."""
    if not argv:
        return "empty command"
    for prefix in _COMMAND_ALLOWLIST:
        if argv[: len(prefix)] == prefix:
            break
    else:
        allowed = ", ".join(" ".join(p) for p in _COMMAND_ALLOWLIST[:10])
        return f"command not allowed. Permitted commands: {allowed}..."
    if argv[0] == "find":
        blocked = _FIND_BLOCKED_FLAGS.intersection(argv)
        if blocked:
            return f"find flag not allowed: {sorted(blocked)[0]}"
    if argv[0] == "curl":
        blocked = _CURL_BLOCKED_FLAGS.intersection(argv)
        if blocked:
            return f"curl flag not allowed: {sorted(blocked)[0]}"
        urls = [a for a in argv if "://" in a]
        if not urls or not all(u.startswith(_CURL_URL_PREFIXES) for u in urls):
            return "curl is restricted to http://localhost / http://127.0.0.1 / http://10.0.0.* URLs"
    return None


# ---------------------------------------------------------------------------
# Inner tools for the coding agent
# ---------------------------------------------------------------------------

_CODEBASE = shared.CODE_AGENT_CODEBASE_PATH


def _inner_read_file(arguments: dict) -> str:
    """Read file contents with optional line range."""
    path = arguments.get("path", "")
    start = arguments.get("start_line")
    end = arguments.get("end_line")

    full_path = Path(_CODEBASE) / path
    if not full_path.resolve().is_relative_to(Path(_CODEBASE).resolve()):
        return f"Error: path {path} is outside the codebase"
    if not full_path.exists():
        return f"Error: file not found: {path}"
    if full_path.is_dir():
        return f"Error: {path} is a directory, use list_files instead"

    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if start is not None or end is not None:
            s = max(0, (int(start) - 1) if start else 0)
            e = int(end) if end else len(lines)
            lines = lines[s:e]
            header = f"[{path} lines {s + 1}-{min(e, len(lines) + s)}]"
        else:
            if len(lines) > 500:
                lines = lines[:500]
                header = f"[{path} first 500 of {len(lines)} lines]"
            else:
                header = f"[{path} ({len(lines)} lines)]"
        numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
        return f"{header}\n" + "\n".join(numbered)
    except Exception as e:
        return f"Error reading {path}: {e}"


def _inner_write_file(arguments: dict, apply_changes: bool) -> str:
    """Write file contents. Creates git safety checkpoint first."""
    if not apply_changes:
        return "Error: apply_changes is false — write operations are not permitted in read-only mode. Ask the user to enable apply_changes."

    path = arguments.get("path", "")
    content = arguments.get("content", "")

    full_path = Path(_CODEBASE) / path
    if not full_path.resolve().is_relative_to(Path(_CODEBASE).resolve()):
        return f"Error: path {path} is outside the codebase"

    # Git safety checkpoint
    try:
        _git_checkpoint()
    except Exception as e:
        logger.warning(f"[CODE_AGENT] Git checkpoint failed: {e}")

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing {path}: {e}"


def _inner_search_code(arguments: dict) -> str:
    """Search codebase with grep/ripgrep."""
    pattern = arguments.get("pattern", "")
    glob_filter = arguments.get("glob", "*.py")
    max_results = arguments.get("max_results", 30)

    if not pattern:
        return "Error: pattern is required"

    try:
        # Try ripgrep first, fall back to grep
        cmd = ["rg", "-n", "--glob", glob_filter, "-m", str(max_results), pattern, _CODEBASE]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            # Strip codebase prefix for cleaner output
            output = result.stdout.replace(f"{_CODEBASE}/", "")
            lines = output.strip().splitlines()
            return f"[{len(lines)} matches]\n" + "\n".join(lines[:max_results])
        elif result.returncode == 1:
            return "No matches found."
        # ripgrep not available, try grep
        cmd = ["grep", "-rn", f"--include={glob_filter}", pattern, _CODEBASE]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = result.stdout.replace(f"{_CODEBASE}/", "")
        lines = output.strip().splitlines()
        return f"[{len(lines)} matches]\n" + "\n".join(lines[:max_results])
    except subprocess.TimeoutExpired:
        return "Error: search timed out"
    except Exception as e:
        return f"Error searching: {e}"


def _inner_list_files(arguments: dict) -> str:
    """List directory contents or glob pattern."""
    path = arguments.get("path", ".")
    pattern = arguments.get("pattern")

    target = Path(_CODEBASE) / path
    if not target.resolve().is_relative_to(Path(_CODEBASE).resolve()):
        return f"Error: path {path} is outside the codebase"

    try:
        if pattern:
            matches = sorted(target.glob(pattern))
            items = [str(m.relative_to(Path(_CODEBASE))) for m in matches[:100]]
        elif target.is_file():
            return f"{path} is a file ({target.stat().st_size} bytes)"
        else:
            items = sorted(
                str(p.relative_to(Path(_CODEBASE))) + ("/" if p.is_dir() else "")
                for p in target.iterdir()
                if not p.name.startswith(".")
            )
        return "\n".join(items[:100]) if items else "Empty directory."
    except Exception as e:
        return f"Error listing {path}: {e}"


def _inner_run_command(arguments: dict) -> str:
    """Run an allowlisted command (no shell — see _command_rejection)."""
    command = arguments.get("command", "")

    try:
        argv = shlex.split(command)
    except ValueError as parse_err:
        return f"Error: could not parse command: {parse_err}"

    reason = _command_rejection(argv)
    if reason:
        return f"Error: {reason}"

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=_CODEBASE,
        )
        output = result.stdout[-3000:] if len(result.stdout) > 3000 else result.stdout
        stderr = result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr
        parts = []
        if output.strip():
            parts.append(output.strip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.strip()}")
        parts.append(f"[exit code: {result.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return "Error: command timed out (60s limit)"
    except Exception as e:
        return f"Error running command: {e}"


def _git_checkpoint():
    """Create a git safety checkpoint before writing files."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=_CODEBASE,
        timeout=10,
    )
    if result.stdout.strip():
        subprocess.run(
            ["git", "add", "-A"],
            cwd=_CODEBASE,
            timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", "code_agent: safety checkpoint before changes"],
            cwd=_CODEBASE,
            timeout=10,
        )
        logger.info("[CODE_AGENT] Git safety checkpoint created")


# ---------------------------------------------------------------------------
# Inner tool definitions (for the coding model)
# ---------------------------------------------------------------------------

_INNER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents from the Brain Gateway codebase. Paths are relative to the project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative file path (e.g. 'orchestrator/selfcare_manager.py')",
                    },
                    "start_line": {"type": "integer", "description": "Start line number (1-indexed, optional)"},
                    "end_line": {"type": "integer", "description": "End line number (inclusive, optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Only available when apply_changes is true. Creates a git checkpoint first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Full file contents to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search for a pattern across the codebase using regex.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "glob": {"type": "string", "description": "File glob filter (default: '*.py')"},
                    "max_results": {"type": "integer", "description": "Max results to return (default: 30)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List directory contents or match a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to project root (default: '.')"},
                    "pattern": {"type": "string", "description": "Glob pattern to match (e.g. '**/*.py')"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run an allowlisted shell command (pytest, docker logs, git diff/log, grep, etc.)",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
        },
    },
]


def _execute_inner_tool(tool_name: str, arguments: dict, apply_changes: bool) -> str:
    """Execute an inner coding tool."""
    handlers = {
        "read_file": lambda args: _inner_read_file(args),
        "write_file": lambda args: _inner_write_file(args, apply_changes),
        "search_code": lambda args: _inner_search_code(args),
        "list_files": lambda args: _inner_list_files(args),
        "run_command": lambda args: _inner_run_command(args),
    }
    handler = handlers.get(tool_name)
    if not handler:
        return f"Unknown tool: {tool_name}"
    try:
        return handler(arguments)
    except Exception as e:
        return f"Error in {tool_name}: {e}"


# ---------------------------------------------------------------------------
# System prompt for the coding model
# ---------------------------------------------------------------------------


def _build_coding_prompt(task: str, apply_changes: bool) -> str:
    """Build the system prompt for the coding agent."""
    mode = "READ-WRITE (apply_changes=true)" if apply_changes else "READ-ONLY (apply_changes=false)"

    # Load CLAUDE.md for architecture context
    claude_md = ""
    claude_path = Path(_CODEBASE) / "CLAUDE.md"
    if claude_path.exists():
        try:
            content = claude_path.read_text(encoding="utf-8")
            # Truncate to key sections
            if len(content) > 6000:
                content = content[:6000] + "\n... (truncated)"
            claude_md = f"\n\nCODEBASE ARCHITECTURE:\n{content}"
        except Exception:
            pass

    # Pull recent Claude Code activity for context (what the primary coding
    # assistant has been doing recently). This gives the local code_agent
    # awareness of in-flight changes that may not yet be in git.
    cc_activity = ""
    try:
        from orchestrator.claude_code_tracker import get_recent_activity_summary

        summary = get_recent_activity_summary(minutes_back=180, max_chars=1200)
        if summary:
            cc_activity = f"\n\n{summary}\n(Claude Code is the primary coding assistant. Review this activity to understand recent in-flight work before making changes.)"
    except Exception:
        pass

    return f"""You are a coding agent for the Brain Gateway project (a personal AI assistant).
Your job is to investigate, diagnose, and optionally fix issues in the codebase.

MODE: {mode}
{"You CAN read and write files, search code, and run commands." if apply_changes else "You can read files, search code, and run commands. You CANNOT write files."}

TASK: {task}

GUIDELINES:
- Be thorough: read relevant files, search for patterns, check logs
- Be precise: reference exact file paths and line numbers
- Be concise: summarize findings clearly
- If you find an issue, explain the root cause and propose a fix
- {"When writing files, include the complete file contents (not just the changed lines)" if apply_changes else "Since this is read-only mode, describe what changes would fix the issue"}
- Always end with a clear summary of what you found and what to do next
- If recent Claude Code activity (below) shows in-flight changes to files you're touching, be careful not to overwrite them
{claude_md}{cc_activity}"""


# ---------------------------------------------------------------------------
# Inner agentic loop
# ---------------------------------------------------------------------------


async def _run_code_agent_loop(
    task: str,
    apply_changes: bool = False,
    max_rounds: int = None,
) -> str:
    """Run the inner agentic loop with the coding model."""
    from orchestrator.orchestrator import call_model

    model_url = shared.CODE_AGENT_MODEL_URL
    model_name = shared.CODE_AGENT_MODEL_NAME
    max_rounds = max_rounds or shared.CODE_AGENT_MAX_ROUNDS

    if not model_url:
        return "Code agent is not configured. Set CODE_AGENT_MODEL_URL in .env."

    system_prompt = _build_coding_prompt(task, apply_changes)
    messages = [{"role": "user", "content": task}]

    tools = _INNER_TOOLS if apply_changes else [t for t in _INNER_TOOLS if t["function"]["name"] != "write_file"]

    for round_num in range(max_rounds):
        logger.info("[CODE_AGENT] Round %d/%d", round_num + 1, max_rounds)

        try:
            llm_resp = await call_model(
                model_url,
                model_name,
                messages,
                system=system_prompt,
                tools=tools,
                tool_choice="auto",
                timeout=120,
            )
        except Exception as e:
            logger.error("[CODE_AGENT] Model call failed: %s", e)
            return f"Code agent model call failed: {e}"

        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []
        content = message.get("content") or ""

        # Parse XML fallback
        if not tool_calls and content:
            tool_calls = _parse_xml_tool_calls(content)

        # No tool calls — return final response
        if not tool_calls:
            result = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            logger.info("[CODE_AGENT] Final response (%d rounds): %s...", round_num + 1, result[:100])
            return result

        # Execute tools
        logger.info("[CODE_AGENT] Executing %d tool(s)", len(tool_calls))
        assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
        messages.append(assistant_msg)

        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            result = _execute_inner_tool(tool_name, args, apply_changes)
            logger.info("[CODE_AGENT] %s -> %d chars", tool_name, len(result))

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{round_num}"),
                    "content": result,
                }
            )

    return "Code agent reached maximum rounds without completing. Partial results may be in the conversation above."


def _parse_xml_tool_calls(content: str) -> list:
    """Parse <tool_call> XML tags from content."""
    tool_calls = []
    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    for i, match in enumerate(re.findall(pattern, content, re.DOTALL)):
        try:
            parsed = json.loads(match)
            tool_calls.append(
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": parsed.get("name", ""),
                        "arguments": json.dumps(parsed.get("arguments", {})),
                    },
                }
            )
        except json.JSONDecodeError:
            continue
    return tool_calls


# ---------------------------------------------------------------------------
# Tool handler — registered with the orchestrator
# ---------------------------------------------------------------------------


@register_tool("code_agent")
async def handle_code_agent(arguments: dict) -> str:
    """
    Delegate a coding task to the code agent.

    Args:
        task: Description of what to investigate or implement.
        apply_changes: If true, agent can write files. Default false (read-only).
    """
    if not shared.CODE_AGENT_ENABLED:
        return "Code agent is disabled. Set CODE_AGENT_ENABLED=true in .env."

    task = arguments.get("task", "")
    apply_changes = arguments.get("apply_changes", False)

    if not task:
        return "No task provided. Describe what you'd like investigated or changed."

    model_url = shared.CODE_AGENT_MODEL_URL
    if not model_url:
        return "Code agent is not configured. Set CODE_AGENT_MODEL_URL in .env."

    # Preflight: the code model runs on Helios, which is powered down most of the
    # time. Probe its endpoint with a short timeout so an asleep Helios fails fast
    # here instead of hanging on connect for up to 120s on every one of the (up to
    # CODE_AGENT_MAX_ROUNDS) agent rounds before giving up.
    if not await _code_model_reachable(model_url):
        CODE_AGENT_PREFLIGHT_FAILURES.inc()
        logger.warning("[CODE_AGENT] Code model unreachable at %s — skipping run", model_url)
        return (
            "The code model isn't reachable right now — it runs on Helios, which is "
            "powered down most of the time. Wake Helios (it's on an HA-controlled smart "
            f"plug) and try again once the model at {model_url} is up."
        )

    mode = "read-write" if apply_changes else "read-only"
    logger.info("[CODE_AGENT] Starting %s task: %s", mode, task[:100])

    result = await _run_code_agent_loop(task, apply_changes)

    logger.info("[CODE_AGENT] Task complete (%d chars)", len(result))
    return result
