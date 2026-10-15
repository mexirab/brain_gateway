"""Test helpers: fake CalDAV / Google clients behind calendar_sources.

Callers (briefings, ambient, wind-down) run the REAL calendar_sources merge —
including the phone-push cache in ``shared`` — with only the network clients
replaced, so tests exercise the same source/merge/failure logic as production.
"""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.calendar_types import CalendarEvent, CalendarResponse


def make_event(title, start: datetime, *, source="google", all_day=False, location="", minutes=60) -> CalendarEvent:
    end = start + (timedelta(days=1) if all_day else timedelta(minutes=minutes))
    return CalendarEvent(
        id=f"{source}:{title}:{start.isoformat()}",
        title=title,
        start=start,
        end=end,
        location=location,
        all_day=all_day,
        source=source,
    )


def fake_client(*, configured=True, success=True, events=None) -> MagicMock:
    client = MagicMock()
    client.is_configured = configured
    client.list_window = AsyncMock(
        return_value=CalendarResponse(success=success, events=list(events or []), error=None if success else "down")
    )
    client.create_event = AsyncMock()
    return client


@contextmanager
def patched_calendar(*, google=None, caldav=None, google_enabled=True):
    """Patch calendar_sources' client factories. ``None`` = not configured."""
    from orchestrator import calendar_sources, shared

    google = google or fake_client(configured=False)
    caldav = caldav or fake_client(configured=False)
    with ExitStack() as stack:
        stack.enter_context(patch.object(calendar_sources, "get_google_client", lambda http_client=None: google))
        stack.enter_context(patch.object(calendar_sources, "get_caldav_client", lambda http_client=None: caldav))
        stack.enter_context(patch.object(shared, "CALENDAR_GOOGLE_ENABLED", google_enabled))
        stack.enter_context(patch.object(shared, "PHONE_CALENDAR_SYNC_ENABLED", True))
        stack.enter_context(patch.object(shared, "PHONE_CALENDAR_MAX_AGE_HOURS", 24.0))
        yield MagicMock(google=google, caldav=caldav)


def no_calendar_patch():
    """A patch (context manager) that makes calendar_sources answer 'no calendar configured'."""
    return patch(
        "orchestrator.calendar_sources.get_events",
        new_callable=AsyncMock,
        return_value=CalendarResponse(success=False, error="No calendar is configured"),
    )
