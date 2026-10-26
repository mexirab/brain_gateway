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

Safety story (on top of jobs_self_audit's command allow-list, dangerous-pattern
and secret filters, which still run on the final report):

1. Nothing here writes, executes or reaches the network beyond Loki. The
   output is text; a suggested diff is never applied (that is step 3).
2. Every tool result is secret-redacted, defanged (control markup, code
   fences, frame markers) and framed as untrusted data, then capped.
3. Hard budgets: rounds, tool calls, wall time, per-result size, calls per
   round, duplicate calls collapsed.
4. Slot etiquette: waits for chat to go idle before every model call and
   never wakes Helios — asleep means the deterministic digest instead.

Logging: tool names, counts, sizes and durations only — never queries, paths,
file contents or model text (no personal content in logs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

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
    SELF_INVESTIGATOR_RUNS_TOTAL,
    SELF_INVESTIGATOR_TOOL_CALLS_TOTAL,
)

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
    {"alertmanager.yml", "prometheus.yml", "api_token", "self_audit_webhook_token", "user_profile.yaml"}
)
_DENY_NAME_RE = re.compile(r"(^\.env|secret|token|credential|password|htpasswd|\.key$|\.pem$)", re.IGNORECASE)


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

SYSTEM_PROMPT = f"""You are the on-call engineer for Brain Gateway ("Jess"), a personal AI assistant: a FastAPI orchestrator plus dashboard, monitoring and Home Assistant on an always-on hub (Jupiter), and a GPU box (Helios) that sleeps most of the day and runs the LLM (llama.cpp), TTS and STT as systemd units.

Deployment facts: on Jupiter everything runs in Docker (the orchestrator is the container brain-orchestrator, compose service orchestrator; Loki, Prometheus, Grafana, Alertmanager are containers too). On Helios the model servers are systemd units: llama-server-primary (the LLM), voxcpm-tts / qwen-tts (TTS), stt-onnx (STT), nut-* (UPS). Use docker commands for Jupiter services and journalctl/systemctl for Helios units.

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


async def _call(messages: list[dict[str, Any]], *, with_tools: bool, timeout_s: float) -> dict[str, Any] | None:
    from orchestrator.orchestrator import call_model

    await _wait_for_idle_chat(min(CHAT_IDLE_WAIT_S, max(5.0, timeout_s / 4)))
    try:
        resp = await call_model(
            shared.MODEL_URL,
            shared.MODEL_NAME,
            messages=messages,
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
    choice = (resp.get("choices") or [{}])[0]
    return choice.get("message") or {}


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
    "rounds": n, "tool_calls": n}``. ``report`` is the model's raw final text
    (the caller sanitizes it).
    """
    trigger_label = "alert" if trigger == "alert" else "daily"
    t0 = time.monotonic()
    if not await brain_ready():
        logger.info("[INVESTIGATOR] Brain not reachable — skipping (never wakes Helios)")
        SELF_INVESTIGATOR_RUNS_TOTAL.labels(trigger=trigger_label, result="brain_asleep").inc()
        return {"result": "brain_asleep", "report": None, "rounds": 0, "tool_calls": 0}

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_task(
                clusters=clusters, groups=groups, lookback_hours=lookback_hours, trigger=trigger, alert=alert
            ),
        },
    ]
    max_seconds = settings.self_audit_investigate_max_seconds
    max_rounds = settings.self_audit_investigate_max_rounds
    max_calls = settings.self_audit_investigate_max_tool_calls
    if trigger_label == "alert":
        max_seconds = min(max_seconds, ALERT_MAX_SECONDS)
        max_rounds = min(max_rounds, ALERT_MAX_ROUNDS)
        if not await _wait_for_quiet_period(ALERT_QUIET_S, ALERT_QUIET_MAX_WAIT_S):
            logger.info("[INVESTIGATOR] Brain never went quiet — investigating anyway with the alert budget")
    t_start = time.monotonic()
    # The loop stops FINAL_RESERVE_S early so the forced final report fits.
    loop_deadline = t_start + max(60, max_seconds - FINAL_RESERVE_S)
    hard_deadline = t_start + max_seconds
    calls_used = 0
    rounds = 0
    seen: set[str] = set()
    report: str | None = None
    result = "budget"

    try:
        while rounds < max_rounds and calls_used < max_calls and time.monotonic() < loop_deadline:
            rounds += 1
            msg = await _call(messages, with_tools=True, timeout_s=loop_deadline - time.monotonic())
            if msg is None:
                result = "error"
                break
            calls = msg.get("tool_calls") or []
            if not calls:
                report = msg.get("content") or ""
                result = "ok" if report.strip() else "error"
                break
            kept = calls[:MAX_CALLS_PER_ROUND]
            for i, call in enumerate(kept):
                if isinstance(call, dict) and not call.get("id"):
                    call["id"] = f"call_{rounds}_{i}"
            for _extra in calls[MAX_CALLS_PER_ROUND:]:
                SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(tool="unknown", result="capped").inc()
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": kept})
            for i, call in enumerate(kept):
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                call_id = str(call.get("id") or f"call_{rounds}_{i}")
                args = _parse_args(fn.get("arguments"))
                if args is None:
                    text = frame_untrusted("Refused: arguments were not a JSON object")
                    SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(
                        tool=name if name in _TOOL_NAMES else "unknown", result="denied"
                    ).inc()
                else:
                    key = name + json.dumps(args, sort_keys=True, default=str)
                    if key in seen:
                        text = frame_untrusted("Duplicate call: you already have this result above.")
                        SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(
                            tool=name if name in _TOOL_NAMES else "unknown", result="duplicate"
                        ).inc()
                    elif calls_used >= max_calls:
                        text = frame_untrusted("Tool budget used up. Write your final report now.")
                        SELF_INVESTIGATOR_TOOL_CALLS_TOTAL.labels(
                            tool=name if name in _TOOL_NAMES else "unknown", result="capped"
                        ).inc()
                    else:
                        seen.add(key)
                        calls_used += 1
                        text = await run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": call_id, "content": text})

        if report is None and result != "error":
            # A budget ran out: one last call without tools for the report.
            result = "budget"
            messages.append(
                {
                    "role": "user",
                    "content": "Your investigation budget is used up. Write the final report now in the "
                    "required format, using only what you have found. No more tool calls.",
                }
            )
            msg = await _call(
                messages, with_tools=False, timeout_s=max(MIN_ROUND_TIMEOUT_S, hard_deadline - time.monotonic())
            )
            report = (msg or {}).get("content") or None
            if not report:
                result = "error"
    except Exception:
        logger.exception("[INVESTIGATOR] Unexpected failure")
        result = "error"

    SELF_INVESTIGATOR_RUNS_TOTAL.labels(trigger=trigger_label, result=result).inc()
    logger.info(
        "[INVESTIGATOR] Done · trigger=%s · result=%s · rounds=%d · tool_calls=%d · %.0fs · report_chars=%d",
        trigger_label,
        result,
        rounds,
        calls_used,
        time.monotonic() - t0,
        len(report or ""),
    )
    return {"result": result, "report": report, "rounds": rounds, "tool_calls": calls_used}


__all__ = [
    "TOOL_SCHEMAS",
    "PathDenied",
    "brain_ready",
    "build_task",
    "defang_untrusted",
    "investigate",
    "resolve_repo_path",
    "run_tool",
    "verify_citations",
]
