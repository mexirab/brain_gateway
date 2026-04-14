# MemPalace — Unified Memory System

MemPalace is the single source of truth for everything Jess remembers. All memory — RAG document chunks, auto-learned facts, user corrections, document vault entries — lives in one `mempalace` ChromaDB collection with wing/room metadata for structured organization.

The `search_memory` tool supports optional wing/room filtering. Source files in `~/rag/nadim_rag/` are auto-ingested every 2 minutes by the in-process `rag_ingest_watch` scheduler job (see `orchestrator/rag_ingest.py`). For on-demand ingestion: `POST /api/rag/ingest`.

## History

The legacy `nadim_rag` collection (pre-MemPalace, flat structure) was deleted on 2026-04-13 after verifying that mempalace had fully absorbed it. See git log `35ab5d6` ("Clean up legacy nadim_rag collection") if you need historical context.

## Write paths

Five code paths write into the mempalace collection. Not all of them go through `MemPalace.store()` — two write directly to `shared.collection.add` for HNSW-staleness reasons:

| Writer | Path | Metric fires? |
|---|---|---|
| `routes_palace.py::store` (manual REST POST) | `palace.store()` | ✅ |
| `session_miner.py::mine_sessions` | `palace.store()` | ✅ |
| `rag_ingest.py::check_and_ingest` (every 2 min) | `shared.collection.add` (HNSW-safe direct write), `PALACE_STORES_TOTAL` incremented manually with `wing="library"` | ✅ (as of commit `e5e1d07`) |
| `auto_learn.py::store_fact` | `shared.collection.add`, `PALACE_STORES_TOTAL` incremented manually with the routed wing/room | ✅ (as of commit `e5e1d07`) |
| Document vault (`document_vault` tool) | `palace.store()` | ✅ |

## Configuration

Palace env vars live in [`ENV_VARS.md`](ENV_VARS.md#mempalace-unified-memory). Palace structure itself (wings, rooms, routing rules) lives in `data/palace.yaml`.

## API endpoints

See [`TECHNICAL_REFERENCE.md`](../TECHNICAL_REFERENCE.md#memory-rag--mempalace--auto-learn) for the full `/api/palace/*` and `/api/memory/*` endpoint reference.

## MCP server (Claude Code integration)

The palace is exposed as an MCP server so Claude Code can read and write memories directly — useful when you want Jess's memory and Claude Code's development context to share the same knowledge base.

```bash
# Install MCP dependencies
pip install -r scripts/requirements-mcp.txt

# Register with Claude Code
claude mcp add mempalace -- python3 /opt/helios/gateway_mvp/scripts/mempalace_mcp_server.py

# Environment: ORCHESTRATOR_URL (default http://localhost:8888), API_TOKEN
```

**MCP tools exposed:** `palace_search`, `palace_store`, `palace_list_wings`, `palace_list_rooms`, `palace_get_memory`, `palace_mine_sessions`.

## Session mining

The palace can mine Claude Code session logs (the JSONL files in `~/.claude/projects/-opt-helios-gateway-mvp/`) for notable decisions, bug fixes, architectural choices, and user preferences. Mined insights are stored with `source="claude_code_mine"` metadata so they're distinguishable from other memory sources.

Run manually via `POST /api/palace/mine` or the MCP tool `palace_mine_sessions`. Not currently on a scheduler — it's on-demand because mining is expensive (reads many sessions, calls the LLM for each).

## Troubleshooting

**Search returns nothing:**
- Check `PALACE_ENABLED=true`
- Check `bgw_palace_memories_total` gauge — if 0, the collection is empty. Run `/api/rag/ingest` to force ingestion, or check that auto_learn is firing (look at `bgw_auto_learn_facts_stored_total`).

**Write volume looks wrong:**
- `bgw_palace_stores_total` should match the sum of `bgw_auto_learn_facts_stored_total` + rag_ingest chunk count + manual palace stores. If it doesn't, one of the writer paths has lost its metric increment (this happened before commit `e5e1d07` — rag_ingest and auto_learn were writing directly to chroma and bypassing the counter).

**Session mining wants to mine the same sessions twice:**
- Session miner dedupes via doc_id fingerprints in metadata, but if you re-run it with different prompts or the LLM extracts different facts, you can get near-duplicates. `PALACE_DEDUP_THRESHOLD` (cosine similarity) gates this. Default `0.85`.
