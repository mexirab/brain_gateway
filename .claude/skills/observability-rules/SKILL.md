---
name: observability-rules
description: Logging and metrics rules for the Brain Gateway orchestrator — what must never be logged (personal content, secrets) at any level, log levels and prefixes, and how to add a Prometheus metric, Grafana panel or alert. Use before adding or changing any logger call, metric or alert rule. Preloaded by the prod-support agent.
---

# Observability rules

These rules apply to whoever writes the code, before it's written. The `prod-support` agent checks them after the fact; the guard test enforces the never-log list in CI.

## Logs

### Levels
- **ERROR**: LLM failures, HA unreachable, DB errors, unhandled exceptions — investigate immediately
- **WARN**: Slow responses (>10s chat, >5s tool), Helios health check fail, Google token refresh, rate limit approaches
- **INFO**: Normal request lifecycle, startup/shutdown, tool execution, scheduler events
- **DEBUG**: extra lifecycle detail (round counts, arg keys, lengths) — dev only. Content rules below apply at every level, DEBUG included

### Format and prefixes
Structured JSON logging via `log_config.configure_logging()` — called in `orchestrator.py` at startup. All modules use `logging.getLogger(__name__)`. Log output is JSON lines with fields: `ts`, `level`, `logger`, `msg`, `request_id`, plus optional extras (`component`, `tool_name`, `latency_ms`). Key log prefixes:
- `[orchestrator]` — startup, shutdown, HTTP client
- `[CLOUD_BRAIN]` — chat flow, mode routing, unified-loop orchestration
- `[UNIFIED_LOOP]` — agentic loop rounds, tool calls, termination
- `[FOCUS]` — focus timer, Endel audio, Pi-hole blocking (`Site blocking not active: …` at INFO is expected while `FOCUS_BLOCKING_ENABLED=false`)
- `[MODEL]` — primary-model health checks, backend routing
- `[STATE]` — SQLite persistence, reminder reload
- `[SCHEDULER]` — job scheduling, calendar poll, morning briefing
- `[GOOGLE_AUTH]` — token refresh
- `[HA]` — entity discovery, service calls
- `[TTS]` — voice announcements
- `[PIHOLE]` — focus blocking toggles (silent on this deployment; blocking deprecated)


## What to always log / monitor

- Every chat request: timestamp, mode (explainer/mirror/counterbalance/challenge/baseline), intensity, response time
- LLM call outcome: backend (primary vs fallback), tokens, response time, success/fail
- Tool execution: tool name, duration, success/fail (Prometheus: `bgw_tool_call_*`)
- Primary model health check failures (Helios is power-tiered: asleep is normal; a failure while it is awake is an incident)
- HA service calls: entity, service, success/fail
- Scheduler events: reminder delivery, calendar poll, morning briefing
- Temperature alerts: closet temp warnings (>80F) and critical (>85F)
- TTS announcements: target speaker, success/fail

## What to NEVER log

Loki keeps 30 days and the nightly self-audit (F-014) feeds WARNING+ lines to the LLM and a Pushover digest; its investigator can read any Loki line from the last 24 h and, with `SELF_AUDIT_CLAUDE_API_KEY` set, sends them to Anthropic's API — anything logged is effectively copied off the log store. At **any** level, DEBUG included:

- Secrets: API keys, HA tokens, Google OAuth tokens, calendar `htmlLink`s
- User/personal content:
  - chat/voice message text, conversation history, raw model output
  - search/memory/RAG/web/Gmail queries, RAG document bodies, email subjects
  - reminder/announcement/nudge text, bookmark descriptions
  - calendar titles/locations, travel destinations, raw phone-calendar events
  - medication names, meal descriptions
  - focus/brain-dump/backlog/decomposition/code-agent task text
  - document titles, Paperless file names
  - finance side-quest/budget names
  - HA call `data` values, tool args (log the dict keys)
  - the user's name, user profile YAML content
  - mode-router distress tags (mental-state data — log a count)
- Query strings on HTTP access logs (`log_config.StripQueryStringFilter` on `uvicorn.access` drops them — they carried `?search=`/`?query=` text)
- `/api/audio/` clip/stream names (tokenless route: the name is the only secret and the clip is a spoken reply; the same filter logs `/api/audio/…`)

**Log instead:** `len()`, ids (`reminder_id`, Google event id, Gmail message id, `quest_id`, task id), categories, counts, dict keys. For unparseable model output, log the parser error + `log_config.content_shape(text)` (content-free: code-fence / think-block / tool-call-block / json-like / text).

**Library loggers:** `log_config.quiet_url_logging_libs()` raises `httpx`/`httpcore` to WARNING globally (their INFO `HTTP Request: GET <url>` lines carry search text), and the payload-logging libraries too (`aioesphomeapi`, `zeroconf`, `anthropic` — which logs whole request bodies at DEBUG — `httpx2`, `httpcore2`); `DropPayloadArgsFilter` drops those libraries' record args. A new dependency that logs URLs or payloads goes on that list.

**Guard:** `orchestrator/tests/test_no_user_content_in_logs.py` AST-scans every logger call (positional + keyword args, `getLogger()` chains, `self.logger`) and fails on sensitive names/suffixes, sensitive dict keys / `.get()` keys, or any slice. Audited non-personal exceptions (upstream error bodies, config values, tool/instance names) need an inline `# log-safe: <reason>` comment on or directly above the call.


## Adding a metric

1. Define it in `orchestrator/metrics.py` (`prometheus_client` Counter / Histogram / Gauge, `bgw_` prefix). Keep labels bounded: never an id, a name or free text as a label value.
2. Import it in the module and call `.inc()` / `.observe()` / `.set()`. A failure that a handler returns as text (not an exception) needs its own counter, or nothing sees it.
3. Check it: `curl -s localhost:8888/metrics | grep bgw_your_metric` (after the change is deployed), or a test with `_helpers.metric_value` (`unit-test` skill).
4. Grafana: dashboards are generated by `monitoring/grafana/dashgen/` (Python). Add the panel in the builder and run `python monitoring/grafana/dashgen/build.py`. Never hand-edit the JSON.
5. Document the metric in `monitoring/README.md` (and its row in the `prod-support` agent's metric tables when it's a diagnosis signal).

## Adding an alert

1. Rule in `monitoring/prometheus/alert-rules.yml`.
2. Routing is an alertname allow-list: add the name to a receiver in the Alertmanager template, or to `GRAFANA_ONLY` in `orchestrator/tests/test_alert_routing.py` if no human should get it. That test fails CI otherwise. Quiet (non-paging) Pushover is the default for anything that isn't an outage.
3. Runbook entry in `monitoring/README.md`: what fired, what to check, how to fix.

## Adding a logger call

1. `logger = logging.getLogger(__name__)` at module top.
2. A consistent prefix: `logger.info("[MODULE_NAME] message")`.
3. Pick the level from "Levels" above.
4. Never log secrets or user content (the list above). The guard test enforces it; an audited non-personal exception needs `# log-safe: <reason>` on or directly above the call.
