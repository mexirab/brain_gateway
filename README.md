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
| `update_data` | Add, update, or remove medications and projects via natural language |

Nemotron receives the full HA entity list and handles all NLP parsing internally - no regex matching needed.

**Files:**
- `orchestrator.py` - Main FastAPI app (v5.0 agentic)
- `ha_integration.py` - HA entity discovery + thin API relay
- `data_manager.py` - YAML-based data management for medications/projects
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

## AI-Editable Structured Data

The `update_data` tool allows natural language updates to medications and projects:

**Example commands:**
- "Add Adderall 20mg to my morning meds"
- "Remove Wellbutrin from my medications"
- "Change Vyvanse to 50mg"
- "Mark voice interface complete on Brain Gateway"
- "Add a project to build a deck"

**Architecture:**
- **YAML files** = source of truth (easy to parse/update programmatically)
- **Markdown files** = auto-generated for RAG indexing
- When YAML is updated → markdown regenerates → `watch_and_ingest.py` auto-reindexes

**Data files:**
| File | Purpose |
|------|---------|
| `~/rag/nadim_rag/10_profile/medications.yaml` | Medications (daily, weekly, as-needed) |
| `~/rag/nadim_rag/30_projects/projects.yaml` | Projects (active, on-hold, completed) |

**Medication actions:**
- `add_medication` - Add to morning/evening/weekly/as_needed
- `remove_medication` - Remove by name
- `update_medication` - Change dose, purpose, or notes

**Project actions:**
- `add_project` - Create new project (active, someday_maybe, parking_lot)
- `update_project_status` - Set to not_started/in_progress/blocked/done
- `add_project_step` - Add a next step or completed item
- `complete_step` - Move step from next_steps to completed

## Voice Pipeline

The system includes a complete voice pipeline with custom TTS using voice cloning.

### Architecture

```
                          URANUS (10.0.0.173)
                    ┌─────────────────────────────────┐
                    │  GPU 0: Qwen3-TTS (port 8002)   │
                    │  GPU 1: Whisper STT (port 8003) │
                    └─────────────────────────────────┘
                                    │
       ┌────────────────────────────┼────────────────────────────┐
       ▼                            ▼                            ▼
  Open WebUI               Orchestrator                    HA Speakers
  (voice chat)         /api/briefing/morning         (morning briefings)
                       /api/audio/{id}.wav
```

### Voice Cloning (Jessica McCabe)
The TTS uses Jessica McCabe's voice from "How to ADHD" - warm, energetic, ADHD-friendly.
- Voice prompts cached for fast generation
- Auto-loads from `~/tts-voices/voices.json` on Uranus

### Morning Briefing (Phase 4)
Personalized morning announcements via Jessica's voice on HA speakers:
1. HA automation triggers at 7:30 AM weekdays
2. Orchestrator searches RAG for routine/meds info
3. Nemotron generates personalized briefing
4. TTS generates audio, hosted at `/api/audio/{id}.wav`
5. HA speaker plays audio via `media_player.play_media`

## Voice Assistant Setup (Open WebUI)

Open WebUI integrates with the voice pipeline for voice chat.

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
| `/api/briefing/morning` | POST | Generate morning briefing (optionally with TTS) |
| `/api/audio/{id}.wav` | GET | Serve generated audio files to HA speakers |

## Configuration

### Environment Variables

Set in `docker-compose.yml` or `.env`:

| Variable | Description | Default |
|----------|-------------|---------|
| `NEMOTRON_URL` | Nemotron LLM endpoint | `http://10.0.0.58:8001/v1` |
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
├── rag/
│   ├── ingest_rag.py       # Document ingestion
│   ├── query_rag.py        # CLI query tool
│   ├── watch_and_ingest.py # File watcher daemon
│   └── rag_chat_llamacpp.py # Standalone RAG chat
├── tts/
│   ├── server.py           # Qwen3-TTS server with voice cloning
│   ├── stt_server.py       # Whisper STT server
│   ├── qwen-tts.service    # Systemd service for TTS
│   ├── whisper-stt.service # Systemd service for STT
│   └── README.md           # TTS documentation
├── scripts/
│   └── morning_briefing.sh # Morning briefing trigger script
└── ha_automations/
    └── morning_briefing.yaml # HA automation template
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

| Node | IP | GPU | VRAM | RAM | Role |
|------|-----|-----|------|-----|------|
| Helios | 10.0.0.195 | RTX 5090 | 32GB | 124GB | Large model inference (120B) |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | 34GB | 62GB | **Nemotron-Orchestrator-8B** on RTX 3090 |
| Uranus | 10.0.0.173 | 2x RTX 5080 | 32GB | 62GB | **Qwen3-TTS (GPU 0)** + **Whisper STT (GPU 1)** |
| Jupiter | 10.0.0.248 | RX 6900 XT + RX 6800 | 32GB | 32GB | AMD ROCm (image gen, backup LLM) |
| Voyager | 10.0.0.186 | None | - | 32GB | Gateway, orchestration |

## Monitoring

Full observability stack with Grafana, Prometheus, and Loki.

### Quick Start

```bash
cd /opt/voyager/gateway_mvp/monitoring
docker-compose -p monitoring up -d
```

**Access Grafana:** http://localhost:3000 (admin / braingw)

### What's Monitored

| Metric Type | Source | Nodes |
|-------------|--------|-------|
| System (CPU, RAM, Disk) | node_exporter | All 5 nodes |
| GPU (VRAM, Utilization, Temp) | nvidia_gpu_exporter | Helios, Uranus, Saturn |
| Logs | Promtail → Loki | All Docker containers |
| LLM Metrics | vLLM /metrics | Nemotron on Saturn |

### Pre-built Dashboard

The "Brain Gateway Overview" dashboard shows:
- Cluster node status (online/offline)
- CPU/Memory/Disk usage per node
- GPU VRAM, utilization, temperature
- Live orchestrator logs with tool call filtering

### Useful Loki Queries

```
# All orchestrator logs
{container="brain-orchestrator"}

# Tool calls only (HA, memory, expert)
{container="brain-orchestrator"} |~ "tool_call|home_assistant|search_memory|ask_expert"

# Home Assistant commands
{container="brain-orchestrator"} |~ "\\[HA\\]"

# Errors only
{container="brain-orchestrator"} |~ "(?i)error|exception|failed"
```

### Hardware Audit

Run a hardware audit across all nodes:

```bash
/opt/voyager/gateway_mvp/monitoring/lab_hw_audit.sh
```

See `monitoring/README.md` for full setup details.

## License

Private project - Nadim Nabi
