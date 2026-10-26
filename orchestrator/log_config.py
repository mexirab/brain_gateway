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


class StripQueryStringFilter(logging.Filter):
    """Uvicorn access lines: drop the query string. Search endpoints carry the
    user's search text there (?search=, ?query=), and access lines go to Loki."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            start = _QUERY_START_RE.search(args[2])
            if start:
                record.args = args[:2] + (args[2][: start.start()] + "?…",) + args[3:]
        return True


# Third-party loggers that log full request URLs at INFO ("HTTP Request: GET
# http://…/?query=<user text>"). httpx is the client for SearXNG, Paperless,
# HA, Google… — the URL routinely carries the user's search words.
_URL_LOGGING_LIBS = ("httpx", "httpcore")


def quiet_url_logging_libs() -> None:
    for name in _URL_LOGGING_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(level: str = "INFO"):
    """Replace the root logger's handler with JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    quiet_url_logging_libs()

    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, StripQueryStringFilter) for f in access.filters):
        access.addFilter(StripQueryStringFilter())
