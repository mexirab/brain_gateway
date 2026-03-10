"""
Brain Gateway Orchestrator v6 - Hybrid Architecture
- Helios (Qwen3-32B) is the primary conversational assistant (Jessica)
- Nemotron (8B) is the tool orchestrator (HA, RAG, reminders, update_data)
- Flow: User → Helios → (ask_orchestrator) → Nemotron → tools → result → Helios → User
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)
"""

import os
import re
import json
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import random
from sentence_transformers import SentenceTransformer
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import chromadb
from chromadb.config import Settings

# Import the HA integration module
from ha_integration import HomeAssistantClient, ExecutionResult

# Import fast-path for simple device commands (bypasses LLMs)
from fast_path import try_fast_path

# Import the data manager for structured data updates
from data_manager import handle_update_data

# Import the reminder manager for voice reminders
from reminder_manager import (
    parse_time_expression,
    format_time_friendly,
    add_reminder,
    get_reminder,
    list_pending_reminders,
    remove_reminder,
    mark_reminder_completed,
    _announce_voice,
    _send_notification,
)

# Import the Pi-hole client for focus blocking
from pihole_client import get_pihole_client

# Import the web search client for SearXNG
from web_search import get_search_client

# Import the Google Calendar client
from google_calendar import get_calendar_client
from mode_router import get_mode_router, MODE_PROMPTS, TONE_CONSTRAINT, get_tone_constraint
from user_profile import get_profile

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# User profile (names, speakers, sensors, etc.)
profile = get_profile()

# Model endpoints and names
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "http://10.0.0.173:8001/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/Nemotron-Orchestrator-8B")
HELIOS_URL = os.environ.get("HELIOS_URL", "http://10.0.0.195:8080/v1")
HELIOS_MODEL = os.environ.get("HELIOS_MODEL", "Qwen3-32B-Q5_K_M")

# Home Assistant
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# RAG settings
CHROMA_PERSIST = os.environ.get("CHROMA_PERSIST", "/home/labadmin/.local/share/chroma/personal_rag")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "nadim_rag")
MIN_COS = float(os.environ.get("MIN_COS", "0.30"))
TOP_K = int(os.environ.get("TOP_K", "6"))

# Agentic settings
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "5"))  # Prevent infinite loops

app = FastAPI(title="Brain Gateway", version="5.0")

# CORS middleware for frontend access
from fastapi.middleware.cors import CORSMiddleware

_cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3001").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared httpx client for connection reuse
_http: httpx.AsyncClient = None

# HA tool definition cache (avoids rebuilding entity list every Nemotron round)
_ha_tool_cache: Optional[Dict[str, Any]] = None
_ha_tool_cache_time: float = 0.0
_HA_TOOL_CACHE_TTL: float = 300.0  # 5 minutes

# Helios idle tracking for auto-shutdown
_last_helios_request: float = 0.0

# Focus timer state (Pomodoro)
current_focus_session = {
    "active": False,
    "task": None,
    "started": None,
    "duration": None,
    "break_duration": None,
    "job_id": None,
    "audio_player": None,
    "block_sites": False
}

# Endel focus audio configuration
ENDEL_API_URL = "https://app.endel.io/api/pacific"
ENDEL_MODES = ["focus", "deeper-focus", "study", "colored-noises"]
FOCUS_AUDIO_PLAYER = os.environ.get("FOCUS_AUDIO_PLAYER", "media_player.office_speaker")
ENDEL_ENABLED = os.environ.get("ENDEL_ENABLED", "true").lower() == "true"

# APScheduler for reminder scheduling (single source of truth: YAML)
TIMEZONE = os.environ.get("TZ", "America/New_York")
scheduler = AsyncIOScheduler(
    jobstores={'default': MemoryJobStore()},
    timezone=TIMEZONE
)


# =============================================================================
# TOOLS DEFINITION - These are the capabilities Nemotron can use
# =============================================================================

def get_ha_tool_definition() -> Dict[str, Any]:
    """Build the home_assistant tool with current entity list (cached)."""
    global _ha_tool_cache, _ha_tool_cache_time

    if _ha_tool_cache and (time.time() - _ha_tool_cache_time) < _HA_TOOL_CACHE_TTL:
        return _ha_tool_cache

    entity_lines = []
    for domain in ["light", "switch", "fan", "climate", "cover", "scene", "lock"]:
        entities = ha_client.get_entities_by_domain(domain)
        for e in entities:
            entity_lines.append(f"  - {e.entity_id} ({e.friendly_name})")

    entity_list = "\n".join(entity_lines[:60]) if entity_lines else "  (entities loading...)"

    _ha_tool_cache = {
        "type": "function",
        "function": {
            "name": "home_assistant",
            "description": f"""Control smart home via Home Assistant API. Call this ONCE per entity.

ENTITIES:
{entity_list}

SERVICES:
- light: turn_on (brightness 0-255, rgb_color [R,G,B]), turn_off, toggle
- switch/fan: turn_on, turn_off, toggle
- climate: set_temperature (temperature: int)
- cover: open_cover, close_cover
- scene: turn_on

COLORS: rgb_color as [R,G,B]. Blue=[0,0,255], Red=[255,0,0], Green=[0,255,0], Purple=[128,0,128], Yellow=[255,255,0], Orange=[255,165,0], Pink=[255,192,203], White=[255,255,255]
BRIGHTNESS: 0-255 scale. 50%=128, 75%=191, 100%=255""",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "Entity ID like 'light.bedroom_fan_lights'"
                    },
                    "service": {
                        "type": "string",
                        "description": "Service: turn_on, turn_off, toggle, set_temperature, etc."
                    },
                    "data": {
                        "type": "object",
                        "description": "Service data, e.g., {\"brightness\": 128, \"rgb_color\": [0,0,255]}"
                    }
                },
                "required": ["entity_id", "service"]
            }
        }
    }
    _ha_tool_cache_time = time.time()
    return _ha_tool_cache


# Static tools (non-HA)
STATIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search the user's personal knowledge base for relevant context. Use this when the user asks about personal information, projects, routines, preferences, medications, schedules, or anything that might be in their notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant personal information"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ask_expert",
            "description": "Delegate to the expert model (Helios Qwen3-32B) for ANY question requiring knowledge or reasoning. Use this for: general knowledge (books, movies, history, science), coding, analysis, explanations, factual questions, creative writing, or ANYTHING you're not certain about. The expert has broad knowledge - use it liberally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question or task to delegate to the expert model"
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional additional context to help the expert"
                    }
                },
                "required": ["question"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_data",
            "description": "Update the user's structured personal data (medications, projects). Use this when they ask to add, remove, or modify medications or project information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_medication", "remove_medication", "update_medication",
                                 "add_project", "update_project_status", "add_project_step", "complete_step"],
                        "description": "The action to perform"
                    },
                    "name": {
                        "type": "string",
                        "description": "Medication name or project name"
                    },
                    "dose": {
                        "type": "string",
                        "description": "Medication dose (e.g., '50mg')"
                    },
                    "schedule": {
                        "type": "string",
                        "enum": ["morning", "evening", "weekly", "as_needed"],
                        "description": "When to take the medication"
                    },
                    "purpose": {
                        "type": "string",
                        "description": "What the medication is for"
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["not_started", "in_progress", "blocked", "done"],
                        "description": "Project status"
                    },
                    "step": {
                        "type": "string",
                        "description": "A project step/task to add or complete"
                    },
                    "goal": {
                        "type": "string",
                        "description": "Project goal description"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Project priority"
                    },
                    "category": {
                        "type": "string",
                        "enum": ["active", "someday_maybe", "parking_lot"],
                        "description": "Project category"
                    },
                    "completed": {
                        "type": "boolean",
                        "description": "Whether to add step as already completed"
                    }
                },
                "required": ["action", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder for the user. The reminder will be announced via voice on home speakers and/or sent as a mobile notification at the specified time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_text": {
                        "type": "string",
                        "description": "What to remind about (e.g., 'take your Vyvanse', 'call mom', 'check the laundry')"
                    },
                    "time": {
                        "type": "string",
                        "description": "When to remind. Accepts: 'in 5 minutes', 'in 2 hours', 'at 3pm', 'at 14:30'"
                    },
                    "target": {
                        "type": "string",
                        "enum": ["voice", "phone", "both"],
                        "description": "Where to deliver the reminder: voice=all speakers, phone=mobile notification, both=all (default: both)"
                    }
                },
                "required": ["reminder_text", "time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "Cancel a pending reminder by its ID. Use list_reminders to see pending reminders first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "string",
                        "description": "The ID of the reminder to cancel (e.g., 'abc12345')"
                    }
                },
                "required": ["reminder_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "start_focus",
            "description": "Start a focus timer (Pomodoro) with Endel focus audio and distraction blocking. Announces break time via voice when timer ends. Helps with ADHD time blindness and hyperfocus protection.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "What you're focusing on (e.g., 'writing report', 'coding', 'emails')"
                    },
                    "duration": {
                        "type": "integer",
                        "description": "Focus duration in minutes (default 25)"
                    },
                    "break_duration": {
                        "type": "integer",
                        "description": "Break duration in minutes (default 5)"
                    },
                    "speaker": {
                        "type": "string",
                        "description": "Media player for audio (e.g., 'office speaker', 'bedroom', 'kitchen'). Uses default if not specified."
                    },
                    "soundscape": {
                        "type": "string",
                        "enum": ["focus", "deeper-focus", "study", "colored-noises", "none"],
                        "description": "Endel soundscape to play (default: focus, 'none' to disable)"
                    },
                    "block_sites": {
                        "type": "boolean",
                        "description": "ALWAYS true unless user explicitly says 'without blocking' or 'no blocking'. Do not set to false unless explicitly requested."
                    }
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_focus",
            "description": "Stop the current focus timer early.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "focus_status",
            "description": "Check how much time is left in the current focus session.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for real-world information. Use for: current events, news, weather, restaurants, sports, businesses, or any factual question about the real world that isn't in the user's personal notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'Houston events this weekend', 'weather in Houston today')"
                    },
                    "category": {
                        "type": "string",
                        "enum": ["general", "news"],
                        "description": "Search category (default: general)"
                    },
                    "time_range": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year"],
                        "description": "Optional time filter for recent results"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_calendar",
            "description": "Check the user's Google Calendar for upcoming events. Use when they ask about their schedule, what's on their calendar, or what's happening today/tomorrow/this week.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Number of days to look ahead (default: 7). Use 1 for today, 2 for tomorrow, 7 for this week."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new event on the user's Google Calendar. Use when they ask to add, schedule, or create a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Event title (e.g., 'Pickleball at Honcho')"
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Event start time in ISO 8601 format (e.g., '2026-02-21T19:00:00')"
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Duration in minutes (default: 60)"
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional event description"
                    },
                    "location": {
                        "type": "string",
                        "description": "Optional event location"
                    }
                },
                "required": ["title", "start_time"]
            }
        }
    }
]


def get_orchestrator_tools() -> List[Dict[str, Any]]:
    """Get all tools including dynamic HA tool with entity list."""
    # For Nemotron orchestrator, exclude ask_expert (Helios IS the expert now)
    nemotron_tools = [t for t in STATIC_TOOLS if t["function"]["name"] != "ask_expert"]
    return [get_ha_tool_definition()] + nemotron_tools


def is_greeting(text: str) -> bool:
    """Check if text is a simple greeting (skip RAG for these)."""
    greetings = ["hi", "hello", "hey", "good morning", "good afternoon",
                 "good evening", "good night", "what's up", "howdy", "yo"]
    text_lower = text.lower().strip().rstrip("!?.,")
    # Check exact match or starts with greeting
    if text_lower in greetings:
        return True
    for g in greetings:
        if text_lower.startswith(g + " ") or text_lower.startswith(g + ","):
            return True
    return False


def get_helios_system_prompt(personal_context: str = "", mode: str = "explainer", intensity: str = "low") -> str:
    """System prompt for Helios as the primary conversational assistant."""
    user = profile.user_name
    assistant = profile.assistant_name
    tone = get_tone_constraint(user)
    context_section = ""
    if personal_context:
        context_section = f"""
PERSONAL CONTEXT (from {user}'s notes):
{personal_context}
"""

    mode_block = MODE_PROMPTS.get(mode, MODE_PROMPTS["explainer"])

    return f"""You are {assistant}, {user}'s personal AI assistant and ADHD coach.

PERSONALITY:
- {profile.assistant_personality}
- Understand ADHD challenges (task initiation, time blindness, overwhelm)
- Keep responses concise and natural for voice conversations
- Celebrate small wins, be encouraging without being patronizing

{tone}

{mode_block}
{context_section}
YOU HAVE ONE TOOL: ask_orchestrator
Use it ONLY when you need to:
- Control smart home devices (lights, fans, switches, thermostats)
- Search {user}'s personal notes/memory for specific info
- Set reminders for specific times
- Update personal data (medications, projects)
- Look up real-world information (events, news, weather, restaurants, sports, businesses)

IMPORTANT RULES:
- For greetings (hi, hello, good morning) - just respond warmly, NO tools
- For general chat/questions - respond naturally using your knowledge + context above
- For device control, reminders, or personal data updates - use ask_orchestrator
- For real-world questions (events, news, weather, businesses, sports) - use ask_orchestrator to search the web
- After getting a tool result, respond naturally to the user (don't just repeat the raw result)
- NEVER mention internal tool names (ask_orchestrator, update_data, etc.) to the user. Just do the action or say you'll handle it.

RESPONSE STYLE:
- Brief and natural (2-3 sentences typical)
- Conversational, not robotic
- For voice: avoid markdown, bullets, or formatting
- No emojis unless {user} uses them first"""


# Tool definition for Helios - just one simple tool
HELIOS_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_orchestrator",
            "description": "Delegate an action to the smart home/personal assistant orchestrator. Use for: controlling devices (lights, fans, switches), searching personal notes, setting reminders, updating medications/projects, or searching the web for real-world information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Natural language command (e.g., 'turn off bedroom lights', 'what are my morning meds', 'remind me to call mom in 30 minutes')"
                    }
                },
                "required": ["command"]
            }
        }
    }
]


# ChromaDB client
chroma = chromadb.PersistentClient(
    path=os.path.expanduser(CHROMA_PERSIST),
    settings=Settings(anonymized_telemetry=False),
)
collection = chroma.get_or_create_collection(CHROMA_COLLECTION)
# Use same embedding model as ingest_rag.py
embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
# Initialize Home Assistant client (auto-discovers entities!)
ha_client = HomeAssistantClient(url=HA_URL, token=HA_TOKEN)

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def last_user_text(messages: List[Dict[str, Any]]) -> str:
    """Extract the most recent user message."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content.strip()
            # Handle multimodal content
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "").strip()
    return ""


def rag_context(query: str) -> str:
    """Query ChromaDB for relevant personal context."""
    original_query = query

    # Normalize query: strip whitespace, leading/trailing punctuation, lowercase
    query = query.strip()
    query = query.strip("\"'`""''?!.,;:()[]{}")
    query = query.lower()

    if not query:
        logger.warning(f"[RAG] Empty query after normalization (original: '{original_query}')")
        return ""

    logger.info(f"[RAG] Searching for: '{query}' (original: '{original_query}')")

    try:
        # Use the same embedding model as ingest_rag.py
        query_embedding = embedding_model.encode(query, normalize_embeddings=True).tolist()

        res = collection.query(
            query_embeddings=[query_embedding],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.error(f"[RAG] Query error: {e}")
        return ""

    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]

    logger.info(f"[RAG] Retrieved {len(docs)} candidates from ChromaDB")

    # Log all scores for debugging
    all_scores = [1.0 - float(d) for d in dists]
    logger.info(f"[RAG] Candidate scores: {[f'{s:.2f}' for s in all_scores]}")

    # Always include all retrieved results when RAG is triggered
    # (LLM can judge relevance better than embedding thresholds)
    MIN_RESULTS = TOP_K
    MIN_CHUNK_LEN = 100  # Skip header-only chunks

    chunks = []
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists)):
        # Skip None or very short chunks (likely just headers)
        if doc is None or len(doc.strip()) < MIN_CHUNK_LEN:
            logger.debug(f"[RAG] Skipping short chunk ({len(doc)} chars)")
            continue

        try:
            cos = 1.0 - float(dist)
        except:
            cos = None

        # Skip low-scoring results UNLESS we haven't hit MIN_RESULTS yet
        if cos is not None and cos < MIN_COS and len(chunks) >= MIN_RESULTS:
            continue

        src = ""
        if isinstance(meta, dict):
            src = meta.get("file_path") or meta.get("source") or ""

        entry = f"- {doc[:800]}"  # Increased from 500 to show more content
        if src:
            entry += f"\n  (source: {src})"
        if cos:
            entry += f" [relevance: {cos:.2f}]"
        chunks.append(entry)

    logger.info(f"[RAG] Returning {len(chunks)} chunks (filtered by MIN_COS={MIN_COS})")

    return "\n".join(chunks) if chunks else ""

async def call_model(url: str, model: str, messages: List[Dict], system: str = "", tools: List = None, tool_choice: str = "auto", timeout: int = 180) -> Dict[str, Any]:
    """Call an LLM endpoint.

    Args:
        tool_choice: "auto" for native tool calling (Helios), "none" for XML-style (Nemotron)
    """
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})

    payload = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    # Pass through tools with specified tool_choice
    # - "auto": Enable native tool calling (for Helios/llama.cpp)
    # - "none": Disable native tool calling, model outputs <tool_call> tags (for Nemotron/vLLM)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    r = await _http.post(f"{url}/chat/completions", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def stream_final_response(url: str, model: str, messages: List[Dict], system: str = "", timeout: int = 180):
    """
    Stream the final response from Nemotron (after tool calls are done).
    Pass through SSE chunks directly for minimal latency.
    """
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})

    payload = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": True,
    }

    async with _http.stream("POST", f"{url}/chat/completions", json=payload, timeout=timeout) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line:
                continue
            if line.startswith("data: "):
                # Pass through directly for minimal latency
                yield f"{line}\n\n"


# =============================================================================
# TOOL EXECUTION HANDLERS
# =============================================================================

async def execute_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Execute a tool and return the result as a string."""
    logger.info(f"[TOOL] Executing: {tool_name} with args: {arguments}")

    try:
        if tool_name == "home_assistant":
            return await tool_home_assistant(
                arguments.get("entity_id", ""),
                arguments.get("service", ""),
                arguments.get("data", {})
            )
        elif tool_name == "search_memory":
            return tool_search_memory(arguments.get("query", ""))
        elif tool_name == "ask_expert":
            return await tool_ask_expert(
                arguments.get("question", ""),
                arguments.get("context", "")
            )
        elif tool_name == "update_data":
            return tool_update_data(arguments)
        elif tool_name == "set_reminder":
            return await tool_set_reminder(
                arguments.get("reminder_text", ""),
                arguments.get("time", ""),
                arguments.get("target", "both")
            )
        elif tool_name == "cancel_reminder":
            return await tool_cancel_reminder(
                arguments.get("reminder_id", "")
            )
        elif tool_name == "start_focus":
            return await tool_start_focus(
                arguments.get("task", "your task"),
                arguments.get("duration", 25),
                arguments.get("break_duration", 5),
                arguments.get("speaker"),
                arguments.get("soundscape", "focus"),
                arguments.get("block_sites", True)
            )
        elif tool_name == "stop_focus":
            return await tool_stop_focus()
        elif tool_name == "focus_status":
            return await tool_focus_status()
        elif tool_name == "web_search":
            return await tool_web_search(
                arguments.get("query", ""),
                arguments.get("category", "general"),
                arguments.get("time_range")
            )
        elif tool_name == "check_calendar":
            return await tool_check_calendar(
                arguments.get("days_ahead", 7)
            )
        elif tool_name == "create_calendar_event":
            return await tool_create_calendar_event(
                arguments.get("title", ""),
                arguments.get("start_time", ""),
                arguments.get("duration_minutes", 60),
                arguments.get("description", ""),
                arguments.get("location", "")
            )
        else:
            return f"Unknown tool: {tool_name}"
    except Exception as e:
        logger.error(f"[TOOL] Error executing {tool_name}: {e}")
        return f"Error executing {tool_name}: {str(e)}"


async def tool_home_assistant(entity_id: str, service: str, data: Dict[str, Any] = None) -> str:
    """Execute a Home Assistant service call directly."""
    if not entity_id or not service:
        return "Missing entity_id or service"

    logger.info(f"[HA] Calling {service} on {entity_id} with data: {data}")
    result = await ha_client.call_service(entity_id, service, data or {})

    if result.success:
        logger.info(f"[HA] Success: {result.message}")
        return result.message
    else:
        logger.warning(f"[HA] Failed: {result.message}")
        return f"Failed: {result.message}"


def tool_search_memory(query: str) -> str:
    """Search the personal knowledge base (RAG)."""
    if not query:
        return "No query provided"

    logger.info(f"[MEMORY] Searching for: {query}")
    context = rag_context(query)

    if context:
        return f"Found relevant information:\n{context}"
    else:
        return "No relevant information found in memory."


async def tool_web_search(query: str, category: str = "general", time_range: str = None) -> str:
    """Search the web via SearXNG and return formatted results."""
    if not query:
        return "No search query provided"

    logger.info(f"[WEB_SEARCH] Searching: '{query}' (category={category}, time_range={time_range})")
    client = get_search_client(http_client=_http)
    response = await client.search(query=query, category=category, time_range=time_range)

    if not response.success:
        logger.error(f"[WEB_SEARCH] Failed for '{query}': {response.error}")
        return f"Web search failed: {response.error}"

    if not response.results:
        logger.warning(f"[WEB_SEARCH] No results for '{query}'")
        return f"No results found for '{query}'"

    # Format results as numbered text for the LLM to synthesize
    lines = [f"Web search results for '{query}':"]
    for i, r in enumerate(response.results, 1):
        lines.append(f"\n{i}. {r.title}")
        if r.content:
            lines.append(f"   {r.content}")
        lines.append(f"   URL: {r.url}")

    logger.info(f"[WEB_SEARCH] Returning {len(response.results)} results for '{query}'")
    return "\n".join(lines)


async def tool_check_calendar(days_ahead: int = 7) -> str:
    """Check Google Calendar for upcoming events."""
    client = get_calendar_client(http_client=_http)
    if not client.is_configured:
        return "Google Calendar is not configured. Run google_setup.py first to set up OAuth2 credentials."

    logger.info(f"[CALENDAR] Checking calendar for next {days_ahead} days")
    response = await client.list_events(days_ahead=days_ahead)

    if not response.success:
        return f"Calendar error: {response.error}"

    if not response.events:
        if days_ahead == 1:
            return "No events on the calendar for today."
        return f"No events on the calendar for the next {days_ahead} days."

    lines = []
    if days_ahead == 1:
        lines.append(f"Today's calendar ({len(response.events)} events):")
    else:
        lines.append(f"Calendar for the next {days_ahead} days ({len(response.events)} events):")

    for event in response.events:
        if event.all_day:
            date_str = event.start.strftime("%A %b %d")
            lines.append(f"\n- {event.title} (all day, {date_str})")
        else:
            time_str = event.start.strftime("%A %b %d, %I:%M %p")
            end_str = event.end.strftime("%I:%M %p")
            lines.append(f"\n- {event.title} — {time_str} to {end_str}")
        if event.location:
            lines.append(f"  Location: {event.location}")

    return "\n".join(lines)


async def tool_create_calendar_event(
    title: str, start_time: str, duration_minutes: int = 60,
    description: str = "", location: str = ""
) -> str:
    """Create a new Google Calendar event."""
    if not title:
        return "Missing event title."
    if not start_time:
        return "Missing event start time. Provide an ISO 8601 datetime like '2026-02-21T19:00:00'."

    client = get_calendar_client(http_client=_http)
    if not client.is_configured:
        return "Google Calendar is not configured. Run google_setup.py first to set up OAuth2 credentials."

    logger.info(f"[CALENDAR] Creating event: {title} at {start_time}")
    response = await client.create_event(
        title=title,
        start_time=start_time,
        duration_minutes=duration_minutes,
        description=description,
        location=location,
    )

    if not response.success:
        return f"Failed to create event: {response.error}"

    event = response.events[0]
    time_str = event.start.strftime("%A %b %d, %I:%M %p")
    result = f"Created event: {event.title} on {time_str}"
    if location:
        result += f" at {location}"
    return result


async def check_helios_health() -> bool:
    """Check if Helios is running and responsive."""
    try:
        r = await _http.get(f"{HELIOS_URL.replace('/v1', '')}/health", timeout=5)
        return r.status_code == 200
    except:
        return False


async def start_helios() -> bool:
    """Start Helios via SSH (paramiko) and wait for it to be ready."""
    import asyncio
    import paramiko

    logger.info("[EXPERT] Helios is offline, attempting to start...")

    # Get SSH config from environment
    helios_ip = os.environ.get("NODE_HELIOS_IP", "10.0.0.195")
    ssh_user = os.environ.get("HELIOS_SSH_USER", "labadmin")
    ssh_key = os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519")

    # Start the service via SSH using paramiko
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=helios_ip,
            username=ssh_user,
            key_filename=ssh_key,  # Mounted from host
            timeout=30
        )
        stdin, stdout, stderr = ssh.exec_command("sudo systemctl start llama-server", timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error(f"[EXPERT] Failed to start Helios: {error_msg}")
            return False
        logger.info("[EXPERT] SSH command succeeded, waiting for model to load...")
    except Exception as e:
        logger.error(f"[EXPERT] SSH to Helios failed: {e}")
        return False

    # Wait for Helios to become ready (up to 3 minutes)
    logger.info("[EXPERT] Waiting for Helios to load model...")
    for i in range(36):  # 36 * 5 seconds = 3 minutes
        await asyncio.sleep(5)
        if await check_helios_health():
            logger.info(f"[EXPERT] Helios ready after ~{(i+1)*5} seconds")
            return True
        logger.debug(f"[EXPERT] Still waiting... ({(i+1)*5}s)")

    logger.error("[EXPERT] Helios failed to start within 3 minutes")
    return False


async def stop_helios() -> bool:
    """Stop Helios via SSH to save power."""
    import paramiko

    logger.info("[EXPERT] Stopping Helios to save power...")

    # Get SSH config from environment
    helios_ip = os.environ.get("NODE_HELIOS_IP", "10.0.0.195")
    ssh_user = os.environ.get("HELIOS_SSH_USER", "labadmin")
    ssh_key = os.environ.get("HELIOS_SSH_KEY", "/root/.ssh/id_ed25519")

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=helios_ip,
            username=ssh_user,
            key_filename=ssh_key,
            timeout=30
        )
        stdin, stdout, stderr = ssh.exec_command("sudo systemctl stop llama-server", timeout=30)
        exit_code = stdout.channel.recv_exit_status()
        ssh.close()

        if exit_code != 0:
            error_msg = stderr.read().decode()
            logger.error(f"[EXPERT] Failed to stop Helios: {error_msg}")
            return False

        logger.info("[EXPERT] Helios stopped successfully")
        return True
    except Exception as e:
        logger.error(f"[EXPERT] SSH to Helios failed: {e}")
        return False


async def check_helios_idle():
    """Check if Helios should be stopped due to inactivity."""
    global _last_helios_request

    # Skip if Helios is already offline
    if not await check_helios_health():
        return

    # Skip if no requests have been made yet (Helios was started manually)
    if _last_helios_request == 0.0:
        return

    idle_timeout = int(os.environ.get("HELIOS_IDLE_TIMEOUT", 1800))
    if idle_timeout <= 0:
        return  # Auto-shutdown disabled

    idle_time = time.time() - _last_helios_request

    if idle_time > idle_timeout:
        logger.info(f"[EXPERT] Helios idle for {idle_time:.0f}s (threshold: {idle_timeout}s), stopping to save power...")
        await stop_helios()


async def tool_ask_expert(question: str, context: str = "") -> str:
    """Delegate a complex question to Helios Qwen3-32B. Auto-starts if offline."""
    global _last_helios_request

    if not question:
        return "No question provided"

    logger.info(f"[EXPERT] Delegating to Helios: {question[:100]}...")
    _last_helios_request = time.time()

    # Check if Helios is available, start if needed
    if not await check_helios_health():
        started = await start_helios()
        if not started:
            return "Expert model is offline and could not be started. Please try again later or start Helios manually."

    # Build the message for Helios
    messages = []
    if context:
        messages.append({
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {question}"
        })
    else:
        messages.append({"role": "user", "content": question})

    system_prompt = """You are an expert assistant helping with complex reasoning, coding, and analysis.
Provide detailed, thorough answers. Be precise and accurate."""

    try:
        response = await call_model(
            HELIOS_URL,
            HELIOS_MODEL,
            messages,
            system=system_prompt,
            timeout=300  # Helios can be slow
        )

        # Extract the response text - check both content and reasoning_content for reasoning models
        msg = response.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        reasoning = msg.get("reasoning_content", "")

        # For reasoning models, content may be empty while reasoning_content has the thought process
        # Return content if available, otherwise return reasoning
        if content:
            logger.info(f"[EXPERT] Helios responded ({len(content)} chars)")
            return content
        elif reasoning:
            logger.info(f"[EXPERT] Helios responded with reasoning ({len(reasoning)} chars)")
            return reasoning
        else:
            return "Expert model returned empty response"
    except Exception as e:
        logger.error(f"[EXPERT] Helios failed: {e}")
        return f"Expert model unavailable: {str(e)}"


def tool_update_data(arguments: Dict[str, Any]) -> str:
    """Update structured personal data (medications, projects)."""
    action = arguments.get("action", "")
    name = arguments.get("name", "")

    if not action:
        return "No action specified"
    if not name:
        return "No name specified"

    logger.info(f"[DATA] Updating: action={action}, name={name}")

    # Pass all arguments to the handler
    return handle_update_data(
        action=action,
        name=name,
        dose=arguments.get("dose"),
        schedule=arguments.get("schedule"),
        purpose=arguments.get("purpose"),
        notes=arguments.get("notes"),
        status=arguments.get("status"),
        step=arguments.get("step"),
        goal=arguments.get("goal"),
        priority=arguments.get("priority"),
        category=arguments.get("category"),
        completed=arguments.get("completed"),
    )


async def deliver_reminder_job(reminder_id: str):
    """Called by APScheduler at the scheduled time to deliver a reminder."""
    logger.info(f"[REMINDER] Triggering: {reminder_id}")

    reminder = get_reminder(reminder_id)
    if not reminder:
        logger.warning(f"[REMINDER] {reminder_id} not found")
        return

    if reminder.get("status") != "pending":
        logger.warning(f"[REMINDER] {reminder_id} already completed, skipping")
        return

    text = reminder.get("text", "You have a reminder")
    target = reminder.get("target", "both")
    spoken_text = f"Hey {profile.user_name}! Quick reminder: {text}"

    if target in ["voice", "both"]:
        await _announce_voice(spoken_text)

    if target in ["phone", "both"]:
        await _send_notification(text)

    mark_reminder_completed(reminder_id)
    logger.info(f"[REMINDER] Completed: {reminder_id}")


async def tool_set_reminder(reminder_text: str, time_str: str, target: str = "both") -> str:
    """
    Set a reminder that will be delivered via voice and/or mobile notification.

    Parses the time expression, stores in memory, and schedules with APScheduler.
    """
    if not reminder_text:
        return "Please tell me what to remind you about."
    if not time_str:
        return "Please tell me when to remind you. Try 'in 5 minutes' or 'at 3pm'."

    if target not in ["voice", "phone", "both"]:
        target = "both"

    logger.info(f"[REMINDER] Setting reminder: '{reminder_text}' at '{time_str}' via {target}")

    trigger_time, error = parse_time_expression(time_str)
    if error:
        return error

    # Deduplication: reject identical pending reminders created within 60s
    DEDUP_WINDOW_SECONDS = 60
    now = datetime.now()
    for existing in list_pending_reminders():
        if existing.get("text", "").lower().strip() == reminder_text.lower().strip():
            try:
                created = datetime.fromisoformat(existing.get("created", ""))
                if (now - created).total_seconds() < DEDUP_WINDOW_SECONDS:
                    logger.warning(f"[REMINDER] Duplicate rejected: '{reminder_text}' (existing {existing.get('id')})")
                    return f"You already have a reminder for '{reminder_text}' - I won't create a duplicate."
            except (ValueError, TypeError):
                pass

    import uuid
    reminder_id = str(uuid.uuid4())[:8]

    add_reminder(reminder_id, reminder_text, trigger_time, target)

    scheduler.add_job(
        deliver_reminder_job,
        trigger='date',
        run_date=trigger_time,
        args=[reminder_id],
        id=f"reminder_{reminder_id}",
        replace_existing=True
    )
    logger.info(f"[SCHEDULER] Scheduled job reminder_{reminder_id} for {trigger_time}")

    time_friendly = format_time_friendly(trigger_time)
    target_desc = {
        "voice": "on all speakers",
        "phone": "on your phone",
        "both": "on all speakers and your phone"
    }.get(target, "")

    return f"Got it! I'll remind you to {reminder_text} {time_friendly} {target_desc}."


async def tool_cancel_reminder(reminder_id: str) -> str:
    """Cancel a pending reminder."""
    # Remove from scheduler
    try:
        scheduler.remove_job(f"reminder_{reminder_id}")
        logger.info(f"[SCHEDULER] Removed job reminder_{reminder_id}")
    except Exception as e:
        logger.debug(f"[SCHEDULER] Job not found: {e}")

    if remove_reminder(reminder_id):
        return f"Reminder {reminder_id} cancelled."
    return f"Reminder {reminder_id} not found."


# =============================================================================
# Focus Timer (Pomodoro) Tools
# =============================================================================

def resolve_speaker_entity(speaker_name: str) -> Optional[str]:
    """Map friendly speaker names to entity IDs."""
    # Common aliases
    aliases = {
        "office": "media_player.dining_room_max",
        "office speaker": "media_player.dining_room_max",
        "bedroom": "media_player.bedroom_pair",
        "kitchen": "media_player.kitchen_display",
        "living room": "media_player.living_room_speakers",
        "laundry": "media_player.laundry_room",
        "closet": "media_player.laundry_room",
        "all": "media_player.all_speakers",
        "everywhere": "media_player.all_speakers",
    }
    name_lower = speaker_name.lower().strip()
    if name_lower in aliases:
        return aliases[name_lower]
    # Check if it's already an entity ID
    if speaker_name.startswith("media_player."):
        return speaker_name
    # Try to match against HA entities
    for entity in ha_client.get_entities_by_domain("media_player"):
        if name_lower in entity.friendly_name.lower():
            return entity.entity_id
    return None


async def get_endel_focus_url(duration_minutes: int, mode: str = "focus") -> Optional[str]:
    """Fetch Endel HLS playlist and extract direct audio URL for Cast devices."""
    if mode not in ENDEL_MODES:
        mode = "focus"
    hour = datetime.now().hour
    playlist_url = f"{ENDEL_API_URL}?mode={mode}&hour={hour}&hlsjs=1&duration={duration_minutes}"

    try:
        # Fetch the HLS playlist and extract direct audio URLs
        resp = await _http.get(playlist_url, timeout=10)
        resp.raise_for_status()

        # Parse M3U8 playlist to get direct audio URLs
        audio_urls = []
        for line in resp.text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                audio_urls.append(line)

        if audio_urls:
            # Return the first audio segment URL (Cast can play .aac directly)
            logger.info(f"[ENDEL] Extracted {len(audio_urls)} audio URLs from playlist")
            return audio_urls[0]
        else:
            logger.warning("[ENDEL] No audio URLs found in playlist")
            return None
    except Exception as e:
        logger.error(f"[ENDEL] Failed to fetch playlist: {e}")
        return None


async def start_focus_audio(duration_minutes: int, player: str, soundscape: str = "focus") -> bool:
    """Start Endel focus audio on specified media player."""
    if not ENDEL_ENABLED:
        logger.info("[FOCUS] Endel audio disabled via ENDEL_ENABLED=false")
        return False

    url = await get_endel_focus_url(duration_minutes, soundscape)
    if not url:
        return False

    logger.info(f"[FOCUS] Starting Endel {soundscape} audio on {player}: {url}")

    # Play via Home Assistant
    result = await ha_client.call_service(
        player,
        "play_media",
        {"media_content_id": url, "media_content_type": "music"}
    )
    if not result.success:
        logger.error(f"[FOCUS] Failed to start audio on {player}: {result.message}")
    return result.success


async def stop_focus_audio(player: str = None) -> bool:
    """Stop audio playback on media player."""
    player = player or FOCUS_AUDIO_PLAYER
    logger.info(f"[FOCUS] Stopping audio on {player}")
    result = await ha_client.call_service(player, "media_stop", {})
    if not result.success:
        logger.error(f"[FOCUS] Failed to stop audio on {player}: {result.message}")
    return result.success


async def tool_start_focus(task: str, duration: int = 25, break_duration: int = 5,
                           speaker: str = None, soundscape: str = "focus",
                           block_sites: bool = True) -> str:
    """Start a focus timer with voice announcement at end, optional Endel audio, and distraction blocking."""
    global current_focus_session

    if current_focus_session["active"]:
        elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
        remaining = current_focus_session["duration"] - elapsed
        return f"You're already focusing on '{current_focus_session['task']}' with {remaining:.0f} minutes left. Say 'stop focus' to end early."

    # Resolve speaker (use parameter, env default, or None)
    player = None
    if speaker:
        player = resolve_speaker_entity(speaker)
        if not player:
            return f"I couldn't find a speaker matching '{speaker}'. Try 'office', 'bedroom', or 'kitchen'."
    else:
        player = FOCUS_AUDIO_PLAYER  # env var default

    # Start Endel focus audio (if soundscape != "none")
    audio_started = False
    if soundscape != "none" and player:
        audio_started = await start_focus_audio(duration, player, soundscape)
        if audio_started:
            current_focus_session["audio_player"] = player
            logger.info(f"[FOCUS] Started Endel {soundscape} audio on {player}")
        else:
            logger.warning(f"[FOCUS] Endel {soundscape} audio failed to start on {player}")

    # Enable site blocking if requested
    blocking_enabled = False
    if block_sites:
        pihole = get_pihole_client()
        result = await pihole.enable_focus_blocking()
        if result.success:
            blocking_enabled = True
            logger.info("[FOCUS] Enabled Pi-hole distraction blocking")
        else:
            logger.warning(f"[FOCUS] Could not enable blocking: {result.message}")
            # Continue anyway - blocking is optional enhancement

    # Schedule break announcement
    end_time = datetime.now() + timedelta(minutes=duration)
    job_id = f"focus_{datetime.now().strftime('%H%M%S')}"

    scheduler.add_job(
        deliver_focus_break,
        trigger='date',
        run_date=end_time,
        args=[task, break_duration],
        id=job_id,
        replace_existing=True
    )

    current_focus_session = {
        "active": True,
        "task": task,
        "started": datetime.now(),
        "duration": duration,
        "break_duration": break_duration,
        "job_id": job_id,
        "audio_player": player if audio_started else None,
        "block_sites": blocking_enabled
    }

    logger.info(f"[FOCUS] Started {duration}min focus on '{task}', break at {end_time.strftime('%H:%M')}")

    # Build response message
    parts = []
    if audio_started:
        speaker_name = player.replace("media_player.", "").replace("_", " ")
        parts.append(f"Focus timer started with Endel {soundscape} sounds on the {speaker_name}!")
    else:
        parts.append("Focus timer started!")

    parts.append(f"You have {duration} minutes to work on '{task}'.")

    if blocking_enabled:
        parts.append("Distracting sites are blocked.")

    parts.append("I'll let you know when it's break time.")

    return " ".join(parts)


async def tool_stop_focus() -> str:
    """Stop the current focus timer."""
    global current_focus_session

    if not current_focus_session["active"]:
        return "No focus timer is running."

    task = current_focus_session["task"]
    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60

    # Stop audio if playing
    if current_focus_session.get("audio_player"):
        await stop_focus_audio(current_focus_session["audio_player"])
        logger.info("[FOCUS] Stopped Endel audio")

    # Disable site blocking if it was enabled
    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            logger.info("[FOCUS] Disabled Pi-hole distraction blocking")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking: {result.message}")

    # Cancel scheduled break
    try:
        scheduler.remove_job(current_focus_session["job_id"])
    except Exception:
        pass

    current_focus_session = {
        "active": False,
        "task": None,
        "started": None,
        "duration": None,
        "break_duration": None,
        "job_id": None,
        "audio_player": None,
        "block_sites": False
    }

    logger.info(f"[FOCUS] Stopped early after {elapsed:.0f}min on '{task}'")
    return f"Focus timer stopped. You worked on '{task}' for {elapsed:.0f} minutes. Nice work!"


async def tool_focus_status() -> str:
    """Check current focus timer status."""
    global current_focus_session

    if not current_focus_session["active"]:
        return "No focus timer is running. Say 'start focus on [task]' to begin."

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    if remaining <= 0:
        return f"Your focus session on '{current_focus_session['task']}' just ended!"

    return f"You're focusing on '{current_focus_session['task']}'. {remaining:.0f} minutes left, {elapsed:.0f} minutes in."


async def deliver_focus_break(task: str, break_duration: int):
    """Called by scheduler when focus time ends."""
    global current_focus_session

    # Stop Endel audio first (before announcement)
    if current_focus_session.get("audio_player"):
        await stop_focus_audio(current_focus_session["audio_player"])
        logger.info("[FOCUS] Stopped Endel audio before break announcement")

    # Disable site blocking during break
    if current_focus_session.get("block_sites"):
        pihole = get_pihole_client()
        result = await pihole.disable_focus_blocking()
        if result.success:
            logger.info("[FOCUS] Disabled Pi-hole blocking for break")
        else:
            logger.warning(f"[FOCUS] Could not disable blocking for break: {result.message}")

    # Generate encouraging break message
    messages = [
        f"Great focus session on {task}! Take a {break_duration} minute break. Stretch, grab water, rest your eyes.",
        f"Time's up! You crushed it working on {task}. {break_duration} minute break - you've earned it!",
        f"Focus session complete! Step away from {task} for {break_duration} minutes. Move around, breathe.",
        f"Nice work on {task}! Your brain needs a {break_duration} minute reset. Get up and stretch!",
        f"Pomodoro done! Great job on {task}. Take {break_duration} minutes to recharge."
    ]
    message = random.choice(messages)

    # Announce via voice
    await _announce_voice(message)

    # Reset state
    current_focus_session = {
        "active": False,
        "task": None,
        "started": None,
        "duration": None,
        "break_duration": None,
        "job_id": None,
        "audio_player": None,
        "block_sites": False
    }

    logger.info(f"[FOCUS] Break announced for '{task}'")


async def _run_nemotron_tool_loop(messages: List[Dict], system_prompt: str, label: str = "NEMOTRON") -> str:
    """
    Shared Nemotron agentic tool loop.

    Runs Nemotron in a loop, parsing tool calls, deduplicating across rounds,
    executing tools, and returning the final text result.
    """
    executed_calls = set()  # Cross-round deduplication

    for round_num in range(MAX_TOOL_ROUNDS):
        logger.info(f"[{label}] Round {round_num + 1}/{MAX_TOOL_ROUNDS}")

        try:
            llm_resp = await call_model(
                NEMOTRON_URL, NEMOTRON_MODEL, messages,
                system=system_prompt,
                tools=get_orchestrator_tools(),
                tool_choice="none",  # Nemotron uses XML-style tool calls in content
                timeout=60
            )
        except Exception as e:
            logger.error(f"[{label}] Call failed: {e}")
            return f"Sorry, I couldn't complete that action: {e}"

        # Extract the assistant's response
        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        content = message.get("content") or ""

        # Parse tool calls from content (Nemotron outputs <tool_call> tags)
        if not tool_calls and content:
            tool_calls = parse_tool_calls_from_content(content)

        # If no tool calls, we're done
        if not tool_calls:
            result = clean_response(content)
            logger.info(f"[{label}] Final result: {result[:100]}...")
            return result

        # Filter out calls we've already executed in previous rounds
        new_tool_calls = []
        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}
            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            if call_key in executed_calls:
                logger.info(f"[{label}] Skipping repeat tool call: {tool_name} (already executed)")
            else:
                new_tool_calls.append(tool_call)

        # If all calls are repeats, force a final response
        if not new_tool_calls:
            logger.info(f"[{label}] All tool calls are repeats, forcing final response")
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": "You already have the results above. Do NOT call any tools again. Summarize the information in a brief, natural response."
            })
            try:
                final_resp = await call_model(
                    NEMOTRON_URL, NEMOTRON_MODEL, messages,
                    system=system_prompt,
                    timeout=60
                )
                final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                return clean_response(final_content)
            except Exception as e:
                logger.error(f"[{label}] Final response failed: {e}")
                return "I found some results but couldn't summarize them. Please try again."

        # Process new tool calls
        logger.info(f"[{label}] Processing {len(new_tool_calls)} tool call(s)")
        messages.append({"role": "assistant", "content": content})

        # Tools that mutate state — return result directly, don't let Nemotron loop
        TERMINAL_TOOLS = {"start_focus", "stop_focus", "set_reminder", "cancel_reminder",
                          "home_assistant", "update_data", "create_calendar_event"}

        tool_results = []
        has_terminal = False
        for tool_call in new_tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")
            args_str = function.get("arguments", "{}")
            try:
                arguments = json.loads(args_str)
            except json.JSONDecodeError:
                arguments = {}

            call_key = (tool_name, json.dumps(arguments, sort_keys=True))
            executed_calls.add(call_key)

            result = await execute_tool(tool_name, arguments)
            tool_results.append(f"[{tool_name}] {result}")

            if tool_name in TERMINAL_TOOLS:
                has_terminal = True

        results_text = "\n".join(tool_results)

        # For state-changing tools, return immediately — don't risk Nemotron undoing them
        if has_terminal:
            logger.info(f"[{label}] Terminal tool executed, returning result directly")
            return results_text

        # Add tool results for next round
        messages.append({
            "role": "user",
            "content": f"<tool_response>\n{results_text}\n</tool_response>\n\nThe action is complete. Summarize the result in a brief, natural response. Do NOT call any more tools."
        })

    # Hit max rounds
    logger.warning(f"[{label}] Hit max tool rounds")
    return "I tried to complete that but ran into some complexity. Please try a simpler request."


async def call_nemotron_orchestrator(command: str) -> str:
    """
    Send a command to Nemotron for tool execution.
    Nemotron has access to HA, RAG, reminders, update_data.
    Returns the final result after executing any necessary tools.
    """
    logger.info(f"[NEMOTRON] Orchestrating command: {command[:100]}...")
    messages = [{"role": "user", "content": command}]
    system_prompt = get_nemotron_system_prompt()
    return await _run_nemotron_tool_loop(messages, system_prompt, label="NEMOTRON")


def clean_response(text: str) -> str:
    """Remove <think> and <tool_call> tags from Nemotron responses."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL)
    return text.strip()


def parse_tool_calls_from_content(content: str) -> List[Dict[str, Any]]:
    """Parse <tool_call> tags from Nemotron's content output.

    Nemotron outputs tool calls as:
    <tool_call>
    {"name": "tool_name", "arguments": {...}}
    </tool_call>
    """
    tool_calls = []
    pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
    matches = re.findall(pattern, content, re.DOTALL)

    for i, match in enumerate(matches):
        try:
            parsed = json.loads(match)
            tool_name = parsed.get("name", "")
            arguments = parsed.get("arguments", {})

            # Convert to standard tool_calls format
            tool_calls.append({
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments
                }
            })
        except json.JSONDecodeError as e:
            logger.warning(f"[TOOL_PARSE] Failed to parse tool call: {e}")
            continue

    return tool_calls


def get_orchestrator_system_prompt(mode: str = "explainer", intensity: str = "low") -> str:
    """System prompt for Nemotron when used in fallback mode (Helios unavailable)."""
    user = profile.user_name
    assistant = profile.assistant_name
    tone = get_tone_constraint(user)
    mode_block = MODE_PROMPTS.get(mode, MODE_PROMPTS["explainer"])
    return f"""You are {assistant}, {user}'s personal AI assistant. You have access to tools to help with actions.

{tone}

{mode_block}

AVAILABLE TOOLS:
1. home_assistant - Control smart home devices (lights, switches, thermostats, media, scenes)
2. search_memory - Search {user}'s personal notes for context (projects, routines, preferences, medications)
3. update_data - Update {user}'s personal data (medications, projects)
4. set_reminder - Set a reminder that will be announced on speakers and/or sent to their phone
5. start_focus - Start a Pomodoro focus timer with Endel audio and site blocking (task, duration, speaker, soundscape, block_sites)
6. stop_focus - Stop the current focus timer early
7. focus_status - Check how much time is left in the current focus session
8. web_search - Search the web for real-world information (events, news, weather, restaurants, sports, businesses)
9. check_calendar - Check {user}'s Google Calendar for upcoming events
10. create_calendar_event - Create a new event on {user}'s Google Calendar

WHEN TO USE TOOLS:
- home_assistant: When user asks to control devices (turn on/off, lights, fan, temperature)
- search_memory: For personal info (projects, routines, preferences, medications, schedules)
- update_data: When user wants to ADD, REMOVE, or UPDATE medications or projects
- set_reminder: When user says "remind me to..." or asks for a reminder
- start_focus: When user wants to start a focus timer, pomodoro, or work session (with optional speaker/soundscape)
- stop_focus: When user wants to stop/cancel/end the current focus timer
- focus_status: When user asks how much time is left or checks focus timer status
- web_search: For real-world questions - events, news, weather, restaurants, sports scores, businesses, or anything NOT in personal notes
- check_calendar: When user asks about their schedule, calendar, or upcoming events
- create_calendar_event: When user wants to add, schedule, or create a calendar event

CONVERSATION STYLE:
- Be warm, friendly, and conversational
- For greetings (good morning, hi, hello), respond naturally WITHOUT using any tools
- Keep responses brief and natural for voice conversations
- Only use tools when actually needed for the request

IMPORTANT:
- After getting tool results, summarize naturally in 1-2 sentences
- Be direct and concise ({user} has ADHD)
- DON'T dump raw data from tools - synthesize it into a natural response

EXAMPLES:
- "Good morning" → "Good morning! How can I help you today?"
- "Turn off the bedroom lights" → home_assistant
- "What projects am I working on?" → search_memory
- "Add Adderall 20mg to my morning meds" → update_data
- "Remind me to call mom in 30 minutes" → set_reminder
- "Start a 25 minute focus timer on writing" → start_focus(task="writing", duration=25)
- "Start focus on emails on the kitchen speaker" → start_focus(task="emails", speaker="kitchen")
- "Start focus with study sounds" → start_focus(task="work", soundscape="study")
- "How much time left on my focus?" → focus_status
- "Stop the focus timer" → stop_focus
- "What's on my calendar this week?" → check_calendar(days_ahead=7)
- "What's tomorrow look like?" → check_calendar(days_ahead=2)
- "Add pickleball Thursday at 7pm" → create_calendar_event(title="Pickleball", start_time="2026-02-26T19:00:00")"""


def get_nemotron_system_prompt() -> str:
    """System prompt for Nemotron as the tool orchestrator (called by Helios)."""
    user = profile.user_name
    return f"""You are a tool orchestrator. Execute the requested action using your available tools.

AVAILABLE TOOLS:
1. home_assistant - Control smart home devices (lights, switches, fans, thermostats, scenes)
2. search_memory - Search {user}'s personal notes for context (projects, routines, medications)
3. update_data - Update {user}'s medications or projects
4. set_reminder - Set a reminder for a specific time
5. start_focus - Start a Pomodoro focus timer with Endel audio and site blocking (task, duration=25, break_duration=5, speaker=optional, soundscape). ALWAYS enables site blocking unless user explicitly says "without blocking" or "no blocking".
6. stop_focus - Stop the current focus timer early
7. focus_status - Check how much time is left in the current focus session
8. web_search - Search the web for real-world information (events, news, weather, restaurants, sports, businesses). NOT for personal notes - use search_memory for that.
9. check_calendar - Check {user}'s Google Calendar for upcoming events (days_ahead=7)
10. create_calendar_event - Create a new event on {user}'s Google Calendar (title, start_time, duration_minutes, description, location)

YOUR JOB:
- Understand the command and use the appropriate tool(s)
- Return a brief, factual summary of what was done
- If the command is ambiguous, make a reasonable interpretation
- IMPORTANT: After a tool succeeds, do NOT call additional tools to verify. Trust the result and respond.

EXAMPLES:
- "turn off bedroom lights" → home_assistant(entity_id="light.bedroom...", service="turn_off")
- "what are my morning meds" → search_memory(query="morning medications")
- "remind me to call mom in 30 minutes" → set_reminder(reminder_text="call mom", time="in 30 minutes")
- "add Adderall to my meds" → update_data(action="add_medication", name="Adderall", ...)
- "start focus on coding for 30 minutes" → start_focus(task="coding", duration=30) [blocking enabled automatically]
- "start focus on emails on the kitchen speaker" → start_focus(task="emails", speaker="kitchen") [blocking enabled]
- "start focus without blocking" → start_focus(task="work", block_sites=false) [only if user explicitly says no blocking]
- "how much time left?" → focus_status()
- "stop the focus timer" → stop_focus()
- "latest news" → web_search(query="latest news today", category="news", time_range="day")
- "what's on my calendar this week" → check_calendar(days_ahead=7)
- "add pickleball Thursday at 7pm" → create_calendar_event(title="Pickleball", start_time="2026-02-26T19:00:00")

Be direct and efficient. Execute the tool and summarize the result."""


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up resources on shutdown."""
    global _http
    if _http:
        await _http.aclose()
        _http = None
        logger.info("[orchestrator] Closed shared HTTP client")


# =============================================================================
# PROACTIVE CALENDAR NOTIFICATIONS
# =============================================================================

# Track which events we've already announced (in-memory, resets on restart)
_notified_events: set = set()

CALENDAR_POLL_INTERVAL = int(os.environ.get("CALENDAR_POLL_INTERVAL", "15"))
MORNING_BRIEFING_TIME = os.environ.get("MORNING_BRIEFING_TIME", "07:30")
MORNING_BRIEFING_ENABLED = os.environ.get("MORNING_BRIEFING_ENABLED", "true").lower() == "true"


async def poll_calendar():
    """Every N minutes: check for events starting within 2 hours, announce via TTS."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TIMEZONE)

    client = get_calendar_client()
    if not client or not client.is_configured:
        return

    try:
        response = await client.get_upcoming(hours_ahead=2)
        if not response.success:
            logger.warning(f"[CALENDAR_POLL] Failed: {response.error}")
            return

        now = datetime.now(tz)
        for event in response.events:
            if event.id in _notified_events:
                continue
            minutes = int((event.start - now).total_seconds() / 60)
            if minutes < 0:
                continue  # Already started
            if event.all_day:
                continue  # Don't announce all-day events

            if minutes <= 120:
                if minutes <= 1:
                    time_str = "now"
                elif minutes < 60:
                    time_str = f"in {minutes} minutes"
                else:
                    hours = minutes // 60
                    remaining = minutes % 60
                    time_str = f"in {hours} hour{'s' if hours > 1 else ''}"
                    if remaining > 0:
                        time_str += f" and {remaining} minutes"

                message = f"Heads up {profile.user_name}: {event.title} {time_str}"
                if event.location:
                    message += f" at {event.location}"
                await _announce_voice(message)
                _notified_events.add(event.id)
                logger.info(f"[CALENDAR_POLL] Announced: {event.title} {time_str}")

    except Exception as e:
        logger.error(f"[CALENDAR_POLL] Error: {e}")


async def morning_briefing():
    """Morning announcement: today's events summary via TTS."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TIMEZONE)

    client = get_calendar_client()
    if not client or not client.is_configured:
        return

    try:
        response = await client.list_events(days_ahead=1)
        events = response.events if response.success else []

        parts = [f"Good morning {profile.user_name}!"]

        if events:
            parts.append(f"You have {len(events)} event{'s' if len(events) > 1 else ''} today.")
            for event in events[:5]:
                if event.all_day:
                    parts.append(f"All day: {event.title}")
                else:
                    time_str = event.start.strftime("%I:%M %p").lstrip("0")
                    parts.append(f"At {time_str}: {event.title}")
        else:
            parts.append("Your calendar is clear today.")

        # Check pending reminders
        pending = list_pending_reminders()
        if pending:
            parts.append(f"You also have {len(pending)} reminder{'s' if len(pending) > 1 else ''} pending.")

        await _announce_voice(" ".join(parts))
        logger.info(f"[MORNING_BRIEFING] Delivered: {len(events)} events, {len(pending)} reminders")

    except Exception as e:
        logger.error(f"[MORNING_BRIEFING] Error: {e}")


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )
    logger.info("[orchestrator] Initialized shared HTTP client")

    # Load HA entities at startup
    global _ha_tool_cache, _ha_tool_cache_time
    print("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    _ha_tool_cache = None  # Invalidate cache after entity refresh
    _ha_tool_cache_time = 0.0
    print(f"[orchestrator] Loaded {count} HA entities")

    # Initialize Google Calendar client
    cal_client = get_calendar_client(http_client=_http)
    if cal_client.is_configured:
        logger.info("[orchestrator] Google Calendar configured — tools enabled")
    else:
        logger.info("[orchestrator] Google Calendar not configured — tools disabled (run google_setup.py)")

    scheduler.start()
    logger.info("[SCHEDULER] Started (in-memory, no reminders to reload)")

    # Schedule proactive calendar polling (if calendar is configured)
    if cal_client.is_configured:
        scheduler.add_job(
            poll_calendar,
            trigger="interval",
            minutes=CALENDAR_POLL_INTERVAL,
            id="calendar_poll",
            name="Calendar event polling",
            replace_existing=True,
        )
        logger.info(f"[SCHEDULER] Calendar polling every {CALENDAR_POLL_INTERVAL} min")

        if MORNING_BRIEFING_ENABLED:
            hour, minute = map(int, MORNING_BRIEFING_TIME.split(":"))
            scheduler.add_job(
                morning_briefing,
                trigger="cron",
                hour=hour,
                minute=minute,
                id="morning_briefing",
                name="Morning briefing",
                replace_existing=True,
            )
            logger.info(f"[SCHEDULER] Morning briefing at {MORNING_BRIEFING_TIME}")


@app.get("/health")
async def health():
    """Health check endpoint."""
    # Check Helios status
    helios_online = await check_helios_health()

    # Get scheduled reminder count
    scheduled_jobs = len(scheduler.get_jobs())

    # Calculate Helios idle time
    idle_timeout = int(os.environ.get("HELIOS_IDLE_TIMEOUT", 1800))
    if _last_helios_request > 0:
        idle_time = int(time.time() - _last_helios_request)
        idle_info = f"{idle_time}s (timeout: {idle_timeout}s)"
    else:
        idle_info = "no requests yet"

    return {
        "ok": True,
        "version": "6.2",
        "architecture": "hybrid",
        "flow": "User → Helios (conversation) → Nemotron (tools) → Helios → User",
        "primary": f"{HELIOS_URL} ({HELIOS_MODEL})",
        "primary_status": "online" if helios_online else "offline (auto-starts on demand)",
        "helios_idle": idle_info,
        "orchestrator": f"{NEMOTRON_URL} ({NEMOTRON_MODEL})",
        "helios_tools": ["ask_orchestrator"],
        "nemotron_tools": ["home_assistant", "search_memory", "update_data", "set_reminder", "cancel_reminder", "start_focus", "stop_focus", "focus_status", "check_calendar", "create_calendar_event"],
        "calendar": {
            "configured": get_calendar_client().is_configured,
            "poll_interval_min": CALENDAR_POLL_INTERVAL if get_calendar_client().is_configured else None,
            "morning_briefing": MORNING_BRIEFING_TIME if MORNING_BRIEFING_ENABLED and get_calendar_client().is_configured else None,
        },
        "rag_collection": CHROMA_COLLECTION,
        "rag_docs": collection.count(),
        "ha_entities": len(ha_client._entities),
        "scheduler": {
            "running": scheduler.running,
            "scheduled_reminders": scheduled_jobs,
            "timezone": str(scheduler.timezone),
        },
        "focus_timer": {
            "active": current_focus_session["active"],
            "task": current_focus_session["task"],
            "remaining_minutes": (
                current_focus_session["duration"] -
                (datetime.now() - current_focus_session["started"]).total_seconds() / 60
            ) if current_focus_session["active"] else None,
            "audio_player": current_focus_session.get("audio_player"),
        } if current_focus_session["active"] else {"active": False},
        "endel": {
            "enabled": ENDEL_ENABLED,
            "default_player": FOCUS_AUDIO_PLAYER,
            "modes": ENDEL_MODES,
        },
    }


@app.get("/v1/models")
def list_models():
    """List available models in OpenAI-compatible format."""
    return {
        "object": "list",
        "data": [
            {
                "id": profile.assistant_voice,
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": f"{profile.assistant_name} (Hybrid)",
            },
            {
                "id": "brain",
                "object": "model",
                "created": 1700000000,
                "owned_by": "brain-gateway",
                "name": "Brain Gateway",
            },
        ]
    }


@app.get("/api/ha/entities")
async def list_ha_entities():
    """List all discovered Home Assistant entities (debug endpoint)."""
    await ha_client.refresh_entities()
    
    controllable = ha_client.get_all_controllable()
    
    return {
        "total": len(ha_client._entities),
        "controllable": {
            domain: [
                {"entity_id": e.entity_id, "friendly_name": e.friendly_name, "state": e.state}
                for e in entities
            ]
            for domain, entities in controllable.items()
        }
    }


@app.post("/api/ha/command")
async def execute_ha_command(req: Request):
    """Execute a Home Assistant command directly (for testing)."""
    body = await req.json()
    command = body.get("command", "")
    
    if not command:
        return JSONResponse({"error": "No command provided"}, status_code=400)
    
    result = await ha_client.execute_command(command)
    
    return {
        "success": result.success,
        "action": result.action,
        "entity_id": result.entity_id,
        "message": result.message,
        "details": result.details,
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    """
    Main chat endpoint - Hybrid Architecture v6.

    Flow: User → Helios (conversation) → ask_orchestrator → Nemotron (tools) → Helios → User

    Helios (Qwen3-32B) handles:
    - Natural conversation, greetings, general knowledge
    - Deciding when to delegate actions to the orchestrator

    Nemotron (8B) handles:
    - Tool execution: HA, RAG, reminders, update_data
    - Called via ask_orchestrator when Helios needs actions performed
    """
    body = await req.json()
    messages = body.get("messages", [])
    external_tools = body.get("tools")  # HA may send its own tools
    stream = body.get("stream", False)
    user_text = last_user_text(messages)

    # Track what we did for debugging
    routing_info = {
        "timestamp": datetime.now().isoformat(),
        "user_query_length": len(user_text),
        "architecture": "hybrid_v6",
        "tool_calls": [],
        "streaming": stream,
    }

    # Route user intent (mode + emotional intensity)
    intent = get_mode_router().route(user_text)
    routing_info["intent_mode"] = intent.mode
    routing_info["intent_intensity"] = intent.intensity
    routing_info["intent_tags"] = intent.tags
    logger.info(f"[MODE_ROUTER] mode={intent.mode} intensity={intent.intensity} tags={intent.tags}")

    # If external tools are provided (e.g., from HA voice pipeline),
    # pass through to Nemotron for native handling
    if external_tools:
        logger.info(f"[HYBRID] External tools provided ({len(external_tools)}), passing to Nemotron")
        routing_info["mode"] = "passthrough"
        try:
            llm_resp = await call_model(
                NEMOTRON_URL, NEMOTRON_MODEL, messages,
                system=get_orchestrator_system_prompt(mode=intent.mode, intensity=intent.intensity),
                tools=external_tools,
                timeout=60
            )
            llm_resp["_routing"] = routing_info
            return JSONResponse(llm_resp)
        except Exception as e:
            logger.error(f"[HYBRID] Passthrough failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=503)

    # === HYBRID MODE: Helios first, Nemotron for tools ===
    routing_info["mode"] = "hybrid"
    logger.info(f"[HYBRID] Processing: {user_text[:100]}... (stream={stream})")

    # Fast-path: intercept simple device commands before any LLM call
    try:
        fast_result = await try_fast_path(user_text, ha_client)
        if fast_result.handled:
            routing_info["mode"] = "fast_path"
            routing_info["fast_path_action"] = fast_result.action
            routing_info["fast_path_entity"] = fast_result.entity_name
            logger.info(f"[FAST-PATH] Handled: {fast_result.action} -> {fast_result.entity_name}")
            if stream:
                return _stream_text_response(fast_result.response_text, "fast-path")
            return JSONResponse({
                "id": f"chatcmpl-fp-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fast-path",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": fast_result.response_text},
                    "finish_reason": "stop",
                }],
                "_routing": routing_info,
            })
    except Exception as e:
        logger.warning(f"[FAST-PATH] Error, falling through to Helios: {e}")

    # 1. Pre-fetch relevant personal context from RAG (skip for greetings)
    personal_context = ""
    if not is_greeting(user_text):
        personal_context = rag_context(user_text)
        if personal_context:
            logger.info(f"[HYBRID] Pre-fetched RAG context ({len(personal_context)} chars)")
            routing_info["rag_prefetch"] = True

    # 2. Build Helios system prompt with personal context + mode
    helios_system = get_helios_system_prompt(personal_context, mode=intent.mode, intensity=intent.intensity)

    # 3. Check if Helios is available, start if needed
    if not await check_helios_health():
        logger.info("[HYBRID] Helios offline, attempting to start...")
        started = await start_helios()
        if not started:
            # Fallback to Nemotron-only mode
            logger.warning("[HYBRID] Helios unavailable, falling back to Nemotron")
            routing_info["fallback"] = "nemotron"
            return await _nemotron_fallback(messages, stream, routing_info,
                                            mode=intent.mode, intensity=intent.intensity)

    # 4. Call Helios
    global _last_helios_request
    logger.info("[HYBRID] Calling Helios...")
    try:
        helios_resp = await call_model(
            HELIOS_URL, HELIOS_MODEL, messages,
            system=helios_system,
            tools=HELIOS_TOOLS,
            timeout=180  # Helios can be slow
        )
        _last_helios_request = time.time()  # Track for auto-shutdown
    except Exception as e:
        logger.error(f"[HYBRID] Helios call failed: {e}")
        # Fallback to Nemotron
        routing_info["fallback"] = "nemotron"
        routing_info["helios_error"] = str(e)
        return await _nemotron_fallback(messages, stream, routing_info,
                                        mode=intent.mode, intensity=intent.intensity)

    # 5. Check for tool calls (ask_orchestrator)
    choice = helios_resp.get("choices", [{}])[0]
    message = choice.get("message", {})
    tool_calls = message.get("tool_calls", [])
    content = message.get("content") or ""

    # Parse tool calls from content if needed
    if not tool_calls and content:
        tool_calls = parse_tool_calls_from_content(content)

    # 6. If no tool calls, return Helios response directly
    if not tool_calls:
        logger.info("[HYBRID] Helios responded directly (no orchestrator needed)")
        routing_info["helios_direct"] = True

        if stream:
            return _stream_text_response(clean_response(content), HELIOS_MODEL)
        else:
            if content:
                message["content"] = clean_response(content)
            helios_resp["_routing"] = routing_info
            return JSONResponse(helios_resp)

    # 7. Execute ask_orchestrator via Nemotron
    logger.info(f"[HYBRID] Helios called orchestrator, delegating to Nemotron")
    conversation = messages.copy()

    for tool_call in tool_calls:
        function = tool_call.get("function", {})
        tool_name = function.get("name", "")

        if tool_name != "ask_orchestrator":
            logger.warning(f"[HYBRID] Unexpected tool from Helios: {tool_name}")
            continue

        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = {}

        command = arguments.get("command", "")
        if not command:
            continue

        logger.info(f"[HYBRID] Orchestrator command: {command[:100]}...")
        routing_info["tool_calls"].append({"tool": "ask_orchestrator", "command": command})

        # Send to Nemotron orchestrator
        orchestrator_result = await call_nemotron_orchestrator(command)
        logger.info(f"[HYBRID] Orchestrator result: {orchestrator_result[:200]}...")

        # Add to conversation for Helios follow-up
        conversation.append({
            "role": "assistant",
            "content": f"I used the orchestrator to: {command}"
        })
        conversation.append({
            "role": "user",
            "content": f"Orchestrator result: {orchestrator_result}\n\nPlease respond naturally to me based on this result. Keep it brief and conversational."
        })

    # 8. Get final response from Helios
    logger.info("[HYBRID] Getting final response from Helios...")
    try:
        final_resp = await call_model(
            HELIOS_URL, HELIOS_MODEL, conversation,
            system=helios_system,
            timeout=120
        )
        _last_helios_request = time.time()  # Track for auto-shutdown
    except Exception as e:
        logger.error(f"[HYBRID] Helios final response failed: {e}")
        # Return the orchestrator result directly
        if stream:
            return _stream_text_response(orchestrator_result, NEMOTRON_MODEL)
        return JSONResponse({
            "choices": [{"message": {"role": "assistant", "content": orchestrator_result}}],
            "_routing": routing_info,
        })

    final_content = final_resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    final_content = clean_response(final_content)

    if stream:
        return _stream_text_response(final_content, HELIOS_MODEL)

    final_resp["_routing"] = routing_info
    if final_content:
        final_resp["choices"][0]["message"]["content"] = final_content
    return JSONResponse(final_resp)


def _stream_text_response(text: str, model: str):
    """Helper to stream a text response in SSE format."""
    chunk_id = f"chatcmpl-{int(time.time())}"

    async def generate():
        chunk_size = 80
        for i in range(0, len(text), chunk_size):
            chunk_text = text[i:i+chunk_size]
            chunk_data = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": chunk_text},
                    "finish_reason": None
                }]
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
        # Final chunk
        final_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def _nemotron_fallback(messages: List[Dict], stream: bool, routing_info: Dict,
                             mode: str = "explainer", intensity: str = "low"):
    """Fallback to Nemotron-only mode when Helios is unavailable."""
    logger.info("[FALLBACK] Using Nemotron-only mode")

    conversation = messages.copy()
    system_prompt = get_orchestrator_system_prompt(mode=mode, intensity=intensity)
    result = await _run_nemotron_tool_loop(conversation, system_prompt, label="FALLBACK")

    if stream:
        return _stream_text_response(result, NEMOTRON_MODEL)
    return JSONResponse({
        "choices": [{"message": {"role": "assistant", "content": result}}],
        "_routing": routing_info,
    })


@app.post("/api/memory/add")
async def add_memory(req: Request):
    """Add a memory to RAG."""
    body = await req.json()
    text = body.get("text", "").strip()
    category = body.get("category", "general")
    source = body.get("source", "manual")
    tags = body.get("tags", [])

    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)

    doc_id = f"{category}_{datetime.now().timestamp()}"

    metadata = {
        "category": category,
        "source": source,
        "kind": "chunk",
        "created_at": datetime.now().isoformat(),
    }
    # ChromaDB metadata supports only scalars; store tags as comma-separated string
    if tags and isinstance(tags, list):
        metadata["tags"] = ",".join(str(t) for t in tags)

    collection.add(
        documents=[text],
        metadatas=[metadata],
        ids=[doc_id],
    )

    return JSONResponse({"ok": True, "id": doc_id})


@app.get("/api/memory/search")
async def search_memory(query: str, n: int = 5):
    """Search RAG memory."""
    context = rag_context(query)
    return JSONResponse({"query": query, "results": context})


@app.get("/api/memory/stats")
def memory_stats():
    """Get RAG statistics."""
    return JSONResponse({
        "collection": CHROMA_COLLECTION,
        "total_documents": collection.count(),
        "persist_path": CHROMA_PERSIST,
    })


# =============================================================================
# Reminder System Endpoints
# =============================================================================

@app.post("/api/reminder/trigger")
async def trigger_reminder(req: Request):
    """
    Manually trigger a reminder (for testing or legacy HA automation callbacks).

    Request body:
    {
        "reminder_id": "abc12345"
    }
    """
    try:
        body = await req.json()
    except:
        body = {}

    reminder_id = body.get("reminder_id")
    if not reminder_id:
        return JSONResponse({"error": "Missing reminder_id"}, status_code=400)

    logger.info(f"[REMINDER] Manual trigger: {reminder_id}")

    # Use the same delivery function as APScheduler
    await deliver_reminder_job(reminder_id)

    return JSONResponse({"success": True, "reminder_id": reminder_id})


@app.get("/api/reminders")
async def get_reminders_api():
    """List all pending reminders with scheduler status."""
    pending = list_pending_reminders()

    # Enrich with scheduler info
    scheduled_job_ids = {job.id for job in scheduler.get_jobs()}
    for reminder in pending:
        job_id = f"reminder_{reminder.get('id')}"
        reminder["scheduled"] = job_id in scheduled_job_ids

    return JSONResponse({
        "count": len(pending),
        "scheduler_jobs": len(scheduled_job_ids),
        "reminders": pending
    })


@app.post("/api/reminder/complete/{reminder_id}")
async def complete_reminder_api(reminder_id: str):
    """Mark a reminder as completed (triggered)."""
    success = mark_reminder_completed(reminder_id)
    if success:
        return JSONResponse({"success": True, "reminder_id": reminder_id})
    return JSONResponse({"error": "Reminder not found"}, status_code=404)


# =============================================================================
# Focus Timer (Pomodoro) Endpoints
# =============================================================================

@app.get("/api/focus")
async def get_focus_status_api():
    """Get current focus timer status (for dashboards/widgets)."""
    if not current_focus_session["active"]:
        return JSONResponse({
            "active": False,
            "task": None,
            "elapsed_minutes": None,
            "remaining_minutes": None,
            "duration": None,
            "break_duration": None,
            "started": None,
        })

    elapsed = (datetime.now() - current_focus_session["started"]).total_seconds() / 60
    remaining = current_focus_session["duration"] - elapsed

    return JSONResponse({
        "active": True,
        "task": current_focus_session["task"],
        "elapsed_minutes": round(elapsed, 1),
        "remaining_minutes": round(max(0, remaining), 1),
        "duration": current_focus_session["duration"],
        "break_duration": current_focus_session["break_duration"],
        "started": current_focus_session["started"].isoformat(),
    })


@app.post("/api/focus/start")
async def start_focus_api(req: Request):
    """
    Start a focus timer via REST API.

    Request body:
    {
        "task": "writing report",
        "duration": 25,
        "break_duration": 5,
        "speaker": "office",  // optional: speaker for Endel audio
        "soundscape": "focus"  // optional: focus, deeper-focus, study, colored-noises, none
    }
    """
    try:
        body = await req.json()
    except:
        body = {}

    task = body.get("task", "focus session")
    duration = body.get("duration", 25)
    break_duration = body.get("break_duration", 5)
    speaker = body.get("speaker")
    soundscape = body.get("soundscape", "focus")

    result = await tool_start_focus(task, duration, break_duration, speaker, soundscape)
    return JSONResponse({
        "success": current_focus_session["active"],
        "message": result,
        "task": task,
        "duration": duration,
        "break_duration": break_duration,
        "speaker": speaker,
        "soundscape": soundscape,
        "audio_player": current_focus_session.get("audio_player"),
    })


@app.post("/api/focus/stop")
async def stop_focus_api():
    """Stop the current focus timer via REST API."""
    result = await tool_stop_focus()
    return JSONResponse({
        "success": True,
        "message": result,
    })


# =============================================================================
# Audio serving for reminder TTS
# =============================================================================

from fastapi.responses import FileResponse

@app.get("/api/audio/{filename}")
async def serve_audio(filename: str):
    """Serve audio files from /tmp/brain_audio/."""
    filepath = f"/tmp/brain_audio/{filename}"
    if os.path.exists(filepath):
        return FileResponse(filepath, media_type="audio/wav")
    return JSONResponse({"error": "Audio file not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
