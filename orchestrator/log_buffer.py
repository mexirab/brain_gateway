"""
In-memory log ring buffer for Jess self-diagnosis.

Captures the last N log lines in a deque so the check_system tool
can query recent logs without needing Docker socket access or subprocess calls.
"""

import logging
import collections
from datetime import datetime
from typing import List, Dict


class LogRingBuffer(logging.Handler):
    """Logging handler that stores recent log entries in a ring buffer."""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.buffer: collections.deque = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append({
            "time": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "message": self.format(record),
        })

    def search(self, pattern: str, limit: int = 20) -> List[Dict]:
        """Search buffer for entries containing pattern (case-insensitive)."""
        pattern_lower = pattern.lower()
        return [
            e for e in reversed(self.buffer)
            if pattern_lower in e["message"].lower()
        ][:limit]

    def errors(self, limit: int = 20) -> List[Dict]:
        """Get recent ERROR and CRITICAL entries."""
        return [
            e for e in reversed(self.buffer)
            if e["level"] in ("ERROR", "CRITICAL")
        ][:limit]

    def recent(self, limit: int = 50) -> List[Dict]:
        """Get the most recent log entries."""
        return list(reversed(list(self.buffer)))[:limit]


# Module-level singleton — attached in orchestrator.py startup
log_ring: LogRingBuffer = LogRingBuffer(capacity=1000)
