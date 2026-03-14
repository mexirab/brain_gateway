"""
Google Gmail Client for Brain Gateway

Provides read-only Gmail access via Gmail API v1.
Uses httpx for async HTTP (consistent with rest of codebase).
Gracefully disabled when OAuth2 credentials are not configured.

Usage:
    client = get_gmail_client(http_client=_http)
    response = await client.list_messages(query="is:unread", max_results=10)
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional, Set

import httpx

from google_auth import get_credentials

logger = logging.getLogger(__name__)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class EmailMessage:
    """A single email message."""

    id: str
    thread_id: str
    subject: str
    sender: str
    date: datetime
    snippet: str
    labels: List[str] = field(default_factory=list)
    body_text: str = ""


@dataclass
class GmailResponse:
    """Response from a Gmail query."""

    success: bool
    messages: List[EmailMessage] = field(default_factory=list)
    total_estimate: int = 0
    error: Optional[str] = None


class GoogleGmailClient:
    """
    Google Gmail API v1 client (read-only).

    Handles:
    - Listing and searching messages
    - Fetching message details with body text
    - Unread count
    - Graceful fallback when not configured
    """

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client
        self._creds = get_credentials()
        self.is_configured = self._creds is not None
        # Track announced email IDs to avoid duplicate TTS announcements
        self._announced_ids: Set[str] = set()
        if self.is_configured:
            logger.info("[GMAIL] Gmail client initialized")
        else:
            logger.info("[GMAIL] Gmail not configured — tools disabled")

    def _auth_headers(self) -> dict:
        """Get authorization headers, refreshing token if needed."""
        if not self._creds:
            return {}
        if self._creds.expired and self._creds.refresh_token:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return {"Authorization": f"Bearer {self._creds.token}"}

    async def _get(self, path: str, params: dict = None) -> dict:
        """Make authenticated GET request to Gmail API."""
        url = f"{GMAIL_API}{path}"
        headers = self._auth_headers()
        if self._http:
            resp = await self._http.get(url, headers=headers, params=params, timeout=15)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload.

        Walks MIME parts to find text/plain, falls back to text/html
        with tags stripped. Truncates to 2000 chars.
        """
        # Simple message (no parts)
        if "parts" not in payload:
            body_data = payload.get("body", {}).get("data", "")
            mime_type = payload.get("mimeType", "")
            if body_data:
                text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
                if "html" in mime_type:
                    text = re.sub(r"<[^>]+>", "", text)
                    text = re.sub(r"\s+", " ", text).strip()
                return text[:2000]
            return ""

        # Multipart — find text/plain first, then text/html
        plain_text = ""
        html_text = ""
        for part in payload.get("parts", []):
            mime_type = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data", "")

            if mime_type == "text/plain" and body_data:
                plain_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            elif mime_type == "text/html" and body_data:
                html_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")
            elif "parts" in part:
                # Nested multipart (e.g. multipart/alternative inside multipart/mixed)
                nested = self._extract_body(part)
                if nested and not plain_text:
                    plain_text = nested

        if plain_text:
            return plain_text[:2000]
        if html_text:
            text = re.sub(r"<[^>]+>", "", html_text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:2000]
        return ""

    def _parse_message(self, data: dict) -> EmailMessage:
        """Parse a Gmail API message response into EmailMessage."""
        headers = {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}

        # Parse date
        date_str = headers.get("date", "")
        try:
            date = parsedate_to_datetime(date_str)
        except Exception:
            date = datetime.now()

        body_text = self._extract_body(data.get("payload", {}))

        return EmailMessage(
            id=data.get("id", ""),
            thread_id=data.get("threadId", ""),
            subject=headers.get("subject", "(No subject)"),
            sender=headers.get("from", "Unknown"),
            date=date,
            snippet=data.get("snippet", ""),
            labels=data.get("labelIds", []),
            body_text=body_text,
        )

    async def list_messages(self, query: str = "", max_results: int = 10, label: str = "INBOX") -> GmailResponse:
        """
        List Gmail messages matching a query.

        Args:
            query: Gmail search query (e.g. "is:unread", "from:amazon")
            max_results: Maximum messages to return (default 10)
            label: Label filter (default "INBOX")

        Returns:
            GmailResponse with messages or error
        """
        if not self.is_configured:
            return GmailResponse(
                success=False,
                error="Gmail not configured. Run google_setup.py first.",
            )

        logger.info(f"[GMAIL] Listing messages: query='{query}', max={max_results}")

        try:
            params = {"maxResults": str(max_results)}
            if query:
                params["q"] = query
            if label:
                params["labelIds"] = label

            data = await self._get("/messages", params=params)

            message_stubs = data.get("messages", [])
            total_estimate = data.get("resultSizeEstimate", 0)

            if not message_stubs:
                return GmailResponse(success=True, total_estimate=0)

            # Fetch full details for each message
            messages = []
            for stub in message_stubs:
                try:
                    msg_data = await self._get(f"/messages/{stub['id']}", params={"format": "full"})
                    messages.append(self._parse_message(msg_data))
                except Exception as e:
                    logger.warning(f"[GMAIL] Failed to fetch message {stub['id']}: {e}")

            logger.info(f"[GMAIL] Found {len(messages)} messages (est. {total_estimate} total)")
            return GmailResponse(success=True, messages=messages, total_estimate=total_estimate)

        except httpx.HTTPStatusError as e:
            logger.error(f"[GMAIL] API error: {e.response.status_code}")
            return GmailResponse(success=False, error=f"Gmail API error: {e.response.status_code}")
        except Exception as e:
            logger.error(f"[GMAIL] Error: {e}")
            return GmailResponse(success=False, error=str(e))

    async def get_message(self, message_id: str) -> GmailResponse:
        """Fetch a single message by ID."""
        if not self.is_configured:
            return GmailResponse(success=False, error="Gmail not configured.")

        try:
            data = await self._get(f"/messages/{message_id}", params={"format": "full"})
            msg = self._parse_message(data)
            return GmailResponse(success=True, messages=[msg])
        except Exception as e:
            logger.error(f"[GMAIL] Get message error: {e}")
            return GmailResponse(success=False, error=str(e))

    async def get_unread_count(self) -> int:
        """Get approximate count of unread messages in inbox."""
        if not self.is_configured:
            return 0
        try:
            data = await self._get(
                "/messages",
                params={"q": "is:unread", "labelIds": "INBOX", "maxResults": "1"},
            )
            return data.get("resultSizeEstimate", 0)
        except Exception:
            return 0


# Global client instance
_gmail_client: Optional[GoogleGmailClient] = None


def get_gmail_client(
    http_client: Optional[httpx.AsyncClient] = None,
) -> GoogleGmailClient:
    """Get or create the global Gmail client."""
    global _gmail_client
    if _gmail_client is None:
        _gmail_client = GoogleGmailClient(http_client=http_client)
    elif http_client and _gmail_client._http is None:
        _gmail_client._http = http_client
    return _gmail_client
