"""Read-only self-investigator (self-monitoring step 2).

The self-audit hands the brain its error clusters (or an alert) plus four
read-only tools and lets it look around before answering:

- ``list_dir`` / ``read_file`` / ``grep`` over the repo, mounted read-only at
  ``SELF_AUDIT_REPO_PATH`` (``/opt/gateway_mvp``), restricted to an allow-list
  of code/config/doc paths — that mount also holds ``.env``, ``data/``,
  ``credentials/`` and the rendered alertmanager config, and the non-root
  container user can read them, so the allow-list is the only thing keeping
  them out of the prompt;
- ``loki_query`` against ``SELF_AUDIT_LOKI_URL`` (log or metric query, capped).

Two backends share one loop (``_run_loop``):

- local (default): the Helios brain via ``call_model``;
- Claude (``SELF_AUDIT_CLAUDE_API_KEY`` set): the Anthropic Messages API via the
  official SDK, ``SELF_AUDIT_CLAUDE_MODEL`` (default Opus 5.5). Runs while
  Helios sleeps. The local brain takes over when the month's estimated spend
  reaches ``SELF_AUDIT_CLAUDE_MONTHLY_USD`` (alert runs: 75 % of it), after
  ``CLAUDE_MAX_RUNS_PER_DAY`` paid runs in a UTC day, or when the FIRST Claude
  call fails (bad key, no credit, outage, decline).

Safety story (on top of jobs_self_audit's command allow-list, dangerous-pattern
and secret filters, which still run on the final report):

1. Nothing here writes or executes. Network: Loki, the model, and with the
   Claude backend api.anthropic.com. Anthropic then sees the system prompt,
   the task (error-cluster sample lines and the alert payload) and every tool
   result: allow-listed repo files (all git-tracked, the repo is public) and
   any Loki line from the last 24 h. Loki holds more than the orchestrator
   (whose logs carry no personal content, tests/test_no_user_content_in_logs.py):
   every Jupiter container (frontend, grafana with its login name and the
   LogQL queries run, prometheus, loki, promtail, redis), Conjure's log files
   and the Helios journal (llama-server-primary, stt-onnx, nut-*, kernel).
   The owner accepted that on 2026-11-24 (no message text, meds or calendar
   in those). Keep server tools and MCP off the Claude call: the four
   client tools are its only way to reach anything. The output is text; a
   suggested diff is never applied (that is step 3).
2. The task and every tool result are secret-redacted; every tool result is
   also defanged (control markup, code fences, frame markers), framed as
   untrusted data and capped.
3. Hard budgets: rounds, tool calls, wall time, per-result size, calls per
   round, duplicate calls collapsed — and for Claude a monthly dollar cap and
   a daily run cap, both failing closed (an unreadable counter = cap reached).
   The cap is an estimate: a spend limit in the Anthropic Console is the hard one.
4. Slot etiquette (local backend): waits for chat to go idle before every
   model call and never wakes Helios — asleep means the deterministic digest.

Logging: tool names, counts, sizes, durations, token counts, cost estimates
and error class/status only — never queries, paths, file contents, API error
messages or model text (no personal content in logs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from orchestrator import shared
from orchestrator.config import settings
from orchestrator.jobs_self_audit import (
    UNTRUSTED_RULE,
    _redact_secrets,
    defang_untrusted,
    format_sources,
    frame_untrusted,
)
from orchestrator.metrics import (
    ACTIVE_REQUESTS,
    BACKGROUND_LLM_ERRORS,
    SELF_INVESTIGATOR_CLAUDE_CAP_USD,
    SELF_INVESTIGATOR_CLAUDE_FALLBACK_TOTAL,
    SELF_INVESTIGATOR_CLAUDE_SPEND_USD,
    SELF_INVESTIGATOR_CLAUDE_TOKENS_TOTAL,
    SELF_INVESTIGATOR_RUNS_TOTAL,
    SELF_INVESTIGATOR_TOOL_CALLS_TOTAL,
)

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from anthropic.types.beta import BetaMessage

logger = logging.getLogger(__name__)

# --- Budgets ------------------------------------------------------------------

MAX_RESULT_CHARS = 6000
MAX_CALLS_PER_ROUND = 5
MAX_READ_LINES = 300
DEFAULT_READ_LINES = 150
MAX_GREP_MATCHES = 100
MAX_GREP_FILES = 4000
MAX_GREP_BYTES = 40_000_000
MAX_PATTERN_CHARS = 200
MAX_LIST_ENTRIES = 200
MAX_FILE_BYTES = 2_000_000
MAX_LOKI_LINES = 200
MAX_LOKI_HOURS = 24
MAX_LOKI_QUERY_CHARS = 500
ROUND_TIMEOUT_S = 120  # background LLM budget (single llama slot)
MIN_ROUND_TIMEOUT_S = 20
FINAL_RESERVE_S = 90  # wall time kept back for the forced final report
# Alert runs happen while the owner is around: smaller budget, and only after
# the brain has been idle for a while.
ALERT_MAX_ROUNDS = 6
ALERT_MAX_SECONDS = 300
ALERT_QUIET_S = 180
ALERT_QUIET_MAX_WAIT_S = 1800
ROUND_MAX_TOKENS = 4096
CHAT_IDLE_WAIT_S = 60

# --- Repo allow-list ------------------------------------------------------------

_ALLOWED_DIRS = (
    "orchestrator",
    "monitoring",
    "scripts",
    "tts",
    "docs",
    "jess-features",
    "frontend/src",
    "homeassistant",
)
_ALLOWED_TOP_FILES = frozenset({"docker-compose.yml", "CLAUDE.md", "README.md", "TECHNICAL_REFERENCE.md"})
_ALLOWED_EXT = frozenset(
    {".py", ".md", ".yml", ".yaml", ".sh", ".service", ".conf", ".template", ".json", ".ts", ".tsx", ".txt", ".toml"}
)
# Path components refused anywhere (runtime state, secrets, VCS, build output).
_DENY_PARTS = frozenset(
    {"data", "credentials", ".git", "node_modules", "__pycache__", ".next", ".venv", "venv", "config"}
)
# Rendered monitoring configs (gitignored; alertmanager.yml holds Pushover keys)
# and token files.
_DENY_NAMES = frozenset(
    {
        "alertmanager.yml",
        "prometheus.yml",
        "api_token",
        "self_audit_webhook_token",
        "llama_api_key",
        "user_profile.yaml",
    }
)
_DENY_NAME_RE = re.compile(
    r"(^\.env|secret|token|credential|password|htpasswd|api[_-]?key|\.key$|\.pem$)", re.IGNORECASE
)


_NOT_READABLE = "no such file, or not in the readable allow-list"


class PathDenied(Exception):
    """A path outside the allow-list (message is safe to show the model)."""


def _repo_root() -> Path:
    return Path(settings.self_audit_repo_path).resolve()


def _allowed_rel(rel: PurePosixPath, *, is_dir: bool) -> bool:
    parts = rel.parts
    if not parts:
        return is_dir  # the root itself may be listed
    if any(p in _DENY_PARTS or p.startswith(".") for p in parts):
        return False
    name = parts[-1]
    if not is_dir and (name in _DENY_NAMES or _DENY_NAME_RE.search(name)):
        return False
    rel_s = rel.as_posix()
    # Untracked maintainer notes (security backlog etc.) stay out of the prompt.
    if rel_s == "docs/internal" or rel_s.startswith("docs/internal/"):
        return False
    if len(parts) == 1 and not is_dir:
        return name in _ALLOWED_TOP_FILES
    for d in _ALLOWED_DIRS:
        if rel_s == d or rel_s.startswith(d + "/"):
            return is_dir or PurePosixPath(name).suffix in _ALLOWED_EXT
        # Parent of an allowed dir (e.g. "frontend") may be listed, not read.
        if is_dir and d.startswith(rel_s + "/"):
            return True
    return False


def resolve_repo_path(path: str) -> tuple[Path, PurePosixPath]:
    """Map a model-supplied repo path to (absolute, repo-relative), or raise
    ``PathDenied``. Resolves symlinks, so a link can't point outside the
    allow-list; both the requested and the resolved path must be allowed."""
    raw = (path or "").strip().replace("\\", "/")
    if "\x00" in raw:
        raise PathDenied("invalid path")
    for prefix in ("/opt/gateway_mvp/", "/app/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            if prefix == "/app/" and not raw.startswith("orchestrator/"):
                raw = "orchestrator/" + raw
            break
    raw = raw.strip("/") or ""
    requested = PurePosixPath(raw) if raw else PurePosixPath()
    if any(p == ".." for p in requested.parts):
        raise PathDenied("'..' is not allowed")
    root = _repo_root()
    full = (root / raw).resolve() if raw else root
    try:
        resolved_rel = PurePosixPath(full.relative_to(root).as_posix())
    except ValueError:
        raise PathDenied("path is outside the repository") from None
    if resolved_rel.as_posix() == ".":
        resolved_rel = PurePosixPath()
    # One message for "missing" and "not allowed": a distinct "no such file"
    # would tell the model whether a denied path (a key file) exists.
    if not full.exists():
        raise PathDenied(_NOT_READABLE)
    is_dir = full.is_dir()
    if not (_allowed_rel(requested, is_dir=is_dir) and _allowed_rel(resolved_rel, is_dir=is_dir)):
        raise PathDenied(_NOT_READABLE)
    return full, resolved_rel


# --- Tools ------------------------------------------------------------------------


def _list_dir(path: str = "") -> str:
    full, rel = resolve_repo_path(path)
    if not full.is_dir():
        raise PathDenied("not a directory")
    out = []
    for entry in sorted(full.iterdir(), key=lambda p: p.name):
        is_dir = entry.is_dir()
        child = rel / entry.name
        if entry.is_symlink() or not _allowed_rel(child, is_dir=is_dir):
            continue
        out.append(entry.name + ("/" if is_dir else ""))
        if len(out) >= MAX_LIST_ENTRIES:
            out.append("… (listing truncated)")
            break
    return f"{rel.as_posix() or '.'}:\n" + ("\n".join(out) or "(nothing readable here)")


def _read_lines(full: Path) -> list[str]:
    if full.stat().st_size > MAX_FILE_BYTES:
        raise PathDenied("file is too large to read")
    return full.read_text(encoding="utf-8", errors="replace").splitlines()


def _read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    full, rel = resolve_repo_path(path)
    if full.is_dir():
        raise PathDenied("that is a directory; use list_dir")
    lines = _read_lines(full)
    start = max(1, int(start_line or 1))
    end = int(end_line) if end_line else start + DEFAULT_READ_LINES - 1
    end = min(end, start + MAX_READ_LINES - 1, len(lines))
    if start > len(lines):
        return f"{rel.as_posix()} has {len(lines)} lines; start_line {start} is past the end."
    body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
    more = f"\n… ({len(lines)} lines total)" if end < len(lines) else ""
    return f"{rel.as_posix()} lines {start}-{end}:\n{body}{more}"


def _iter_files(full: Path, rel: PurePosixPath) -> Iterator[tuple[Path, PurePosixPath]]:
    if full.is_file():
        yield full, rel
        return
    for dirpath, dirnames, filenames in os.walk(full):
        d_rel = PurePosixPath(Path(dirpath).relative_to(_repo_root()).as_posix())
        dirnames[:] = sorted(
            n for n in dirnames if not os.path.islink(os.path.join(dirpath, n)) and _allowed_rel(d_rel / n, is_dir=True)
        )
        for name in sorted(filenames):
            f = Path(dirpath) / name
            if f.is_symlink():
                continue
            f_rel = d_rel / name
            if _allowed_rel(f_rel, is_dir=False):
                yield f, f_rel


def _grep(pattern: str, path: str = "") -> str:
    """Plain substring search. Deliberately no regex: CPython's ``re`` holds
    the GIL while backtracking, so one catastrophic model-chosen pattern
    (``(a+)+$``) would freeze the whole orchestrator — a thread timeout can't
    stop it. Substring search is linear; total bytes scanned are capped too."""
    pattern = str(pattern or "")
    if not pattern:
        raise PathDenied("empty pattern")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise PathDenied(f"pattern longer than {MAX_PATTERN_CHARS} characters")
    full, rel = resolve_repo_path(path)
    hits: list[str] = []
    scanned_files = 0
    scanned_bytes = 0
    for f, f_rel in _iter_files(full, rel):
        scanned_files += 1
        if scanned_files > MAX_GREP_FILES or scanned_bytes > MAX_GREP_BYTES:
            hits.append("… (stopped: searched too much; narrow the path)")
            break
        try:
            lines = _read_lines(f)
        except (OSError, PathDenied):
            continue
        scanned_bytes += sum(len(line) for line in lines)
        for n, line in enumerate(lines, 1):
            if pattern in line:
                hits.append(f"{f_rel.as_posix()}:{n}: {line.strip()[:200]}")
                if len(hits) >= MAX_GREP_MATCHES:
                    hits.append("… (match limit reached; narrow the search)")
                    return "\n".join(hits)
    return "\n".join(hits) or "no matches"


_METRIC_QUERY_RE = re.compile(r"^\s*(sum|count|rate|avg|max|min|topk|bottomk|count_over_time|bytes_over_time)\b")


# The time window is enforced on the QUERY, not just on start/end: a metric query
# carries its own range (`[20d]`, `offset 8d`) and LogQL parser/format stages can
# copy line text into labels. Without these checks a metric query could read log
# text from the full 30-day retention (security review, 2026-10-21) — including
# lines from before PR #81 stopped personal text reaching the logs.
# No word boundaries: strings are blanked first, so any remaining "offset" is
# the keyword (Loki rejects "offset8d" today, but don't rely on it).
_LOGQL_OFFSET_RE = re.compile(r"offset", re.IGNORECASE)
# LogQL accepts `#` line comments (verified live 2026-10-21): a `"` inside a
# comment would make the string blanker and Loki disagree about where strings
# end, hiding a stage on the next line. No comments, no multi-line queries.
_LOGQL_FORBIDDEN_RE = re.compile(r"[#\r\n]")
_LOGQL_RANGE_RE = re.compile(r"\[([^\]]*)\]")
_LOGQL_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)")
_DURATION_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_LOGQL_STAGE_RE = re.compile(
    r"\|\s*(regexp|pattern|json|logfmt|unpack|label_format|line_format|unwrap)\b", re.IGNORECASE
)
# Metric results print only these labels, each capped — never arbitrary labels
# (which a parser stage or a high-cardinality label could fill with log text).
_LOKI_LABEL_ALLOW = ("service", "level", "host", "job", "unit", "container", "transport", "group")
_LOKI_LABEL_MAX = 64


_LOGQL_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"|`[^`]*`')


def _check_logql(query: str) -> None:
    """Raise PathDenied unless the query stays inside the last MAX_LOKI_HOURS.

    Quoted strings (line filters, label matchers) are blanked first, so a
    regex like |~ "[0-9]+" or the word "offset" in a filter isn't mistaken
    for a range or keyword. Stage names are checked on the blanked query too:
    `| json` inside a string is just text."""
    if "\r" in query or "\n" in query:
        raise PathDenied("multi-line queries are not allowed")
    query = _LOGQL_STRING_RE.sub('""', query)
    if _LOGQL_FORBIDDEN_RE.search(query):
        raise PathDenied("comments are not allowed")
    if _LOGQL_OFFSET_RE.search(query):
        raise PathDenied("offset is not allowed")
    if _LOGQL_STAGE_RE.search(query):
        raise PathDenied(
            "parser/format stages (regexp, pattern, json, logfmt, unpack, label_format, line_format, unwrap) are not allowed"
        )
    for raw in _LOGQL_RANGE_RE.findall(query):
        spec = raw.strip()
        parts = _LOGQL_DURATION_RE.findall(spec)
        if not parts or "".join(n + u for n, u in parts) != spec.replace(" ", ""):
            raise PathDenied(f"unsupported range [{raw}] (use e.g. [1h], [30m])")
        seconds = sum(float(n) * _DURATION_SECONDS[u] for n, u in parts)
        if seconds > MAX_LOKI_HOURS * 3600:
            raise PathDenied(f"range [{raw}] is longer than {MAX_LOKI_HOURS}h")


async def _loki_query(query: str, hours: int = 1, limit: int = 50) -> str:
    query = str(query or "").strip()
    if not query or len(query) > MAX_LOKI_QUERY_CHARS:
        raise PathDenied(f"query must be 1-{MAX_LOKI_QUERY_CHARS} characters")
    _check_logql(query)
    hours = min(max(int(hours or 1), 1), MAX_LOKI_HOURS)
    limit = min(max(int(limit or 50), 1), MAX_LOKI_LINES)
    base = settings.self_audit_loki_url.rstrip("/")
    end = time.time()
    is_metric = bool(_METRIC_QUERY_RE.match(query))
    if is_metric:
        url, params = base + "/loki/api/v1/query", {"query": query, "time": str(int(end * 1e9))}
    else:
        url = base + "/loki/api/v1/query_range"
        params = {
            "query": query,
            "start": str(int((end - hours * 3600) * 1e9)),
            "end": str(int(end * 1e9)),
            "limit": str(limit),
            "direction": "backward",
        }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
    except httpx.HTTPError as e:
        return f"Loki request failed ({type(e).__name__})"
    if resp.status_code >= 400:
        return f"Loki rejected the query (HTTP {resp.status_code}): {resp.text[:300]}"
    try:
        result = (resp.json().get("data") or {}).get("result") or []
    except (ValueError, AttributeError):
        return "Loki returned an unreadable response"
    try:
        return _format_loki(result, is_metric, limit)
    except (AttributeError, TypeError, ValueError, IndexError):
        return "Loki returned an unexpected shape"


def _format_loki(result: Any, is_metric: bool, limit: int) -> str:
    lines: list[str] = []
    if is_metric:
        for row in result[:50]:
            metric = row.get("metric") or {}
            value = (row.get("value") or [None, "?"])[1]
            labels = ", ".join(f"{k}={str(metric[k])[:_LOKI_LABEL_MAX]}" for k in _LOKI_LABEL_ALLOW if k in metric)
            lines.append(f"{{{labels}}} {value}")
        return "\n".join(lines) or "no series"
    rows: list[tuple[str, str, str]] = []
    for stream in result:
        svc = str((stream.get("stream") or {}).get("service") or "?")
        for ts, line in stream.get("values") or []:
            rows.append((str(ts), svc, str(line)))
    rows.sort(reverse=True)
    for ts, svc, line in rows[:limit]:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts) / 1e9))
        except (ValueError, OverflowError):
            when = "?"
        lines.append(f"{when} {svc}: {line[:500]}")
    return "\n".join(lines) or "no lines"


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a folder of the Brain Gateway repository (read-only). Empty path = repo root.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Repo-relative folder, e.g. orchestrator"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": f"Read numbered lines of a repository file (at most {MAX_READ_LINES} lines per call).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative file, e.g. orchestrator/unified_loop.py"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search repository files for a plain text substring (case-sensitive, no regex). "
            "Returns path:line: text, at most 100 matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Repo-relative file or folder; empty = whole repo"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "loki_query",
            "description": "Run a LogQL query against Loki (the log store). A log query like "
            '{service="orchestrator"} |= "Traceback" returns recent lines (newest first); a metric query like '
            'sum by (service) (count_over_time({host="helios"}[1h])) returns numbers. Labels: service, level '
            '(error|warning|info|debug), host ("helios" for the GPU box; absent for the always-on hub).',
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "hours": {"type": "integer", "description": f"How far back, 1-{MAX_LOKI_HOURS}"},
                    "limit": {"type": "integer", "description": f"Max lines, 1-{MAX_LOKI_LINES}"},
                },
                "required": ["query"],
            },
        },
    },
]
_TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOL_SCHEMAS)


async def run_tool(name: str, args: dict[str, Any]) -> str:
    """Execute one tool call. Never raises; always returns framed text."""
    if name not in _TOOL_NAMES:
        SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool="unknown", result="denied").inc()
        return frame_untrusted("Unknown tool. Available: " + ", ".join(sorted(_TOOL_NAMES)))
    try:
        if name == "list_dir":
            text = await asyncio.to_thread(_list_dir, str(args.get("path") or ""))
        elif name == "read_file":
            text = await asyncio.to_thread(
                _read_file, str(args.get("path") or ""), args.get("start_line") or 1, args.get("end_line")
            )
        elif name == "grep":
            text = await asyncio.to_thread(_grep, str(args.get("pattern") or ""), str(args.get("path") or ""))
        else:
            text = await _loki_query(str(args.get("query") or ""), args.get("hours") or 1, args.get("limit") or 50)
        result = "ok"
    except PathDenied as e:
        text, result = f"Refused: {e}", "denied"
    except (TypeError, ValueError, ArithmeticError):  # ArithmeticError: int(float("inf")) from JSON Infinity
        text, result = "Refused: bad arguments", "denied"
    except OSError as e:
        text, result = f"Could not read ({type(e).__name__})", "error"
    except Exception as e:  # never raise into the loop: one bad tool result must not end the run
        # log-safe: name is one of the four allow-listed tool names
        logger.warning("[INVESTIGATOR] tool %s failed: %s", name, type(e).__name__)
        text, result = f"Tool failed ({type(e).__name__})", "error"
    SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool=name, result=result).inc()
    text = _redact_secrets(text)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n… (cut at {MAX_RESULT_CHARS} characters; ask for a narrower range)"
    return frame_untrusted(text)


# --- Citation check -----------------------------------------------------------------

_CITATION_RE = re.compile(
    r"(?<![\w/.-])(/?(?:[\w.-]+/)*[\w.-]+\.(?:py|ya?ml|sh|md|service|conf|template|json|tsx?|toml)):(\d+)(?!\d)"
)
UNVERIFIED_TAG = " (unverified citation)"


def verify_citations(text: str) -> str:
    """Tag every ``path:line`` the model cites that doesn't exist in the
    allow-listed repo (wrong path, line past the end, unreadable file)."""
    if not text:
        return text
    cache: dict[str, int | None] = {}

    def n_lines(path: str) -> int | None:
        if path not in cache:
            try:
                full, _rel = resolve_repo_path(path)
                cache[path] = None if full.is_dir() else len(_read_lines(full))
            except (PathDenied, OSError):
                cache[path] = None
        return cache[path]

    def repl(m: re.Match) -> str:
        path, line = m.group(1), int(m.group(2))
        total = n_lines(path)
        if total is not None and 1 <= line <= total:
            return m.group(0)
        return m.group(0) + UNVERIFIED_TAG

    return _CITATION_RE.sub(repl, text)


# --- Slot etiquette -------------------------------------------------------------------


async def brain_ready() -> bool:
    """True if the primary model answers /models within 5 s. Never wakes Helios."""
    client = shared._http
    if client is None:
        return False
    try:
        r = await client.get(f"{shared.MODEL_URL.rstrip('/')}/models", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


async def _slot_busy() -> bool:
    """True if chat (or anything) is using the brain right now.

    ``bgw_active_requests`` counts chat turns, streamed ones until their
    stream ends, but not other brain callers (auto_learn, photo scans, the meal
    planner), so also ask llama-server itself: ``llamacpp:requests_processing``
    on its /metrics. If that can't be read, fall back to the gauge alone.
    """
    try:
        if ACTIVE_REQUESTS._value.get() > 0:
            return True
    except Exception:
        pass
    from orchestrator.brain_slot import coder_busy
    from orchestrator.research_helper import lookup_active

    if lookup_active():  # between a lookup's rounds llama-server looks idle
        return True
    if coder_busy():  # a coder-gate request (docs/internal/CODER_PLAN.md)
        return True
    client = shared._http
    if client is None:
        return False
    base = shared.MODEL_URL.rstrip("/")
    base = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        r = await client.get(base + "/metrics", timeout=3.0)
        if r.status_code != 200:
            return False
        for line in r.text.splitlines():
            if line.startswith("llamacpp:requests_processing"):
                return float(line.rsplit(" ", 1)[-1]) > 0
    except Exception:
        return False
    return False


async def _wait_for_idle_chat(max_wait_s: float = CHAT_IDLE_WAIT_S) -> None:
    """Let chat in flight finish first (bounded): the investigator shares the
    single llama slot with interactive chat."""
    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        if not await _slot_busy():
            return
        await asyncio.sleep(2.0)


async def _wait_for_quiet_period(quiet_s: float, max_wait_s: float) -> bool:
    """Alert runs: alerts like EmptyRepliesReachingUsers fire mid-conversation,
    so don't start until the slot has been idle for ``quiet_s`` (polling every
    15 s), giving up after ``max_wait_s``. Returns False if it never got quiet."""
    deadline = time.monotonic() + max_wait_s
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        if await _slot_busy():
            quiet_since = None
        else:
            if quiet_since is None:
                quiet_since = time.monotonic()
            if time.monotonic() - quiet_since >= quiet_s:
                return True
        await asyncio.sleep(15.0)
    return False


# --- Prompt ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are the on-call engineer for Brain Gateway ("Jess"), a personal AI assistant: a FastAPI orchestrator plus dashboard, monitoring and Home Assistant on an always-on hub (Jupiter), which also runs the TTS (VoxCPM2) as a systemd unit on one of its GPUs, and a GPU box (Helios) that sleeps most of the day and runs the LLM (llama.cpp) and STT as systemd units. A backup box (Saturn) holds the off-box backups.

Deployment facts: on Jupiter the services run in Docker (the orchestrator is the container brain-orchestrator, compose service orchestrator; Loki, Prometheus, Grafana, Alertmanager are containers too). On Helios the model servers are systemd units: llama-server-primary (the LLM), stt-onnx (STT), nut-* (UPS); qwen-tts and Helios's voxcpm-tts are disabled rollbacks. The live TTS is the voxcpm-tts systemd unit on Jupiter (its journal is not in Loki: Jupiter's promtail ships container logs and Conjure's log files, not the systemd journal). Use docker commands for Jupiter's containers and journalctl/systemctl for systemd units (voxcpm-tts on Jupiter, the model units on Helios).

You are investigating a problem with READ-ONLY tools: list_dir, read_file and grep over the repository, and loki_query over the logs. You cannot change anything, run commands or open URLs. Repository paths are relative to the repo root (the orchestrator's Python code is under orchestrator/; inside its container that is /app/orchestrator/).

{UNTRUSTED_RULE} Every tool result is wrapped in that block too.

How to work: start from the evidence, check the code paths that produce the error, confirm with logs, and stop when you can name the cause. Do not re-run a call you already made. Be economical: you have a limited number of tool calls.

Your final answer (a message with no tool calls) must use EXACTLY this format, one section per distinct problem, most severe first:

Summary: <one sentence>

## <service> · <SEVERITY>
- **Count:** <count>x in last <hours>h
- **First/Last:** <first_seen> -> <last_seen>
- **Evidence:** <repo/path.py:LINE and/or the LogQL query that showed it>
- **Likely cause:** <one or two sentences>
- **Suggestion:** <INVESTIGATE or FIX>: <one shell command>

Optionally, after a section, a suggested fix as a unified diff in a ```diff block (it will NOT be applied; a human reviews it).

Rules:
- SEVERITY is one of CRITICAL, HIGH, MEDIUM, LOW, by user impact.
- Cite only file:line locations you actually read; uncited guesses must say "unconfirmed".
- The shell command MUST be a single command using ONLY: journalctl, systemctl status/restart/reload/show/cat, docker logs/ps/stats/inspect, docker compose logs/ps/config/top/restart, ls, cat, tail, head, grep, ss, top, ps, df, du, free, uptime, find, lsof, nvidia-smi, wc, stat. No rm, dd, kill, chmod, curl, wget, sh, bash, python, eval, redirects, $(...) or backticks. FIX only for a confident service-level restart.
- End with EXACTLY one line: **Overall:** <C> critical, <H> high, <M> medium, <L> low
"""


def build_task(
    *,
    clusters: list[dict[str, Any]],
    groups: list[dict[str, Any]] | None,
    lookback_hours: int,
    trigger: str,
    alert: dict[str, Any] | None,
) -> str:
    from orchestrator.jobs_self_audit import _format_cluster_for_prompt

    parts: list[str] = []
    if trigger == "alert" and alert:
        name = re.sub(r"[^A-Za-z0-9]", "", str(alert.get("alertname") or ""))[:64]
        parts.append(f"An alert just fired: {name}. Find out why and whether it is still happening.")
        detail = {
            "labels": alert.get("labels") or {},
            "annotations": alert.get("annotations") or {},
            "startsAt": alert.get("startsAt") or "",
        }
        parts.append("Alert details (from the alerting system):\n" + frame_untrusted(json.dumps(detail)[:3000]))
    else:
        parts.append("This is the daily audit. Investigate the most important error clusters below.")
    parts.append(
        f"SOURCES (line counts per log source for the last {lookback_hours}h; a TRUNCATED source was sampled):\n"
        + format_sources(groups)
    )
    if clusters:
        parts.append(
            f"ERROR CLUSTERS (most frequent first, last {lookback_hours}h):\n\n"
            + "\n".join(_format_cluster_for_prompt(c) for c in clusters)
        )
    else:
        parts.append(f"No error/warning lines were logged in the last {lookback_hours}h.")
    return "\n\n".join(parts)


# --- Loop -------------------------------------------------------------------------------


def _parse_args(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        val = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    return val if isinstance(val, dict) else None


@dataclass
class _ToolCall:
    id: str
    name: str
    args: dict[str, Any] | None  # None = arguments were not a JSON object
    raw: Any = None  # the backend's own form (the local loop replays it)


@dataclass
class _Reply:
    text: str
    calls: list[_ToolCall] = field(default_factory=list)


class _Conversation(Protocol):
    """One model backend's view of the investigation history."""

    backend: str
    # Don't start a round with less than this left before the loop deadline.
    min_round_s: float

    async def call(self, *, with_tools: bool, timeout_s: float) -> _Reply | None: ...

    def add_tool_round(self, kept: list[_ToolCall], results: list[str], dropped: list[_ToolCall]) -> None: ...

    def add_user_text(self, text: str) -> None: ...

    def salvageable(self) -> bool:
        """After a failed call: is a forced final report still worth one try?"""
        ...

    def note_crash(self) -> None:
        """The loop hit an unexpected exception."""
        ...


class _LocalConversation:
    """The Helios brain over the OpenAI-shaped call_model (the original loop)."""

    backend = "local"
    min_round_s = 0.0

    def __init__(self, task: str) -> None:
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        self._last_text = ""

    async def call(self, *, with_tools: bool, timeout_s: float) -> _Reply | None:
        from orchestrator.orchestrator import call_model

        await _wait_for_idle_chat(min(CHAT_IDLE_WAIT_S, max(5.0, timeout_s / 4)))
        try:
            resp = await call_model(
                shared.MODEL_URL,
                shared.MODEL_NAME,
                messages=self.messages,
                tools=TOOL_SCHEMAS if with_tools else None,
                timeout=int(max(MIN_ROUND_TIMEOUT_S, min(ROUND_TIMEOUT_S, timeout_s))),
                extra_body={"max_tokens": ROUND_MAX_TOKENS, "temperature": 0.6},
            )
        except Exception as e:
            logger.warning("[INVESTIGATOR] model call failed: %s", type(e).__name__)
            BACKGROUND_LLM_ERRORS.labels(caller="self_investigator", error_type=type(e).__name__).inc()
            return None
        if not isinstance(resp, dict):
            return None
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        calls = []
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            calls.append(
                _ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    args=_parse_args(fn.get("arguments")),
                    raw=call,
                )
            )
        self._last_text = msg.get("content") or ""
        return _Reply(text=self._last_text, calls=calls)

    def add_tool_round(self, kept: list[_ToolCall], results: list[str], dropped: list[_ToolCall]) -> None:
        # Calls past MAX_CALLS_PER_ROUND are left out of the replayed turn.
        raws = []
        for c in kept:
            raw = c.raw if isinstance(c.raw, dict) else {}
            raw["id"] = c.id
            raws.append(raw)
        self.messages.append({"role": "assistant", "content": self._last_text, "tool_calls": raws})
        for c, text in zip(kept, results, strict=True):
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": text})

    def add_user_text(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def salvageable(self) -> bool:
        return False  # unchanged behaviour: a failed round ends the run

    def note_crash(self) -> None:
        pass


# --- Claude backend -------------------------------------------------------------------

# USD per million tokens: (input, output, cache read, 5-minute cache write),
# from the Anthropic price list (Opus 5.5 / Sonnet 5.5 cache reads are 0.05x
# input, the older rows 0.1x). An ESTIMATE for the monthly cap; the Anthropic
# Console usage page is the truth. A server-side fallback can serve part of a
# turn on another model, so a call is priced at the dearest model involved.
# Unknown models get the Fable-tier row: over, not under.
_CLAUDE_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
_CLAUDE_UNKNOWN_PRICE = (10.0, 50.0, 1.0, 12.5)
CLAUDE_MAX_TOKENS = 16000  # thinking counts toward it on Opus 5.5 (always on)
# The first call decides between Claude and the local brain: keep it short so
# a hung API doesn't eat the local run's time.
CLAUDE_FIRST_ROUND_TIMEOUT_S = 120
CLAUDE_ROUND_TIMEOUT_S = 240
CLAUDE_MIN_ROUND_S = 60
# Paid runs per UTC day, any trigger (POST /api/self_audit/run has no limit of
# its own). Past it, the local brain.
CLAUDE_MAX_RUNS_PER_DAY = 8
# Alert-triggered runs (up to 6/day) only use Claude below this share of the
# monthly cap, so alerts can't use up what the daily audits need.
CLAUDE_ALERT_CAP_SHARE = 0.75
_CLAUDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_CLAUDE_BASE_URL = "https://api.anthropic.com"  # explicit: ignore a stray ANTHROPIC_BASE_URL
# Same tool definitions as the local loop, in Messages API shape. Built once:
# the tools array must stay byte-identical across a run (prompt cache +
# thinking-block replay both check the prefix). Never add server tools or MCP.
CLAUDE_TOOLS: list[dict[str, Any]] = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOL_SCHEMAS
]
# SDK error classes that mean "no money / account blocked" whatever their
# status (the spend-limit ones are 429 RateLimitError subclasses).
_BILLING_ERRORS = (
    "PaymentRequiredError",
    "EnforcedSpendLimitReachedError",
    "ContractSpendLimitReachedError",
    "RampLimitExceededError",
    "LinkedOrganizationSpendLimitReachedError",
    "LinkedOrganizationBillingUnavailableError",
    "OrganizationOnHoldError",
)
_TRANSIENT_REASONS = frozenset({"connection", "server", "rate_limit"})

_claude_client: AsyncAnthropic | None = None


def _get_claude_client() -> AsyncAnthropic:
    """Lazily built so a missing package or key never breaks import."""
    global _claude_client
    if _claude_client is None:
        import anthropic

        _claude_client = anthropic.AsyncAnthropic(
            api_key=settings.self_audit_claude_api_key, base_url=_CLAUDE_BASE_URL, max_retries=2
        )
    return _claude_client


def _price(model: str) -> tuple[float, float, float, float]:
    return _CLAUDE_PRICES.get(model, _CLAUDE_UNKNOWN_PRICE)


# --- Spend + run counters (app_state) --------------------------------------------------
# Fail closed: an unreadable counter means "cap reached", and a total is only
# written back when it was read, so one bad read can't reset the month.
# _spend_seen keeps this process's highest total per month as a floor;
# _spend_unwritten holds cost that couldn't be added (total unreadable) until
# the next successful write, so a bad read doesn't lose it.
_spend_seen: dict[str, float] = {}
_spend_unwritten: dict[str, float] = {}


def _month_key(now: datetime | None = None) -> str:
    return f"self_audit_claude_usd_{(now or datetime.now(UTC)):%Y-%m}"


def _day_key(now: datetime | None = None) -> str:
    return f"self_audit_claude_runs_{(now or datetime.now(UTC)):%Y-%m-%d}"


def _read_number(key: str) -> float | None:
    """A non-negative finite number from app_state; 0 if unset; None if unreadable."""
    from orchestrator import state_store

    try:
        raw = state_store.get_app_state(key)
        val = float(raw) if raw not in (None, "") else 0.0
    except Exception:
        return None
    return val if math.isfinite(val) and val >= 0 else None


def _write_number(key: str, value: float) -> bool:
    from orchestrator import state_store

    try:
        state_store.set_app_state(key, f"{value:.6f}")
        return True
    except Exception:
        return False


def _stored_spend(key: str) -> float | None:
    val = _read_number(key)
    return None if val is None else max(val, _spend_seen.get(key, 0.0))


def claude_spend_this_month() -> float | None:
    """Estimated USD spent on the Claude investigator this calendar month (UTC).
    None when the counter can't be read (callers treat that as cap reached)."""
    key = _month_key()
    stored = _stored_spend(key)
    return None if stored is None else stored + _spend_unwritten.get(key, 0.0)


def _add_claude_spend(usd: float) -> None:
    key = _month_key()
    owed = _spend_unwritten.pop(key, 0.0) + max(usd, 0.0)
    stored = _stored_spend(key)
    if stored is None:
        _spend_unwritten[key] = owed  # never write a total we couldn't read
        logger.warning("[INVESTIGATOR] Claude spend counter unreadable; cost kept for the next write")
        return
    total = stored + owed
    _spend_seen[key] = total
    if not _write_number(key, total):
        logger.warning("[INVESTIGATOR] Claude spend counter write failed; kept in memory")


def _count_claude_run() -> None:
    key = _day_key()
    n = _read_number(key)
    if n is None or not _write_number(key, n + 1):
        logger.warning("[INVESTIGATOR] Claude daily run counter not stored")


def _spend_for_gauge() -> float:
    val = claude_spend_this_month()
    return math.nan if val is None else val


def refresh_claude_spend_gauge() -> None:
    """Point the spend gauge at the stored counter (read on every scrape, so it
    drops to 0 on the 1st without waiting for a run) and set the cap gauge.
    Startup and before every run. Never raises."""
    try:
        SELF_INVESTIGATOR_CLAUDE_CAP_USD.set(
            settings.self_audit_claude_monthly_usd if settings.self_audit_claude_enabled else 0.0
        )
        SELF_INVESTIGATOR_CLAUDE_SPEND_USD.set_function(_spend_for_gauge)
    except Exception:
        logger.warning("[INVESTIGATOR] Could not set up the Claude spend gauges")


def _claude_skip_reason(trigger: str) -> str | None:
    """Why this run must not use Claude (None = go ahead)."""
    refresh_claude_spend_gauge()
    spent = claude_spend_this_month()
    if spent is None:
        logger.warning("[INVESTIGATOR] Claude spend counter unreadable — treating the cap as reached")
        return "cap"
    cap = settings.self_audit_claude_monthly_usd
    if spent >= cap * (CLAUDE_ALERT_CAP_SHARE if trigger == "alert" else 1.0):
        return "cap"
    runs = _read_number(_day_key())
    if runs is None or runs >= CLAUDE_MAX_RUNS_PER_DAY:
        return "daily_cap"
    return None


def _claude_failure_reason(exc: BaseException) -> str:
    """Bounded label for a failed Claude call (metric + log; never the message)."""
    if isinstance(exc, asyncio.TimeoutError):
        return "connection"
    try:
        import anthropic
    except ImportError:
        return "other"
    billing = tuple(c for c in (getattr(anthropic, n, None) for n in _BILLING_ERRORS) if isinstance(c, type))
    if billing and isinstance(exc, billing):
        return "billing"
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return "connection"
    if isinstance(exc, anthropic.APIStatusError):
        if getattr(exc, "type", None) == "billing_error":  # e.g. a spent credit balance
            return "billing"
        status = exc.status_code
        if status in (401, 403):
            return "auth"
        if status == 402:
            return "billing"
        if status == 404:
            return "model"  # retired or mistyped SELF_AUDIT_CLAUDE_MODEL
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "server"
        if status == 400:
            return "bad_request"
    return "other"


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.TimeoutError) or type(exc).__name__ == "APITimeoutError"


def _last_fallback(content: list[Any]) -> int:
    last = -1
    for i, b in enumerate(content):
        if getattr(b, "type", "") == "fallback":
            last = i
    return last


def _visible_blocks(content: list[Any]) -> list[Any]:
    """Blocks after the last server-side `fallback` marker: what came before it
    is the declining model's partial output (not part of the report, and its
    tool calls never run)."""
    return content[_last_fallback(content) + 1 :]


def _replay_content(content: list[Any]) -> list[Any]:
    """The assistant turn as it must be sent back. Before the final `fallback`
    block only text blocks (and the fallback blocks, in place) are kept:
    thinking, redacted_thinking, tool_use and any other model-internal block
    from the declining model are left out, and a left-out tool_use gets no
    tool_result (refusals-and-fallback guide, "continuing the conversation").
    Paired server-tool blocks would be kept too, but this loop has none.
    Without a fallback block the turn goes back unchanged."""
    last = _last_fallback(content)
    if last < 0:
        return content
    return [b for i, b in enumerate(content) if i >= last or getattr(b, "type", "") in ("text", "fallback")]


def _models_in(resp: BetaMessage) -> set[str]:
    """Every model that may have run (and billed) this response."""
    models = {settings.self_audit_claude_model, str(getattr(resp, "model", "") or "")}
    for it in getattr(getattr(resp, "usage", None), "iterations", None) or []:
        models.add(str(getattr(it, "model", "") or ""))
    for b in getattr(resp, "content", None) or []:
        if getattr(b, "type", "") == "fallback":
            for side in (getattr(b, "from_", None), getattr(b, "to", None)):
                models.add(str(getattr(side, "model", "") or ""))
    models.discard("")
    return models


class _ClaudeConversation:
    """Native Messages API history. Each assistant turn is replayed exactly as
    returned (thinking blocks included: Opus 5.5 always thinks, and its blocks
    must come back unchanged with their tool_use turn; the one exception is the
    declined part of a turn that fell back, see _replay_content), and the
    history is append-only so the prompt cache and the thinking-block prefix
    check hold."""

    backend = "claude"
    min_round_s = float(CLAUDE_MIN_ROUND_S)

    def __init__(self, task: str) -> None:
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
        self._pending: list[dict[str, Any]] | None = None  # next user turn's blocks
        self.calls_made = 0
        self.failure: str | None = None  # set when the FIRST call fails -> local brain
        self._salvage = False  # a later call failed transiently: try the final report
        self.cost_usd = 0.0

    async def call(self, *, with_tools: bool, timeout_s: float) -> _Reply | None:
        if self._pending is not None:
            self.messages.append({"role": "user", "content": self._pending})
            self._pending = None
        first = self.calls_made == 0
        self.calls_made += 1
        cap = CLAUDE_FIRST_ROUND_TIMEOUT_S if first else CLAUDE_ROUND_TIMEOUT_S
        timeout = max(MIN_ROUND_TIMEOUT_S, min(cap, timeout_s))
        kwargs: dict[str, Any] = {
            "model": settings.self_audit_claude_model,
            "max_tokens": CLAUDE_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            # Tools stay on the final forced call too: the history holds tool_use
            # blocks. tool_choice none stops new calls (forced any/tool 400s on Opus 5.5).
            "tools": CLAUDE_TOOLS,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": settings.self_audit_claude_effort},
            # A safety-classifier decline is re-run server-side on the model
            # Anthropic recommends for that category (log text can look like an attack).
            "betas": [_CLAUDE_FALLBACK_BETA],
            "fallbacks": "default",
            "timeout": timeout,
        }
        if not with_tools:
            kwargs["tool_choice"] = {"type": "none"}
        try:
            resp = await asyncio.wait_for(_get_claude_client().beta.messages.create(**kwargs), timeout=timeout + 5)
        except Exception as e:
            reason = _claude_failure_reason(e)
            status = getattr(e, "status_code", "-")
            logger.warning(
                "[INVESTIGATOR] Claude call failed: %s status=%s reason=%s", type(e).__name__, status, reason
            )
            BACKGROUND_LLM_ERRORS.labels(caller="self_investigator_claude", error_type=type(e).__name__).inc()
            if _is_timeout(e):
                # Never saw its usage, but it may still be billed: charge a full round.
                usd = CLAUDE_MAX_TOKENS * _price(settings.self_audit_claude_model)[1] / 1_000_000
                self.cost_usd += usd
                _add_claude_spend(usd)
            if first:
                self.failure = reason
            elif reason in _TRANSIENT_REASONS:
                self._salvage = True
            return None
        self._account(resp)
        stop = getattr(resp, "stop_reason", None)
        content = list(getattr(resp, "content", None) or [])
        if stop == "refusal":
            logger.warning("[INVESTIGATOR] Claude declined the request (after server-side fallback)")
            if first:
                self.failure = "refusal"
            return None
        if stop not in ("end_turn", "tool_use", "max_tokens", "stop_sequence"):
            label = stop if stop in ("pause_turn", "compaction", "model_context_window_exceeded") else "unknown"
            logger.warning("[INVESTIGATOR] Claude stopped with %s", label)  # log-safe: fixed API enum values only
            if first:
                self.failure = "other"
            return None
        self.messages.append({"role": "assistant", "content": _replay_content(content)})
        visible = _visible_blocks(content)
        text = "\n".join(b.text for b in visible if getattr(b, "type", "") == "text" and b.text)
        calls = [
            _ToolCall(id=str(b.id), name=str(b.name), args=b.input if isinstance(b.input, dict) else None)
            for b in visible
            if getattr(b, "type", "") == "tool_use"
        ]
        return _Reply(text=text, calls=calls)

    def _account(self, resp: BetaMessage) -> None:
        """Count tokens and add the estimated cost. Never raises."""
        try:
            usage = resp.usage
            tokens = {
                "input": int(getattr(usage, "input_tokens", 0) or 0),
                "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                "output": int(getattr(usage, "output_tokens", 0) or 0),
            }
            for kind, n in tokens.items():
                SELF_INVESTIGATOR_CLAUDE_TOKENS_TOTAL.labels(kind=kind).inc(n)
            rows = [_price(m) for m in _models_in(resp)] or [_CLAUDE_UNKNOWN_PRICE]
            p_in, p_out, p_read, p_write = (max(col) for col in zip(*rows, strict=True))
            usd = (
                tokens["input"] * p_in
                + tokens["output"] * p_out
                + tokens["cache_read"] * p_read
                + tokens["cache_write"] * p_write
            ) / 1_000_000
        except Exception:
            logger.warning("[INVESTIGATOR] Could not read Claude usage — charging the full round")
            p_out = max(_price(settings.self_audit_claude_model)[1], _CLAUDE_UNKNOWN_PRICE[1])
            usd = CLAUDE_MAX_TOKENS * p_out / 1_000_000
        self.cost_usd += usd
        _add_claude_spend(usd)

    def add_tool_round(self, kept: list[_ToolCall], results: list[str], dropped: list[_ToolCall]) -> None:
        # Every tool_use in the replayed turn needs a tool_result, so calls past
        # MAX_CALLS_PER_ROUND get a refusal. All results go in ONE user turn.
        blocks: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": c.id, "content": text} for c, text in zip(kept, results, strict=True)
        ]
        refused = frame_untrusted(f"Refused: at most {MAX_CALLS_PER_ROUND} tool calls per round.")
        blocks += [{"type": "tool_result", "tool_use_id": c.id, "content": refused, "is_error": True} for c in dropped]
        self._pending = blocks

    def add_user_text(self, text: str) -> None:
        if self._pending is not None:
            self._pending.append({"type": "text", "text": text})  # after the tool results
        else:
            # After a failed round its tool results were already sent: never
            # edit a sent turn (prefix check), add a new one (the API merges them).
            self.messages.append({"role": "user", "content": text})

    def salvageable(self) -> bool:
        return self._salvage

    def note_crash(self) -> None:
        if self.calls_made <= 1 and self.failure is None:
            self.failure = "other"


async def _run_loop(conv: _Conversation, *, max_rounds: int, max_calls: int, max_seconds: int) -> dict[str, Any]:
    """The investigation loop, shared by both backends. Never raises."""
    t_start = time.monotonic()
    # The loop stops FINAL_RESERVE_S early so the forced final report fits.
    loop_deadline = t_start + max(60, max_seconds - FINAL_RESERVE_S)
    hard_deadline = t_start + max_seconds
    calls_used = 0
    rounds = 0
    seen: set[str] = set()
    report: str | None = None
    result = "budget"
    salvage = False

    try:
        while (
            rounds < max_rounds
            and calls_used < max_calls
            and time.monotonic() < loop_deadline
            and (rounds == 0 or loop_deadline - time.monotonic() >= conv.min_round_s)
        ):
            rounds += 1
            reply = await conv.call(with_tools=True, timeout_s=loop_deadline - time.monotonic())
            if reply is None:
                result = "error"
                salvage = rounds > 1 and conv.salvageable()
                break
            if not reply.calls:
                report = reply.text
                result = "ok" if report.strip() else "error"
                break
            kept = reply.calls[:MAX_CALLS_PER_ROUND]
            dropped = reply.calls[MAX_CALLS_PER_ROUND:]
            for i, call in enumerate(kept):
                if not call.id:
                    call.id = f"call_{rounds}_{i}"
            for _extra in dropped:
                SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool="unknown", result="capped").inc()
            results: list[str] = []
            for call in kept:
                name = call.name
                tool_label = name if name in _TOOL_NAMES else "unknown"
                if call.args is None:
                    text = frame_untrusted("Refused: arguments were not a JSON object")
                    SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool=tool_label, result="denied").inc()
                else:
                    key = name + json.dumps(call.args, sort_keys=True, default=str)
                    if key in seen:
                        text = frame_untrusted("Duplicate call: you already have this result above.")
                        SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool=tool_label, result="duplicate").inc()
                    elif calls_used >= max_calls:
                        text = frame_untrusted("Tool budget used up. Write your final report now.")
                        SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool=tool_label, result="capped").inc()
                    else:
                        seen.add(key)
                        calls_used += 1
                        text = await run_tool(name, call.args)
                results.append(text)
            conv.add_tool_round(kept, results, dropped)

        if report is None and (result != "error" or salvage):
            # A budget ran out (or a later Claude round failed on a transient
            # error): one last call without tools for the report.
            result = "budget"
            conv.add_user_text(
                "Your investigation budget is used up. Write the final report now in the "
                "required format, using only what you have found. No more tool calls."
            )
            reply = await conv.call(
                with_tools=False, timeout_s=max(MIN_ROUND_TIMEOUT_S, hard_deadline - time.monotonic())
            )
            report = (reply.text if reply else "") or None
            if not report:
                result = "error"
    except Exception:
        logger.exception("[INVESTIGATOR] Unexpected failure")
        conv.note_crash()
        result = "error"
    return {"result": result, "report": report, "rounds": rounds, "tool_calls": calls_used}


async def investigate(
    *,
    clusters: list[dict[str, Any]],
    groups: list[dict[str, Any]] | None,
    lookback_hours: int,
    trigger: str = "daily",
    alert: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the read-only investigation. Never raises.

    Returns ``{"result": ok|budget|brain_asleep|error, "report": str|None,
    "rounds": n, "tool_calls": n, "backend": local|claude, "claude_skipped":
    None | cap | daily_cap | <first-call failure reason>, "claude_usd": float}``.
    ``report`` is the model's raw final text (the caller sanitizes it).
    """
    trigger_label = "alert" if trigger == "alert" else "daily"
    t0 = time.monotonic()
    # Secret-redacted for both backends: with Claude, this text leaves the house.
    task = _redact_secrets(
        build_task(clusters=clusters, groups=groups, lookback_hours=lookback_hours, trigger=trigger, alert=alert)
    )
    max_seconds = settings.self_audit_investigate_max_seconds
    max_rounds = settings.self_audit_investigate_max_rounds
    max_calls = settings.self_audit_investigate_max_tool_calls
    if trigger_label == "alert":
        max_seconds = min(max_seconds, ALERT_MAX_SECONDS)
        max_rounds = min(max_rounds, ALERT_MAX_ROUNDS)

    claude_skipped: str | None = None
    claude_usd = 0.0
    out: dict[str, Any] | None = None
    if settings.self_audit_claude_enabled:
        claude_skipped = _claude_skip_reason(trigger_label)
        if claude_skipped is None:
            _count_claude_run()
            conv = _ClaudeConversation(task)
            out = await _run_loop(conv, max_rounds=max_rounds, max_calls=max_calls, max_seconds=max_seconds)
            claude_usd = conv.cost_usd
            if conv.failure:
                claude_skipped = conv.failure
                out = None  # nothing usable yet: run on the local brain instead
        else:
            logger.info("[INVESTIGATOR] Claude skipped (%s) — using the local brain", claude_skipped)
        if claude_skipped:
            SELF_INVESTIGATOR_CLAUDE_FALLBACK_TOTAL.labels(reason=claude_skipped).inc()

    backend = "claude" if out is not None else "local"
    if out is None:
        if not await brain_ready():
            logger.info("[INVESTIGATOR] Brain not reachable — skipping (never wakes Helios)")
            SELF_INVESTIGATOR_RUNS_TOTAL.labels(trigger=trigger_label, result="brain_asleep", backend="local").inc()
            return {
                "result": "brain_asleep",
                "report": None,
                "rounds": 0,
                "tool_calls": 0,
                "backend": "local",
                "claude_skipped": claude_skipped,
                "claude_usd": claude_usd,
            }
        if trigger_label == "alert" and not await _wait_for_quiet_period(ALERT_QUIET_S, ALERT_QUIET_MAX_WAIT_S):
            logger.info("[INVESTIGATOR] Brain never went quiet — investigating anyway with the alert budget")
        # A failed first Claude call already used some of the run's time.
        local_seconds = max(60, int(max_seconds - (time.monotonic() - t0)))
        out = await _run_loop(
            _LocalConversation(task), max_rounds=max_rounds, max_calls=max_calls, max_seconds=local_seconds
        )

    SELF_INVESTIGATOR_RUNS_TOTAL.labels(trigger=trigger_label, result=out["result"], backend=backend).inc()
    logger.info(
        "[INVESTIGATOR] Done · trigger=%s · backend=%s · result=%s · rounds=%d · tool_calls=%d · %.0fs"
        " · report_chars=%d · claude_usd=%.3f",
        trigger_label,
        backend,
        out["result"],
        out["rounds"],
        out["tool_calls"],
        time.monotonic() - t0,
        len(out["report"] or ""),
        claude_usd,
    )
    return {**out, "backend": backend, "claude_skipped": claude_skipped, "claude_usd": claude_usd}


__all__ = [
    "CLAUDE_TOOLS",
    "TOOL_SCHEMAS",
    "PathDenied",
    "brain_ready",
    "build_task",
    "claude_spend_this_month",
    "defang_untrusted",
    "investigate",
    "refresh_claude_spend_gauge",
    "resolve_repo_path",
    "run_tool",
    "verify_citations",
]
