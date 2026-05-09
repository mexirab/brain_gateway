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

Every day at 07:00 UTC (configurable), the orchestrator's APScheduler fires `run_self_audit` from `orchestrator/jobs_self_audit.py`. The job:

1. **Queries Loki** (Jupiter, port 3100) for the last 24h of error/warn level logs from every service the Helios promtail sidecar ships — both Docker containers (orchestrator, frontend, searxng, etc.) and systemd units (llama-server, llama-server-coder, qwen-tts, brain-gateway). The promtail-helios config gains a `journal:` scrape stage in this feature so systemd units land in Loki alongside container logs.

2. **Buckets** the entries into clusters by `(service, first 80 chars of message)` and keeps the top N most frequent. Crude but cheap and good enough — exact-prefix matching catches "ConnectionError to ChromaDB" as one cluster regardless of the trailing IP/port detail.

3. **Asks Jess** (single call_model invocation, no multi-turn loop — mitigates the known Qwen3.5-27B tool-call drift) to diagnose each cluster: severity, likely cause, and one suggested action prefixed with `INVESTIGATE:` (read-only diagnostic) or `FIX:` (mutation). The prompt explicitly forbids destructive commands (`rm`, `dd`, `mkfs`, `format`, `drop`, `truncate`) and constrains FIX commands to read-only or service-restart level.

4. **Saves** the full markdown report to `/app/data/self_audits/YYYY-MM-DD.md` (host-mounted, so the user can `cat` it from a regular shell).

5. **Pushes** a one-line digest via Pushover: title `Jess audit · 2026-04-25`, body summary like `3 critical, 1 high, 12 medium · Top: llama-server-coder restart loop`. Priority bumps to 1 (high) if any cluster is tagged CRITICAL, else 0.

6. **Indexes** a short summary into mempalace under wing="system", room="audit" so future Jess can recall recent operational state ("yesterday's audit flagged a Loki push failure, has it recurred?").

The user's review path is: read the Pushover digest → if interesting, open the markdown → paste suspicious commands into Claude Code → discuss → run if approved. **Jess never executes the suggested commands herself.** Read-only audit is the entire safety story.

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

- NEW `jess-features/F-014-self-audit.md` (this file).
- NEW `orchestrator/jobs_self_audit.py` — main implementation. Async, single-pass, never raises.
- `orchestrator/orchestrator.py` — register the cron job at 07:00 UTC (configurable) plus a manual-trigger endpoint at `POST /api/self_audit/run` (bearer-gated).
- `orchestrator/config.py` — new `SELF_AUDIT_*` settings + `self_audit_loki_url` + `model_validator` to disable cleanly if `SELF_AUDIT_ENABLED=true` but Loki URL is unset.
- `orchestrator/metrics.py` — `bgw_self_audit_runs_total{result}`, `bgw_self_audit_clusters_total{service,severity}`, `bgw_self_audit_latency_seconds`.
- `orchestrator/api_routes.py` — `POST /api/self_audit/run` bearer-protected manual trigger so the user can fire today's audit without waiting for tomorrow.
- `monitoring/promtail/promtail-helios.yml` — add `journal:` scrape stage with `keep` regex restricted to `llama-server`, `llama-server-coder`, `qwen-tts`, `brain-gateway`.
- `docker-compose.yml` — add `/var/log/journal:/var/log/journal:ro` and `/etc/machine-id:/etc/machine-id:ro` mounts to the `promtail-helios` service so it can read systemd journal.

## Env vars (all in `docs/ENV_VARS.md`)

| Var | Default | Purpose |
|-----|---------|---------|
| `SELF_AUDIT_ENABLED` | `false` | Master kill switch |
| `SELF_AUDIT_HOUR_UTC` | `7` | Hour-of-day in UTC for the daily run |
| `SELF_AUDIT_LOOKBACK_HOURS` | `24` | Loki query range |
| `SELF_AUDIT_LOKI_URL` | `http://jupiter-amds.tail74fc4a.ts.net:3100` | Loki base URL (no path) |
| `SELF_AUDIT_MAX_CLUSTERS` | `30` | Cap on clusters fed to Jess (prompt-size bound) |
| `SELF_AUDIT_OUTPUT_DIR` | `/app/data/self_audits` | Markdown report directory |
| `SELF_AUDIT_PUSHOVER_PRIORITY_NORMAL` | `0` | Priority when no CRITICAL clusters |
| `SELF_AUDIT_PUSHOVER_PRIORITY_CRITICAL` | `1` | Priority when ≥1 CRITICAL cluster |
| `SELF_AUDIT_LLM_TIMEOUT_SEC` | `120` | LLM call timeout (matches unified_loop) |

## Metrics

- `bgw_self_audit_runs_total{result}` — `result ∈ {ok, partial, failed, skipped}`. `partial` = Loki query succeeded but LLM call failed; report saved without diagnosis. `failed` = Loki unreachable, no report saved. `skipped` = feature disabled.
- `bgw_self_audit_clusters_total{service, severity}` — counter incremented per cluster Jess returned. Lets Grafana show "what services break most often" over weeks.
- `bgw_self_audit_latency_seconds` — histogram, full job runtime.

## Failure modes (graceful)

- **Loki unreachable** → log error, skip job, increment `bgw_self_audit_runs_total{result="failed"}`. No Pushover sent (false-positive cost too high). Cron retries tomorrow.
- **Loki returns nothing** → save a one-line "no errors in last 24h" report, push priority-0 digest. Distinguishable from failure.
- **Jess unreachable / LLM timeout** → save raw cluster data to disk anyway, push a "audit captured, diagnosis unavailable" digest. `result="partial"`.
- **Pushover disabled / fails** → write report to disk, log a warning, return success. The disk file is the source of truth; the push is best-effort.
- **Output directory not writable** → log error and continue with Pushover-only delivery. Don't lose the diagnosis just because a mount went read-only.

## Security model

- **Read-only design:** Jess can only emit text. The orchestrator never exec()s anything from her output.
- **Destructive command filter:** the prompt explicitly forbids `rm`, `dd`, `mkfs`, `format`, `drop`, `truncate`. The implementation also greps the LLM output for those tokens and tags any cluster with one as `severity=LOW, action=REJECTED-AUTO` so it's visible but flagged.
- **Loki URL** is on the trusted Tailscale network (jupiter.tail74fc4a.ts.net). No auth on the query path is intentional and documented; if Tailscale ACLs ever loosen, this needs revisiting.
- **systemd journal exposure to Promtail:** the journal mount is `:ro`. Promtail-helios already runs with `cap_drop: ALL` and `no-new-privileges`. Adding the journal mount widens its blast-radius slightly but doesn't grant new capabilities.
- **Manual trigger endpoint** (`POST /api/self_audit/run`) is bearer-gated via the existing `BearerAuthMiddleware` — not added to `PUBLIC_PREFIXES`. Additionally, `run_self_audit()` re-checks `SELF_AUDIT_ENABLED` and `JESS_ADVANCED` at function entry, so the manual route honors the same productization gate as the cron registration in `orchestrator.py` (no bypass).

## Testing checklist

- [x] Unit test: `_bucket_logs` groups identical messages, keeps top N
- [x] Unit test: `_build_audit_prompt` includes destructive-command guard rails
- [x] Unit test: `_parse_jess_diagnosis` handles missing severity / malformed lines without raising
- [x] Unit test: failure paths (Loki down, LLM down) increment correct metrics + return correct result tag
- [x] Integration: hit `POST /api/self_audit/run` manually, see Pushover digest land
- [x] Manual: induce a known error (e.g. fake a llama-server crash via `systemctl stop`), wait one cycle, verify the cluster shows up in the report
