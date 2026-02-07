"""
SearXNG Web Search Client for Brain Gateway

Provides web search capabilities via a self-hosted SearXNG instance.
Gracefully handles SearXNG being unavailable.
"""

import os
import logging
from typing import Optional, List
from dataclasses import dataclass, field
import httpx

logger = logging.getLogger(__name__)


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
    ) -> SearchResponse:
        """
        Search the web via SearXNG.

        Args:
            query: Search query string
            category: Search category ("general" or "news")
            time_range: Optional time filter ("day", "week", "month", "year")

        Returns:
            SearchResponse with results or error
        """
        if not query:
            return SearchResponse(success=False, error="No query provided", query=query)

        params = {
            "q": query,
            "format": "json",
            "categories": category,
        }
        if time_range:
            params["time_range"] = time_range

        logger.info(f"[WEB_SEARCH] Querying SearXNG: '{query}' (category={category}, time_range={time_range})")

        try:
            if self._http:
                resp = await self._http.get(f"{self.url}/search", params=params, timeout=15)
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.get(f"{self.url}/search", params=params)
            resp.raise_for_status()
            data = resp.json()

            raw_results = data.get("results", [])
            results = []
            for r in raw_results[:self.max_results]:
                results.append(SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    content=r.get("content", ""),
                    engine=r.get("engine", ""),
                    score=r.get("score", 0.0),
                ))

            logger.info(f"[WEB_SEARCH] Got {len(results)} results (from {len(raw_results)} total)")
            return SearchResponse(success=True, results=results, query=query)

        except httpx.HTTPStatusError as e:
            logger.error(f"[WEB_SEARCH] SearXNG HTTP error: {e.response.status_code}")
            return SearchResponse(
                success=False,
                error=f"Search service error: {e.response.status_code}",
                query=query,
            )
        except Exception as e:
            logger.error(f"[WEB_SEARCH] SearXNG error: {e}")
            return SearchResponse(
                success=False,
                error=f"Search service unavailable: {str(e)}",
                query=query,
            )

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
