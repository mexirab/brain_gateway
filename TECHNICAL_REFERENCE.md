# Technical Reference

API specs and schemas for implementation.

## API Endpoints

All orchestrator endpoints live on port 8888. Many endpoints require `Authorization: Bearer $API_TOKEN` when `API_TOKEN` is set in `.env` — if you get a 401, that's why.

### Core

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Status, versions, counts |
| GET | `/metrics` | Prometheus metrics (bearer-auth) |
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| GET | `/v1/models` | List models |

### Home Assistant

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/ha/entities` | List HA entities |
| POST | `/api/ha/command` | Direct HA service call |
| GET | `/api/temperatures` | Temperature sensor readings from HA |

### Memory (RAG + MemPalace + Auto-Learn)

Legacy flat RAG endpoints and the structured MemPalace endpoints both read/write the same unified `mempalace` ChromaDB collection.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/memory/search` | Flat semantic search |
| GET | `/api/memory/stats` | Collection stats |
| POST | `/api/memory/add` | Add document (optional `tags` array) |
| GET | `/api/memory/learned` | List auto-learned facts (optional `?category=`, `?limit=`) |
| DELETE | `/api/memory/learned/{doc_id}` | Delete a single learned fact |
| DELETE | `/api/memory/learned?confirm=true` | Wipe all learned facts |
| GET | `/api/memory/learned/stats` | Auto-learn statistics (counts by category) |
| POST | `/api/memory/learned/toggle` | Enable/disable auto-learn at runtime |
| GET | `/api/palace/search?query=&wing=&room=&n=5` | Structured palace search with optional wing/room filter |
| POST | `/api/palace/store` | Store a memory: `{text, wing?, room?, source?, category?, project?}` |
| GET | `/api/palace/memory/{doc_id}` | Get a single memory by ID |
| DELETE | `/api/palace/memory/{doc_id}` | Delete a memory by ID |
| GET | `/api/palace/wings` | Palace wing structure |
| GET | `/api/palace/wings/{wing}/rooms` | Rooms in a wing with memory counts |
| GET | `/api/palace/stats` | Memory counts by wing |
| POST | `/api/palace/mine` | Trigger Claude Code session mining |
| POST | `/api/rag/ingest` | Force immediate re-ingest of source files (bypasses 2-min scheduler) |

### Chat History

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/chat/conversations?limit=50` | List conversations (most recent first) |
| POST | `/api/chat/conversations` | Create conversation: `{title}` |
| GET | `/api/chat/conversations/:id/messages` | Get conversation + messages |
| POST | `/api/chat/conversations/:id/messages` | Save message: `{role, content, routing?, announcement_type?}` |
| PUT | `/api/chat/conversations/:id` | Update title: `{title}` |
| DELETE | `/api/chat/conversations/:id` | Delete conversation + messages |

### Voice (STT / TTS / Announcements)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/stt/transcribe` | Proxy audio to Whisper STT (multipart, max 10 MB) |
| POST | `/api/tts/synthesize` | Synthesize text to WAV: `{text}` |
| POST | `/api/announce` | Trigger TTS announcement via voice system |
| GET | `/api/audio/{filename}` | Serve audio files (reminders, TTS) |
| GET | `/api/announcements/history` | Recent announcement history (optional `?limit=`, `?type=`) |
| GET | `/api/announcements/stats` | Success rates, per-speaker breakdown, latency |

### Reminders & Focus

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/reminder/trigger` | Trigger a reminder |
| GET | `/api/reminders` | List pending reminders |
| POST | `/api/reminder/complete/{id}` | Mark reminder completed |
| POST | `/api/reminder/ack/{id}?sig=&exp=` | Ntfy Done callback (HMAC-gated, bearer-exempt). Marks reminder acked via ntfy and fires selfcare bridge. |
| POST | `/api/reminder/snooze/{id}?sig=&exp=&minutes=` | Ntfy Snooze callback (HMAC-gated, bearer-exempt). Reschedules delivery by `minutes` (default 15); rejected once `snooze_count >= NTFY_MAX_SNOOZE_COUNT`. |
| GET | `/api/focus` | Current focus session status |
| POST | `/api/focus/start` | Start focus session via API |
| POST | `/api/focus/stop` | Stop focus session via API |

**Ntfy callback HMAC scheme.** Both `/api/reminder/ack/{id}` and `/api/reminder/snooze/{id}` are registered in `BearerAuthMiddleware.PUBLIC_PREFIXES` (bearer-exempt) and gated instead by an HMAC-SHA256 signature. Signature construction: `sig = hmac.new(NTFY_HMAC_SECRET, f"{id}|{action}|{exp}|{extra}".encode(), sha256).hexdigest()[:32]` where `action` is `ack` or `snooze`, `exp` is a Unix timestamp, and `extra` is the snooze-`minutes` value for snooze callbacks (empty string for ack). Requests are rejected when the signature mismatches, `exp` is in the past, the reminder doesn't exist, or (snooze only) `snooze_count >= NTFY_MAX_SNOOZE_COUNT`. Rejections increment `bgw_ntfy_callback_rejected_total{reason}`. On successful ack/snooze, if `NTFY_CONFIRM_ENABLED=true` the routes fire `asyncio.create_task(reminder_manager.deliver_ack_confirm(...))` to push a low-priority (priority=1) confirm message back to the same topic ("✓ Logged" / "💤 Snoozed until ..."). The confirm title stays generic (topic is open-tailnet; titles render on lockscreen). `bgw_ntfy_push_total` and `bgw_ntfy_push_latency_seconds` carry a `kind` label (`reminder|confirm`) to distinguish the two paths.

**Feature-flag gate (F-013 widening).** The routes' bearer-exempt / feature-enabled gate widens to `if not (ntfy_enabled or pushover_enabled)` on BOTH ack and snooze so that pushover-only deployments (`NTFY_ENABLED=false`, `PUSHOVER_ENABLED=true`) still process callbacks. On success the routes fire both `_fire_and_forget(deliver_ack_confirm(...))` (F-011) and `_fire_and_forget(deliver_pushover_confirm(...))` (F-013) — each channel is independently gated by its own enable flag inside its manager, so users who run only one channel get exactly one confirm.

**Pushover push metrics (F-013).** `bgw_pushover_push_total{result,kind,reason}` counter + `bgw_pushover_push_latency_seconds{kind}` histogram. `kind` ∈ `{reminder, confirm}` (same split as ntfy). `reason` ∈ `{ok, http_4xx, http_5xx, timeout, connect_error, other, disabled, missing_user_key, missing_app_token, missing_credentials}` — lets Grafana distinguish "Pushover token revoked" (http_4xx) from "Pushover down" (http_5xx / connect_error / timeout) from "user turned feature off" (disabled). Reminder text is HTML-escaped via `html.escape(text, quote=False)` before embedding in the Pushover HTML body (prompt-injection defense — reminder text comes from the LLM via `set_reminder` and would otherwise render as tappable `<a href>`). Error bodies run through a credential-regex-strip before being logged.

### Selfcare

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/selfcare/today` | Today's selfcare log + last-seen-ever timestamps for the 4 tracked actions (`medication`, `meal`, `water`, `movement`). Bearer-gated (NOT in `PUBLIC_PREFIXES`). Backed by existing `state_store.get_selfcare_today()` + `get_last_selfcare()` helpers — no new schema. |

**Response shape:**

```json
{
  "ok": true,
  "as_of": "2026-04-27T14:30:00-05:00",
  "today_date": "2026-04-27",
  "actions": {
    "medication": {"today_count": 1, "last_at": "2026-04-27T08:15:00-05:00", "entries": [{"id": 42, "logged_at": "...", "detail": "Adderall"}]},
    "meal":       {"today_count": 2, "last_at": "...", "entries": [...]},
    "water":      {"today_count": 0, "last_at": "2026-04-26T22:10:00-05:00", "entries": []},
    "movement":   {"today_count": 3, "last_at": "...", "entries": [...]}
  }
}
```

On exception → 500 `{ok: false, error: "Selfcare read failed"}` and a `logger.error`. Powers the dashboard `SelfcareTodayCard` (polls every 30s).

### Calendar & Email

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/calendar/today` | Today's calendar events (phone sync + Google fallback) |
| GET/POST/PUT | `/api/calendar/sync` | Phone calendar sync (GET=status, POST/PUT=receive events) |
| POST | `/api/email-to-calendar/run` | Manually trigger email-to-calendar extraction (dormant by default — see `EMAIL_TO_CALENDAR_ENABLED`) |

### Progress Tracking

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/progress/today` | Today's stats (tasks, focus, brain dumps) |
| GET | `/api/progress/week` | This week's stats + trend vs prior week |
| GET | `/api/progress/streaks` | Active streaks (with lazy decay — stale streaks report `current: 0`) |

### Shopping Lists

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/shopping?list_name=&include_checked=` | Get shopping list items |
| POST | `/api/shopping` | Add item: `{item, list_name}` |
| POST | `/api/shopping/{id}/check` | Check off item |
| POST | `/api/shopping/{id}/uncheck` | Uncheck item |
| DELETE | `/api/shopping/checked?list_name=` | Clear all checked items |
| DELETE | `/api/shopping/{id}` | Delete item |

### Vision

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/vision/analyze` | Analyze an image (multipart form or JSON with base64) |
| GET | `/api/vision/status` | Vision model health and configuration |

### Ambient / System

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/ambient/status` | Aggregated ambient status (schedule, focus, tasks, LED color). Calendar source: phone-sync-first (<24h, at least one parseable record) → Google fallback. Same priority as `check_calendar` and `morning_briefing` — see `docs/GOOGLE_INTEGRATIONS.md` → Phone Calendar Sync. |

### Workouts

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/workouts/today` | Today's workout plan + logged sets |
| POST | `/api/workouts/generate` | Generate today's plan (idempotent — returns existing plan if one exists) |
| GET | `/api/workouts/history` | Past workout sessions |
| GET | `/api/workouts/exercises` | Full exercise catalog (52 entries) |
| POST | `/api/workouts/sets` | Log a set: `{workout_id, exercise_id, set_number, weight_lbs, reps}` |
| PATCH | `/api/workouts/{id}` | Modify workout (swap/remove exercise) |
| DELETE | `/api/workouts/{id}` | Delete a workout |
| POST | `/api/workouts/{id}/end` | End a workout session |
| DELETE | `/api/workouts/sets/{id}` | Delete a logged set |

### Meals

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/meals/today` | Today's meals + running calorie total |
| GET | `/api/meals/history?days=7` | Meal history (`days` clamped 1–365) |
| POST | `/api/meals/` | Log a meal: `{name, calories, notes?}` |
| PATCH | `/api/meals/{id}` | Update a meal (`photo_path` field excluded from allowlist) |
| DELETE | `/api/meals/{id}` | Delete a meal |
| POST | `/api/meals/photo` | Upload meal photo → vision estimate: multipart `file` field; returns `{calories_estimate, description, confidence}` |
| GET | `/api/meals/photo/{filename}` | Serve a stored meal photo |

**Photo flow:** upload → Qwen3-VL-8B strict-JSON prompt → return estimate → user confirms in UI before save (or pass `auto_log=true` in POST body to skip confirmation). Extension allowlist: `jpg`, `jpeg`, `png`, `gif`, `webp`. Files saved as uuid4 names under `MEAL_PHOTOS_DIR`.

### Paperless Bridge (F-012)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/paperless/upload` | Multipart upload → forward file to Paperless-ngx for OCR + tagging. Bearer-gated (NOT in `PUBLIC_PREFIXES`). 100 MB cap via `_LARGE_UPLOAD_PATHS`. Form fields: `file` (required), `title`, `correspondent`, `document_type`, `tags` (repeat field). Returns `{ok, task_id, latency_ms}` on 200, `{ok:false, error}` on 503 (feature disabled / runtime-disabled) or 502 (Paperless rejected / unreachable). |

Responses are never cached. No local copy is persisted on Helios — Paperless owns the file once it returns a task id. Metrics: `bgw_paperless_upload_total{result,reason}` (labels: `result` ∈ `{ok, fail, skipped}`, `reason` ∈ `{ok, http_4xx, http_5xx, timeout, connect_error, other, disabled, missing_url, missing_token, file_too_large, file_missing}`), `bgw_paperless_upload_latency_seconds`.

### Claude Code Integration

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/claude_code/turn` | Stop hook target — logs a completed Claude Code turn to the 7-day rolling buffer |
| GET | `/api/claude_code/recent?minutes=120&limit=20` | List recent Claude Code turns for dashboards or the `check_claude_activity` tool |

### Settings (`/api/config/*`)

All bearer-gated. Reached from the frontend via `/api/proxy/*`. Backed by `orchestrator/routes_config.py`. Every successful PUT/POST/DELETE writes a redacted before/after diff to the `config_changes` SQLite table via `config_writer.log_config_change(panel, before, after)`. YAML writes go through `config_writer.atomic_write_yaml()` (tmpfile + `os.replace` + fsync).

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/api/config/identity` | Returns `{assistant_name, user_name, adhd_mode, tone_preference, timezone}`. Reads the merged base + overrides view of the user profile. |
| PUT    | `/api/config/identity` | Partial update of the same shape. Writes to `USER_PROFILE_OVERRIDES_PATH` only (base is `:ro`). Calls `reload_profile()` to mutate the singleton in place. |
| GET    | `/api/config/selfcare` | Returns `{categories: {medication: {...}, meal: {...}, water: {...}, movement: {...}}}`. |
| PUT    | `/api/config/selfcare` | Partial `categories.*` merge. Writes to `SELFCARE_SCHEDULE_PATH` and calls `selfcare_schedule.reload_schedule()`. |
| GET    | `/api/config/quiet_hours` | Returns `{start, end, days: ["mon", ...]}`. Source-of-truth shared with the Selfcare panel (lives in the same YAML). |
| PUT    | `/api/config/quiet_hours` | Partial update of the same shape. Same write/reload path as selfcare. |
| GET    | `/api/config/recurring_reminders` | Returns `{rules: [{id, text, cron_expression, target, days_of_week, enabled, ...}, ...]}`. |
| POST   | `/api/config/recurring_reminders` | Create a rule: `{text, cron_expression, target, days_of_week, enabled}`. Cron validated via `croniter`. |
| PUT    | `/api/config/recurring_reminders/{id}` | Partial update. |
| DELETE | `/api/config/recurring_reminders/{id}` | Delete a rule. |

**Recurring expansion job.** `recurring_reminders.expand_due_reminders()` runs every 5 min as APScheduler interval job `id="recurring_reminders_expand"` (registered in `orchestrator.py`). For each enabled rule it walks `croniter` forward (capped at 14 days lookahead), filters by `days_of_week`, materializes due rows into the existing one-shot `reminders` table for `deliver_reminder_job` to pick up, and auto-disables rules whose cron never fires (e.g. `0 0 30 2 *` — Feb 30). Metrics: `bgw_recurring_reminders_expanded_total`, `bgw_recurring_reminders_expand_errors_total`.

### HA Command Format

```json
{
  "entity_id": "light.living_room",
  "service": "turn_on",
  "data": {"brightness": 128, "rgb_color": [0, 0, 255]}
}
```

**Services by domain:**

| Domain | Services | Data |
|--------|----------|------|
| light | turn_on, turn_off, toggle | brightness (0-255), rgb_color |
| switch/fan | turn_on, turn_off, toggle | - |
| climate | set_temperature | temperature |
| cover | open_cover, close_cover | position (0-100) |
| scene | turn_on | - |

**Colors (RGB):** blue=[0,0,255], red=[255,0,0], green=[0,255,0], purple=[128,0,128], white=[255,255,255]

**Brightness:** 50%=128, 75%=191, 100%=255

## Tool Result Cap

The unified loop enforces `MAX_TOOL_RESULT_CHARS = 8000` (~2000 tokens) on every tool result before it is appended to the conversation. This leaves context headroom for the system prompt (~1500 tokens), RAG injection (~1000 tokens), turn history, and several concurrent tool results within the 32K context window.

**Overflow behavior:** `_cap_tool_result()` truncates to 8000 chars and appends a model-facing footer: _"Work with the information above; do not call this tool again to retrieve the rest."_ A `WARNING` log is emitted with the tool name and both char counts.

**Design implication:** Tools must not be designed around returning large blobs (full email threads, long document text, etc.). Summarize or paginate at the tool handler level — the cap is a safety net, not a substitute for well-scoped tool output.

## Tool Schemas

### home_assistant
```json
{"entity_id": "light.x", "service": "turn_on", "data": {"brightness": 128}}
```

### search_memory
```json
{"query": "current projects"}
```

### update_data
```json
{"action": "add_medication", "name": "Adderall", "dose": "20mg", "schedule": "morning"}
```

**Actions:** add_medication, remove_medication, update_medication, add_project, update_project_status, add_project_step, complete_step

### set_reminder
```json
{"reminder_text": "call mom", "time": "in 30 minutes", "target": "both"}
```

### cancel_reminder
```json
{"reminder_id": "abc123"}
```

### start_focus
```json
{"duration_minutes": 30, "blocking": true}
```

### stop_focus
```json
{}
```

### focus_status
```json
{}
```

### web_search
```json
{"query": "weather Houston today", "category": "general", "time_range": "day"}
```

### check_calendar
```json
{"days_ahead": 7}
```
- `days_ahead` (int, optional): Number of days to look ahead. Default: 7. Use 1 for today, 2 for tomorrow.

### create_calendar_event
```json
{"title": "Pickleball at Honcho", "start_time": "2026-02-26T19:00:00", "duration_minutes": 60, "location": "Honcho"}
```
- `title` (string, required): Event title
- `start_time` (string, required): ISO 8601 datetime
- `duration_minutes` (int, optional): Default 60
- `description` (string, optional): Event description
- `location` (string, optional): Event location

### focus_sprint
```json
{"action": "next_sprint", "duration_minutes": 25}
```
- `action` (string, required): `next_sprint`, `extend`, or `end_session`
- `duration_minutes` (int, optional): Override sprint length or minutes to add for extend

### start_routine
```json
{"routine_id": "morning"}
```
- `routine_id` (string, required): `morning` or `evening`

### routine_action
```json
{"action": "done"}
```
- `action` (string, required): `done`, `skip`, `pause`, `resume`, `stop`, or `status`
- Side effect on `"done"`: `routine_manager.advance_step` calls `selfcare_manager.mark_selfcare_from_routine_step(step)`, which dispatches to `record_medication_logged`/`record_meal_logged`/`record_hydration_logged`/`record_movement_logged` based on the medication/meal/water/movement keyword map against step id/label. Suppresses the corresponding nudge. NOT fired on `"skip"` or auto-end `"stop"`.

### routine_status
```json
{}
```

### selfcare_log
```json
{"action": "meal", "detail": "lunch"}
```
- `action` (string, required): `meal`, `medication`, `water`, or `movement`
- `detail` (string, optional): Medication name or meal type
- Side effect: if an active routine's current step matches `action` (word-boundary match on step id/label against the medication/meal/water/movement keyword map), the routine auto-advances via `routine_manager.advance_step("done")`. Fire-and-forget; never blocks the selfcare write.

### decide_for_me
```json
{"domain": "work", "constraints": "under 30 minutes"}
```
- `domain` (string, required): `work`, `food`, `general`, or `overwhelm`
- `constraints` (string, optional): Constraints like "quick", "healthy", "under 30 minutes"

### bookmark_context
```json
{"description": "writing the API docs"}
```
- `description` (string, optional): What the user is working on (auto-detected from active focus/task if omitted)

### recall_context
```json
{"count": 3}
```
- `count` (int, optional): Number of recent contexts to return (default: 3)

### check_system
```json
{"query": "system_health"}
```
- `query` (string, required): `morning_briefing`, `calendar_poll`, `email_poll`, `reminders`, `focus_timer`, `temperature`, `system_health`, or `recent_errors`

### memory/add
```json
{
  "content": "Document text to store",
  "category": "personal",
  "source": "api",
  "tags": ["adhd", "pattern"]
}
```
- `content` (string, required): Text to embed and store
- `category` (string, optional): Category label
- `source` (string, optional): Source identifier
- `tags` (array of strings, optional): Stored as comma-separated string in ChromaDB metadata

### generate_workout
```json
{}
```
Returns the full workout plan as text (model retains it in context for follow-up questions like "swap squats for leg press"). Model should NOT read the plan aloud — user is at the gym.

**Adaptive logic:**
- < 1 session in last 4 days → `full_body`
- 1 session in last 3 days → `full_body_complement` (skewed toward undertrained muscles)
- 2+ sessions in last 4 days → `push` / `pull` / `legs` split, chosen to complement recency

### log_set
```json
{"workout_id": 1, "exercise_id": 7, "set_number": 1, "weight_lbs": 135.0, "reps": 8}
```
- All weights in lbs.
- Side effect: after persisting the set, calls `selfcare_manager.record_movement_logged(f"set:{exercise_name}")`, which resets both `last_movement_nudge` and `sitting_since` (prevents sitting-timer nudges while at the gym). Wrapped in try/except; never blocks the set write.

### workout_status
```json
{}
```

### modify_workout
```json
{"workout_id": 1, "action": "swap", "exercise_id": 7, "replacement_exercise_id": 12}
```
- `action`: `swap` or `remove`

### log_meal
```json
{"name": "chicken and rice", "calories": 650, "notes": "post-gym"}
```
- Calories-only (v1). Independent of `selfcare_log` (which is nudge tracking, not calorie accounting).
- `auto_log` (bool, optional): If `true`, skips confirmation when called after a photo estimate.

### paperless_save
```json
{"filename": "tax-q3-2026.pdf", "title": "Q3 2026 Taxes", "correspondent": "IRS", "document_type": "tax", "tags": ["taxes", "2026"]}
```
- `filename` (string, required): Basename of a file already present in `PAPERLESS_INBOX_PATH`. Handler rejects path separators, `..`, absolute paths, null bytes, and symlink escape (`Path.resolve() + relative_to(inbox)`).
- `title`, `correspondent`, `document_type` (string, optional): Paperless metadata. Inferred from filename/OCR when omitted.
- `tags` (array of strings, optional): Added to `PAPERLESS_DEFAULT_TAGS`. Missing tags are created by Paperless if the server setting allows; otherwise ignored.
- File read uses `asyncio.to_thread` to avoid blocking the event loop. Size cap enforced before `read_bytes()`. Auto-disabled bridge surfaces as a structured "skipped" tool result rather than raising.

### ask_expert
```json
{"question": "Why might my 2025 gaming spend have spiked in November?"}
```
- `question` (string, required): A self-contained question. The expert (Qwen3-32B Thinking on Saturn 3090, port 8084) is stateless — pass all needed context in the question.
- One-shot, blocking. Latency 30-150s in practice; 180s timeout. The primary should warn the user before invoking.
- llama.cpp `--jinja` mode separates `message.content` (final answer) from `message.reasoning_content` (the `<think>` trace); only the final content is returned.
- Auto-disabled when `EXPERT_ENABLED=false` or `EXPERT_MODEL_URL` is empty — returns a short string explaining the disabled state instead of raising. Circuit breaker opens after `EXPERT_CIRCUIT_BREAKER_FAILURES` (default 3) consecutive failures, half-opens after `EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default 120s).
- Metrics: `bgw_expert_call_total{result}`, `bgw_expert_call_latency_seconds`, `bgw_expert_circuit_open` (gauge), `bgw_expert_reasoning_tokens` (histogram).

### query_budget
```json
{"question_type": "analyze", "analysis_question": "What patterns stood out in my 2025 gaming spend?", "year": 2025, "category": "Gaming"}
```
- `question_type` (enum, required): `total | by_category | by_month | top_payees | outliers | analyze`. Narrow types return structured facts for a single dimension. `analyze` gathers totals + top 5 categories + top 5 payees + up to 36 months + top 3 outliers (respecting filters) and delegates the synthesis to the expert reasoning model (`handle_ask_expert`) in a single tool call.
- `analysis_question` (string, required when `question_type="analyze"`): The user's synthesis question, passed verbatim to the expert.
- Filters (all optional): `year`, `month`, `category`, `payee`, `min_amount`, `max_amount`.
- Response for `analyze`: `{expert_synthesis: str | null, expert_error: str | null, data: {...}, ...}`. `expert_synthesis=null` + non-null `expert_error` when the expert is unreachable, disabled, or its circuit is open — the `data` block is always populated so the primary model can fall back to surface-level summary.
- `budget_manager.query()` is `async` (awaits the expert). `by_month` internally capped at 36 entries.

## ChromaDB Schema

**Chunk ID:** `chunk::{path}::{section}::{index}::{hash[:12]}`

```json
{
  "metadata": {
    "file_path": "path/to/file.md",
    "section": "h2:Section Name",
    "chunk_index": 0,
    "kind": "chunk",
    "tags": "adhd,pattern"
  }
}
```

## SQLite Schema (`brain_state.db`)

Defined in `orchestrator/state_store.py::SCHEMA_SQL`. Listing only the tables not covered elsewhere in this doc.

### `recurring_reminders`

CRUD source for the `/api/config/recurring_reminders` endpoints. Rows are read every 5 min by `expand_due_reminders()` and materialized into one-shot `reminders` rows for `deliver_reminder_job`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | Auto-increment rule id. |
| `text` | TEXT NOT NULL | Reminder text (HTML-escaped before Pushover delivery — F-013). |
| `cron_expression` | TEXT NOT NULL | Standard 5-field cron, validated with `croniter`. Impossible crons (e.g. `0 0 30 2 *`) are detected at expand time and the rule is auto-disabled. |
| `target` | TEXT | Speaker/device target — same shape `set_reminder` uses. |
| `days_of_week` | TEXT | Comma-separated `mon,tue,...` filter applied on top of the cron. Empty = all days. |
| `enabled` | INTEGER | `1` enabled, `0` disabled. Set to `0` automatically on impossible crons. |
| `last_expanded_at` | TEXT | ISO 8601 timestamp of the last successful expansion pass; used to dedupe materialized rows. |
| `created_at` / `updated_at` | TEXT | ISO 8601 timestamps. |

### `config_changes`

Append-only audit log written by `config_writer.log_config_change(panel, before, after)`. Every successful PUT/POST/DELETE on `/api/config/*` adds one row. Sensitive fields are masked through `_redact()` before persistence.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | Auto-increment. |
| `panel` | TEXT NOT NULL | One of `identity`, `selfcare`, `quiet_hours`, `recurring_reminders`. |
| `before` | TEXT | Redacted JSON snapshot of the prior value. |
| `after` | TEXT | Redacted JSON snapshot of the new value. |
| `changed_at` | TEXT | ISO 8601 timestamp. |

## Environment Variables

See `.env.example` for full list. Key vars:

| Variable | Purpose |
|----------|---------|
| NODE_*_IP | Cluster node IPs |
| HA_TOKEN | Home Assistant token |
| API_TOKEN | Orchestrator API auth |
| CHROMA_PERSIST | ChromaDB path |
| MIN_COS, TOP_K | RAG params |
| GOOGLE_CREDENTIALS_PATH | OAuth2 credentials JSON path |
| GOOGLE_TOKEN_PATH | OAuth2 token JSON path |
| CALENDAR_POLL_INTERVAL | Minutes between calendar polls (default: 15) |
| MORNING_BRIEFING_TIME | HH:MM for morning briefing (default: 07:30) |
| MORNING_BRIEFING_ENABLED | true/false (default: true) |

## External APIs

| API | Base URL | Auth |
|-----|----------|------|
| Home Assistant | http://10.0.0.106:8123/api | Bearer token |
| Google Calendar | https://www.googleapis.com/calendar/v3 | OAuth2 bearer token |
| SearXNG | http://searxng:8080 (internal) | None |
