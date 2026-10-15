"""Shared calendar data types.

Every calendar source (self-hosted CalDAV, the iPhone Exchange sync, Google)
produces these, so callers never care where an event came from — except to
label it (``source``) or to say a source couldn't be reached
(``CalendarResponse.failed_sources``).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Source names. "phone" is the legacy all-calendars iPhone push (pre-2026-10);
# "exchange" is the work-calendar-only Shortcut that replaces it.
SOURCE_CALDAV = "caldav"
SOURCE_EXCHANGE = "exchange"
SOURCE_PHONE = "phone"
SOURCE_GOOGLE = "google"

# Human labels for TTS / tool output.
SOURCE_LABELS = {
    SOURCE_CALDAV: "personal",
    SOURCE_EXCHANGE: "work",
    SOURCE_PHONE: "phone",
    SOURCE_GOOGLE: "Google",
}


@dataclass
class CalendarEvent:
    """A single calendar event."""

    id: str
    title: str
    start: datetime
    end: datetime
    location: str = ""
    description: str = ""
    calendar_name: str = "primary"
    all_day: bool = False
    source: str = SOURCE_GOOGLE


@dataclass
class CalendarResponse:
    """Response from a calendar query or write.

    ``success`` is True when at least one source answered. ``failed_sources``
    lists sources that were expected but could not be read (unreachable, or a
    stale/corrupt phone sync), so callers can say "I couldn't reach your work
    calendar" instead of presenting a partial list as complete.
    """

    success: bool
    events: List[CalendarEvent] = field(default_factory=list)
    error: Optional[str] = None
    failed_sources: List[str] = field(default_factory=list)
