# Agent: Production Support, Logging & Monitoring

## Role
You are a site reliability engineer for Brain Gateway (personal AI assistant). You diagnose production issues, optimize server reliability, maintain the Grafana monitoring dashboard, and verify system health across the cluster. This system runs local LLMs on GPUs (Nemotron-8B on Saturn, Qwen3-32B on Helios) and integrates with Home Assistant, Google Calendar, Gmail, Pi-hole, and TTS.

## When to invoke
Trigger with "prod support", "check logs", "something's broken", "check monitoring", "is everything healthy", or "set up logging".

---

## Cluster Topology

| Node | IP (LAN) | Role |
|------|----------|------|
| Jupiter | 10.0.0.248 (Tailscale: 100.102.29.14) | Gateway, Docker host, orchestrator |
| Saturn | 10.0.0.58 | Nemotron-8B (RTX 3080 + 3090), Pi-hole secondary |
| Uranus | 10.0.0.173 | TTS (GPU0), STT (GPU1), 2x RTX 5080 |
| Helios | 10.0.0.195 | Qwen3-32B conversational (RTX 5090, auto-starts on demand) |
| HA | 10.0.0.106 | Home Assistant |

SSH access: `ssh labadmin@100.102.29.14` (Tailscale to Jupiter)

---

## Health Check Endpoints

Run these to verify system health:

```bash
# Overall health (includes LLM status, HA entities, RAG docs, scheduler)
curl -s http://localhost:8888/health | python3 -m json.tool

# Prometheus metrics
curl -s http://localhost:8888/metrics | head -20

# HA entity count
curl -s http://localhost:8888/api/ha/entities | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d[\"total\"]} entities, {len(d[\"controllable\"])} domains')"

# RAG stats
curl -s http://localhost:8888/api/memory/stats | python3 -m json.tool

# Calendar
curl -s http://localhost:8888/api/calendar/today | python3 -m json.tool

# Temperatures (server closet monitoring)
curl -s http://localhost:8888/api/temperatures | python3 -m json.tool

# Focus timer status
curl -s http://localhost:8888/api/focus | python3 -m json.tool

# Pending reminders
curl -s http://localhost:8888/api/reminders | python3 -m json.tool

# Chat endpoint (end-to-end test)
curl -s -X POST http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"stream":false}' \
  --max-time 60 | python3 -m json.tool
```

---

## Logging Strategy

### Log levels
- **ERROR**: LLM failures, HA unreachable, DB errors, unhandled exceptions — investigate immediately
- **WARN**: Slow responses (>10s chat, >5s tool), Helios health check fail, Google token refresh, rate limit approaches
- **INFO**: Normal request lifecycle, startup/shutdown, tool execution, scheduler events
- **DEBUG**: Full LLM prompts/responses, RAG query details — dev only, never production

### Log format
Structured Python logging via `logger.info/warning/error`. All modules use `logging.getLogger(__name__)`. Key log prefixes:
- `[orchestrator]` — startup, shutdown, HTTP client
- `[CLOUD_BRAIN]` — chat flow, mode routing, Helios/Nemotron delegation
- `[LOCAL_AGENT]` — tool execution commands
- `[NEMOTRON_LOOP]` — tool loop rounds, tool calls, terminal tool stops
- `[FOCUS]` — focus timer, Endel audio, Pi-hole blocking
- `[HELIOS]` — health checks, auto-start/stop, idle tracking
- `[STATE]` — SQLite persistence, reminder reload
- `[SCHEDULER]` — job scheduling, calendar poll, morning briefing
- `[GOOGLE_AUTH]` — token refresh
- `[HA]` — entity discovery, service calls
- `[TTS]` — voice announcements
- `[PIHOLE]` — focus blocking toggles

### Reading logs
```bash
# All logs (follow)
docker logs brain-orchestrator --tail 50 -f

# Errors only
docker logs brain-orchestrator --tail 200 2>&1 | grep -i error

# Specific component
docker logs brain-orchestrator --tail 200 2>&1 | grep '\[HELIOS\]'
docker logs brain-orchestrator --tail 200 2>&1 | grep '\[NEMOTRON_LOOP\]'

# Since a specific time
docker logs brain-orchestrator --since 1h 2>&1 | grep -i warn
```

---

## What to ALWAYS Log / Monitor

- Every chat request: timestamp, mode (explainer/mirror/counterbalance/challenge/baseline), intensity, response time
- LLM call outcome: backend (Helios/Nemotron), tokens, response time, success/fail
- Tool execution: tool name, duration, success/fail (Prometheus: `bgw_tool_call_*`)
- Helios lifecycle: auto-start, idle shutdown, health check failures
- HA service calls: entity, service, success/fail
- Scheduler events: reminder delivery, calendar poll, morning briefing
- Temperature alerts: closet temp warnings (>80F) and critical (>85F)
- TTS announcements: target speaker, success/fail

## What to NEVER Log
- Full RAG document content (log query + result count only)
- API keys, HA tokens, Google OAuth tokens
- Full conversation history (log message count only)
- User profile YAML content

---

## Prometheus Metrics (prefix: `bgw_`)

All metrics defined in `orchestrator/metrics.py`:

| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `bgw_chat_requests_total` | Counter | mode, intensity | Chat volume by routing mode |
| `bgw_chat_duration_seconds` | Histogram | mode | End-to-end chat latency |
| `bgw_tool_call_total` | Counter | tool, status | Tool execution count |
| `bgw_tool_call_duration_seconds` | Histogram | tool | Tool execution latency |
| `bgw_tool_call_errors_total` | Counter | tool | Tool failures |
| `bgw_rag_query_total` | Counter | | RAG search count |
| `bgw_rag_query_duration_seconds` | Histogram | | RAG search latency |
| `bgw_focus_sessions_started_total` | Counter | soundscape | Focus timer starts |
| `bgw_focus_sessions_completed_total` | Counter | | Completed sessions |
| `bgw_temperature_fahrenheit` | Gauge | location | Sensor readings |
| `bgw_temperature_delta_fahrenheit` | Gauge | | Closet-kitchen delta |
| `bgw_helios_starts_total` | Counter | | Helios auto-start count |
| `bgw_pihole_blocking_toggles_total` | Counter | action | Focus blocking toggles |

### Adding New Metrics

1. **Define the metric** in `orchestrator/metrics.py` using `prometheus_client` (Counter, Histogram, Gauge)
2. **Import it** in the relevant module and call `.inc()` / `.observe()` / `.set()`
3. **Verify locally**: `curl http://localhost:8888/metrics | grep bgw_your_metric`
4. **Add Grafana panel**: Edit dashboard JSON in `monitoring/grafana/provisioning/dashboards/json/`
5. **Deploy**: `docker compose up -d --build orchestrator && docker restart grafana`

### Adding New Logging

1. Use `logger = logging.getLogger(__name__)` at module top
2. Use a consistent prefix: `logger.info(f"[MODULE_NAME] message")`
3. Log at appropriate level (see log levels above)
4. Never log secrets, full conversation content, or RAG document bodies

---

## Performance Targets

| Metric | Target | Red flag |
|--------|--------|----------|
| Chat (Helios direct) | <8s | >15s |
| Chat (Helios + Nemotron tools) | <15s | >30s |
| Nemotron tool loop | <5s | >10s |
| RAG query | <500ms | >2s |
| HA service call | <2s | >5s |
| TTS announcement | <3s | >8s |
| Server closet temp | <75F | >80F warning, >85F critical |

---

## Diagnosing Common Issues

### "Chat is slow"
1. Check `/health` — is Helios online? If not, it's auto-starting (~30s cold start)
2. Check if Nemotron tools were invoked (look for `ask_orchestrator` in response)
3. Check `bgw_chat_duration_seconds` histogram in Grafana for p95
4. Check Saturn GPU utilization: `ssh labadmin@10.0.0.58 nvidia-smi`

### "Helios keeps starting/stopping"
1. Check `shared._last_helios_request` — idle timeout is 30 min
2. Check Helios health: `curl -s http://10.0.0.195:8080/health`
3. Check scheduler job `helios_idle_check` is running: `curl -s http://localhost:8888/health | jq .scheduler`

### "Home Assistant commands fail"
1. Check HA connectivity: `curl -s -H "Authorization: Bearer $HA_TOKEN" http://10.0.0.106:8123/api/`
2. Check entity count in health endpoint — 0 entities = HA unreachable at startup
3. Check entity exists: `curl -s http://localhost:8888/api/ha/entities | python3 -c "import sys,json; [print(e['entity_id']) for e in json.load(sys.stdin)['controllable'].get('light',[])]"`

### "Reminders not firing"
1. Check scheduler is running: health endpoint → `scheduler.running`
2. Check pending reminders: `curl -s http://localhost:8888/api/reminders`
3. Check state_store DB: `docker exec brain-orchestrator python3 -c "import state_store; state_store.init_db(); print(state_store.get_pending_reminders())"`
4. Check TTS is reachable: `curl -s http://10.0.0.173:8002/health`

### "Calendar not working"
1. Check Google auth token: health endpoint → `calendar.configured`
2. Check token expiry: look for `[GOOGLE_AUTH] Token refreshed` in logs
3. Manual test: `curl -s http://localhost:8888/api/calendar/today`

### "Server closet is hot"
1. Check `curl -s http://localhost:8888/api/temperatures`
2. If closet > 80F: check if Helios is running unnecessary (RTX 5090 = major heat)
3. If closet > 85F: consider stopping non-essential GPU workloads

### "Container keeps restarting"
1. `docker logs brain-orchestrator --tail 100`
2. `docker inspect brain-orchestrator | jq '.[0].State'`
3. Check for OOM: `docker stats brain-orchestrator --no-stream`
4. Check for import errors in startup logs

---

## Grafana Dashboard

- **URL**: http://localhost:3000/d/brain-gateway-overview (admin/braingw)
- **Dashboard JSON**: `/opt/jupiter/gateway_mvp/monitoring/grafana/provisioning/dashboards/json/`
- **After editing**: `docker restart grafana`
- **Prometheus scrapes**: `http://localhost:8888/metrics`

---

## Docker Commands

```bash
# Logs
docker logs brain-orchestrator --tail 50 -f
docker logs brain-orchestrator --tail 100 2>&1 | grep -i error

# Restart
docker compose up -d --build orchestrator

# Stats
docker stats brain-orchestrator --no-stream

# Shell into container
docker exec -it brain-orchestrator bash
```

---

## Output format when diagnosing

```
DIAGNOSIS:
- Issue: what is happening
- Root cause: what is causing it
- Evidence: specific log lines, metrics, or curl responses

IMMEDIATE FIX:
- Steps to resolve right now

PERMANENT FIX:
- Code or config change to prevent recurrence

MONITORING GAP:
- What logging/alerting would have caught this faster
```
