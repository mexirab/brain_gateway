# Brain Gateway

Personal AI assistant with Nemotron-Orchestrator-8B as the brain, agentic tool-calling, Home Assistant integration, and RAG-based memory.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│   Open WebUI    │────▶│   Orchestrator   │────▶│  Nemotron (8B)   │
│   (Frontend)    │     │   (v5 Agentic)   │     │   THE BRAIN      │
└─────────────────┘     └────────┬─────────┘     └────────┬─────────┘
                                 │                        │
                                 │              ┌─────────┴─────────┐
                                 │              │    TOOL CALLS     │
                    ┌────────────┼──────────────┼───────────────────┤
                    ▼            ▼              ▼                   ▼
              ┌──────────┐ ┌──────────┐  ┌──────────┐       ┌──────────┐
              │ ChromaDB │ │   Home   │  │  Helios  │       │  LiteLLM │
              │   (RAG)  │ │Assistant │  │  (120B)  │       │  (Proxy) │
              │          │ │   API    │  │  Expert  │       │          │
              └──────────┘ └──────────┘  └──────────┘       └──────────┘
```

## Components

### Orchestrator (`/orchestrator`)

**Nemotron-Orchestrator-8B is the brain** with agentic tool-calling:

| Tool | Purpose |
|------|---------|
| `home_assistant` | Control smart home - Nemotron outputs structured API calls (`entity_id`, `service`, `data`) |
| `search_memory` | Query personal RAG knowledge base |
| `ask_expert` | Delegate complex reasoning/coding to Helios 120B |

Nemotron receives the full HA entity list and handles all NLP parsing internally - no regex matching needed.

**Files:**
- `orchestrator.py` - Main FastAPI app (v5.0 agentic)
- `ha_integration.py` - HA entity discovery + thin API relay
- `Dockerfile` - Container build config

### RAG Tools (`/rag`)

Personal knowledge base tools:

| Script | Purpose | Usage |
|--------|---------|-------|
| `ingest_rag.py` | Index documents into ChromaDB | `python ingest_rag.py --source ~/rag/nadim_rag --persist ~/.local/share/chroma/personal_rag --collection nadim_rag` |
| `query_rag.py` | Test RAG queries from CLI | `python query_rag.py --persist ~/.local/share/chroma/personal_rag --collection nadim_rag --q "your question"` |
| `watch_and_ingest.py` | Auto-reindex on file changes | `python watch_and_ingest.py` (runs as daemon) |
| `rag_chat_llamacpp.py` | Standalone RAG chat | `python rag_chat_llamacpp.py --persist ~/.local/share/chroma/personal_rag --collection nadim_rag` |

## Quick Start

### 1. Start the stack

```bash
cd /opt/voyager/gateway_mvp
docker-compose -p brain up -d
```

### 2. Check health

```bash
curl http://localhost:8888/health
```

### 3. Access the UI

Open http://localhost in your browser (Open WebUI)

## Home Assistant Integration

Nemotron has full visibility into your HA entities and outputs structured API calls directly:

```json
{
  "entity_id": "light.bedroom_fan_lights",
  "service": "turn_on",
  "data": {"brightness": 128, "rgb_color": [0, 0, 255]}
}
```

**Example commands:**
- "Turn on the bedroom lights" → `turn_on light.bedroom_fan_lights`
- "Set living room to blue at 50%" → `turn_on light.living_room {brightness: 128, rgb_color: [0,0,255]}`
- "Turn off the kitchen" → `turn_off switch.kitchen`

**Supported services:**
- `light`: turn_on, turn_off, toggle (+ brightness, rgb_color)
- `switch/fan`: turn_on, turn_off, toggle
- `climate`: set_temperature
- `cover`: open_cover, close_cover
- `scene`: turn_on

## Voice Assistant Setup

The orchestrator integrates with Home Assistant's voice pipeline for full voice control with personal context.

### Architecture

```
Voice → Whisper STT → Home Assistant → Brain Gateway Orchestrator
                                               ↓
                                    RAG (personal context)
                                    HA commands (device control)
                                    Smart routing (Nemotron/Helios)
                                               ↓
                                         Piper TTS → Speaker
```

### Setup with Local LLM Conversation Integration

1. **Install the integration** in Home Assistant:
   - Settings → Devices & Services → Add Integration
   - Search for "Local LLM Conversation"

2. **Configure the orchestrator endpoint**:
   - Host: `10.0.0.186` (Voyager)
   - Port: `8888`
   - Model: `brain` (or any name - orchestrator routes automatically)

3. **Set a minimal system prompt** (orchestrator injects its own with RAG context):
   ```
   You are Nadim's personal AI assistant.
   ```
   - Uncheck "Assist" and "Home Assistant Services" APIs (orchestrator handles HA commands)

4. **Configure Voice Assistant pipeline**:
   - Settings → Voice assistants → Add/Edit assistant
   - Speech-to-text: faster-whisper (or your Whisper instance)
   - Conversation agent: Select the orchestrator you just added
   - Text-to-speech: Piper (or your TTS)

### Voice Commands

Voice commands work for both personal queries and home control:

**Personal context (via RAG):**
- "What are my current projects?"
- "What medications do I take?"
- "What's my morning routine?"

**Home control (via HA integration):**
- "Turn on the living room"
- "Turn off the kitchen lights"
- "Set the bedroom to blue at 50%"

**Smart routing:**
- Simple queries → Nemotron 8B (fast, handles most requests)
- Complex questions → Helios 120B via `ask_expert` tool

### Notes

- **HTTPS required for browser mic**: Use the HA mobile app, or enable HTTPS, or set Chrome flag `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
- **Mic works in HA Companion App** without HTTPS configuration

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | System health check |
| `/v1/chat/completions` | POST | Main chat endpoint (OpenAI-compatible) |
| `/v1/models` | GET | List available models |
| `/api/ha/entities` | GET | List all Home Assistant entities |
| `/api/ha/command` | POST | Execute HA command directly |
| `/api/memory/add` | POST | Add memory to RAG |
| `/api/memory/search` | GET | Search RAG memory |
| `/api/memory/stats` | GET | RAG statistics |

## Configuration

### Environment Variables

Set in `docker-compose.yml` or `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `NEMOTRON_URL` | Nemotron LLM endpoint | `http://10.0.0.173:8001/v1` |
| `NEMOTRON_MODEL` | Nemotron model name (for vLLM) | `nvidia/Nemotron-Orchestrator-8B` |
| `HELIOS_URL` | Helios LLM endpoint | `http://10.0.0.195:8080/v1` |
| `HELIOS_MODEL` | Helios model name (for llama.cpp) | `unsloth_gpt-oss-120b-GGUF...` |
| `HA_URL` | Home Assistant URL | `http://10.0.0.106:8123` |
| `HA_TOKEN` | Home Assistant long-lived token | (required) |
| `CHROMA_PERSIST` | ChromaDB storage path | `/chroma/personal_rag` |
| `CHROMA_COLLECTION` | RAG collection name | `nadim_rag` |
| `MIN_COS` | Minimum cosine similarity for RAG results | `0.20` |
| `TOP_K` | Number of RAG candidates to retrieve | `25` |

## File Structure

```
brain_gateway/
├── docker-compose.yml      # Docker stack config
├── litellm-config.yaml     # LiteLLM proxy config
├── .gitignore              # Git ignore rules
├── README.md               # This file
├── orchestrator/
│   ├── Dockerfile          # Orchestrator container
│   ├── orchestrator.py     # v5.0 agentic orchestrator
│   └── ha_integration.py   # HA entity discovery + API relay
└── rag/
    ├── ingest_rag.py       # Document ingestion
    ├── query_rag.py        # CLI query tool
    ├── watch_and_ingest.py # File watcher daemon
    └── rag_chat_llamacpp.py # Standalone RAG chat
```

## RAG Document Structure

Documents are stored in `~/rag/nadim_rag/`:

```
nadim_rag/
├── 00_system/      # System prompts, AI instructions
├── 10_profile/     # Personal info, preferences
├── 20_routines/    # Daily routines, habits
├── 30_projects/    # Project notes
├── 40_relationships/ # People, contacts
└── 90_archive/     # Old/archived content
```

### RAG Features

The RAG system includes several optimizations for better retrieval:

- **Parent header context**: Each chunk includes its parent markdown headers for better semantic matching (e.g., a "Brain Gateway" project chunk also contains "# Current Projects" context)
- **Query normalization**: Strips punctuation, quotes, and lowercases queries for consistent matching
- **Minimum results guarantee**: Always returns at least N results when RAG is triggered, letting the LLM judge relevance
- **Short chunk filtering**: Skips header-only chunks (<100 chars) that lack meaningful content
- **Logging**: RAG queries log search terms, candidate scores, and filtered results for debugging

### Re-indexing RAG

After modifying `ingest_rag.py` or to force re-indexing:

```bash
# Delete existing collection and re-ingest
python3 -c "
import chromadb
from chromadb.config import Settings
chroma = chromadb.PersistentClient(path='$HOME/.local/share/chroma/personal_rag', settings=Settings(anonymized_telemetry=False))
chroma.delete_collection('nadim_rag')
"

# Re-run ingestion
cd /opt/voyager/gateway_mvp/rag
python ingest_rag.py --source ~/rag/nadim_rag --persist ~/.local/share/chroma/personal_rag --collection nadim_rag

# Restart orchestrator to pick up new data
docker-compose -p brain restart orchestrator
```

## Development

### Rebuild after changes

```bash
cd /opt/voyager/gateway_mvp
docker-compose -p brain down
docker-compose -p brain build --no-cache orchestrator
docker-compose -p brain up -d
```

### View logs

```bash
docker logs brain-orchestrator --tail 50 -f
```

### Test HA integration directly

```bash
curl -X POST http://localhost:8888/api/ha/command \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room", "service": "turn_on", "data": {"brightness": 255}}'
```

## Hardware

This system runs on a home lab cluster:

| Node | GPU | Role |
|------|-----|------|
| Helios | RTX 5090 (32GB) | Large model inference (120B) |
| Saturn | RTX 5080 (16GB) | Medium models |
| Uranus | RTX 3080 (10GB) | Nemotron-Orchestrator-8B, Whisper STT |
| Neptune | RTX 3090 (24GB) | Backup inference |
| Voyager | None | Gateway, orchestration |

## License

Private project - Nadim Nabi
