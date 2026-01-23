# Brain Gateway Architecture

Deep dive into the v5 agentic orchestrator internals.

## Overview

The system uses **Nemotron-Orchestrator-8B** as the decision-making brain. It receives user requests, decides which tools to call, and synthesizes responses. The orchestrator runs an agentic loop that continues until Nemotron responds without requesting more tool calls.

```
User Request
     │
     ▼
┌─────────────────────────────────────────────────────────┐
│                    AGENTIC LOOP                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 1. Send conversation + tools to Nemotron          │  │
│  │ 2. Parse <tool_call> tags from response           │  │
│  │ 3. Execute tools, collect results                 │  │
│  │ 4. Add results to conversation                    │  │
│  │ 5. If no tool calls → return response             │  │
│  │    Else → loop back to step 1                     │  │
│  └───────────────────────────────────────────────────┘  │
│                    (max 5 rounds)                       │
└─────────────────────────────────────────────────────────┘
     │
     ▼
Final Response to User
```

## Key Files

### `orchestrator/orchestrator.py`

Main FastAPI application (v5.0). ~630 lines.

#### Constants & Configuration

```python
NEMOTRON_URL = "http://10.0.0.58:8001/v1"  # Saturn - brain (RTX 3090)
NEMOTRON_MODEL = "nvidia/Nemotron-Orchestrator-8B"
HELIOS_URL = "http://10.0.0.195:8080/v1"    # Helios - expert
HELIOS_MODEL = "unsloth_gpt-oss-120b-GGUF..."
MAX_TOOL_ROUNDS = 5  # Prevent infinite loops
```

#### Tool Definitions

**`get_ha_tool_definition()`** - Builds the home_assistant tool dynamically with current entity list injected into the description. This gives Nemotron visibility into available devices.

**`STATIC_TOOLS`** - Contains `search_memory` and `ask_expert` tool definitions.

**`get_orchestrator_tools()`** - Returns `[get_ha_tool_definition()] + STATIC_TOOLS`

#### Core Functions

| Function | Purpose |
|----------|---------|
| `call_model(url, model, messages, system, tools, timeout)` | Generic LLM caller. Adds `tool_choice: "none"` when tools provided (workaround for vLLM). |
| `parse_tool_calls_from_content(content)` | Extracts `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` from Nemotron's text output. Returns list of tool call dicts. |
| `clean_response(text)` | Strips `<think>` and `<tool_call>` tags from final response. |
| `execute_tool(tool_name, arguments)` | Router that dispatches to the appropriate tool handler. |
| `get_orchestrator_system_prompt()` | Returns the system prompt that instructs Nemotron on tool usage. |

#### Tool Handlers

| Handler | Purpose |
|---------|---------|
| `tool_home_assistant(entity_id, service, data)` | Calls `ha_client.call_service()` with structured params |
| `tool_search_memory(query)` | Calls `rag_context()` to search ChromaDB |
| `tool_ask_expert(question, context)` | Forwards to Helios 120B for complex reasoning |

#### RAG Functions

| Function | Purpose |
|----------|---------|
| `rag_context(query)` | Embeds query with sentence-transformers, queries ChromaDB, returns formatted chunks with relevance scores |
| `last_user_text(messages)` | Extracts most recent user message from conversation |

#### Main Endpoint

**`POST /v1/chat/completions`** - The agentic loop:

```python
for round_num in range(MAX_TOOL_ROUNDS):
    # 1. Call Nemotron with conversation + tools
    llm_resp = await call_model(NEMOTRON_URL, NEMOTRON_MODEL, conversation,
                                system=system_prompt, tools=get_orchestrator_tools())

    # 2. Parse tool calls from content (Nemotron outputs <tool_call> tags)
    tool_calls = parse_tool_calls_from_content(content)

    # 3. If no tool calls, return response
    if not tool_calls:
        return JSONResponse(llm_resp)

    # 4. Execute tools and add results to conversation
    for tool_call in tool_calls:
        result = await execute_tool(tool_name, arguments)
        tool_results.append(f"[{tool_name}] {result}")

    # 5. Add as user message with instruction to respond
    conversation.append({
        "role": "user",
        "content": f"<tool_response>\n{results}\n</tool_response>\n\n...respond naturally..."
    })
```

#### Why `tool_choice: "none"`?

vLLM on Saturn doesn't have `--enable-auto-tool-choice` flag set. Without it, passing `tools` returns a 400 error. Setting `tool_choice: "none"` tells vLLM to ignore native tool calling, but Nemotron still outputs tool calls as `<tool_call>` XML tags in its content, which we parse manually.

---

### `orchestrator/ha_integration.py`

Home Assistant client with entity discovery. ~720 lines.

#### Key Classes

**`Entity`** - Dataclass representing an HA entity:
```python
@dataclass
class Entity:
    entity_id: str      # "light.bedroom_fan_lights"
    domain: str         # "light"
    friendly_name: str  # "Bedroom Fan Lights"
    state: str          # "on", "off", etc.
    attributes: Dict    # brightness, rgb_color, etc.
```

**`ExecutionResult`** - Dataclass for command results:
```python
@dataclass
class ExecutionResult:
    success: bool
    action: str         # "light.turn_on"
    entity_id: str
    message: str        # "✓ Set Bedroom Fan Lights to color [0,0,255] at 50%"
    details: Optional[Dict]
```

**`HomeAssistantClient`** - Main client class.

#### HomeAssistantClient Methods

| Method | Purpose |
|--------|---------|
| `refresh_entities(force=False)` | Fetches all entities from HA REST API, caches them. Called at startup. |
| `get_entities_by_domain(domain)` | Returns list of entities for a domain (used to build tool description) |
| `call_service(entity_id, service, data)` | **PRIMARY METHOD** - Direct HA API call. No NLP parsing. |

#### `call_service()` Detail

This is the thin API relay that Nemotron's structured calls use:

```python
async def call_service(self, entity_id: str, service: str, data: Dict = None) -> ExecutionResult:
    domain = entity_id.split(".")[0]  # "light" from "light.bedroom"
    service_data = {"entity_id": entity_id, **data}

    url = f"{self.url}/api/services/{domain}/{service}"
    resp = await client.post(url, headers=self._headers, json=service_data)

    # Build human-readable message
    if data and "rgb_color" in data:
        msg = f"✓ Set {friendly_name} to color {data['rgb_color']}"
    ...
    return ExecutionResult(success=True, message=msg, ...)
```

#### Legacy NLP Parsing (Still Present, Unused)

The file still contains regex-based command parsing (`COMMAND_PATTERNS`, `parse_command()`, `execute_command()`, `_fuzzy_match_entity()`). These are **no longer used** since Nemotron handles all NLP. They could be removed in a future cleanup.

---

## Data Flow Examples

### Example 1: "Turn on bedroom lights and set to blue at 50%"

```
1. User sends message
2. Orchestrator calls Nemotron with:
   - Conversation history
   - System prompt
   - Tools (including entity list)

3. Nemotron responds with:
   <think>User wants bedroom lights on, blue, 50%...</think>
   <tool_call>
   {"name": "home_assistant", "arguments": {
     "entity_id": "light.bedroom_fan_lights",
     "service": "turn_on",
     "data": {"brightness": 128, "rgb_color": [0,0,255]}
   }}
   </tool_call>

4. Orchestrator parses <tool_call>, executes:
   ha_client.call_service("light.bedroom_fan_lights", "turn_on",
                          {"brightness": 128, "rgb_color": [0,0,255]})

5. Result: "✓ Set Bedroom Fan Lights to color [0,0,255] at 50%"

6. Orchestrator adds to conversation:
   {"role": "user", "content": "<tool_response>[home_assistant] ✓ Set...</tool_response>"}

7. Nemotron responds (no more tool calls):
   "Done! Your bedroom lights are now blue at 50% brightness."

8. Return to user
```

### Example 2: "What projects am I working on?"

```
1. Nemotron decides to use search_memory tool
2. <tool_call>{"name": "search_memory", "arguments": {"query": "current projects"}}</tool_call>
3. RAG returns chunks from ~/rag/nadim_rag/30_projects/
4. Nemotron may then call ask_expert to synthesize
5. Final response with project summary
```

---

## ChromaDB / RAG

**Collection:** `nadim_rag`
**Persist path:** `/home/nadim/.local/share/chroma/personal_rag` (mounted into container)
**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2`

Query flow:
```python
query_embedding = embedding_model.encode(query, normalize_embeddings=True)
results = collection.query(query_embeddings=[query_embedding], n_results=TOP_K)
# Filter by MIN_COS similarity, format with source paths
```

---

## Docker Setup

**Project name:** `brain` (use `docker-compose -p brain ...`)

**Containers:**
| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| brain-orchestrator | gateway_mvp-orchestrator | 8888 | Main API |
| brain-redis-1 | redis:7-alpine | 6379 (internal) | Cache |
| brain-litellm-1 | ghcr.io/berriai/litellm | 4000 | LLM proxy |
| brain-open-webui-1 | ghcr.io/open-webui/open-webui | 80 | Web UI |

**Volumes:**
- ChromaDB: `/home/nadim/.local/share/chroma:/chroma:rw`
- Env file: `/home/nadim/brain_gateway/.env:/app/.env:ro`

---

## Troubleshooting

### "400 Bad Request" from Nemotron
vLLM rejects `tools` parameter without `--enable-auto-tool-choice`. The orchestrator works around this with `tool_choice: "none"`.

### Tool calls looping infinitely
Check that tool results are being added to conversation correctly. The `<tool_response>` wrapper and "Do NOT call any more tools" instruction should stop the loop.

### HA commands failing
1. Check `docker logs brain-orchestrator` for `[HA]` messages
2. Verify entity_id exists: `curl http://localhost:8888/api/ha/entities`
3. Test direct call:
   ```bash
   curl -X POST http://localhost:8888/api/ha/command \
     -d '{"entity_id": "light.living_room", "service": "turn_on"}'
   ```

### RAG returning no results
1. Check collection has docs: `curl http://localhost:8888/api/memory/stats`
2. Test query: `curl "http://localhost:8888/api/memory/search?query=projects"`
3. Re-ingest if needed (see README)

---

## Voice Pipeline

Full voice synthesis and speech recognition on Uranus (both GPUs utilized).

### Architecture

```
                          URANUS (10.0.0.173)
         ┌──────────────────────────────────────────────┐
         │  GPU 0 (cuda:0)          GPU 1 (cuda:1)      │
         │  ┌─────────────────┐     ┌────────────────┐  │
         │  │   Qwen3-TTS     │     │  Whisper STT   │  │
         │  │   port 8002     │     │   port 8003    │  │
         │  │ Jessica voice   │     │ OpenAI compat  │  │
         │  └─────────────────┘     └────────────────┘  │
         └──────────────────────────────────────────────┘
                    │                        │
       ┌────────────┼────────────┬───────────┼──────────┐
       ▼            ▼            ▼           ▼          ▼
  Orchestrator  Open WebUI   HA Speakers  Open WebUI  Any STT
  (briefings)   (TTS chat)   (play_media) (voice in)  client
```

### TTS Server (`tts/server.py`)

Qwen3-TTS with voice cloning capability.

**Key endpoints:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/tts` | POST | Generate speech with preset or cloned voice |
| `/voices` | GET | List available voices (preset + cloned) |
| `/voices/load` | POST | Load a cloned voice from audio sample |
| `/v1/audio/speech` | POST | OpenAI-compatible endpoint |

**Voice cloning flow:**
1. Reference audio + transcript stored in `~/tts-voices/voices.json`
2. On startup, server auto-loads voices from config
3. First use generates voice prompt (cached for future requests)
4. Subsequent requests use cached prompt for fast generation

**Important:** Requires `Qwen3-TTS-1.7B-Base` model (not CustomVoice) for voice cloning.

### STT Server (`tts/stt_server.py`)

Whisper-based speech recognition with OpenAI-compatible API.

**Endpoints:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/audio/transcriptions` | POST | Transcribe audio file |
| `/health` | GET | Health check |

**Configuration:**
```bash
WHISPER_MODEL=base      # Model size (tiny, base, small, medium, large)
WHISPER_DEVICE=cuda:1   # Use GPU 1 (GPU 0 is TTS)
WHISPER_PORT=8003
```

### Morning Briefing Flow

```
1. HA Automation (7:30 AM weekdays)
        │
        ▼
2. POST /api/briefing/morning
   {generate_tts: true, play_on: "media_player.kitchen_display"}
        │
        ▼
3. Orchestrator searches RAG for morning routine/meds
        │
        ▼
4. Nemotron generates personalized briefing text
        │
        ▼
5. POST to TTS server with Jessica voice
        │
        ▼
6. Audio saved to /tmp/brain_audio/{uuid}.wav
   Registered in audio_cache dict
        │
        ▼
7. HA call: media_player.play_media
   media_content_id: http://10.0.0.186:8888/api/audio/{uuid}.wav
        │
        ▼
8. HA speaker fetches and plays audio
```

### Systemd Services

Both services run on Uranus as systemd units:

```bash
# TTS service
sudo systemctl status qwen-tts
# Config: /etc/systemd/system/qwen-tts.service

# STT service
sudo systemctl status whisper-stt
# Config: /etc/systemd/system/whisper-stt.service
```

---

## Monitoring Stack

Full observability with Grafana, Prometheus, and Loki running on Voyager.

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        VOYAGER                              │
│  ┌─────────┐  ┌────────────┐  ┌──────┐  ┌──────────┐       │
│  │ Grafana │◄─│ Prometheus │◄─│ Loki │◄─│ Promtail │       │
│  │  :3000  │  │   :9090    │  │:3100 │  │          │       │
│  └─────────┘  └─────┬──────┘  └──────┘  └────┬─────┘       │
│                     │                        │              │
│               scrape metrics            Docker logs         │
└─────────────────────┼────────────────────────┼──────────────┘
                      │                        │
      ┌───────────────┼───────────────┬────────┘
      ▼               ▼               ▼
┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│  HELIOS   │  │  URANUS   │  │  SATURN   │  │  JUPITER  │
│ node:9100 │  │ node:9100 │  │ node:9100 │  │ node:9100 │
│ gpu:9400  │  │ gpu:9400  │  │ gpu:9400  │  │           │
│ (Expert)  │  │(TTS+STT)  │  │ (Brain)   │  │(AMD ROCm) │
└───────────┘  └───────────┘  └───────────┘  └───────────┘
```

### Components

| Component | Port | Purpose |
|-----------|------|---------|
| Grafana | 3000 | Dashboard UI (admin/braingw) |
| Prometheus | 9090 | Metrics collection, 30-day retention |
| Loki | 3100 | Log aggregation, 30-day retention |
| Promtail | - | Ships Docker container logs to Loki |
| node_exporter | 9100 | System metrics (CPU, RAM, disk) |
| nvidia_gpu_exporter | 9400 | GPU metrics (VRAM, utilization, temp) |

### Prometheus Targets

```
node-exporter:     voyager, helios, uranus, saturn, jupiter
gpu-exporter:      helios, uranus, saturn
llm-services:      nemotron (saturn:8001)
```

### Key Metrics

**System (node_exporter):**
- `node_cpu_seconds_total` - CPU usage
- `node_memory_MemAvailable_bytes` - Available RAM
- `node_filesystem_avail_bytes` - Disk space

**GPU (nvidia_gpu_exporter):**
- `nvidia_smi_memory_total_bytes` / `nvidia_smi_memory_free_bytes` - VRAM
- `nvidia_smi_utilization_gpu_ratio` - GPU utilization (0-1)
- `nvidia_smi_temperature_gpu` - GPU temperature (Celsius)

### Log Labels

Promtail adds these labels to logs:
- `container` - Docker container name (e.g., `brain-orchestrator`)
- `service` - Compose service name
- `project` - Compose project name

### Files

```
monitoring/
├── docker-compose.yml           # Stack definition
├── README.md                    # Setup instructions
├── lab_hw_audit.sh              # Hardware audit script
├── prometheus/
│   └── prometheus.yml           # Scrape targets
├── loki/
│   └── loki-config.yml          # Log storage config
├── promtail/
│   └── promtail-config.yml      # Log shipping rules
└── grafana/
    └── provisioning/
        ├── datasources/         # Prometheus + Loki
        └── dashboards/          # Pre-built dashboards
```

### Commands

```bash
# Start monitoring
cd /opt/voyager/gateway_mvp/monitoring
docker-compose -p monitoring up -d

# Stop monitoring
docker-compose -p monitoring down

# View Prometheus targets
curl http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | "\(.labels.job): \(.health)"'

# Query Loki
curl -sG 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={container="brain-orchestrator"}' | jq .

# Check GPU metrics
curl -s 'http://localhost:9090/api/v1/query?query=nvidia_smi_temperature_gpu' | jq '.data.result[]'
```
