"""
Google OAuth2 Authentication Helper

Manages OAuth2 credentials for Google APIs (Calendar, Gmail, etc.).
Handles token loading, refresh, and graceful fallback when not configured.

Setup:
    python google_setup.py --credentials ./credentials/google_credentials.json
"""

import os
import json
import logging
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]


def get_credentials(
    token_path: Optional[str] = None,
    credentials_path: Optional[str] = None,
) -> Optional[Credentials]:
    """
    Load and refresh Google OAuth2 credentials.

    Returns None if not configured (no credentials file).
    Raises if credentials exist but are invalid/expired and can't refresh.
    """
    token_path = token_path or os.environ.get(
        "GOOGLE_TOKEN_PATH", "/app/credentials/google_token.json"
    )
    credentials_path = credentials_path or os.environ.get(
        "GOOGLE_CREDENTIALS_PATH", "/app/credentials/google_credentials.json"
    )

    if not os.path.exists(credentials_path):
        logger.info("[GOOGLE_AUTH] No credentials file found — Google Calendar disabled")
        return None

    creds = None

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            logger.warning(f"[GOOGLE_AUTH] Failed to load token: {e}")

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            # Save refreshed token
            with open(token_path, "w") as f:
                f.write(creds.to_json())
            logger.info("[GOOGLE_AUTH] Token refreshed successfully")
        except Exception as e:
            logger.error(f"[GOOGLE_AUTH] Token refresh failed: {e}")
            return None

    if not creds or not creds.valid:
        logger.warning(
            "[GOOGLE_AUTH] No valid token — run google_setup.py to authenticate"
        )
        return None

    return creds
