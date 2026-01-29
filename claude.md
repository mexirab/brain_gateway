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
                              ┌────────────────────┼────────────────────┐
                              ▼                    ▼                    ▼
                        home_assistant      search_memory        set_reminder
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
docker-compose -p brain up -d
docker-compose -p brain up -d --build orchestrator

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
| docker-compose.yml | Service stack |
| .env | Environment config (from .env.example) |
| litellm-config.yaml | LLM proxy config |

## Detailed Docs

- **ARCHITECTURE.md** - Internals, data flow, troubleshooting
- **COMMANDS.md** - Command quick reference
- **TECHNICAL_REFERENCE.md** - API specs, schemas
- **monitoring/README.md** - Monitoring setup

## Notes

- Owner: Nadim (ADHD - prefers step-by-step with verification)
- Docker project: always use `-p brain`
- Helios auto-starts when needed, stops to save ~150W
- TTS uses Jessica McCabe voice clone (Qwen3-TTS)
