# Architecture

Deep dive into Brain Gateway internals. See `CLAUDE.md` for quick reference.

## Agentic Loop

```
User Request → Orchestrator
                  │
           Mode Router
      (intent + intensity)
                  │
    ┌─────────────┴─────────────┐
    │       AGENTIC LOOP        │
    │  1. Send to Helios        │
    │     (mode-adapted prompt) │
    │  2. Parse tool calls      │
    │  3. Execute tools         │
    │  4. Feed results back     │
    │  5. Loop or return        │
    │       (max 5 rounds)      │
    └───────────────────────────┘
                  │
            Final Response
```

## Key Files

### orchestrator/orchestrator.py (~2500 lines)

**Configuration:**
- `MODEL_URL`, `FALLBACK_MODEL_URL` - LLM endpoints
- `MAX_TOOL_ROUNDS = 5` - prevent infinite loops

**Core Functions:**

| Function | Purpose |
|----------|---------|
| `call_model()` | Generic LLM caller, adds `tool_choice: "none"` for vLLM |
| `_run_nemotron_tool_loop()` | Shared agentic loop (dedup, tool exec, force-final) |
| `call_nemotron_orchestrator()` | Thin wrapper: builds messages, calls loop |
| `_nemotron_fallback()` | Fallback wrapper: calls loop, returns HTTP response |
| `parse_tool_calls_from_content()` | Extract `<tool_call>` XML from response |
| `execute_tool()` | Route to tool handler |
| `rag_context()` | Query ChromaDB, return formatted chunks |
| `get_mode_router().route()` | Classify intent → mode + intensity (from `mode_router.py`) |

**Tool Handlers:**

| Handler | Action |
|---------|--------|
| `tool_home_assistant()` | → `ha_client.call_service()` |
| `tool_search_memory()` | → `rag_context()` |
| `tool_ask_expert()` | → `expert_agent.handle_ask_expert()` — one-shot blocking call to Qwen3-32B Thinking on Saturn 3090 (port 8084). Auto-disabled if `EXPERT_ENABLED=false` or `EXPERT_MODEL_URL` empty. Circuit breaker after `EXPERT_CIRCUIT_BREAKER_FAILURES` failures. |
| `tool_update_data()` | → `data_manager.handle_update_data()` |
| `tool_set_reminder()` | → APScheduler + TTS + HA notification |
| `tool_cancel_reminder()` | → Remove pending reminder by ID |
| `tool_start_focus()` | → Endel audio + Pi-hole blocking + timer |
| `tool_focus_status()` | → Check remaining focus time |
| `tool_web_search()` | → `web_search.SearXNGClient.search()` |
| `tool_check_calendar()` | → `google_calendar.GoogleCalendarClient.list_events()` |
| `tool_create_calendar_event()` | → `google_calendar.GoogleCalendarClient.create_event()` |
| `tool_check_email()` | → `google_gmail.GmailClient.check_inbox()` |
| `tool_search_email()` | → `google_gmail.GmailClient.search()` |

**Proactive Background Jobs:**

| Job | Trigger | Action |
|-----|---------|--------|
| `poll_calendar()` | interval, every 5 min | TTS announce events within 2 hours (tiered countdown alerts) |
| `morning_briefing()` | cron, 7:00 AM daily | TTS announce today's events + pending reminders (bedroom pair) |
| `poll_email()` | interval, every 30 min | TTS announce new unread emails (Primary inbox only) |

**Why `tool_choice: "none"`?** vLLM lacks `--enable-auto-tool-choice`. Nemotron outputs `<tool_call>` XML in content instead.

### orchestrator/google_calendar.py (~295 lines)

Google Calendar API v3 client. Follows `web_search.py` pattern: dataclasses + client class + singleton.

```python
# Dataclasses
CalendarEvent(id, title, start, end, location, description, all_day)
CalendarResponse(success, events, error)

# Client methods
GoogleCalendarClient.list_events(days_ahead=7) → CalendarResponse
GoogleCalendarClient.create_event(title, start_time, duration_minutes=60, ...) → CalendarResponse
GoogleCalendarClient.get_upcoming(hours_ahead=2) → CalendarResponse  # for proactive polling

# Singleton
get_calendar_client(http_client=_http) → GoogleCalendarClient
```

### orchestrator/mode_router.py (~150 lines)

Deterministic intent classifier. Adapts Jess's personality per-request.

```python
# Routing flow:
# 1. Explicit intent overrides (phrases → mirror or challenge)
# 2. Emotional intensity classification (high/medium/low keywords)
# 3. Curiosity detection (mechanism language + low intensity → explainer)
# 4. Default: high→baseline, medium→counterbalance, low→explainer

RoutingResult(mode, intensity, tags)  # returned to orchestrator
MODE_PROMPTS[mode]                     # injected into system prompt
TONE_CONSTRAINT                        # always injected (no default grounding)
```

### orchestrator/google_auth.py (~75 lines)

OAuth2 token management. Loads token from file, auto-refreshes if expired, returns None if not configured.

### orchestrator/ha_integration.py (~820 lines)

**Key Method:** `call_service(entity_id, service, data)` - Direct HA API relay.

```python
# Nemotron outputs structured calls:
{"entity_id": "light.bedroom", "service": "turn_on", "data": {"brightness": 128}}
# → ha_client.call_service() → HA REST API
```

Legacy NLP parsing exists but is unused.

### orchestrator/data_manager.py (~560 lines)

YAML-based data for meds/projects. Changes auto-regenerate markdown for RAG.

```
YAML (source) → Markdown (for RAG) → ChromaDB (via watch_and_ingest.py)
```

## Data Flow Examples

### "Turn on bedroom lights to blue at 50%"

```
1. User → Orchestrator → Helios
2. Helios: <tool_call>{"name":"ask_orchestrator","arguments":{"command":"turn on bedroom lights blue 50%"}}</tool_call>
3. Orchestrator → Nemotron with command
4. Nemotron: <tool_call>{"name":"home_assistant","arguments":{"entity_id":"light.bedroom","service":"turn_on","data":{"brightness":128,"rgb_color":[0,0,255]}}}</tool_call>
5. Execute → HA API → "✓ Set Bedroom to blue at 50%"
6. Result → Nemotron → natural response → Helios → User
```

### "What's on my calendar this week?"

```
1. User → Orchestrator → Helios
2. Helios: <tool_call>{"name":"ask_orchestrator","arguments":{"command":"check calendar this week"}}</tool_call>
3. Orchestrator → Nemotron with command
4. Nemotron: <tool_call>{"name":"check_calendar","arguments":{"days_ahead":7}}</tool_call>
5. Execute → Google Calendar API → list of events
6. Result → Nemotron → natural summary → Helios → User
```

### "Add Adderall 20mg to morning meds"

```
1. Helios → ask_orchestrator → Nemotron
2. Nemotron: <tool_call>{"name":"update_data","arguments":{"action":"add_medication","name":"Adderall","dose":"20mg","schedule":"morning"}}</tool_call>
3. data_manager updates YAML + regenerates markdown
4. watch_and_ingest.py auto-reindexes ChromaDB
```

### Dashboard calendar (merged sources)

```
1. iPhone Shortcut → POST /api/calendar/sync (every few hours)
   - Sends Outlook + Google + iCloud events
   - Persisted to /app/data/phone_calendar.json
2. GET /api/calendar/today
   - If phone data fresh (<24h) → use phone events (all calendars)
   - Else → fallback to Google Calendar API
   - Dedup by title + start time
   - Returns {events, source: "phone"|"google", count}
```

### Proactive calendar alert (background)

```
1. APScheduler triggers poll_calendar() every 5 min
2. GoogleCalendarClient.get_upcoming(hours_ahead=2)
3. Tiered alerts: picks closest un-announced tier (60/30/15/5 min),
   auto-marks larger tiers as notified on catch-up
4. TTS via _announce_voice() → HA media_player (with speaker fallback)
```

### TTS announcement routing

```
_announce_voice(text, speaker=None)
  - speaker param overrides default (REMINDER_SPEAKER), may be comma-separated
    or the literal "all" (alias for REMINDER_SPEAKER list)
  - Morning briefing → bedroom pair (MORNING_BRIEFING_SPEAKER)
  - /api/announce → optional speaker in request body
  - Broadcasts to every entity in the list; succeeds if at least one works
```

## ChromaDB / RAG

- **Collection:** `nadim_rag`
- **Embedding:** `sentence-transformers/all-MiniLM-L6-v2`
- **Params:** `TOP_K=25`, `MIN_COS=0.20`

## Voice Pipeline

```
"Hey Jess" (on-device wake word, ATOM Echo S3R)
    → ESPHome voice_assistant → Home Assistant
    → Wyoming Whisper STT (Jupiter :10300)
    → HA Conversation Agent → Orchestrator :8888
    → Wyoming Jessica TTS bridge (:10301) → Helios TTS :8002
    → Speaker output
```

## TTS / STT (Helios, GPU1 RTX PRO 5000 Blackwell)

Both TTS and STT run on Helios as systemd services, pinned to GPU1 alongside the primary LLM (Qwen3.5-27B). TTS pins via `QWEN_TTS_DEVICE=cuda:1`; Parakeet STT uses `CUDA_VISIBLE_DEVICES=1` + `PARAKEET_DEVICE=cuda:0` (hides GPU0 to keep NeMo from OOM'ing against the code agent).

```
qwen-tts     (port 8002) - Jessica voice clone (Qwen3-TTS-1.7B-Base)
parakeet-stt (port 8003) - OpenAI-compatible (Parakeet TDT v3, since 2026-04-26)
```

The Wyoming bridge (`wyoming-faster-whisper`, port 10300) used by the HA voice pipeline is a separate process and was not part of the STT engine swap.

**TTS pacing pipeline:**
```
Open WebUI → split on paragraph (\n\n) → TTS server
                                           │
                                    inject_sentence_pauses()
                                    (regex: [.!?] → [.!?] ...)
                                           │
                                    Qwen3-TTS → audio
```

The sentence pause injection (`/home/labadmin/server.py` on Helios) inserts `...` between sentences before Qwen3-TTS processes the text. This produces calmer, more natural speech. Service: `qwen-tts` (systemd).

## HTTPS (Tailscale Serve)

```
Phone/Browser → https://helios.tail74fc4a.ts.net (port 443)
                    │
              Tailscale Serve (TLS termination, auto-cert)
                    │
              Open WebUI (port 80)
```

Required for browser microphone access (Web Audio API mandates HTTPS). No nginx or reverse proxy container needed — Tailscale handles cert issuance and renewal automatically.

## Monitoring (Jupiter)

```
Grafana ← Prometheus ← node_exporter (all nodes)
              ↑              gpu_exporter (GPU nodes)
          Loki ← Promtail (Docker logs)
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| 400 from Nemotron | Check `tool_choice: "none"` in call_model() |
| Tool loops forever | Check `<tool_response>` wrapper + "Do NOT call more tools" |
| HA commands fail | `curl localhost:8888/api/ha/entities`, check logs |
| RAG empty | `curl localhost:8888/health`, re-run ingest_rag.py |
| Helios offline | Auto-starts on demand, or `./scripts/start-helios.sh` |
| Calendar not configured | Check `/health` → `calendar.configured`, re-run google_setup.py |
| Calendar token expired | Token auto-refreshes. If fails, re-run google_setup.py and copy to Jupiter |
| Morning briefing not firing | Check `MORNING_BRIEFING_ENABLED=true`, verify scheduler has 2+ jobs |
| Morning briefing wrong speaker | Check `MORNING_BRIEFING_SPEAKER` env var in docker-compose.yml |
| Phone calendar not showing | Check phone sync age (<24h), iOS date format parsing in logs |
| Email announces promotions | Ensure `-category:updates` in email polling queries (background_jobs.py) |
| Frontend changes not deploying | Must `docker compose up -d --build --force-recreate frontend` (not just restart) |
