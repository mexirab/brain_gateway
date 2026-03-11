"""
Shared state for the Brain Gateway orchestrator modules.

All cross-module state lives here so modules can import what they need
without circular dependencies.
"""

import os
import logging
import time
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

from ha_integration import HomeAssistantClient
from user_profile import get_profile
from llm_backend import LLMConfig, LLMBackend, create_backend

# Load environment (fallback for local dev; Docker passes env vars directly)
load_dotenv("/app/.env", override=False)

# ---------------------------------------------------------------------------
# User profile (loaded once at import time)
# ---------------------------------------------------------------------------
profile = get_profile()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model endpoints
# ---------------------------------------------------------------------------
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "http://10.0.0.58:8001/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/Nemotron-Orchestrator-8B")
HELIOS_URL = os.environ.get("HELIOS_URL", "http://10.0.0.195:8080/v1")
HELIOS_MODEL = os.environ.get("HELIOS_MODEL", "Qwen3-32B-Q5_K_M.gguf")

# ---------------------------------------------------------------------------
# LLM Backend instances (initialized in startup_event after _http is ready)
# ---------------------------------------------------------------------------
conversation_backend: Optional[LLMBackend] = None
orchestrator_backend: Optional[LLMBackend] = None


def init_backends(http_client: httpx.AsyncClient):
    """
    Initialize LLM backend instances.

    Priority: user_profile.yaml llm section > env vars > defaults.
    Called from startup_event after _http is initialized.
    """
    global conversation_backend, orchestrator_backend

    # Check if profile YAML has llm config
    llm_cfg = {}
    try:
        from user_profile import _find_profile_path
        import yaml
        path = _find_profile_path()
        if path:
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            llm_cfg = data.get("llm", {})
    except Exception:
        pass

    conv_cfg = llm_cfg.get("conversation", {})
    orch_cfg = llm_cfg.get("orchestrator", {})

    conv_config = LLMConfig(
        backend=conv_cfg.get("backend", "openai_compatible"),
        url=conv_cfg.get("url", HELIOS_URL),
        model=conv_cfg.get("model", HELIOS_MODEL),
        api_key=_resolve_api_key(conv_cfg.get("api_key", "")),
    )
    orch_config = LLMConfig(
        backend=orch_cfg.get("backend", "openai_compatible"),
        url=orch_cfg.get("url", NEMOTRON_URL),
        model=orch_cfg.get("model", NEMOTRON_MODEL),
        api_key=_resolve_api_key(orch_cfg.get("api_key", "")),
    )

    conversation_backend = create_backend(conv_config, http_client)
    orchestrator_backend = create_backend(orch_config, http_client)

    logger.info(f"[LLM] Conversation backend: {conv_config.backend} -> {conv_config.url} ({conv_config.model})")
    logger.info(f"[LLM] Orchestrator backend: {orch_config.backend} -> {orch_config.url} ({orch_config.model})")


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
embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

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
# Helios idle tracking
# ---------------------------------------------------------------------------
_last_helios_request: float = 0.0

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

# Track which calendar events we've already announced (resets on restart)
_notified_events: set = set()

# ---------------------------------------------------------------------------
# Email polling config
# ---------------------------------------------------------------------------
EMAIL_POLL_INTERVAL = int(os.environ.get("EMAIL_POLL_INTERVAL", "30"))
EMAIL_POLL_ENABLED = os.environ.get("EMAIL_POLL_ENABLED", "true").lower() == "true"

# Track which emails we've already announced (resets on restart)
_notified_emails: set = set()

# ---------------------------------------------------------------------------
# Temperature monitoring
# ---------------------------------------------------------------------------
CLOSET_TEMP_WARNING = float(os.environ.get("CLOSET_TEMP_WARNING", str(profile.temp_warning)))
CLOSET_TEMP_CRITICAL = float(os.environ.get("CLOSET_TEMP_CRITICAL", str(profile.temp_critical)))

# Track which temperature alerts have fired (resets on restart)
_notified_temp_alerts: set = set()

# ---------------------------------------------------------------------------
# Email-to-calendar config
# ---------------------------------------------------------------------------
EMAIL_TO_CALENDAR_ENABLED = os.environ.get("EMAIL_TO_CALENDAR_ENABLED", "true").lower() == "true"
EMAIL_TO_CALENDAR_INTERVAL = int(os.environ.get("EMAIL_TO_CALENDAR_INTERVAL", "60"))

# Track which emails we've already scanned for events (resets on restart)
_processed_for_events: set = set()

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
            with open(PHONE_CALENDAR_FILE, "r") as f:
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

# Track which events we have already sent travel-time alerts for
_notified_travel_events: set = set()
