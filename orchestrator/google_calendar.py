"""
Google Calendar Client for Brain Gateway

Provides calendar read/write via Google Calendar API v3.
Uses httpx for async HTTP (consistent with rest of codebase).
Gracefully disabled when OAuth2 credentials are not configured.

Usage:
    client = get_calendar_client(http_client=_http)
    resp = await client.list_window(start, end)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from orchestrator.calendar_types import CalendarEvent, CalendarResponse  # noqa: F401 — re-exported
from orchestrator.google_auth import get_credentials

logger = logging.getLogger(__name__)

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
TIMEZONE = os.environ.get("TZ", "America/Chicago")


class GoogleCalendarClient:
    """
    Google Calendar API v3 client.

    Handles:
    - Listing event instances in a time window (list_window, used by
      calendar_sources — the only reader)
    - Creating new events (only when CalDAV isn't configured)
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
        # Token refresh is a blocking HTTP call — keep it off the event loop.
        headers = await asyncio.to_thread(self._auth_headers)
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
        # Token refresh is a blocking HTTP call — keep it off the event loop.
        headers = await asyncio.to_thread(self._auth_headers)
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

    async def list_window(self, start: datetime, end: datetime, calendar_id: str = "primary") -> CalendarResponse:
        """List event instances overlapping [start, end) (recurrences expanded).

        Follows nextPageToken (up to 5 pages of 250) so a long window isn't
        silently truncated.
        """
        if not self.is_configured:
            return CalendarResponse(success=False, error="Google Calendar not configured")
        try:
            items: list = []
            page_token = None
            for _ in range(5):
                params = {
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": "250",
                }
                if page_token:
                    params["pageToken"] = page_token
                data = await self._get(f"/calendars/{calendar_id}/events", params=params)
                items.extend(data.get("items", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            return CalendarResponse(success=True, events=[self._parse_event(i) for i in items])
        except httpx.HTTPStatusError as e:
            logger.warning(f"[CALENDAR] Google API error: {e.response.status_code}")
            return CalendarResponse(success=False, error=f"Calendar API error: {e.response.status_code}")
        except Exception as e:
            logger.warning(f"[CALENDAR] Google read error: {type(e).__name__}")
            return CalendarResponse(success=False, error="Google Calendar request failed")

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

        logger.info(f"[CALENDAR] Creating event ({len(title)}-char title) at {start}")

        try:
            data = await self._post(f"/calendars/{calendar_id}/events", event_body)
            event = self._parse_event(data)
            logger.info(f"[CALENDAR] Created event: {event.id}")
            return CalendarResponse(success=True, events=[event])

        except httpx.HTTPStatusError as e:
            logger.error(f"[CALENDAR] Create error: {e.response.status_code}")
            return CalendarResponse(success=False, error=f"Failed to create event: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[CALENDAR] Create error: {e}")
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
