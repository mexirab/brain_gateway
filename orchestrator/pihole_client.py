"""
Pi-hole v6 API Client for Focus Mode Blocking

Manages authentication and group enable/disable for focus session
distraction blocking. Gracefully handles Pi-hole being unavailable.
"""

import os
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
import httpx

logger = logging.getLogger(__name__)


@dataclass
class PiHoleResult:
    """Result of a Pi-hole API operation."""
    success: bool
    message: str
    details: Optional[Dict[str, Any]] = None


class PiHoleClient:
    """
    Pi-hole v6 API client for managing focus mode blocking.

    Handles:
    - Session-based authentication (Pi-hole v6 uses SID, not static tokens)
    - Group enable/disable for focus blocking
    - Graceful failure when Pi-hole unavailable
    """

    def __init__(
        self,
        url: Optional[str] = None,
        password: Optional[str] = None,
        focus_group: Optional[str] = None,
        enabled: bool = True,
    ):
        self.url = (url or os.environ.get("PIHOLE_URL", "http://pihole:80")).rstrip("/")
        self.password = password or os.environ.get("PIHOLE_PASSWORD", "")
        self.focus_group = focus_group or os.environ.get("PIHOLE_FOCUS_GROUP", "focus_blocklist")
        self.enabled = enabled and os.environ.get("FOCUS_BLOCKING_ENABLED", "true").lower() == "true"

        # Session ID for authenticated requests
        self._sid: Optional[str] = None

    async def _authenticate(self) -> bool:
        """
        Authenticate with Pi-hole and get session ID.
        Returns True if successful, False otherwise.
        """
        if not self.password:
            logger.warning("[PIHOLE] No password configured, skipping authentication")
            return False

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.url}/api/auth",
                    json={"password": self.password}
                )
                resp.raise_for_status()
                data = resp.json()

                if "session" in data and "sid" in data["session"]:
                    self._sid = data["session"]["sid"]
                    logger.info("[PIHOLE] Authentication successful")
                    return True
                else:
                    logger.error(f"[PIHOLE] Unexpected auth response: {data}")
                    return False

        except Exception as e:
            logger.error(f"[PIHOLE] Authentication failed: {e}")
            return False

    async def _end_session(self) -> None:
        """End the current session (cleanup)."""
        if not self._sid:
            return

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.delete(
                    f"{self.url}/api/auth",
                    headers={"sid": self._sid}
                )
        except Exception:
            pass  # Best effort cleanup
        finally:
            self._sid = None

    async def set_focus_group_enabled(self, enabled: bool) -> PiHoleResult:
        """
        Enable or disable the focus blocking group.

        Args:
            enabled: True to enable blocking (block distracting sites),
                    False to disable blocking (allow all sites)

        Returns:
            PiHoleResult with success status and message
        """
        if not self.enabled:
            return PiHoleResult(
                success=True,
                message="Focus blocking disabled in config"
            )

        # Authenticate first
        if not await self._authenticate():
            return PiHoleResult(
                success=False,
                message="Failed to authenticate with Pi-hole"
            )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # PUT to update group enabled status
                resp = await client.put(
                    f"{self.url}/api/groups/{self.focus_group}",
                    headers={
                        "Content-Type": "application/json",
                        "sid": self._sid
                    },
                    json={"enabled": enabled}
                )

                if resp.status_code == 404:
                    return PiHoleResult(
                        success=False,
                        message=f"Focus group '{self.focus_group}' not found in Pi-hole"
                    )

                resp.raise_for_status()

                action = "enabled" if enabled else "disabled"
                logger.info(f"[PIHOLE] Focus blocking {action}")

                return PiHoleResult(
                    success=True,
                    message=f"Focus blocking {action}",
                    details={"group": self.focus_group, "enabled": enabled}
                )

        except httpx.HTTPStatusError as e:
            logger.error(f"[PIHOLE] API error: {e.response.status_code}")
            return PiHoleResult(
                success=False,
                message=f"Pi-hole API error: {e.response.status_code}"
            )
        except Exception as e:
            logger.error(f"[PIHOLE] Error setting group status: {e}")
            return PiHoleResult(
                success=False,
                message=f"Pi-hole error: {str(e)}"
            )
        finally:
            await self._end_session()

    async def enable_focus_blocking(self) -> PiHoleResult:
        """Enable focus mode blocking (block distracting sites)."""
        return await self.set_focus_group_enabled(enabled=True)

    async def disable_focus_blocking(self) -> PiHoleResult:
        """Disable focus mode blocking (allow all sites)."""
        return await self.set_focus_group_enabled(enabled=False)

    async def is_available(self) -> bool:
        """Check if Pi-hole is reachable."""
        if not self.enabled:
            return False

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.url}/api")
                return resp.status_code == 200
        except Exception:
            return False


# Global client instance
pihole_client: Optional[PiHoleClient] = None


def get_pihole_client() -> PiHoleClient:
    """Get or create the global Pi-hole client."""
    global pihole_client
    if pihole_client is None:
        pihole_client = PiHoleClient()
    return pihole_client
