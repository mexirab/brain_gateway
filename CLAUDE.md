# Brain Gateway

Personal AI assistant for ADHD support. Primary model (Qwen3-VL-30B-A3B abliterated on Helios RTX PRO 5000) handles conversation, tools, and vision in one unified agentic loop. v6 hybrid mode has been removed.

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Jupiter | 10.0.0.248 | 100.102.29.14 | - | Gateway, Docker host |
| Saturn | 10.0.0.58 | - | RTX 3080 + RTX 3090 | Vision model (RTX 3080), Pi-hole secondary |
| Uranus | 10.0.0.173 | - | 2x RTX 5080 | ComfyUI/Conjure (GPU1) |
| Helios | 10.0.0.195 | - | RTX 5090 + RTX PRO 5000 | Primary LLM: Qwen3-VL-30B-A3B abliterated (PRO 5000, port 8081), TTS + STT (5090), always-on |
| HA | 10.0.0.106 | - | - | Home Assistant |
| Callisto | 10.0.0.136 | - | - | Monitoring kiosk display (Pi 4) |

## Services

| Service | Port | URL |
|---------|------|-----|
| Open WebUI (HTTPS) | 443 | https://jupiter-amds.tail74fc4a.ts.net (Tailscale) |
| Open WebUI (HTTP) | 80 | http://localhost |
| Orchestrator | 8888 | http://localhost:8888 |
| Helios primary (Qwen3-VL-30B-A3B abliterated) | 8081 | http://10.0.0.195:8081/v1 |
| TTS (Qwen3-TTS) | 8002 | http://10.0.0.195:8002 |
| STT (Whisper) | 8003 | http://10.0.0.195:8003 |
| Pi-hole (Jupiter) | 53/8053 | http://10.0.0.248:8053/admin |
| Pi-hole (Saturn) | 53/8053 | http://10.0.0.58:8053/admin |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.248:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.248:10301 |
| Vision Model (Qwen2.5-VL-7B) | 8010 | http://10.0.0.58:8010 |
| Frontend (dashboard) | 3001 | http://10.0.0.248:3001 (future: convivialprophet.com) |
| SearXNG | 8090 | http://localhost:8090 |
| Grafana | 3000 | http://localhost:3000/d/brain-gateway-overview (admin/braingw) |

## Architecture (v7 Unified)

```
User -> Open WebUI -> Orchestrator -> Unified Loop -> Model (Qwen3-VL-30B-A3B abliterated)
                                                         |
                                          conversation + tool calls in one loop
                                                         |
                    +----------+----------+----+----+----------+----------+
                    v          v          v    v    v          v          v
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow:** Single model handles conversation and tool execution in one agentic loop. No delegation between models. Helios is always-on (no auto-shutdown).

**Infrastructure:** `config.py` centralizes all env vars via Pydantic Settings. `tool_registry.py` provides decorator-based tool registration (replaces legacy if-elif dispatch). `service_registry.py` auto-detects healthy services and disables tools when dependencies are down. `exceptions.py` defines a typed exception hierarchy for consistent error handling.

## Tools

All tools are called directly by the single model in one agentic loop.

| Tool | Purpose |
|------|---------|
| home_assistant | HA API: `{entity_id, service, data}` |
| search_memory | ChromaDB RAG query |
| set_reminder / cancel_reminder | Voice/phone reminders |
| update_data | Update meds/projects YAML |
| start_focus / stop_focus / focus_status | Focus sessions: sprints, check-ins, ambient audio, Pi-hole blocking |
| focus_sprint | Continue next sprint, extend current, or end session with summary |
| web_search | Search the web via SearXNG |
| check_calendar / create_calendar_event | Google Calendar read/write |
| check_email / search_email | Gmail inbox (read-only) |
| decompose_task / task_step | Break tasks into micro-steps, advance through them |
| start_routine / routine_action / routine_status | Step-by-step morning/evening routines with TTS guidance |
| decide_for_me | Decision simplifier: gathers context for 1-2 concrete recommendations |
| selfcare_log | Log meals, meds, water, movement for self-care nudge tracking |
| bookmark_context / recall_context | Interruption recovery: save and recall work context |
| brain_dump | Capture & route thoughts/tasks/ideas to RAG or reminders |
| check_system | System diagnostics: logs, health, recent errors |
| finance_status | Budget, spending, XP/levels from YNAB integration |
| analyze_image | Re-analyze or ask follow-up questions about a shared image |
| shopping_list | Add/check/remove items from shopping/grocery lists |
| sleep_mode | Do Not Disturb: suppress all announcements until morning |

## Key Files

| File | Purpose |
|------|---------|
| orchestrator/auto_learn.py | Auto-learn: extract facts from conversations, encrypt, store in RAG |
| orchestrator/brain_dump_manager.py | Brain dump: capture, categorize, dedup, and route items to RAG or reminders |
| orchestrator/tests/test_auto_learn.py | Auto-learn unit tests (sensitive data, privacy, JSON parsing, encryption) |
| orchestrator/tests/test_brain_dump.py | Brain dump unit tests (routing, dedup, validation, error handling) |
| orchestrator/unified_loop.py | v7 unified agentic loop: single model conversation + tool execution |
| orchestrator/model_manager.py | Model health, SSH start/stop |
| orchestrator/orchestrator.py | FastAPI app, main chat endpoint, startup/shutdown |
| orchestrator/config.py | Centralized Pydantic Settings: all env vars in one place |
| orchestrator/db.py | Shared SQLite context manager (used by all DB consumers) |
| orchestrator/exceptions.py | Exception hierarchy: BrainGatewayError, TransientError, ToolError, etc. |
| orchestrator/service_registry.py | Service health tracking: auto-detect healthy services, disable tools when down |
| orchestrator/tool_registry.py | Decorator-based tool registration (replaces legacy if-elif dispatch) |
| orchestrator/focus_state.py | FocusSession dataclass (replaces raw dict in shared.py) |
| orchestrator/shared.py | Module-level shared state (imports from config.py, exports backward-compat aliases) |
| orchestrator/tool_definitions.py | Tool JSON schemas (STATIC_TOOLS, HA tool builder) |
| orchestrator/prompt_builder.py | System prompt builder, RAG context, helpers |
| orchestrator/ambient_manager.py | Ambient awareness: aggregated status, periodic TTS summaries, LED control |
| orchestrator/selfcare_manager.py | Self-care nudges: meals, meds, hydration, movement — persisted to SQLite, phone notifications |
| orchestrator/tests/test_selfcare_manager.py | Self-care manager unit tests (logging, nudge timing, quiet hours) |
| orchestrator/context_tracker.py | Interruption recovery: context stack, bookmarks, auto-capture, check-in timer |
| orchestrator/tests/test_context_tracker.py | Context tracker unit tests (bookmarks, recall, prompt context) |
| orchestrator/routine_manager.py | Routine scaffolding: morning/evening step-by-step TTS guidance, nudges, calendar awareness |
| orchestrator/tests/test_routine_manager.py | Routine manager unit tests (session lifecycle, steps, nudges, pause/resume) |
| orchestrator/progress_tracker.py | Progress tracking: daily stats, streaks, personal bests, daily/weekly TTS summaries |
| orchestrator/tests/test_progress_tracker.py | Progress tracker unit tests (events, streaks, summaries, personal bests) |
| orchestrator/task_decomposition.py | Task decomposition: break tasks into micro-steps with ADHD time buffer |
| orchestrator/focus_manager.py | Pomodoro timer, Endel audio, Pi-hole blocking, body doubling sprints |
| orchestrator/tool_handlers.py | execute_tool dispatcher + all tool_* functions (uses tool_registry, legacy if-elif removed) |
| orchestrator/api_routes.py | REST endpoints: thin facade, imports from domain route modules |
| orchestrator/routes_calendar.py | Calendar API routes (split from api_routes.py) |
| orchestrator/routes_chat.py | Chat conversation API routes |
| orchestrator/routes_documents.py | Document vault API routes |
| orchestrator/routes_shopping.py | Shopping list API routes |
| orchestrator/routes_vision.py | Vision/STT/TTS API routes |
| orchestrator/background_jobs.py | Background jobs: thin re-export facade, imports from domain job modules |
| orchestrator/jobs_calendar.py | Calendar/email/weather background jobs (split from background_jobs.py) |
| orchestrator/jobs_finance.py | YNAB sync background jobs |
| orchestrator/jobs_monitoring.py | Temperature/ambient/selfcare background jobs |
| orchestrator/ha_integration.py | HA entity discovery + call_service() |
| orchestrator/mode_router.py | Intent-based mode router |
| orchestrator/google_calendar.py | Google Calendar API v3 client |
| orchestrator/google_gmail.py | Gmail API v1 client (read-only) |
| orchestrator/pihole_client.py | Pi-hole v6 multi-instance client |
| orchestrator/reminder_manager.py | TTS announcements, reminders, announcement history, DND gate, multi-phone notifications |
| orchestrator/state_store.py | SQLite persistence: reminders, focus sessions, notifications, announcements, selfcare, shopping |
| orchestrator/cloud_brain.py | CloudBrain class: model routing, fallback, unified loop orchestration |
| orchestrator/llm_backend.py | LLM backend initialization and configuration |
| orchestrator/data_manager.py | YAML data management: medications, projects |
| orchestrator/finance_manager.py | YNAB integration, budget tracking, XP/levels |
| orchestrator/schemas.py | Pydantic models for API request/response validation |
| orchestrator/vision_handler.py | Vision model client: image analysis, OCR, scene understanding |
| orchestrator/tests/test_vision_handler.py | Vision handler unit tests |
| orchestrator/web_search.py | SearXNG search client |
| orchestrator/system_diagnostics.py | System health checks, log analysis |
| orchestrator/fast_path.py | Fast path handler for simple commands (lights, greetings) |
| orchestrator/user_profile.py | User profile loader from YAML |
| orchestrator/log_config.py | Structured JSON logging configuration |
| orchestrator/travel_time.py | Google Maps Directions API client |
| orchestrator/metrics.py | Prometheus metrics (bgw_* namespace) |
| scripts/reindex_rag.py | Re-index RAG documents into ChromaDB |
| scripts/setup.sh | Interactive setup wizard: generates .env + user_profile.yaml |
| scripts/setup-jupiter-claude.sh | One-time Jupiter Claude Code setup (hooks, ruff, permissions) |
| docker-compose.yml | Service stack (env-var driven, no hardcoded IPs) |
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
# First-time setup (generates .env + user_profile.yaml)
bash scripts/setup.sh

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

# Run tests (inside Docker — full deps available)
docker exec brain-orchestrator pip install pytest pytest-asyncio -q
docker cp orchestrator/tests brain-orchestrator:/app/tests
docker exec brain-orchestrator python -m pytest tests/ -v

# Run specific test file
docker exec brain-orchestrator python -m pytest tests/test_progress_tracker.py -v
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
| **docs/REMOTE_DEV.md** | Remote dev workflow (mosh + tmux on Jupiter, jdev alias, git sync) |
| **ARCHITECTURE.md** | Internals, data flow, troubleshooting |
| **COMMANDS.md** | Command quick reference |
| **TECHNICAL_REFERENCE.md** | API specs, schemas |
| **ROADMAP.md** | Feature roadmap and what's done/planned |
| **monitoring/README.md** | Monitoring setup |
| **docs/JESS_QUICK_START.md** | One-page user guide: everything Jess can do |
| **jess-features/README.md** | ADHD feature specs (F-001 through F-010) — all 10 complete |

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
- Helios is always-on (no auto-shutdown); can be manually started/stopped via SSH
- TTS uses Jessica McCabe voice clone (Qwen3-TTS) with sentence pause injection
- Jupiter SSH: `labadmin@100.102.29.14` (Tailscale) or `labadmin@10.0.0.248` (LAN)
- Uranus SSH (from Jupiter): `ssh labadmin@10.0.0.173`

## Model Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| MODEL_URL | - | Primary model endpoint (e.g. `http://10.0.0.195:8080/v1`) |
| MODEL_NAME | - | Primary model name (e.g. `Qwen3.5-27B`) |
| FALLBACK_MODEL_URL | - | Fallback model endpoint (optional) |
| FALLBACK_MODEL_NAME | - | Fallback model name (optional) |
| EMBEDDING_MODEL | - | Embedding model for RAG indexing |
| MODEL_SERVER_IP | - | SSH target for remote model start/stop |
| MODEL_SSH_USER | - | SSH user for model server |
| MODEL_START_CMD | - | Command to start model server via SSH |
| MODEL_STOP_CMD | - | Command to stop model server via SSH |

## Ambient Awareness Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| AMBIENT_ENABLED | true | Enable ambient summaries |
| AMBIENT_SUMMARY_TIMES | 10:00,12:00,14:00,16:00 | TTS summary times |
| AMBIENT_LED_ENTITY | (empty) | HA entity for LED status (disabled if empty) |
| AMBIENT_SPEAKER | (empty) | Speaker for ambient summaries (default if empty) |

## Ambient Awareness API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/ambient/status | Aggregated ambient status (schedule, focus, tasks, LED color) |

## Announcement History API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/announcements/history?limit=50&type= | Recent announcement history (text, speaker, success, latency) |
| GET | /api/announcements/stats | Success rates, per-speaker breakdown, avg latency, today's count |

## Chat History API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/chat/conversations?limit=50 | List conversations (most recent first) |
| POST | /api/chat/conversations | Create conversation: `{title}` |
| GET | /api/chat/conversations/:id/messages | Get conversation + messages |
| POST | /api/chat/conversations/:id/messages | Save message: `{role, content, routing?, announcement_type?}` |
| PUT | /api/chat/conversations/:id | Update title: `{title}` |
| DELETE | /api/chat/conversations/:id | Delete conversation + messages |

## Voice API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/stt/transcribe | Proxy audio to Whisper STT (multipart, max 10MB) |
| POST | /api/tts/synthesize | Synthesize text to WAV: `{text}` |

## Self-Care Nudge Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| SELFCARE_ENABLED | true | Enable self-care nudges |
| MEAL_NUDGE_HOURS | 4 | Hours since last meal before nudging |
| HYDRATION_INTERVAL | 90 | Minutes between water reminders |
| MOVEMENT_INTERVAL | 90 | Minutes between movement reminders |
| QUIET_HOURS_START | 22:00 | No nudges after this |
| QUIET_HOURS_END | 07:00 | No nudges before this |

## Interruption Recovery Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| INTERRUPT_CHECKIN_DELAY | 5 | Minutes after interruption before TTS check-in |
| CONTEXT_STACK_SIZE | 10 | Max rolling context entries |

## Routine Scaffolding Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| ROUTINES_YAML_PATH | /app/data/routines.yaml | Routine definitions file |
| ROUTINE_ENABLED | true | Enable scheduled routine triggers |
| ROUTINE_NUDGE_MAX | 3 | Max nudges per step before auto-skip option |
| ROUTINE_AUTO_SKIP | false | Auto-skip after max nudges (default: wait for user) |

## Progress Tracking Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| PROGRESS_ENABLED | true | Enable progress tracking |
| PROGRESS_DB_PATH | /app/data/progress.db | SQLite database path |
| DAILY_SUMMARY_TIME | 18:00 | When to announce daily summary via TTS |
| WEEKLY_SUMMARY_DAY | sunday | Day for weekly digest |
| WEEKLY_SUMMARY_TIME | 19:00 | Time for weekly digest |

## Progress Tracking API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/progress/today | Today's stats (tasks, focus, brain dumps) |
| GET | /api/progress/week | This week's stats + trend vs prior week |
| GET | /api/progress/streaks | Active streaks (task, focus, brain dump) |

## Vision / Image Recognition Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| VISION_ENABLED | true | Enable/disable image analysis feature |
| VISION_MODEL_URL | http://10.0.0.58:8010/v1 | Vision model endpoint (Qwen2.5-VL-7B on Saturn RTX 3080) |
| VISION_MODEL_NAME | Qwen2.5-VL-7B-Instruct-q4_k_m.gguf | Vision model identifier |
| VISION_MAX_IMAGE_SIZE | 10485760 | Maximum image upload size in bytes (10MB) |
| VISION_TIMEOUT | 60 | Vision model request timeout in seconds |

## Vision API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/vision/analyze | Analyze an image (multipart form or JSON with base64) |
| GET | /api/vision/status | Vision model health and configuration |

## Notification Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| WEBUI_URL | (empty) | Deep link URL for notifications (opens Open WebUI on tap) |

## Shopping List API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | /api/shopping?list_name=&include_checked= | Get shopping list items |
| POST | /api/shopping | Add item: `{item, list_name}` |
| POST | /api/shopping/{id}/check | Check off item |
| POST | /api/shopping/{id}/uncheck | Uncheck item |
| DELETE | /api/shopping/checked?list_name= | Clear all checked items |
| DELETE | /api/shopping/{id} | Delete item |

## Sleep Mode (Do Not Disturb)

Say "goodnight" or "bedtime" → `sleep_mode` tool activates DND. All TTS announcements
and phone notifications are suppressed. State persists to SQLite across restarts.
Auto-clears when morning briefing fires (5-11am). Say "good morning" to manually disable.

## Weather in Morning Briefing

Morning briefing includes weather forecast from the National Weather Service API.
Home address is geocoded via Nominatim (free, no API key). Override with env vars:

| Variable | Default | Purpose |
|----------|---------|---------|
| WEATHER_LAT | (geocoded) | Latitude for weather forecast |
| WEATHER_LON | (geocoded) | Longitude for weather forecast |

## DB Maintenance

Weekly scheduled job (Sundays 3am): cleans announcements (>30 days), selfcare logs
(>90 days), and runs VACUUM. Also runs cleanup on startup.

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
