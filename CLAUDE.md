# Brain Gateway

Personal AI assistant for ADHD support. v7 unified mode: single model (Qwen3.5-27B on Helios) handles both conversation and tools. v6 hybrid mode still available as fallback (Nemotron-8B orchestrates tools; Helios handles conversation).

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Jupiter | 10.0.0.248 | 100.102.29.14 | - | Gateway, Docker host |
| Saturn | 10.0.0.58 | - | RTX 3080 + RTX 3090 | Nemotron-8B (fallback brain), Pi-hole secondary |
| Uranus | 10.0.0.173 | - | 2x RTX 5080 | TTS (GPU0), STT (GPU1) |
| Helios | 10.0.0.195 | - | RTX 5090 | Qwen3.5-27B unified (conversation + tools), auto-starts on demand |
| HA | 10.0.0.106 | - | - | Home Assistant |
| Callisto | 10.0.0.136 | - | - | Monitoring kiosk display (Pi 4) |

## Services

| Service | Port | URL |
|---------|------|-----|
| Open WebUI (HTTPS) | 443 | https://jupiter-amds.tail74fc4a.ts.net (Tailscale) |
| Open WebUI (HTTP) | 80 | http://localhost |
| Orchestrator | 8888 | http://localhost:8888 |
| Nemotron | 8001 | http://10.0.0.58:8001/v1 |
| Helios | 8080 | http://10.0.0.195:8080/v1 |
| TTS | 8002 | http://10.0.0.173:8002 |
| STT | 8003 | http://10.0.0.173:8003 |
| Pi-hole (Jupiter) | 53/8053 | http://10.0.0.248:8053/admin |
| Pi-hole (Saturn) | 53/8053 | http://10.0.0.58:8053/admin |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.248:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.248:10301 |
| Frontend (dashboard) | 3001 | http://10.0.0.248:3001 (future: convivialprophet.com) |
| SearXNG | 8090 | http://localhost:8090 |
| Grafana | 3000 | http://localhost:3000/d/brain-gateway-overview (admin/braingw) |

## Architecture (v7 Unified)

```
User -> Open WebUI -> Orchestrator -> Unified Loop -> Model (Qwen3.5-27B)
                                                         |
                                          conversation + tool calls in one loop
                                                         |
                    +----------+----------+----+----+----------+----------+
                    v          v          v    v    v          v          v
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow (v7):** Single model handles conversation and tool execution in one agentic loop. No delegation between models. Falls back to Saturn (Nemotron) if Helios is unavailable. Controlled by `UNIFIED_MODE=true`.

### Architecture (v6 Hybrid — legacy)

```
User -> Open WebUI -> Orchestrator -> Mode Router -> Helios (conversation)
                                   (intent+intensity)      |
                                                +----------+----------+
                                           Direct response      ask_orchestrator
                                                                      |
                                                               Nemotron (tools)
                                                                      |
                    +----------+----------+----+----+----------+----------+
                    v          v          v    v    v          v          v
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow (v6):** Mode router classifies intent + emotional intensity. Helios handles conversation. For actions, calls `ask_orchestrator` -> Nemotron executes tools -> result back to Helios. Active when `UNIFIED_MODE=false`.

## Tools

In **v7 unified mode**, all tools are called directly by the single model (no `ask_orchestrator` delegation). In **v6 hybrid mode**, Helios delegates to Nemotron via `ask_orchestrator`.

| Tool | v6 Model | Purpose |
|------|----------|---------|
| ask_orchestrator | Helios (v6 only) | Delegate to Nemotron for actions |
| home_assistant | Nemotron / unified | HA API: `{entity_id, service, data}` |
| search_memory | Nemotron / unified | ChromaDB RAG query |
| set_reminder / cancel_reminder | Nemotron / unified | Voice/phone reminders |
| update_data | Nemotron / unified | Update meds/projects YAML |
| start_focus / stop_focus / focus_status | Nemotron / unified | Pomodoro timer + Endel + Pi-hole |
| web_search | Nemotron / unified | Search the web via SearXNG |
| check_calendar / create_calendar_event | Nemotron / unified | Google Calendar read/write |
| check_email / search_email | Nemotron / unified | Gmail inbox (read-only) |
| brain_dump | Nemotron / unified | Capture & route thoughts/tasks/ideas to RAG or reminders |

## Key Files

| File | Purpose |
|------|---------|
| orchestrator/auto_learn.py | Auto-learn: extract facts from conversations, encrypt, store in RAG |
| orchestrator/brain_dump_manager.py | Brain dump: capture, categorize, dedup, and route items to RAG or reminders |
| orchestrator/tests/test_auto_learn.py | Auto-learn unit tests (sensitive data, privacy, JSON parsing, encryption) |
| orchestrator/tests/test_brain_dump.py | Brain dump unit tests (routing, dedup, validation, error handling) |
| orchestrator/unified_loop.py | v7 unified agentic loop: single model conversation + tool execution |
| orchestrator/model_manager.py | Model health, SSH start/stop, fallback selection (replaces helios_manager for v7) |
| orchestrator/orchestrator.py | FastAPI app, main chat endpoint, startup/shutdown |
| orchestrator/shared.py | Module-level shared state (http client, scheduler, config) |
| orchestrator/tool_definitions.py | Tool JSON schemas (STATIC_TOOLS, HELIOS_TOOLS, HA tool builder) |
| orchestrator/prompt_builder.py | System prompts for Helios/Nemotron, RAG context, helpers |
| orchestrator/helios_manager.py | Helios health check, SSH start/stop, idle tracking |
| orchestrator/nemotron_loop.py | Agentic tool loop, XML parsing, dedup |
| orchestrator/focus_manager.py | Pomodoro timer, Endel audio, Pi-hole blocking |
| orchestrator/tool_handlers.py | execute_tool dispatcher + all tool_* functions |
| orchestrator/api_routes.py | Secondary REST endpoints (health, metrics, memory, reminders, focus) |
| orchestrator/background_jobs.py | Calendar polling, morning briefing, email polling, temperature alerts |
| orchestrator/ha_integration.py | HA entity discovery + call_service() |
| orchestrator/mode_router.py | Intent-based mode router |
| orchestrator/google_calendar.py | Google Calendar API v3 client |
| orchestrator/google_gmail.py | Gmail API v1 client (read-only) |
| orchestrator/pihole_client.py | Pi-hole v6 multi-instance client |
| orchestrator/travel_time.py | Google Maps Directions API client |
| orchestrator/metrics.py | Prometheus metrics (bgw_* namespace) |
| scripts/reindex_rag.py | Re-index RAG documents into ChromaDB |
| docker-compose.yml | Service stack |
| .env | Environment config (from .env.example) |

## Key Paths

```
/opt/jupiter/gateway_mvp/           # Project root on Jupiter
~/.env                              # Secrets (HA_TOKEN, LITELLM_KEY)
~/rag/nadim_rag/                    # RAG source documents (154 docs indexed)
~/.local/share/chroma/personal_rag/ # ChromaDB persistence
/opt/jupiter/gateway_mvp/credentials/  # Google OAuth2 creds (gitignored)
/opt/jupiter/gateway_mvp/certs/     # Tailscale TLS certs (gitignored)
```

## Common Commands

```bash
# Start/rebuild
docker compose up -d
docker compose up -d --build orchestrator

# Logs
docker logs brain-orchestrator --tail 50 -f

# Health
curl http://localhost:8888/health

# Remote deploy from Mac (via Tailscale)
ssh labadmin@100.102.29.14 "cd /opt/jupiter/gateway_mvp && git pull && docker compose up -d --build orchestrator"

# Frontend rebuild
docker compose up -d --build --force-recreate frontend

# Re-index RAG documents
docker exec brain-orchestrator python scripts/reindex_rag.py
```

## Detailed Docs

| Doc | What |
|-----|------|
| **docs/FOCUS_AND_PIHOLE.md** | Focus timer (Pomodoro), Pi-hole DNS blocking, Nebula Sync |
| **docs/VOICE_AND_TTS.md** | ATOM Echo voice assistant, TTS pacing, Wyoming bridges |
| **docs/GOOGLE_INTEGRATIONS.md** | Calendar, Gmail, phone sync, travel-time alerts, OAuth2 setup |
| **docs/FRONTEND.md** | Dashboard pages, widgets, YNAB finance, API proxy pattern |
| **docs/MODE_ROUTER.md** | Intent classification modes (explainer/mirror/counterbalance/challenge/baseline) |
| **docs/INFRASTRUCTURE.md** | HTTPS/Tailscale, RAG, temperature monitoring, performance notes, kiosk |
| **ARCHITECTURE.md** | Internals, data flow, troubleshooting |
| **COMMANDS.md** | Command quick reference |
| **TECHNICAL_REFERENCE.md** | API specs, schemas |
| **ROADMAP.md** | Feature roadmap and what's done/planned |
| **monitoring/README.md** | Monitoring setup |
| **jess-features/README.md** | ADHD feature roadmap (F-001 through F-010) — build order, dependencies, per-feature implementation specs |

## Jess Feature Specs

ADHD-informed feature specs live in `jess-features/`. Each file is a self-contained implementation spec with interaction examples, tool schemas, modified files, TTS templates, env vars, and testing checklists. Read `jess-features/README.md` for the build order and dependency graph, then load individual feature files as needed:

| File | Feature | Priority |
|------|---------|----------|
| `jess-features/F-001-brain-dump.md` | Voice-First Brain Dump | P0 |
| `jess-features/F-002-time-nudges.md` | Proactive Time-Aware Nudges | P0 |
| `jess-features/F-003-task-decomposition.md` | Task Decomposition Engine | P0 |
| `jess-features/F-004-body-doubling.md` | Body Doubling & Focus Sessions | P1 |
| `jess-features/F-005-progress-tracking.md` | Dopamine-Aware Progress Tracking | P1 |
| `jess-features/F-006-routine-scaffolding.md` | Context-Aware Routine Scaffolding | P1 |
| `jess-features/F-007-interruption-recovery.md` | Interruption Recovery | P1 |
| `jess-features/F-008-selfcare-nudges.md` | Meal & Self-Care Nudges | P2 |
| `jess-features/F-009-decision-simplifier.md` | Decision Simplifier | P2 |
| `jess-features/F-010-ambient-awareness.md` | Ambient Awareness Mode | P2 |

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_mvp` (default from directory name)
- Helios auto-starts on demand via SSH, auto-stops after 30 min idle (~150W savings)
- TTS uses Jessica McCabe voice clone (Qwen3-TTS) with sentence pause injection
- Jupiter SSH: `labadmin@100.102.29.14` (Tailscale) or `labadmin@10.0.0.248` (LAN)
- Uranus SSH (from Jupiter): `ssh labadmin@10.0.0.173`

## Unified Mode Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| UNIFIED_MODE | false | Enable v7 unified loop (single model for conversation + tools) |
| MODEL_URL | - | Primary model endpoint (e.g. `http://10.0.0.195:8080/v1`) |
| MODEL_NAME | - | Primary model name (e.g. `Qwen3.5-27B`) |
| FALLBACK_MODEL_URL | - | Fallback model endpoint (e.g. `http://10.0.0.58:8001/v1`) |
| FALLBACK_MODEL_NAME | - | Fallback model name (e.g. `Nemotron-8B`) |
| EMBEDDING_MODEL | - | Embedding model for RAG indexing |
| MODEL_SERVER_IP | - | SSH target for remote model start/stop |
| MODEL_SSH_USER | - | SSH user for model server |
| MODEL_START_CMD | - | Command to start model server via SSH |
| MODEL_STOP_CMD | - | Command to stop model server via SSH |

## Auto-Learn Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| AUTO_LEARN_ENABLED | true | Enable/disable background fact extraction |
| AUTO_LEARN_DELAY_MINUTES | 10 | Delay after conversation before extracting facts |
| AUTO_LEARN_MAX_FACTS | 5 | Max facts extracted per conversation |
| AUTO_LEARN_DEDUP_THRESHOLD | 0.85 | Cosine similarity threshold for deduplication |
| AUTO_LEARN_MARKDOWN | false | Also append facts to a markdown file |
| AUTO_LEARN_ENCRYPT | true | Encrypt stored facts at rest (Fernet) |
| AUTO_LEARN_ENCRYPTION_KEY | (auto-generated) | Fernet key; auto-generated if empty |

## Auto-Learn API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/memory/learned | List learned facts (decrypted), optional `?category=` and `?limit=` |
| DELETE | /api/memory/learned/{doc_id} | Delete a single learned fact |
| DELETE | /api/memory/learned?confirm=true | Wipe all learned facts |
| GET | /api/memory/learned/stats | Auto-learn statistics (counts by category) |
| POST | /api/memory/learned/toggle | Enable/disable auto-learn at runtime |
