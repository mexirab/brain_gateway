# Environment Variables

All orchestrator-side environment variables, grouped by subsystem. Defaults in parentheses. Anything not listed here either lives in `.env.example` as a bootstrap template or is part of the infrastructure (Docker, HA, Google OAuth) rather than the application.

For the authoritative template, see `.env.example` at the repo root.

## Model / LLM backends

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODEL_URL` | — | Primary model endpoint (e.g. `http://10.0.0.195:8080/v1`) |
| `MODEL_NAME` | — | Primary model name (e.g. `Qwen3.5-27B`) |
| `FALLBACK_MODEL_URL` | — | Fallback model endpoint (optional) |
| `FALLBACK_MODEL_NAME` | — | Fallback model name (optional) |
| `EMBEDDING_MODEL` | — | Embedding model for RAG indexing |
| `MODEL_SERVER_IP` | — | SSH target for remote model start/stop |
| `MODEL_SSH_USER` | — | SSH user for model server |
| `MODEL_START_CMD` | — | Command to start model server via SSH |
| `MODEL_STOP_CMD` | — | Command to stop model server via SSH |

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

| Variable | Default | Purpose |
|----------|---------|---------|
| `SELFCARE_ENABLED` | `true` | Enable self-care nudges |
| `MEAL_NUDGE_HOURS` | `4` | Hours since last meal before nudging |
| `HYDRATION_INTERVAL` | `90` | Minutes between water reminders |
| `MOVEMENT_INTERVAL` | `90` | Minutes between movement reminders |
| `QUIET_HOURS_START` | `22:00` | No nudges after this |
| `QUIET_HOURS_END` | `07:00` | No nudges before this |

## Interruption Recovery

| Variable | Default | Purpose |
|----------|---------|---------|
| `INTERRUPT_CHECKIN_DELAY` | `5` | Minutes after interruption before TTS check-in |
| `CONTEXT_STACK_SIZE` | `10` | Max rolling context entries |

## Routine Scaffolding

| Variable | Default | Purpose |
|----------|---------|---------|
| `ROUTINES_YAML_PATH` | `/app/config/routines.yaml` | Routine definitions file |
| `ROUTINE_ENABLED` | `true` | Enable scheduled routine triggers |
| `ROUTINE_NUDGE_MAX` | `3` | Max nudges per step before auto-skip option |
| `ROUTINE_AUTO_SKIP` | `false` | Auto-skip after max nudges (default: wait for user) |

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
| `VISION_ENABLED` | `true` | Enable/disable image analysis feature |
| `VISION_MODEL_URL` | `http://10.0.0.58:8010/v1` | Vision model endpoint (Qwen3-VL-8B-Instruct Q4_K_M on Saturn RTX 3080) |
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
| `TRAINING_CORPUS_CC_DIR` | `/root/.claude/projects/-opt-helios-gateway-mvp` | Claude Code session jsonls; user-turn extraction only. |

## Paperless bridge (F-012)

Thin file handoff to Paperless-ngx on Jupiter for OCR + auto-tagging. If `PAPERLESS_URL` is empty or `PAPERLESS_API_TOKEN` is shorter than 8 characters, a `model_validator` in `config.py` auto-disables the bridge and logs an error (no exception — other subsystems keep running). `document_vault` is deliberately unaffected and keeps handling typed/pasted text notes.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PAPERLESS_ENABLED` | `false` | Enable the F-012 bridge (`paperless_save` tool + `POST /api/paperless/upload`). Forced off if `PAPERLESS_URL` is empty or `PAPERLESS_API_TOKEN` is too short. |
| `PAPERLESS_URL` | (empty) | Paperless-ngx base URL (e.g. `http://10.0.0.248:8777`). Required when enabled. |
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

## Monitoring

| Variable | Default | Purpose |
|----------|---------|---------|
| `LOKI_PUSH_URL` | `http://jupiter-amds.tail74fc4a.ts.net:3100/loki/api/v1/push` | Loki push endpoint used by the Helios promtail sidecar. Override with `http://10.0.0.248:3100/loki/api/v1/push` if the tailnet is unavailable. |
