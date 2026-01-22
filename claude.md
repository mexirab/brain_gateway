# Brain Gateway - Project Context for Claude

> **Owner:** Nadim (has ADHD - prefers step-by-step instructions with verification)
> **Goal:** Self-hosted personal AI assistant that rivals Google Assistant but keeps all data private

## Quick Reference

### Hardware Cluster
| Node | IP | GPU | VRAM | RAM | Primary Role |
|------|-----|-----|------|-----|--------------|
| Helios | 10.0.0.195 | RTX 5090 | 32GB | 124GB | Large models (120B), primary inference |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | 34GB | 62GB | **Nemotron-Orchestrator-8B (brain)** on RTX 3090 |
| Uranus | 10.0.0.173 | 2x RTX 5080 | 32GB | 62GB | Available (Whisper STT) |
| Jupiter | 10.0.0.248 | None | - | 32GB | Compute (no GPU currently) |
| Voyager | 10.0.0.186 | None | - | 32GB | Gateway, orchestration, Docker host |

### Key Paths
- **Orchestrator:** `/opt/voyager/gateway_mvp/`
- **RAG Documents:** `~/rag/nadim_rag/`
- **ChromaDB:** `~/.local/share/chroma/personal_rag`
- **Brain Gateway .env:** `~/brain_gateway/.env`

### Services & Ports
| Service | URL | Purpose |
|---------|-----|---------|
| Brain Gateway | `http://localhost:8888` | Main orchestrator API |
| Open WebUI | `http://localhost` | Chat interface |
| Home Assistant | `http://10.0.0.106:8123` | Smart home control |
| Nemotron (8B) | `http://10.0.0.58:8001/v1` | **THE BRAIN** on Saturn RTX 3090 |
| Helios (120B) | `http://10.0.0.195:8080/v1` | Expert model for complex tasks |
| **Grafana** | `http://localhost:3000` | Monitoring dashboards (admin/braingw) |
| Prometheus | `http://localhost:9090` | Metrics collection |
| Loki | `http://localhost:3100` | Log aggregation |

---

## Architecture Overview (v5 Agentic)

```
User → Open WebUI → Brain Gateway Orchestrator (v5)
                           │
                           ▼
                   Nemotron-Orchestrator-8B
                      (THE BRAIN)
                           │
                    AGENTIC TOOL CALLS
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
    search_memory     home_assistant     ask_expert
         │                 │                 │
    ChromaDB (RAG)    HA REST API      Helios (120B)
    Personal context  Structured calls  Complex reasoning
```

**How it works:**
1. User sends request to orchestrator
2. Orchestrator forwards to Nemotron with system prompt + tools
3. Nemotron decides which tools to call and outputs `<tool_call>` tags
4. Orchestrator parses tool calls, executes them, feeds results back
5. Loop continues until Nemotron responds without tool calls (max 5 rounds)

**Available Tools:**
| Tool | Purpose |
|------|---------|
| `home_assistant` | Nemotron outputs structured API calls: `{entity_id, service, data}` |
| `search_memory` | Query ChromaDB RAG for personal context |
| `ask_expert` | Delegate complex reasoning/coding to Helios 120B |

**Key insight:** Nemotron receives the full HA entity list in the tool description, so it handles all NLP parsing internally. No regex matching needed - it outputs exact entity IDs and service calls.

---

## Current State (What's Working)

✅ **Phase 1:** RAG with markdown/text files
✅ **Phase 2:** Home Assistant integration (lights, scenes, media, colors, brightness)
✅ **Phase 3:** Voice interface (Whisper STT + Piper TTS via HA voice pipeline)
⬜ **Phase 4:** Proactive reminders (meds, calendar, briefings)
⬜ **Phase 5:** Mobile access
⬜ **Phase 6:** Fine-tuning

---

## Roadmap (Phases 7-12)

### Phase 7: Document Intelligence
Add PDF/OCR support to RAG for contracts, tax docs, medical records.
- Add `pymupdf` for PDF text extraction
- Add `pytesseract` for scanned document OCR
- Folder structure: `~/rag/documents/{taxes,contracts,medical,legal}/`

### Phase 8: Video Memory Library
Digitize VHS home videos → searchable, queryable library.
- Whisper transcription on Uranus
- Index transcripts with timestamps in ChromaDB
- Query: "What did grandma say about birds?"

### Phase 9: Self-Hosted Calendar (Optional)
Nextcloud for full privacy. Not required - can use Google Calendar via CalDAV.

### Phase 10: ADHD Support Tools
- **Mood tracker:** Log energy/mood, find patterns
- **Dopamine menu:** Pre-made list of things that help when stuck
- **Wins journal:** Combat imposter syndrome
- **Transition helper:** Ease context switches
- **Body doubling:** Virtual accountability check-ins

### Phase 11: Life Management Trackers
All use same pattern: markdown files in RAG, natural language queries.
- **Car maintenance:** Oil changes, repairs, mileage
- **Where did I put it?:** Track storage locations
- **Inventory:** Freezer, pantry, garage contents
- **Plant care:** Watering schedules
- **Pet care:** Vet records, medications
- **Relationship notes:** Remember details about people, birthdays, gift ideas

### Phase 12: YNAB Integration
Connect to YNAB budget API for natural language budget queries.
- "How much left in dining out?"
- Include budget warnings in morning briefing

---

## RAG Folder Structure

```
~/rag/nadim_rag/
├── 00_system/           # AI instructions, system prompts
├── 10_profile/          # Personal info, preferences, wins journal
├── 20_routines/         # Med schedule, dopamine menu, transitions
├── 30_projects/         # Vehicles, storage, inventory, plants, pets
├── 40_relationships/    # People notes, birthdays, gift ideas
├── 50_documents/        # PDFs, contracts (future)
├── 60_videos/           # Video transcripts (future)
└── 90_archive/          # Old content
```

---

## Key Files to Know

| File | Purpose |
|------|---------|
| `README.md` | High-level overview, quick start, config |
| `ARCHITECTURE.md` | **Detailed internals** - functions, data flow, troubleshooting |
| `orchestrator/orchestrator.py` | Main FastAPI app (v5.0 agentic), tool parsing, agentic loop |
| `orchestrator/ha_integration.py` | HA entity discovery + `call_service()` API relay |
| `rag/ingest_rag.py` | Index documents into ChromaDB |
| `docker-compose.yml` | Service stack definition |
| `.env` | API tokens (HA_TOKEN, YNAB_TOKEN, etc.) |
| `monitoring/docker-compose.yml` | Grafana/Prometheus/Loki stack |
| `monitoring/README.md` | Monitoring setup instructions |
| `monitoring/lab_hw_audit.sh` | Hardware audit script for cluster |

**Important implementation details:**
- vLLM on Uranus doesn't have `--enable-auto-tool-choice`, so we use `tool_choice: "none"` and parse `<tool_call>` tags from Nemotron's content manually
- Tool results are fed back as user messages with `<tool_response>` wrapper
- `ha_integration.py` still has legacy NLP parsing code but it's unused - `call_service()` is the active method

---

## Common Tasks

### Rebuild orchestrator after code changes
```bash
cd /opt/voyager/gateway_mvp
docker-compose -p brain down
docker-compose -p brain build --no-cache orchestrator
docker-compose -p brain up -d
```

### Quick rebuild (if pip deps unchanged)
```bash
docker-compose -p brain up -d --build orchestrator
```

### Re-index RAG after adding documents
```bash
cd /opt/voyager/gateway_mvp/rag
python ingest_rag.py \
  --source ~/rag/nadim_rag \
  --persist ~/.local/share/chroma/personal_rag \
  --collection nadim_rag

# Restart orchestrator to pick up changes
docker-compose -p brain restart orchestrator
```

### Test Home Assistant command (structured)
```bash
curl -X POST http://localhost:8888/api/ha/command \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room", "service": "turn_on", "data": {"brightness": 128}}'
```

### Test full orchestrator flow
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "brain", "messages": [{"role": "user", "content": "Turn on bedroom lights and set to blue at 50%"}]}' | jq .
```

### Check orchestrator health
```bash
curl http://localhost:8888/health
```

### View orchestrator logs
```bash
docker logs brain-orchestrator --tail 50 -f
```

### List HA entities available to Nemotron
```bash
curl http://localhost:8888/api/ha/entities | jq .
```

### Start/stop monitoring stack
```bash
cd /opt/voyager/gateway_mvp/monitoring
docker-compose -p monitoring up -d    # Start
docker-compose -p monitoring down     # Stop
```

### View logs in Grafana
1. Open http://localhost:3000 (admin/braingw)
2. Go to Explore → Select Loki
3. Query: `{container="brain-orchestrator"}`

### Useful Loki queries
```
# All orchestrator logs
{container="brain-orchestrator"}

# Tool calls only
{container="brain-orchestrator"} |~ "tool_call|home_assistant|search_memory|ask_expert"

# Errors only
{container="brain-orchestrator"} |~ "(?i)error|exception|failed"
```

### Hardware audit across cluster
```bash
/opt/voyager/gateway_mvp/monitoring/lab_hw_audit.sh
```

---

## Environment Variables Needed

```bash
# Home Assistant
HA_URL=http://10.0.0.106:8123
HA_TOKEN=<long-lived-access-token>

# LLM Endpoints
NEMOTRON_URL=http://10.0.0.58:8001/v1
NEMOTRON_MODEL=nvidia/Nemotron-Orchestrator-8B
HELIOS_URL=http://10.0.0.195:8080/v1
HELIOS_MODEL=unsloth_gpt-oss-120b-GGUF_Q4_K_S_gpt-oss-120b-Q4_K_S-00001-of-00002.gguf

# RAG
CHROMA_PERSIST=/chroma/personal_rag
CHROMA_COLLECTION=nadim_rag
MIN_COS=0.20
TOP_K=25

# Future integrations
YNAB_TOKEN=<ynab-personal-access-token>
OPENWEATHERMAP_KEY=<optional-for-weather>
```

---

## Design Principles

1. **Privacy first** - All data stays on local servers
2. **Markdown for data** - Simple, human-readable, git-friendly
3. **Natural language** - Query everything conversationally
4. **ADHD-friendly** - Step-by-step, verify each step, celebrate wins
5. **Graceful degradation** - If one service is down, others still work
6. **LLM does the parsing** - Nemotron handles NLP, code just relays structured calls

---

## Docker Setup

**Project name:** `brain` (always use `-p brain` with docker-compose)

**Brain Gateway Containers:**
| Container | Port | Purpose |
|-----------|------|---------|
| brain-orchestrator | 8888 | Main API |
| brain-redis-1 | 6379 (internal) | Cache |
| brain-litellm-1 | 4000 | LLM proxy |
| brain-open-webui-1 | 80 | Web UI |

**Monitoring Stack** (project name: `monitoring`)
| Container | Port | Purpose |
|-----------|------|---------|
| grafana | 3000 | Dashboards |
| prometheus | 9090 | Metrics |
| loki | 3100 | Logs |
| promtail | - | Log shipper |
| node-exporter | 9100 | System metrics |

**Remote Node Exporters** (systemd services on each node):
- `node_exporter` on Helios, Uranus, Saturn, Jupiter (port 9100)
- `nvidia_gpu_exporter` on Helios, Uranus, Saturn (port 9400)

---

## ClickUp Integration

Project tasks are tracked in ClickUp:
- **Workspace:** AI Lab Setup
- **List:** Brain Gateway - Next Steps
- **Phases:** Organized as parent tasks with subtasks

Claude has MCP access to ClickUp for reading/creating tasks.

---

## When Starting a New Session

1. Read this file + `ARCHITECTURE.md` for full context
2. Ask what phase/task Nadim wants to work on
3. Break work into small, verifiable steps
4. Remember: Nemotron is the brain, orchestrator just manages the agentic loop
5. Use `docker-compose -p brain` for all docker commands
6. Explain the "why" not just the "what"
7. Celebrate small wins along the way
