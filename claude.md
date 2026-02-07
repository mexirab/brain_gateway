# Brain Gateway

Personal AI assistant for ADHD support. Nemotron-8B orchestrates tools; Helios-120B handles conversation.

## Cluster

| Node | IP | GPU | Role |
|------|-----|-----|------|
| Jupiter | 10.0.0.248 | - | Gateway, Docker host |
| Saturn | 10.0.0.58 | RTX 3090 | Nemotron-8B (brain) |
| Uranus | 10.0.0.173 | 2x RTX 5080 | TTS (GPU0), STT (GPU1) |
| Helios | 10.0.0.195 | RTX 5090 | 120B expert (off by default) |
| HA | 10.0.0.106 | - | Home Assistant |

## Services

| Service | Port | URL |
|---------|------|-----|
| Orchestrator | 8888 | http://localhost:8888 |
| Open WebUI | 80 | http://localhost |
| Nemotron | 8001 | http://10.0.0.58:8001/v1 |
| Helios | 8080 | http://10.0.0.195:8080/v1 |
| TTS | 8002 | http://10.0.0.173:8002 |
| STT | 8003 | http://10.0.0.173:8003 |
| Pi-hole | 53/8053 | http://localhost:8053/admin |
| SearXNG | 8090 | http://localhost:8090 |
| Grafana | 3000 | http://localhost:3000 (admin/braingw) |

## Architecture (v6 Hybrid)

```
User → Open WebUI → Orchestrator → Helios (conversation)
                                      │
                         ┌────────────┴────────────┐
                    Direct response          ask_orchestrator
                                                   │
                                            Nemotron (tools)
                                                   │
                              ┌──────────────┬──────┼──────┬─────────────┐
                              ▼              ▼      ▼      ▼             ▼
                        home_assistant  search_memory  set_reminder  web_search
```

**Flow:** Helios handles conversation naturally. For actions (HA, reminders, RAG search), calls `ask_orchestrator` → Nemotron executes tools → result back to Helios.

## Tools

| Tool | Model | Purpose |
|------|-------|---------|
| ask_orchestrator | Helios | Delegate to Nemotron for actions |
| home_assistant | Nemotron | HA API: `{entity_id, service, data}` |
| search_memory | Nemotron | ChromaDB RAG query |
| set_reminder | Nemotron | Voice/phone reminders |
| update_data | Nemotron | Update meds/projects YAML |
| start_focus | Nemotron | Pomodoro timer + Endel audio + Pi-hole blocking |
| stop_focus | Nemotron | Stop focus timer early |
| focus_status | Nemotron | Check remaining focus time |
| web_search | Nemotron | Search the web via SearXNG (events, news, weather, etc.) |

## Key Paths

```
/opt/jupiter/gateway_mvp/     # Project root
~/.env                        # Secrets (HA_TOKEN, LITELLM_KEY)
~/rag/nadim_rag/             # RAG source documents
~/.local/share/chroma/        # ChromaDB persistence
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
cd monitoring && docker-compose --env-file ../.env -p monitoring up -d
```

## Key Files

| File | Purpose |
|------|---------|
| orchestrator/orchestrator.py | Main FastAPI, hybrid Helios+Nemotron routing |
| orchestrator/ha_integration.py | HA entity discovery + call_service() |
| orchestrator/reminder_manager.py | APScheduler reminders |
| orchestrator/pihole_client.py | Pi-hole v6 API client for focus blocking |
| orchestrator/web_search.py | SearXNG client for web search |
| docker-compose.yml | Service stack |
| .env | Environment config (from .env.example) |
| litellm-config.yaml | LLM proxy config |

## Detailed Docs

- **ARCHITECTURE.md** - Internals, data flow, troubleshooting
- **COMMANDS.md** - Command quick reference
- **TECHNICAL_REFERENCE.md** - API specs, schemas
- **monitoring/README.md** - Monitoring setup

## Focus Timer (Pomodoro)

ADHD-friendly focus timer with ambient audio and site blocking:

| Feature | Status | Notes |
|---------|--------|-------|
| Timer + voice break | Done | `start_focus`, `stop_focus`, `focus_status` tools |
| Endel audio | Done | Streams HLS from Endel Pacific API to Office speaker |
| Pi-hole blocking | Done | 19 focus domains + 72 always-blocked adult domains |

**Usage:**
- `"start focus on coding for 30 minutes"` - starts timer + audio + blocking
- `"start focus without blocking"` - no site blocking
- `"stop focus"` or timer expires → unblocks sites, announces break

## Pi-hole DNS (whole-house)

Pi-hole v6 runs on Jupiter as the network DNS server.

| Item | Value |
|------|-------|
| Admin UI | http://10.0.0.248:8053/admin |
| DNS | 10.0.0.248:53 |
| Upstream | 8.8.8.8, 8.8.4.4 |
| Docker project | `gateway_mvp` (same as all services) |

**Blocking groups:**
- **Default (group 0):** 72 adult domains — always blocked for all clients
- **focus_blocklist (group 1):** 19 distraction domains (reddit, twitter, youtube, etc.) — toggled by `start_focus`/`stop_focus`

**TODO:** Set router DHCP DNS to 10.0.0.248 + add DHCP reservation for Jupiter

## Performance Notes

- Shared `httpx.AsyncClient` (`_http`) reused across all requests — init at startup, closed at shutdown
- HA tool definition cached 300s (`_ha_tool_cache`) — invalidated on entity refresh
- Nemotron agentic loop deduplicated into `_run_nemotron_tool_loop()` — both `call_nemotron_orchestrator()` and `_nemotron_fallback()` call it
- Streaming chunk size: 80 chars (was 20)

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: `gateway_mvp` (default from directory name, no `-p` flag needed)
- Helios auto-starts when needed, stops to save ~150W
- TTS uses Jessica McCabe voice clone (Qwen3-TTS)
