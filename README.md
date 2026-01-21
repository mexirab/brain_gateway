# Brain Gateway 🧠

Personal AI assistant with intelligent routing, Home Assistant integration, and RAG-based memory.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Open WebUI    │────▶│   Orchestrator   │────▶│  Nemotron (8B)  │
│   (Frontend)    │     │   (Router/HA)    │     │  or Helios(120B)│
└─────────────────┘     └────────┬─────────┘     └─────────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ ChromaDB │ │   Home   │ │  LiteLLM │
              │   (RAG)  │ │Assistant │ │  (Proxy) │
              └──────────┘ └──────────┘ └──────────┘
```

## Components

### Orchestrator (`/orchestrator`)

The brain of the system. Routes requests intelligently:

- **Simple queries** → Nemotron 8B (fast)
- **Complex reasoning/code** → Helios 120B (powerful)
- **Home automation** → Home Assistant API
- **Personal context** → ChromaDB RAG

**Files:**
- `orchestrator.py` - Main FastAPI app (v4.0)
- `ha_integration.py` - Smart Home Assistant integration with auto-discovery
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
docker-compose up -d
```

### 2. Check health

```bash
curl http://localhost:8888/health
```

### 3. Access the UI

Open http://localhost in your browser (Open WebUI)

## Home Assistant Commands

The orchestrator auto-discovers all HA entities. Supported commands:

**Lights:**
```
turn on living room
turn off the kitchen lights
dim bedroom to 50%
turn the office red
set living room to blue
```

**Scenes:**
```
activate movie scene
turn on cozy scene
```

**Media:**
```
pause the living room speaker
play music on bedroom
volume on office to 30%
```

**Supported colors:** red, green, blue, yellow, orange, purple, pink, white, cyan, magenta, lavender, coral, teal, turquoise, gold, salmon, lime, violet, indigo, sunset, sunrise, ocean, forest, fire, ice, romantic, party, relax, focus, energize, night, movie

## Voice Assistant Setup

The orchestrator integrates with Home Assistant's voice pipeline for full voice control with personal context.

### Architecture

```
🎤 Voice → Whisper STT → Home Assistant → Brain Gateway Orchestrator
                                                    ↓
                                         RAG (personal context)
                                         HA commands (device control)
                                         Smart routing (Nemotron/Helios)
                                                    ↓
                                              Piper TTS → 🔊
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
- "Set the bedroom to 50%"

**Smart routing:**
- Simple queries → Nemotron 8B (fast)
- Complex questions → Helios 120B (powerful)

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
│   ├── orchestrator.py     # Main routing logic
│   └── ha_integration.py   # Home Assistant module
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
docker-compose restart orchestrator
```

## Development

### Rebuild after changes

```bash
cd /opt/voyager/gateway_mvp
docker-compose down
docker-compose build --no-cache orchestrator
docker-compose up -d
```

### View logs

```bash
docker logs brain-orchestrator --tail 50 -f
```

### Test HA integration directly

```bash
curl -X POST http://localhost:8888/api/ha/command \
  -H "Content-Type: application/json" \
  -d '{"command": "turn on living room"}'
```

## Hardware

This system runs on a home lab cluster:

| Node | GPU | Role |
|------|-----|------|
| Helios | RTX 5090 (32GB) | Large model inference (120B) |
| Saturn | RTX 5080 (16GB) | Medium models |
| Uranus | RTX 3080 (10GB) | Small models, Whisper STT |
| Neptune | RTX 3090 (24GB) | Backup inference |
| Voyager | None | Gateway, orchestration |

## License

Private project - Nadim Nabi
