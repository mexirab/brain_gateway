"""
Brain Gateway Orchestrator v5
- Nemotron-Orchestrator-8B is the BRAIN with tool-calling
- Tools: home_assistant, search_memory, ask_expert (Helios-120B)
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)
"""

import os
import re
import json
import logging
import time
from typing import Any, Dict, List, Optional
from datetime import datetime
from sentence_transformers import SentenceTransformer
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import chromadb
from chromadb.config import Settings

# Import the HA integration module
from ha_integration import HomeAssistantClient, ExecutionResult

# Import the data manager for structured data updates
from data_manager import handle_update_data

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# Model endpoints and names
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "http://10.0.0.173:8001/v1")
NEMOTRON_MODEL = os.environ.get("NEMOTRON_MODEL", "nvidia/Nemotron-Orchestrator-8B")
HELIOS_URL = os.environ.get("HELIOS_URL", "http://10.0.0.195:8080/v1")
HELIOS_MODEL = os.environ.get("HELIOS_MODEL", "unsloth_gpt-oss-120b-GGUF_Q4_K_S_gpt-oss-120b-Q4_K_S-00001-of-00002.gguf")

# Home Assistant
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# RAG settings
CHROMA_PERSIST = os.environ.get("CHROMA_PERSIST", "/home/nadim/.local/share/chroma/personal_rag")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "nadim_rag")
MIN_COS = float(os.environ.get("MIN_COS", "0.30"))
TOP_K = int(os.environ.get("TOP_K", "6"))

# Agentic settings
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "5"))  # Prevent infinite loops

app = FastAPI(title="Brain Gateway", version="5.0")


# =============================================================================
# TOOLS DEFINITION - These are the capabilities Nemotron can use
# =============================================================================

def get_ha_tool_definition() -> Dict[str, Any]:
    """Build the home_assistant tool with current entity list."""
    entity_lines = []
    for domain in ["light", "switch", "fan", "climate", "cover", "scene", "lock"]:
        entities = ha_client.get_entities_by_domain(domain)
        for e in entities:
            entity_lines.append(f"  - {e.entity_id} ({e.friendly_name})")

    entity_list = "\n".join(entity_lines[:60]) if entity_lines else "  (entities loading...)"

    return {
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


# Static tools (non-HA)
STATIC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search Nadim's personal knowledge base for relevant context. Use this when the user asks about personal information, projects, routines, preferences, medications, schedules, or anything that might be in their notes.",
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
            "description": "Delegate to the expert model (Helios 120B) for ANY question requiring knowledge or reasoning. Use this for: general knowledge (books, movies, history, science), coding, analysis, explanations, factual questions, creative writing, or ANYTHING you're not certain about. The expert has broad knowledge - use it liberally.",
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
            "description": "Update Nadim's structured personal data (medications, projects). Use this when he asks to add, remove, or modify medications or project information.",
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
    }
]


def get_orchestrator_tools() -> List[Dict[str, Any]]:
    """Get all tools including dynamic HA tool with entity list."""
    return [get_ha_tool_definition()] + STATIC_TOOLS


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
        # Skip very short chunks (likely just headers)
        if len(doc.strip()) < MIN_CHUNK_LEN:
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

async def call_model(url: str, model: str, messages: List[Dict], system: str = "", tools: List = None, timeout: int = 180) -> Dict[str, Any]:
    """Call an LLM endpoint."""
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})

    payload = {
        "model": model,
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    # Pass through tools for HA native device control
    # Use tool_choice: "none" to let Nemotron output tool calls in content
    # (vLLM requires --enable-auto-tool-choice flag for native tool calling)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "none"

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{url}/chat/completions", json=payload)
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

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", f"{url}/chat/completions", json=payload) as response:
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


async def check_helios_health() -> bool:
    """Check if Helios is running and responsive."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{HELIOS_URL.replace('/v1', '')}/health")
            return r.status_code == 200
    except:
        return False


async def start_helios() -> bool:
    """Start Helios via SSH (paramiko) and wait for it to be ready."""
    import asyncio
    import paramiko

    logger.info("[EXPERT] Helios is offline, attempting to start...")

    # Start the service via SSH using paramiko
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname="10.0.0.195",
            username="labadmin",
            key_filename="/root/.ssh/id_ed25519",  # Mounted from host
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


async def tool_ask_expert(question: str, context: str = "") -> str:
    """Delegate a complex question to Helios 120B. Auto-starts if offline."""
    if not question:
        return "No question provided"

    logger.info(f"[EXPERT] Delegating to Helios: {question[:100]}...")

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


def get_orchestrator_system_prompt() -> str:
    """System prompt for Nemotron as the orchestrating brain."""
    return """You are Nadim's personal AI assistant and orchestrator. You have access to tools to help answer questions and perform actions.

AVAILABLE TOOLS:
1. home_assistant - Control smart home devices (lights, switches, thermostats, media, scenes)
2. search_memory - Search Nadim's personal notes for context (projects, routines, preferences, medications)
3. ask_expert - Delegate to the expert model (120B) for: general knowledge, books, movies, history, science, coding, analysis, or ANY question you're unsure about
4. update_data - Update Nadim's personal data (medications, projects). Use when he asks to add, remove, or change medications or project info.

ROUTING RULES:
- home_assistant: ONLY when user EXPLICITLY asks to control devices (turn on/off, lights, fan, temperature, etc.)
- search_memory: ONLY for Nadim's personal info (projects, routines, preferences, medications, schedules)
- update_data: When user wants to ADD, REMOVE, or UPDATE medications or projects
- ask_expert: Use for EVERYTHING ELSE - general knowledge, books, movies, coding, analysis, explanations, or anything you're not 100% sure about

CRITICAL:
- You are a small model - when in doubt, use ask_expert
- For ANY general knowledge question (books, history, science, etc.) → ask_expert
- NEVER say "I don't have access to that information" - use ask_expert instead
- After getting tool results, respond naturally - don't call more tools
- Be direct and concise (Nadim has ADHD)

EXAMPLES:
- "Turn off the bedroom lights" → home_assistant
- "What projects am I working on?" → search_memory
- "Summarize the Stormlight Archive" → ask_expert (general knowledge)
- "Write a Python function" → ask_expert (coding)
- "What's the capital of France?" → ask_expert (general knowledge)
- "Explain quantum computing" → ask_expert (complex topic)
- "Add Adderall 20mg to my morning meds" → update_data(action="add_medication", name="Adderall", dose="20mg", schedule="morning")
- "Remove Wellbutrin" → update_data(action="remove_medication", name="Wellbutrin")
- "Change Vyvanse to 50mg" → update_data(action="update_medication", name="Vyvanse", dose="50mg")
- "Mark voice interface complete on Brain Gateway" → update_data(action="complete_step", name="Brain Gateway", step="voice interface")
- "Add a project to build a deck" → update_data(action="add_project", name="Build a deck")"""


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    # Load HA entities at startup
    print("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    print(f"[orchestrator] Loaded {count} HA entities")


@app.get("/health")
async def health():
    """Health check endpoint."""
    # Check Helios status
    helios_online = await check_helios_health()

    return {
        "ok": True,
        "version": "5.1",
        "architecture": "agentic",
        "brain": f"{NEMOTRON_URL} ({NEMOTRON_MODEL})",
        "expert": f"{HELIOS_URL} ({HELIOS_MODEL})",
        "expert_status": "online" if helios_online else "offline (auto-starts on demand)",
        "tools": ["home_assistant", "search_memory", "ask_expert", "update_data"],
        "rag_collection": CHROMA_COLLECTION,
        "rag_docs": collection.count(),
        "ha_entities": len(ha_client._entities),
    }


@app.get("/v1/models")
def list_models():
    """List available models."""
    return {
        "data": [
            {"id": "brain", "object": "model"},
            {"id": "brain-orchestrator", "object": "model"},
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
    Main chat endpoint with agentic tool-calling and streaming support.

    Nemotron-Orchestrator is the brain that decides when to use tools:
    - home_assistant: Control smart home devices
    - search_memory: Query personal knowledge base (RAG)
    - ask_expert: Delegate complex tasks to Helios 120B

    Streaming: When stream=true, tool-calling rounds run normally,
    then the final response is streamed to the client.
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
        "tool_calls": [],
        "rounds": 0,
        "streaming": stream,
    }

    # If external tools are provided (e.g., from HA voice pipeline),
    # pass through to Nemotron and let it handle natively
    if external_tools:
        logger.info(f"[ORCHESTRATOR] External tools provided ({len(external_tools)}), passing through")
        routing_info["mode"] = "passthrough"
        try:
            llm_resp = await call_model(
                NEMOTRON_URL, NEMOTRON_MODEL, messages,
                system=get_orchestrator_system_prompt(),
                tools=external_tools,
                timeout=60
            )
            llm_resp["_routing"] = routing_info
            return JSONResponse(llm_resp)
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Passthrough failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=503)

    # Agentic mode: Nemotron orchestrates with our tools
    routing_info["mode"] = "agentic"
    logger.info(f"[ORCHESTRATOR] Starting agentic loop for: {user_text[:100]}... (stream={stream})")

    # Build conversation with system prompt
    conversation = messages.copy()
    system_prompt = get_orchestrator_system_prompt()

    # Agentic loop: let Nemotron call tools until it responds with content
    for round_num in range(MAX_TOOL_ROUNDS):
        routing_info["rounds"] = round_num + 1
        logger.info(f"[ORCHESTRATOR] Round {round_num + 1}/{MAX_TOOL_ROUNDS}")

        try:
            llm_resp = await call_model(
                NEMOTRON_URL, NEMOTRON_MODEL, conversation,
                system=system_prompt,
                tools=get_orchestrator_tools(),
                timeout=60
            )
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Nemotron call failed: {e}")
            error_response = {"error": f"Orchestrator failed: {e}", "_routing": routing_info}
            if stream:
                # Return error as streaming format
                async def error_stream():
                    yield f"data: {json.dumps({'error': str(e)})}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_stream(), media_type="text/event-stream")
            return JSONResponse(error_response, status_code=503)

        # Extract the assistant's response
        choice = llm_resp.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        content = message.get("content") or ""

        # Nemotron outputs tool calls as <tool_call> tags in content
        # Parse them if no native tool_calls were returned
        if not tool_calls and content:
            tool_calls = parse_tool_calls_from_content(content)
            if tool_calls:
                logger.info(f"[ORCHESTRATOR] Parsed {len(tool_calls)} tool call(s) from content")

        # If no tool calls, we're done - return the response (streaming or not)
        if not tool_calls:
            logger.info(f"[ORCHESTRATOR] Final response (no tool calls, stream={stream})")

            if stream:
                # For true streaming, make a fresh streaming call to Nemotron
                # This gives real token-by-token output instead of fake chunking
                logger.info(f"[ORCHESTRATOR] Starting streaming response")
                return StreamingResponse(
                    stream_final_response(
                        NEMOTRON_URL, NEMOTRON_MODEL, conversation,
                        system=system_prompt, timeout=120
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    }
                )
            else:
                # Non-streaming response
                if content:
                    message["content"] = clean_response(content)
                llm_resp["_routing"] = routing_info
                return JSONResponse(llm_resp)

        # Process tool calls
        logger.info(f"[ORCHESTRATOR] Processing {len(tool_calls)} tool call(s)")

        # Add assistant message to conversation (keep original content with tool_call tags)
        conversation.append({"role": "assistant", "content": content})

        # Execute each tool and collect results
        tool_results = []
        expert_response = None  # Track if ask_expert was called

        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            tool_name = function.get("name", "")

            # Parse arguments
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {}

            # Execute the tool
            result = await execute_tool(tool_name, arguments)

            # Track for debugging
            routing_info["tool_calls"].append({
                "tool": tool_name,
                "args": arguments,
                "result_preview": result[:200] if result else None,
            })

            tool_results.append(f"[{tool_name}] {result}")

            # If ask_expert was called, save the response for potential short-circuit
            if tool_name == "ask_expert":
                expert_response = result

        # SHORT-CIRCUIT: If only ask_expert was called, return its response directly
        # This saves a round-trip to Nemotron and speeds up general knowledge queries
        if len(tool_calls) == 1 and expert_response:
            logger.info(f"[ORCHESTRATOR] Short-circuit: returning expert response directly")
            routing_info["short_circuit"] = True

            if stream:
                # Stream the expert response
                chunk_id = f"chatcmpl-{int(time.time())}"
                async def expert_stream():
                    chunk_size = 15
                    for i in range(0, len(expert_response), chunk_size):
                        chunk_text = expert_response[i:i+chunk_size]
                        chunk_data = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": NEMOTRON_MODEL,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": chunk_text},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk_data)}\n\n"
                    final_chunk = {
                        "id": chunk_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": NEMOTRON_MODEL,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(
                    expert_stream(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
                )
            else:
                return JSONResponse({
                    "choices": [{
                        "message": {"role": "assistant", "content": expert_response}
                    }],
                    "_routing": routing_info,
                })

        # Add tool results as a user message (Nemotron understands this format)
        # Include instruction to respond naturally without more tool calls
        results_text = "\n".join(tool_results)
        conversation.append({
            "role": "user",
            "content": f"<tool_response>\n{results_text}\n</tool_response>\n\nThe tool has completed. Please provide a brief, natural response to the user based on the result above. Do NOT call any more tools."
        })

    # If we exit the loop, we hit max rounds
    logger.warning(f"[ORCHESTRATOR] Hit max tool rounds ({MAX_TOOL_ROUNDS})")
    error_msg = "I apologize, but I wasn't able to complete the request within the allowed number of steps. Please try rephrasing or breaking down your request."

    if stream:
        async def max_rounds_stream():
            yield f"data: {json.dumps({'choices': [{'delta': {'content': error_msg}, 'index': 0}]})}\n\n"
            yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop', 'index': 0}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(max_rounds_stream(), media_type="text/event-stream")

    return JSONResponse({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": error_msg
            }
        }],
        "_routing": routing_info,
    })


@app.post("/api/memory/add")
async def add_memory(req: Request):
    """Add a memory to RAG."""
    body = await req.json()
    text = body.get("text", "").strip()
    category = body.get("category", "general")
    source = body.get("source", "manual")
    
    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)
    
    doc_id = f"{category}_{datetime.now().timestamp()}"
    
    collection.add(
        documents=[text],
        metadatas=[{
            "category": category,
            "source": source,
            "kind": "chunk",
            "created_at": datetime.now().isoformat(),
        }],
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
# Morning Briefing Endpoint (Phase 4: Proactive Reminders)
# =============================================================================

TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.173:8002")
TTS_VOICE = os.environ.get("TTS_VOICE", "jessica")

# Store generated audio files for serving to speakers
import uuid
from fastapi.responses import FileResponse
audio_cache = {}  # id -> filepath

@app.get("/api/audio/{audio_id}.wav")
async def serve_audio(audio_id: str):
    """Serve generated audio files for playback on speakers."""
    if audio_id in audio_cache:
        filepath = audio_cache[audio_id]
        if os.path.exists(filepath):
            return FileResponse(filepath, media_type="audio/wav")
    return JSONResponse({"error": "Audio not found"}, status_code=404)

@app.post("/api/briefing/morning")
async def morning_briefing(req: Request):
    """
    Generate a personalized morning briefing.

    Searches RAG for morning routine, meds, and calendar info,
    then generates a friendly reminder via Nemotron.

    Optional: generate TTS audio and/or announce on HA speaker.

    Request body (all optional):
    {
        "generate_tts": true,
        "play_on": "media_player.kitchen_display",
        "include_calendar": true
    }
    """
    try:
        body = await req.json() if await req.body() else {}
    except:
        body = {}

    generate_tts = body.get("generate_tts", False)
    play_on = body.get("play_on", None)

    # Get current context
    now = datetime.now()
    day_name = now.strftime("%A")
    time_str = now.strftime("%-I:%M %p")
    date_str = now.strftime("%B %-d")

    # Search RAG for relevant content
    morning_context = rag_context("morning routine wake up meds")
    meds_context = rag_context("medications daily schedule")

    # Build the prompt
    system_prompt = """You are Jessica, Nadim's supportive ADHD coach assistant.
You speak with warmth, energy, and encouragement - like a caring friend who understands ADHD.
Keep responses concise (2-4 sentences for the greeting, then a few key reminders).
Use a conversational, upbeat tone. Celebrate small wins. Be specific and actionable.

IMPORTANT: This will be spoken aloud via text-to-speech, so:
- Do NOT use emojis, markdown, asterisks, or special formatting
- Do NOT include thinking tags or internal reasoning
- Write in plain, natural spoken English
- Use natural pauses with commas and periods"""

    user_prompt = f"""It's {day_name}, {date_str} at {time_str}. Generate a morning briefing for Nadim.

Based on his routines and preferences:
{morning_context}

Medication info:
{meds_context}

Create a brief, encouraging wake-up message that:
1. Greets him warmly for the day
2. Reminds about morning meds (Vyvanse 70mg and Wellbutrin 300mg with breakfast, protein, no citrus)
3. Lists 3-4 key morning routine steps
4. Ends with an encouraging note

Keep it natural and conversational - this will be spoken aloud."""

    # Generate the briefing via Nemotron
    messages = [{"role": "user", "content": user_prompt}]

    try:
        response = await call_model(
            url=NEMOTRON_URL,
            model=NEMOTRON_MODEL,
            messages=messages,
            system=system_prompt,
            timeout=60
        )
        briefing_text = response.get("choices", [{}])[0].get("message", {}).get("content", "Good morning! Time to start your day.")
        # Clean up any thinking tags or markdown for TTS
        briefing_text = re.sub(r'<think>.*?</think>', '', briefing_text, flags=re.DOTALL)
        briefing_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', briefing_text)  # Remove bold
        briefing_text = re.sub(r'\*([^*]+)\*', r'\1', briefing_text)  # Remove italic
        briefing_text = re.sub(r'[🌞🚀💪✅❌📋🎯]+', '', briefing_text)  # Remove common emojis
        briefing_text = briefing_text.strip()

        # Add pauses between sentences for more natural TTS pacing
        # Replace period-space with period-ellipsis-space for longer pauses
        briefing_text = re.sub(r'\.\s+', '... ', briefing_text)
        briefing_text = re.sub(r'!\s+', '!... ', briefing_text)
        briefing_text = re.sub(r'\?\s+', '?... ', briefing_text)
    except Exception as e:
        logger.error(f"Failed to generate briefing: {e}")
        briefing_text = f"Good morning Nadim! It's {day_name}. Remember to take your morning meds with breakfast - Vyvanse and Wellbutrin. Have a great day!"

    result = {
        "day": day_name,
        "date": date_str,
        "time": time_str,
        "briefing": briefing_text,
    }

    # Optionally generate TTS
    if generate_tts or play_on:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                tts_response = await client.post(
                    f"{TTS_URL}/tts",
                    json={"text": briefing_text, "voice": TTS_VOICE}
                )
                if tts_response.status_code == 200:
                    # Save audio with UUID for serving
                    audio_id = str(uuid.uuid4())[:8]
                    audio_dir = "/tmp/brain_audio"
                    os.makedirs(audio_dir, exist_ok=True)
                    audio_path = f"{audio_dir}/{audio_id}.wav"
                    with open(audio_path, "wb") as f:
                        f.write(tts_response.content)
                    audio_cache[audio_id] = audio_path
                    result["audio_file"] = audio_path
                    result["audio_id"] = audio_id
                    result["audio_url"] = f"http://10.0.0.186:8888/api/audio/{audio_id}.wav"
                    result["tts_generated"] = True
        except Exception as e:
            logger.error(f"TTS generation failed: {e}")
            result["tts_error"] = str(e)

    # Optionally play on HA media player using Jessica's voice
    if play_on and result.get("audio_url"):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Use media_player.play_media to play our custom audio
                ha_response = await client.post(
                    f"{HA_URL}/api/services/media_player/play_media",
                    headers={"Authorization": f"Bearer {HA_TOKEN}"},
                    json={
                        "entity_id": play_on,
                        "media_content_id": result["audio_url"],
                        "media_content_type": "audio/wav"
                    }
                )
                if ha_response.status_code == 200:
                    result["announced_on"] = play_on
                    result["voice"] = "jessica"
                else:
                    result["announce_error"] = f"HA returned {ha_response.status_code}"
        except Exception as e:
            logger.error(f"HA announcement failed: {e}")
            result["announce_error"] = str(e)

    return JSONResponse(result)


@app.get("/api/briefing/morning")
async def morning_briefing_get():
    """GET version for easy testing."""
    from starlette.requests import Request as StarletteRequest
    from starlette.testclient import TestClient
    # Simple redirect to POST with empty body
    return await morning_briefing(Request(scope={"type": "http", "method": "POST"}, receive=lambda: None))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
