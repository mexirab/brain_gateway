# Brain Gateway

> **For AI assistants (Claude Code, Cursor, etc.):** This file is the developer briefing for AI coding tools working in this repo. End users installing Brain Gateway should read `README.md` instead. Maintainer reference docs (Helios cluster details, historical migration plans) live in `docs/internal/`.

Personal AI assistant for ADHD support. One model (Qwen3.8-Flash-Next on llama.cpp, `llama-server-primary.service` on Helios) handles conversation and tools in one unified agentic loop.

## Post-change review workflow (MANDATORY)

After any code change, before reporting the task done, run the `review-change` skill (`/review-change`) — even when the change looks small. It starts the reviewer agents in parallel (`code-reviewer`, `security`, `prod-support`, plus `frontend` / `hacker` when the diff warrants), then runs the `unit-test` and `docs-updater` skills in this session. Fix every HIGH or EXPLOITABLE finding, give the user each medium/low one with a one-line recommendation, and never hide or soften a finding.

Helpers: agents (`.claude/agents/`) are the independent reviewers and noisy investigators (`code-reviewer`, `security`, `prod-support`, `frontend`, `hacker`, `model-scout`). Skills (`.claude/skills/`) are how-tos that need this session's context (`review-change`, `unit-test`, `docs-updater`, `observability-rules`).

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Helios | 10.0.0.195 | — (off Tailscale by design; `ssh -J` through Jupiter or Saturn) | RTX 5090 + RTX PRO 5000 | **GPU model layer, power-tiered**: asleep most of the time, woken through an HA smart plug. Brain + vision (`llama-server-primary`, :8080), STT on CPU (:8003). No TTS. |
| Jupiter | 10.0.0.248 | jupiter-amds.tail74fc4a.ts.net | 2x RTX 5080 (16GB each) | **Always-on hub**: orchestrator, frontend, HA, monitoring, Actual, Radicale, Grist, live TTS (VoxCPM2). The voice card is `cuda:0`: pin experiments to the other 5080 by UUID, never by index. |
| Saturn | 10.0.0.58 | saturn-3090.tail74fc4a.ts.net | RTX 3090 (24GB) + RTX 3080 (10GB) | **Backup box**, no Jess runtime services: off-box backup target, planned home of the dead-man's switch, spare GPUs. |
| Uranus, Pi, Callisto (retired) | 10.0.0.173, 10.0.0.106, 10.0.0.136 | - | - | Decommissioned (old test box — its two RTX 5080s are now Jupiter's; old HA Pi; old kiosk Pi). Don't use. |

Per-node detail (what runs where, access, GPUs): `docs/internal/HELIOS_INFRASTRUCTURE.md` → Cluster nodes.

## Services

Compose profiles (`COMPOSE_PROFILES` in `.env`): `advanced` adds `nebula-sync`; `models` adds the model layer (`vllm-primary`, `qwen-tts`, `parakeet-stt`) for fresh single-box installs. The `models` profile is **not deployed on Helios**: those servers run as host systemd units there, and its `.env` keeps `COMPOSE_PROFILES=advanced` so compose doesn't double-start them. See `docs/ENV_VARS.md` → Distribution profile + Model layer.

| Service | Port | URL |
|---------|------|-----|
| Orchestrator (Jupiter) | 8888 | http://10.0.0.248:8888 |
| Primary LLM + vision (Helios; alias `qwen3.8-flash-next-q3kxl`; every path but `/health` needs `Bearer $MODEL_API_KEY`) | 8080 | http://10.0.0.195:8080/v1 |
| TTS (VoxCPM2, Jupiter host unit `voxcpm-tts`, socket-activated, Docker networks only; every path but `/health` needs `Bearer $TTS_API_KEY`) | 8004 (172.17.0.1 only) | http://host.docker.internal:8004 from containers (a host-side curl times out) |
| STT (Parakeet TDT 0.6b v2, int8 ONNX on CPU, Helios) | 8003 | http://10.0.0.195:8003 |
| Wyoming Whisper STT / Jessica TTS bridges (HA Assist, Helios; the TTS bridge gets no audio now) | 10300 / 10301 | tcp://10.0.0.195:10300, :10301 |
| Frontend (dashboard) | 3001 (127.0.0.1 via `FRONTEND_BIND_ADDR`) | https://jupiter-amds.tail74fc4a.ts.net (Tailscale Serve, tailnet-only). See `docs/FRONTEND.md` → HTTPS |
| Grafana (Jupiter) | 3000 (127.0.0.1 only) | https://jupiter-amds.tail74fc4a.ts.net:3000 (Tailscale Serve, login required) |
| Prometheus / Alertmanager / Loki / Blackbox (Jupiter) | 9090 / 9093 / 3100 / 9115 (127.0.0.1 only) | No auth: the loopback bind is the control. Containers use `prometheus:9090`, `alertmanager:9093`, `loki:3100`. See `monitoring/README.md` |
| loki-push-gw (Jupiter) | 3101 (LAN) | Push-only nginx in front of Loki for Helios logs (`NODE_HELIOS_IP` + `LOKI_PUSH_TOKEN`) |
| Promtail / nut-exporter (Helios) | 9080 / 9199 | Own compose projects on Helios (`/opt/promtail-helios`, `/opt/nut-exporter-helios`). See `monitoring/README.md` → Log Pipeline |
| SearXNG (Jupiter) | 8090 (127.0.0.1 only) | Jess uses `http://searxng:8080` over `searxng-net`. See `docs/ENV_VARS.md` → Web search (SearXNG) |
| Actual Budget (own compose `actual/`) | 5006 (127.0.0.1 only) | https://jupiter-amds.tail74fc4a.ts.net:5007; containers: `http://actual-budget:5006`. See `docs/BACKUP.md` → Actual Budget |
| Radicale CalDAV (own compose `radicale/`) | 5232 (127.0.0.1 only) | https://jupiter-amds.tail74fc4a.ts.net:5233; containers: `http://radicale:5232`. See `docs/CALENDAR.md` |
| Grist (own compose `grist/`; `spreadsheet` tool) | 8484 (127.0.0.1 only) | https://jupiter-amds.tail74fc4a.ts.net:8485; orchestrator: `http://grist:8484` over `grist-net`. See `docs/SPREADSHEETS.md` |
| Voice satellites (ATOM Echo, ESPHome API; default off) | 6053 | `VOICE_SATELLITE_DEVICES`. See `docs/VOICE_AND_TTS.md` → Voice satellite |
| jupiter-deadman (Saturn, systemd; **not installed yet**) | 9466 | See `monitoring/deadman/README.md` |
| Pi-hole (Jupiter + Saturn; no clients, LAN DNS is on the router) | 53/8053 | http://jupiter-amds.tail74fc4a.ts.net:8053/admin. See `docs/FOCUS_AND_PIHOLE.md` |
| Retired / disabled: code agent :8082 + Qwen3-TTS :8002 (Helios), expert model :8084 + vision :8010 (Saturn) | — | Nothing uses them; don't point anything at them. See `docs/internal/HISTORY.md` |

## Architecture (v7 Unified)

```
User -> Dashboard / HA Assist / Telegram -> Orchestrator -> Unified Loop -> Model (Qwen3.8-Flash-Next UD-Q3_K_XL via llama.cpp)
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

One model calls every tool directly in one agentic loop. **[advanced]** = shown only with `JESS_ADVANCED=true` (default off, cut from the shippable build; `docs/ENV_VARS.md` → Distribution profile). "Hidden unless X" = left out of the schema while flag X is off; the handler stays registered and self-gates. Schemas and behavior: `TECHNICAL_REFERENCE.md` → Tool Schemas.

| Tool | Purpose |
|------|---------|
| home_assistant | Call an HA service (`{entity_id, service, data}`) on the listed entities. Every call passes `ha_intent` ("did the user ask?") and `ha_guard` (allow-listed domains/services, no unlock/cover-open, deny list, device settings). |
| search_memory | Semantic search of the memory palace, optional wing/room filter. |
| update_memory | Store a correction or new fact; replaces only clearly matching older facts. Meds/projects go through `update_data`. |
| set_reminder / cancel_reminder | Voice/phone reminders. |
| update_data | Write meds/projects YAML, the source of truth (incl. per-med weekdays). |
| get_data | Read meds/projects/profile from the YAML. The read path for meds/schedule questions, never RAG/`search_memory`. |
| calculate | Exact math and local date/time math in a restricted interpreter (no eval). Never for doses. |
| start_focus / stop_focus / focus_status | Focus sessions: sprints, check-ins, ambient audio. Site blocking is deprecated here (`FOCUS_BLOCKING_ENABLED=false`). |
| focus_sprint | Next sprint, extend the current one, or end the session with a summary. |
| web_search | Web search via the local SearXNG. Also a voice tool. |
| check_calendar / create_calendar_event | Merged read (CalDAV + work Exchange push + Google) that names any source it couldn't reach. Create writes to CalDAV (Google only if CalDAV is off, never Exchange). |
| check_email / search_email | Gmail inbox, read-only. |
| decompose_task / task_step | Break a task into micro-steps and advance through them. |
| start_routine / routine_action / routine_status | Step-by-step morning/evening routines with TTS guidance. |
| decide_for_me | Decision simplifier: one or two concrete recommendations. |
| selfcare_log | Log meals, meds, water, movement for self-care nudge tracking. |
| bookmark_context / recall_context | Interruption recovery: save and recall work context. |
| brain_dump | Capture thoughts/tasks/ideas and route them to memory or reminders. |
| check_system | System diagnostics: logs, health, recent errors. |
| finance_status | Current-period Quest Board from the read-only Actual Budget sync. **[advanced]** |
| analyze_image | Follow-up questions about a shared photo (Helios brain via `VISION_*`). Photo text is framed as untrusted content. |
| ask_expert | **Deprecated**, off on the live deployment. Delegates hard reasoning to an expert model. **[advanced]** + `EXPERT_ENABLED`. |
| shopping_list | Add/check/remove items on shopping and grocery lists. |
| document_vault | Structured document store with semantic search (list/create/read/update/delete). |
| paperless_save / paperless_search / paperless_read | Push a staged file to Paperless-ngx for OCR; read-only archive search and paged OCR text, framed untrusted. Hidden unless `PAPERLESS_ENABLED`. |
| spreadsheet | Read, append and update allow-listed Grist sheets; no delete, writes only when asked. Hidden unless `GRIST_ENABLED`. |
| look_it_up | Sandboxed research helper for coding questions (docs + public web, no personal context). Hidden unless `RESEARCH_HELPER_ENABLED`. |
| check_claude_activity | What Claude Code has been working on. **[advanced]** |
| ~~code_agent~~ | Removed (it gave a model shell + git). Self-troubleshooting = the read-only self-investigator. |
| sleep_mode | Do Not Disturb: no announcements until morning. |
| voice_style | How Jess's voice sounds, for one reply or ongoing. Voice tool. |
| generate_workout / workout_setup / log_set / workout_status / modify_workout | Gym: today's plan (weekly program or recent history, the location's equipment, progression), program + locations setup, set logging, plan/history/form steps, swaps. Hidden unless `WORKOUTS_ENABLED`. |
| log_meal | Log a meal with its calories. Hidden unless `MEALS_ENABLED`. |
| pantry / meal_plan | Kitchen inventory (`out` also adds to the grocery list) and the weekly meal-prep plan (accept only on the user's say-so). Hidden unless `PANTRY_ENABLED`. |
| query_budget | Budget history from the synced Actual dataset or old imports; `analyze` mode for synthesis. **[advanced]** |
| helios_power | Wake, sleep or check the Helios GPU box via its HA smart plug. Hidden unless `HELIOS_WAKE_ENABLED`. |

## Key Files (load-bearing)

The files you'll touch most often, with the rule to know before editing each. Everything else follows `<feature>_manager.py` / `routes_<feature>.py` / `jobs_<feature>.py` naming: grep by feature name, or see `docs/DEV.md` → Module map. A module's docstring is usually its spec: read it first.

| File | Purpose |
|------|---------|
| `orchestrator/orchestrator.py` | FastAPI app, main chat endpoint, startup/shutdown, scheduler job registration |
| `orchestrator/unified_loop.py` | v7 unified agentic loop — single model conversation + tool execution. THE main chat flow. |
| `orchestrator/cloud_brain.py` | Model routing, fallback, unified-loop orchestration. `_sanitize_client_history` runs first on every path (incl. the fallback model): only client `user`/`assistant` turns survive, markup neutralized; no image part ever reaches the brain (`_process_vision`) |
| `orchestrator/prompt_builder.py` | `get_unified_system_prompt`: byte-stable STATIC block → `DYNAMIC_CONTEXT_MARKER` → per-turn context. Never put volatile text above the marker; never add a second `system` message (Qwen3.8 template rejects it) |
| `orchestrator/tool_handlers.py` + `tool_registry.py` + `tool_definitions.py` | All `tool_*` handlers + `execute_tool`; `@register_tool` registration + metrics wrapping; the JSON schemas the LLM sees. Keep schemas and handlers in sync |
| `orchestrator/config.py` + `shared.py` | Pydantic Settings, every env var in one place; module-level state + env aliases re-exported from `config.py` |
| `orchestrator/model_auth.py` | Sends `MODEL_API_KEY` / `TTS_API_KEY` only to the model / TTS server's own origin, never to another host. Docstring = the rule |
| `orchestrator/state_store.py` | SQLite persistence (`brain_state.db`): reminders, focus, chat, inbox, workouts, `app_state` KV and more |
| `orchestrator/mempalace.py` + `auto_learn.py` | The memory system (store, search, wing/room routing, wakeup block) + background fact extraction (encrypted, deduped, one job per conversation). `docs/MEMPALACE.md` |
| `orchestrator/embedding_pin.py` | Pinned embedding model + remote-code revisions (`load_kwargs()`), bumped together with `transformers` in `requirements.txt`. Procedure in the docstring |
| `orchestrator/reminder_manager.py` + `reminder_actions.py` | Announcements (`_announce_voice`: DND gate, speaker routes) + reminders. `reminder_actions` is THE Done/Snooze for every channel: change ack/snooze semantics there only |
| `orchestrator/background_jobs.py` | Re-export facade; job code lives in `jobs_<domain>.py` |
| `orchestrator/calendar_sources.py` | THE calendar read/write path (CalDAV + Exchange push + Google, reports `failed_sources`). `docs/CALENDAR.md` |
| `orchestrator/ha_guard.py` | THE guard for every user/model-steerable HA call (chat tool, NLP/fast path, `/api/ha/command`). Internal callers bypass it |
| `orchestrator/ha_intent.py` | "Did the user ask?" gate on the chat model's `home_assistant` calls only. Module docstring = rules + known limits |
| `orchestrator/brain_slot.py` | Who gets the single brain slot (`--parallel 1`): chat always first; background idle-waits also treat a coder request as busy (`coder_busy()`) |
| `orchestrator/actual_client.py` + `finance_manager.py` | Read-only Actual Budget sync → `finance.db`. `actual_client` docstring defines spending + the SQL confinement: read it before changing either or bumping `actualpy`. Mass-delete guard: comments above `MASS_DELETE_MIN_ROWS` |
| `orchestrator/research_helper.py` | `look_it_up` helper. Module docstring is the authoritative security story |
| `orchestrator/calculator.py` | The `calculate` tool's language: AST allow-list + own interpreter, never eval/exec. Module docstring = the safety model |
| `orchestrator/coder_gate.py` + `routes_coder.py` | Coder-box gate (default-off `CODER_ENABLED`). Module docstring = the guarantees; `TECHNICAL_REFERENCE.md` → Coder Gate |
| `coder/` + `scripts/jess-coder` | Coder box image + host launcher (`box_command()` = the only `docker run`). `docs/CODER.md` |
| `orchestrator/jobs_self_audit.py` + `self_investigator.py` | Daily self-audit + read-only investigator. Nothing executes model output; docstrings = the safety story. `jess-features/F-014-self-audit.md` |
| `orchestrator/routes_config.py` | `/api/config/*` behind `/settings`; YAML writes go through `config_writer.atomic_write_yaml()` + `log_config_change()`. A new `(private)/` dashboard page must be in `PROTECTED_PATHS` + `config.matcher` (CI test) — `docs/FRONTEND.md` |
| `orchestrator/voice_satellite.py` | ATOM Echo bridge (default-off `VOICE_SATELLITE_ENABLED`), with `voice_match.py` + `audio_stream.py`. Docstring = event contract (`RUN_END` alone), decisions, known limits. `docs/VOICE_AND_TTS.md` |
| `orchestrator/voice_style.py` | TTS style resolution. Only `PRESETS` phrases ever reach the TTS, never free text |
| `orchestrator/routes_setup.py` + `welcome.py` | First-boot setup backend for `scripts/setup.sh` (`setup_state.json`) + the one-time first-chat welcome. `docs/INSTALL.md` |
| `orchestrator/metrics.py` | All `bgw_*` Prometheus metrics. Source of truth for dashboards |
| `docker-compose.yml` | Service stack (env-var driven, no hardcoded IPs) |
| `tts/llama-server-primary.service` | Repo copy of the LIVE Helios brain unit (install to `/etc/systemd/system/` on Helios). Header comments are authoritative: flags, VRAM, rollbacks, `VOICE_THINKING_ENABLED=true`, API key. Rollback units: `tts/llama-server-primary-27b-q6k.service`, older `tts/vllm-primary.service` |
| `tts/voxcpm_server.py` + `tts/voxcpm-tts-jupiter.service` | The live TTS server on a Jupiter RTX 5080. Unit header = install, card pinning, sandbox |
| `theme/` | Suite theme: edit only `tokens.json`, then `python3 theme/build.py`. Never hand-edit outputs (CI `check-theme` fails the deploy on drift) |
| `scripts/container-firewall/jess_docker_input.py` | Jupiter Docker → host INPUT allow-list; allow-lists networks, not containers. Touching a compose network of the orchestrator / prometheus / blackbox-exporter → read its docstring first |
| `scripts/jupiter-kiosk/`, `monitoring/deadman/` | Hand-installed host tools, CI never deploys them: closet monitors kiosk (never on the voice card), off-box dead-man's switch (Saturn). Install steps: each folder's README |

## Key Paths

```
/home/labadmin/gateway_nerves/       # Project root on Jupiter (the always-on hub)
/home/labadmin/gateway_nerves/.env  # Secrets + config (HA_TOKEN, API_TOKEN, MODEL_API_KEY, …; gitignored)
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

# Deploy = merge to main: CI's Jupiter runner fast-forwards the live checkout and rebuilds changed services.
# Never `git pull` / edit the live checkout by hand (docs/DEV.md → CI and auto-deploy)
# Manual rebuild of what's already checked out, from a Mac over Tailscale:
ssh labadmin@jupiter-amds "cd /home/labadmin/gateway_nerves && docker compose up -d --build orchestrator"

# Frontend rebuild
docker compose up -d --build --force-recreate frontend

# New/changed files under RAG_BASE are ingested automatically (job rag_ingest_watch, every 2 min, in-process).
# Re-embed everything (only after an embedding-model change): reindex_rag.py writes Chroma directly and Chroma isn't
# safe for two writers, so back up CHROMA_HOST_PATH, stop the orchestrator, run it one-off (docs/MEMPALACE.md → Offline repairs)
docker compose stop orchestrator && docker compose run --rm --no-deps orchestrator python scripts/reindex_rag.py && docker compose up -d orchestrator

# Detect VRAM tier (prints JESS_VRAM_TIER + suggested VLLM_MODEL; read-only)
bash scripts/detect_hardware.sh

# Orchestrator runs as non-root uid ORCHESTRATOR_UID: report / fix ownership of its mounts
bash scripts/orchestrator_state_perms.sh [--apply]

# Validate the model-layer compose stanzas without starting anything
docker compose --profile models config

# Run tests in a throwaway container, never inside brain-orchestrator (why each flag is there: the unit-test skill)
docker run --rm --tmpfs /tmp:rw,exec,size=8g -v "$PWD/orchestrator:/app/orchestrator:ro" -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data:/app/data_repo:ro" -w /app gateway_nerves-orchestrator \
  sh -c "pip install -q pytest pytest-asyncio respx && python -m pytest -o asyncio_mode=auto -p no:cacheprovider orchestrator/tests -q"

# Run specific test file: swap orchestrator/tests for orchestrator/tests/test_progress_tracker.py
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
| `docs/DEV.md` | Module map (every orchestrator file in one line), CI jobs (`.github/workflows/ci.yml`), the auto-deploy to Jupiter, test-writing rules |
| `ROADMAP.md` | planning new features or checking what's shipped vs planned |

**Subsystem (read when the task is specifically about…)**

| Doc | Read when you're working on… |
|-----|------|
| `docs/MEMPALACE.md` | memory system internals, write paths, MCP server, session mining |
| `docs/CLAUDE_CODE_INTEGRATION.md` | the Stop hook or `check_claude_activity` tool |
| `docs/FOCUS_AND_PIHOLE.md` | focus timer, Pomodoro flow, Pi-hole blocking (deprecated for this deployment — status + re-enable steps), Nebula Sync (historical) |
| `docs/VOICE_AND_TTS.md` | ATOM Echo voice satellite (firmware, decisions D1–D14, VAD tuning, several devices + duplicate wakes, voice match), VoxCPM2 TTS server + the Jupiter move runbook (measurements, cutover, rollback), TTS pacing, Wyoming bridges, STT config |
| `docs/GOOGLE_INTEGRATIONS.md` | Calendar API, Gmail API, phone sync, travel-time alerts, OAuth2 setup |
| `docs/FRONTEND.md` | dashboard pages, widgets, theme (tokens, light/dark, nav, safe areas), finance (Actual Budget sync), API proxy pattern, auth (login cookie, read-only kiosk session) |
| `docs/WORKOUTS_AND_MEALS.md` | Workout generator adaptive logic, meal photo flow, API endpoints, env vars |
| `docs/PANTRY.md` | Pantry (F-015): inventory, `pantry` tool, photo stock-take, receipt scans, barcode lookup, diet settings + weekly meal planner (`meal_plan` tool), consumption tracking (cook/portions/decay/fridge check), env vars, metrics |
| `docs/MODE_ROUTER.md` | intent classification (explainer/mirror/counterbalance/challenge/baseline) |
| `docs/SPREADSHEETS.md` | Self-hosted Grist on Jupiter: security model (forwarded-header trust, isolated networks), deploy order, least-privilege Jess user, rollback, known limits |
| `docs/CALENDAR.md` | Self-hosted Radicale CalDAV on Jupiter: layout, network exposure, users/rights, client setup, backup/restore, monitoring |
| `docs/HA.md` | Home Assistant on Jupiter: run/upgrade, the `homeassistant/` compose, backup/restore, Saturn failover, migration history |
| `docs/CODER.md` | The coder box (`jess-coder`, sandboxed Qwen Code → patches): threat model, refused paths, setup, usage, self-test, monitoring, Qwen Code upgrades, lab testing |
| `docs/BACKUP.md` | orchestrator state backup (`scripts/backup_state.py`), what's backed up, restore, off-box to Saturn |
| `docs/internal/HELIOS_INFRASTRUCTURE.md` | Cluster nodes (what runs where, access, GPUs), Helios GPU layout, temperature monitoring, egress audit (maintainer reference) |
| `docs/internal/HISTORY.md` | How we got here: every brain era, migrations between boxes, removed and retired services |
| `docs/internal/POWER_TIERING_SPEC.md` | Helios wake-on-demand (PT-C): design + as built |
| `docs/internal/FLASH_NEXT_TRIAL.md` | The live brain (Qwen3.8-Flash-Next, 2026-10-24): why this model/quant, VRAM fit, A/B + no-action probe results vs the 27B, the voice-thinking requirement, gotchas, known limits |
| `docs/internal/LLAMA_SERVER_REMOTE_MEDIA.md` | llama-server fetches http(s) image URLs (blind SSRF from Helios): the finding + brain-side options. `--api-key-file` enforced on Helios since 2026-10-29; narrow `IPAddressAllow` + Loki detection alert still open |
| `docs/internal/CONTAINER_HOST_REACHABILITY.md` | Which Jupiter host services each Docker network can reach (measured 2026-11-21), what containers need from the host, options compared, the `jess-docker-input` design + install record, what it doesn't cover. Runbook: `monitoring/README.md` → Container → host firewall |
| `docs/internal/VLLM_PHASE_3_PLAN.md` | Historical: 2026-04-26 cutover plan (Plan A vs Plan B rationale, rollback notes) |
| `docs/internal/QWEN38_PREP_RESULTS.md` | Historical (superseded 2026-10-02): the Qwen3.8 NVFP4 / vLLM 0.27.1 brain — tested config, trials, acceptance session, 2026-09-28 Helios state. Live brain is llama.cpp Flash-Next; see `tts/llama-server-primary.service` header |
| `docs/TRAINING_CORPUS.md` | working on fine-tune data pipelines, debugging missing conversation data, or changing the drain schedule |
| `monitoring/README.md` | Prometheus, Grafana, Loki — scrape targets, dashboard generator, alerts, off-box dead-man's switch (Saturn install + test plan: `monitoring/deadman/README.md`) |
| `monitoring/grafana/dashgen/` | editing dashboards (Python generator; don't hand-edit the JSON) |

**User-facing**

| Doc | What |
|-----|------|
| `docs/JESS_QUICK_START.md` | User-facing feature guide — every voice command, grouped by domain |
| `docs/JESS_REFERENCE_CARD.md` + `.html` | Printable ADHD reference card — grouped by *situation*, not feature |
| `jess-features/README.md` | ADHD feature spec index (F-001 through F-015) — read for implementation rationale |

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
| `jess-features/F-014-self-audit.md` | Daily Self-Audit + read-only investigator | P2 |
| `jess-features/F-015-pantry-meal-prep.md` | Pantry & Meal Prep (all 4 phases built: inventory, weekly plan, receipts, consumption tracking) | P1 |

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_nerves` (from the checkout's directory name): images `gateway_nerves-<service>`, networks `gateway_nerves_<network>`.

### Cluster, brain and host services

- **Helios SSH:** `labadmin@10.0.0.195`, LAN only. Helios stays off Tailscale by design (`tailscaled` is logged out — don't re-join it). From off the LAN: `ssh -J labadmin@jupiter-amds labadmin@10.0.0.195`. Its ports (:8080, :8003) can't be reached from the tailnet.
- **Brain: Qwen3.8-Flash-Next (since 2026-10-24).** `llama-server-primary.service` on Helios, GPU0 + GPU1, 262K context, also serves vision. **Requires Jupiter `.env` `VOICE_THINKING_ENABLED=true`** (without it this brain took unrequested HA actions; a mitigation, not a control: `ha_intent` is the gate. The flag defaults `false`, so never restore a pre-2026-10-24 `.env` with this unit installed). **Never start `voxcpm-tts` on Helios with the 262K unit installed** (it doesn't fit). Authoritative: the header of `tts/llama-server-primary.service`; detail `docs/internal/HELIOS_INFRASTRUCTURE.md`, known limits `docs/internal/FLASH_NEXT_TRIAL.md`.
- **Brain API key:** the llama-server units run with `--api-key-file`: every :8080 path but `/health` needs `Bearer $MODEL_API_KEY`, which `model_auth.py` sends to the model origin only. A wrong key looks like "Helios asleep" (`BrainAuthRejected`). Rolling back to `vllm-primary` drops enforcement. Enable/rotate: unit header; egress audit: `docs/internal/HELIOS_INFRASTRUCTURE.md` → Egress audit.
- **llama-server rules (live and rollback units):** single slot `--parallel 1` (background LLM callers queue behind chat; watch `llamacpp:requests_deferred`). Temperature ≤ 1.0 and `repeat_penalty` 1.0, or MTP acceptance collapses. If output garbles, the first lever is `--spec-type none`. Never add `--verbose` or `--slot-save-path` (they put prompt text in the journal / on disk). Don't add `MemoryDenyWriteExecute` or `PrivateDevices` (both break CUDA). Rollbacks (64K, 27B, vLLM): unit headers in `tts/`.
- **Helios power-tiering (PT-C):** Helios is asleep most of the time; the orchestrator powers it through its HA smart plug (`helios_power.py`, tool `helios_power`, `/api/helios/*`). Wake is automatic from the brain-asleep chat path; sleep is manual only (a hard power cut, safe because Helios is stateless). Default-off: `HELIOS_WAKE_ENABLED`. Detail: `docs/internal/POWER_TIERING_SPEC.md` → As built.
- **Model history:** earlier brains (v6 hybrid → Qwen3.5-27B → Qwen3.6 and Qwen3.8 NVFP4 on vLLM → Qwen3.8-27B Q6_K → Flash-Next), past migrations and removed services (ollama, Helios Pi-hole, model-server, code agent, expert model): `docs/internal/HISTORY.md`.
- **TTS (VoxCPM2 on Jupiter):** `voxcpm-tts.service` on one Jupiter RTX 5080, `TTS_URL=http://host.docker.internal:8004`, style notes from `voice_style.py`. The socket admits Docker networks only (a host curl times out) and every path but `/health` needs the bearer key (`TTS_API_KEY`). Wire IDs `jessica-tts` / `jessica` stay for HA back-compat. Detail: `docs/VOICE_AND_TTS.md` → VoxCPM2 TTS server, Voice style.
- **NVIDIA module check (Jupiter):** an hourly host timer (`scripts/check_nvidia_modules.py`) fires `NvidiaModuleMissing` when the running or next-boot kernel has no NVIDIA module (unattended-upgrades holds the module packages back). Fix **before** rebooting: `sudo apt full-upgrade`. Host units are owner-installed; CI never deploys them. Detail: `monitoring/README.md` → TTS on Jupiter.
- **Home Assistant** runs on Jupiter from its own compose project in `homeassistant/` (host network, :8123, pinned image), not the main `docker-compose.yml`; the orchestrator's deploy never touches it. Runbook (upgrade, backup, restore, Saturn failover): `docs/HA.md`.
- **Container → host firewall (`jess-docker-input`, Jupiter, OBSERVE mode):** a host INPUT allow-list per Docker network; host traffic arrives on a container's default-route bridge, so only `host-access` / `host-scrape` are allowed. Keep `gw_priority: 1` on them; never add members (a container needing a host port gets its own network + allow line). Install + enforce are manual owner steps. Detail: `monitoring/README.md` → Container → host firewall.
- **Stale `/mnt` mounts (Jupiter):** after a disk dropout + remount, running containers keep the dead filesystem (bind mounts resolve at container start); host units in `scripts/usb-storage/` restart only the stale ones. Hand-installed on Jupiter; CI never deploys them. Recovery: `sudo systemctl start usb-storage-remount.service`; never `docker compose start` a whole project. Detail: `monitoring/README.md` → USB Storage.

### Prompt, tool loop and data safety

- **Prompt cache layout + loop hardening (since 2026-10-02):** `prompt_builder.get_unified_system_prompt` = byte-stable STATIC block → `DYNAMIC_CONTEXT_MARKER` → per-turn context → `DYNAMIC_CONTEXT_FOOTER`, for llama.cpp prefix caching. **Maintainer rules: never put per-turn/volatile text above the marker; never add a second system message** (the Qwen3.8 template rejects it); keep the HA entity list in the schema sorted (an unstable order invalidates the whole cache prefix); tool trigger phrases belong in the schemas, not the prompt. The MEDICATION SAFETY (dose) and no-verbatim-dump rules are SOFT: known limits, not protections. Loop hardening (`unified_loop.py`): every tool result goes through `_neutralize_control_markup()`, the XML tool-call fallback is gated (`_xml_fallback_allowed`), a round is capped at 5 calls; `cloud_brain._sanitize_client_history` runs first in `_chat_unified_inner`, so every path, fallback included, gets the cleaned history. Detail + open security gaps: `TECHNICAL_REFERENCE.md` → System Prompt Layout, Tool-Call Loop Guards → Known gaps.
- **Tool result cap:** the unified loop caps every tool result at 8000 chars (~2000 tokens), after `_neutralize_control_markup()` rewrites tool-call / chat-template markup as quoted text. Tools that return large blobs must summarize/paginate in the handler; the cap is a safety net. Detail: `TECHNICAL_REFERENCE.md` → Tool Result Cap.
- **Real streaming:** `/v1/chat/completions` with `stream=true` relays model tokens over SSE while the tool loop runs; `unified_loop.StreamGate` hides `<think>`/`<tool_call>` blocks, tool calls are trusted only from a cleanly finished stream, failures fall back to buffered. `REAL_STREAMING_ENABLED` is read at process start: flipping it needs `docker compose up -d --force-recreate orchestrator`. Detail: `TECHNICAL_REFERENCE.md` → Real Streaming.
- **`calculate` tool:** the IMPORTANT RULES MATH rule sends any arithmetic past one simple step to `calculator.py` (restricted AST interpreter, never eval/exec); never for doses. Its dose-word refusal is a SOFT backstop for MEDICATION SAFETY, not a control: neutral names and bare numbers still compute. Failures return as text, never raised. Detail: `TECHNICAL_REFERENCE.md` → calculate.
- **No personal content in logs:** log lengths, ids, categories, dict keys — never message/query/reminder/calendar/med/meal/task text, tool args, or the user's name, at any level (Loki keeps 30 days and the self-audit investigator reads it, on Anthropic's API when `SELF_AUDIT_CLAUDE_API_KEY` is set). Enforced by `orchestrator/tests/test_no_user_content_in_logs.py`; audited exceptions need `# log-safe: <reason>`. Full rules: the `observability-rules` skill.
- **Research helper (`look_it_up`, default-off `RESEARCH_HELPER_ENABLED`):** a sandboxed helper loop on the brain: no personal context, fixed-schema read-only sources written in code, the question screened in code before it leaves the house, the answer framed untrusted. It replaces a direct MCP client on purpose (third-party tool schemas would render ahead of the system prompt). Detail: `TECHNICAL_REFERENCE.md` → Research Helper (look_it_up).
- **Coder box + gate (`jess-coder`; default-off `CODER_ENABLED`):** sandboxed Qwen Code on Jupiter hands back a **patch** for review; it never commits/pushes/deploys. `coder-net` needs Docker ≥ 28; `CODER_CONTEXT_WINDOW` never above the brain's `-c`; never add websocket routes (the coder-net guard is a `BaseHTTPMiddleware`). Detail: `docs/CODER.md`; gate: `TECHNICAL_REFERENCE.md` → Coder Gate.

### Deploy, runtime and storage

- **CI + auto-deploy:** deploy = merge to `main`: the Jupiter runner fast-forwards the live checkout to the commit whose CI passed and rebuilds only what changed. Never `git pull` or edit the live checkout by hand; hold deploys by stopping the runner service; every new CI job goes in the deploy job's `needs:`. Test node ids over 300 chars fail collection (`pytest.param(..., id=...)`). Detail: `docs/DEV.md` → CI and auto-deploy.
- **Orchestrator runs non-root** (`jess`, uid/gid `ORCHESTRATOR_UID`/`ORCHESTRATOR_GID`, default 1000, HOME `/home/jess`). A host that ran the root container: `bash scripts/orchestrator_state_perms.sh --apply` right before the rebuild and again after. `docker exec brain-orchestrator …` runs as `jess` (pip → user site). Detail: `docs/UPGRADE.md` → Non-root orchestrator.
- **Embedding pin:** `transformers>=5.0,<5.12` in `orchestrator/requirements.txt` and the model + remote-code revisions in `orchestrator/embedding_pin.py` move together; unpinned, a rebuild broke every embedding (memory search, RAG, palace writes, auto_learn) with no alert. Detail + upgrade procedure: `docs/MEMPALACE.md` → Embedding model.
- **Memory (MemPalace + auto_learn):** fact docs (`shared.MEMORY_FACT_SOURCES`) have their own cos floors (`MEMORY_FACT_MIN_COS`, `MEMORY_SEARCH_MIN_COS`, never above `MIN_COS`); auto_learn runs one job per conversation, not per turn (single llama slot). Chroma isn't safe for two writers: run one-off Chroma writes with the orchestrator stopped. Detail: `docs/MEMPALACE.md`.
- **Training corpus drain** (`jobs_training_corpus.py`, nightly): registered only with `JESS_ADVANCED`, because it keeps user conversations and Claude Code prompts forever (privacy hazard for fresh installs). Detail: `docs/TRAINING_CORPUS.md`.
- **Setup-wizard backend (`/api/setup/*`, `routes_setup.py` + `setup_env.py`):** bearer-gated endpoints for the CLI wizard `scripts/setup.sh` (the web `/setup` UI is gone). The container is CPU-only, so `hardware_scan.json` is produced on the host. The env write + validate routes are first-boot-only (410 after `/complete`) and allow-listed; secrets are never echoed. Detail: `TECHNICAL_REFERENCE.md` → Setup Wizard.
- **SearXNG / web search:** `SEARXNG_SECRET` is required in `.env`; never set `SEARXNG_DEBUG`. Only the orchestrator shares `searxng-net`, so it is recreated BEFORE searxng: never `docker compose up -d searxng` alone on a fresh pull. Never probe `/search`. `web_search` failures return as text, so `bgw_web_search_total` is the only failure signal. Detail: `docs/ENV_VARS.md` → Web search (SearXNG); `monitoring/README.md` → Prometheus Targets → Web search.
- **Nightly backups:** five host cron jobs on Jupiter (state, HA, Radicale, Actual, Grist) copy off-box to Saturn, each with a paging staleness alert plus the quiet `JessBackupOffboxFailing`; a weekly cron keeps the Google token alive. `media-mirror.timer` copies `/mnt/media`'s irreplaceable folders to the `/mnt/mirror` NVMe; a real copy runs only via `systemctl start media-mirror`. `data/app/auto_learn.key` decrypts every learned fact: losing it bricks memory. Crontabs and units are owner-installed host steps. Detail: `docs/BACKUP.md` → Nightly jobs on Jupiter, Local mirror of /mnt/media.

### Monitoring and self-audit

- **Monitoring stack off the LAN:** ufw is inactive on Jupiter, so the publish address is the only control: Grafana, Prometheus, Loki, Alertmanager and Blackbox bind `127.0.0.1` only (Grafana via Tailscale Serve, login required). Monitoring compose commands need `--env-file` (`GF_SECURITY_ADMIN_PASSWORD` is required) and the main stack up first. Detail: `monitoring/README.md` → Quick Start.
- **Monitoring configs + routing:** edit templates, never renders: `scripts/generate-configs.sh` then `scripts/reload-monitoring.sh` (CI runs both on merge; `.env` is bash-sourced, so quote values with spaces). Bind-mount inode trap: Prometheus/Alertmanager/blackbox use directory mounts and renders are truncated in place — never `rm`/`mv` one. Routing is an alertname allow-list: an unnamed rule notifies nobody (`test_alert_routing.py` fails CI unless it is in `GRAFANA_ONLY`). Detail: `monitoring/README.md` → Rendered Configs, Alertmanager.
- **Off-box dead-man's switch:** `Watchdog` heartbeats from Jupiter's Alertmanager to `jupiter_deadman.py` on Saturn, which pages via its own Pushover when they stop. NOT installed yet (owner step; CI never deploys to Saturn); gated on `DEADMAN_WEBHOOK_TOKEN` (≥ 24 chars). Never silence `Watchdog` (that pages); before planned Jupiter downtime, stop the switch on Saturn. Detail: `monitoring/README.md` → Dead-man's switch; install: `monitoring/deadman/README.md`.
- **Deploy drift:** `jobs_deploy_drift.py` alerts (`DeployDrift`) when CI-green commits on main were never deployed. Default-off: needs `DEPLOY_DRIFT_REPO` in Jupiter `.env` before the deploy that recreates the orchestrator. Detail: `monitoring/README.md` → Deploy Drift.
- **Scheduler missed jobs:** `misfire_grace_time=300` (`shared.py`) means a job due during a >300 s event-loop stall is dropped, and a one-shot job (reminder, focus break) is lost for good. `_on_job_missed` must stay registered before `scheduler.start()`; it counts `bgw_scheduler_jobs_missed_total` → paging `SchedulerJobsMissed`. Detail: `monitoring/README.md` → Prometheus Targets → Scheduler missed jobs.
- **Helios observability:** Helios promtail is its own compose project (`/opt/promtail-helios`, journald only, no Docker socket, needs a persistent journal) pushing to `loki-push-gw` on Jupiter, which needs `LOKI_PUSH_TOKEN` + `NODE_HELIOS_IP` in Jupiter `.env`. CI deploys nothing to Helios. Pin both promtails to the same image digest, never a floating tag. `NodeDown` excludes Helios: outage paging needs `HELIOS_PLUG_MONITOR_ENABLED` (or wake). Detail: `monitoring/README.md` → Log Pipeline, Helios Health Alerts.
- **Closet temperature:** `jobs_monitoring.check_closet_temperature` speaks warning/critical from `user_profile.yaml` `sensors.temperature.*` (env `CLOSET_TEMP_WARNING`/`_CRITICAL` override). `ClosetTemperatureCritical` hard-codes 95°F: keep it in sync with the live profile's critical. Detail: `docs/internal/HELIOS_INFRASTRUCTURE.md` → Temperature Monitoring.
- **Self-audit (F-014) + read-only investigator:** daily Loki error digest, diagnosed by the brain or, with opt-in `SELF_AUDIT_CLAUDE_API_KEY`, by Claude. Gates `SELF_AUDIT_ENABLED` + `JESS_ADVANCED` (re-checked in `run_self_audit()`); investigator `SELF_AUDIT_INVESTIGATE`. Nothing executes model output; the local investigator never wakes Helios; log text is untrusted data. Detail: `jess-features/F-014-self-audit.md`.

### Dashboard, voice and channels

- **Settings page (`/settings`):** dashboard panels over `routes_config.py` (`/api/config/*`). Every write goes through `config_writer.atomic_write_yaml()` + `log_config_change()`; the base `user_profile.yaml` is mounted `:ro`, profile edits go to `user_profile_overrides.yaml`. Detail: `docs/FRONTEND.md` → Settings.
- **Dashboard pages + chat avatar:** every `(private)/` page (and `/avatar`) must be in `PROTECTED_PATHS` (`frontend/src/lib/protected-paths.ts`) and `config.matcher` (`frontend/src/middleware.ts`); a CI test enforces it. `frontend/public/avatar/jess.vrm` is gitignored and operator-supplied: it must exist before the frontend image build. Detail: `docs/FRONTEND.md` → Auth, Chat Avatar, Chat Photos.
- **Dashboard chat voice:** `/chat` speaks replies per sentence (`POST /api/tts/synthesize`) and has hands-free call mode (`POST /api/stt/transcribe`). `VOICE_THINKING_ENABLED` must stay `true` with the live brain (it acts unasked with thinking off); a change needs `docker compose up -d --force-recreate orchestrator`. Detail: `docs/VOICE_AND_TTS.md` → Dashboard chat voice replies, Hands-free call mode.
- **Dashboard inbox + Web Push (default-off `WEBPUSH_ENABLED`, needs `WEBPUSH_SUBJECT`):** reminders, selfcare nudges and briefing cards become `inbox_items` (`inbox.py`) in `/chat`, pushed by `webpush_manager.py`; Done/Snooze go through `reminder_actions.py`. The VAPID key at `WEBPUSH_KEY_PATH` is a backup-critical file. Detail: `docs/FRONTEND.md` → Dashboard Inbox + Web Push.
- **Closet monitors kiosk (Jupiter, hand-installed):** `jupiter-kiosk.service` (kiosk.py + Chrome) pulls in `jupiter-kiosk-x.service` (Xorg in its own network namespace). Enable/stop/restart `jupiter-kiosk` only; never point the kiosk at the voice card; a new dashboard card's GET must be added to `frontend/src/lib/kiosk-session.ts`. Detail: `scripts/jupiter-kiosk/README.md`.
- **Voice satellite (ATOM Echo, default-off `VOICE_SATELLITE_ENABLED`):** `voice_satellite.py` talks to each ESPHome device directly, not through HA. Rules: keep the devices out of HA's ESPHome integration; end a run with `RUN_END` alone (`STT_VAD_END` first wedges ESPHome); mic and speaker share one I2S bus, so the firmware stops the wake word before any sound and bounds every wait; `VOICE_SATELLITE_DEVICES` has no spaces and names rooms, never a person; `aioesphomeapi`'s exact pin moves together with `cryptography`. Anyone in the room has full access (D8); voice match (`VOICE_MATCH_DEVICES`, D14) is a TV filter, not security. Detail: `docs/VOICE_AND_TTS.md` → Voice satellite.
- **Reminder push channels (ntfy F-011 `NTFY_ENABLED`, Pushover F-013 `PUSHOVER_ENABLED`, both default off):** Done/Snooze buttons call the HMAC-signed, bearer-exempt `/api/reminder/{ack,snooze}/{id}` routes. Change ack/snooze semantics only in `reminder_actions.py` (shared by every channel). Each channel auto-disables on a missing secret; lock-screen titles stay generic. Both on = a double push. Pushover HTML-escapes the reminder text, so an injected `<a href>` can't ride the trusted notification. Detail: `jess-features/F-011-ntfy-feedback-loop.md`, `F-013-pushover-bridge.md`.
- **Telegram bot (optional, default-off `TELEGRAM_ENABLED`):** `telegram_bot.py` runs as a forever asyncio task, never a scheduler job (its 50 s long-poll would pin a worker). The allow-listed chat ID is the auth boundary for its Done/Snooze buttons; replies stay plain text (no `parse_mode`). Detail: `TECHNICAL_REFERENCE.md` → Telegram Bot; env `docs/ENV_VARS.md` → Telegram Bot.

### Features

- **Calendar (Radicale + `calendar_sources.py`):** every calendar read/write goes through `calendar_sources.py`, which merges Radicale CalDAV (own `radicale/` compose project, loopback-only; its logs stay out of Loki), the work Exchange push and Google (while `CALENDAR_GOOGLE_ENABLED`). Jess writes CalDAV (Google only if CalDAV is off), never Exchange. The Exchange push must carry `"source": "exchange"`; without it it's the ignored legacy push. A failed source is reported, never read as a clear day. Detail + runbook: `docs/CALENDAR.md`.
- **Evening shutdown ritual:** `jobs_calendar.evening_briefing()` (`EVENING_BRIEFING_*`) parks one unfinished thing in `app_state`; `morning_briefing` clears it only after a successful, non-suppressed announce. Daily jobs stamp heartbeats via `job_heartbeat.py`, so `*Stale` alerts survive restarts. Detail: `jess-features/F-006-routine-scaffolding.md` → Evening shutdown ritual.
- **Sleep wind-down (`jobs_winddown.py`):** non-`scene.*` entries in `WIND_DOWN_SCENE` are dropped (a `.env` typo must not nightly-turn_on an arbitrary domain); both rungs skip under DND. `sleep_mode("on")` stamps `sleep_started_at` only for goodnight-intent mutes (indefinite, 20:00–05:00), so "mute, guests over" never softens the next morning. Detail: `jess-features/F-006-routine-scaffolding.md` → Sleep wind-down ladder.
- **Routines + selfcare bridge (F-006):** `selfcare_log` advances a matching routine step, and a step marked `done` logs the matching selfcare action. That reverse bridge fires only on `done`, never on `skip` or an auto-end `stop` (skipped meds must not count as taken), and neither direction may block the primary write. A step's `ha_action` is skipped under DND; a non-skippable step (or any step with `ROUTINE_AUTO_SKIP` off) nudged past `ROUTINE_NUDGE_MAX` force-ends the routine. Detail: `jess-features/F-006-routine-scaffolding.md`.
- **Movement from the phone (F-008, default-off `MOVEMENT_STEPS_ENTITY` / `MOVEMENT_STAND_ENTITY`):** Apple Health steps / stand hours read via HA reset the sitting clock; fails open toward nudging. Logs never carry values or entity ids (they hold the phone's name), and real entity ids never get committed. Detail: `jess-features/F-008-selfcare-nudges.md` → Movement seen by the phone.
- **Workouts (default-off `WORKOUTS_ENABLED`):** weekly program + workout locations, per-exercise progression with deload weeks, and an exercise how-to library the operator fetches onto the data volume (not in the repo, not backed up). Detail: `docs/WORKOUTS_AND_MEALS.md`.
- **Pantry + meal planner (F-015, default-off `PANTRY_ENABLED`):** the diet `avoid` list is a hard rule; medication interaction notes are read server-side and only ever produce "check with your prescriber" flags. Cooking reminders use `mp-` ids, which `reminder_manager.selfcare_action_for_reminder` excludes from the selfcare bridge (a Done tap never logs a meal/med). Detail: `docs/PANTRY.md`.
- **Paperless bridge (F-012, default-off `PAPERLESS_ENABLED`):** `paperless_save` + `POST /api/paperless/upload` hand files to Paperless-ngx; the read tools use a least-privilege view-only Paperless user. `paperless_save` takes a basename only, resolved inside the inbox. `document_vault` stays the home for typed text notes. Detail: `jess-features/F-012-paperless-bridge.md`.
- **Finance: Actual Budget sync (read-only).** The Quest Board mirrors the self-hosted Actual Budget server (`actual/` compose project) via `actualpy`. actualpy runs server-supplied migration SQL, so never bypass `actual_client._confine_actualpy()`; on every actualpy bump re-read `Actual.run_migrations` and run `tests/test_actual_sqlite_confinement.py`. Bump the actual-server image and `actualpy` together. Accept a refused mass-delete sync via `POST /api/finance/sync/accept-deletions`, never `/sync/reset` (it also drops months before the window and every override). Detail: `docs/BACKUP.md` → Actual Budget, `TECHNICAL_REFERENCE.md` → Finance.
