# Technical Reference

API specs and schemas for implementation.

## API Endpoints

### Orchestrator (port 8888)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Status, versions, counts |
| `/v1/chat/completions` | POST | OpenAI-compatible chat |
| `/v1/models` | GET | List models |
| `/api/ha/entities` | GET | List HA entities |
| `/api/ha/command` | POST | Direct HA service call |
| `/api/memory/search` | GET | RAG search |
| `/api/memory/stats` | GET | RAG stats |
| `/api/memory/add` | POST | Add document to RAG (optional `tags` array) |
| `/api/reminder/trigger` | POST | Trigger a reminder |
| `/api/reminders` | GET | List pending reminders |
| `/api/reminder/complete/{id}` | POST | Mark reminder completed |
| `/api/focus` | GET | Current focus session status |
| `/api/focus/start` | POST | Start focus session via API |
| `/api/focus/stop` | POST | Stop focus session via API |
| `/api/audio/{filename}` | GET | Serve audio files (reminders, TTS) |

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
| LITELLM_MASTER_KEY | LiteLLM auth |
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
