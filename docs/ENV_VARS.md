# Environment Variables

All orchestrator-side environment variables, grouped by subsystem. Defaults in parentheses. Anything not listed here either lives in `.env.example` as a bootstrap template or is part of the infrastructure (Docker, HA, Google OAuth) rather than the application.

For the authoritative template, see `.env.example` at the repo root.

## Distribution profile

| Variable | Default | Purpose |
|----------|---------|---------|
| `JESS_ADVANCED` | `false` | Master gate for owner-specific surface cut from the default shippable build. Two effects: (1) exposes 5 advanced tools to the LLM — `code_agent`, `ask_expert`, `query_budget`, `finance_status`, `check_claude_activity` — via `tool_definitions.get_all_tools()` filtering; (2) registers two background jobs in `orchestrator.py` startup — the F-014 self-audit (additionally gated by `SELF_AUDIT_ENABLED`) and the nightly training-corpus drain (privacy hazard for fresh installs — collects user conversations). Handlers / job functions stay imported, just unregistered when off. Flip to `true` in `.env` for the development stack. |
| `COMPOSE_PROFILES` | _empty_ | Native Docker Compose profile selector. Comma-separated for multiple profiles. Two valid values: `advanced` brings up the operator/owner-specific services — `nebula-sync` (Pi-hole multi-instance config sync), `promtail` (log shipper to external Loki), `nut-exporter` (UPS metrics, requires NUT on host); `models` brings up the model layer — `vllm-primary` + `qwen-tts` + `parakeet-stt` (see Model layer below). Default-empty leaves only the core stack: orchestrator + open-webui + redis + searxng + frontend + the two wyoming bridges. Independent of `JESS_ADVANCED` — that gates application-level features; this gates compose-service startup. |

## Model layer (compose profile: `models`)

The vLLM primary LLM, Qwen3-TTS, and Parakeet STT have compose stanzas (`vllm-primary`, `qwen-tts`, `parakeet-stt`) and Dockerfiles (`tts/Dockerfile`, `tts/Dockerfile.parakeet`), gated behind the `models` compose profile. **Status: authored and build-validated, not deployed.** On the live Helios box the model layer still runs as host systemd units (`vllm-primary.service`, `qwen-tts.service`, `parakeet-stt.service`); Helios's `.env` keeps `COMPOSE_PROFILES=advanced` (no `models`) so compose does not double-start the systemd-managed servers. The `models` profile exists for fresh single-box installs, which set `COMPOSE_PROFILES=models` (or `advanced,models`).

GPU pinning: vLLM → GPU0, TTS + STT → GPU1. HF model downloads persist in the `model-hf-cache` named volume. The env vars below feed those stanzas only — they are inert unless `models` is in `COMPOSE_PROFILES`. When the model layer runs in compose, also repoint the orchestrator at the compose-internal service names: `MODEL_URL=http://vllm-primary:8000/v1`, `TTS_URL=http://qwen-tts:8002`, `STT_URL=http://parakeet-stt:8003`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODEL_BIND_ADDR` | `127.0.0.1` | Host interface the `vllm-primary` / `qwen-tts` / `parakeet-stt` published ports bind to. Loopback by default — the model APIs are unauthenticated and the orchestrator reaches them over the compose network. Set `0.0.0.0` for LAN access. |
| `VLLM_MODEL` | `Lorbus/Qwen3.6-27B-int4-AutoRound` | HuggingFace repo id for the primary LLM. 24GB tier → `Qwen/Qwen3-14B-Instruct-AWQ`; 32/48GB tier → the default. |
| `VLLM_SERVED_NAME` | `qwen3.6-27b-int4` | `--served-model-name` exposed on the OpenAI-compatible API. Should match the orchestrator's `MODEL_NAME`. |
| `VLLM_QUANTIZATION` | `auto_round` | vLLM `--quantization`. `auto_round` for the default AutoRound model; `awq` for AWQ models; `auto` to detect from the model config. |
| `VLLM_MAX_MODEL_LEN` | `153600` | `--max-model-len` (context window). |
| `VLLM_GPU_MEM_UTIL` | `0.93` | `--gpu-memory-utilization` fraction. |
| `JESS_VRAM_TIER` | (empty) | VRAM tier (`24` \| `32` \| `48`). Written by `scripts/detect_hardware.sh`, which reads `nvidia-smi` and also prints a suggested `VLLM_MODEL`. |
| `QWEN_TTS_MODEL` | `Qwen/Qwen3-TTS-1.7B-Base` | HuggingFace repo id for the TTS model; downloads into `model-hf-cache` on first run. |
| `QWEN_TTS_DTYPE` | `bfloat16` | TTS model compute dtype. |
| `QWEN_TTS_FLASH_ATTN` | `false` | Enable FlashAttention in the TTS server. |
| `TTS_VOICES_PATH` | `./data/tts_voices` | Host dir for cloned-voice files (`voices.json` + reference wavs), bind-mounted into the `qwen-tts` container at `/app/voices`. |
| `PARAKEET_MODEL` | `nvidia/parakeet-tdt-0.6b-v3` | HuggingFace repo id for the Parakeet STT model. |

## Model / LLM backends

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODEL_URL` | `http://localhost:8080/v1` | Primary model endpoint (e.g. `http://llm.example.tld:8080/v1` for off-host) |
| `MODEL_NAME` | — | Primary model name (current production: `qwen3.6-27b-int4` — Lorbus/Qwen3.6-27B-int4-AutoRound served by vLLM. Was `Qwen3.5-27B` until 2026-04-26 vLLM Phase 3 cutover.) |
| `FALLBACK_MODEL_URL` | — | Fallback model endpoint (optional) |
| `FALLBACK_MODEL_NAME` | — | Fallback model name (current production: `qwen3.6-27b-int4`; matches `MODEL_NAME` post Phase 3 cutover) |
| `EMBEDDING_MODEL` | — | Embedding model for RAG indexing |
| `MODEL_SERVER_IP` | — | SSH target for remote model start/stop |
| `MODEL_SSH_USER` | — | SSH user for model server |
| `MODEL_START_CMD` | — | Command to start model server via SSH |
| `MODEL_STOP_CMD` | — | Command to stop model server via SSH |

## Expert reasoning model (`ask_expert` tool)

One-shot blocking delegation to Qwen3-32B Thinking on Saturn 3090 (host port 8084 via the `expert-model` Docker container, image `ghcr.io/ggml-org/llama.cpp:server-cuda`). Used by `query_budget` analyze-mode synthesis and any future hard-reasoning task. Auto-disabled (handler returns a short "disabled" string) when `EXPERT_ENABLED=false` or `EXPERT_MODEL_URL` is empty.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EXPERT_ENABLED` | `false` | Master flag. When false, `ask_expert` returns a disabled message and `query_budget` analyze-mode falls back to the surface-level `data` block. |
| `EXPERT_MODEL_URL` | (empty) | Expert endpoint, e.g. `http://expert.example.tld:8084/v1`. |
| `EXPERT_MODEL_NAME` | `default` | Model name passed to llama-server's OpenAI-compatible API. |
| `EXPERT_TIMEOUT_SECONDS` | `180` | Per-call timeout. Real latency is 30-150s. |
| `EXPERT_MAX_TOKENS` | `8000` | Max output tokens. Set high enough that real reasoning completes — there is no thinking-budget lever in llama-server for Qwen3, and a low cap yields empty `content`. |
| `EXPERT_CIRCUIT_BREAKER_FAILURES` | `3` | Consecutive failures before the breaker opens. |
| `EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `120` | Open-state cooldown before half-open retry. |

## MemPalace (unified memory)

| Variable | Default | Purpose |
|----------|---------|---------|
| `PALACE_ENABLED` | `true` | Enable/disable MemPalace structured memory |
| `PALACE_COLLECTION` | `mempalace` | ChromaDB collection name for palace |
| `PALACE_YAML_PATH` | `/app/config/palace.yaml` | Palace structure config file |
| `PALACE_WAKEUP_ENABLED` | `true` | Inject wakeup identity context into system prompts |
| `PALACE_WAKEUP_MAX_TOKENS` | `170` | Max tokens for wakeup context block |
| `PALACE_DEDUP_THRESHOLD` | `0.85` | Cosine similarity threshold for dedup |
| `PALACE_SESSION_MINE_PATH` | (empty) | Path to Claude Code session logs for mining |

## Auto-Learn

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUTO_LEARN_ENABLED` | `true` | Enable/disable background fact extraction |
| `AUTO_LEARN_DELAY_MINUTES` | `10` | Delay after conversation before extracting facts |
| `AUTO_LEARN_MAX_FACTS` | `5` | Max facts extracted per conversation |
| `AUTO_LEARN_DEDUP_THRESHOLD` | `0.85` | Cosine similarity threshold for deduplication |
| `AUTO_LEARN_MARKDOWN` | `false` | Also append facts to a markdown file |
| `AUTO_LEARN_ENCRYPT` | `true` | Encrypt stored facts at rest (Fernet) |
| `AUTO_LEARN_ENCRYPTION_KEY` | (auto-generated) | Fernet key; auto-generated if empty |

## Ambient Awareness

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMBIENT_ENABLED` | `true` | Enable ambient summaries |
| `AMBIENT_SUMMARY_TIMES` | `10:00,12:00,14:00,16:00` | TTS summary times |
| `AMBIENT_LED_ENTITY` | (empty) | HA entity for LED status (disabled if empty) |
| `AMBIENT_SPEAKER` | (empty) | Speaker for ambient summaries (default if empty) |

## Self-Care Nudges

These env vars are bootstrap defaults. Once the runtime YAML at `SELFCARE_SCHEDULE_PATH` exists (created on first save from the `/settings` page Selfcare or Quiet Hours panel), `selfcare_manager._check_*` reads enabled/interval/active-hours/quiet-hours from the YAML and ignores the env-var defaults. The legacy hardcoded 9am–9pm meal window is gone — quiet hours additionally honor `is_quiet_day` (day-of-week filter).

| Variable | Default | Purpose |
|----------|---------|---------|
| `SELFCARE_ENABLED` | `true` | Enable self-care nudges |
| `MEAL_NUDGE_HOURS` | `4` | Hours since last meal before nudging |
| `HYDRATION_INTERVAL` | `90` | Minutes between water reminders |
| `MOVEMENT_INTERVAL` | `90` | Minutes between movement reminders |
| `QUIET_HOURS_START` | `22:00` | No nudges after this |
| `QUIET_HOURS_END` | `07:00` | No nudges before this |
| `SELFCARE_SCHEDULE_PATH` | `/app/data/selfcare_schedule.yaml` | Runtime YAML written by the Selfcare + Quiet Hours panels. Loaded by `selfcare_schedule.load_schedule()`; hot-reloaded on every save via `reload_schedule()`. |

## User profile overrides

| Variable | Default | Purpose |
|----------|---------|---------|
| `USER_PROFILE_OVERRIDES_PATH` | `/app/data/user_profile_overrides.yaml` | Writable overlay merged on top of the base `/app/config/user_profile.yaml` (which is mounted `:ro`). The Identity panel writes here via `user_profile.save_profile_partial()`. Loader precedence: base + overrides + env. `reload_profile()` mutates the existing singleton in place so `shared.profile` consumers see updates without a restart. |

## Interruption Recovery

| Variable | Default | Purpose |
|----------|---------|---------|
| `INTERRUPT_CHECKIN_DELAY` | `5` | Minutes after interruption before TTS check-in |
| `CONTEXT_STACK_SIZE` | `10` | Max rolling context entries |

## Routine Scaffolding

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROUTINES_YAML_PATH` | `/app/config/routines.yaml` | Read-only base routine definitions (mounted `:ro` from the repo). |
| `ROUTINES_OVERRIDES_PATH` | `/app/data/routines.yaml` | Writable shadow written by the `/settings → Routines` panel via `routines_config.save_routines()`. Loader precedence: shadow → base. Power-user fields (`ha_action`, `fallback_label`, `fallback_threshold_minutes`, `include_calendar_summary`, `calendar_days_ahead`) are spliced back from the existing on-disk YAML on every save so the panel can't accidentally drop them. PUT triggers `reload_routines_and_reschedule()` which updates `routine_manager._routines` AND replaces the `routine_<id>` cron jobs in APScheduler so a time/day change takes effect without restart; deleted routines have their cron jobs removed. |
| `ROUTINE_ENABLED` | `true` | Enable scheduled routine triggers |
| `ROUTINE_NUDGE_MAX` | `3` | Max nudges per step before auto-skip option |
| `ROUTINE_AUTO_SKIP` | `false` | Auto-skip after max nudges (default: wait for user) |

## Speaker Routing

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANNOUNCEMENT_ROUTES_PATH` | `/app/data/announcement_routes.yaml` | Writable YAML written by the `/settings → Speakers` panel. Maps each announcement category (`selfcare`, `reminder`, `calendar`, `ambient`, `progress`, `focus`, `briefing`) → speaker entity-id (single or comma-separated for multi-room). `_announce_voice(speaker=None, announcement_type=...)` consults `announcement_routes.route_for(...)`. Empty string in a category means "use the legacy fallback" (`REMINDER_SPEAKER` for most, `MORNING_BRIEFING_SPEAKER` for `briefing`, `FOCUS_AUDIO_PLAYER` for `focus`). Hot-reloads on every PUT via `reload_routes()`. The `default` key (if present) acts as a wildcard for any announcement_type not in `CATEGORIES`. |

## Progress Tracking

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROGRESS_ENABLED` | `true` | Enable progress tracking |
| `PROGRESS_DB_PATH` | `/app/data/progress.db` | SQLite database path |
| `DAILY_SUMMARY_TIME` | `18:00` | When to announce daily summary via TTS |
| `WEEKLY_SUMMARY_DAY` | `sunday` | Day for weekly digest |
| `WEEKLY_SUMMARY_TIME` | `19:00` | Time for weekly digest |

## Vision / Image Recognition

| Variable | Default | Purpose |
|----------|---------|---------|
| `VISION_ENABLED` | `false` | Enable/disable image analysis feature |
| `VISION_MODEL_URL` | (empty) | Vision model endpoint, e.g. `http://vision.example.tld:8010/v1` (Qwen3-VL-8B-Instruct Q4_K_M on Saturn RTX 3080 in this deployment). Required when `VISION_ENABLED=true`. |
| `VISION_MODEL_NAME` | `Qwen3VL-8B-Instruct-Q4_K_M.gguf` | Vision model identifier (matches llama.cpp `--model` filename) |
| `VISION_MAX_IMAGE_SIZE` | `10485760` | Maximum image upload size in bytes (10 MB) |
| `VISION_TIMEOUT` | `60` | Vision model request timeout in seconds |

## Email → Calendar (dormant by default)

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMAIL_TO_CALENDAR_ENABLED` | `false` | Enable autonomous email→calendar event creation. **Dormant by default.** Flip to `true` to turn on. |
| `EMAIL_TO_CALENDAR_INTERVAL` | `60` | Minutes between inbox scans |

Implementation lives in `orchestrator/jobs_calendar.py::process_emails_for_events` and is wired into `orchestrator.py` startup behind the enable flag. See `jess-features/F-009-decision-simplifier.md` or the git log entry `840c77e` for context.

## Weather (morning briefing)

Morning briefing includes weather forecast from the National Weather Service API. Home address is geocoded via Nominatim (free, no API key). Override with env vars:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEATHER_LAT` | (geocoded) | Latitude for weather forecast |
| `WEATHER_LON` | (geocoded) | Longitude for weather forecast |

## Notifications

| Variable | Default | Purpose |
|----------|---------|---------|
| `WEBUI_URL` | (empty) | Deep link URL for notifications (opens Open WebUI on tap) |

## Brain Gateway Core

A few essential vars that always need to be set. These are usually defined in `.env` at the repo root:

| Variable | Purpose |
|----------|---------|
| `HA_TOKEN` | Home Assistant long-lived access token |
| `API_TOKEN` | Orchestrator API bearer auth (also required for `/metrics`, `/api/rag/ingest`, `/api/claude_code/turn`) |
| `CHROMA_PERSIST` | ChromaDB persistence path |
| `MIN_COS`, `TOP_K` | RAG retrieval params |
| `GOOGLE_CREDENTIALS_PATH` | OAuth2 client credentials JSON |
| `GOOGLE_TOKEN_PATH` | OAuth2 refresh token JSON |
| `CALENDAR_POLL_INTERVAL` | Minutes between calendar polls (default 15) |
| `MORNING_BRIEFING_TIME` | `HH:MM` for morning briefing (default 07:30) |
| `MORNING_BRIEFING_ENABLED` | `true`/`false` (default true) |
| `MORNING_BRIEFING_MIN_VOLUME` | Volume floor (0.0–1.0) the briefing forces on its target speaker via `media_player.volume_set` before `play_media`. Bumps up only — never lowers an already-loud speaker. Set to `0` to disable the floor. Default `0.4`. Defeats "speaker still at sleep-sound volume" — see the 2026-04-30 incident where the briefing played at 0.10 because the bedroom_pair was still on overnight fireplace audio. |

## Workouts & Meals

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEAL_PHOTOS_DIR` | `/app/data/meal_photos` | Directory for uploaded meal photos. Extension allowlist enforced at save: jpg, jpeg, png, gif, webp. Files named as uuid4. |

## Training corpus drain

| Variable | Default | Purpose |
|----------|---------|---------|
| `TRAINING_CORPUS_DIR` | `/app/data/training_corpus` | Output dir for monthly `YYYY-MM.jsonl` archives. Append-only. |
| `TRAINING_CORPUS_OWUI_DB` | `/app/owui_data/webui.db` | Open WebUI sqlite db read read-only via `open-webui-data:/app/owui_data:ro` mount. |
| `TRAINING_CORPUS_STATE_DB` | `/app/data/brain_state.db` | state_store sqlite db; drains `chat_messages`. |
| `TRAINING_CORPUS_CC_DIR` | `/root/.claude/projects/-opt-gateway-mvp` | Claude Code session jsonls; user-turn extraction only. |

## Paperless bridge (F-012)

Thin file handoff to Paperless-ngx on Jupiter for OCR + auto-tagging. If `PAPERLESS_URL` is empty or `PAPERLESS_API_TOKEN` is shorter than 8 characters, a `model_validator` in `config.py` auto-disables the bridge and logs an error (no exception — other subsystems keep running). `document_vault` is deliberately unaffected and keeps handling typed/pasted text notes.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PAPERLESS_ENABLED` | `false` | Enable the F-012 bridge (`paperless_save` tool + `POST /api/paperless/upload`). Forced off if `PAPERLESS_URL` is empty or `PAPERLESS_API_TOKEN` is too short. |
| `PAPERLESS_URL` | (empty) | Paperless-ngx base URL (e.g. `http://paperless.example.tld:8777`). Required when enabled. |
| `PAPERLESS_API_TOKEN` | (empty) | Paperless API token. Required when enabled; must be >= 8 characters. |
| `PAPERLESS_INBOX_PATH` | `/app/data/paperless_inbox` | Container-side staging dir the `paperless_save` tool reads from. Host-side: `/opt/gateway_mvp/data/app/paperless_inbox/`, bind-mounted via the existing `/app/data` mount. Filename-only inputs — handler rejects `/`, `\`, `..`, absolute paths, null bytes, and symlink escape. |
| `PAPERLESS_DEFAULT_TAGS` | (empty) | Comma-separated tag names applied to every upload (in addition to tool-supplied tags). |
| `PAPERLESS_UPLOAD_TIMEOUT_SECONDS` | `30` | Per-upload httpx timeout. Metric bucketed into `bgw_paperless_upload_latency_seconds`. |

## Ntfy Feedback Loop (F-011)

Third reminder delivery channel with Done/Snooze action buttons. Callbacks hit HMAC-signed URLs (`/api/reminder/ack/{id}`, `/api/reminder/snooze/{id}`). If `NTFY_HMAC_SECRET` is empty, a `model_validator` in `config.py` auto-disables the channel regardless of `NTFY_ENABLED` — reminders continue via TTS + HA Companion push.

| Variable | Default | Purpose |
|----------|---------|---------|
| `NTFY_ENABLED` | `false` | Enable ntfy delivery for reminders. Forced off if `NTFY_HMAC_SECRET` is unset. |
| `NTFY_URL` | `https://ntfy.sh` | Ntfy server base URL. Typically points at the self-hosted instance on Jupiter. |
| `NTFY_TOPIC` | (empty) | Ntfy topic to publish to. Required when enabled. |
| `NTFY_DEFAULT_PRIORITY` | `3` | Ntfy priority (1-5). Overridden per-reminder when urgency is set. |
| `NTFY_CALLBACK_BASE_URL` | (empty) | Public base URL the orchestrator is reachable at for callbacks (e.g. `https://helios.tail74fc4a.ts.net`). Used to build Done/Snooze action URLs. |
| `NTFY_HMAC_SECRET` | (empty) | Secret for signing callback URLs. Scheme: `sig = HMAC-SHA256(secret, f"{id}|{action}|{exp}|{extra}")[:32]`. Missing → channel auto-disables. |
| `NTFY_ACK_EXP_SECONDS` | `86400` | TTL for ack/snooze callback signatures (seconds). |
| `NTFY_MAX_SNOOZE_COUNT` | `3` | Max snoozes per reminder before Snooze button is dropped. |
| `NTFY_CONFIRM_ENABLED` | `false` | Opt-in. When true, after a successful ack/snooze callback the orchestrator pushes a low-priority (priority=1) confirmation message back to the same ntfy topic so the user sees visible feedback that the button registered (iOS can't mutate action buttons in-place). Title stays generic; action-specific detail lives in body only (topic is open-tailnet, titles render on lockscreen). |

## Pushover Bridge (F-013)

Parallel iOS push channel alongside F-011 ntfy. Pushover has native APNs integration so lockscreen banners land reliably on iOS where self-hosted ntfy-upstream was flaky. Runs ALONGSIDE ntfy (double push if both enabled); `NTFY_ENABLED` and `PUSHOVER_ENABLED` are independent flags. Reuses F-011's HMAC-signed `/api/reminder/ack/{id}` + `/api/reminder/snooze/{id}` callback routes — no new routes, just a new outbound channel. If `PUSHOVER_USER_KEY` or `PUSHOVER_APP_TOKEN` is missing, a `model_validator` in `config.py` auto-disables the channel regardless of `PUSHOVER_ENABLED` and logs an error (matches F-011/F-012 pattern; no exception). Reminder text is HTML-escaped before embedding in Pushover's HTML body to block prompt-injection `<a href>` planted via `set_reminder`.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUSHOVER_ENABLED` | `false` | Enable Pushover delivery for reminders. Forced off if `PUSHOVER_USER_KEY` or `PUSHOVER_APP_TOKEN` is missing. |
| `PUSHOVER_USER_KEY` | (empty) | Pushover user key. Required when enabled. |
| `PUSHOVER_APP_TOKEN` | (empty) | Pushover application API token. Required when enabled. |
| `PUSHOVER_DEFAULT_PRIORITY` | `0` | Pushover priority (-2 to 2). Overridden per-reminder when urgency is set; values are clamped to the Pushover-valid range. |
| `PUSHOVER_DEVICE` | (empty) | Optional device name to target a specific device; empty = all user devices. |
| `PUSHOVER_TIMEOUT_SECONDS` | `10` | Per-request httpx timeout. Bucketed into `bgw_pushover_push_latency_seconds`. |

## Self-audit (F-014)

Daily 7am UTC scheduled job (`orchestrator/jobs_self_audit.py`) that queries Loki for the last-24h error/warn logs across Helios services, asks Jess to diagnose each cluster (single `call_model` pass, no tool loop), saves a markdown report under `/app/data/self_audits/YYYY-MM-DD.md`, and pushes a one-line digest via Pushover. Read-only by design — Jess emits text only, the orchestrator never executes her output. Three-layer safety: allow-list filter on Jess's suggested shell commands, dangerous-pattern regex, secret-pattern filter on both disk report and mempalace summary. Concurrency lock prevents manual + cron collision. Loki-unreachable is distinguished from a clean week (upfront probe + explicit `result="failed"` digest, never a green "all clean" lie). Summary indexed into mempalace under wing=`system`, room=`audit`. Default-OFF.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SELF_AUDIT_ENABLED` | `false` | Master flag. Default-OFF; flip to `true` in `.env` to enable both the daily cron job and `POST /api/self_audit/run`. |
| `SELF_AUDIT_HOUR_UTC` | `7` | Hour of day (UTC) the daily cron fires. |
| `SELF_AUDIT_LOOKBACK_HOURS` | `24` | How far back to query Loki for error/warn logs. |
| `SELF_AUDIT_LOKI_URL` | _empty (required when `SELF_AUDIT_ENABLED=true`)_ | Loki base URL (e.g. `http://loki.example.tld:3100`). **Operator-controlled and trusted same as `MODEL_URL`** — there's no allow-list validator, only an `http(s)://` prefix check. Operators should not point this at attacker-controlled hosts. If empty while self-audit is on, the model_validator auto-disables self-audit at startup. |
| `SELF_AUDIT_PROM_URL` | _empty (optional)_ | Prometheus base URL used by the weekly review (e.g. `http://prom.example.tld:9090`). When unset, the weekly review skips Prometheus aggregates. |
| `SELF_AUDIT_MAX_CLUSTERS` | `30` | Max number of `(service, first-80-chars-of-message)` clusters kept after frequency-bucketing before sending to Jess. |
| `SELF_AUDIT_OUTPUT_DIR` | `/app/data/self_audits` | Directory for `YYYY-MM-DD.md` markdown reports. Host-mounted via the existing `/app/data` bind. |
| `SELF_AUDIT_LLM_TIMEOUT_SEC` | `120` | Per-call timeout for the diagnose-each-cluster `call_model` invocation. |

## Monitoring

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOKI_PUSH_URL` | _empty_ | Loki push endpoint used by the Helios promtail sidecar (e.g. `http://loki.example.tld:3100/loki/api/v1/push`). Required when `COMPOSE_PROFILES=advanced` is on (which brings up promtail); ignored otherwise. Soft default — fail-fast happens inside the promtail container, not at compose-parse time, because compose expands env vars file-wide before profile filtering. |
