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

## Embedding model

All palace vectors come from `nomic-ai/nomic-embed-text-v2-moe` (`EMBEDDING_MODEL`), loaded once as `shared.embedding_model` and also by `scripts/reindex_rag.py` and `rag/ingest_rag.py`. Every memory feature — `search_memory`, per-turn RAG context, palace writes (`update_memory`, document vault), auto_learn — depends on it.

**Pins (all three must move together):**

| Pin | Where | Why |
|---|---|---|
| `transformers>=5.0,<5.12` | `orchestrator/requirements.txt` | The model's remote code calls `get_extended_attention_mask`, removed in transformers 5.12 |
| Model revision `1066b6599d099fbb93dfcb64f9c37a7c9e503e85` | `orchestrator/embedding_pin.py` (`revision`) | Reproducible weights/config |
| Remote-code revision `7710840340a098cfb869c4f65e87cf2b1b70caca` (repo `nomic-ai/nomic-bert-2048`) | `orchestrator/embedding_pin.py` (`code_revision` via `config_kwargs`/`model_kwargs`) | The config's `auto_map` points at code in a second repo; `revision` alone does not pin it — unpinned, every start executed that repo's `main` |

`embedding_pin.load_kwargs(name)` applies the pins only when `EMBEDDING_MODEL` is the default; a custom model loads unpinned.

**Upgrading:** test the new SHAs (and any transformers bump) in a throwaway container — `encode()` must succeed and the log must not show "A new version of the following files was downloaded" — then change them together. Procedure also in the `embedding_pin.py` docstring.

**Health:** startup self-test encodes one sentence (60 s timeout) → gauge `bgw_embedding_healthy` (1/0, log line `[EMBEDDING] Self-test OK|FAILED`). Paging alert `EmbeddingModelBroken` fires on `== 0` for 5 m (boot-only signal).

**Runtime errors:** `shared.embedding_model` is wrapped in a pass-through `shared._CountingEmbedder`; every `encode()` failure increments `bgw_embedding_errors_total{caller}` (caller = calling module; known callers pre-initialised at 0) and re-raises. Alert `EmbeddingErrors`: `sum(increase(...[1h])) >= 3` for 10 m, `keep_firing_for: 2h` (doesn't resolve overnight just because memory is idle), quiet `pushover-quiet`. Inhibited by `EmbeddingModelBroken`. Grafana Memory row: "Embedding Model Healthy" + "Embedding Errors by Caller".

**Outage 2026-10-02 → 2026-10-15:** transformers was unpinned; a rebuild pulled 5.18 and every encode failed with `'NomicBertModel' object has no attribute 'get_extended_attention_mask'`. Memory search, RAG context, palace writes and auto_learn all failed (auto_learn stored 0 facts) for ~12 days with no alert. Explicit "remember this" requests in that window were lost and cannot be recovered. The pinned load produces identical vectors to the pre-outage model, so existing memories remain compatible (no re-index needed).

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
claude mcp add mempalace -- python3 /opt/gateway_mvp/scripts/mempalace_mcp_server.py

# Environment: ORCHESTRATOR_URL (default http://localhost:8888), API_TOKEN
```

**MCP tools exposed:** `palace_search`, `palace_store`, `palace_list_wings`, `palace_list_rooms`, `palace_get_memory`, `palace_mine_sessions`.

## Session mining

The palace can mine Claude Code session logs (the JSONL files in `~/.claude/projects/-opt-helios-gateway-mvp/`) for notable decisions, bug fixes, architectural choices, and user preferences. Mined insights are stored with `source="claude_code_mine"` metadata so they're distinguishable from other memory sources.

Run manually via `POST /api/palace/mine` or the MCP tool `palace_mine_sessions`. Not currently on a scheduler — it's on-demand because mining is expensive (reads many sessions, calls the LLM for each). Single-flight: a second request while a run is in progress gets 409. Each model call has a 120 s timeout (it queues behind chat on the single llama slot); a failed call counts in the returned `errors` and in `bgw_background_llm_errors_total{caller="session_miner"}`.

## Auto-learn

Background fact extraction from chats (`orchestrator/auto_learn.py`; scheduling in `cloud_brain._schedule_auto_learn` / `_run_auto_learn_job`). Writes via `store_fact` (see Write paths).

| Aspect | Behavior |
|---|---|
| Granularity | ONE extraction per conversation: APScheduler date job `auto_learn_<16hex>`, re-armed (`replace_existing`) every turn, fires `AUTO_LEARN_DELAY_MINUTES` after the last turn |
| Key (with `conversation_id`) | Request field on `/v1/chat/completions` (dashboard `dashboard:<uuid>`, Telegram `telegram:<chat_id>:<start ts>`). Rolling windows (Telegram sends the last N messages) are merged onto the cached history by overlap, capped at 200 messages; no overlap (edited history) → replace |
| Key (fallback) | Clients without an id (HA Assist): first user message (`[Image N Analysis]` text stripped) + date. A turn that doesn't prefix-continue the cached conversation flushes the cached one to its own job |
| Streaming | Schedules the client transcript, not the loop-mutated message list |
| Cache | In-RAM, 20 conversations; overflow evicts (counted) |
| Model asleep | Model unreachable at fire time → re-queue every 30 min, up to 16 attempts (~7.5 h), then drop |
| Budget | 12000 chars: assistant replies shortened to 300 chars first, then the most recent text kept |
| Re-arm | New turns during a running extraction re-cache the conversation, but APScheduler skips the re-armed job if it comes due mid-run (one instance per id). `_rearm_if_orphaned` (after every run) schedules a fresh job when the conversation is still cached and has none |

**Metrics:** `bgw_auto_learn_extractions_total` (per conversation, not per turn), `bgw_auto_learn_dropped_total{reason=evicted|shutdown|model_unavailable}`, `bgw_auto_learn_deferred_total`. `shutdown` = pending conversations lost on restart/deploy; the count is persisted in `app_state` key `auto_learn_dropped_at_shutdown` and added to the counter at next startup. `bgw_auto_learn_runs_total{outcome=stored|duplicates_only|no_facts|opt_out|error}` (pre-initialised), `bgw_auto_learn_errors_total{stage=llm|empty|parse|dedup|store|pipeline}` (`empty` = model returned nothing — a hiccup, kept separate), `bgw_auto_learn_rearmed_total`. Grafana "Auto-Learn" row → "Extractions (per conversation) + Facts Stored", "Auto-Learn Runs by Outcome", "Auto-Learn Errors by Stage + Re-arms".

**Alert `AutoLearnFailing`** (quiet `pushover-quiet`): ≥3 non-`empty` errors in 24 h AND no successful run (`stored|duplicates_only|no_facts`) in 24 h, for 30 m. Inhibited by `EmbeddingModelBroken` and `EmbeddingErrors` (dedup + store need embeddings).

**Known limits:** pending conversations are RAM-only (lost on restart, counted as `shutdown`); a fallback-keyed conversation crossing midnight, or a conversation resumed after its job ran, is extracted again (fact dedup absorbs it).

## Troubleshooting

**Search returns nothing:**
- Check `PALACE_ENABLED=true`
- Check `bgw_embedding_healthy` — if 0, the embedding model can't encode (see Embedding model above; `docker logs brain-orchestrator | grep EMBEDDING`).
- Check `bgw_palace_memories_total` gauge — if 0, the collection is empty. Run `/api/rag/ingest` to force ingestion, or check that auto_learn is firing (look at `bgw_auto_learn_facts_stored_total`).

**Write volume looks wrong:**
- `bgw_palace_stores_total` should match the sum of `bgw_auto_learn_facts_stored_total` + rag_ingest chunk count + manual palace stores. If it doesn't, one of the writer paths has lost its metric increment (this happened before commit `e5e1d07` — rag_ingest and auto_learn were writing directly to chroma and bypassing the counter).

**Session mining wants to mine the same sessions twice:**
- Session miner dedupes via doc_id fingerprints in metadata, but if you re-run it with different prompts or the LLM extracts different facts, you can get near-duplicates. `PALACE_DEDUP_THRESHOLD` (cosine similarity) gates this. Default `0.85`.
