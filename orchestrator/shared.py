"""
Shared state for the Brain Gateway orchestrator modules.

All cross-module state lives here so modules can import what they need
without circular dependencies.  Configuration comes from config.py (Pydantic
Settings); this module re-exports constants for backward compatibility.
"""

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import chromadb
import httpx
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from chromadb.config import Settings as ChromaSettings
from sentence_transformers import SentenceTransformer

from orchestrator.config import settings
from orchestrator.focus_state import FocusSession
from orchestrator.ha_integration import HomeAssistantClient
from orchestrator.llm_backend import LLMBackend, LLMConfig, create_backend
from orchestrator.tts_backend import TTSBackend, TTSConfig, create_tts_backend
from orchestrator.user_profile import get_profile

# ---------------------------------------------------------------------------
# User profile (loaded once at import time)
# ---------------------------------------------------------------------------
profile = get_profile()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backward-compatible constant aliases (prefer `settings.x` in new code)
# ---------------------------------------------------------------------------
MODEL_BACKEND = settings.model_backend
MODEL_URL = settings.model_url
MODEL_NAME = settings.model_name
MODEL_API_KEY = settings.model_api_key
FALLBACK_MODEL_BACKEND = settings.fallback_model_backend
FALLBACK_MODEL_URL = settings.fallback_model_url
FALLBACK_MODEL_NAME = settings.fallback_model_name
FALLBACK_MODEL_API_KEY = settings.fallback_model_api_key
EMBEDDING_MODEL_NAME = settings.embedding_model

# ---------------------------------------------------------------------------
# LLM Backend instances (initialized in startup_event after _http is ready)
# ---------------------------------------------------------------------------
primary_backend: Optional[LLMBackend] = None
fallback_backend: Optional[LLMBackend] = None

# ---------------------------------------------------------------------------
# TTS Backend instance (initialized in startup_event after _http is ready)
# ---------------------------------------------------------------------------
tts_backend: Optional[TTSBackend] = None


def init_backends(http_client: httpx.AsyncClient):
    """
    Initialize LLM and TTS backend instances.

    Priority: user_profile.yaml llm/tts section > env vars > defaults.
    Called from startup_event after _http is initialized.
    """
    global primary_backend, fallback_backend, tts_backend

    # Load profile YAML for llm/tts config sections
    yaml_data = {}
    try:
        import yaml

        from orchestrator.user_profile import _find_profile_path

        path = _find_profile_path()
        if path:
            with open(path) as f:
                yaml_data = yaml.safe_load(f) or {}
    except Exception:
        pass

    llm_cfg = yaml_data.get("llm", {})

    # --- v7 unified mode: single primary + optional fallback ---
    model_cfg = llm_cfg.get("model", llm_cfg.get("conversation", {}))
    fb_cfg = llm_cfg.get("fallback", llm_cfg.get("orchestrator", {}))

    primary_config = LLMConfig(
        backend=model_cfg.get("backend", MODEL_BACKEND),
        url=model_cfg.get("url", MODEL_URL),
        model=model_cfg.get("model", MODEL_NAME),
        # YAML api_key (with ${ENV} indirection) wins; else fall back to the
        # MODEL_API_KEY env var (raw value). Empty for local backends.
        api_key=_resolve_api_key(model_cfg.get("api_key", "")) or MODEL_API_KEY,
    )
    primary_backend = create_backend(primary_config, http_client)

    logger.info("[LLM] Primary: %s -> %s (%s)", primary_config.backend, primary_config.url, primary_config.model)

    # Optional fallback
    fb_url = fb_cfg.get("url", FALLBACK_MODEL_URL)
    if fb_url:
        fb_config = LLMConfig(
            backend=fb_cfg.get("backend", FALLBACK_MODEL_BACKEND),
            url=fb_url,
            model=fb_cfg.get("model", FALLBACK_MODEL_NAME),
            api_key=_resolve_api_key(fb_cfg.get("api_key", "")) or FALLBACK_MODEL_API_KEY,
        )
        fallback_backend = create_backend(fb_config, http_client)
        logger.info("[LLM] Fallback: %s -> %s (%s)", fb_config.backend, fb_config.url, fb_config.model)
    else:
        logger.info("[LLM] No fallback model configured")

    # --- TTS backend ---
    TTS_URL = settings.tts_url
    TTS_VOICE = settings.tts_voice or profile.assistant_voice

    tts_cfg = yaml_data.get("tts", {})
    tts_config = TTSConfig(
        backend=tts_cfg.get("backend", "local_http"),
        url=tts_cfg.get("url", TTS_URL),
        voice=tts_cfg.get("voice", TTS_VOICE),
        api_key=_resolve_api_key(tts_cfg.get("api_key", "")),
        model=tts_cfg.get("model", ""),
    )

    tts_backend = create_tts_backend(tts_config, http_client)

    logger.info(f"[TTS] Backend: {tts_config.backend} -> {tts_config.url} ({tts_config.voice})")


def _resolve_api_key(value: str) -> str:
    """Resolve API key — supports ${ENV_VAR} syntax for env var references."""
    if not value:
        return ""
    if value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        return os.environ.get(env_name, "")
    return value


# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------
HA_URL = settings.ha_url
HA_TOKEN = settings.ha_token
ha_client = HomeAssistantClient(url=HA_URL, token=HA_TOKEN)

# Helios wake-on-demand (PT-C) — gates the helios_power tool + auto-wake hook.
HELIOS_WAKE_ENABLED = settings.helios_wake_enabled

# ---------------------------------------------------------------------------
# RAG / ChromaDB
# ---------------------------------------------------------------------------
# Single unified collection for all memory: RAG document chunks, auto-learn
# facts, user corrections, document vault entries. The old `nadim_rag`
# collection was deleted on 2026-04-13 after confirming mempalace had
# fully absorbed everything (see git log: "Clean up legacy nadim_rag").
CHROMA_PERSIST = settings.chroma_persist
CHROMA_COLLECTION = settings.palace_collection
MIN_COS = settings.min_cos
TOP_K = settings.top_k

chroma = chromadb.PersistentClient(
    path=os.path.expanduser(CHROMA_PERSIST),
    settings=ChromaSettings(anonymized_telemetry=False),
)
collection = chroma.get_or_create_collection(CHROMA_COLLECTION)
embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME, trust_remote_code=True)

# ---------------------------------------------------------------------------
# Agentic settings
# ---------------------------------------------------------------------------
MAX_TOOL_ROUNDS = settings.max_tool_rounds

# ---------------------------------------------------------------------------
# Shared httpx client (initialized in startup_event)
# ---------------------------------------------------------------------------
_http: Optional[httpx.AsyncClient] = None

# ---------------------------------------------------------------------------
# HA tool definition cache
# ---------------------------------------------------------------------------
_ha_tool_cache: Optional[Dict[str, Any]] = None
_ha_tool_cache_time: float = 0.0
_HA_TOOL_CACHE_TTL: float = 300.0  # 5 minutes

# ---------------------------------------------------------------------------
# Focus timer state (Pomodoro) — FocusSession supports dict-style access
# ---------------------------------------------------------------------------
current_focus_session = FocusSession()

# Endel focus audio configuration
ENDEL_API_URL = "https://app.endel.io/api/pacific"
ENDEL_MODES = ["focus", "deeper-focus", "study", "colored-noises"]
FOCUS_AUDIO_PLAYER = settings.focus_audio_player or profile.focus_audio_player
ENDEL_ENABLED = settings.endel_enabled
FOCUS_AUDIO_LOFI_URL = settings.focus_audio_lofi_url
FOCUS_AUDIO_COFFEE_URL = settings.focus_audio_coffee_url

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
TIMEZONE = settings.tz
scheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=TIMEZONE,
)

# ---------------------------------------------------------------------------
# Calendar polling config
# ---------------------------------------------------------------------------
CALENDAR_POLL_INTERVAL = settings.calendar_poll_interval
CALENDAR_TIERED_ALERTS = settings.calendar_tiered_alerts
CALENDAR_ALERT_TIERS = settings.alert_tiers
MORNING_BRIEFING_TIME = settings.morning_briefing_time
MORNING_BRIEFING_ENABLED = settings.morning_briefing_enabled
MORNING_BRIEFING_SPEAKER = settings.morning_briefing_speaker or profile.morning_briefing_speaker
MORNING_BRIEFING_MIN_VOLUME = settings.morning_briefing_min_volume

# ---------------------------------------------------------------------------
# Temperature monitoring
# ---------------------------------------------------------------------------
CLOSET_TEMP_WARNING = settings.closet_temp_warning
CLOSET_TEMP_CRITICAL = settings.closet_temp_critical

# ---------------------------------------------------------------------------
# Email-to-calendar config
# ---------------------------------------------------------------------------
EMAIL_TO_CALENDAR_ENABLED = settings.email_to_calendar_enabled
EMAIL_TO_CALENDAR_INTERVAL = settings.email_to_calendar_interval

# ---------------------------------------------------------------------------
# Phone calendar sync (iPhone Shortcut pushes consolidated calendar)
# ---------------------------------------------------------------------------
PHONE_CALENDAR_SYNC_ENABLED = settings.phone_calendar_sync_enabled

# Cached events from last phone sync (list of dicts)
# Persisted to disk so they survive orchestrator restarts
_phone_calendar_events: list = []
_phone_calendar_sync_time: float = 0.0

PHONE_CALENDAR_FILE = os.path.join(
    os.path.dirname(settings.finance_db_path),
    "phone_calendar.json",
)

# ---------------------------------------------------------------------------
# Voice-mode beacon
# ---------------------------------------------------------------------------
# _last_voice_at — consume-on-read beacon. Set by STT proxy, consumed by the
# next chat request within VOICE_FLAG_WINDOW_SEC to tag it as a voice turn.
# Exactly-once semantics: one STT → one voice chat.
#
# _last_voice_activity_at — sticky timestamp of the most recent voice activity
# (STT call OR is_voice=True chat turn). NEVER consumed — just updated. Used
# by announcement gating so reminders don't interrupt mid-conversation.
_last_voice_at: float = 0.0
_last_voice_activity_at: float = 0.0
VOICE_FLAG_WINDOW_SEC: float = 30.0
VOICE_SESSION_WINDOW_SEC: float = 60.0


def consume_voice_flag() -> bool:
    """Return True (and clear the flag) if a recent STT call makes the next
    chat request a voice turn. Fresh STT has to arrive within the window."""
    global _last_voice_at
    if _last_voice_at <= 0:
        return False
    if time.time() - _last_voice_at > VOICE_FLAG_WINDOW_SEC:
        _last_voice_at = 0.0
        return False
    _last_voice_at = 0.0
    return True


def mark_voice_activity() -> None:
    """Record that voice activity just happened. Called from STT proxy and
    from the chat endpoint whenever is_voice=True. Used by is_voice_session_active."""
    global _last_voice_activity_at
    _last_voice_activity_at = time.time()


def is_voice_session_active(window_sec: float = VOICE_SESSION_WINDOW_SEC) -> bool:
    """True if voice activity happened within the last window_sec seconds.
    Use this to gate reminder announcements so they don't stomp on an active
    conversation with Jess."""
    if _last_voice_activity_at <= 0:
        return False
    return (time.time() - _last_voice_activity_at) <= window_sec


def _load_phone_calendar():
    """Load phone calendar events from disk (called at startup)."""
    global _phone_calendar_events, _phone_calendar_sync_time
    import json

    try:
        if os.path.exists(PHONE_CALENDAR_FILE):
            with open(PHONE_CALENDAR_FILE) as f:
                data = json.load(f)
            _phone_calendar_events = data.get("events", [])
            _phone_calendar_sync_time = data.get("sync_time", 0.0)
            logging.getLogger(__name__).info(
                f"[PHONE_CAL] Loaded {len(_phone_calendar_events)} events from disk "
                f"(synced {int((time.time() - _phone_calendar_sync_time) / 60)}m ago)"
            )
    except Exception as e:
        logging.getLogger(__name__).warning(f"[PHONE_CAL] Failed to load from disk: {e}")


def _save_phone_calendar():
    """Save phone calendar events to disk (called after each sync)."""
    import json

    try:
        os.makedirs(os.path.dirname(PHONE_CALENDAR_FILE), exist_ok=True)
        with open(PHONE_CALENDAR_FILE, "w") as f:
            json.dump({"events": _phone_calendar_events, "sync_time": _phone_calendar_sync_time}, f)
    except Exception as e:
        logging.getLogger(__name__).warning(f"[PHONE_CAL] Failed to save to disk: {e}")


# ---------------------------------------------------------------------------
# Travel time config (Google Maps Directions API)
# ---------------------------------------------------------------------------
GOOGLE_MAPS_API_KEY = settings.google_maps_api_key
HOME_ADDRESS = settings.home_address or profile.home_address
TRAVEL_TIME_BUFFER = settings.travel_time_buffer

# ---------------------------------------------------------------------------
# Auto-learn configuration
# ---------------------------------------------------------------------------
AUTO_LEARN_ENABLED = settings.auto_learn_enabled
AUTO_LEARN_DELAY_MINUTES = settings.auto_learn_delay_minutes
AUTO_LEARN_MAX_FACTS = settings.auto_learn_max_facts
AUTO_LEARN_DEDUP_THRESHOLD = settings.auto_learn_dedup_threshold
AUTO_LEARN_MARKDOWN = settings.auto_learn_markdown
AUTO_LEARN_ENCRYPT = settings.auto_learn_encrypt
AUTO_LEARN_ENCRYPTION_KEY = settings.auto_learn_encryption_key

# In-memory conversation buffer for auto-learn (never written to disk)
_auto_learn_conversations: Dict[str, list] = {}

# ---------------------------------------------------------------------------
# Routine scaffolding configuration (F-006)
# ---------------------------------------------------------------------------
ROUTINES_YAML_PATH = settings.routines_yaml_path
ROUTINES_OVERRIDES_PATH = settings.routines_overrides_path
ROUTINE_ENABLED = settings.routine_enabled
ROUTINE_NUDGE_MAX = settings.routine_nudge_max
ROUTINE_AUTO_SKIP = settings.routine_auto_skip

# ---------------------------------------------------------------------------
# Presence awareness
# ---------------------------------------------------------------------------
PRESENCE_ENABLED = settings.presence_enabled
PRESENCE_ENTITY = settings.presence_entity
PRESENCE_MOTION_SENSORS = settings.presence_motion_sensors
PRESENCE_POLL_INTERVAL = settings.presence_poll_interval
PRESENCE_TARGETED_TTS = settings.presence_targeted_tts
PRESENCE_WELCOME_HOME = settings.presence_welcome_home
PRESENCE_WELCOME_COOLDOWN = settings.presence_welcome_cooldown

# ---------------------------------------------------------------------------
# Distribution profile
# ---------------------------------------------------------------------------
JESS_ADVANCED = settings.jess_advanced
WORKOUTS_ENABLED = settings.workouts_enabled
MEALS_ENABLED = settings.meals_enabled

# ---------------------------------------------------------------------------
# Code Agent (coding-focused model for self-troubleshooting)
# ---------------------------------------------------------------------------
CODE_AGENT_ENABLED = settings.code_agent_enabled
CODE_AGENT_MODEL_URL = settings.code_agent_model_url
CODE_AGENT_MODEL_NAME = settings.code_agent_model_name
CODE_AGENT_CODEBASE_PATH = settings.code_agent_codebase_path
CODE_AGENT_MAX_ROUNDS = settings.code_agent_max_rounds

# ---------------------------------------------------------------------------
# Expert Model (Qwen3-32B Thinking on Saturn 3090)
# ---------------------------------------------------------------------------
EXPERT_ENABLED = settings.expert_enabled
EXPERT_MODEL_URL = settings.expert_model_url
EXPERT_MODEL_NAME = settings.expert_model_name
EXPERT_TIMEOUT_SECONDS = settings.expert_timeout_seconds
EXPERT_MAX_TOKENS = settings.expert_max_tokens
EXPERT_CIRCUIT_BREAKER_FAILURES = settings.expert_circuit_breaker_failures
EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS = settings.expert_circuit_breaker_cooldown_seconds

# ---------------------------------------------------------------------------
# Do Not Disturb (sleep mode) — suppresses all announcements
# ---------------------------------------------------------------------------
DND_ACTIVE = False  # runtime state, set via goodnight tool

AMBIENT_ENABLED = settings.ambient_enabled
AMBIENT_SUMMARY_TIMES = settings.ambient_summary_times
AMBIENT_LED_ENTITY = settings.ambient_led_entity
AMBIENT_SPEAKER = settings.ambient_speaker

SELFCARE_ENABLED = settings.selfcare_enabled
MEAL_NUDGE_HOURS = settings.meal_nudge_hours
HYDRATION_INTERVAL = settings.hydration_interval
MOVEMENT_INTERVAL = settings.movement_interval
QUIET_HOURS_START = settings.quiet_hours_start
QUIET_HOURS_END = settings.quiet_hours_end

INTERRUPT_CHECKIN_DELAY = settings.interrupt_checkin_delay
CONTEXT_STACK_SIZE = settings.context_stack_size

PROGRESS_ENABLED = settings.progress_enabled
DAILY_SUMMARY_TIME = settings.daily_summary_time
WEEKLY_SUMMARY_DAY = settings.weekly_summary_day
WEEKLY_SUMMARY_TIME = settings.weekly_summary_time

# -- Self-audit (F-014) ----------------------------------------------------
SELF_AUDIT_ENABLED = settings.self_audit_enabled
SELF_AUDIT_HOUR_UTC = settings.self_audit_hour_utc

# ---------------------------------------------------------------------------
# Vision / image recognition
# ---------------------------------------------------------------------------
VISION_ENABLED = settings.vision_enabled
VISION_MODEL_URL = settings.vision_model_url
VISION_MODEL_NAME = settings.vision_model_name
VISION_MAX_IMAGE_SIZE = settings.vision_max_image_size
VISION_TIMEOUT = settings.vision_timeout

# Per-session image cache for follow-up analysis (keyed by session hash)
_vision_image_cache: Dict[str, str] = {}

# ---------------------------------------------------------------------------
# MemPalace (unified memory system — replaces separate personal_rag)
# ---------------------------------------------------------------------------
PALACE_ENABLED = settings.palace_enabled
PALACE_YAML_PATH = settings.palace_yaml_path
PALACE_WAKEUP_ENABLED = settings.palace_wakeup_enabled
PALACE_WAKEUP_MAX_TOKENS = settings.palace_wakeup_max_tokens
PALACE_DEDUP_THRESHOLD = settings.palace_dedup_threshold


def get_palace_collection():
    """Get the unified palace collection (same as shared.collection)."""
    return collection


# Palace singleton (lazy — avoids import-time side effects).
# Protected by a lock because `get_palace()` is called from `asyncio.to_thread`
# workers (via mempalace.store / is_duplicate), which means two threads can
# race the None-check and construct two MemPalace instances simultaneously.
_palace_instance = None
_palace_lock = threading.Lock()


def get_palace():
    """Get or create the MemPalace singleton (thread-safe double-checked init)."""
    global _palace_instance
    if _palace_instance is None:
        with _palace_lock:
            if _palace_instance is None:  # re-check inside the lock
                from orchestrator.mempalace import MemPalace

                _palace_instance = MemPalace()
    return _palace_instance
