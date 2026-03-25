"""
Presence Awareness for Brain Gateway.

Polls Home Assistant for person state and motion/occupancy sensors to determine:
- Whether the user is home or away
- Which room they're most likely in (based on most recent motion)

Used by:
- reminder_manager: room-targeted TTS announcements
- selfcare_manager: skip nudges when away
- prompt_builder: inject location context into system prompt
- background_jobs: welcome home greeting on arrival
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import shared

logger = logging.getLogger(__name__)


@dataclass
class PresenceState:
    is_home: bool = True
    current_room: Optional[str] = None
    last_motion_room: Optional[str] = None
    last_motion_time: Optional[datetime] = None
    away_since: Optional[datetime] = None
    # For welcome home detection
    _was_home: bool = True
    _last_welcome: Optional[datetime] = None


_state = PresenceState()

# room → entity_id mapping, parsed from env var
_motion_sensors: Dict[str, str] = {}


def _init_sensors() -> None:
    """Parse PRESENCE_MOTION_SENSORS JSON into the room→entity map."""
    global _motion_sensors
    try:
        _motion_sensors = json.loads(shared.PRESENCE_MOTION_SENSORS)
    except (json.JSONDecodeError, TypeError):
        logger.warning("[PRESENCE] Failed to parse PRESENCE_MOTION_SENSORS, using empty map")
        _motion_sensors = {}


async def poll_presence() -> None:
    """Poll HA for person state and motion sensors. Called by scheduler every N seconds."""
    if not shared.PRESENCE_ENABLED:
        return

    if not _motion_sensors:
        _init_sensors()

    now = datetime.now()

    try:
        http = shared._http
        headers = {"Authorization": f"Bearer {shared.HA_TOKEN}"}

        # Check person entity (home/away)
        person_url = f"{shared.HA_URL}/api/states/{shared.PRESENCE_ENTITY}"
        resp = await http.get(person_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            person_data = resp.json()
            new_is_home = person_data.get("state", "").lower() in ("home", "on")

            # Detect away→home transition
            if new_is_home and not _state.is_home:
                _state.away_since = None
                logger.info("[PRESENCE] Transition: away → home")
            elif not new_is_home and _state.is_home:
                _state.away_since = now
                _state.current_room = None
                logger.info("[PRESENCE] Transition: home → away")

            _state._was_home = _state.is_home
            _state.is_home = new_is_home

        # Check motion sensors for room detection (only if home)
        if _state.is_home:
            for room, entity_id in _motion_sensors.items():
                try:
                    sensor_url = f"{shared.HA_URL}/api/states/{entity_id}"
                    resp = await http.get(sensor_url, headers=headers, timeout=5)
                    if resp.status_code == 200:
                        sensor_data = resp.json()
                        state = sensor_data.get("state", "").lower()
                        # binary_sensor: "on" = motion detected; device_tracker: "home" = present
                        if state in ("on", "home"):
                            _state.last_motion_room = room
                            _state.last_motion_time = now
                            _state.current_room = room
                except Exception:
                    pass

            # If no motion detected recently (>10 min), clear current room
            if _state.last_motion_time and (now - _state.last_motion_time).total_seconds() > 600:
                _state.current_room = None

    except Exception as e:
        logger.warning(f"[PRESENCE] Poll failed: {e}")


def get_presence() -> Dict[str, Any]:
    """Get current presence state. Thread-safe read of dataclass fields."""
    now = datetime.now()
    result: Dict[str, Any] = {
        "is_home": _state.is_home,
        "current_room": _state.current_room,
        "last_motion_room": _state.last_motion_room,
    }

    if _state.last_motion_time:
        result["last_seen_ago_minutes"] = int((now - _state.last_motion_time).total_seconds() / 60)
    else:
        result["last_seen_ago_minutes"] = None

    if _state.away_since:
        result["away_since"] = _state.away_since.isoformat()
        result["away_minutes"] = int((now - _state.away_since).total_seconds() / 60)
    else:
        result["away_since"] = None
        result["away_minutes"] = None

    return result


def check_welcome_home() -> bool:
    """Check if a welcome home greeting should fire. Returns True once per transition."""
    if not shared.PRESENCE_WELCOME_HOME:
        return False
    if not _state.is_home or _state._was_home:
        return False

    now = datetime.now()
    cooldown = shared.PRESENCE_WELCOME_COOLDOWN * 60  # minutes → seconds
    if _state._last_welcome and (now - _state._last_welcome).total_seconds() < cooldown:
        return False

    _state._last_welcome = now
    return True


def get_presence_prompt_context() -> str:
    """Get a one-line presence summary for the system prompt."""
    if not shared.PRESENCE_ENABLED:
        return ""

    p = get_presence()
    if p["is_home"]:
        if p["current_room"]:
            return f"Nadim is home, currently in the {p['current_room']}."
        elif p["last_motion_room"] and p["last_seen_ago_minutes"] is not None:
            return f"Nadim is home, last seen in the {p['last_motion_room']} {p['last_seen_ago_minutes']} minutes ago."
        return "Nadim is home."
    else:
        if p["away_minutes"] is not None:
            return f"Nadim is away from home ({p['away_minutes']} minutes)."
        return "Nadim is away from home."
