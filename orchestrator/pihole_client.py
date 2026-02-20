"""
Pi-hole v6 API Client for Focus Mode Blocking

Manages authentication and group enable/disable for focus session
distraction blocking. Supports multiple Pi-hole instances (Jupiter + Saturn)
with graceful per-instance failure handling.
"""

import asyncio
import os
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
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
    Pi-hole v6 API client for a single instance.

    Handles:
    - Session-based authentication (Pi-hole v6 uses SID, not static tokens)
    - Group enable/disable for focus blocking
    - Graceful failure when Pi-hole unavailable
    """

    def __init__(
        self,
        url: str,
        password: str = "",
        focus_group: str = "focus_blocklist",
        name: str = "",
    ):
        self.url = url.rstrip("/")
        self.password = password
        self.focus_group = focus_group
        self.name = name or self.url

        # Session ID for authenticated requests
        self._sid: Optional[str] = None

    async def _authenticate(self) -> bool:
        """
        Authenticate with Pi-hole and get session ID.
        Returns True if successful, False otherwise.
        """
        if not self.password:
            logger.warning("[PIHOLE:%s] No password configured, skipping authentication", self.name)
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
                    logger.info("[PIHOLE:%s] Authentication successful", self.name)
                    return True
                else:
                    logger.error("[PIHOLE:%s] Unexpected auth response: %s", self.name, data)
                    return False

        except Exception as e:
            logger.error("[PIHOLE:%s] Authentication failed: %s", self.name, e)
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
        # Authenticate first
        if not await self._authenticate():
            return PiHoleResult(
                success=False,
                message=f"[{self.name}] Failed to authenticate with Pi-hole"
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
                        message=f"[{self.name}] Focus group '{self.focus_group}' not found"
                    )

                # Filter domains that belong to focus group
                focus_domains = [
                    d for d in domains_data.get("domains", [])
                    if focus_group_id in d.get("groups", [])
                ]

                if not focus_domains:
                    return PiHoleResult(
                        success=True,
                        message=f"[{self.name}] No domains in focus group to toggle"
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
                logger.info("[PIHOLE:%s] Focus blocking %s for %d domains", self.name, action, toggled)

                return PiHoleResult(
                    success=True,
                    message=f"[{self.name}] Focus blocking {action} ({toggled} domains)",
                    details={"instance": self.name, "group": self.focus_group, "enabled": enabled, "domains_toggled": toggled}
                )

        except httpx.HTTPStatusError as e:
            logger.error("[PIHOLE:%s] API error: %s", self.name, e.response.status_code)
            return PiHoleResult(
                success=False,
                message=f"[{self.name}] Pi-hole API error: {e.response.status_code}"
            )
        except Exception as e:
            logger.error("[PIHOLE:%s] Error setting blocking status: %s", self.name, e)
            return PiHoleResult(
                success=False,
                message=f"[{self.name}] Pi-hole error: {str(e)}"
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
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.url}/api")
                return resp.status_code == 200
        except Exception:
            return False


class PiHoleMultiClient:
    """
    Manages multiple Pi-hole instances for redundant focus blocking.

    Applies operations to all instances concurrently. Succeeds if at least
    one instance succeeds — a single failure doesn't block the operation.
    """

    def __init__(self, clients: List[PiHoleClient], enabled: bool = True):
        self.clients = clients
        self.enabled = enabled

    async def enable_focus_blocking(self) -> PiHoleResult:
        """Enable focus blocking on all Pi-hole instances."""
        return await self._apply_all(enabled=True)

    async def disable_focus_blocking(self) -> PiHoleResult:
        """Disable focus blocking on all Pi-hole instances."""
        return await self._apply_all(enabled=False)

    async def _apply_all(self, enabled: bool) -> PiHoleResult:
        """Apply focus blocking change to all instances concurrently."""
        if not self.enabled:
            return PiHoleResult(success=True, message="Focus blocking disabled in config")

        if not self.clients:
            return PiHoleResult(success=True, message="No Pi-hole instances configured")

        results = await asyncio.gather(
            *[c.set_focus_group_enabled(enabled) for c in self.clients],
            return_exceptions=True,
        )

        succeeded = []
        failed = []
        for i, result in enumerate(results):
            name = self.clients[i].name
            if isinstance(result, Exception):
                failed.append(name)
                logger.error("[PIHOLE:%s] Exception: %s", name, result)
            elif result.success:
                succeeded.append(name)
            else:
                failed.append(name)

        action = "enabled" if enabled else "disabled"

        if succeeded:
            msg = f"Focus blocking {action} on {', '.join(succeeded)}"
            if failed:
                msg += f" (failed on {', '.join(failed)})"
            return PiHoleResult(
                success=True,
                message=msg,
                details={"succeeded": succeeded, "failed": failed, "enabled": enabled}
            )
        else:
            return PiHoleResult(
                success=False,
                message=f"Focus blocking {action} failed on all instances: {', '.join(failed)}",
                details={"succeeded": [], "failed": failed, "enabled": enabled}
            )

    async def is_available(self) -> bool:
        """Check if any Pi-hole instance is reachable."""
        if not self.enabled or not self.clients:
            return False
        checks = await asyncio.gather(*[c.is_available() for c in self.clients])
        return any(checks)


# Global client instance
_pihole_multi_client: Optional[PiHoleMultiClient] = None


def get_pihole_client() -> PiHoleMultiClient:
    """Get or create the global Pi-hole multi-client."""
    global _pihole_multi_client
    if _pihole_multi_client is not None:
        return _pihole_multi_client

    enabled = os.environ.get("FOCUS_BLOCKING_ENABLED", "true").lower() == "true"
    password = os.environ.get("PIHOLE_PASSWORD", "")
    focus_group = os.environ.get("PIHOLE_FOCUS_GROUP", "focus_blocklist")

    # PIHOLE_URLS takes precedence (comma-separated), falls back to single PIHOLE_URL
    urls_str = os.environ.get("PIHOLE_URLS", "")
    if urls_str:
        urls = [u.strip() for u in urls_str.split(",") if u.strip()]
    else:
        urls = [os.environ.get("PIHOLE_URL", "http://pihole:80")]

    clients = []
    for url in urls:
        # Derive a short name from the URL for logging
        name = url.split("//")[-1].split(":")[0]
        clients.append(PiHoleClient(
            url=url,
            password=password,
            focus_group=focus_group,
            name=name,
        ))

    logger.info("[PIHOLE] Initialized %d instance(s): %s", len(clients), ", ".join(c.name for c in clients))
    _pihole_multi_client = PiHoleMultiClient(clients=clients, enabled=enabled)
    return _pihole_multi_client
