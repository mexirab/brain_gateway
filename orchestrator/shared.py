"""
Shared state for the Brain Gateway orchestrator modules.

All cross-module state lives here so modules can import what they need
without circular dependencies.
"""

import logging
import os
import time
from typing import Any, Dict, Optional

import chromadb
import httpx
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from chromadb.config import Settings
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

from ha_integration import HomeAssistantClient
from llm_backend import LLMBackend, LLMConfig, create_backend
from tts_backend import TTSBackend, TTSConfig, create_tts_backend
from user_profile import get_profile

# Load environment (fallback for local dev; Docker passes env vars directly)
load_dotenv("/app/.env", override=False)

# ---------------------------------------------------------------------------
# User profile (loaded once at import time)
# ---------------------------------------------------------------------------
profile = get_profile()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model endpoints (v7 unified)
# ---------------------------------------------------------------------------
# Primary model (conversation + tools)
MODEL_URL = os.environ.get("MODEL_URL", "http://10.0.0.195:8080/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen3.5-27B")

# Fallback model (used when primary is unavailable)
FALLBACK_MODEL_URL = os.environ.get("FALLBACK_MODEL_URL", "http://10.0.0.58:8001/v1")
FALLBACK_MODEL_NAME = os.environ.get("FALLBACK_MODEL_NAME", "nvidia/Nemotron-Orchestrator-8B")

# ---------------------------------------------------------------------------
# Embedding model (configurable)
# ---------------------------------------------------------------------------
EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "nomic-ai/nomic-embed-text-v2-moe")

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

        from user_profile import _find_profile_path

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
        backend=model_cfg.get("backend", "openai_compatible"),
        url=model_cfg.get("url", MODEL_URL),
        model=model_cfg.get("model", MODEL_NAME),
        api_key=_resolve_api_key(model_cfg.get("api_key", "")),
    )
    primary_backend = create_backend(primary_config, http_client)

    logger.info("[LLM] Primary: %s -> %s (%s)", primary_config.backend, primary_config.url, primary_config.model)

    # Optional fallback
    fb_url = fb_cfg.get("url", FALLBACK_MODEL_URL)
    if fb_url:
        fb_config = LLMConfig(
            backend=fb_cfg.get("backend", "openai_compatible"),
            url=fb_url,
            model=fb_cfg.get("model", FALLBACK_MODEL_NAME),
            api_key=_resolve_api_key(fb_cfg.get("api_key", "")),
        )
        fallback_backend = create_backend(fb_config, http_client)
        logger.info("[LLM] Fallback: %s -> %s (%s)", fb_config.backend, fb_config.url, fb_config.model)
    else:
        logger.info("[LLM] No fallback model configured")

    # --- TTS backend ---
    TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.173:8002")
    TTS_VOICE = os.environ.get("TTS_VOICE", profile.assistant_voice)

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
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
ha_client = HomeAssistantClient(url=HA_URL, token=HA_TOKEN)

# ---------------------------------------------------------------------------
# RAG / ChromaDB
# ---------------------------------------------------------------------------
CHROMA_PERSIST = os.environ.get("CHROMA_PERSIST", "/home/labadmin/.local/share/chroma/personal_rag")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "nadim_rag")
MIN_COS = float(os.environ.get("MIN_COS", "0.30"))
TOP_K = int(os.environ.get("TOP_K", "6"))

chroma = chromadb.PersistentClient(
    path=os.path.expanduser(CHROMA_PERSIST),
    settings=Settings(anonymized_telemetry=False),
)
collection = chroma.get_or_create_collection(CHROMA_COLLECTION)
embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME, trust_remote_code=True)

# ---------------------------------------------------------------------------
# Agentic settings
# ---------------------------------------------------------------------------
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "5"))

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
# Focus timer state (Pomodoro)
# ---------------------------------------------------------------------------
current_focus_session = {
    "active": False,
    "task": None,
    "started": None,
    "duration": None,
    "break_duration": None,
    "job_id": None,
    "audio_player": None,
    "block_sites": False,
}

# Endel focus audio configuration
ENDEL_API_URL = "https://app.endel.io/api/pacific"
ENDEL_MODES = ["focus", "deeper-focus", "study", "colored-noises"]
FOCUS_AUDIO_PLAYER = os.environ.get("FOCUS_AUDIO_PLAYER", profile.focus_audio_player)
ENDEL_ENABLED = os.environ.get("ENDEL_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
TIMEZONE = os.environ.get("TZ", "America/Chicago")
scheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=TIMEZONE,
)

# ---------------------------------------------------------------------------
# Calendar polling config
# ---------------------------------------------------------------------------
CALENDAR_POLL_INTERVAL = int(os.environ.get("CALENDAR_POLL_INTERVAL", "15"))
MORNING_BRIEFING_TIME = os.environ.get("MORNING_BRIEFING_TIME", "07:00")
MORNING_BRIEFING_ENABLED = os.environ.get("MORNING_BRIEFING_ENABLED", "true").lower() == "true"
MORNING_BRIEFING_SPEAKER = os.environ.get("MORNING_BRIEFING_SPEAKER", profile.morning_briefing_speaker)

# ---------------------------------------------------------------------------
# Email polling config
# ---------------------------------------------------------------------------
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", "30"))
EMAIL_POLL_ENABLED = os.environ.get("EMAIL_POLL_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Temperature monitoring
# ---------------------------------------------------------------------------
CLOSET_TEMP_WARNING = float(os.environ.get("CLOSET_TEMP_WARNING", str(profile.temp_warning)))
CLOSET_TEMP_CRITICAL = float(os.environ.get("CLOSET_TEMP_CRITICAL", str(profile.temp_critical)))

# ---------------------------------------------------------------------------
# Email-to-calendar config
# ---------------------------------------------------------------------------
EMAIL_TO_CALENDAR_ENABLED = os.environ.get("EMAIL_TO_CALENDAR_ENABLED", "true").lower() == "true"
EMAIL_TO_CALENDAR_INTERVAL = int(os.environ.get("EMAIL_TO_CALENDAR_INTERVAL", "60"))

# ---------------------------------------------------------------------------
# Phone calendar sync (iPhone Shortcut pushes consolidated calendar)
# ---------------------------------------------------------------------------
PHONE_CALENDAR_SYNC_ENABLED = os.environ.get("PHONE_CALENDAR_SYNC_ENABLED", "true").lower() == "true"

# Cached events from last phone sync (list of dicts)
# Persisted to disk so they survive orchestrator restarts
_phone_calendar_events: list = []
_phone_calendar_sync_time: float = 0.0

PHONE_CALENDAR_FILE = os.path.join(
    os.environ.get("FINANCE_DB_PATH", "/app/data/finance.db").rsplit("/", 1)[0],
    "phone_calendar.json",
)


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
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
HOME_ADDRESS = os.environ.get("HOME_ADDRESS", profile.home_address)
TRAVEL_TIME_BUFFER = int(os.environ.get("TRAVEL_TIME_BUFFER", "10"))  # extra minutes

# ---------------------------------------------------------------------------
# Auto-learn configuration
# ---------------------------------------------------------------------------
AUTO_LEARN_ENABLED = os.environ.get("AUTO_LEARN_ENABLED", "true").lower() == "true"
AUTO_LEARN_DELAY_MINUTES = int(os.environ.get("AUTO_LEARN_DELAY_MINUTES", "10"))
AUTO_LEARN_MAX_FACTS = int(os.environ.get("AUTO_LEARN_MAX_FACTS", "5"))
AUTO_LEARN_DEDUP_THRESHOLD = float(os.environ.get("AUTO_LEARN_DEDUP_THRESHOLD", "0.85"))
AUTO_LEARN_MARKDOWN = os.environ.get("AUTO_LEARN_MARKDOWN", "false").lower() == "true"
AUTO_LEARN_ENCRYPT = os.environ.get("AUTO_LEARN_ENCRYPT", "true").lower() == "true"
AUTO_LEARN_ENCRYPTION_KEY = os.environ.get("AUTO_LEARN_ENCRYPTION_KEY", "")

# In-memory conversation buffer for auto-learn (never written to disk)
_auto_learn_conversations: Dict[str, list] = {}
