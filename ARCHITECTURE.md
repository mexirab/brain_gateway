# Architecture

Deep dive into Brain Gateway internals. See `CLAUDE.md` for quick reference.

## Agentic Loop

```
User Request → Orchestrator
                  │
    ┌─────────────┴─────────────┐
    │       AGENTIC LOOP        │
    │  1. Send to Helios        │
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

### orchestrator/orchestrator.py (~2200 lines)

**Configuration:**
- `NEMOTRON_URL`, `HELIOS_URL` - LLM endpoints
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

**Tool Handlers:**

| Handler | Action |
|---------|--------|
| `tool_home_assistant()` | → `ha_client.call_service()` |
| `tool_search_memory()` | → `rag_context()` |
| `tool_ask_expert()` | → Helios 120B (auto-starts if needed) |
| `tool_update_data()` | → `data_manager.handle_update_data()` |
| `tool_set_reminder()` | → APScheduler + TTS + HA notification |
| `tool_cancel_reminder()` | → Remove pending reminder by ID |
| `tool_start_focus()` | → Endel audio + Pi-hole blocking + timer |
| `tool_focus_status()` | → Check remaining focus time |
| `tool_web_search()` | → `web_search.SearXNGClient.search()` |

**Why `tool_choice: "none"`?** vLLM lacks `--enable-auto-tool-choice`. Nemotron outputs `<tool_call>` XML in content instead.

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

### "Add Adderall 20mg to morning meds"

```
1. Helios → ask_orchestrator → Nemotron
2. Nemotron: <tool_call>{"name":"update_data","arguments":{"action":"add_medication","name":"Adderall","dose":"20mg","schedule":"morning"}}</tool_call>
3. data_manager updates YAML + regenerates markdown
4. watch_and_ingest.py auto-reindexes ChromaDB
```

## ChromaDB / RAG

- **Collection:** `nadim_rag`
- **Embedding:** `sentence-transformers/all-MiniLM-L6-v2`
- **Params:** `TOP_K=25`, `MIN_COS=0.20`

## Voice Pipeline (Uranus)

```
GPU 0: Qwen3-TTS (port 8002) - Jessica voice clone
GPU 1: Whisper STT (port 8003) - OpenAI-compatible
```

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
