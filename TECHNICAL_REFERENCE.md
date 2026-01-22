# Brain Gateway - Technical Reference

> Detailed specs for implementation. Reference when building specific features.

---

## Hardware Specifications

### Helios (Primary Inference)
- **GPU:** NVIDIA RTX 5090 (32GB VRAM)
- **CPU:** AMD Ryzen 9 9900X3D (12-core, 24 threads)
- **RAM:** 128GB DDR5
- **Motherboard:** Gigabyte X870 Gaming X WiFi7
- **Driver:** NVIDIA 570.169, CUDA 12.8
- **Role:** Large model inference (120B) - `ask_expert` tool target

### Jupiter
- **GPU:** NVIDIA RTX 5080 (16GB VRAM)
- **CPU:** AMD Ryzen 5 5600X (6-core, 12 threads)
- **RAM:** 16GB
- **Motherboard:** MSI B550-A PRO
- **Driver:** NVIDIA 580.95.05, CUDA 13.0

### Saturn
- **GPU:** NVIDIA RTX 5080 (16GB VRAM)
- **CPU:** AMD Ryzen 7 3800X (8-core, 16 threads)
- **RAM:** 32GB
- **Motherboard:** ASUS TUF Gaming X570-Plus
- **Driver:** NVIDIA 580.95.05, CUDA 13.0
- **Role:** Medium models, batch processing

### Uranus
- **GPU:** NVIDIA RTX 3080 (10GB VRAM)
- **CPU:** AMD Ryzen 5 4500 (6-core, 12 threads)
- **RAM:** 16GB
- **Motherboard:** ASUS ROG Strix X570-E Gaming
- **Driver:** NVIDIA 580.95.05, CUDA 13.0
- **Role:** **Nemotron-Orchestrator-8B (THE BRAIN)**, Whisper STT

### Neptune
- **GPU:** NVIDIA RTX 3090 (24GB VRAM)
- **CPU:** Intel Core i7-4790K (4-core, 8 threads)
- **RAM:** 32GB
- **Driver:** NVIDIA 580.95.05, CUDA 13.0
- **Role:** Backup inference, vision models

### Voyager (Gateway)
- **GPU:** None
- **CPU:** Intel Core i5-4690K (4-core, 4 threads)
- **RAM:** 16GB
- **Role:** Docker host, orchestration, no GPU compute

---

## API Endpoint Specifications

### Brain Gateway Orchestrator (port 8888)

#### `GET /health`
```json
{
  "ok": true,
  "version": "5.0",
  "architecture": "agentic",
  "brain": "http://10.0.0.173:8001/v1 (nvidia/Nemotron-Orchestrator-8B)",
  "expert": "http://10.0.0.195:8080/v1 (unsloth_gpt-oss-120b-GGUF...)",
  "tools": ["home_assistant", "search_memory", "ask_expert"],
  "rag_collection": "nadim_rag",
  "rag_docs": 94,
  "ha_entities": 370
}
```

#### `POST /v1/chat/completions`
OpenAI-compatible chat endpoint. Uses agentic tool-calling loop with Nemotron.

Request:
```json
{
  "model": "brain",
  "messages": [
    {"role": "user", "content": "Turn on the bedroom lights and set them to blue at 50%"}
  ]
}
```

Response includes `_routing` for debugging:
```json
{
  "id": "chatcmpl-...",
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "The bedroom lights are now set to blue at 50% brightness."
    }
  }],
  "_routing": {
    "timestamp": "2026-01-22T04:50:04.258498",
    "user_query_length": 54,
    "tool_calls": [
      {
        "tool": "home_assistant",
        "args": {
          "entity_id": "light.bedroom_fan_lights",
          "service": "turn_on",
          "data": {
            "brightness": 128,
            "rgb_color": [0, 0, 255]
          }
        },
        "result_preview": "✓ Set Bedroom Fan Lights to color [0, 0, 255] at 50%"
      }
    ],
    "rounds": 2,
    "mode": "agentic"
  }
}
```

#### `POST /api/ha/command`
Direct Home Assistant service call (structured, not natural language).
```json
{
  "entity_id": "light.living_room",
  "service": "turn_on",
  "data": {"brightness": 255, "rgb_color": [255, 0, 0]}
}
```

Response:
```json
{
  "success": true,
  "action": "light.turn_on",
  "entity_id": "light.living_room",
  "message": "✓ Set Living Room to color [255, 0, 0] at 100%",
  "details": {"service_data": {...}}
}
```

#### `GET /api/ha/entities`
List all discovered Home Assistant entities (what Nemotron sees).
```json
{
  "total": 370,
  "controllable": {
    "light": [
      {"entity_id": "light.bedroom_fan_lights", "friendly_name": "Bedroom Fan Lights", "state": "on"},
      ...
    ],
    "switch": [...],
    "climate": [...]
  }
}
```

#### `POST /api/memory/add`
Add memory to RAG.
```json
{
  "text": "Remember this information",
  "category": "general",
  "source": "manual"
}
```

#### `GET /api/memory/search?query=...&n=5`
Search RAG memory.

#### `GET /api/memory/stats`
```json
{
  "collection": "nadim_rag",
  "total_documents": 94,
  "persist_path": "/chroma/personal_rag"
}
```

---

## Agentic Tool System

### How Nemotron Tool Calling Works

vLLM on Uranus doesn't have `--enable-auto-tool-choice` enabled, so native OpenAI-style tool calling returns 400. Workaround:

1. Send `tool_choice: "none"` with tools in payload
2. Nemotron outputs tool calls as XML in content:
   ```
   <think>User wants lights on...</think>
   <tool_call>
   {"name": "home_assistant", "arguments": {"entity_id": "light.bedroom", "service": "turn_on"}}
   </tool_call>
   ```
3. `parse_tool_calls_from_content()` extracts these
4. Results fed back as user message with `<tool_response>` wrapper
5. Loop until Nemotron responds without tool calls (max 5 rounds)

### Tool Definitions

#### `home_assistant`
Nemotron receives full entity list in tool description. Outputs structured calls:
```json
{
  "entity_id": "light.bedroom_fan_lights",
  "service": "turn_on",
  "data": {"brightness": 128, "rgb_color": [0, 0, 255]}
}
```

**Services by domain:**
| Domain | Services | Data Parameters |
|--------|----------|-----------------|
| light | turn_on, turn_off, toggle | brightness (0-255), rgb_color ([R,G,B]) |
| switch | turn_on, turn_off, toggle | - |
| fan | turn_on, turn_off, toggle | percentage (0-100) |
| climate | set_temperature | temperature (int) |
| cover | open_cover, close_cover | position (0-100) |
| scene | turn_on | - |
| lock | lock, unlock | - |

**Color reference (RGB):**
- Blue: [0, 0, 255]
- Red: [255, 0, 0]
- Green: [0, 255, 0]
- Purple: [128, 0, 128]
- Yellow: [255, 255, 0]
- Orange: [255, 165, 0]
- White: [255, 255, 255]

**Brightness:** 0-255 scale (50% = 128, 75% = 191, 100% = 255)

#### `search_memory`
Query ChromaDB RAG for personal context.
```json
{"query": "current projects"}
```

#### `ask_expert`
Delegate to Helios 120B for complex reasoning.
```json
{
  "question": "Explain the tradeoffs between...",
  "context": "Optional additional context"
}
```

---

## Planned API Endpoints

### Phase 4: Reminders

#### `POST /api/remind/meds`
Trigger medication reminder.
```json
{"time": "morning|afternoon|evening"}
```

#### `GET /api/briefing/morning`
Generate morning briefing with weather, calendar, tasks, budget.

### Phase 12: YNAB

#### `GET /api/ynab/categories`
Get all budget categories with balances.

#### `GET /api/ynab/category/{name}`
Get specific category balance (fuzzy name match).

---

## ChromaDB Schema

### Collection: `nadim_rag`

**Chunk Document:**
```json
{
  "id": "chunk::path/to/file.md::h2:Section Name::0::abc123",
  "document": "The actual text content...",
  "metadata": {
    "file_path": "path/to/file.md",
    "file_hash": "sha256...",
    "section": "h2:Section Name",
    "chunk_index": 0,
    "source_root": "/home/nadim/rag/nadim_rag",
    "kind": "chunk"
  }
}
```

**Embedding Model:** `sentence-transformers/all-MiniLM-L6-v2`

**Query Parameters:**
- `TOP_K`: 25 (candidates to retrieve)
- `MIN_COS`: 0.20 (minimum cosine similarity)
- `MIN_CHUNK_LEN`: 100 (skip header-only chunks)

---

## File Templates

### Medication Schedule
`~/rag/nadim_rag/20_routines/medications.md`
```markdown
# Medication Schedule

## Morning (8:00 AM)
- Medication Name - dosage - with/without food

## Afternoon (2:00 PM)
- Medication Name - dosage - notes

## Evening (8:00 PM)
- Medication Name - dosage - notes
```

### Person Profile
`~/rag/nadim_rag/40_relationships/people/{name}.md`
```markdown
# Full Name

## Basics
- **Relationship:** friend/family/colleague
- **Birthday:** Month Day
- **Location:** City, State

## Family
- Spouse: Name
- Kids: Names, ages

## Work
- Current job/company
- Previous roles

## Interests
- Hobbies, topics they care about

## Gift Ideas
- Things they've mentioned wanting

## Notes
### YYYY-MM-DD
- What you talked about
```

### Vehicle Maintenance
`~/rag/nadim_rag/30_projects/vehicles/{vehicle}.md`
```markdown
# Year Make Model

## Info
- **VIN:**
- **License:**
- **Purchase Date:**
- **Current Mileage:**

## Maintenance Schedule
- Oil change: Every X miles
- Tire rotation: Every X miles

## Maintenance Log

### YYYY-MM-DD - Service Type
- Mileage:
- Service location:
- Cost: $
- Notes:
- Next due:
```

### Dopamine Menu
`~/rag/nadim_rag/20_routines/dopamine_menu.md`
```markdown
# Dopamine Menu

## Quick Hits (5 min)
- Item 1
- Item 2

## Medium (15-30 min)
- Item 1
- Item 2

## Deep Reset (30+ min)
- Item 1
- Item 2

## Emergency
- Compassionate reminders
```

---

## External API References

### YNAB API
- Base URL: `https://api.youneedabudget.com/v1`
- Auth: `Authorization: Bearer {token}`
- Docs: https://api.ynab.com/
- Python: `pip install ynab-api` or direct REST

### Open-Meteo Weather (Free, No Key)
```
GET https://api.open-meteo.com/v1/forecast
  ?latitude=29.76
  &longitude=-95.36
  &current_weather=true
  &temperature_unit=fahrenheit
```

### Home Assistant REST API
- Base URL: `http://10.0.0.106:8123/api`
- Auth: `Authorization: Bearer {long_lived_token}`
- Services: `POST /api/services/{domain}/{service}`
- States: `GET /api/states/{entity_id}`

---

## Docker Stack

**Project name:** `brain` (always use `docker-compose -p brain`)

```yaml
services:
  orchestrator:
    build: ./orchestrator
    container_name: brain-orchestrator
    ports:
      - "8888:8888"
    environment:
      - HA_URL=http://10.0.0.106:8123
      - HA_TOKEN=${HA_TOKEN}
      - NEMOTRON_URL=http://10.0.0.173:8001/v1
      - NEMOTRON_MODEL=nvidia/Nemotron-Orchestrator-8B
      - HELIOS_URL=http://10.0.0.195:8080/v1
      - CHROMA_PERSIST=/chroma/personal_rag
      - CHROMA_COLLECTION=nadim_rag
    volumes:
      - /home/nadim/.local/share/chroma:/chroma:rw
      - /home/nadim/brain_gateway/.env:/app/.env:ro

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    ports:
      - "80:8080"
    environment:
      - OPENAI_API_BASE_URL=http://orchestrator:8888/v1

  redis:
    image: redis:7-alpine
    container_name: redis

  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    container_name: litellm
    ports:
      - "4000:4000"
```

---

## Troubleshooting

### nvidia-smi not working after driver install
1. Check Secure Boot: `mokutil --sb-state`
2. Blacklist nouveau: Check `/etc/modprobe.d/blacklist-nouveau.conf`
3. Reboot required after driver install

### RAG not finding documents
1. Check collection count: `curl localhost:8888/api/memory/stats`
2. Re-run ingest:
   ```bash
   cd /opt/voyager/gateway_mvp/rag
   python ingest_rag.py --source ~/rag/nadim_rag --persist ~/.local/share/chroma/personal_rag --collection nadim_rag
   docker-compose -p brain restart orchestrator
   ```
3. Verify file extensions (.md, .txt supported)

### Home Assistant commands failing
1. Check entity exists: `curl localhost:8888/api/ha/entities | jq '.controllable.light'`
2. Check token: `curl -H "Authorization: Bearer $HA_TOKEN" http://10.0.0.106:8123/api/`
3. Test direct call:
   ```bash
   curl -X POST http://localhost:8888/api/ha/command \
     -H "Content-Type: application/json" \
     -d '{"entity_id": "light.living_room", "service": "turn_on"}'
   ```
4. Check orchestrator logs: `docker logs brain-orchestrator -f`

### Nemotron returning 400 Bad Request
This happens if `tools` is sent without workaround. The orchestrator should automatically add `tool_choice: "none"`. If you see this:
1. Check orchestrator code has `payload["tool_choice"] = "none"` in `call_model()`
2. Restart: `docker-compose -p brain restart orchestrator`

### Tool calls looping infinitely (hits max rounds)
1. Check tool results are being added to conversation correctly
2. Verify `<tool_response>` wrapper is present in the user message
3. Check the "Do NOT call any more tools" instruction is in the prompt
4. Review logs: `docker logs brain-orchestrator --tail 100`

### Orchestrator not starting
1. Check if sentence-transformers model is downloading (takes time on first start)
2. Check logs: `docker logs brain-orchestrator`
3. Verify ChromaDB mount: `ls -la /home/nadim/.local/share/chroma/personal_rag`
