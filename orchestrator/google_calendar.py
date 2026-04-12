"""
Google Calendar Client for Brain Gateway

Provides calendar read/write via Google Calendar API v3.
Uses httpx for async HTTP (consistent with rest of codebase).
Gracefully disabled when OAuth2 credentials are not configured.

Usage:
    client = get_calendar_client(http_client=_http)
    events = await client.list_events(days_ahead=7)
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx

from orchestrator.google_auth import get_credentials

logger = logging.getLogger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TIMEZONE = os.environ.get("TZ", "America/Chicago")


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


@dataclass
class CalendarResponse:
    """Response from a calendar query."""

    success: bool
    events: List[CalendarEvent] = field(default_factory=list)
    error: Optional[str] = None


class GoogleCalendarClient:
    """
    Google Calendar API v3 client.

    Handles:
    - Listing events with day-range filtering
    - Creating new events
    - Fetching upcoming events for proactive notifications
    - Graceful fallback when not configured
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client
        self._creds = get_credentials()
        self.is_configured = self._creds is not None
        if self.is_configured:
            logger.info("[CALENDAR] Google Calendar client initialized")
        else:
            logger.info("[CALENDAR] Google Calendar not configured — tools disabled")

    def _auth_headers(self) -> dict:
        """Get authorization headers, refreshing token if needed."""
        if not self._creds:
            return {}
        if self._creds.expired and self._creds.refresh_token:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return {"Authorization": f"Bearer {self._creds.token}"}

    async def _get(self, path: str, params: dict = None) -> dict:
        """Make authenticated GET request to Calendar API."""
        url = f"{CALENDAR_API}{path}"
        headers = self._auth_headers()
        if self._http:
            resp = await self._http.get(url, headers=headers, params=params, timeout=15)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_data: dict) -> dict:
        """Make authenticated POST request to Calendar API."""
        url = f"{CALENDAR_API}{path}"
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        if self._http:
            resp = await self._http.post(url, headers=headers, json=json_data, timeout=15)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers=headers, json=json_data)
        resp.raise_for_status()
        return resp.json()

    def _parse_event(self, item: dict) -> CalendarEvent:
        """Parse a Google Calendar API event item into CalendarEvent."""
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})
        tz = ZoneInfo(TIMEZONE)

        # All-day events use 'date', timed events use 'dateTime'
        all_day = "date" in start_raw and "dateTime" not in start_raw
        if all_day:
            start = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=tz)
            end = datetime.fromisoformat(end_raw.get("date", start_raw["date"])).replace(tzinfo=tz)
        else:
            start = datetime.fromisoformat(start_raw.get("dateTime", ""))
            end = datetime.fromisoformat(end_raw.get("dateTime", ""))

        return CalendarEvent(
            id=item.get("id", ""),
            title=item.get("summary", "(No title)"),
            start=start,
            end=end,
            location=item.get("location", ""),
            description=item.get("description", ""),
            all_day=all_day,
        )

    async def list_events(self, days_ahead: int = 7, calendar_id: str = "primary") -> CalendarResponse:
        """
        List calendar events for the next N days.

        Args:
            days_ahead: Number of days to look ahead (default 7)
            calendar_id: Calendar ID (default "primary")

        Returns:
            CalendarResponse with events or error
        """
        if not self.is_configured:
            return CalendarResponse(
                success=False,
                error="Google Calendar not configured. Run google_setup.py first.",
            )

        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()

        logger.info(f"[CALENDAR] Listing events for next {days_ahead} days")

        try:
            data = await self._get(
                f"/calendars/{calendar_id}/events",
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "50",
                },
            )

            events = [self._parse_event(item) for item in data.get("items", [])]
            logger.info(f"[CALENDAR] Found {len(events)} events")
            return CalendarResponse(success=True, events=events)

        except httpx.HTTPStatusError as e:
            logger.error(f"[CALENDAR] API error: {e.response.status_code}")
            return CalendarResponse(success=False, error=f"Calendar API error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[CALENDAR] Error: {e}")
            return CalendarResponse(success=False, error=str(e))

    async def create_event(
        self,
        title: str,
        start_time: str,
        duration_minutes: int = 60,
        description: str = "",
        location: str = "",
        calendar_id: str = "primary",
    ) -> CalendarResponse:
        """
        Create a new calendar event.

        Args:
            title: Event title/summary
            start_time: ISO 8601 datetime string (e.g. "2026-02-21T19:00:00")
            duration_minutes: Duration in minutes (default 60)
            description: Optional event description
            location: Optional location
            calendar_id: Calendar ID (default "primary")

        Returns:
            CalendarResponse with the created event
        """
        if not self.is_configured:
            return CalendarResponse(
                success=False,
                error="Google Calendar not configured. Run google_setup.py first.",
            )

        tz = ZoneInfo(TIMEZONE)

        try:
            start = datetime.fromisoformat(start_time)
            if start.tzinfo is None:
                start = start.replace(tzinfo=tz)
            end = start + timedelta(minutes=duration_minutes)
        except ValueError as e:
            return CalendarResponse(success=False, error=f"Invalid start_time: {e}")

        event_body = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
        }
        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location

        logger.info(f"[CALENDAR] Creating event: {title} at {start}")

        try:
            data = await self._post(f"/calendars/{calendar_id}/events", event_body)
            event = self._parse_event(data)
            link = data.get("htmlLink", "")
            logger.info(f"[CALENDAR] Created event: {event.id} — {link}")
            return CalendarResponse(success=True, events=[event])

        except httpx.HTTPStatusError as e:
            logger.error(f"[CALENDAR] Create error: {e.response.status_code}")
            return CalendarResponse(success=False, error=f"Failed to create event: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[CALENDAR] Create error: {e}")
            return CalendarResponse(success=False, error=str(e))

    async def get_upcoming(self, hours_ahead: int = 2) -> CalendarResponse:
        """
        Get events starting within the next N hours.
        Used by proactive calendar polling.
        """
        if not self.is_configured:
            return CalendarResponse(success=False, error="Not configured")

        tz = ZoneInfo(TIMEZONE)
        now = datetime.now(tz)
        time_min = now.isoformat()
        time_max = (now + timedelta(hours=hours_ahead)).isoformat()

        try:
            data = await self._get(
                "/calendars/primary/events",
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "10",
                },
            )

            events = [self._parse_event(item) for item in data.get("items", [])]
            return CalendarResponse(success=True, events=events)

        except Exception as e:
            logger.error(f"[CALENDAR] Upcoming poll error: {e}")
            return CalendarResponse(success=False, error=str(e))


# Global client instance
_calendar_client: Optional[GoogleCalendarClient] = None


def get_calendar_client(
    http_client: Optional[httpx.AsyncClient] = None,
) -> GoogleCalendarClient:
    """Get or create the global Google Calendar client."""
    global _calendar_client
    if _calendar_client is None:
        _calendar_client = GoogleCalendarClient(http_client=http_client)
    elif http_client and _calendar_client._http is None:
        _calendar_client._http = http_client
    return _calendar_client
