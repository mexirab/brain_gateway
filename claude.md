# Brain Gateway - Project Context for Claude

> **Owner:** Nadim (has ADHD - prefers step-by-step instructions with verification)
> **Goal:** Self-hosted personal AI assistant that rivals Google Assistant but keeps all data private

## Quick Reference

### Hardware Cluster
| Node | IP | GPU | VRAM | RAM | Primary Role |
|------|-----|-----|------|-----|--------------|
| Helios | 10.0.0.195 | RTX 5090 | 32GB | 124GB | Large models (120B), primary inference |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | 34GB | 62GB | **Nemotron-Orchestrator-8B (brain)** on RTX 3090 |
| Uranus | 10.0.0.173 | 2x RTX 5080 | 32GB | 62GB | **TTS (GPU 0)** + **STT (GPU 1)** |
| Jupiter | 10.0.0.248 | RX 6900 XT + RX 6800 | 32GB | 32GB | AMD ROCm (image gen, backup LLM) |
| Voyager | 10.0.0.186 | None | - | 32GB | Gateway, orchestration, Docker host |

### Key Paths
- **Orchestrator:** `/opt/voyager/gateway_mvp/`
- **RAG Documents:** `~/rag/nadim_rag/`
- **ChromaDB:** `~/.local/share/chroma/personal_rag`
- **Brain Gateway .env:** `~/brain_gateway/.env`

### SSH Access
Use `labadmin@[node]` to SSH to any cluster server:
```bash
ssh labadmin@10.0.0.195   # Helios
ssh labadmin@10.0.0.58    # Saturn
ssh labadmin@10.0.0.173   # Uranus
ssh labadmin@10.0.0.248   # Jupiter
```

### Services & Ports
| Service | URL | Purpose |
|---------|-----|---------|
| Brain Gateway | `http://localhost:8888` | Main orchestrator API (v6 hybrid) |
| Open WebUI | `http://localhost` | Chat interface |
| Home Assistant | `http://10.0.0.106:8123` | Smart home control |
| Helios (120B) | `http://10.0.0.195:8080/v1` | **PRIMARY** - Jessica conversational AI |
| Nemotron (8B) | `http://10.0.0.58:8001/v1` | **TOOL ORCHESTRATOR** - HA, RAG, reminders |
| **Qwen3-TTS** | `http://10.0.0.173:8002` | Voice synthesis with Jessica clone (Uranus GPU 0) |
| **Whisper STT** | `http://10.0.0.173:8003/v1` | Speech-to-text (Uranus GPU 1) |
| **Grafana** | `http://localhost:3000` | Monitoring dashboards (admin/braingw) |
| Prometheus | `http://localhost:9090` | Metrics collection |
| Loki | `http://localhost:3100` | Log aggregation |

---

## Architecture Overview (v6 Hybrid)

```
User → Open WebUI → Brain Gateway Orchestrator (v6)
                           │
                           ▼
                    Helios (120B) - "Jessica"
                  Primary Conversational AI
                           │
              ┌────────────┴────────────┐
              │                         │
        Direct Response          ask_orchestrator
     (greetings, chat,              (tool call)
      general knowledge)                │
                                       ▼
                              Nemotron (8B) - Tool Orchestrator
                                       │
                         ┌─────────────┼─────────────┐
                         ▼             ▼             ▼
                   home_assistant  search_memory  set_reminder
                         │             │         update_data
                    HA REST API   ChromaDB RAG     │
                                                   │
                         └─────────────┴───────────┘
                                       │
                                       ▼
                              Result back to Helios
                                       │
                                       ▼
                              Natural response to user
```

**How it works (Hybrid Flow):**
1. User sends request to orchestrator
2. RAG context pre-fetched (unless simple greeting)
3. Helios (Jessica) receives request with personal context
4. For greetings/chat/general knowledge → Helios responds directly
5. For actions (HA, reminders, memory search) → Helios calls `ask_orchestrator`
6. Nemotron receives command, executes tools (HA, RAG, reminders)
7. Result returned to Helios
8. Helios formulates natural response to user

**Why Hybrid?**
- **Problem solved:** Nemotron (8B) was misinterpreting greetings as requests for help
- **Solution:** Helios handles conversation naturally; Nemotron focuses on tool execution
- Each model does what it's best at

**Helios Tool (single tool):**
| Tool | Purpose |
|------|---------|
| `ask_orchestrator` | Delegate actions to Nemotron: device control, memory search, reminders, data updates |

**Nemotron Tools (called via ask_orchestrator):**
| Tool | Purpose |
|------|---------|
| `home_assistant` | Structured HA API calls: `{entity_id, service, data}` |
| `search_memory` | Query ChromaDB RAG for personal context |
| `set_reminder` | Set voice/phone reminders for specific times |
| `update_data` | Update medications or projects |

**Key insight:** Helios receives relevant RAG context pre-fetched in its system prompt, so it can answer personal questions without tools. Only actions require the orchestrator.

---

## Voice Pipeline (Phase 3 + 4)

### Architecture
```
                          URANUS (10.0.0.173)
                    ┌─────────────────────────────────┐
                    │  GPU 0: Qwen3-TTS (port 8002)   │
User Voice ─────────│  GPU 1: Whisper STT (port 8003) │─────────► HA Speaker
                    └─────────────────────────────────┘
                                    │
                                    ▼
                         Orchestrator (Voyager)
                          /api/briefing/morning
                          /api/audio/{id}.wav
```

### Voice Cloning (Jessica McCabe)
The TTS server uses Jessica McCabe's voice (from "How to ADHD") for a warm, ADHD-friendly experience.

**Voice prompt stored in:** `~/tts-voices/voices.json` on Uranus
```json
{
  "jessica": {
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus on anything I was not excited about was like trying to nail jello to the wall.",
    "description": "Jessica McCabe - warm, energetic ADHD advocate"
  }
}
```

**Important:** Uses `Qwen3-TTS-1.7B-Base` model (not CustomVoice) - only Base supports voice cloning.

### Morning Briefing (Phase 4)
Personalized morning announcement via Jessica's voice on HA speakers.

**Flow:**
1. HA automation triggers at 7:30 AM (weekdays)
2. Calls `POST /api/briefing/morning` with `generate_tts: true, play_on: media_player.kitchen_display`
3. Orchestrator searches RAG for morning routine/meds info
4. Nemotron generates personalized briefing
5. TTS generates audio with Jessica's voice
6. Audio saved to `/tmp/brain_audio/{uuid}.wav`
7. HA speaker plays audio via `media_player.play_media` service

**Endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `POST /api/briefing/morning` | Generate and optionally play morning briefing |
| `GET /api/audio/{id}.wav` | Serve generated audio files to HA speakers |

---

## Current State (What's Working)

✅ **Phase 1:** RAG with markdown/text files
✅ **Phase 2:** Home Assistant integration (lights, scenes, media, colors, brightness)
✅ **Phase 3:** Voice interface (Qwen3-TTS with Jessica voice clone + Whisper STT on Uranus)
✅ **Phase 4:** Morning briefing with personalized content (Jessica's voice on HA speakers)
✅ **Phase 4.6:** Hybrid architecture - Helios converses, Nemotron orchestrates tools
🔄 **Phase 4.5:** ATOM Echo with "Hey Jess" wake word (IN PROGRESS - see below)
⬜ **Phase 5:** Mobile access
⬜ **Phase 6:** Fine-tuning

---

## 🔄 WORK IN PROGRESS: ATOM Echo "Hey Jess" Wake Word (2026-01-26)

### Goal
M5Stack ATOM Echo as voice input device with custom "Hey Jess" wake word → Brain Gateway → response on Google speakers via Jessica TTS.

### What's Done
1. ✅ **Trained "hey_jess" wake word model** on Saturn (RTX 3080) using microWakeWord Docker
   - Model: `/opt/voyager/gateway_mvp/ha_automations/hey_jess.tflite`
   - Also copied to HA: `/config/esphome/hey_jess.tflite`
   - JSON manifest: `/config/esphome/hey_jess.json`

2. ✅ **Flashed ATOM Echo** with ESPHome config
   - Device name: `atom-echo-jess`
   - Device IP: `10.0.0.226`
   - Config: `/config/esphome/atom-echo-jess.yaml`

3. ✅ **Wyoming services running on Uranus** (for HA voice pipeline)
   - Wyoming Faster-Whisper STT: `tcp://10.0.0.173:10300`
   - Wyoming Piper TTS: `tcp://10.0.0.173:10301`
   - Started manually (not systemd yet):
     ```bash
     ssh labadmin@10.0.0.173
     nohup ~/.local/bin/wyoming-faster-whisper --model base --language en --device cuda --data-dir ~/wyoming-data --uri tcp://0.0.0.0:10300 > /tmp/wyoming-stt.log 2>&1 &
     nohup ~/.local/bin/wyoming-piper --voice en_US-lessac-medium --data-dir ~/wyoming-piper-data --download-dir ~/wyoming-piper-data --uri tcp://0.0.0.0:10301 --use-cuda > /tmp/wyoming-tts.log 2>&1 &
     ```

4. ✅ **openWakeWord add-on installed** in HA with "Hey Jess" wake word
   - Model copied to `/share/openwakeword/hey_jess.tflite`

5. ✅ **Voice Assistant pipeline configured** in HA
   - Wake word: Hey Jess (openWakeWord)
   - STT: Wyoming Faster-Whisper (10.0.0.173:10300)
   - TTS: Wyoming Piper (10.0.0.173:10301)
   - Conversation agent: OpenAI Conversation → Brain Gateway (10.0.0.186:8888)

### Current Issue
**Audio buffer overflow crashes HA** when ATOM Echo sends audio:
```
[E][voice_assistant:855]: Cannot receive audio, buffer is full
```

The ATOM Echo sends audio faster than HA can consume it. Possible causes:
- Network latency between ATOM Echo (10.0.0.226) and HA (10.0.0.106)
- ESPHome config needs buffer tuning
- WiFi signal strength issues

### Next Steps (Resume Here)

1. **Flash updated ESPHome config** with buffer improvements:
   - Added `bits_per_sample: 16bit` to microphone
   - Added `buffer_duration: 100ms` to speaker
   - Fixed YAML indentation (no leading spaces on top-level keys!)
   - Updated config ready to paste - see `ha_automations/atom_echo.yaml` or ask Claude for it

2. **Check WiFi signal strength** on ATOM Echo device page in HA
   - If below -70 dBm, relocate device or add WiFi extender

3. **Create systemd services** for Wyoming STT/TTS on Uranus (so they auto-start)

4. **Integrate Jessica TTS** instead of Piper
   - Need to create Wyoming wrapper for Qwen3-TTS, or
   - Use HA automation to route TTS to Jessica via REST API

5. **Route TTS to Google speakers** instead of ATOM Echo's weak speaker
   - Use `voice_conversation_automation.yaml` template

### Key Files
| File | Location | Purpose |
|------|----------|---------|
| `hey_jess.tflite` | `/config/esphome/` on HA | Trained wake word model |
| `hey_jess.json` | `/config/esphome/` on HA | Wake word manifest |
| `atom-echo-jess.yaml` | `/config/esphome/` on HA | ESPHome device config |
| `atom_echo_setup.md` | `ha_automations/` | Full setup guide |
| `hey_jess_training.md` | `ha_automations/` | Wake word training guide |

### Services to Check
```bash
# Wyoming STT on Uranus (manually started)
ssh labadmin@10.0.0.173 "ps aux | grep wyoming"

# TTS/STT health
curl http://10.0.0.173:8002/health  # Qwen3-TTS (Jessica)
curl http://10.0.0.173:8003/health  # Whisper STT (OpenAI-compatible)
curl http://10.0.0.173:10300        # Wyoming STT (won't respond to HTTP, use nc)
curl http://10.0.0.173:10301        # Wyoming TTS (won't respond to HTTP, use nc)
```

### Docker on Saturn
Docker was installed on Saturn for wake word training:
```bash
ssh labadmin@10.0.0.58
# microWakeWord container may still be running
docker ps
docker stop microwakeword  # if needed
```

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
| `orchestrator/orchestrator.py` | Main FastAPI app (v6.0 hybrid), Helios+Nemotron routing |
| `orchestrator/ha_integration.py` | HA entity discovery + `call_service()` API relay |
| `rag/ingest_rag.py` | Index documents into ChromaDB |
| `docker-compose.yml` | Service stack definition |
| `.env` | API tokens (HA_TOKEN, YNAB_TOKEN, etc.) |
| `monitoring/docker-compose.yml` | Grafana/Prometheus/Loki stack |
| `monitoring/README.md` | Monitoring setup instructions |
| `monitoring/lab_hw_audit.sh` | Hardware audit script for cluster |
| `tts/server.py` | Qwen3-TTS server with voice cloning (runs on Uranus) |
| `tts/stt_server.py` | Whisper STT server (runs on Uranus GPU 1) |
| `tts/qwen-tts.service` | Systemd service for TTS |
| `tts/whisper-stt.service` | Systemd service for STT |
| `scripts/morning_briefing.sh` | Shell script to trigger morning briefing |
| `ha_automations/morning_briefing.yaml` | HA automation template for morning briefing |

**Important implementation details:**
- **Hybrid architecture (v6):** Helios uses native tool calling (`tool_choice: "auto"`), Nemotron uses XML-style (`tool_choice: "none"`)
- Helios gets RAG context pre-fetched in system prompt for personal questions
- Tool results from Nemotron are fed back to Helios for natural response formatting
- Fallback: If Helios is offline, orchestrator falls back to Nemotron-only mode
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

# Hybrid flow logs (Helios → Nemotron)
{container="brain-orchestrator"} |~ "HYBRID|NEMOTRON|TOOL"

# Tool calls only
{container="brain-orchestrator"} |~ "home_assistant|search_memory|ask_orchestrator|set_reminder|update_data"

# Errors only
{container="brain-orchestrator"} |~ "(?i)error|exception|failed"
```

### Hardware audit across cluster
```bash
/opt/voyager/gateway_mvp/monitoring/lab_hw_audit.sh
```

### Test morning briefing
```bash
curl -X POST http://localhost:8888/api/briefing/morning \
  -H "Content-Type: application/json" \
  -d '{"generate_tts": true, "play_on": "media_player.kitchen_display"}'
```

### Test TTS with Jessica voice
```bash
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning Nadim!", "voice": "jessica"}' \
  --output test.wav
```

### Manage TTS/STT services on Uranus
```bash
# Check status
ssh labadmin@10.0.0.173 "sudo systemctl status qwen-tts whisper-stt"

# Restart services
ssh labadmin@10.0.0.173 "sudo systemctl restart qwen-tts whisper-stt"

# View logs
ssh labadmin@10.0.0.173 "journalctl -u qwen-tts -f"
ssh labadmin@10.0.0.173 "journalctl -u whisper-stt -f"
```

### Load a new voice clone
```bash
curl -X POST http://10.0.0.173:8002/voices/load \
  -H "Content-Type: application/json" \
  -d '{
    "name": "jessica",
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus on anything I was not excited about was like trying to nail jello to the wall.",
    "description": "Jessica McCabe - warm, energetic ADHD advocate"
  }'
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
