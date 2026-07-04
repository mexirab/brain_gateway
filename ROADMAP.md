# Brain Gateway Roadmap

Personal ADHD brain — voice-first, proactive, low-friction.

The guiding principle: **if it requires opening an app, I won't do it.** Everything should be capturable by voice ("Hey Jess, ...") or happen automatically in the background.

## What Works Today (v1)

| Feature | How it works | Status |
|---------|-------------|--------|
| Voice control | "Hey Jess" wake word → ATOM Echo S3R → HA voice pipeline | Working (office) |
| Home automation | "Turn off the lights" → fast-path or unified-model tool call | Working |
| Focus timer | "Start focus for 30 minutes" → Pomodoro + Endel audio + Pi-hole blocking | Working |
| Reminders | "Remind me to call the dentist at 3pm" → TTS announcement on speakers | Working |
| Personal memory (RAG) | ChromaDB with 154 docs: profile, patterns, meds, projects | Working |
| Web search | "What's the weather?" → SearXNG | Working |
| Unified LLM (v7) | Single Lorbus/Qwen3.6-27B-int4-AutoRound served by vLLM 0.19.1 on Helios GPU0 (RTX 5090) handles conversation + tools in one agentic loop; expert reasoning delegated to Qwen3-32B on Saturn 3090 via `ask_expert`; coding delegated to Qwen3-Coder-Next 80B/3B MoE on Helios GPU1 (RTX PRO 5000) via `code_agent` | Working |
| Google Calendar | "What's on my calendar?" → check/create events, proactive alerts | Working |
| Phone calendar sync | iPhone Shortcut → all calendars (Outlook+Google+iCloud) merged on dashboard | Working |
| Gmail monitoring | check_email/search_email tools + proactive polling (Primary inbox) | Working |
| Morning briefing | 7:00 AM daily on bedroom pair → today's events + pending reminders via TTS | Working |
| Email polling | Every 30 min → announce new unread Primary emails via TTS | Working |
| Calendar polling | Every 5 min → announce events starting within 2 hours | Working |
| Travel-time alerts | Google Maps API → "leave in X minutes" for events with physical locations | Working |
| Temperature monitoring | Server closet temp dashboard widget + TTS alerts at 80°F/85°F + Grafana metrics | Working |
| Interactive system diagram | Animated SVG on architecture page showing all data flows | Working |
| Mode router | Intent-based coaching: explainer/mirror/counterbalance/challenge/baseline | Working |
| HTTPS access | Tailscale Serve → valid TLS cert → mobile mic support | Working |
| TTS pacing | Sentence pause injection + paragraph splitting for calmer speech | Working |
| Brain dump (F-001) | Voice-first capture & routing → categorize, dedup, store in RAG or set reminders | Working |
| Time-aware nudges (F-002) | Tiered calendar countdown alerts with escalating urgency | Working |
| Task decomposition (F-003) | Break tasks into ADHD-friendly micro-steps with time buffers | Working |
| Body doubling (F-004) | Focus sessions with sprints, check-ins, ambient audio, Pi-hole blocking | Working |
| Progress tracking (F-005) | Dopamine-aware streaks, XP, celebrations for completed tasks | Working |
| Routine scaffolding (F-006) | Morning/evening routine scaffolding with step-by-step prompts | Working |
| Interruption recovery (F-007) | Context bookmarks & recovery prompts after interruptions | Working |
| Self-care nudges (F-008) | Meal, medication, hydration & movement reminders | Working |
| Decision simplifier (F-009) | Choice paralysis helper — narrow options, structured comparison | Working |
| Ambient awareness (F-010) | Periodic environmental/schedule summaries in the background | Working |
| Ntfy feedback loop (F-011) | Reminder delivery via ntfy with Done/Snooze buttons + HMAC-signed callback routes | Working |
| Paperless bridge (F-012) | `paperless_save` tool + `POST /api/paperless/upload` → Paperless-ngx on Jupiter for OCR + auto-tagging | Working |
| Pushover bridge (F-013) | Parallel iOS push channel alongside F-011 ntfy; reuses HMAC ack/snooze routes; independent `PUSHOVER_ENABLED` flag | Working |
| Self-audit (F-014) | Daily 7am UTC Loki scan + Jess diagnosis + Pushover digest + markdown report under `/app/data/self_audits/`; read-only by design; default-OFF (`SELF_AUDIT_ENABLED`) | Working |
| Announcement observability | Announcement history, metrics, per-speaker tracking | Working |

## ADHD Feature Suite — DONE

All 14 ADHD-informed features (F-001 through F-014) are complete and deployed. Each feature was built from a self-contained implementation spec with interaction examples, tool schemas, TTS templates, and testing checklists. See [jess-features/README.md](jess-features/README.md) for the full build order, dependency graph, and per-feature specs.

| ID | Feature | Tools |
|----|---------|-------|
| F-001 | Brain Dump — voice-first capture, categorize, dedup, route to RAG or reminders | `brain_dump` |
| F-002 | Time-Aware Nudges — tiered calendar countdown alerts with escalating urgency | (background job) |
| F-003 | Task Decomposition — break tasks into ADHD-friendly micro-steps with time buffer | `decompose_task`, `task_step` |
| F-004 | Body Doubling & Focus Sessions — sprints, check-ins, ambient audio, Pi-hole blocking | `start_focus`, `stop_focus`, `focus_status`, `focus_sprint` |
| F-005 | Dopamine-Aware Progress Tracking — streaks, XP, celebrations | (integrated) |
| F-006 | Routine Scaffolding — morning/evening routines with step-by-step prompts | (integrated) |
| F-007 | Interruption Recovery — context bookmarks & recovery after interruptions | (integrated) |
| F-008 | Self-Care Nudges — meal, medication, hydration & movement reminders | (background job) |
| F-009 | Decision Simplifier — choice paralysis helper, structured comparison | (integrated) |
| F-010 | Ambient Awareness — periodic environmental/schedule summaries | (background job) |
| F-011 | Ntfy Feedback Loop — reminder delivery via ntfy with Done/Snooze action buttons + HMAC callback routes | (integrated in `reminder_manager`) |
| F-012 | Paperless-ngx Bridge — push files from `/app/data/paperless_inbox/` to Paperless for OCR + auto-tagging; `document_vault` unchanged | `paperless_save` |
| F-013 | Pushover Push Bridge — parallel iOS push channel alongside F-011 ntfy; reuses HMAC ack/snooze routes; HTML-escaped reminder text | (integrated in `pushover_manager`) |
| F-014 | Daily Self-Audit — 7am UTC Loki scan + Jess diagnosis + Pushover digest + markdown report; read-only safety story (allow-list + dangerous-pattern + secret-pattern filters); default-OFF | (background job in `jobs_self_audit.py`) |

## vLLM Migration (Phase 2 → Phase 3) — DONE

**Phase 2 trial complete 2026-04-26.** `vllm/vllm-openai:0.19.1` running `Lorbus/Qwen3.6-27B-int4-AutoRound` (AutoRound INT4 + MTP n=3 + flashinfer + fp8_e4m3 KV) on Helios GPU0 (RTX 5090, 32 GB) hit:

| Workload | vLLM tps | × llama.cpp baseline (~50 tps) |
|----------|---------:|-------------------------------:|
| short_gen narrative (200 tok) | 130.8 | 2.6× |
| mid_mix narrative (500 tok) | 121.1 | 2.4× |
| long_prompt warm (64 tok @ 6.5K ctx) | 78.0 | 1.6× |
| code structured (1000 tok) | 151.0 | 3.0× |

Max practical context: **153,600 tokens** at `--gpu-memory-utilization 0.93`. Full 256K requires vLLM 0.19.2+ (unmerged KV-calc fix). All decision-criteria gates passed: TG ≥ 1.5× ✅, tool-call parsing ✅, context expansion to ≥128K ✅.

**Phase 3 cutover landed 2026-04-26 — Plan A, not Plan B.** A pre-cutover bench of Lorbus 27B on GPU1 (RTX PRO 5000) hit only 28–79% of the Phase 2 throughput recorded on GPU0 (the 5090 has higher memory bandwidth and more SMs); since the Phase 2 numbers were RTX 5090 numbers, moving to GPU1 would have given up the throughput win. Plan A keeps vLLM on the 5090 and instead repins the coder GPU0 → GPU1. Voice services (qwen-tts, parakeet-stt) stayed on GPU1 unchanged.

**Final layout:**
- **GPU0 RTX 5090 (32 GB):** `vllm-primary.service` (port 8080, Lorbus 27B INT4)
- **GPU1 RTX PRO 5000 (48 GB):** `llama-server-coder.service` (port 8082) + `qwen-tts.service` (port 8002) + `parakeet-stt.service` (port 8003)

`llama-server.service` disabled (unit retained on disk). `MODEL_NAME` and `FALLBACK_MODEL_NAME` updated to `qwen3.6-27b-int4`. Idempotent rollback at `/home/labadmin/vllm-trial/rollback_phase3.sh`. Forward-looking: when vLLM 0.19.2 + 256K context becomes worth pursuing, the primary must migrate to GPU1 (the 5090 can't hold Lorbus + 256K KV in 32 GB).

See [docs/internal/VLLM_PHASE_3_PLAN.md](docs/internal/VLLM_PHASE_3_PLAN.md) → Outcome for the full landed config.

## In Progress: Productization (de-personalization)

Per `/home/labadmin/.claude/plans/ship-jess-as-product.md` Phase 2. Replacing hardcoded user identity with `shared.profile.user_name` so the codebase can ship as a multi-user product.

- ✅ Step 1 — Hardcoded user identity: `presence_tracker.py` 5× "Nadim is home/away" strings parameterized via `shared.profile.user_name` (`tests/test_presence_tracker.py`, 7 tests); `mempalace.py`, `tool_handlers.py`, `user_profile.py` docstring examples updated
- ✅ Step 2 — Hardcoded-IP sweep: `config.py` defaults flipped to empty strings + `*.example.tld` comments (`expert_model_url`, `paperless_url`, `ntfy_url`, `ntfy_callback_base_url`, `self_audit_loki_url`); new `self_audit_prom_url` setting consumed by `jobs_self_audit._query_prometheus_summary` (skip-when-empty); `training_corpus_cc_dir` default corrected to `-opt-gateway-mvp`; `.env.example` and `user_profile.example.yaml` de-personalized; `docker-compose.yml`, `ha_automations/*`, `monitoring/promtail/promtail-helios.yml`, `tts/wyoming_*` env-driven. Live behavior preserved via `.env` additions.
- ✅ Step 3 — `JESS_ADVANCED` flag (default false) gates 5 owner-only tools out of the shippable build: `code_agent`, `ask_expert`, `query_budget`, `finance_status`, `check_claude_activity`. New setting in `config.py`, exposed via `shared.JESS_ADVANCED`; `tool_definitions.ADVANCED_ONLY_TOOL_NAMES` frozenset filters `get_all_tools()`. Tests in `orchestrator/tests/test_tool_gating.py`.
- ✅ Step 4 — `JESS_ADVANCED` extended to background jobs: self-audit (`jobs_self_audit`) and training-corpus drain (`jobs_training_corpus`) registration in `orchestrator.py` startup is gated. `run_self_audit()` also checks both `SELF_AUDIT_ENABLED` and `JESS_ADVANCED` at the function level, closing the `POST /api/self_audit/run` manual-trigger bypass (same code path now applies to cron + route). Tests in `orchestrator/tests/test_self_audit_gate.py` (3 tests).
- ✅ Step 5 — Compose profile gating: `nebula-sync`, `promtail` (Helios sidecar), and `nut-exporter` moved behind `profiles: ["advanced"]` in `docker-compose.yml`. Default install brings up only the 7 core services (open-webui, orchestrator, redis, searxng, wyoming-whisper, wyoming-jessica-tts, frontend); set `COMPOSE_PROFILES=advanced` in `.env` to enable the owner-specific log-shipping, multi-Pi-hole sync, and NUT/UPS observability services. `LOKI_PUSH_URL` and `NODE_*_IP` defaults relaxed to `${VAR:-}` (soft fallback) because compose expands env vars file-wide before profile filtering — the previous `:?error` form would have blocked fresh installs.
- ✅ Step 6 — Dead-code cleanup: removed the 7-day calibration review block from `jobs_self_audit.py` (scheduled for 2026-05-02, already past). `run_self_audit_review()` and 6 review-only helpers (~350 lines) deleted; `prometheus.yml` `nut` scrape job documented as advanced-profile-dependent.
- ✅ Step 7 — Voice rebrand: personal voice-clone branding scrubbed from shippable code. `tts_voice` default flipped from `jessica` to `default` (`config.py`); compose `--voice ${TTS_VOICE:-default}`; descriptions/examples/docstrings in `tts/server.py` and `tts/wyoming_jessica_bridge.py` genericized; `user_profile.example.yaml` example voice set to `default`. Wire IDs `jessica-tts` (server name) and `jessica` (Wyoming bridge voice ID) retained for HA back-compat with explanatory comments. Live deployment behavior preserved — Nadim's `.env` keeps `TTS_VOICE=jessica`. Deferred to Phase 4 docs sweep: file/container rename (`wyoming_jessica_bridge.py`, `wyoming-jessica-tts` service), `JessicaTtsHandler` class rename, `tts/README.md` scrub (16 instances, owner-specific dev README).

**Status (2026-05-09):** Phase 2 ~95% complete (Steps 1-7 shipped on `main` via commits `6bcd375` + `344ef7e`). Phase 1 (containerize vLLM/TTS/STT into compose) + Phase 3 (browser setup wizard) **in progress** — Phase 1 compose-stanza spike + Phase 3 backend (`/api/setup/*`) and frontend `/setup` slice 1 (Welcome/Identity/Review) landed; remaining wizard steps + Phase 4 (distribution + docs) **not started**. Resume from progress log at `/home/labadmin/.claude/plans/ship-jess-as-product.md` (top section). Recommended next slice: either the Phase 2 tail (git-rm `rag/nadim_rag/`, `user_profile.nadim.yaml`, `data/palace.yaml` from public view + `tts/README.md` scrub) OR a Phase 1 containerization spike on a separate test target (write compose stanzas + Dockerfiles, no deploy).
- ⬜ Remaining production code paths with hardcoded "Nadim" string literals (audit pending)

## Known Issues / TODOs

| Issue | Priority | Notes |
|-------|----------|-------|
| TTS output on ATOM Echo tiny speaker | High | Should route to Google speakers group ("all speakers"). Requires HA UI. |
| ATOM Echo S3R has no LED feedback | Low | S3R variant has no programmable RGB LED (GPIO35 conflicts with PSRAM). Hardware limitation. |
| Env/time-coupled tests can flake CI | Low | A few unit tests still read ambient state instead of pinning it: `test_selfcare_manager::TestMealCheck` (wall-clock-dependent), `test_config` defaults (host env vars), `test_ntfy_feedback` `disabled_returns_404` (assumes `PUSHOVER_ENABLED` off). Green on a clean CI runner today, but worth pinning clock/env to remove the flake risk. |

## CI & Test Suite Health — DONE (2026-06-16)

The GitHub Actions **Python Lint & Format** check had been red on every PR regardless of content (the `ruff-action@v3` scans the whole repo, and two `scripts/` files were missing from pyproject's `extend-exclude`). Cleared the pre-existing `ruff` debt across `orchestrator/` + `scripts/` (auto-fixes + behavior-preserving SIM/B-rule rewrites; no production behavior change).

Fixing it also surfaced a long-masked **Python Tests** job: it installed only `pytest pytest-asyncio`, so the four `respx`-importing modules aborted collection and the whole suite was silently skipped on `main`. Added `respx` to the job, then fixed the ~37 pre-existing failures it revealed (none caused by the lint work):

- **Event-loop (~21):** pytest-asyncio 1.x leaves no current loop after async tests → sync `get_event_loop()` callers raised (order-dependent flake). Added an autouse `conftest` fixture guaranteeing a loop per test.
- **Test isolation:** `announcement_routes._cache` global leaking across tests (autouse reset); `sys.modules` stub that missed the real package attribute; unisolated `/app` config write (now tmp-scoped).
- **Stale / env-coupled tests:** a `budget_analyze` test asserting a deliberately-removed gate; `jobs_self_audit` fixture missing the `jess_advanced` flag; a timezone-naive snooze-reschedule assertion (5h offset on a UTC runner).

All five CI checks (Lint, Tests, Frontend, Docker Build) green; shipped via [PR #23](https://github.com/mexirab/brain_gateway/pull/23).

## In Progress: Frontend Dashboard (ConvivialProphet.com)

**Goal:** Custom dashboard + chat at ConvivialProphet.com. Hybrid: public showcase + private daily-driver.

**Tech:** Next.js 14, Tailwind dark theme, Docker on Jupiter (port 3001), Cloudflare Tunnel for public access.

| Phase | What | Status |
|-------|------|--------|
| 1 | Project scaffold, Docker, auth middleware, login, placeholder pages | ✅ Done |
| 2 | Public pages: architecture page with interactive system diagram, cluster nodes, data flow, capabilities | ✅ Done |
| 3 | Private dashboard: calendar, reminders, focus timer, system health, temperature monitoring, finance snapshot | ✅ Done |
| 4 | Chat interface: streaming SSE with Jess, routing badges | ✅ Done |
| 5 | Home controls: HA entity cards, toggles, brightness, scenes | ✅ Done |
| F1-F6 | Gamified finance dashboard: YNAB sync, budget health, XP/levels, quest board | ✅ Done |
| F7 | Workouts + Meals: adaptive gym generator, set logging, calorie tracking, meal photo estimation | ✅ Done (2026-04-15) |
| F8 | In-app settings page: Identity & Tone, Selfcare Nudges, Quiet Hours, Recurring Reminders (`/settings` + `/api/config/*` + audit log) | ✅ Done (2026-04-29) |
| 6 | DNS + Cloudflare Tunnel: ConvivialProphet.com → Jupiter | Not started |
| 7 | Polish: animations, PWA, mobile optimization, toasts | Not started |

**Remaining orchestrator changes needed:**
- CORS origins updated when Cloudflare domain is live

## Phase 2: Calendar & Email Awareness

**Goal:** Jess knows what's on my schedule without me telling her.

### Google Calendar sync — DONE
- ✅ OAuth2 setup (Google Cloud project, Desktop credentials, consent flow)
- ✅ `check_calendar` tool — "Hey Jess, what's on my calendar this week?"
- ✅ `create_calendar_event` tool — "Add pickleball Thursday at 7pm"
- ✅ Proactive polling: every 5 min, TTS announcement for events within 2 hours
- ✅ Morning briefing: 7:00 AM on bedroom pair, today's events + pending reminders
- ✅ Deployed and configured on Jupiter

### Calendar unification — DONE
- ✅ iPhone Shortcuts bridge: sends all calendars (Outlook + Google + iCloud) to `/api/calendar/sync`
- ✅ Dashboard merges phone-synced events with Google Calendar API, deduplicates by title+start
- ✅ Phone events persisted to disk (survives restarts)
- ✅ iOS date format parsing with Unicode space normalization

### Gmail monitoring — DONE
- ✅ `check_email` tool — check inbox for recent/unread emails
- ✅ `search_email` tool — Gmail query syntax (`from:`, `subject:`, `newer_than:`, etc.)
- ✅ Proactive polling: every 30 min, announces new Primary inbox emails via TTS
- ✅ Filters out promotions, social, forums, and updates categories
- ✅ OAuth2 scopes: `calendar.readonly`, `calendar.events`, `gmail.readonly`

### Travel-time calendar alerts — DONE
- ✅ Google Maps Directions API integration for real-time traffic
- ✅ "Leave in X minutes" alerts instead of "Event in X minutes" for physical locations
- ✅ Virtual meeting detection (skips Zoom/Teams/Meet/WebEx links)
- ✅ Configurable buffer time (`TRAVEL_TIME_BUFFER`, default 10 min)
- ✅ Caches API results per destination+date to avoid redundant calls

### Temperature monitoring — DONE
- ✅ Dashboard widget: closet temp, kitchen ambient, heat delta, estimated cooling cost
- ✅ TTS alerts: 80°F warning, 85°F critical, auto-clears below 78°F
- ✅ Prometheus gauges: `bgw_temperature_fahrenheit`, `bgw_temperature_delta_fahrenheit`
- ✅ Background job polls HA sensors every 10 min

## Phase 3: Document Memory — NOT STARTED

**Goal:** Upload anything important → ask about it later by voice.

### Document ingestion pipeline
- Upload via Open WebUI file attachment, or a simple web endpoint
- Supported formats: PDF, images (OCR), Word docs, plain text
- Parse → chunk → embed → store in ChromaDB
- Metadata: upload date, document type, source

### Use cases
- Upload a lease → "Hey Jess, when does my lease expire?"
- Upload insurance card → "Hey Jess, what's my insurance policy number?"
- Photo of a receipt → stored for expense tracking
- Photo of pantry → "Hey Jess, what can I make for dinner with what I have?"

### Implementation notes
- PDF parsing: PyMuPDF or pdfplumber
- OCR: Tesseract or vision model
- Extend existing `ingest_rag.py` with format handlers
- New tool: `ingest_document` (or auto-ingest on upload)

## Phase 4: Proactive Agent — NOT STARTED

**Goal:** Jess doesn't just respond — she anticipates.

### Background agent loop
- Runs continuously, checks various sources on schedules
- Gmail → new important emails → summarize and notify
- Calendar → upcoming events → pre-reminders (✅ done)
- Bills/deadlines from ingested documents → warning notifications
- ~~Medication schedule → daily reminders at set times~~ ✅ Done (F-008 self-care nudges)

### Proactive notifications
- Push to Google speakers via HA media_player TTS
- Priority levels: urgent (immediate), normal (next quiet moment), low (morning briefing)
- Quiet hours: no announcements during sleep, configurable

### Task capture and follow-up
- ~~"Hey Jess, I need to call the insurance company" → stored as task~~ ✅ Done (F-001 brain dump)
- ~~Jess follows up: "You mentioned calling the insurance company yesterday. Want me to remind you at a good time today?"~~ ✅ Done (F-001 brain dump + reminders)
- ~~Gentle nagging with escalation for overdue items (ADHD-friendly, not guilt-inducing)~~ ✅ Done (F-002 time nudges + F-005 progress tracking)
- ClickUp integration for task visibility on phone

### Context awareness
- ~~Track focus sessions → "What was I working on before lunch?"~~ ✅ Done (F-007 interruption recovery)
- ~~Time-of-day awareness → suggest dinner ideas in the evening~~ ✅ Done (F-006 routine scaffolding + F-010 ambient awareness)
- Location awareness (future) → remind about errands when leaving

### OpenClaw consideration
- Researched extensively — NOT recommended as orchestrator replacement
- Better as multi-channel frontend (WhatsApp/Telegram) alongside existing system
- Security concerns (3 CVEs, exposed instances), unreliable memory, high API costs
- Current custom orchestrator is more deterministic and reliable for tool execution

## Phase 5: Vision & Multimodal — NOT STARTED

**Goal:** See the world, not just hear it.

- Pantry photos → meal planning with dietary preferences
- Document photos → OCR → RAG (contracts, receipts, business cards)
- Whiteboard photos → extract and store notes
- Screen sharing → "Hey Jess, what am I looking at?" (future)

## Hardware Roadmap

| Device | Location | Status |
|--------|----------|--------|
| ATOM Echo S3R | Office | Flashed, online, wake word working |
| ATOM Echo S3R #2 | Bedroom | Not purchased |
| ATOM Echo S3R #3 | Kitchen | Not purchased |
| Google speakers | Whole house (office, bedroom, kitchen) | Existing, TTS output target |

## Priority Order

1. ~~**Calendar integration**~~ — ✅ DONE
2. ~~**Mode-aware coaching**~~ — ✅ DONE (intent router + personalized system prompts)
3. ~~**Personal RAG knowledge**~~ — ✅ DONE (154 docs: identity, patterns, preferences)
4. ~~**HTTPS + mobile mic**~~ — ✅ DONE (Tailscale Serve)
5. ~~**TTS pacing**~~ — ✅ DONE (paragraph splitting + sentence pause injection)
6. ~~**Frontend dashboard**~~ — ✅ DONE (Architecture, Dashboard, Chat, Home, Finance pages)
7. ~~**Gmail integration**~~ — ✅ DONE (check/search tools + proactive polling)
8. ~~**Calendar unification**~~ — ✅ DONE (iPhone Shortcuts bridge for all calendars)
9. **TTS to Google speakers** — Better audio output than tiny ATOM Echo speaker (needs HA UI)
10. **Document ingestion** — builds on existing RAG, immediate utility
11. **Proactive agent** — transforms from reactive to anticipatory
12. **Vision/multimodal** — nice to have, depends on model capabilities
13. **Jess avatar** — Animated talking head synced to TTS output
