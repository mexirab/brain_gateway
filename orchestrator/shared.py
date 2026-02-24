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

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model endpoints
# ---------------------------------------------------------------------------
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "http://10.0.0.58:8001/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/Nemotron-Orchestrator-8B")
HELIOS_URL = os.environ.get("HELIOS_URL", "http://10.0.0.195:8080/v1")
HELIOS_MODEL = os.environ.get("HELIOS_MODEL", "Qwen3-32B-Q5_K_M.gguf")

# ---------------------------------------------------------------------------
# Home Assistant
# ---------------------------------------------------------------------------
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
ha_client = HomeAssistantClient(url=HA_URL, token=HA_TOKEN)

# ---------------------------------------------------------------------------
# RAG / ChromaDB
# ---------------------------------------------------------------------------
CHROMA_PERSIST = os.environ.get("CHROMA_PERSIST", "/home/nadim/.local/share/chroma/personal_rag")
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
FOCUS_AUDIO_PLAYER = os.environ.get("FOCUS_AUDIO_PLAYER", "media_player.office_speaker")
ENDEL_ENABLED = os.environ.get("ENDEL_ENABLED", "true").lower() == "true"

# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------
TIMEZONE = os.environ.get("TZ", "America/New_York")
scheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    timezone=TIMEZONE,
)

# ---------------------------------------------------------------------------
# Calendar polling config
# ---------------------------------------------------------------------------
CALENDAR_POLL_INTERVAL = int(os.environ.get("CALENDAR_POLL_INTERVAL", "15"))
MORNING_BRIEFING_TIME = os.environ.get("MORNING_BRIEFING_TIME", "07:30")
MORNING_BRIEFING_ENABLED = os.environ.get("MORNING_BRIEFING_ENABLED", "true").lower() == "true"

# Track which calendar events we've already announced (resets on restart)
_notified_events: set = set()
