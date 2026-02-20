# Brain Gateway

Personal AI assistant optimized for ADHD support. Voice-first interface with Home Assistant integration, personal RAG memory, and power-optimized cluster architecture.

## Quick Start

```bash
# Configure environment
cp .env.example .env
vim .env  # Set HA_TOKEN, LITELLM_MASTER_KEY

# Start services
cd /opt/jupiter/gateway_mvp
docker compose up -d

# Check health
curl http://localhost:8888/health

# Access UI
open http://localhost
```

## Architecture

```
┌─ ALWAYS-ON ──────────────────────────────────────────────┐
│  Jupiter → Orchestrator → Nemotron-8B (Saturn)           │
│                │                                          │
│                ├─► ChromaDB (RAG)                        │
│                ├─► Home Assistant                         │
│                └─► TTS/STT (Uranus)                       │
└──────────────────────────────────────────────────────────┘
┌─ ON-DEMAND (saves ~150W) ────────────────────────────────┐
│  Helios (120B) ◄── Auto-starts when needed               │
└──────────────────────────────────────────────────────────┘
```

**Hybrid Flow (v6):** Helios handles conversation naturally. For actions, delegates to Nemotron via `ask_orchestrator` tool.

## Cluster

| Node | IP | GPU | Role |
|------|-----|-----|------|
| Jupiter | 10.0.0.248 | - | Gateway, Docker |
| Saturn | 10.0.0.58 | RTX 3090 | Nemotron-8B brain |
| Uranus | 10.0.0.173 | 2x RTX 5080 | TTS + STT |
| Helios | 10.0.0.195 | RTX 5090 | 120B expert |

## Features

- **Home Assistant:** Natural language control of lights, switches, climate, scenes
- **RAG Memory:** Personal knowledge base from markdown files
- **Voice:** Jessica McCabe voice clone (TTS) + Whisper (STT)
- **Reminders:** Voice + mobile notifications via HA
- **Web Search:** Real-time info via SearXNG (weather, news, events)
- **Data Management:** Natural language updates to meds/projects
- **Focus Timer:** Pomodoro with Endel ambient audio and Pi-hole site blocking

## Configuration

All settings in `.env` (see `.env.example` for full list):

| Variable | Purpose |
|----------|---------|
| HA_TOKEN | Home Assistant long-lived token |
| LITELLM_MASTER_KEY | API authentication |
| NODE_*_IP | Cluster node addresses |
| RAG_BASE | Personal documents path |

## Commands

```bash
# Rebuild after code changes
docker compose up -d --build orchestrator

# View logs
docker logs brain-orchestrator --tail 50 -f

# Reindex RAG
cd rag && python ingest_rag.py \
  --source ~/rag/nadim_rag \
  --persist ~/.local/share/chroma/personal_rag \
  --collection nadim_rag

# Start monitoring
cd monitoring && docker compose --env-file ../.env -p monitoring up -d
```

## Documentation

| File | Content |
|------|---------|
| ARCHITECTURE.md | Internals, data flow |
| COMMANDS.md | Command reference |
| TECHNICAL_REFERENCE.md | API specs |
| monitoring/README.md | Monitoring setup |

## API

| Endpoint | Purpose |
|----------|---------|
| GET /health | Status check |
| POST /v1/chat/completions | OpenAI-compatible chat |
| GET /api/ha/entities | List HA devices |
| POST /api/ha/command | Direct HA control |

## License

Private project - Nadim Nabi
