# Brain Gateway

Personal AI assistant optimized for ADHD support. Voice-first interface with Home Assistant integration, personal RAG memory, and power-optimized cluster architecture.

## Quick Start

```bash
# Configure environment
cp .env.example .env
vim .env  # Set HA_TOKEN, API_TOKEN, PIHOLE_PASSWORD, GF_SECURITY_ADMIN_PASSWORD

# Start services
cd /opt/gateway_mvp
docker compose up -d

# Check health
curl http://localhost:8888/health

# Access UI
open http://localhost
```

## Architecture

```
┌─ HELIOS (always-on) ─────────────────────────────────────┐
│  Orchestrator → Qwen3.5-27B (unified loop)                │
│        │                                                  │
│        ├─► ChromaDB (RAG)                                 │
│        ├─► Home Assistant                                 │
│        ├─► TTS (Qwen3-TTS) / STT (Whisper)                │
│        ├─► Code agent (Qwen3-Coder-Next 80B/3B MoE, GPU0) │
│        ├─► Expert reasoner (Saturn 3090, Qwen3-32B :8084) │
│        └─► Vision model (Saturn 3080, Qwen3-VL-8B :8010)  │
└──────────────────────────────────────────────────────────┘
```

**v7 Unified Flow:** Single primary model (Qwen3.5-27B on Helios RTX PRO 5000) handles conversation and tool execution in one agentic loop. See `ARCHITECTURE.md`.

## Cluster

| Node | IP | GPU | Role |
|------|-----|-----|------|
| Helios | 10.0.0.195 | RTX 5090 + RTX PRO 5000 | Brain gateway, Docker host, Primary LLM (Qwen3.5-27B on PRO 5000), TTS + STT (PRO 5000), Code agent (Qwen3-Coder-Next 80B/3B MoE on RTX 5090 + RAM-spilled experts) |
| Jupiter | 10.0.0.248 | - | Pi-hole primary, monitoring stack (Prometheus, Grafana, Loki, Blackbox), nebula-sync, Conjure API |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | Vision model (Qwen3-VL-8B-Instruct on 3080), Expert reasoning model (Qwen3-32B Q4_K_M on 3090, port 8084), Pi-hole secondary |
| Uranus | 10.0.0.173 | 2x RTX 5080 | Currently offline (hardware removed 2026-04-26 for troubleshooting) |
| HA | 10.0.0.106 | - | Home Assistant |

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
| API_TOKEN | Orchestrator API authentication |
| PIHOLE_PASSWORD | Pi-hole v6 web admin + API password |
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
