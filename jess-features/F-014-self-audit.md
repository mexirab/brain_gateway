# F-014: Daily Self-Audit

**Priority:** P2 — operational hygiene; not user-facing in the moment but builds trust over time
**Status:** Done
**Depends on:** F-013 (Pushover bridge — used as the digest channel)
**Blocks:** None

---

## ADHD Insight

The user runs a multi-host personal assistant with ~10 always-on services. When something breaks overnight (a llama-server crash, a Loki push that started failing, a job that started ERRORing every minute), the only way they currently find out is either (a) something user-facing breaks, or (b) they happen to open Grafana. Both fail the ADHD test: low signal-to-noise effort, easy to skip, and stale alerts compound into "the dashboard is always red, ignore it."

A daily 7am push from Jess that says "here's what got noisy in the last 24 hours, here's what I'd investigate, here's a command you can run by me" is the equivalent of a coworker who skims the logs every morning before standup. The user only has to engage if Jess says something interesting, and the engagement is bounded: read the report, paste a command into chat for review, run it. No dashboard archaeology.

The *secondary* benefit — and the one the user explicitly asked to evaluate — is whether Jess's own diagnosis is good enough to trust. Reviewing her suggested-fix output against what Claude Code would have said builds calibration on where Qwen3.5-27B's reasoning is reliable vs handwavy.

## What Jess Does

Every day at 07:00 (configurable; `SELF_AUDIT_HOUR_UTC` is actually interpreted in the scheduler's local timezone despite its name), the orchestrator's APScheduler fires `run_self_audit` from `orchestrator/jobs_self_audit.py`. The job:

1. **Queries Loki** (`SELF_AUDIT_LOKI_URL`, live `http://loki:3100`) for the last 24h of error/warn level logs (`level=~"error|warning|warn|critical|fatal"`), **one query per source group** so a noisy source can't crowd out the rest (a UPS losing its USB link logs ~1800 warnings/h and used to fill a single 5000-line window in ~3 h):

   | Group | Selector |
   |-------|----------|
   | `orchestrator` | `service="orchestrator"` |
   | `jupiter_other` | every other Jupiter stream (`host=""`) |
   | `helios` | `host="helios"`, not `nut-*` (model units + kernel via `promtail-helios`'s journald job) |
   | `ups` | `host="helios"`, `service=~"nut-.*"` |

   Each group samples up to `SELF_AUDIT_LOKI_LIMIT_PER_GROUP` lines (default 1500); exact per-group counts come from a separate metric query. A group whose sample hit the limit is reported as truncated and counted in `bgw_self_audit_truncated_total{group}`. A group whose query failed makes the run `partial` (`reason=sources_failed`) and the digest never says "All clean" while a source didn't answer. The "Loki alive" probe is a `query_range` for any line in the window (Loki rejects log selectors on instant `/query`).

2. **Buckets** the entries into clusters by `(service, first 80 chars of message)` — after replacing hex ids (8+ chars) with `<id>` and every digit run with `<n>`, and keeps the top N most frequent.

3. **Diagnoses** the clusters. Log text is defanged (control markup, code fences, frame markers) and wrapped in `BEGIN UNTRUSTED LOG DATA` / `END UNTRUSTED LOG DATA` before it reaches the model. Two modes:
   - `SELF_AUDIT_INVESTIGATE=false` (default): a single no-tools `call_model` pass. For each cluster: severity, likely cause, and one `**Suggestion:**` prefixed `INVESTIGATE:` (read-only) or `FIX:` (service-restart level).
   - `SELF_AUDIT_INVESTIGATE=true`: the read-only investigator (see below) looks at code and logs before answering.

4. **Saves** the full markdown report to `/app/data/self_audits/YYYY-MM-DD.md` (alert runs: `YYYY-MM-DD-HHMM-alert-<Name>.md`).

5. **Pushes** a one-line digest via Pushover (title `Jess audit · <date>`, or `Jess investigated <Alert>`; priority 1 if any CRITICAL cluster, else 0) and posts the same text to the dashboard inbox (ref `self_audit`, no Web Push).

6. **Indexes** one line of facts into mempalace (wing `system`, room `audit`), built by code: date, trigger, cluster and severity counts, sanitized service names, truncated sources, report filename. Never model prose or log text — normal chat retrieval searches every wing.

The user's review path is: read the Pushover digest → if interesting, open the markdown → paste suspicious commands into Claude Code → discuss → run if approved. **Jess never executes the suggested commands herself.** Nothing executes model output; fix PRs are a later step (self-monitoring step 3).

### Read-only investigator (`orchestrator/self_investigator.py`, self-monitoring step 2)

Behind `SELF_AUDIT_INVESTIGATE=false`. Design + as-built notes: `docs/internal/SELF_INVESTIGATOR_DESIGN.md`.

- **Tools:** `list_dir`, `read_file`, `grep` (substring only — regex was dropped because a backtracking pattern holds the GIL and froze the orchestrator) over the repo mounted read-only at `SELF_AUDIT_REPO_PATH` (`/opt/gateway_mvp`), plus `loki_query` (log or metric query, capped lines/hours). Path allow-list: `orchestrator/`, `monitoring/`, `scripts/`, `tts/`, `docs/` (not `docs/internal/`), `jess-features/`, `frontend/src/`, `homeassistant/` and a few top-level docs; `.env*`, `data/`, `credentials/`, `config/`, rendered configs and token/secret files are refused. "Missing" and "denied" return the same message.
- **Every tool result** is secret-redacted, defanged, framed as untrusted and capped (6000 chars).
- **Budgets:** daily runs `SELF_AUDIT_INVESTIGATE_MAX_ROUNDS` 12 / `_MAX_TOOL_CALLS` 30 / `_MAX_SECONDS` 600; alert runs 6 rounds / 300 s. ≤ 5 calls per round, duplicates collapsed, per-call timeouts clipped to the deadline with 90 s kept for a forced final report (`result=budget`).
- **Slot etiquette:** waits for the single llama slot to be idle (`bgw_active_requests` and llama-server's `llamacpp:requests_processing`) before every model call; alert runs first wait for 3 min of idle (up to 30 min). **Never wakes Helios** — asleep means the deterministic digest (`result=brain_asleep`, run `partial`).
- **Citation check:** every `path:line` the model cites is checked against the allow-listed repo; wrong paths or out-of-range lines are tagged unverified.

### Alert trigger (`POST /api/self_audit/alert`, `orchestrator/routes_self_audit.py`)

Alertmanager's `self-investigator` receiver (`continue: true`, so the normal Pushover route still fires) posts the standard webhook JSON. Auth: bearer `API_TOKEN` or the scoped `SELF_AUDIT_WEBHOOK_TOKEN` (≥ 24 chars, accepted on this path only). Body ≤ 64 KB. Only allow-listed alertnames run: `HeliosHardwareFault`, `ChatStreamTruncating`, `EmptyRepliesReachingUsers`, `ToolCallsSilentlyDropped`, `EmbeddingErrors`, `AutoLearnFailing`, `SchedulerJobsMissed`, `HeliosLogsSilent` (`BrainOnCPU`/`BrainCudaInitFailed`/`HeliosGpuDriverBroken` left out — on a CPU-speed brain an investigation only times out). Rate limits 6 h per alert, 6 a day (RAM-only). Alert runs look back `SELF_AUDIT_ALERT_LOOKBACK_HOURS` (3) and don't push Loki-outage digests. The 07:00 cron waits for an in-flight alert run instead of being dropped. Response codes: `TECHNICAL_REFERENCE.md` → Self-Audit. Receiver rendering: `monitoring/README.md` → Alertmanager.

### Helios log-silence check

`check_helios_log_silence` (5-min interval job, first run 30 s after start, registered with the self-audit) sets `bgw_helios_logs_silent` = 1 when Loki has no `llama-server-primary` lines from Helios in 30 min, and counts `bgw_helios_log_check_total{result=lines|silent|error}`. Feeds `HeliosLogsSilent` and `HeliosLogCheckFailing` (see Alerting).

---

## Interaction Examples

### Quiet day

```
07:00:01: Pushover lands.
  Title: Jess audit · 2026-04-25
  Body:  ✓ All clean (3 minor warnings)
         Full: /app/data/self_audits/2026-04-25.md
  Priority: 0
```

### Interesting day

```
07:00:01: Pushover lands.
  Title: Jess audit · 2026-04-25
  Body:  1 critical, 2 high, 8 medium
         Top: llama-server-coder restart loop (12x)
         Full: /app/data/self_audits/2026-04-25.md
  Priority: 1 (high)

User reads the markdown:

  ## llama-server-coder · CRITICAL
  - 12 restarts in 24h, all from `main: exiting due to HTTP server error`
  - Likely cause: Race between systemd start and another process holding 8082.
  - INVESTIGATE: sudo lsof -iTCP:8082 -sTCP:LISTEN
  - FIX: sudo systemctl restart llama-server-coder (transient, may recur)

User pastes the INVESTIGATE command to Claude Code:
  "Jess says to run: sudo lsof -iTCP:8082 -sTCP:LISTEN — is that safe?"
Claude reviews, confirms, runs. Returns the open file: a leftover wget from
the model download. User kills it. No more restart loop.
```

---

## Modified files

- `orchestrator/jobs_self_audit.py` — main implementation (grouped Loki collection, clustering, untrusted framing, suggestion filter, digest, palace facts, Helios log-silence check). Never raises.
- `orchestrator/self_investigator.py` — read-only investigator (tools, budgets, slot etiquette, citation check).
- `orchestrator/routes_self_audit.py` — `POST /api/self_audit/alert` (Alertmanager webhook).
- `orchestrator/api_routes.py` — `POST /api/self_audit/run` manual trigger; includes the alert router.
- `orchestrator/orchestrator.py` — daily cron (`wait_for_lock`), `helios_log_silence_check` job, `WEBHOOK_TOKEN_PATHS` scoped token in `BearerAuthMiddleware`, 64 KB body cap.
- `orchestrator/config.py` — `SELF_AUDIT_*` settings + `model_validator` (auto-disable on bad Loki URL, clamps, short webhook token discarded even with the audit off).
- `orchestrator/inbox.py` — `self_audit` message ref.
- `monitoring/prometheus/alert-rules.yml`, `monitoring/alertmanager/alertmanager.yml.template`, `scripts/generate-configs.sh` — alerts + webhook receiver.

## Env vars (all in `docs/ENV_VARS.md`)

| Var | Default | Purpose |
|-----|---------|---------|
| `SELF_AUDIT_ENABLED` | `false` | Master kill switch |
| `SELF_AUDIT_HOUR_UTC` | `7` | Hour-of-day for the daily run (scheduler-local timezone despite the name) |
| `SELF_AUDIT_LOOKBACK_HOURS` | `24` | Loki query range |
| `SELF_AUDIT_LOKI_URL` | `http://loki:3100` (live) | Loki base URL (no path). Loki joins `gateway_nerves_gateway-net`, so the orchestrator uses the container name; Jupiter Loki is loopback-only and containers can't reach Tailscale Serve on the host's own tailnet IP |
| `SELF_AUDIT_MAX_CLUSTERS` | `30` | Cap on clusters fed to Jess (prompt-size bound) |
| `SELF_AUDIT_OUTPUT_DIR` | `/app/data/self_audits` | Markdown report directory |
| `SELF_AUDIT_PUSHOVER_PRIORITY_NORMAL` | `0` | Priority when no CRITICAL clusters |
| `SELF_AUDIT_PUSHOVER_PRIORITY_CRITICAL` | `1` | Priority when ≥1 CRITICAL cluster |
| `SELF_AUDIT_LLM_TIMEOUT_SEC` | `120` | LLM call timeout, no-investigator path (matches unified_loop) |
| `SELF_AUDIT_LOKI_LIMIT_PER_GROUP` | `1500` | Lines sampled per Loki source group |
| `SELF_AUDIT_INVESTIGATE` | `false` | Read-only investigator + alert webhook |
| `SELF_AUDIT_REPO_PATH` | `/opt/gateway_mvp` | Read-only repo mount for the file tools |
| `SELF_AUDIT_INVESTIGATE_MAX_ROUNDS` / `_MAX_TOOL_CALLS` / `_MAX_SECONDS` | `12` / `30` / `600` | Daily investigation budgets |
| `SELF_AUDIT_WEBHOOK_TOKEN` | _empty_ | Scoped Alertmanager bearer (≥ 24 chars) |
| `SELF_AUDIT_ALERT_LOOKBACK_HOURS` | `3` | Loki window for alert runs |

## Metrics

- `bgw_self_audit_runs_total{result}` — **daily runs only**. `result ∈ {ok, partial, failed, skipped}`. `partial` = logs read but no diagnosis (Helios asleep / LLM failed) or a source group failed (`reason=sources_failed`). `failed` = Loki unreachable. `skipped` = feature disabled. Labels pre-initialized at 0.
- `bgw_self_audit_clusters_total{severity}` — per cluster Jess returned.
- `bgw_self_audit_latency_seconds` — histogram, full job runtime.
- `bgw_self_audit_format_drift_total` — diagnosis didn't match the expected format.
- `bgw_self_audit_truncated_total{group}` — a source group's sample hit `SELF_AUDIT_LOKI_LIMIT_PER_GROUP`.
- `bgw_self_audit_last_run_timestamp_seconds` — on `job_heartbeat` (`app_state` key `self_audit_last_run_ts`), stamped after each **daily** run (alert runs must not mask a dead cron), re-seeded at startup from the persisted value; disabling the audit forgets it.
- `bgw_self_investigator_runs_total{trigger,result}` — `trigger ∈ {daily, alert}`, `result ∈ {ok, budget, brain_asleep, error}`.
- `bgw_self_investigator_tool_calls_total{tool,result}` — `result ∈ {ok, denied, error, duplicate, capped}`.
- `bgw_background_llm_errors_total{caller="self_investigator"}`.
- `bgw_self_audit_alert_trigger_total{result}` — `accepted | ignored | rate_limited | busy | disabled | bad_request`.
- `bgw_helios_logs_silent` (gauge), `bgw_helios_log_check_total{result}`.

## Alerting

Both in `monitoring/prometheus/alert-rules.yml` group `brain_gateway_deadmans`, routed to the non-paging `pushover-quiet` receiver:

| Alert | Fires when |
|-------|-----------|
| `SelfAuditStale` | No completed run in 25.5h |
| `SelfAuditFailing` | ≥2 `failed` runs in 2d with no `ok`/`partial` run (`partial` = Helios asleep, logs still read — counts as healthy) |

In group `helios_health`, also `pushover-quiet`:

| Alert | Fires when |
|-------|-----------|
| `HeliosLogsSilent` | `bgw_helios_logs_silent == 1` AND llama-server generated tokens in the same 30 min, for 15m — promtail-helios or loki-push-gw is down |
| `HeliosLogCheckFailing` | ≥ 5 `bgw_helios_log_check_total{result="error"}` in 30 min — the orchestrator can't query Loki |

Grafana (`brain_gateway_sre.py`, "Background Jobs" row): "Self-Audit Heartbeat Age" stat + "Self-Audit Runs (by result, /day)". No panels yet for the investigator / alert-trigger / log-silence metrics.

## Failure modes (graceful)

- **Loki unreachable** → log error, increment `bgw_self_audit_runs_total{result="failed"}`, push an explicit CRITICAL "audit failed" digest (never a green "all clean"). Cron retries tomorrow; `SelfAuditFailing` fires if it keeps failing.
- **One source group's query fails** → run continues on the rest, `result="partial"` (`reason=sources_failed`), digest names the blind source and never says "All clean".
- **Loki returns nothing** → save a one-line "no errors in last 24h" report, push priority-0 digest. Distinguishable from failure.
- **Helios asleep (investigator on)** → never woken; deterministic digest, `bgw_self_investigator_runs_total{result="brain_asleep"}`.
- **Investigation budget runs out** → final answer forced with the evidence so far (`result="budget"`).
- **Jess unreachable / LLM timeout** → save raw cluster data to disk anyway, push a "audit captured, diagnosis unavailable" digest. `result="partial"`.
- **Pushover disabled / fails** → write report to disk, log a warning, return success. The disk file is the source of truth; the push is best-effort.
- **Output directory not writable** → log error and continue with Pushover-only delivery. Don't lose the diagnosis just because a mount went read-only.

## Security model

- **Read-only design:** the orchestrator never executes model output. The investigator's tools only read the allow-listed repo and query Loki. Suggested diffs carry an "UNREVIEWED, possibly influenced by log content" banner.
- **Untrusted log text:** log lines, tool results and alert labels/annotations are defanged and framed (`BEGIN/END UNTRUSTED LOG DATA`). The frame is advisory; the real boundary is that every tool is read-only.
- **Suggested-command allow-list** (`_check_suggestion`): matches both `**Suggestion**:` and `**Suggestion:**` (the second format used to bypass it). Every stage of a `|` pipeline is checked; any other shell operator is refused (including redirects); `awk`/`sed` removed; `find -delete/-exec/-ok/-fprint*` refused; `docker compose` limited to `logs/ps/config/top/restart`, `docker image` to `ls/inspect`. Plus a dangerous-pattern regex and the secret-pattern filter on the report.
- **Mempalace** only ever gets a code-built facts line, never model prose or log text.
- **Loki URL** is container-internal (`loki:3100` over `gateway-net`); Loki publishes on `127.0.0.1` only. Loki's delete API (`deletion_mode: disabled`) and ruler API are off, so nothing reachable can wipe error streams into a false "all clean".
- **Webhook:** scoped `SELF_AUDIT_WEBHOOK_TOKEN` opens only `/api/self_audit/alert`, so Alertmanager never holds `API_TOKEN`. Only an allow-listed `alertname` selects what runs; nothing from the payload is logged except that name.
- **Manual trigger** (`POST /api/self_audit/run`) is bearer-gated. `run_self_audit()` re-checks `SELF_AUDIT_ENABLED` and `JESS_ADVANCED` at entry (alert runs also `SELF_AUDIT_INVESTIGATE`), so no route bypasses the gate.
- **Known limits:** Loki still holds pre-PR #81 log text until it ages out (30 d); `loki_query` can read it into the on-disk report (never mempalace/Pushover/inbox). Mempalace entries written by the old audit (model prose, `source="self_audit"`) are still reachable from chat until purged.

## Testing checklist

- [x] Unit test: `_bucket_logs` groups identical messages, keeps top N
- [x] Unit test: `_build_audit_prompt` includes destructive-command guard rails
- [x] Unit test: `_parse_jess_diagnosis` handles missing severity / malformed lines without raising
- [x] Unit tests: investigator path allow-list, budgets, citation check, slot wait, webhook auth/rate limits/payload shapes, config clamps, `generate-configs.sh` receiver render (`test_self_investigator.py`, `test_jobs_self_audit.py`, `test_config.py`, `test_generate_configs.py`)
- [x] Unit test: failure paths (Loki down, LLM down) increment correct metrics + return correct result tag
- [x] Integration: hit `POST /api/self_audit/run` manually, see Pushover digest land
- [x] Manual: induce a known error (e.g. fake a llama-server crash via `systemctl stop`), wait one cycle, verify the cluster shows up in the report
