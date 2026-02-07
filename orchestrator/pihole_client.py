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
        Enable or disable focus blocking by toggling domain enabled state.

        Pi-hole v6 approach: domains in focus_blocklist group are toggled
        directly since group membership requires client assignment.

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
            async with httpx.AsyncClient(timeout=30) as client:
                # First, get all domains in the focus group
                resp = await client.get(
                    f"{self.url}/api/domains/deny/exact",
                    headers={"sid": self._sid}
                )
                resp.raise_for_status()
                domains_data = resp.json()

                # Find focus group ID
                groups_resp = await client.get(
                    f"{self.url}/api/groups",
                    headers={"sid": self._sid}
                )
                groups_resp.raise_for_status()
                groups = groups_resp.json().get("groups", [])
                focus_group_id = None
                for g in groups:
                    if g.get("name") == self.focus_group:
                        focus_group_id = g.get("id")
                        break

                if focus_group_id is None:
                    return PiHoleResult(
                        success=False,
                        message=f"Focus group '{self.focus_group}' not found"
                    )

                # Filter domains that belong to focus group
                focus_domains = [
                    d for d in domains_data.get("domains", [])
                    if focus_group_id in d.get("groups", [])
                ]

                if not focus_domains:
                    return PiHoleResult(
                        success=True,
                        message="No domains in focus group to toggle"
                    )

                # Toggle each domain's enabled state and add to Default group (0) when enabling
                toggled = 0
                for domain in focus_domains:
                    domain_name = domain.get("domain")
                    current_groups = domain.get("groups", [])

                    if enabled:
                        # Add to Default group (0) so it applies to all clients
                        new_groups = list(set(current_groups + [0]))
                    else:
                        # Remove from Default group (0)
                        new_groups = [g for g in current_groups if g != 0]

                    update_resp = await client.put(
                        f"{self.url}/api/domains/deny/exact/{domain_name}",
                        headers={
                            "Content-Type": "application/json",
                            "sid": self._sid
                        },
                        json={"groups": new_groups}
                    )
                    if update_resp.status_code == 200:
                        toggled += 1

                action = "enabled" if enabled else "disabled"
                logger.info(f"[PIHOLE] Focus blocking {action} for {toggled} domains")

                return PiHoleResult(
                    success=True,
                    message=f"Focus blocking {action} ({toggled} domains)",
                    details={"group": self.focus_group, "enabled": enabled, "domains_toggled": toggled}
                )

        except httpx.HTTPStatusError as e:
            logger.error(f"[PIHOLE] API error: {e.response.status_code}")
            return PiHoleResult(
                success=False,
                message=f"Pi-hole API error: {e.response.status_code}"
            )
        except Exception as e:
            logger.error(f"[PIHOLE] Error setting blocking status: {e}")
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
