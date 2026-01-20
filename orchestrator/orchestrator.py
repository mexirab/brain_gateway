"""
Brain Gateway Orchestrator v4
- Nemotron-8B decides routing
- Helios-120B for complex tasks
- ChromaDB RAG for personal context
- Home Assistant integration (auto-discovery!)
"""

import os
import re
import json
from typing import Any, Dict, List, Optional
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import chromadb
from chromadb.config import Settings

# Import the new HA integration module
from ha_integration import HomeAssistantClient, ExecutionResult

# Load environment
load_dotenv(os.path.expanduser("~/brain_gateway/.env"))

# Model endpoints
NEMOTRON_URL = os.environ.get("NEMOTRON_URL", "http://10.0.0.173:8001/v1")
HELIOS_URL = os.environ.get("HELIOS_URL", "http://10.0.0.195:8080/v1")

# Home Assistant (now handled by ha_integration module)
HA_URL = os.environ.get("HA_URL", "http://10.0.0.106:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# RAG settings
CHROMA_PERSIST = os.environ.get("CHROMA_PERSIST", "/home/nadim/.local/share/chroma/personal_rag")
CHROMA_COLLECTION = os.environ.get("CHROMA_COLLECTION", "nadim_rag")
MIN_COS = float(os.environ.get("MIN_COS", "0.30"))
TOP_K = int(os.environ.get("TOP_K", "6"))

app = FastAPI(title="Brain Gateway", version="4.0")

# ChromaDB client
chroma = chromadb.PersistentClient(
    path=os.path.expanduser(CHROMA_PERSIST),
    settings=Settings(anonymized_telemetry=False),
)
collection = chroma.get_or_create_collection(CHROMA_COLLECTION)

# Initialize Home Assistant client (auto-discovers entities!)
ha_client = HomeAssistantClient(url=HA_URL, token=HA_TOKEN)

# Routing keywords
CODE_KEYWORDS = ["code", "function", "debug", "python", "javascript", "script", "programming", "algorithm"]
COMPLEX_KEYWORDS = ["analyze", "explain in detail", "help me think", "pros and cons", "comprehensive", "deep dive", "step by step"]
RAG_KEYWORDS = ["medication", "meds", "schedule", "routine", "remember", "last time", "my preference", "usually", "nadim"]

# HA keywords - expanded to catch more commands
HA_KEYWORDS = [
    "turn on", "turn off", "switch on", "switch off", "toggle",
    "lights", "light", "lamp",
    "dim", "brightness", "brighten",
    "play", "pause", "stop", "volume", "music",
    "scene", "activate",
    "thermostat", "temperature", "temp",
    "lock", "unlock",
    "open", "close",
]


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


def detect_intent(text: str) -> Dict[str, Any]:
    """Detect what kind of request this is."""
    t = text.lower()
    word_count = len(text.split())
    
    # Check for HA intent - more comprehensive check
    needs_ha = any(kw in t for kw in HA_KEYWORDS)
    
    return {
        "needs_code": any(kw in t for kw in CODE_KEYWORDS),
        "needs_complex": any(kw in t for kw in COMPLEX_KEYWORDS) or word_count > 100,
        "needs_rag": any(kw in t for kw in RAG_KEYWORDS),
        "needs_ha": needs_ha,
        "is_simple": word_count < 20 and not any([
            any(kw in t for kw in CODE_KEYWORDS),
            any(kw in t for kw in COMPLEX_KEYWORDS),
        ]),
        "word_count": word_count,
    }


def rag_context(query: str) -> str:
    """Query ChromaDB for relevant personal context."""
    if not query.strip():
        return ""
    
    try:
        res = collection.query(
            query_texts=[query],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        print(f"RAG query error: {e}")
        return ""
    
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    dists = res.get("distances", [[]])[0]
    
    chunks = []
    for doc, meta, dist in zip(docs, metas, dists):
        try:
            cos = 1.0 - float(dist)
        except:
            cos = None
        
        if cos is not None and cos < MIN_COS:
            continue
        
        src = ""
        if isinstance(meta, dict):
            src = meta.get("file_path") or meta.get("source") or ""
        
        entry = f"- {doc[:500]}"
        if src:
            entry += f"\n  (source: {src})"
        if cos:
            entry += f" [relevance: {cos:.2f}]"
        chunks.append(entry)
    
    return "\n".join(chunks) if chunks else ""


async def call_model(url: str, messages: List[Dict], system: str = "", timeout: int = 180) -> Dict[str, Any]:
    """Call an LLM endpoint."""
    final_messages = messages.copy()
    if system:
        final_messages.insert(0, {"role": "system", "content": system})
    
    payload = {
        "model": "default",
        "messages": final_messages,
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{url}/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()


def should_route_to_helios(intent: Dict[str, Any], user_text: str) -> bool:
    """Decide if this request needs Helios (120B) or can be handled by Nemotron (8B)."""
    
    # Always route to Helios for:
    if intent["needs_complex"]:
        return True
    if intent["needs_code"]:
        return True
    if intent["word_count"] > 50:
        return True
    
    # Questions that need deep reasoning
    deep_patterns = [
        r"\bwhy\b.*\?",
        r"\bhow\s+(?:do|does|can|could|would|should)\b",
        r"\bexplain\b",
        r"\bcompare\b",
        r"\bwhat\s+(?:are|is)\s+the\s+(?:best|difference|pros|cons)\b",
    ]
    for pattern in deep_patterns:
        if re.search(pattern, user_text.lower()):
            return True
    
    return False


def clean_response(text: str) -> str:
    """Remove <think> tags from Nemotron responses."""
    # Remove think blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    return text.strip()


def build_system_prompt(rag_context: str, ha_result: str, routed_to: str) -> str:
    """Build the system prompt with context."""
    
    parts = [
        "You are Nadim's personal AI assistant.",
        "",
        "RULES:",
        "- Be direct and action-oriented (Nadim has ADHD)",
        "- Break complex tasks into clear steps",
        "- If you executed a home action, confirm it first",
        "- Use personal context when relevant",
        "- Don't overwhelm with options - suggest ONE next step",
    ]
    
    if rag_context:
        parts.extend([
            "",
            "PERSONAL CONTEXT (from Nadim's notes):",
            rag_context,
        ])
    
    if ha_result:
        parts.extend([
            "",
            f"HOME AUTOMATION: {ha_result}",
        ])
    
    return "\n".join(parts)


@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    # Load HA entities at startup
    print("[orchestrator] Loading Home Assistant entities...")
    count = await ha_client.refresh_entities()
    print(f"[orchestrator] Loaded {count} HA entities")


@app.get("/health")
def health():
    """Health check endpoint."""
    return {
        "ok": True,
        "version": "4.0",
        "nemotron": NEMOTRON_URL,
        "helios": HELIOS_URL,
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
    """Main chat endpoint with intelligent routing."""
    body = await req.json()
    messages = body.get("messages", [])
    user_text = last_user_text(messages)
    
    # Track what we did
    routing_info = {
        "timestamp": datetime.now().isoformat(),
        "user_query_length": len(user_text),
    }
    
    # Step 1: Detect intent
    intent = detect_intent(user_text)
    routing_info["intent"] = intent
    
    # Step 2: Check for Home Assistant action (using new smart module!)
    ha_result = ""
    if intent["needs_ha"]:
        result = await ha_client.execute_command(user_text)
        if result.success:
            ha_result = result.message
        else:
            # Only show error if it seemed like an HA command
            if ha_client.parse_command(user_text):
                ha_result = f"⚠ {result.message}"
        
        routing_info["ha_attempted"] = True
        routing_info["ha_result"] = ha_result
    
    # Step 3: Get RAG context if needed
    rag = ""
    if intent["needs_rag"] or intent["needs_complex"]:
        rag = rag_context(user_text)
        routing_info["rag_retrieved"] = bool(rag)
    
    # Step 4: Decide routing
    use_helios = should_route_to_helios(intent, user_text)
    target_url = HELIOS_URL if use_helios else NEMOTRON_URL
    routing_info["routed_to"] = "helios" if use_helios else "nemotron"
    
    # Step 5: Build system prompt
    system = build_system_prompt(rag, ha_result, routing_info["routed_to"])
    
    # Step 6: Call the model
    try:
        llm_resp = await call_model(target_url, messages, system, timeout=300 if use_helios else 60)
    except Exception as e:
        # Fallback: if primary fails, try the other
        fallback_url = NEMOTRON_URL if use_helios else HELIOS_URL
        routing_info["fallback"] = True
        try:
            llm_resp = await call_model(fallback_url, messages, system, timeout=180)
        except Exception as e2:
            return JSONResponse({
                "error": f"Both models failed: {e}, {e2}",
                "_routing": routing_info,
            }, status_code=503)
    
    # Step 7: Clean response (remove <think> tags if from Nemotron)
    if not use_helios and "choices" in llm_resp:
        for choice in llm_resp.get("choices", []):
            if "message" in choice and "content" in choice["message"]:
                choice["message"]["content"] = clean_response(choice["message"]["content"])
    
    # Add routing info for debugging (can be removed in production)
    llm_resp["_routing"] = routing_info
    
    return JSONResponse(llm_resp)


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
