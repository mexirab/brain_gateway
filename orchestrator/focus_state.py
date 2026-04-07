"""
Focus session state for Brain Gateway.

Replaces the plain dict in shared.py with a class that supports both
attribute access (new code) and dict-style access (backward compat).
"""

from datetime import datetime
from typing import Any, Optional

_DEFAULTS = {
    "active": False,
    "task": None,
    "started": None,
    "duration": None,
    "break_duration": None,
    "job_id": None,
    "audio_player": None,
    "block_sites": False,
    "task_description": None,
    "sprint_count": 0,
    "sprints_planned": None,
    "check_in_interval": None,
    "check_in_job_id": None,
    "total_focus_minutes": 0,
    "audio_source": "endel",
}


class FocusSession:
    """
    Mutable focus session state with dict-style backward compatibility.

    Supports:
        session["key"]           # __getitem__
        session["key"] = value   # __setitem__
        session.get("key", default)
        session.update({...})
    """

    def __init__(self):
        self._data: dict = dict(_DEFAULTS)

    # -- Dict-style access (backward compat) ----------------------------------

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def update(self, data: dict) -> None:
        self._data.update(data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    # -- Attribute access (new code) ------------------------------------------

    @property
    def active(self) -> bool:
        return self._data["active"]

    @property
    def task(self) -> Optional[str]:
        return self._data["task"]

    @property
    def started(self) -> Optional[datetime]:
        return self._data["started"]

    @property
    def duration(self) -> Optional[int]:
        return self._data["duration"]

    # -- Methods --------------------------------------------------------------

    def reset(self) -> None:
        """Reset to default inactive state."""
        self._data.update(_DEFAULTS)

    def to_dict(self) -> dict:
        """Return a copy of the internal state as a plain dict."""
        return dict(self._data)

    def __repr__(self) -> str:
        if self.active:
            return f"<FocusSession active=True task={self.task!r}>"
        return "<FocusSession active=False>"
