# Brain Gateway

Personal AI assistant for ADHD support. Nemotron-8B orchestrates tools; Helios (Qwen3-32B) handles conversation.

## Cluster

| Node | IP (LAN) | IP (Tailscale) | GPU | Role |
|------|----------|----------------|-----|------|
| Jupiter | 10.0.0.248 | 100.102.29.14 | - | Gateway, Docker host |
| Saturn | 10.0.0.58 | - | RTX 3080 + RTX 3090 | Nemotron-8B (brain), Pi-hole secondary |
| Uranus | 10.0.0.173 | - | 2x RTX 5080 | TTS (GPU0), STT (GPU1) |
| Helios | 10.0.0.195 | - | RTX 5090 | Qwen3-32B conversational (auto-starts on demand) |
| HA | 10.0.0.106 | - | - | Home Assistant |
| Callisto | 10.0.0.136 | - | - | Monitoring kiosk display (Pi 4) |

## Services

| Service | Port | URL |
|---------|------|-----|
| Open WebUI (HTTPS) | 443 | https://jupiter-amds.tail74fc4a.ts.net (Tailscale) |
| Open WebUI (HTTP) | 80 | http://localhost |
| Orchestrator | 8888 | http://localhost:8888 |
| Nemotron | 8001 | http://10.0.0.58:8001/v1 |
| Helios | 8080 | http://10.0.0.195:8080/v1 |
| TTS | 8002 | http://10.0.0.173:8002 |
| STT | 8003 | http://10.0.0.173:8003 |
| Pi-hole (Jupiter) | 53/8053 | http://10.0.0.248:8053/admin |
| Pi-hole (Saturn) | 53/8053 | http://10.0.0.58:8053/admin |
| Wyoming Whisper (STT) | 10300 | tcp://10.0.0.248:10300 |
| Wyoming Jessica (TTS) | 10301 | tcp://10.0.0.248:10301 |
| Frontend (dashboard) | 3001 | http://10.0.0.248:3001 (future: convivialprophet.com) |
| SearXNG | 8090 | http://localhost:8090 |
| Grafana | 3000 | http://localhost:3000/d/brain-gateway-overview (admin/braingw) |

## Architecture (v6 Hybrid)

```
User -> Open WebUI -> Orchestrator -> Mode Router -> Helios (conversation)
                                   (intent+intensity)      |
                                                +----------+----------+
                                           Direct response      ask_orchestrator
                                                                      |
                                                               Nemotron (tools)
                                                                      |
                    +----------+----------+----+----+----------+----------+
                    v          v          v    v    v          v          v
              home_assistant  search_memory  set_reminder  web_search  check_calendar
```

**Flow:** Mode router classifies intent + emotional intensity. Helios handles conversation. For actions, calls `ask_orchestrator` -> Nemotron executes tools -> result back to Helios.

## Tools

| Tool | Model | Purpose |
|------|-------|---------|
| ask_orchestrator | Helios | Delegate to Nemotron for actions |
| home_assistant | Nemotron | HA API: `{entity_id, service, data}` |
| search_memory | Nemotron | ChromaDB RAG query |
| set_reminder / cancel_reminder | Nemotron | Voice/phone reminders |
| update_data | Nemotron | Update meds/projects YAML |
| start_focus / stop_focus / focus_status | Nemotron | Pomodoro timer + Endel + Pi-hole |
| web_search | Nemotron | Search the web via SearXNG |
| check_calendar / create_calendar_event | Nemotron | Google Calendar read/write |
| check_email / search_email | Nemotron | Gmail inbox (read-only) |

## Key Files

| File | Purpose |
|------|---------|
| orchestrator/orchestrator.py | FastAPI app, main chat endpoint, startup/shutdown |
| orchestrator/shared.py | Module-level shared state (http client, scheduler, config) |
| orchestrator/tool_definitions.py | Tool JSON schemas (STATIC_TOOLS, HELIOS_TOOLS, HA tool builder) |
| orchestrator/prompt_builder.py | System prompts for Helios/Nemotron, RAG context, helpers |
| orchestrator/helios_manager.py | Helios health check, SSH start/stop, idle tracking |
| orchestrator/nemotron_loop.py | Agentic tool loop, XML parsing, dedup |
| orchestrator/focus_manager.py | Pomodoro timer, Endel audio, Pi-hole blocking |
| orchestrator/tool_handlers.py | execute_tool dispatcher + all tool_* functions |
| orchestrator/api_routes.py | Secondary REST endpoints (health, metrics, memory, reminders, focus) |
| orchestrator/background_jobs.py | Calendar polling, morning briefing, email polling, temperature alerts |
| orchestrator/ha_integration.py | HA entity discovery + call_service() |
| orchestrator/mode_router.py | Intent-based mode router |
| orchestrator/google_calendar.py | Google Calendar API v3 client |
| orchestrator/google_gmail.py | Gmail API v1 client (read-only) |
| orchestrator/pihole_client.py | Pi-hole v6 multi-instance client |
| orchestrator/travel_time.py | Google Maps Directions API client |
| orchestrator/metrics.py | Prometheus metrics (bgw_* namespace) |
| docker-compose.yml | Service stack |
| .env | Environment config (from .env.example) |

## Key Paths

```
/opt/jupiter/gateway_mvp/           # Project root on Jupiter
~/.env                              # Secrets (HA_TOKEN, LITELLM_KEY)
~/rag/nadim_rag/                    # RAG source documents (154 docs indexed)
~/.local/share/chroma/personal_rag/ # ChromaDB persistence
/opt/jupiter/gateway_mvp/credentials/  # Google OAuth2 creds (gitignored)
/opt/jupiter/gateway_mvp/certs/     # Tailscale TLS certs (gitignored)
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

# Remote deploy from Mac (via Tailscale)
ssh labadmin@100.102.29.14 "cd /opt/jupiter/gateway_mvp && git pull && docker compose up -d --build orchestrator"

# Frontend rebuild
docker compose up -d --build --force-recreate frontend
```

## Detailed Docs

| Doc | What |
|-----|------|
| **docs/FOCUS_AND_PIHOLE.md** | Focus timer (Pomodoro), Pi-hole DNS blocking, Nebula Sync |
| **docs/VOICE_AND_TTS.md** | ATOM Echo voice assistant, TTS pacing, Wyoming bridges |
| **docs/GOOGLE_INTEGRATIONS.md** | Calendar, Gmail, phone sync, travel-time alerts, OAuth2 setup |
| **docs/FRONTEND.md** | Dashboard pages, widgets, YNAB finance, API proxy pattern |
| **docs/MODE_ROUTER.md** | Intent classification modes (explainer/mirror/counterbalance/challenge/baseline) |
| **docs/INFRASTRUCTURE.md** | HTTPS/Tailscale, RAG, temperature monitoring, performance notes, kiosk |
| **ARCHITECTURE.md** | Internals, data flow, troubleshooting |
| **COMMANDS.md** | Command quick reference |
| **TECHNICAL_REFERENCE.md** | API specs, schemas |
| **ROADMAP.md** | Feature roadmap and what's done/planned |
| **monitoring/README.md** | Monitoring setup |

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_mvp` (default from directory name)
- Helios auto-starts on demand via SSH, auto-stops after 30 min idle (~150W savings)
- TTS uses Jessica McCabe voice clone (Qwen3-TTS) with sentence pause injection
- Jupiter SSH: `labadmin@100.102.29.14` (Tailscale) or `labadmin@10.0.0.248` (LAN)
- Uranus SSH (from Jupiter): `ssh labadmin@10.0.0.173`
