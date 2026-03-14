"""
User Profile — Loads user-specific configuration from YAML.

Replaces all hardcoded personal data (names, addresses, speakers, sensors, etc.)
with a configurable profile. Nadim's setup keeps working identically via
user_profile.nadim.yaml. New users create their own user_profile.yaml.

Priority: env vars > profile YAML > built-in defaults
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default values (used when no profile YAML exists)
# ---------------------------------------------------------------------------
_DEFAULTS = {
    "user": {
        "name": "User",
        "home_address": "",
        "timezone": "America/Chicago",
    },
    "assistant": {
        "name": "Jess",
        "voice": "jessica",
        "personality": "warm, friendly, supportive ADHD coach",
    },
    "speakers": {
        "default": "media_player.office_speaker",
        "morning_briefing": "media_player.bedroom_pair",
        "focus_audio": "media_player.dining_room_max",
        "aliases": {},
    },
    "notifications": {
        "mobile_service": "",
        "title": "Reminder",
    },
    "sensors": {
        "temperature": {
            "closet": "sensor.closet_temperature",
            "ambient": "sensor.kitchen_temperature",
            "warning": 80,
            "critical": 85,
        },
    },
    "coaching": {
        "default_mode": "explainer",
        "grounding_preference": "only_when_high",
    },
    "finance": {
        "monthly_discretionary": 1000,
        "retirement_target_age": 62,
        "current_age": 30,
    },
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class UserProfile:
    """All user-specific configuration in one place."""

    # User identity
    user_name: str = "User"
    home_address: str = ""
    timezone: str = "America/Chicago"

    # Assistant identity
    assistant_name: str = "Jess"
    assistant_voice: str = "jessica"
    assistant_personality: str = "warm, friendly, supportive ADHD coach"

    # Speaker entity IDs
    default_speaker: str = "media_player.office_speaker"
    morning_briefing_speaker: str = "media_player.bedroom_pair"
    focus_audio_player: str = "media_player.dining_room_max"
    speaker_aliases: Dict[str, str] = field(default_factory=dict)

    # Notifications
    mobile_notify_service: str = ""
    notification_title: str = "Reminder"

    # Temperature sensors
    closet_temp_sensor: str = "sensor.closet_temperature"
    ambient_temp_sensor: str = "sensor.kitchen_temperature"
    temp_warning: float = 80.0
    temp_critical: float = 85.0

    # Coaching
    default_mode: str = "explainer"
    grounding_preference: str = "only_when_high"

    # Finance defaults
    monthly_discretionary: float = 1000.0
    retirement_target_age: int = 62
    current_age: int = 30

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserProfile":
        """Build a UserProfile from a parsed YAML dict."""
        user = data.get("user", {})
        assistant = data.get("assistant", {})
        speakers = data.get("speakers", {})
        notifications = data.get("notifications", {})
        sensors = data.get("sensors", {})
        temp = sensors.get("temperature", {})
        coaching = data.get("coaching", {})
        finance = data.get("finance", {})

        return cls(
            user_name=user.get("name", _DEFAULTS["user"]["name"]),
            home_address=user.get("home_address", _DEFAULTS["user"]["home_address"]),
            timezone=user.get("timezone", _DEFAULTS["user"]["timezone"]),
            assistant_name=assistant.get("name", _DEFAULTS["assistant"]["name"]),
            assistant_voice=assistant.get("voice", _DEFAULTS["assistant"]["voice"]),
            assistant_personality=assistant.get("personality", _DEFAULTS["assistant"]["personality"]),
            default_speaker=speakers.get("default", _DEFAULTS["speakers"]["default"]),
            morning_briefing_speaker=speakers.get("morning_briefing", _DEFAULTS["speakers"]["morning_briefing"]),
            focus_audio_player=speakers.get("focus_audio", _DEFAULTS["speakers"]["focus_audio"]),
            speaker_aliases=speakers.get("aliases", {}),
            mobile_notify_service=notifications.get("mobile_service", _DEFAULTS["notifications"]["mobile_service"]),
            notification_title=notifications.get("title", _DEFAULTS["notifications"]["title"]),
            closet_temp_sensor=temp.get("closet", _DEFAULTS["sensors"]["temperature"]["closet"]),
            ambient_temp_sensor=temp.get("ambient", _DEFAULTS["sensors"]["temperature"]["ambient"]),
            temp_warning=float(temp.get("warning", _DEFAULTS["sensors"]["temperature"]["warning"])),
            temp_critical=float(temp.get("critical", _DEFAULTS["sensors"]["temperature"]["critical"])),
            default_mode=coaching.get("default_mode", _DEFAULTS["coaching"]["default_mode"]),
            grounding_preference=coaching.get("grounding_preference", _DEFAULTS["coaching"]["grounding_preference"]),
            monthly_discretionary=float(
                finance.get("monthly_discretionary", _DEFAULTS["finance"]["monthly_discretionary"])
            ),
            retirement_target_age=int(
                finance.get("retirement_target_age", _DEFAULTS["finance"]["retirement_target_age"])
            ),
            current_age=int(finance.get("current_age", _DEFAULTS["finance"]["current_age"])),
        )


# ---------------------------------------------------------------------------
# Loader — finds and loads the profile YAML
# ---------------------------------------------------------------------------


def _find_profile_path() -> Optional[Path]:
    """Search for user_profile.yaml in standard locations."""
    # Env var override
    env_path = os.environ.get("USER_PROFILE_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p

    # Check /app/ (Docker container) then working directory
    candidates = [
        Path("/app/user_profile.yaml"),
        Path("/app/user_profile.yml"),
        Path("user_profile.yaml"),
        Path("user_profile.yml"),
    ]
    for c in candidates:
        if c.exists():
            return c

    return None


def load_profile() -> UserProfile:
    """Load user profile from YAML file, falling back to defaults.

    Priority: env vars > profile YAML > built-in defaults.
    """
    profile_path = _find_profile_path()

    if profile_path:
        try:
            import yaml

            with open(profile_path) as f:
                data = yaml.safe_load(f) or {}
            logger.info(f"[PROFILE] Loaded from {profile_path}")
            profile = UserProfile.from_dict(data)
        except ImportError:
            logger.warning("[PROFILE] PyYAML not installed, using defaults")
            profile = UserProfile()
        except Exception as e:
            logger.error(f"[PROFILE] Failed to load {profile_path}: {e}, using defaults")
            profile = UserProfile()
    else:
        logger.info("[PROFILE] No user_profile.yaml found, using defaults")
        profile = UserProfile()

    # Env var overrides (highest priority)
    _apply_env_overrides(profile)

    logger.info(f"[PROFILE] User: {profile.user_name}, Assistant: {profile.assistant_name}")
    return profile


def _apply_env_overrides(profile: UserProfile) -> None:
    """Override profile values with env vars where set."""
    mapping = {
        "USER_NAME": "user_name",
        "ASSISTANT_NAME": "assistant_name",
        "TTS_VOICE": "assistant_voice",
        "HOME_ADDRESS": "home_address",
        "TZ": "timezone",
        "REMINDER_SPEAKER": "default_speaker",
        "MORNING_BRIEFING_SPEAKER": "morning_briefing_speaker",
        "FOCUS_AUDIO_PLAYER": "focus_audio_player",
        "MOBILE_NOTIFY": "mobile_notify_service",
        "NOTIFICATION_TITLE": "notification_title",
        "CLOSET_TEMP_SENSOR": "closet_temp_sensor",
        "AMBIENT_TEMP_SENSOR": "ambient_temp_sensor",
        "CLOSET_TEMP_WARNING": "temp_warning",
        "CLOSET_TEMP_CRITICAL": "temp_critical",
    }
    for env_key, attr in mapping.items():
        val = os.environ.get(env_key)
        if val is not None:
            current = getattr(profile, attr)
            if isinstance(current, float):
                setattr(profile, attr, float(val))
            elif isinstance(current, int):
                setattr(profile, attr, int(val))
            else:
                setattr(profile, attr, val)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_profile: Optional[UserProfile] = None


def get_profile() -> UserProfile:
    """Get or create the singleton UserProfile."""
    global _profile
    if _profile is None:
        _profile = load_profile()
    return _profile


def reload_profile() -> UserProfile:
    """Force reload the profile from disk."""
    global _profile
    _profile = load_profile()
    return _profile
