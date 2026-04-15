# Brain Gateway

Personal AI assistant for ADHD support. Primary model (Qwen3.5-27B on Helios RTX PRO 5000) handles conversation and tools in one unified agentic loop. v6 hybrid mode has been removed.

## Post-change review workflow (MANDATORY)

After completing any code change in this repo — before reporting the task as done — run the following pipeline. Do not skip this because the change "looks small"; small changes are where slop accumulates.

The pipeline has two phases. Phase 1 agents run in parallel (they're all read-only reviewers). Phase 2 agents run sequentially because each one can add new work the next needs to see.

### Phase 1 — Parallel review (invoke in ONE message with multiple Agent tool calls)

**Always run:**
- `code-reviewer` — Python/FastAPI correctness, async, types, architecture, ruff
- `security` — secrets, input validation, LLM/tool abuse surface, data protection
- `prod-support` — logging, metrics, health impact, SRE-visible consequences

**Conditionally add:**
- `frontend` — if any file under `frontend/` was touched, or a backend route shape changed that a page consumes
- `hacker` — if a route was added/modified, auth/input-handling changed, a tool schema changed, or anything under `tool_handlers.py` / `api_routes.py` / `routes_*.py` was touched. The orchestrator must be running on `localhost:8888` for this to work; if it's not, say so and skip.

Fix any HIGH severity or EXPLOITABLE finding before moving to Phase 2. Surface medium/low findings to the user with a one-line recommendation each.

### Phase 2 — Sequential follow-up

1. **`unit-test`** — after Phase 1 passes, for any new/modified function, tool handler, route, or background job. Writes and runs tests inside the `brain-orchestrator` container. If tests fail, fix the code (or the test if the test was wrong) and re-run. Do not move on with failing tests.
2. **`docs-updater`** — the FINAL step. Updates `CLAUDE.md` and `docs/` to reflect new files, tools, env vars, endpoints, or removed functionality. Runs last so it captures everything Phase 1 and Phase 2 changed.

### Invocation rules (applies to every agent call)

- Send a single message with parallel Agent calls for Phase 1.
- Each prompt must be self-contained: name the specific files and line ranges, paste the diff, state what the change is trying to accomplish. Subagents have no access to this conversation — don't say "review my changes."
- Never hide or soften a finding when reporting to the user.

Manual on-demand equivalent: `/review-change` (runs Phase 1 only; invoke `unit-test` and `docs-updater` explicitly afterward if needed).

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Helios | 10.0.0.195 | helios.tail74fc4a.ts.net | RTX 5090 + RTX PRO 5000 | **Brain gateway + Docker host**, Primary LLM: Qwen3.5-27B (GPU1 RTX PRO 5000, port 8080), TTS + STT (GPU1), Code agent: Qwen2.5-Coder-32B (GPU0 RTX 5090, port 8082), always-on |
| Jupiter | 10.0.0.248 | jupiter.tail74fc4a.ts.net | - | **Pi-hole primary + Monitoring host** (Prometheus, Grafana, Loki, Promtail, Blackbox exporter), nebula-sync, Conjure API |
| Saturn | 10.0.0.58 | - | RTX 3080 (10GB) + RTX 3090 (24GB) | Vision model (RTX 3080, Qwen3-VL-8B-Instruct Q4_K_M), Pi-hole secondary. RTX 3090 currently idle. |
| Uranus | 10.0.0.173 | - | 2x RTX 5080 | ComfyUI/Conjure (GPU1) |
| HA | 10.0.0.106 | - | - | Home Assistant |
| Callisto | 10.0.0.136 | - | - | Monitoring kiosk display (Pi 4) |

## Services

| Service | Port | URL |
|---------|------|-----|
| Open WebUI (HTTPS) | 443 | https://helios.tail74fc4a.ts.net (Tailscale, tailnet-only) |
| Open WebUI (HTTP) | 80 | http://10.0.0.195 |
| Orchestrator | 8888 | http://10.0.0.195:8888 |
| Primary LLM (Qwen3.5-27B) | 8080 | http://10.0.0.195:8080/v1 |
| Code agent (Qwen2.5-Coder-32B) | 8082 | http://10.0.0.195:8082/v1 |
| TTS (Qwen3-TTS) | 8002 | http://10.0.0.195:8002 |
| STT (Whisper) | 8003 | http://10.0.0.195:8003 |
| Pi-hole (Jupiter primary) | 53/8053 | http://10.0.0.248:8053/admin |
| Pi-hole (Saturn secondary) | 53/8053 | http://10.0.0.58:8053/admin |
| Grafana (Jupiter) | 3000 | http://10.0.0.248:3000/d/brain-gateway-overview |
| Prometheus (Jupiter) | 9090 | http://10.0.0.248:9090 |
| Loki (Jupiter) | 3100 | http://10.0.0.248:3100 |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.195:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.195:10301 |
| Vision Model (Qwen3-VL-8B) | 8010 | http://10.0.0.58:8010 |
| Frontend (dashboard) | 3001 | http://helios.tail74fc4a.ts.net:3001 (future: convivialprophet.com) |
| SearXNG | 8090 | http://10.0.0.195:8090 (Helios) |
| Promtail (Helios) | 9080 (internal) | Scrapes Helios Docker socket → pushes to Loki on Jupiter via tailnet |

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

**Flow:** Single model handles conversation and tool execution in one agentic loop. No delegation between models. Helios is always-on (no auto-shutdown).

**Infrastructure:** `config.py` centralizes all env vars via Pydantic Settings. `tool_registry.py` provides decorator-based tool registration (replaces legacy if-elif dispatch). `service_registry.py` auto-detects healthy services and disables tools when dependencies are down. `exceptions.py` defines a typed exception hierarchy for consistent error handling. All memory (RAG chunks, auto-learned facts, user corrections) lives in a single `mempalace` ChromaDB collection with wing/room metadata for structured organization.

## Tools

All tools are called directly by the single model in one agentic loop.

| Tool | Purpose |
|------|---------|
| home_assistant | HA API: `{entity_id, service, data}` |
| search_memory | Unified memory palace search (semantic, optional wing/room filtering) |
| update_memory | Write a new memory directly into the palace (wing/room routed) |
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
| document_vault | Structured doc storage with semantic search (list/create/read/update/delete) |
| check_claude_activity | Read what Claude Code has been working on — recent turns, current session, files touched |
| code_agent | Delegate a coding task to the Qwen2.5-Coder-32B agent on Helios GPU0 |
| sleep_mode | Do Not Disturb: suppress all announcements until morning |
| generate_workout | Generate today's adaptive gym plan (full-body / split based on recency); returns plan as text for context |
| log_set | Log a completed exercise set (exercise, weight_lbs, reps, set_number) |
| workout_status | Get today's workout plan + logged sets |
| modify_workout | Swap or remove an exercise from today's plan |
| log_meal | Log a meal with calorie count (calories-only v1; independent of selfcare_log) |

## Key Files (top 20 load-bearing)

The files you'll touch most often. For the full map, run `ls orchestrator/` or grep by feature name — everything follows `<feature>_manager.py` / `routes_<feature>.py` / `jobs_<feature>.py` naming.

| File | Purpose |
|------|---------|
| `orchestrator/orchestrator.py` | FastAPI app, main chat endpoint, startup/shutdown, scheduler job registration |
| `orchestrator/unified_loop.py` | v7 unified agentic loop — single model conversation + tool execution. THE main chat flow. |
| `orchestrator/cloud_brain.py` | Model routing, fallback, unified-loop orchestration |
| `orchestrator/tool_handlers.py` | All `tool_*` functions + `execute_tool` facade. Tool-side behavior lives here. |
| `orchestrator/tool_registry.py` | Decorator-based tool registration. `@register_tool` + metrics wrapping. |
| `orchestrator/tool_definitions.py` | Tool JSON schemas (what the LLM sees). Keep in sync with `tool_handlers.py`. |
| `orchestrator/config.py` | Pydantic Settings — every env var defined in one place |
| `orchestrator/shared.py` | Module-level state + env var aliases re-exported from `config.py` |
| `orchestrator/state_store.py` | SQLite persistence for reminders, focus, announcements, selfcare, shopping, chat, claude_code_turns, workouts, meals |
| `orchestrator/mempalace.py` | MemPalace — the unified memory system (store, search, wing/room routing, wakeup context) |
| `orchestrator/auto_learn.py` | Background fact extraction from conversations — encrypt, dedup, store in palace |
| `orchestrator/focus_manager.py` | Pomodoro timer, ambient audio, Pi-hole blocking, body doubling sprints |
| `orchestrator/reminder_manager.py` | TTS announcements, reminders, DND gate, announcement history, phone notifications |
| `orchestrator/brain_dump_manager.py` | Brain dump capture, categorization, dedup, routing to RAG or reminders |
| `orchestrator/routine_manager.py` | Morning/evening routine scaffolding — step-by-step TTS guidance |
| `orchestrator/progress_tracker.py` | Daily stats, streaks, daily/weekly TTS summaries |
| `orchestrator/background_jobs.py` | Background job facade (imports from `jobs_*.py` domain modules) |
| `orchestrator/ha_integration.py` | Home Assistant entity discovery + service call wrapper |
| `orchestrator/metrics.py` | 70+ Prometheus metrics (`bgw_*` namespace). Source of truth for dashboards. |
| `docker-compose.yml` | Service stack (env-var driven, no hardcoded IPs) |
| `monitoring/promtail/promtail-helios.yml` | Promtail config for Helios sidecar — scrapes Docker socket, pushes to Loki |
| `orchestrator/workout_manager.py` | Adaptive gym workout generator — recency-aware split logic, set logging, PR tracking |
| `orchestrator/meal_manager.py` | Calorie-only meal logging with optional photo-based vision estimation (Qwen3-VL-8B) |
| `orchestrator/exercises_seed.py` | ~52-entry static exercise catalog; seeded idempotently into `exercises` table on startup |

## Key Paths

```
/opt/helios/gateway_mvp/            # Project root on Helios
~/.env                              # Secrets (HA_TOKEN, API_TOKEN, PIHOLE_PASSWORD)
~/rag/nadim_rag/                    # RAG source documents (154 docs indexed)
~/.local/share/chroma/personal_rag/ # ChromaDB persistence
/opt/helios/gateway_mvp/credentials/   # Google OAuth2 creds (gitignored)
/opt/helios/gateway_mvp/certs/         # Tailscale TLS certs (gitignored)
/app/data/meal_photos/              # Meal photo uploads (uuid4 names, jpg/jpeg/png/gif/webp only)
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

# Remote deploy from Mac (via Tailscale or LAN)
ssh labadmin@10.0.0.195 "cd /opt/helios/gateway_mvp && git pull && docker compose up -d --build orchestrator"

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

## Detailed Docs (router)

This is a **load-on-demand router**. Read the specific doc when the task touches that domain. Don't read them all up front — each one is written to be self-contained.

**Reference (read when…)**

| Doc | Read when you're working on… |
|-----|------|
| `TECHNICAL_REFERENCE.md` | any API endpoint, tool schema, ChromaDB metadata shape, or HA service call |
| `docs/ENV_VARS.md` | adding or modifying an environment variable in any subsystem |
| `ARCHITECTURE.md` | data flow, internals, anything that touches how modules interact |
| `COMMANDS.md` | you need a command you don't remember and Common Commands below isn't enough |
| `ROADMAP.md` | planning new features or checking what's shipped vs planned |

**Subsystem (read when the task is specifically about…)**

| Doc | Read when you're working on… |
|-----|------|
| `docs/MEMPALACE.md` | memory system internals, write paths, MCP server, session mining |
| `docs/CLAUDE_CODE_INTEGRATION.md` | the Stop hook, `check_claude_activity` tool, or code_agent activity injection |
| `docs/FOCUS_AND_PIHOLE.md` | focus timer, Pomodoro flow, Pi-hole DNS blocking, Nebula Sync |
| `docs/VOICE_AND_TTS.md` | ATOM Echo voice assistant, TTS pacing, Wyoming bridges, STT config |
| `docs/GOOGLE_INTEGRATIONS.md` | Calendar API, Gmail API, phone sync, travel-time alerts, OAuth2 setup |
| `docs/FRONTEND.md` | dashboard pages, widgets, YNAB finance, API proxy pattern |
| `docs/WORKOUTS_AND_MEALS.md` | Workout generator adaptive logic, meal photo flow, API endpoints, env vars |
| `docs/MODE_ROUTER.md` | intent classification (explainer/mirror/counterbalance/challenge/baseline) |
| `docs/INFRASTRUCTURE.md` | HTTPS/Tailscale, RAG host setup, temperature monitoring, kiosk config |
| `docs/REMOTE_DEV.md` | remote dev workflow (mosh + tmux, jdev alias, git sync) |
| `monitoring/README.md` | Prometheus, Grafana, Loki — scrape targets, dashboard generator, alerts |
| `monitoring/grafana/dashgen/` | editing dashboards (Python generator; don't hand-edit the JSON) |

**User-facing**

| Doc | What |
|-----|------|
| `docs/JESS_QUICK_START.md` | User-facing feature guide — every voice command, grouped by domain |
| `docs/JESS_REFERENCE_CARD.md` + `.html` | Printable ADHD reference card — grouped by *situation*, not feature |
| `jess-features/README.md` | ADHD feature spec index (F-001 through F-010) — read for implementation rationale |

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
- Helios SSH: `labadmin@10.0.0.195` (LAN) or `labadmin@helios.tail74fc4a.ts.net` (Tailscale)
- Uranus SSH (from Helios): `ssh labadmin@10.0.0.173`
- **Model history:** Qwen3-VL-30B-A3B (Huihui abliterated) was trialed as primary in early April 2026 but hallucinated tool calls instead of executing them — reverted to Qwen3.5-27B. `llama-server-moe.service` is disabled but the unit file remains on disk as historical reference.
- **Tool result cap:** unified loop enforces 8000-char cap per tool result (~2000 tokens). Tools that return large blobs must summarize/paginate at the handler level. See `TECHNICAL_REFERENCE.md` → Tool Result Cap.
- **Both promtails digest-pinned:** Jupiter promtail now matches Helios sidecar posture — same `grafana/promtail:3.4.2` digest, `cap_drop: ALL`, `no-new-privileges`, `-config.expand-env=true`. See `monitoring/README.md`.
- **auto_learn LLM timeout:** 120s (aligned with unified_loop). Was 30s; caused silent ReadTimeout failures under slot contention.

