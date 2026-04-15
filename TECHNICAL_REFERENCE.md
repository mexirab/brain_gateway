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
| GET | `/api/focus` | Current focus session status |
| POST | `/api/focus/start` | Start focus session via API |
| POST | `/api/focus/stop` | Stop focus session via API |

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
| GET | `/api/ambient/status` | Aggregated ambient status (schedule, focus, tasks, LED color) |

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

**Photo flow:** upload → Qwen2.5-VL strict-JSON prompt → return estimate → user confirms in UI before save (or pass `auto_log=true` in POST body to skip confirmation). Extension allowlist: `jpg`, `jpeg`, `png`, `gif`, `webp`. Files saved as uuid4 names under `MEAL_PHOTOS_DIR`.

### Claude Code Integration

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/claude_code/turn` | Stop hook target — logs a completed Claude Code turn to the 7-day rolling buffer |
| GET | `/api/claude_code/recent?minutes=120&limit=20` | List recent Claude Code turns for dashboards or the `check_claude_activity` tool |

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
