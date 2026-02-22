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
        "component", "tool_name", "latency_ms", "mode", "intensity",
        "model", "entity_id", "error_type", "result_count", "request_id",
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


def configure_logging(level: str = "INFO"):
    """Replace the root logger's handler with JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
