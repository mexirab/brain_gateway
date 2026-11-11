"""
Structured JSON logging for Brain Gateway.

Replaces plain text logging with JSON lines. Each log entry includes:
- timestamp, level, message (standard)
- request_id (correlation across a single chat request)
- component (which subsystem: tool, rag, hybrid, etc.)
- Extra fields as needed (tool_name, latency_ms, etc.)
"""

import json
import logging
import re
import uuid
from contextvars import ContextVar
from typing import Any, Dict

# Context variable for request ID tracking
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    return request_id_var.get()


def set_request_id(rid: str = "") -> str:
    if not rid:
        rid = uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    EXTRA_FIELDS = (
        "component",
        "tool_name",
        "latency_ms",
        "mode",
        "intensity",
        "model",
        "entity_id",
        "error_type",
        "result_count",
        "request_id",
    )

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Add request_id if set
        rid = request_id_var.get()
        if rid:
            entry["request_id"] = rid

        # Add any extra fields passed via extra={"component": "rag", ...}
        for key in self.EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def content_shape(text: Any) -> str:
    """Coarse, content-free description of model output for diagnostics:
    what it *looks like*, never what it says (safe to log)."""
    if not isinstance(text, str):
        return type(text).__name__
    t = text.lstrip()
    if not t:
        return "empty"
    if t.startswith("```"):
        return "code-fence"
    if t.startswith("<think>"):
        return "think-block"
    if t.startswith("<tool_call>"):
        return "tool-call-block"
    if t[0] in "{[":
        return "json-like"
    return "text"


# Where the query text starts in an access line's path: a "?", or one the
# client percent-encoded. uvicorn re-quotes the decoded path, so a "%3F" sent
# by the client is logged as "%3F" and a "%253F" as "%253F", text following.
_QUERY_START_RE = re.compile(r"\?|%(?:25)*3f", re.IGNORECASE)


# Tokenless audio URLs (/api/audio/<name>): the name is the only secret, and
# the clip is a spoken reply. Logged, it would be fetchable from Loki.
_AUDIO_NAME_RE = re.compile(r"(/api/audio/)[^/?#\s]+")


class StripQueryStringFilter(logging.Filter):
    """Uvicorn access lines: drop the query string (search endpoints carry the
    user's search text there: ?search=, ?query=) and the name of a spoken
    clip/stream under /api/audio/. Access lines go to Loki."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path = args[2]
            start = _QUERY_START_RE.search(path)
            if start:
                path = path[: start.start()] + "?…"
            path = _AUDIO_NAME_RE.sub(r"\1…", path)
            if path != args[2]:
                record.args = args[:2] + (path,) + args[3:]
        return True


# Third-party loggers that log full request URLs at INFO ("HTTP Request: GET
# http://…/?query=<user text>"). httpx is the client for SearXNG, Paperless,
# HA, Google… — the URL routinely carries the user's search words.
_URL_LOGGING_LIBS = ("httpx", "httpcore")


def quiet_url_logging_libs() -> None:
    for name in (*_URL_LOGGING_LIBS, *_PAYLOAD_LOGGING_LIBS):
        logging.getLogger(name).setLevel(logging.WARNING)


# Third-party loggers whose records can interpolate raw payloads: aioesphomeapi
# (voice satellite) logs decoded messages — mic audio included — at DEBUG and
# dumps raw bytes in some ERROR lines ("Invalid protobuf message … data=%s").
_PAYLOAD_LOGGING_LIBS = ("aioesphomeapi", "zeroconf")


class DropPayloadArgsFilter(logging.Filter):
    """Handler filter: keep only the format string of records from
    _PAYLOAD_LOGGING_LIBS (and their child loggers); their arguments are
    dropped, so no payload bytes reach Loki / the self-audit / Pushover."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.split(".", 1)[0] not in _PAYLOAD_LOGGING_LIBS:
            return True
        if record.args:
            record.msg = f"{record.msg} [args dropped]"
            record.args = None
        if record.exc_info:
            # Exception text / traceback locals can carry payload bytes too.
            exc = record.exc_info[1]
            record.msg = f"{record.msg} [exc: {type(exc).__name__ if exc else '?'}]"
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging(level: str = "INFO"):
    """Replace the root logger's handler with JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    handler.addFilter(DropPayloadArgsFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    quiet_url_logging_libs()

    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, StripQueryStringFilter) for f in access.filters):
        access.addFilter(StripQueryStringFilter())
