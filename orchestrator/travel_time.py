"""
Google Maps Directions API client for travel time estimates.
Used by calendar polling to announce "leave by" times with real-time traffic.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from orchestrator import shared

logger = logging.getLogger(__name__)

# Cache: (destination_lower, event_date_str) -> TravelInfo
_travel_cache: dict[tuple[str, str], "TravelInfo"] = {}

# Patterns that indicate a virtual meeting: a link, or a conferencing product
# named in the location field (Exchange often puts "Microsoft Teams Meeting" or a
# bare Webex/Zoom URL there). Used to skip the Maps API and to keep locations
# out of spoken announcements.
_VIRTUAL_PATTERNS = re.compile(
    r"https?://|www\.|zoom\.us|\bzoom\b|teams\.microsoft|microsoft teams|\bteams meeting\b|^\s*teams\s*$"
    r"|meet\.google|google meet|webex|goto ?meeting|goto\.com|chime\.aws|\bamazon chime\b|bluejeans|whereby\.com",
    re.IGNORECASE,
)


@dataclass
class TravelInfo:
    duration_minutes: int
    duration_in_traffic_minutes: int
    distance_text: str


def is_virtual_location(location: str) -> bool:
    """Return True if location looks like a video call (link or product name)."""
    return bool(_VIRTUAL_PATTERNS.search(location))


# Kept for existing callers/tests.
_is_virtual_location = is_virtual_location

# Longer than this isn't something to read aloud, even if it is a real place.
_MAX_SPOKEN_LOCATION = 100


def spoken_location(location: str) -> str:
    """The location worth saying out loud, or "" — virtual-meeting links and
    names (Webex, Teams, Zoom…) and over-long strings are never spoken."""
    loc = " ".join((location or "").split())
    if not loc or is_virtual_location(loc) or len(loc) > _MAX_SPOKEN_LOCATION:
        return ""
    return loc


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
            # log-safe: Google Maps API status/error text
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

        logger.info(f"[TRAVEL] {info.duration_in_traffic_minutes} min (traffic)")
        return info

    except Exception as e:
        # Type only: an HTTP error's message would carry the request URL, which
        # holds the home address, the destination and the API key.
        logger.error(f"[TRAVEL] Error fetching directions: {type(e).__name__}")
        return None
