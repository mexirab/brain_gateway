# Developer guide

This is the contributor / hacker doc. End users installing Brain Gateway should read [`README.md`](../README.md). AI coding assistants should read [`CLAUDE.md`](../CLAUDE.md) for the codebase tour + post-change pipeline.

---

## Architecture in 30 seconds

```
User → Open WebUI (or any OpenAI client) → Orchestrator → Unified agentic loop
                                                                │
                                                                ├─► Primary LLM (vLLM, Qwen3.6-27B INT4)
                                                                ├─► ChromaDB (mempalace memory)
                                                                ├─► Home Assistant API
                                                                ├─► TTS (Qwen3-TTS) / STT (Parakeet TDT)
                                                                ├─► SearXNG web search
                                                                └─► Tool registry (decorator-based)
```

**Single model, single loop.** No router, no delegation, no v6 hybrid. The primary handles conversation *and* tool execution in one OpenAI-style function-calling loop. See `ARCHITECTURE.md` for the full data flow.

---

## Repo layout

| Path | What's there |
|------|--------------|
| `orchestrator/` | FastAPI app, unified loop, tool handlers, all the brain logic |
| `frontend/` | Next.js 14 dashboard (App Router); served on port 3001 |
| `tts/` | Qwen3-TTS server + Parakeet STT server + Wyoming bridges |
| `scripts/` | One-shot helpers: `detect_hardware.sh`, `model_layer_smoketest.sh`, `setup.sh`, `reindex_rag.py` |
| `monitoring/` | Prometheus + Grafana + Loki stack (docker compose, advanced profile) |
| `docs/` | User-facing docs (install, hardware, env vars, feature guides) |
| `docs/internal/` | Maintainer's Helios runbook + historical migration plans |
| `jess-features/` | ADHD feature specs (F-001 → F-014), one per file |
| `rag/` | RAG ingest script |
| `monitoring/grafana/dashgen/` | Python generator for the Grafana dashboards (don't hand-edit the JSON) |

The full file map is in [`CLAUDE.md`](../CLAUDE.md) → **Key Files**.

---

## Local development

```bash
# Rebuild + restart the orchestrator after a Python change
docker compose up -d --build orchestrator

# Tail orchestrator logs
docker logs brain-orchestrator --tail 50 -f

# Run the test suite (tests live in orchestrator/tests/, pytest runs inside the container)
docker exec brain-orchestrator pip install pytest pytest-asyncio -q
docker cp orchestrator/tests brain-orchestrator:/app/tests
docker exec brain-orchestrator python -m pytest tests/ -v

# Rebuild the frontend
docker compose up -d --build --force-recreate frontend

# Validate compose stanzas without starting anything
docker compose --profile models config
```

**Post-change pipeline (mandatory).** Every code change runs through a two-phase review pipeline (code-reviewer + security + prod-support + frontend/hacker + unit-test + docs-updater). Full spec in [`CLAUDE.md`](../CLAUDE.md) → **Post-change review workflow**.

---

## Reference cluster

The maintainer's deployment is a 4-node cluster centered on Helios. Brain Gateway itself runs as a single-box appliance — the cluster shape is **not** required for end users.

| Node | IP | GPU | Role |
|------|-----|-----|------|
| Helios | 10.0.0.195 | RTX 5090 + RTX PRO 5000 | Brain gateway + Docker host; primary LLM, TTS, STT, code agent |
| Jupiter | 10.0.0.248 | — | Pi-hole primary + monitoring stack (Prometheus, Grafana, Loki) |
| Saturn | 10.0.0.58 | RTX 3080 + RTX 3090 | Vision model (3080), expert reasoner Qwen3-32B (3090), Pi-hole secondary |
| Uranus | 10.0.0.173 | 2× RTX 5080 | Test box for Phase 1 model-layer boot test |

For Helios-specific details (Tailscale cert, GPU layout, kiosk display, etc.), see [`docs/internal/HELIOS_INFRASTRUCTURE.md`](internal/HELIOS_INFRASTRUCTURE.md).

---

## Contributing

1. Read [`CLAUDE.md`](../CLAUDE.md) end-to-end — it's the briefing.
2. Pick something from [`ROADMAP.md`](../ROADMAP.md) or [`jess-features/`](../jess-features/).
3. Open a branch, make the change, run the post-change pipeline.
4. Open a PR. Small, single-purpose PRs land fastest.

Bug reports and feature requests: file an issue with reproduction steps and the relevant `brain-orchestrator` logs.

---

## Where the user-facing docs live

| File | Audience |
|------|----------|
| [`README.md`](../README.md) | End user installing Brain Gateway |
| [`docs/INSTALL.md`](INSTALL.md) | Detailed install procedure |
| [`docs/HARDWARE.md`](HARDWARE.md) | GPU/VRAM tier matrix |
| [`docs/UPGRADE.md`](UPGRADE.md) | Release-to-release upgrade path |
| [`docs/JESS_QUICK_START.md`](JESS_QUICK_START.md) | Voice command reference |
| [`docs/ENV_VARS.md`](ENV_VARS.md) | Every environment variable |
| [`CHANGELOG.md`](../CHANGELOG.md) | Release notes |
