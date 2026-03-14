"""
Google Maps Directions API client for travel time estimates.
Used by calendar polling to announce "leave by" times with real-time traffic.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import shared

logger = logging.getLogger(__name__)

# Cache: (destination_lower, event_date_str) -> TravelInfo
_travel_cache: dict[tuple[str, str], "TravelInfo"] = {}

# Patterns that indicate a virtual meeting (skip Maps API)
_VIRTUAL_PATTERNS = re.compile(
    r"zoom\.us|teams\.microsoft|meet\.google|webex|https?://",
    re.IGNORECASE,
)


@dataclass
class TravelInfo:
    duration_minutes: int
    duration_in_traffic_minutes: int
    distance_text: str


def _is_virtual_location(location: str) -> bool:
    """Return True if location looks like a video call link."""
    return bool(_VIRTUAL_PATTERNS.search(location))


async def get_travel_time(
    origin: str,
    destination: str,
    departure_time: Optional[datetime] = None,
) -> Optional[TravelInfo]:
    """Get driving time from origin to destination with real-time traffic.

    Returns None if:
    - API key not configured
    - destination is empty or virtual
    - API call fails
    """
    if not shared.GOOGLE_MAPS_API_KEY:
        return None
    if not destination or not destination.strip():
        return None
    if _is_virtual_location(destination):
        return None

    # Check cache (keyed by destination + date to avoid redundant calls)
    date_str = departure_time.strftime("%Y-%m-%d") if departure_time else "today"
    cache_key = (destination.lower().strip(), date_str)
    if cache_key in _travel_cache:
        return _travel_cache[cache_key]

    # Build request
    params = {
        "origin": origin,
        "destination": destination,
        "departure_time": "now",
        "traffic_model": "best_guess",
        "key": shared.GOOGLE_MAPS_API_KEY,
    }

    try:
        http = shared._http
        if not http:
            logger.warning("[TRAVEL] No HTTP client available")
            return None

        resp = await http.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params=params,
            timeout=10,
        )
        data = resp.json()

        if data.get("status") != "OK":
            logger.warning(f"[TRAVEL] API returned {data.get('status')}: {data.get('error_message', '')}")
            return None

        leg = data["routes"][0]["legs"][0]
        duration_sec = leg["duration"]["value"]
        traffic_sec = leg.get("duration_in_traffic", leg["duration"])["value"]
        distance = leg["distance"]["text"]

        info = TravelInfo(
            duration_minutes=round(duration_sec / 60),
            duration_in_traffic_minutes=round(traffic_sec / 60),
            distance_text=distance,
        )

        # Cache it
        _travel_cache[cache_key] = info

        # Prevent unbounded cache growth
        if len(_travel_cache) > 100:
            keys = list(_travel_cache.keys())
            for k in keys[:50]:
                _travel_cache.pop(k, None)

        logger.info(f"[TRAVEL] {destination}: {info.duration_in_traffic_minutes} min (traffic), {info.distance_text}")
        return info

    except Exception as e:
        logger.error(f"[TRAVEL] Error fetching directions: {e}")
        return None
