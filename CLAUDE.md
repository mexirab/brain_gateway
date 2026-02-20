# Brain Gateway

Personal AI assistant for ADHD support. Nemotron-8B orchestrates tools; Helios-120B handles conversation.

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Jupiter | 10.0.0.248 | 100.102.29.14 | - | Gateway, Docker host |
| Saturn | 10.0.0.58 | - | RTX 3090 | Nemotron-8B (brain), Pi-hole secondary |
| Uranus | 10.0.0.173 | - | 2x RTX 5080 | TTS (GPU0), STT (GPU1) |
| Helios | 10.0.0.195 | - | RTX 5090 | 120B conversational (auto-starts on demand) |
| HA | 10.0.0.106 | - | - | Home Assistant |

## Services

| Service | Port | URL |
|---------|------|-----|
| Orchestrator | 8888 | http://localhost:8888 |
| Open WebUI | 80 | http://localhost |
| Nemotron | 8001 | http://10.0.0.58:8001/v1 |
| Helios | 8080 | http://10.0.0.195:8080/v1 |
| TTS | 8002 | http://10.0.0.173:8002 |
| STT | 8003 | http://10.0.0.173:8003 |
| Pi-hole (Jupiter) | 53/8053 | http://10.0.0.248:8053/admin |
| Pi-hole (Saturn) | 53/8053 | http://10.0.0.58:8053/admin |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.248:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.248:10301 |
| SearXNG | 8090 | http://localhost:8090 |
| Grafana | 3000 | http://localhost:3000 (admin/braingw) |

## Architecture (v6 Hybrid)

```
User → Open WebUI → Orchestrator → Mode Router → Helios (conversation)
                                   (intent+intensity)      │
                                                ┌──────────┴──────────┐
                                           Direct response      ask_orchestrator
                                                                      │
                                                               Nemotron (tools)
                                                                      │
                    ┌──────────┬──────────┬────┼────┬──────────┬──────────┐
                    ▼          ▼          ▼    ▼    ▼          ▼          ▼
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow:** Mode router classifies intent (explainer/mirror/counterbalance/challenge/baseline) + emotional intensity (low/medium/high). System prompt adapts accordingly. Helios handles conversation. For actions, calls `ask_orchestrator` → Nemotron executes tools → result back to Helios.

## Tools

| Tool | Model | Purpose | Status |
|------|-------|---------|--------|
| ask_orchestrator | Helios | Delegate to Nemotron for actions | Working |
| home_assistant | Nemotron | HA API: `{entity_id, service, data}` | Working |
| search_memory | Nemotron | ChromaDB RAG query | Working |
| set_reminder | Nemotron | Voice/phone reminders | Working |
| cancel_reminder | Nemotron | Cancel a pending reminder by ID | Working |
| update_data | Nemotron | Update meds/projects YAML | Working |
| start_focus | Nemotron | Pomodoro timer + Endel audio + Pi-hole blocking | Working |
| stop_focus | Nemotron | Stop focus timer early | Working |
| focus_status | Nemotron | Check remaining focus time | Working |
| web_search | Nemotron | Search the web via SearXNG | Working |
| check_calendar | Nemotron | Check Google Calendar for upcoming events | Working |
| create_calendar_event | Nemotron | Create a new Google Calendar event | Working |

## Key Paths

```
/opt/jupiter/gateway_mvp/     # Project root on Jupiter
~/.env                        # Secrets (HA_TOKEN, LITELLM_KEY)
~/rag/nadim_rag/             # RAG source documents
~/.local/share/chroma/        # ChromaDB persistence
/opt/jupiter/gateway_mvp/credentials/  # Google OAuth2 creds (gitignored)
```

## Common Commands

```bash
# Start/rebuild
docker compose up -d
docker compose up -d --build orchestrator

# Logs
docker logs brain-orchestrator --tail 50 -f

# Health
curl http://localhost:8888/health

# RAG reindex
cd /opt/jupiter/gateway_mvp/rag && python ingest_rag.py \
  --source ~/rag/nadim_rag \
  --persist ~/.local/share/chroma/personal_rag \
  --collection nadim_rag

# Monitoring
cd monitoring && docker compose --env-file ../.env -p monitoring up -d

# Saturn Pi-hole
./saturn/deploy-pihole.sh           # deploy and start
./saturn/deploy-pihole.sh logs      # tail logs
./saturn/deploy-pihole.sh stop      # stop

# Remote deploy from Mac (via Tailscale)
ssh labadmin@100.102.29.14 "cd /opt/jupiter/gateway_mvp && git pull && docker compose up -d --build orchestrator"
```

## Key Files

| File | Purpose |
|------|---------|
| orchestrator/orchestrator.py | Main FastAPI, hybrid Helios+Nemotron routing |
| orchestrator/ha_integration.py | HA entity discovery + call_service() |
| orchestrator/reminder_manager.py | APScheduler reminders |
| orchestrator/pihole_client.py | Pi-hole v6 multi-instance client for focus blocking |
| orchestrator/web_search.py | SearXNG client for web search |
| orchestrator/google_auth.py | Google OAuth2 token management |
| orchestrator/google_calendar.py | Google Calendar API v3 client |
| orchestrator/google_setup.py | One-time OAuth2 consent flow script |
| orchestrator/mode_router.py | Intent-based mode router (explainer/mirror/counterbalance/challenge/baseline) |
| docker-compose.yml | Service stack |
| saturn/docker-compose.pihole.yml | Saturn Pi-hole secondary deployment |
| saturn/deploy-pihole.sh | Deploy/manage Pi-hole on Saturn via SSH |
| .env | Environment config (from .env.example) |
| litellm-config.yaml | LLM proxy config |
| ha_automations/atom_echo.yaml | ESPHome config for ATOM Echo S3R voice satellite |
| ha_automations/hey_jess.tflite | On-device "Hey Jess" wake word model (microWakeWord) |
| ha_automations/hey_jess.json | Wake word JSON manifest for ESPHome |
| tts/wyoming_jessica_bridge.py | Wyoming-to-HTTP bridge for Jessica TTS |
| tts/Dockerfile.wyoming-jessica | Docker image for Wyoming Jessica bridge |

## Detailed Docs

- **ARCHITECTURE.md** - Internals, data flow, troubleshooting
- **COMMANDS.md** - Command quick reference
- **TECHNICAL_REFERENCE.md** - API specs, schemas
- **ROADMAP.md** - Feature roadmap and what's done/planned
- **monitoring/README.md** - Monitoring setup

## Focus Timer (Pomodoro)

ADHD-friendly focus timer with ambient audio and site blocking:

| Feature | Status | Notes |
|---------|--------|-------|
| Timer + voice break | Done | `start_focus`, `stop_focus`, `focus_status` tools |
| Endel audio | Done | Streams HLS from Endel Pacific API to Office speaker |
| Pi-hole blocking | Done | 24 focus domains + 72 always-blocked adult domains |

**Usage:**
- `"start focus on coding for 30 minutes"` - starts timer + audio + blocking
- `"start focus without blocking"` - no site blocking
- `"stop focus"` or timer expires → unblocks sites, announces break

## Pi-hole DNS (whole-house)

Redundant Pi-hole v6 pair synced via Nebula Sync. Jupiter is primary, Saturn is secondary.

| Item | Jupiter (primary) | Saturn (secondary) |
|------|-------------------|-------------------|
| Admin UI | http://10.0.0.248:8053/admin | http://10.0.0.58:8053/admin |
| DNS | 10.0.0.248:53 | 10.0.0.58:53 |
| Upstream | 8.8.8.8, 8.8.4.4 | 8.8.8.8, 8.8.4.4 |
| Docker project | `gateway_mvp` | `pihole` |
| Compose file | `docker-compose.yml` | `saturn/docker-compose.pihole.yml` |

**Nebula Sync:** Runs as a Docker container on Jupiter (`nebula-sync` service). Uses Pi-hole v6 Teleporter API to sync config from Jupiter → Saturn every 15 min. No SSH needed.

**Blocking groups:**
- **Default (group 0):** 72 adult domains — always blocked for all clients
- **focus_blocklist (group 1):** 19 distraction domains (reddit, twitter, youtube, etc.) — toggled by `start_focus`/`stop_focus`

**Focus blocking:** Orchestrator applies focus blocking to both instances concurrently via `PIHOLE_URLS`. If one is down, the other still blocks.

## Voice Assistant (ATOM Echo S3R)

Hands-free "Hey Jess" voice control via M5Stack ATOM Echo S3R (ESP32-S3).

```
"Hey Jess" (on-device microWakeWord)
    → ATOM Echo S3R (ESPHome voice_assistant)
    → Home Assistant voice pipeline
    → Wyoming Whisper STT (Docker on Jupiter :10300)
    → HA Conversation Agent → Brain Gateway :8888
    → Wyoming Jessica TTS bridge (:10301) → Uranus TTS :8002
    → ATOM Echo S3R speaker (or Google speakers group)
```

**Current status:**
- Office ATOM Echo S3R: flashed, online, wake word working
- Voice pipeline: currently routed to Nemotron directly (TODO: route through orchestrator for hybrid LLM)
- TTS output: currently on ATOM Echo tiny speaker (TODO: route to Google speakers group)
- No programmable RGB LED on S3R variant (GPIO35 conflicts with PSRAM)

**Key components:**
- **Wake word:** `hey_jess.tflite` runs on-device (ESP32-S3 only, not original ATOM Echo)
- **Wake word manifest:** `hey_jess.json` served via nginx model-server at http://10.0.0.248:8080/hey_jess.json
- **STT:** `wyoming-faster-whisper` (base-int8 model, CPU on Jupiter)
- **TTS bridge:** `wyoming-jessica-tts` bridges Wyoming protocol → HTTP Jessica TTS on Uranus
- **ESPHome:** `ha_automations/atom_echo.yaml` — multi-room via substitutions

**Multi-room deployment:**
```bash
esphome run atom_echo.yaml -s name atom-echo-office -s friendly_name "Office Jess"
esphome run atom_echo.yaml -s name atom-echo-bedroom -s friendly_name "Bedroom Jess"
```

## Google Calendar Integration

Google Calendar read/write via OAuth2. Tools: `check_calendar`, `create_calendar_event`.

**Status:** Fully deployed and configured on Jupiter. OAuth2 token generated and mounted.

**Setup (one-time on dev machine):**
1. Google Cloud Console → create project → enable Calendar API → create OAuth2 Desktop credentials
2. Add your Google account as a test user (OAuth consent screen → Test users)
3. Download `credentials.json` → `credentials/google_credentials.json`
4. Run consent flow:
   ```bash
   python3 -m venv /tmp/google-auth-venv
   /tmp/google-auth-venv/bin/pip install google-auth google-auth-oauthlib
   /tmp/google-auth-venv/bin/python orchestrator/google_setup.py \
     --credentials credentials/google_credentials.json \
     --token-output credentials/google_token.json
   ```
5. Copy credentials to Jupiter:
   ```bash
   scp credentials/google_credentials.json labadmin@100.102.29.14:/opt/jupiter/gateway_mvp/credentials/
   scp credentials/google_token.json labadmin@100.102.29.14:/opt/jupiter/gateway_mvp/credentials/
   ```
6. Restart orchestrator: `docker compose restart orchestrator`

**Proactive features (APScheduler):**
- Calendar polling: every 15 min, announces events starting within 2 hours via TTS
- Morning briefing: 7:30 AM, announces today's events + pending reminders via TTS

**Config (env vars):**
- `CALENDAR_POLL_INTERVAL` — minutes between polls (default: 15)
- `MORNING_BRIEFING_TIME` — HH:MM 24h format (default: 07:30)
- `MORNING_BRIEFING_ENABLED` — true/false (default: true)

## Mode Router (Personalized Coaching)

Deterministic v1 intent classifier. Adapts Jess's system prompt based on what Nadim needs.

**Modes:**

| Mode | When | Behavior |
|------|------|----------|
| explainer | Curiosity/mechanism questions, default for low intensity | Analytical, structured, no coaching language |
| mirror | "Analyze me", "reflect", "behavioral tendencies" | Pattern identification, ends with one question |
| counterbalance | Medium emotional intensity (lonely, shame, spiral) | Names distortions, small actions, no shame |
| challenge | "Hold me accountable", "push me", "no excuses" | Firm, one specific action, time-bound |
| baseline | High emotional intensity (panic, hopeless, can't breathe) | Low cognitive load, 2-3 options, grounding allowed |

**Global tone constraint:** Never default to grounding techniques unless intensity is high or explicitly requested.

**Routing logged in `_routing`:** `intent_mode`, `intent_intensity`, `intent_tags` — visible in API response for debugging.

## Performance Notes

- Shared `httpx.AsyncClient` (`_http`) reused across all requests — init at startup, closed at shutdown
- HA tool definition cached 300s (`_ha_tool_cache`) — invalidated on entity refresh
- Nemotron agentic loop deduplicated into `_run_nemotron_tool_loop()` — both `call_nemotron_orchestrator()` and `_nemotron_fallback()` call it
- `TERMINAL_TOOLS` set in the loop short-circuits after state-changing tools (start_focus, stop_focus, home_assistant, set_reminder, cancel_reminder, update_data, create_calendar_event) — prevents Nemotron from undoing its own actions in subsequent rounds
- Streaming chunk size: 80 chars (was 20)

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_mvp` (default from directory name, no `-p` flag needed)
- Helios auto-starts on demand via SSH, auto-stops after 30 min idle (~150W savings)
- TTS uses Jessica McCabe voice clone (Qwen3-TTS)
- Jupiter SSH: `labadmin@100.102.29.14` (Tailscale) or `labadmin@10.0.0.248` (LAN)
