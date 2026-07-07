# Brain Gateway

> **For AI assistants (Claude Code, Cursor, etc.):** This file is the developer briefing for AI coding tools working in this repo. End users installing Brain Gateway should read `README.md` instead. Maintainer reference docs (Helios cluster details, historical migration plans) live in `docs/internal/`.

Personal AI assistant for ADHD support. Primary model (Lorbus/Qwen3.6-27B-int4-AutoRound served by vLLM on Helios RTX 5090) handles conversation and tools in one unified agentic loop. v6 hybrid mode has been removed.

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
| Helios | 10.0.0.195 | helios.tail74fc4a.ts.net | RTX 5090 + RTX PRO 5000 | **GPU model layer** (LLM + TTS/STT + code agent). Primary LLM: Lorbus/Qwen3.6-27B-int4-AutoRound served by vLLM 0.19.1 (GPU0 RTX 5090, port 8080), TTS + STT (GPU1 RTX PRO 5000), Code agent: Qwen3-Coder-Next 80B/3B MoE Q4_K_XL (GPU1 RTX PRO 5000 + system RAM via `-ot .ffn_.*_exps.=CPU`, port 8082). **Power-tiered: asleep most of the time, woken on demand via an HA-controlled smart plug** — NOT always-on; the orchestrator/frontend/HA run 24/7 on Jupiter instead. |
| Jupiter | 10.0.0.248 | jupiter-amds.tail74fc4a.ts.net | - | **Always-on hub**: Orchestrator (`brain-orchestrator` :8888), Frontend (:3001), **Home Assistant** (docker, host-networked, :8123 — migrated here off the dead Pi on 2026-07-04), Monitoring host (Prometheus, Grafana, Alertmanager, Loki, Promtail, Blackbox exporter), Pi-hole primary, nebula-sync, Conjure API |
| Saturn | 10.0.0.58 | saturn-3090.tail74fc4a.ts.net | RTX 3080 (10GB) + RTX 3090 (24GB) | Vision model (Qwen3-VL-8B-Instruct Q4_K_M, RTX 3080, port 8010), Expert reasoning model (Qwen3-32B Q4_K_M, RTX 3090, port 8084 via `expert-model` docker container), Pi-hole secondary, **off-box backup target** (orchestrator state + HA config) |
| Uranus | 10.0.0.173 | uranus-5080s.tail74fc4a.ts.net | 2x RTX 5080 (16GB each) | Non-Helios **test box** (Ubuntu 24.04). **Currently unreachable** (as of 2026-07-04); not part of the runtime path. |
| Pi (retired) | 10.0.0.106 | - | - | **Dead** — ran Home Assistant until its SD card failed 2026-07-04; HA was migrated to Jupiter. Safe to reimage. |
| Callisto | 10.0.0.136 | - | - | Monitoring kiosk display (Pi 4) |

## Services

Services marked **[advanced]** require `COMPOSE_PROFILES=advanced` in `.env` and are excluded from the default install (along with `nebula-sync` and `nut-exporter`, which have no port surface). The model layer (`vllm-primary`, `qwen-tts`, `parakeet-stt`) has compose stanzas gated behind a separate `models` profile — authored for fresh single-box installs but **not deployed on Helios**, where those servers run as host systemd units. See `docs/ENV_VARS.md` → Distribution profile + Model layer.

| Service | Port | URL |
|---------|------|-----|
| Open WebUI (HTTPS) | 443 | https://helios.tail74fc4a.ts.net (Tailscale, tailnet-only) |
| Open WebUI (HTTP) | 80 | http://10.0.0.195 |
| Orchestrator | 8888 | http://10.0.0.248:8888 (Jupiter) |
| Primary LLM (Qwen3.6-27B INT4 via vLLM) | 8080 | http://10.0.0.195:8080/v1 |
| Code agent (Qwen3-Coder-Next 80B/3B MoE) | 8082 | http://10.0.0.195:8082/v1 |
| Expert model (Qwen3-32B Q4_K_M) | 8084 | http://10.0.0.58:8084/v1 |
| TTS (Qwen3-TTS) | 8002 | http://10.0.0.195:8002 |
| STT (Parakeet TDT v3) | 8003 | http://10.0.0.195:8003 |
| Pi-hole (Jupiter primary) | 53/8053 | http://jupiter-amds.tail74fc4a.ts.net:8053/admin |
| Pi-hole (Saturn secondary) | 53/8053 | http://saturn-3090.tail74fc4a.ts.net:8053/admin |
| Grafana (Jupiter) | 3000 | http://jupiter-amds.tail74fc4a.ts.net:3000/d/brain-gateway-overview |
| Prometheus (Jupiter) | 9090 | http://jupiter-amds.tail74fc4a.ts.net:9090 |
| Alertmanager (Jupiter) | 9093 | 127.0.0.1 only (unauthenticated API — loopback bind is the exposure control; in-network as `alertmanager:9093`) |
| Loki (Jupiter) | 3100 | http://10.0.0.248:3100 |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.195:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.195:10301 |
| Vision Model (Qwen3-VL-8B) | 8010 | http://10.0.0.58:8010 |
| Frontend (dashboard) | 3001 | http://jupiter-amds.tail74fc4a.ts.net:3001 (future: convivialprophet.com) |
| SearXNG | 8090 | http://jupiter-amds.tail74fc4a.ts.net:8090 (Jupiter) |
| Promtail (Helios) **[advanced]** | 9080 (internal) | Scrapes Helios Docker socket → pushes to Loki on Jupiter via tailnet |

## Architecture (v7 Unified)

```
User -> Open WebUI -> Orchestrator -> Unified Loop -> Model (Qwen3.6-27B INT4 via vLLM)
                                                         |
                                          conversation + tool calls in one loop
                                                         |
                    +----------+----------+----+----+----------+----------+
                    v          v          v    v    v          v          v
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow:** Single model handles conversation and tool execution in one agentic loop. No delegation between models. Helios (the GPU model layer) is power-tiered — asleep most of the time, woken on demand via an HA smart plug — while the orchestrator and its proactive jobs run 24/7 on Jupiter.

**Infrastructure:** `config.py` centralizes all env vars via Pydantic Settings. `tool_registry.py` provides decorator-based tool registration (replaces legacy if-elif dispatch). `service_registry.py` auto-detects healthy services and disables tools when dependencies are down. `exceptions.py` defines a typed exception hierarchy for consistent error handling. All memory (RAG chunks, auto-learned facts, user corrections) lives in a single `mempalace` ChromaDB collection with wing/room metadata for structured organization.

## Tools

All tools are called directly by the single model in one agentic loop.

Tools marked **[advanced]** are gated behind `JESS_ADVANCED=true` (default false) and excluded from the default shippable build. Set in `.env` to enable. See `docs/ENV_VARS.md` → Distribution profile.

| Tool | Purpose |
|------|---------|
| home_assistant | HA API: `{entity_id, service, data}` |
| search_memory | Unified memory palace search (semantic, optional wing/room filtering) |
| update_memory | Write a new memory directly into the palace (wing/room routed) |
| set_reminder / cancel_reminder | Voice/phone reminders |
| update_data | Update meds/projects YAML (source of truth for structured personal facts; write path) |
| get_data | Read meds/projects/profile from the YAML source of truth (`{kind}`). The authoritative READ path — the model answers meds/schedule questions from here, never from RAG/`search_memory`. Non-terminal. A compact meds+projects block is also injected into every system prompt via `data_manager.get_structured_facts_block`. |
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
| finance_status | Budget, spending, XP/levels from YNAB integration **[advanced]** |
| analyze_image | Re-analyze or ask follow-up questions about a shared image |
| ask_expert | Delegate a HARD reasoning task to the expert model (Qwen3-32B on Saturn RTX 3090, port 8084). Used by `query_budget` analyze-mode synthesis. See `orchestrator/expert_agent.py`. **[advanced]** |
| shopping_list | Add/check/remove items from shopping/grocery lists |
| document_vault | Structured doc storage with semantic search (list/create/read/update/delete) |
| paperless_save | Push a file from `/app/data/paperless_inbox/` to Paperless-ngx for OCR + auto-tagging (F-012) |
| check_claude_activity | Read what Claude Code has been working on — recent turns, current session, files touched **[advanced]** |
| code_agent | Delegate a coding task to the Qwen3-Coder-Next 80B/3B MoE agent on Helios GPU0 (port 8082) **[advanced]** |
| sleep_mode | Do Not Disturb: suppress all announcements until morning |
| generate_workout | Generate today's adaptive gym plan (full-body / split based on recency); returns plan as text for context |
| log_set | Log a completed exercise set (exercise, weight_lbs, reps, set_number) |
| workout_status | Get today's workout plan + logged sets |
| modify_workout | Swap or remove an exercise from today's plan |
| log_meal | Log a meal with calorie count (calories-only v1; independent of selfcare_log) |
| query_budget | Query historical budget/spending data imported from CSV/Excel. Use `question_type="analyze"` with `analysis_question=<user's question>` for synthesis (internally chains to the expert reasoning model, slow ~50s); other types for narrow per-dimension facts. **[advanced]** |
| helios_power | Power the Helios GPU box on/off (`{action: wake|sleep|status}`) via its HA-controlled smart plug. Gated behind `HELIOS_WAKE_ENABLED` (default false; not `JESS_ADVANCED`). |

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
| `orchestrator/helios_power.py` | Helios wake-on-demand (PT-C) — `wake_helios()`/`sleep_helios()`/`helios_power_status()` via HA smart-plug. Self-contained httpx, never raises, default-OFF behind `HELIOS_WAKE_ENABLED`. |
| `orchestrator/metrics.py` | 70+ Prometheus metrics (`bgw_*` namespace). Source of truth for dashboards. |
| `docker-compose.yml` | Service stack (env-var driven, no hardcoded IPs) |
| `monitoring/promtail/promtail-helios.yml` | Promtail config for Helios sidecar — scrapes Docker socket, pushes to Loki |
| `orchestrator/workout_manager.py` | Adaptive gym workout generator — recency-aware split logic, set logging, PR tracking |
| `orchestrator/meal_manager.py` | Calorie-only meal logging with optional photo-based vision estimation (Qwen3-VL-8B) |
| `orchestrator/exercises_seed.py` | ~52-entry static exercise catalog; seeded idempotently into `exercises` table on startup |
| `orchestrator/jobs_self_audit.py` | Daily self-audit (F-014) — queries Loki, asks Jess, pushes Pushover digest. Read-only safety story. |
| `orchestrator/routes_config.py` | `/api/config/*` REST surface for the `/settings` page (Identity, Selfcare, Quiet Hours, Routines, Speakers, Recurring Reminders) + read-only `GET /api/config/features` (`{workouts_enabled, meals_enabled, jess_advanced}`) consumed by the dashboard nav to hide disabled-feature links + read-only `GET /api/config/personal-facts` (`{medications, projects, profile}`, allowlisted — strips pharmacy/interactions) powering the `/personal-facts` dashboard "peek" page (`frontend/src/app/(private)/personal-facts/page.tsx`) so the user sees exactly what the model reads from the YAML source of truth |
| `orchestrator/routines_config.py` | Loader/saver for `/api/config/routines`. Reads from writable shadow at `/app/data/routines.yaml` (falls back to read-only `/app/config/routines.yaml`). `merge_with_existing()` preserves power-user step fields (`ha_action`, `fallback_*`, `include_calendar_summary`, `calendar_days_ahead`) on round-trip. `reload_routines_and_reschedule()` re-registers `routine_<id>` APScheduler cron jobs after each save and prunes jobs for deleted routines. |
| `orchestrator/announcement_routes.py` | Loader/saver for `/api/config/speakers`. YAML at `/app/data/announcement_routes.yaml` maps each announcement category → speaker entity-id (single or comma-separated for multi-room). `route_for(announcement_type)` is consulted by `reminder_manager._announce_voice` whenever no explicit `speaker=` is passed; empty values fall back to the legacy `REMINDER_SPEAKER` / `MORNING_BRIEFING_SPEAKER` / `FOCUS_AUDIO_PLAYER` env vars. `discover_ha_speakers()` enumerates HA `media_player.*` entities for the panel autocomplete datalist. |
| `orchestrator/config_writer.py` | `atomic_write_yaml()` (tmpfile + os.replace + fsync), `log_config_change(panel, before, after)` audit helper, credential-redacting `_redact()` |
| `orchestrator/selfcare_schedule.py` | Runtime YAML config at `/app/data/selfcare_schedule.yaml`; load/save/reload + accessors (`category_enabled`, `category_interval_minutes`, `category_active_hours`, `category_times`, `quiet_hours`, `is_quiet_day`) |
| `orchestrator/recurring_reminders.py` | CRUD + `expand_due_reminders()` APScheduler job (every 5 min) materializing croniter-based rules into one-shot `reminders` rows; auto-disables impossible crons |
| `orchestrator/routes_setup.py` | `/api/setup/*` first-boot setup-wizard backend (status/hardware/complete) + first-chat helpers (`is_first_chat`, `mark_first_chat_done`); persists `setup_state.json`, serves the host-produced `hardware_scan.json` |
| `orchestrator/welcome.py` | First-chat welcome generator. Pure formatter — renders a one-time markdown tour listing what's working + un-configured integrations + the `/settings` link. Defangs markdown injection in `user_name`/`assistant_name`. Wired in by `cloud_brain._maybe_prepend_welcome` (skips on fast-path + voice). |

## Key Paths

```
/home/labadmin/gateway_nerves/       # Project root on Jupiter (the always-on hub)
~/.env                              # Secrets (HA_TOKEN, API_TOKEN, PIHOLE_PASSWORD)
~/rag/                              # RAG source documents (auto-ingested from here)
~/.local/share/chroma/personal_rag/ # ChromaDB persistence
/home/labadmin/gateway_nerves/credentials/   # Google OAuth2 creds (gitignored)
/home/labadmin/gateway_nerves/homeassistant/ # Home Assistant compose + backup script (config volume at ~/homeassistant/config)
/app/data/meal_photos/              # Meal photo uploads (uuid4 names, jpg/jpeg/png/gif/webp only)
```

## Common Commands

```bash
# Fresh box → working install (2-stage: drivers/docker, reboot, full stack)
bash install.sh

# Express CLI wizard (2 questions: name + timezone; runs at end of install.sh)
bash scripts/setup.sh

# Start/rebuild
docker compose up -d
docker compose up -d --build orchestrator

# Logs
docker logs brain-orchestrator --tail 50 -f

# Health
curl http://localhost:8888/health

# Remote deploy from Mac (via Tailscale or LAN)
ssh labadmin@10.0.0.195 "cd /opt/gateway_mvp && git pull && docker compose up -d --build orchestrator"

# Frontend rebuild
docker compose up -d --build --force-recreate frontend

# Re-index RAG documents
docker exec brain-orchestrator python scripts/reindex_rag.py

# Detect VRAM tier (prints JESS_VRAM_TIER + suggested VLLM_MODEL; read-only)
bash scripts/detect_hardware.sh

# Validate the model-layer compose stanzas without starting anything
docker compose --profile models config

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
| `ARCHITECTURE.md` | ⚠️ SUPERSEDED (documents the removed v6 Nemotron loop) — for current internals use this "Architecture (v7 Unified)" section + the code (`unified_loop.py`, `cloud_brain.py`, `tool_registry.py`) |
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
| `docs/HA.md` | Home Assistant on Jupiter: run/upgrade, the `homeassistant/` compose, backup/restore, Saturn failover, migration history |
| `docs/BACKUP.md` | orchestrator state backup (`scripts/backup_state.py`), what's backed up, restore, off-box to Saturn |
| `docs/internal/HELIOS_INFRASTRUCTURE.md` | Helios-specific runbook: Tailscale HTTPS cert, GPU layout, temperature monitoring, kiosk config (maintainer reference only) |
| `docs/internal/VLLM_PHASE_3_PLAN.md` | Historical: 2026-04-26 cutover plan (Plan A vs Plan B rationale, rollback notes) |
| `docs/TRAINING_CORPUS.md` | working on fine-tune data pipelines, debugging missing conversation data, or changing the drain schedule |
| `monitoring/README.md` | Prometheus, Grafana, Loki — scrape targets, dashboard generator, alerts |
| `monitoring/grafana/dashgen/` | editing dashboards (Python generator; don't hand-edit the JSON) |

**User-facing**

| Doc | What |
|-----|------|
| `docs/JESS_QUICK_START.md` | User-facing feature guide — every voice command, grouped by domain |
| `docs/JESS_REFERENCE_CARD.md` + `.html` | Printable ADHD reference card — grouped by *situation*, not feature |
| `jess-features/README.md` | ADHD feature spec index (F-001 through F-013) — read for implementation rationale |

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
| `jess-features/F-011-ntfy-feedback-loop.md` | Ntfy Feedback Loop (Done/Snooze) | P1 |
| `jess-features/F-012-paperless-bridge.md` | Paperless-ngx Bridge (file handoff for OCR + tagging) | P2 |
| `jess-features/F-013-pushover-bridge.md` | Pushover Push Bridge (parallel iOS push channel) | P1 |

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_mvp` (default from directory name)
- Helios (GPU model layer) is **power-tiered** — asleep most of the time, woken on demand via an HA smart plug (see PT-C note below). The orchestrator/frontend/HA run 24/7 on Jupiter. Not always-on.
- TTS uses Qwen3-TTS with sentence pause injection. Voice is env-configurable via `TTS_VOICE` (default `default`); the live deployment sets `TTS_VOICE=jessica` for the personal cloned voice, but the codebase no longer hardcodes that branding. Wire IDs `jessica-tts` (server name) and `jessica` (Wyoming bridge voice ID) are retained for HA back-compat — see `docs/VOICE_AND_TTS.md`.
- Helios SSH: `labadmin@10.0.0.195` (LAN) or `labadmin@helios.tail74fc4a.ts.net` (Tailscale)
- Uranus SSH (from Helios): `ssh labadmin@10.0.0.173` (back online 2026-05-18, reimaged — Helios key re-authorized)
- **Model history:** Qwen3-VL-30B-A3B (Huihui abliterated) was trialed as primary in early April 2026 but hallucinated tool calls instead of executing them — reverted to Qwen3.5-27B (llama.cpp). Qwen3.5-27B served as primary until the 2026-04-26 vLLM Phase 3 cutover replaced it with Lorbus/Qwen3.6-27B-int4-AutoRound on vLLM. `llama-server-moe.service` and `llama-server.service` are both disabled but their unit files remain on disk as historical references.
- **Tool result cap:** unified loop enforces 8000-char cap per tool result (~2000 tokens). Tools that return large blobs must summarize/paginate at the handler level. See `TECHNICAL_REFERENCE.md` → Tool Result Cap.
- **Both promtails digest-pinned:** Jupiter promtail now matches Helios sidecar posture — same `grafana/promtail:3.4.2` digest, `cap_drop: ALL`, `no-new-privileges`, `-config.expand-env=true`. See `monitoring/README.md`.
- **Alertmanager compose-managed + monitoring config render pipeline (2026-07-06):** `alertmanager` is a service in `monitoring/docker-compose.yml` (digest-pinned, `cap_drop: ALL`, bound to `127.0.0.1:9093` — unauthenticated API, loopback bind is the exposure control; replaced a hand-run container reading a stale render from the abandoned `/opt/gateway_mvp` checkout). `scripts/generate-configs.sh` renders BOTH `prometheus.yml` and the gitignored `monitoring/alertmanager/alertmanager.yml` from templates (Pushover keys from `.env`), validating with promtool/amtool before touching the live files; `scripts/reload-monitoring.sh` reloads Prometheus + Alertmanager and content-verifies (alert-rule names in `/api/v1/rules`, receivers/routes match the render). CI runs render → compose up → reload on merge to main when monitoring files change. **Bind-mount inode trap:** a single-file bind mount is pinned to the inode at container start — `git pull`/editors replace files with new inodes, so the container silently keeps reading the old file while `/-/reload` returns 200 (bit us 2026-07-06). Prometheus + Alertmanager therefore use DIRECTORY mounts (`./prometheus`, `./alertmanager`), and rendered configs are truncated in place, never `rm`/`mv`'d. See `monitoring/README.md`.
- **auto_learn LLM timeout:** 120s (aligned with unified_loop). Was 30s; caused silent ReadTimeout failures under slot contention.
- **Training corpus drain:** `orchestrator/jobs_training_corpus.py` runs nightly at 02:30 + one-shot 30s after startup. Registered in `orchestrator.py` behind `JESS_ADVANCED` (cut from default shippable build — collects user conversations, privacy hazard for fresh installs). Pulls user/assistant turns from OWUI sqlite (`open-webui-data:/app/owui_data:ro`), `brain_state.chat_messages`, and Claude Code session jsonls into append-only `/app/data/training_corpus/YYYY-MM.jsonl`. Content-addressed sha1 dedup across all months, secret-pattern filter, 8KB-equivalent 50k-char per-turn cap, no retention. Metric: `bgw_training_corpus_records_total{source}`. Env vars: `TRAINING_CORPUS_*` (see `docs/ENV_VARS.md`). Full spec: `docs/TRAINING_CORPUS.md`.
- **Settings page (`/settings`):** Backed by `routes_config.py` exposing `/api/config/{identity,selfcare,quiet_hours,routines,speakers,recurring_reminders}` (all bearer-gated, all proxied via `/api/proxy/*` from the frontend). Speakers panel maps each announcement category (`selfcare/reminder/calendar/ambient/progress/focus/briefing`) → HA `media_player.*` entity (single or comma-list); `_announce_voice(speaker=None, announcement_type=...)` calls `announcement_routes.route_for(...)`; empty value = legacy env-var fallback (`REMINDER_SPEAKER` / `MORNING_BRIEFING_SPEAKER` / `FOCUS_AUDIO_PLAYER`); morning_briefing now passes `speaker=None` so it inherits the panel routing. Six panels total: Identity & Tone (assistant_name, user_name, `adhd_mode`, `tone_preference`, timezone — `prompt_builder._resolve_tone(user, profile)` consumes these: `adhd_mode=False` → neutral block; else `tone_preference in {warm, balanced, direct}` → preset; else fall back to `get_tone_constraint(user)`), Selfcare Nudges, Quiet Hours, Routines (morning/evening: edits `display_name`, `trigger.time`, `trigger.days`, `speaker`, `nudge_delay_minutes`, `nudge_max`, `auto_skip`, and per-step `id/label/est_minutes/skippable`; per-routine `nudge_max`/`auto_skip` override the global `ROUTINE_NUDGE_MAX`/`ROUTINE_AUTO_SKIP` env-var defaults — `_deliver_nudge` reads them off `RoutineSession` with global fallback; preserves power-user `ha_action` + `fallback_*` + `include_calendar_summary` + `calendar_days_ahead` via `routines_config.merge_with_existing()`; PUT calls `reload_routines_and_reschedule()` to re-register `routine_<id>` APScheduler crons live), Recurring Reminders. Profile updates write to `/app/data/user_profile_overrides.yaml` (the base `user_profile.yaml` is mounted `:ro`); `reload_profile()` mutates the singleton in place so existing `shared.profile` imports see updates without restart. Selfcare/quiet-hours edits land in `/app/data/selfcare_schedule.yaml` (replaces the old hardcoded 9am–9pm meal window + env-var intervals — `selfcare_manager._check_*` now reads the YAML at call time, including day-of-week-aware quiet hours via `is_quiet_day`). Recurring reminders are croniter-based rules in the new `recurring_reminders` table; `expand_due_reminders()` runs every 5 min as APScheduler interval job `id="recurring_reminders_expand"` (registered in `orchestrator.py` alongside the training_corpus drain) and materializes due rules into the existing one-shot `reminders` table, capped at 14 days lookahead, auto-disabling rules whose cron never fires (e.g. `0 0 30 2 *`). All writes go through `config_writer.atomic_write_yaml()` (tmpfile + `os.replace` + fsync) and append a redacted before/after diff to the new `config_changes` table via `log_config_change()`. Metrics: `bgw_recurring_reminders_expanded_total`, `bgw_recurring_reminders_expand_errors_total`. New env vars: `SELFCARE_SCHEDULE_PATH`, `USER_PROFILE_OVERRIDES_PATH` (see `docs/ENV_VARS.md`). API surface + table schemas: `TECHNICAL_REFERENCE.md`. Frontend: `frontend/src/app/(private)/settings/page.tsx` + `components/settings/*Panel.tsx` + `lib/settings-api.ts`; nav lives on desktop sidebar and inside the mobile More overflow (NOT the always-visible primary tabs).
- **Calendar source priority:** `tool_check_calendar`, `morning_briefing`, `evening_briefing`, and `get_ambient_status` (ambient summary TTS at 10am/12pm/2pm/4pm + dashboard LED) all read phone sync first (<24h, at least one parseable record) and fall through to Google, including the "all-records-unparseable → fall through" defensive guard. See `docs/GOOGLE_INTEGRATIONS.md` → Phone Calendar Sync.
- **Evening shutdown ritual:** `jobs_calendar.evening_briefing()`, daily cron (default 21:30, `EVENING_BRIEFING_TIME`/`EVENING_BRIEFING_ENABLED`, registered in `orchestrator.py` NOT gated on calendar config — meds + parking deliver without one; registration is try/except-guarded so a malformed time can't crash-loop startup). Mirror of the morning briefing: tomorrow's first event + leave-by time (`get_travel_time` + `TRAVEL_TIME_BUFFER`), evening meds check (`selfcare_manager.evening_meds_status()` — same confirmed-today-hour≥17 semantics as `_check_meds`), and parks ONE unfinished thing (F-007: active focus task, else top open backlog task) into the new `app_state` SQLite KV (`state_store.set_app_state/get_app_state_entry/delete_app_state`). `morning_briefing` offers the parked item back and clears it only on a successful, non-suppressed announce; items older than 36h are dropped, not announced as "last night". Skips the announce (but still parks) when DND is active or a guided routine session is mid-flight (the evening routine auto-triggers at 21:00). Full digest mirrors to Telegram via `fire_system_message`. Speaker route: `briefing` category. Metrics: `bgw_evening_briefing_last_run_timestamp_seconds` gauge seeded at startup + `EveningBriefingStale` alert (24.5h threshold so it pages ~22:30, not midnight). Tests: `tests/test_evening_briefing.py`.
- **Sleep wind-down ladder:** `orchestrator/jobs_winddown.py`, two cron rungs ahead of `WIND_DOWN_BEDTIME` (default 22:30; registration in `orchestrator.py`, try/except-guarded, minute math wraps past-midnight bedtimes): `wind_down_dim` (T-60) activates each scene in the comma-separated `WIND_DOWN_SCENE` (non-`scene.*` entries dropped with a WARN — a .env typo must not nightly-turn_on an arbitrary domain) via `ha_client.call_service(scene, "turn_on")` — silent, per-scene error isolation, skipped under DND (scene.turn_on can raise lights an early goodnight turned off), no-op when unset; `wind_down_nudge` (T-30) speaks a screens-away line + one-line tomorrow anchor via `jobs_calendar.get_tomorrow_events()` (the shared phone-first/Google-fallback helper extracted from `evening_briefing`), same DND/active-routine silent-skip rules, `briefing` speaker route, outcome-honest log. Morning half: `sleep_mode("on")` stamps `app_state.sleep_started_at` via `tool_handlers._stamp_sleep_started` — goodnight-intent only: indefinite mutes (no `duration_hours`) in the 20:00–05:00 window, because the same tool is also "mute, guests over" / "quiet for 2h" and stamping those falsely softens the next morning; `morning_briefing` reads+clears it and goes gentle (softer greeting, weather skipped) when the night ran under `WIND_DOWN_SHORT_NIGHT_HOURS` (6.5h); stamps >16h old are ignored so afternoon timed mutes don't count. Metrics: `bgw_wind_down_last_run_timestamp_seconds` (T-30 nudge) + `bgw_wind_down_dim_last_run_timestamp_seconds` (T-60 dim — stamped at the TOP of `wind_down_dim` before its no-scene/DND early returns, so it proves the job FIRED even on nights it does no work; the scene counter alone can't distinguish a dropped job from a legitimate no-op), both seeded at startup like the briefing gauges. The nudge has a `WindDownNudgeStale` warning alert (25.5h, `brain_gateway_deadmans` group) routed to the non-paging `pushover-quiet` receiver (priority 0) — a missing spoken nudge is invisible by omission and the feature targets the person least likely to notice; the re-seed-on-restart masks both panel and alert during active-dev weeks, so the alert only bites once deploys quiesce. The T-60 dim rung has a heartbeat dashboard panel but NO alert by design (a failed dim is self-evident in the house). Sleep Wind-Down dashboard row in `brain_gateway_sre.py`. Tests: `tests/test_wind_down.py`.
- **Scheduler missed-job observability:** the scheduler-wide `misfire_grace_time=300` / `coalesce=True` `job_defaults` (`shared.py`) mean a job whose fire time passes during a >300s event-loop stall is silently dropped — one-shot date jobs (reminders, focus-break delivery, `dnd_auto_unmute`) have no next occurrence and no runtime recovery, so a miss is a permanently lost action. `_on_job_missed(event)` (`orchestrator.py`, registered via `scheduler.add_listener(..., EVENT_JOB_MISSED)` immediately before `scheduler.start()`) closes that gap: logs at ERROR (`[SCHEDULER] Job MISSED ...`) and bumps `bgw_scheduler_jobs_missed_total{job_family}`. `metrics.scheduler_job_family(job_id)` collapses UUID/timestamp ids to a bounded family (`reminder|focus|routine|auto_learn|interrupt|ambient`, fixed-string ids → `cron:<id>`) to keep label cardinality flat. Grafana "Background Jobs" row (`brain_gateway_sre.py`) + warning alert `SchedulerJobsMissed` in the `brain_gateway_deadmans` group (routed to paging Pushover). Tests: `orchestrator/tests/test_scheduler_missed_jobs.py`.
- **Routine step advancement & greeting:** `routine_manager._build_step_announcement` picks "Morning/Afternoon/Evening" from wall-clock hour (4-11/12-16/else) — no longer hardcoded to "Morning". Prevents the 2026-04-17 evening-routine-stuck-all-night class of bug where non-skippable steps nudged forever: `_deliver_nudge` now force-ends the routine once `nudge_count > ROUTINE_NUDGE_MAX` on a non-skippable step (or any step when `ROUTINE_AUTO_SKIP=off`), logged at WARNING. Metrics gap: no `bgw_routine_*` or `bgw_selfcare_*` counters exist yet — a `bgw_routine_auto_ended_total{routine,step}` would let Grafana alert on stuck routines instead of relying on user notice.
- **Ntfy feedback loop (F-011):** Third reminder delivery channel alongside TTS + HA Companion push. `reminder_manager.deliver_via_ntfy` publishes to ntfy with Done/Snooze action buttons that POST back to HMAC-signed callback URLs — `/api/reminder/ack/{id}` and `/api/reminder/snooze/{id}` (bearer-exempt, listed in `BearerAuthMiddleware.PUBLIC_PREFIXES`). Done taps fire the selfcare bridge via `infer_selfcare_action_from_text` (shares `selfcare_manager.ACTION_KEYWORDS` with `selfcare_log`). Dispatched fire-and-forget from `deliver_reminder_job` after HA Companion push. Auto-disables via `config.py` `model_validator` if `NTFY_HMAC_SECRET` is missing. Metrics: `bgw_ntfy_push_total{result,kind}`, `bgw_ntfy_push_latency_seconds{kind}` (kind=`reminder|confirm`), `bgw_ntfy_ack_total`, `bgw_ntfy_snooze_total`, `bgw_ntfy_callback_rejected_total`, `bgw_reminder_ack_latency_seconds`. Opt-in confirm side-channel (`NTFY_CONFIRM_ENABLED`) pushes a low-priority "✓ Logged" / "💤 Snoozed..." back to the topic after ack/snooze so the user sees visible feedback; confirm title stays generic (lockscreen privacy). Env vars: `NTFY_*` (see `docs/ENV_VARS.md`). Full spec: `jess-features/F-011-ntfy-feedback-loop.md`.
- **Pushover bridge (F-013):** Parallel iOS push channel alongside F-011 ntfy — Pushover has native APNs integration so lockscreen banners land reliably on iOS where self-hosted ntfy-upstream was flaky. Runs ALONGSIDE ntfy (double push if both enabled); `NTFY_ENABLED` and `PUSHOVER_ENABLED` are independent flags. Reuses F-011's HMAC-signed `/api/reminder/ack/{id}` + `/api/reminder/snooze/{id}` callback routes — no new routes, just a new outbound channel. `pushover_manager.deliver_via_pushover` fires from `deliver_reminder_job` via `create_task` alongside ntfy dispatch; `deliver_pushover_confirm` fires from both ack and snooze routes via `_fire_and_forget` alongside the ntfy confirm. Feature-flag gate on both routes widened to `if not (ntfy_enabled or pushover_enabled)` so pushover-only deployments work. Reminder text is HTML-escaped (`html.escape(text, quote=False)`) before embedding in Pushover's HTML message body to block prompt-injection-planted `<a href>` in the trusted "Jess reminder" notification. Error bodies run through a credential-regex-strip before logging. Confirm titles stay generic ("✓ Logged", "💤 Snoozed until H:MM") — action category lives only in body (same lockscreen-privacy rule as F-011). Auto-disables in `config.py` `model_validator` on missing user key / app token. Metrics: `bgw_pushover_push_total{result,kind,reason}` + `bgw_pushover_push_latency_seconds{kind}`. `reason` label distinguishes `ok | http_4xx | http_5xx | timeout | connect_error | other | disabled | missing_user_key | missing_app_token | missing_credentials` so Grafana can tell "token revoked" from "Pushover down". Env vars: `PUSHOVER_*` (see `docs/ENV_VARS.md`). Full spec: `jess-features/F-013-pushover-bridge.md`.
- **Telegram bot:** Two-way capture + reminder channel (`orchestrator/telegram_bot.py`). A dedicated forever asyncio task (NOT a scheduler job — the 50s `getUpdates` long-poll would pin a scheduler worker) started in `orchestrator.py` startup when `TELEGRAM_ENABLED` + token set, cancelled in `_shutdown_logic` (handle on `shared.telegram_task`). Inbound text from the allow-listed chat relays through the orchestrator's own `/v1/chat/completions` (self-HTTP with Bearer `API_TOKEN` via `TELEGRAM_SELF_URL`) so Telegram gets identical routing to every other client; RAM-only rolling history per chat (`TELEGRAM_HISTORY_TURNS`, `/new` resets). `deliver_via_telegram` fires from `deliver_reminder_job` via `create_task` alongside ntfy/pushover and sends inline **Done/Snooze** buttons (`callback_data`, NOT HMAC URLs — a callback_query only arrives via Telegram's API from a chat we allow-listed, so the chat-ID check is the auth boundary; unknown chats are dropped with ID logged rate-limited, content never). Callback handling replicates the F-011 ack/snooze route semantics in-process: `mark_reminder_acked(via="telegram")` / snooze-cap check → `scheduler.add_job` reschedule → `reopen_reminder` → `increment_snooze_count`, retry-job cancellation, and the selfcare bridge via `infer_selfcare_action_from_text`. Replies are plain text (no parse_mode — LLM output can't fail Telegram's Markdown parser). Auto-disables in `config.py` `model_validator` on missing/short bot token; empty `TELEGRAM_ALLOWED_CHAT_ID` is allowed for the log-your-chat-ID setup flow. Metrics: `bgw_telegram_send_total{result,kind,reason}`, `bgw_telegram_send_latency_seconds{kind}`, `bgw_telegram_update_total{kind,result}`, `bgw_telegram_callback_total{action,result}`. Env vars: `TELEGRAM_*` (see `docs/ENV_VARS.md`).
- **Self-audit (F-014):** Daily 7am UTC scheduled job (`orchestrator/jobs_self_audit.py`) queries Loki on Jupiter for last-24h error/warn logs across Helios services (Docker containers + systemd units), buckets into clusters by `(service, first 80 chars)`, asks Jess to diagnose each cluster (single `call_model` invocation, no tool loop), and pushes a one-line digest via Pushover with the markdown report saved at `/app/data/self_audits/YYYY-MM-DD.md`. Three-layer safety: (1) allow-list filter on Jess's suggested shell commands, (2) dangerous-pattern regex (`rm`, `dd`, `mkfs`, `drop`, `truncate`, etc.), (3) secret-pattern filter applied to both disk report and mempalace summary. Read-only by design — Jess emits text only, the orchestrator never executes her output. Concurrency lock prevents manual `POST /api/self_audit/run` + cron collision (returns `result="busy"` HTTP 409). Loki-unreachable is distinguished from a clean week via an upfront probe + explicit `result="failed"` digest — never a green "all clean" lie. Summary indexed into mempalace under wing=`system`, room=`audit` so future Jess can recall recent operational state. Default-OFF: flip both `SELF_AUDIT_ENABLED=true` and `JESS_ADVANCED=true` in `.env` (operator feature, requires Loki + Pushover stack — gated out of the default shippable build). Gate is enforced both at startup (job registration in `orchestrator.py`) AND at function entry in `run_self_audit()`, so the bearer-gated `POST /api/self_audit/run` manual trigger respects the same flag (no bypass). `SELF_AUDIT_LOKI_URL` is operator-controlled and trusted (same posture as `MODEL_URL` — only http(s) prefix check, no allow-list). Promtail-helios sidecar now scrapes systemd journal for `vllm-primary`, `llama-server` (disabled, retained for rollback), `llama-server-coder`, `qwen-tts`, `parakeet-stt`, `brain-gateway` with a drop stage for qwen-tts health-check noise. Metrics: `bgw_self_audit_runs_total{result}`, `bgw_self_audit_clusters_total{severity}`, `bgw_self_audit_latency_seconds`, `bgw_self_audit_format_drift_total`. Env vars: `SELF_AUDIT_*` (see `docs/ENV_VARS.md`). Full spec: `jess-features/F-014-self-audit.md`.
- **Paperless bridge (F-012):** `paperless_save` tool + `POST /api/paperless/upload` (bearer-gated, 100MB cap via `_LARGE_UPLOAD_PATHS`) push files to Paperless-ngx on Jupiter for OCR + auto-tagging. Staging inbox: `/app/data/paperless_inbox/` (host: `/opt/gateway_mvp/data/app/paperless_inbox/`). Handler guards against path traversal (`/`, `\`, `..`, absolute, null byte) and symlink escape via `Path.resolve() + relative_to(inbox)`. Auto-disables in `config.py` `validate_paperless_config` model_validator if `PAPERLESS_URL` is missing or `PAPERLESS_API_TOKEN` < 8 chars (logs error, never raises — matches F-011 pattern). `document_vault` is deliberately untouched and remains the home for typed/pasted text notes (mempalace-searchable); `paperless_save` handles files (Paperless-managed). Metrics: `bgw_paperless_upload_total{result,reason}`, `bgw_paperless_upload_latency_seconds`. Env vars: `PAPERLESS_*` (see `docs/ENV_VARS.md`). Full spec: `jess-features/F-012-paperless-bridge.md`.
- **Selfcare <-> routine bridge (symmetric):** `selfcare_log` fires `_maybe_advance_routine_for_action` fire-and-forget; if the active routine's current step matches the logged action (keyword map for medication/meal/water/movement), it calls `advance_step("done")`. Reverse direction now works too: `routine_manager.advance_step("done")` calls `selfcare_manager.mark_selfcare_from_routine_step(step)`, which dispatches to `record_medication_logged`/`record_meal_logged`/`record_hydration_logged`/`record_movement_logged` based on the same keyword inference. Routine-sourced medication logging sets the generic `last_med_confirmation["medication"]` key unconditionally (routine labels like `'routine:meds'` can't be window-mapped). Only fires on `"done"`, never on `"skip"`/auto-end `"stop"`. Workout `log_set` also calls `record_movement_logged(f"set:{exercise_name}")` — closes the "sitting 274 min while at gym" gap. Both bridges wrapped in try/except with `logger.error(exc_info=True)`; never block the primary write.
- **Helios pihole + nginx model-server removed from compose 2026-04-26.** Production Pi-holes run on Jupiter (primary) + Saturn (secondary); orchestrator already pointed `PIHOLE_URLS` at those two. The model-server was an unfinished "Hey Jess" custom wake word file server — files remain in `/opt/gateway_mvp/models/` (`hey_jess.tflite`, `hey_jess.json`) for when the feature is finished. To re-enable: change `SERVICE_MODEL_SERVER_PORT` to a non-conflicting value (8080 collides with llama-server), uncomment the wake-word block in `models/atom-echo-jess.yaml:171`, reflash the ATOM Echo via ESPHome. `pihole-etc` and `pihole-dnsmasq` named volumes still exist on disk until manually removed. Promtail-helios scrape regex narrowed to `'/(brain-.*|open-webui.*|redis.*|searxng|wyoming-.*|nebula-sync)'` (dropped `model-server` and `pihole`). As of Step 5 productization, `nebula-sync`, `promtail` (Helios sidecar), and `nut-exporter` are gated behind `profiles: ["advanced"]` — set `COMPOSE_PROFILES=advanced` in `.env` to bring them up.
- **ollama disabled on Jupiter + Saturn 2026-04-26.** `systemctl stop && systemctl disable` on both 10.0.0.248 and 10.0.0.58 — both were running with 0 models loaded. No tool routes through ollama; this was leftover from earlier experimentation.
- **STT engine swap 2026-04-26 — Whisper -> Parakeet (port 8003).** Whisper HTTP STT (`whisper-stt.service`) stopped + disabled; `parakeet-stt.service` active and enabled (model: `nvidia/parakeet-tdt-0.6b-v3`, ~6.3 GB on GPU1, English-only, ~10× faster than Whisper medium with lower WER). Same port 8003, identical OpenAI-compatible API surface (`/health`, `/transcribe`, `/v1/audio/transcriptions`) — orchestrator `STT_URL` unchanged. Pinned to GPU1 via `CUDA_VISIBLE_DEVICES=1` + `PARAKEET_DEVICE=cuda:0`. Wrapper: `tts/stt_server_parakeet.py`, unit: `tts/parakeet-stt.service`. The dead Whisper HTTP STT artifacts (`tts/stt_server.py`, `tts/whisper-stt.service`) were **removed from the repo 2026-06-16** (restore from git history if rollback ever needed). The Wyoming Whisper bridge (port 10300, `wyoming-faster-whisper`, HA voice pipeline) is a SEPARATE live component and was **not** removed. See `docs/VOICE_AND_TTS.md`.
- **Model-layer containerization spike (Phase 1, 2026-05-16) — authored, NOT deployed.** The three model servers (vLLM primary LLM, Qwen3-TTS, Parakeet STT) now have `docker-compose.yml` stanzas (`vllm-primary`, `qwen-tts`, `parakeet-stt`) + Dockerfiles (`tts/Dockerfile`, `tts/Dockerfile.parakeet`, both base `nvidia/cuda:12.8.1-runtime-ubuntu24.04`, cu128 torch) + pinned lockfiles (`tts/requirements.txt` from `qwen-tts-env`, `tts/requirements-parakeet.txt` from `parakeet-env`). All three are gated behind a new `models` compose profile; new named volume `model-hf-cache` holds HF downloads; GPU pinning vLLM→GPU0, TTS+STT→GPU1. These are build-validated but **not running** — on Helios the model layer is still host systemd units (`vllm-primary.service`, `qwen-tts.service`, `parakeet-stt.service`), and Helios's `.env` deliberately keeps `COMPOSE_PROFILES=advanced` (no `models`) so compose does not double-start them on ports 8080/8002/8003. The `models` profile is for fresh single-box installs (`COMPOSE_PROFILES=models` or `advanced,models`). `scripts/detect_hardware.sh` reads `nvidia-smi` and prints `JESS_VRAM_TIER=24|32|48` + a suggested `VLLM_MODEL`. New env vars: `VLLM_MODEL`, `VLLM_SERVED_NAME`, `VLLM_MAX_MODEL_LEN`, `VLLM_GPU_MEM_UTIL`, `JESS_VRAM_TIER`, `QWEN_TTS_MODEL`, `QWEN_TTS_DTYPE`, `QWEN_TTS_FLASH_ATTN`, `TTS_VOICES_PATH`, `PARAKEET_MODEL` (see `docs/ENV_VARS.md` → Model layer). Removing the systemd units + repointing `MODEL_URL`/`TTS_URL`/`STT_URL` at compose-internal DNS is a later deploy-time slice.
- **Setup-wizard backend (Phase 3, 2026-05-16) — web UI deleted May 2026; consumed by `scripts/setup.sh` over localhost.** `orchestrator/routes_setup.py` adds an `APIRouter(prefix="/api/setup")` with bearer-gated endpoints: `GET /api/setup/status`, `GET /api/setup/hardware`, `POST /api/setup/complete`, plus the env-overrides surface (`GET/POST/DELETE /api/setup/env` + `POST /api/setup/env/validate`). Registered via `app.include_router(setup_router)` in `orchestrator.py`; startup logs first-boot state via `routes_setup.is_first_boot()`. State is two JSON files under the `/app/data` bind mount — no DB schema change: `setup_state.json` (`{setup_completed, completed_at, first_chat_completed}`, written here via atomic tmpfile+`os.replace`+fsync; `/complete` is idempotent and preserves both `completed_at` and `first_chat_completed`) and `hardware_scan.json` (read-only here). The orchestrator container is **CPU-only** — it cannot run hardware detection itself. `hardware_scan.json` is produced HOST-SIDE: the operator runs `bash scripts/detect_hardware.sh --json data/app/hardware_scan.json`, which (in addition to the normal `KEY=value` output) writes a structured scan — GPU list, driver, RAM, VRAM tier, model recommendation. `GET /api/setup/hardware` just serves that cached artifact (`{ok, available, scan}` when present, `{ok, available:false, hint}` with a re-run instruction when absent). **Env-overrides**: `orchestrator/setup_env.py` owns a `chmod 600` overlay at `/app/data/setup_overrides.env` loaded by `config.py` BEFORE `Settings()` (so wizard writes win over compose env); `/api/setup/env/validate` does live HA / Pushover / ntfy / Paperless checks via `httpx`. Write surface is **first-boot-only** (HTTP 410 after `/complete`; the first-boot check + write run under `setup_env._write_lock` so a concurrent `/complete` can't race past it). Allow-listed keys only — see `ALLOWED_KEYS` in `setup_env.py`. Secret keys never echo `value` on read-back; validate IS locked post-complete (hacker-tightened — was an SSRF/port-scan oracle otherwise). The web `/setup` page + `frontend/src/components/setup/*` were **deleted** in favor of the express CLI wizard (`scripts/setup.sh`) which calls these same endpoints over `localhost:8888` with the API_TOKEN bearer. API shapes: `TECHNICAL_REFERENCE.md` → Setup Wizard.
- **Dream install + first-chat welcome (v1.0.0, May 2026).** `install.sh` (root-level, 2-stage) brings up the FULL local-AI base on first run via `COMPOSE_PROFILES=models` — orchestrator + vLLM + qwen-tts + parakeet-stt + frontend, not just the orchestrator. Below-floor GPUs (<20 GiB usable VRAM) get an auto-substitution: `VLLM_MODEL=Qwen/Qwen3-8B-AWQ` + `VLLM_EXTRA_ARGS=--tool-call-parser hermes` + `VLLM_MAX_MODEL_LEN=16384` (overrides the .env.example Lorbus default; 8B AWQ has no MTP weights so the default speculative-config crashes). `scripts/setup.sh` is the **2-question express CLI wizard** (name + timezone — everything else takes defaults: `assistant_name=Jess`, `adhd_mode=true`, `tone=warm`, `TTS_VOICE=aiden`). The web `/setup` wizard + `frontend/src/components/setup/` + `frontend/src/app/setup/` are **DELETED** — only `/settings` remains for post-install changes. End-of-`setup.sh` runs `docker compose up -d --force-recreate orchestrator` (not `restart` — env_file changes need recreate). First-chat welcome: `orchestrator/welcome.py` (pure formatter) is wired in via `cloud_brain._maybe_prepend_welcome` to prepend a one-time markdown tour to the assistant's first reply; skips on fast-path + voice; detection via `routes_setup.is_first_chat()` + `mark_first_chat_done()` with the `first_chat_completed: true` flag in `setup_state.json`. `user_name`/`assistant_name` are stripped of markdown control chars (`[](){}*_<>|~#\`!\`) before interpolation. New env vars `install.sh` writes: `JESS_LAN_IP` (auto-detected via `ip -4 route get 1.1.1.1`, used by welcome for the clickable `/settings` URL), `DASHBOARD_TOKEN` (random 24-byte token printed at end of setup.sh; frontend middleware fallback is still `'changeme'`), `VLLM_EXTRA_ARGS` (parameterizes the Lorbus-27B-specific vLLM tuning), `GATEWAY_ROOT_PATH` (auto-detected — was hardcoded `/opt/gateway_mvp` in .env.example, which broke bind-mounts at non-standard install paths). `.env.example` defaults flipped from Helios-specific to portable: `MODEL_URL=http://vllm-primary:8000/v1`, `MODEL_NAME=qwen3.6-27b-int4`, `TTS_URL=http://qwen-tts:8002`, `STT_URL=http://parakeet-stt:8003`, `ORCHESTRATOR_URL=http://${JESS_LAN_IP:-localhost}:...`, `GATEWAY_ROOT_PATH=` (empty). `docker-compose.yml`'s `vllm-primary` switched to `entrypoint: ["/bin/sh", "-c"]` so `${VLLM_EXTRA_ARGS:-LORBUS_DEFAULTS}` can be expanded — lets below-floor overrides land. Metric: `bgw_welcome_fired_total{result}` (labels: `prepended` | `error`). Configuration of optional integrations (HA, ntfy, Pushover, Paperless) happens post-install via `/settings`; 4 short-lived `configure_*` chat tools were added in d9ce730 and removed in 6ca7d21 before reaching v1.0.0 (never canonical).
- **Helios wake-on-demand + manual sleep (PT-C):** The orchestrator runs 24/7 on the always-on box; Helios (RTX 5090 GPU box) runs only the model layer and is powered off most of the time to save electricity. Its NIC Wake-on-LAN is a dead end (Aquantia `atlantic` driver), so remote power control = power-cycling Helios's TP-Link Tapo smart plug **through Home Assistant** (reuses `HA_URL`/`HA_TOKEN` — no python-kasa, no TP-Link creds). BIOS `AC Back = Last State` means restoring plug power auto-boots Helios only if it was running when cut → "sleep" = `switch.turn_off` while running (a hard power-cut, safe because Helios is now stateless — all DBs/ChromaDB live on the always-on box). **Wake is AUTOMATIC** from the brain-asleep chat path (`cloud_brain._maybe_wake_helios` fires a debounced fire-and-forget `wake_helios()` with a strong-ref task set; the asleep reply now says it's waking Helios); **sleep is MANUAL only** (no idle auto-sleep). `orchestrator/helios_power.py` owns `wake_helios()` (debounced via module-level monotonic timestamp + optimistic slot-claim), `sleep_helios()`, `helios_power_status()` (returns `switch`/`watts`/`inferred running|asleep|unknown`); self-contained httpx, never raises (returns error dict), increments its metric exactly once per call — mirrors the F-013 pushover_manager pattern. Bearer-gated routes: `POST /api/helios/wake`, `POST /api/helios/sleep`, `GET /api/helios/power` (409 when disabled, 502 on HA error). New `helios_power` tool (`{action: wake|sleep|status}`) gated via `HELIOS_TOOL_NAMES` in `get_all_tools()` (handler stays registered, self-gates). When enabled a 60s `helios_status_poll` APScheduler job keeps `bgw_helios_plug_watts`/`bgw_helios_running` gauges fresh. Default-OFF behind `HELIOS_WAKE_ENABLED` (consistent with the productization boundary; NOT `JESS_ADVANCED`); `config.py` `validate_helios_wake_config` auto-disables (logs, never raises) if HA_URL/HA_TOKEN missing or either entity id malformed, and clamps negative debounce. A host helper `~/plug.sh on|off|state|power` exists on the always-on box for manual ops, but the orchestrator uses the HA API directly and never shells out. Metrics: `bgw_helios_wake_total{result}` (`ok|debounced|disabled|error`), `bgw_helios_sleep_total{result}` (`ok|disabled|error`), `bgw_helios_status_total{result}` (`ok|disabled|error`), `bgw_helios_plug_watts`, `bgw_helios_running`. Env vars: `HELIOS_WAKE_ENABLED`, `HELIOS_PLUG_ENTITY`, `HELIOS_PLUG_POWER_SENSOR`, `HELIOS_WAKE_DEBOUNCE_SECONDS` (see `docs/ENV_VARS.md`). API shapes + tool schema: `TECHNICAL_REFERENCE.md`.
- **Home Assistant migrated to Jupiter (2026-07-04).** HA ran on a Raspberry Pi at 10.0.0.106 until its SD card failed (booted to an emergency console — classic Pi-HA death from recorder writes). It now runs as a docker container on Jupiter (host-networked, `:8123`), managed from `homeassistant/docker-compose.yml` (its own compose project, like the monitoring stack — NOT in the main `docker-compose.yml`; the orchestrator's build/deploy never touches it). Image pinned to `2026.5.1`; config volume at `/home/labadmin/homeassistant/config` (external, gitignored). `HA_URL` changed `http://10.0.0.106:8123` → `http://10.0.0.248:8123`. The setup is all-network (ESPHome WiFi, Bluetooth via remote proxies, Cast, cloud — no USB radios), so the migration was a config copy + container. Full runbook (run/upgrade, backup/restore, Saturn failover): `docs/HA.md`. The Pi is dead but untouched; pristine rescue config kept at `~/ha-rescue` on Jupiter + Saturn.
- **Nightly backups to Saturn (2026-07-04).** Two independent nightly cron jobs on Jupiter back up the irreplaceable state off-box to Saturn, each with a consistent SQLite snapshot + Prometheus staleness alert: (1) orchestrator state — `scripts/backup_state.py` (cron 03:30), backs up `data/app/` (brain_state/progress/finance DBs + `auto_learn.key` — the Fernet key that decrypts learned facts) + chroma + credentials, excludes the reconstructable `hf_cache`; alert `JessBackupStale` (>36h). See `docs/BACKUP.md`. (2) HA config — `homeassistant/backup_ha.sh` (cron 03:45); alert `JessHABackupStale`. Both rsync to `saturn:/home/labadmin/*-backups/`. The weekly Google-token refresh cron (Mondays 04:00, `scripts/refresh_google_token.py`, alert `GoogleTokenRefreshStale`) keeps Calendar/Gmail alive independent of orchestrator uptime.
- **vLLM Phase 3 cutover landed 2026-04-26 (Plan A, not Plan B).** Primary model is now `Lorbus/Qwen3.6-27B-int4-AutoRound` served by `vllm-primary.service` (wraps `docker run vllm/vllm-openai:v0.19.1`, port 8080) pinned to GPU0 RTX 5090 — the same card the Phase 2 trial validated. Coder repinned GPU0 → GPU1 (`CUDA_VISIBLE_DEVICES=0` → `1`); voice services (qwen-tts, parakeet-stt) stayed on GPU1. `llama-server.service` is disabled (unit retained on disk, same pattern as `llama-server-moe.service`). `MODEL_NAME` and `FALLBACK_MODEL_NAME` flipped to `qwen3.6-27b-int4`. Why Plan A and not Plan B (which would have moved vLLM to the 48 GB PRO 5000): a GPU1 bench showed 28–79% of Phase 2 throughput because the PRO 5000 has lower memory bandwidth and fewer SMs than the 5090. Trade-off: when vLLM 0.19.2 ships and we want the full 256K context, the primary will need to migrate to GPU1 (32 GB on the 5090 can't hold Lorbus + 256K KV). Rollback script: `/home/labadmin/vllm-trial/rollback_phase3.sh` (idempotent). Phase 2 trial details + Plan A vs B rationale live in `docs/internal/VLLM_PHASE_3_PLAN.md` (now marked DONE with Outcome section).

