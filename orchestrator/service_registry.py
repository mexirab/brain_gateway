"""
Service registry for Brain Gateway.

Tracks health of external services and auto-disables features when
services are unavailable. Runs health checks at startup and periodically.

Usage:
    from orchestrator.service_registry import services, check_all_services

    if services.is_healthy("tts"):
        await announce(...)

    status = services.status_summary()  # for /api/services endpoint
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

import httpx
from prometheus_client import Gauge

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Prometheus gauge: 1 = healthy, 0 = unhealthy, per service
SERVICE_HEALTHY = Gauge(
    "bgw_service_healthy",
    "Whether an external service is reachable (1=healthy, 0=unhealthy)",
    ["service"],
)


@dataclass
class ServiceInfo:
    """Health state for a single external service."""

    name: str
    url: str = ""
    healthy: bool = False
    last_check: float = 0.0
    last_error: str = ""
    features_when_down: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return public-safe summary (no internal URLs)."""
        return {
            "name": self.name,
            "configured": bool(self.url),
            "healthy": self.healthy,
            "last_check_ago": f"{int(time.time() - self.last_check)}s" if self.last_check else "never",
            "last_error": self.last_error,
            "features_disabled_when_down": self.features_when_down,
        }


class ServiceRegistry:
    """Registry of external services with health tracking."""

    def __init__(self):
        self._services: Dict[str, ServiceInfo] = {}
        self._init_services()

    def _init_services(self):
        """Define all known services and their URLs from config."""
        self._services = {
            "model": ServiceInfo(
                name="Primary LLM",
                url=settings.model_url,
                features_when_down=["chat"],
            ),
            "fallback_model": ServiceInfo(
                name="Fallback LLM",
                url=settings.fallback_model_url,
                features_when_down=[],
            ),
            "tts": ServiceInfo(
                name="TTS",
                url=settings.tts_url,
                features_when_down=["announcements", "reminders_voice", "morning_briefing"],
            ),
            "stt": ServiceInfo(
                name="STT",
                url=settings.stt_url,
                features_when_down=["voice_input"],
            ),
            "ha": ServiceInfo(
                name="Home Assistant",
                url=settings.ha_url,
                features_when_down=["home_control", "presence", "speakers", "temperature"],
            ),
            "vision": ServiceInfo(
                name="Vision Model",
                url=settings.vision_model_url,
                features_when_down=["analyze_image"],
            ),
            "searxng": ServiceInfo(
                name="SearXNG",
                url=settings.searxng_url,
                features_when_down=["web_search"],
            ),
        }

    def is_healthy(self, service_name: str) -> bool:
        """Check if a service is currently healthy."""
        svc = self._services.get(service_name)
        if not svc:
            return False
        if not svc.url:
            return False
        return svc.healthy

    def is_configured(self, service_name: str) -> bool:
        """Check if a service has a URL configured."""
        svc = self._services.get(service_name)
        return bool(svc and svc.url)

    def get_disabled_tools(self) -> List[str]:
        """Return tool names that should be hidden because their service is down."""
        disabled = []
        for svc in self._services.values():
            if svc.url and not svc.healthy:
                disabled.extend(svc.features_when_down)
        return disabled

    def status_summary(self) -> dict:
        """Return a summary of all services for API/dashboard."""
        configured = {}
        unconfigured = []
        for key, svc in self._services.items():
            if svc.url:
                configured[key] = svc.to_dict()
            else:
                unconfigured.append(key)
        return {
            "services": configured,
            "unconfigured": unconfigured,
        }

    async def check_service(self, name: str, client: httpx.AsyncClient | None = None, timeout: float = 5.0) -> bool:
        """Run a health check on a single service."""
        svc = self._services.get(name)
        if not svc or not svc.url:
            return False

        # Determine health endpoint
        url = svc.url.rstrip("/")
        if name == "ha":
            health_url = f"{url}/api/"
        elif name in ("model", "fallback_model", "vision"):
            health_url = url.replace("/v1", "") + "/health"
        elif name in ("tts", "stt"):
            health_url = f"{url}/health"
        else:
            health_url = f"{url}/"

        try:
            _client = client or httpx.AsyncClient(timeout=timeout)
            try:
                r = await _client.get(health_url)
                svc.healthy = r.status_code < 500
                svc.last_error = "" if svc.healthy else f"HTTP {r.status_code}"
            finally:
                if not client:
                    await _client.aclose()
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            svc.healthy = False
            svc.last_error = str(e)[:100]
        except Exception as e:
            svc.healthy = False
            svc.last_error = str(e)[:100]

        svc.last_check = time.time()
        SERVICE_HEALTHY.labels(service=name).set(1 if svc.healthy else 0)
        return svc.healthy

    async def check_all(self) -> Dict[str, bool]:
        """Check all configured services concurrently with a shared HTTP client."""
        results = {}
        names = []
        tasks = []

        async with httpx.AsyncClient(timeout=5.0) as client:
            for name, svc in self._services.items():
                if svc.url:
                    tasks.append(self.check_service(name, client=client))
                    names.append(name)

            if tasks:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for name, outcome in zip(names, outcomes, strict=False):
                    if isinstance(outcome, Exception):
                        results[name] = False
                        SERVICE_HEALTHY.labels(service=name).set(0)
                    else:
                        results[name] = outcome

        # Log summary
        healthy = [n for n, ok in results.items() if ok]
        unhealthy = [n for n, ok in results.items() if not ok]
        unconfigured = [n for n, s in self._services.items() if not s.url]

        if healthy:
            logger.info("[SERVICES] Healthy: %s", ", ".join(healthy))
        if unhealthy:
            logger.warning("[SERVICES] Unhealthy: %s", ", ".join(unhealthy))
        if unconfigured:
            logger.info("[SERVICES] Not configured (disabled): %s", ", ".join(unconfigured))

        return results


# Module-level singleton
services = ServiceRegistry()


async def check_all_services() -> Dict[str, bool]:
    """Convenience function to check all services."""
    return await services.check_all()
