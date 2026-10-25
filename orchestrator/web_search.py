"""
SearXNG Web Search Client for Brain Gateway

Provides web search capabilities via a self-hosted SearXNG instance.
Gracefully handles SearXNG being unavailable.

Every response carries an ``outcome`` (ok | no_results | empty | error |
timeout) for the ``bgw_web_search_total`` metric. Logs never carry the query: an httpx
exception's text can hold the request URL, which includes ``?q=``.
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Whole-request deadline (connect + read), not httpx's per-phase timeout.
DEFAULT_TIMEOUT_SECONDS = 15.0
# Voice turns: someone is waiting in silence. SearXNG gives each engine 3 s
# (its default outgoing.request_timeout), so a healthy search answers well
# inside this.
VOICE_TIMEOUT_SECONDS = 5.0

# Engine names come from the SearXNG config; anything else is folded into
# "other" so the metric label stays bounded — by shape, and by count: after
# _MAX_ENGINE_LABELS distinct names, new ones are "other" too.
_ENGINE_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9 ._-]{0,31}")
_MAX_UNRESPONSIVE = 20
_MAX_ENGINE_LABELS = 40
_seen_engine_labels: set[str] = set()
# SearXNG reports "<error type>" or "Suspended: <error type>" per engine.
_ENGINE_REASONS = (
    ("timeout", "timeout"),
    ("captcha", "captcha"),
    ("access denied", "access_denied"),
    ("too many requests", "too_many_requests"),
)


def _engine_reason(error: str) -> str:
    """Bounded label for a SearXNG engine error message."""
    low = error.lower()
    if "suspended" in low:
        return "suspended"
    for needle, label in _ENGINE_REASONS:
        if needle in low:
            return label
    return "other"


def _parse_unresponsive(raw: object) -> List[Tuple[str, str]]:
    """(engine, reason) pairs from SearXNG's ``unresponsive_engines`` field,
    normalized to bounded label values. Malformed entries are skipped."""
    if not isinstance(raw, list):
        return []
    out: set[Tuple[str, str]] = set()
    for entry in raw[:_MAX_UNRESPONSIVE]:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        engine, error = entry[0], entry[1]
        if not isinstance(engine, str) or not isinstance(error, str):
            continue
        engine = engine.strip().lower()
        if not _ENGINE_LABEL_RE.fullmatch(engine):
            engine = "other"
        elif engine not in _seen_engine_labels:
            if len(_seen_engine_labels) >= _MAX_ENGINE_LABELS:
                engine = "other"
            else:
                _seen_engine_labels.add(engine)
        out.add((engine, _engine_reason(error)))
    return sorted(out)


@dataclass
class SearchResult:
    """A single search result."""

    title: str
    url: str
    content: str
    engine: str = ""
    score: float = 0.0


@dataclass
class SearchResponse:
    """Response from a web search."""

    success: bool
    results: List[SearchResult] = field(default_factory=list)
    error: Optional[str] = None
    query: str = ""
    # The bgw_web_search_total{result} label; derived when not given (only
    # "timeout" has to be set explicitly). No results splits two ways:
    # "empty" — SearXNG reported unresponsive engines (suspended, timing out,
    # CAPTCHA'd), so the search itself is broken; "no_results" — every engine
    # answered and found nothing, which an obscure query can do.
    outcome: Optional[Literal["ok", "no_results", "empty", "error", "timeout"]] = None
    # (engine, reason) pairs SearXNG reported as unresponsive, label-safe
    unresponsive_engines: List[Tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        if self.outcome is None:
            if not self.success:
                self.outcome = "error"
            elif self.results:
                self.outcome = "ok"
            else:
                self.outcome = "empty" if self.unresponsive_engines else "no_results"


class SearXNGClient:
    """
    SearXNG meta search engine client.

    Handles:
    - Web search queries with category and time range filtering
    - Graceful failure when SearXNG is unavailable
    """

    def __init__(
        self,
        url: Optional[str] = None,
        max_results: Optional[int] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.url = (url or os.environ.get("SEARXNG_URL", "http://searxng:8080")).rstrip("/")
        self.max_results = max_results or int(os.environ.get("SEARXNG_MAX_RESULTS", "5"))
        self._http = http_client

    async def search(
        self,
        query: str,
        category: str = "general",
        time_range: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> SearchResponse:
        """
        Search the web via SearXNG.

        Args:
            query: Search query string
            category: Search category ("general" or "news")
            time_range: Optional time filter ("day", "week", "month", "year")
            timeout: Whole-request deadline in seconds (default 15)

        Returns:
            SearchResponse with results or error
        """
        timeout = timeout or DEFAULT_TIMEOUT_SECONDS
        if not query:
            return SearchResponse(success=False, error="No query provided", query=query)

        params = {
            "q": query,
            "format": "json",
            "categories": category,
        }
        if time_range:
            params["time_range"] = time_range

        logger.info(f"[WEB_SEARCH] Querying SearXNG ({len(query)} chars, category={category}, time_range={time_range})")

        try:
            async with asyncio.timeout(timeout):
                resp = await self._get_search(params, timeout)
            resp.raise_for_status()
            data = resp.json()

            raw_results = data.get("results", [])
            results = []
            for r in raw_results[: self.max_results]:
                results.append(
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        content=r.get("content", ""),
                        engine=r.get("engine", ""),
                        score=r.get("score", 0.0),
                    )
                )

            logger.info(f"[WEB_SEARCH] Got {len(results)} results (from {len(raw_results)} total)")
            return SearchResponse(
                success=True,
                results=results,
                query=query,
                unresponsive_engines=_parse_unresponsive(data.get("unresponsive_engines")),
            )

        except (httpx.TimeoutException, TimeoutError) as e:
            logger.warning(f"[WEB_SEARCH] SearXNG timed out after {timeout:g}s ({type(e).__name__})")
            return SearchResponse(
                success=False,
                error=f"Search timed out after {timeout:g} seconds",
                query=query,
                outcome="timeout",
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"[WEB_SEARCH] SearXNG HTTP error: {e.response.status_code}")
            return SearchResponse(
                success=False,
                error=f"Search service error: {e.response.status_code}",
                query=query,
                outcome="error",
            )
        except Exception as e:
            # Exception name only: httpx messages can carry the URL (and ?q=).
            logger.error(f"[WEB_SEARCH] SearXNG error: {type(e).__name__}")
            return SearchResponse(
                success=False,
                error=f"Search service unavailable ({type(e).__name__})",
                query=query,
                outcome="error",
            )

    async def _get_search(self, params: dict, timeout: float) -> httpx.Response:
        if self._http:
            return await self._http.get(f"{self.url}/search", params=params, timeout=timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.get(f"{self.url}/search", params=params)

    async def is_available(self) -> bool:
        """Check if SearXNG is reachable."""
        try:
            if self._http:
                resp = await self._http.get(f"{self.url}/healthz", timeout=5)
            else:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{self.url}/healthz")
            return resp.status_code == 200
        except Exception:
            return False


# Global client instance
_search_client: Optional[SearXNGClient] = None


def get_search_client(http_client: Optional[httpx.AsyncClient] = None) -> SearXNGClient:
    """Get or create the global SearXNG client."""
    global _search_client
    if _search_client is None:
        _search_client = SearXNGClient(http_client=http_client)
    elif http_client and _search_client._http is None:
        _search_client._http = http_client
    return _search_client
