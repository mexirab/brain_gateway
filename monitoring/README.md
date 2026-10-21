# Monitoring Stack

Grafana + Prometheus + Alertmanager + Loki for Brain Gateway cluster.

## Quick Start

```bash
cd ~/gateway_nerves
bash scripts/generate-configs.sh   # render configs first — compose bind-mounts them
docker compose -f monitoring/docker-compose.yml --env-file .env -p monitoring up -d
```

- **Every** compose command for this project needs the env file (`--env-file .env` from the repo root, `--env-file ../.env` from `monitoring/`) — `GF_SECURITY_ADMIN_PASSWORD` is required (`:?`) and compose refuses to start without it.
- Bring the main stack up first: Loki joins the external `gateway_nerves_gateway-net` network and blackbox-exporter the external `gateway_nerves_frontend-probe-net` (both created by the main compose). If the main stack was `down`'d (network deleted/recreated), re-run `up -d` here.

**Network exposure:** ufw is inactive on Jupiter, so the publish address is the only control. Grafana, Prometheus, Loki, Alertmanager and Blackbox all bind `127.0.0.1` only; the dashboard too once `FRONTEND_BIND_ADDR=127.0.0.1` (served via Tailscale Serve HTTPS).

**`frontend-probe-net`:** holds only `brain-frontend` and `blackbox-exporter`. Deliberately not `gateway-net`: blackbox `/probe` is an unauthenticated request proxy, and on `gateway-net` searxng/redis/orchestrator could drive it (and through it Prometheus' lifecycle API).

**Grafana:** https://jupiter-amds.tail74fc4a.ts.net:3000 (Tailscale Serve, tailnet only) or `http://localhost:3000` from Jupiter. Login required (anonymous access off): `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` from `.env`. The password only seeds a fresh `grafana.db` — to change it run `docker exec grafana grafana cli admin reset-admin-password '<new>'` and update `.env`. `GRAFANA_ROOT_URL` sets `GF_SERVER_ROOT_URL`; `GF_SECURITY_COOKIE_SECURE=true`.

**Prometheus:** no browser URL — `http://localhost:9090` from Jupiter only (`--web.enable-lifecycle` makes `/-/quit` reachable by anyone who can reach the port). Grafana uses `prometheus:9090` over `monitoring-net`.

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Grafana | 3000 (127.0.0.1 only; tailnet via Tailscale Serve HTTPS) | Dashboards |
| Prometheus | 9090 (127.0.0.1 only) | Metrics |
| Alertmanager | 9093 (127.0.0.1 only) | Alert routing → Pushover |
| Loki | 3100 (127.0.0.1 only; containers use `loki:3100`) | Logs |
| Promtail (Jupiter) | - | Log shipper — Jupiter-local containers (Grafana, Prometheus, Loki, Conjure, etc.) |
| Promtail (Helios) | 9080 (internal only) | Log shipper — Helios systemd journal → `loki-push-gw` on Jupiter (own compose project on Helios, `/opt/promtail-helios`) |
| loki-push-gw | 3101 (`${NODE_JUPITER_IP}`, LAN; loopback if unset) | Push-only nginx in front of Loki for Helios promtail (Helios IP + bearer token) |
| Node Exporter | 9100 | System metrics |
| Blackbox | 9115 (127.0.0.1 only) | HTTP probes |

## Remote Node Setup

Install on Helios, Uranus, Saturn:

```bash
# Node exporter
wget -qO- https://github.com/prometheus/node_exporter/releases/download/v1.7.0/node_exporter-1.7.0.linux-amd64.tar.gz | sudo tar xz -C /usr/local/bin --strip-components=1

# GPU exporter
wget -qO- https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v1.2.0/nvidia_gpu_exporter_1.2.0_linux_amd64.tar.gz | sudo tar xz -C /usr/local/bin

# Create systemd services (see scripts in this dir)
sudo systemctl enable --now node_exporter nvidia_gpu_exporter
```

## Log Pipeline (two-promtail topology)

Helios and Jupiter each run their own promtail instance. Logs from both hosts land in Loki on Jupiter.

| Promtail | Scrapes | Pushes to | `host` label |
|----------|---------|-----------|--------------|
| Jupiter (monitoring stack) | Jupiter Docker socket + journal | local Loki | (Jupiter default, no static override) |
| Helios (`promtail-helios`, own compose project `monitoring/promtail/docker-compose.helios.yml`, deployed to `/opt/promtail-helios`) | Helios systemd journal only (no Docker job, no Docker socket) | `loki-push-gw` on Jupiter (`LOKI_PUSH_URL`, default `http://10.0.0.248:3101/loki/api/v1/push`, LAN, bearer `LOKI_PUSH_TOKEN`) | `host=helios` (static) |

**Loki reachability:** loopback-only publish. Grafana + Jupiter promtail reach it as `loki:3100` over `monitoring-net`; the orchestrator reaches it as `loki:3100` over `gateway-net` (`SELF_AUDIT_LOKI_URL=http://loki:3100` — containers can't reach Tailscale Serve on the host's own tailnet IP). The only off-box path in is `loki-push-gw` (below); there is no tailnet forward of 3100. Loki has no auth, so the delete API (`deletion_mode: disabled`) and ruler API (`enable_api: false`) are off — an open delete API could wipe error streams and make the self-audit report a false "all clean".

### Helios journal job (`promtail/promtail-helios.yml`)

- **Allow-list:** units `llama-server-primary`, `voxcpm-tts`, `qwen-tts`, `stt-onnx`, `nut-*`; systemd's own lines about those units (`UNIT=`); kernel messages.
- **Labels:** `host=helios`, `job=systemd-journal`, `service` (unit name without `.service`; kernel lines → `service=kernel`), `level` (from journal priority; `err` → `error`, `crit|alert|emerg` → `critical`), `transport`.
- **Level re-derivation:** journald stores the model units' stdout as info, so `level` is re-set from the line format (llama.cpp `E `/`W `, Python `[ERROR]`/`[WARNING]`, uvicorn `ERROR:`, `Traceback`). llama.cpp's fixed startup notices (no API key, Qwen-VL image-token hint, etc.) stay info.
- **Drops:** `GET /health` lines, the old qwen-tts `Stream TTS failed for sentence` line (logged sentence text; fixed in `tts/server.py`, backstop for older deployed copies), lines over 2 KB.
- **No rate limit on `nut-*`:** a promtail `limit` stage dropped every UPS line during the startup backfill (2026-10-17), so it was removed. A UPS USB-link flap (~1800 warnings/h) is evidence worth keeping; the self-audit should query Helios separately rather than rely on one 5000-line window (**not done yet** — open follow-up; until then a long flap can push older errors out of the audit).
- **No backslashes in match selectors:** `-config.expand-env=true` collapses `\\` to `\` before LogQL parses them, so `\S` fails as an invalid escape and promtail refuses to start. Use bracket classes (`[^ ]`, `[[]`, `[]]`).
- **Backoff:** 30 retries, ≤5 min apart (rides out a ~2 h Jupiter outage). 4xx (gateway 403, Loki 400) are never retried — those lines are lost.
- **Requires a persistent journal** on Helios (`/var/log/journal` must exist; a volatile `/run/log/journal`-only host ships nothing).
- No personal content: these units log request shapes/timings only. Keep it that way (Loki keeps 30 d; the self-audit sends WARNING+ to the LLM + Pushover).

### loki-push-gw (Jupiter)

`nginxinc/nginx-unprivileged:1.28` (digest-pinned) in `monitoring/docker-compose.yml`, template `loki-push-gw/templates/default.conf.template`.

- Published on `${NODE_JUPITER_IP:-127.0.0.1}:3101` — LAN only when `NODE_JUPITER_IP` is set.
- Allows only `POST /loki/api/v1/push`, only from `${NODE_HELIOS_IP:-127.0.0.1}`, only with `Authorization: Bearer ${LOKI_PUSH_TOKEN}`. Empty token → every push is 403. Everything else 404/403.
- Rate-limited (5 r/s, burst 50), 8 MiB body cap, access log off.
- Own network `loki-push-net` (Loki only) — cannot reach Alertmanager, Prometheus or blackbox.
- Not in the Jupiter promtail keep-regex on purpose (its error log echoes attacker-chosen URIs).
- Residual risk: plain http on the LAN; planted lines would reach the self-audit LLM.

### Deploy promtail on Helios

```bash
# From the repo on Jupiter
rsync -a monitoring/promtail/{docker-compose.helios.yml,promtail-helios.yml} \
    labadmin@helios:/opt/promtail-helios/
# On Helios: /opt/promtail-helios/.env (chmod 600) with
#   LOKI_PUSH_TOKEN=<same value as Jupiter .env>
#   LOKI_PUSH_URL=...   (only if Jupiter's address differs from the default)
ssh labadmin@helios 'cd /opt/promtail-helios && \
    docker compose -f docker-compose.helios.yml -p promtail-helios up -d'
```

Jupiter `.env` needs `LOKI_PUSH_TOKEN` and `NODE_HELIOS_IP` (gateway allow-list) — then `docker compose --env-file ../.env up -d loki-push-gw` from `monitoring/`. Check: `{host="helios"}` in Grafana Explore.

**Both promtails share the same security posture:** image pinned by digest (`grafana/promtail:3.4.2@sha256:c6e9a987…`), `cap_drop: [ALL]`, `security_opt: no-new-privileges:true`, `-config.expand-env=true`. When updating the image, pin both to the new digest; don't use a floating tag. The Helios promtail also runs read-only on an isolated `promtail-net` network and has no Docker socket (`:ro` doesn't restrict the Docker API — it would mean root on Helios).

**Radicale is excluded from Loki on purpose** — the Jupiter promtail keep-regex doesn't match the `radicale` container (request lines carry paths, client IPs and login usernames). Don't widen the regex; use `docker logs radicale`. See `docs/CALENDAR.md`.

**Level detection on non-JSON containers:** the Jupiter promtail uses a `level_fallback` regex group (not `level`) for lines that aren't JSON, so the JSON-extracted `level` label is never overwritten by the regex stage. `entity_id` is intentionally not a label (cardinality) — query it via `| json entity_id`.

## Loki Queries

```
{container="brain-orchestrator"}                          # All orchestrator logs (Jupiter)
{host="helios", service="llama-server-primary"}           # Brain (llama.cpp) journal
{host="helios", service="kernel"}                         # Helios kernel log
{host="helios", level=~"error|critical"}                  # Helios errors
{container="brain-orchestrator"} |~ "tool_call"          # Tool calls
{container="brain-orchestrator"} | json | level="error"  # Errors (lowercase; orchestrator emits lowercase levels)
```

## Rendered Configs (templates → live bind mounts)

`prometheus/prometheus.yml` and `alertmanager/alertmanager.yml` are GENERATED by `scripts/generate-configs.sh` from their `.template` siblings — edit the templates, never the renders. The alertmanager render is gitignored (it carries `PUSHOVER_USER_KEY`/`PUSHOVER_APP_TOKEN` injected from `.env`; mode 644 because the container runs as nobody — same posture as `prometheus/api_token`).

The generator renders to a temp file, **validates it first** (`promtool check config` / `amtool check-config` via the digest-pinned images from the compose file; promtool also syntax-checks `alert-rules.yml`), then cat-truncates the live file in place. Missing Pushover keys → the alertmanager render is skipped loudly (an empty-key render can't load and would crash-loop the container at the next restart). Missing template, or a root-owned/directory output → hard fail with fix instructions.

After editing a template, `prometheus/alert-rules.yml`, or `blackbox/blackbox.yml`:

```bash
bash scripts/generate-configs.sh     # render + validate
bash scripts/reload-monitoring.sh    # reload + verify (PROM_URL / AM_URL / BLACKBOX_URL env overrides)
```

`reload-monitoring.sh` verifies CONTENT, not just HTTP 200: every `- alert:` name in `alert-rules.yml` must appear in Prometheus `/api/v1/rules`, and the Alertmanager loaded config (`/api/v2/status`, secrets masked) must structurally match the render (receiver names + normalized route matchers), and every module in `blackbox/blackbox.yml` must be loaded in the Blackbox exporter's `/config` with each setting we wrote at the same value (subset match — `/config` adds defaults and omits zero values; `blackbox.yml` is not templated, so the checkout file is the live config). An invalid `blackbox.yml` makes `/-/reload` return 500; the script fails and the exporter keeps its previous config. A 200 reload against a stale mount is exactly how the pre-compose alertmanager rotted — it silently dropped `EveningBriefingStale` paging during PR #54.

Before reloading, `reload-monitoring.sh` also **fails if `alertmanager.yml` or `prometheus.yml` is older than its `.template`** (stale render — the content checks compare live config against the render, so a render that never happened passes them; bit us 2026-10-02 when `generate-configs.sh` aborted and the new `SelfAudit*` routes never went live).

**`.env` is sourced with bash:** `generate-configs.sh` `source`s `.env`, so any value containing a space must be quoted (e.g. `ACTUAL_BUDGET_FILE='My Budget'`) — an unquoted one aborts the render (the 2026-10-02 failure above).

**CI deploys this automatically:** on merge to main, changed files under `monitoring/prometheus/`, `monitoring/alertmanager/`, `monitoring/blackbox/`, `monitoring/loki/`, `monitoring/loki-push-gw/`, `monitoring/docker-compose.yml`, or `scripts/{generate-configs,reload-monitoring}.sh` trigger render → compose `up -d --remove-orphans` (only if the compose file changed) → reload + verify. A `loki/loki-config.yml` change force-recreates `loki`; a `loki-push-gw/` change force-recreates `loki-push-gw`; rule files need no restart. The job fails if Loki `/ready` doesn't answer within 60 s or the gateway's push location didn't render.

**Directory mounts, not single-file mounts:** prometheus, alertmanager and blackbox-exporter mount `./prometheus`, `./alertmanager` and `./blackbox` as directories. A single-file bind mount is pinned to the inode at container start, and `git pull` / most editors replace files with new inodes — the container keeps reading the pre-edit file while `/-/reload` reports success (bit us 2026-07-06 with an invisible `alert-rules.yml` edit, and again 2026-10-15 when PR #83's new `http_2xx_http10` blackbox module stayed unloaded — and `blackbox-http10` DOWN — until a manual restart). Directory mounts resolve inner files by path on every access. Same reason `generate-configs.sh` truncates renders in place — never `rm`/`mv` a rendered config.

## Alertmanager

Compose-managed since 2026-07-06 (digest-pinned image, `cap_drop: ALL`, `no-new-privileges`; replaced a hand-run container that bind-mounted a stale root-owned render from the abandoned `/opt/gateway_mvp` checkout — repo template changes were inert). Routes alerts to Pushover in two tiers by alertname **allow-list** (the `severity` label routes nothing; a rule not named in a matcher falls through to the `null` receiver and notifies nobody): paging alerts (e.g. `OrchestratorDown`, `NodeDown` (excludes `helios` — power-tiered, asleep by design — and the dead `uranus`), the briefing/backup staleness alerts, mount-missing alerts, `EmbeddingModelBroken`, `ClosetTemperatureCritical`, `HeliosPoweredButUnreachable`, `UPSOnBattery`, `UPSLowBattery`) hit the `pushover` receiver (priority 1), while non-paging alerts (`WindDownNudgeStale`, `JessBackupOffboxFailing` (matches `jess_(ha_|calendar_|actual_)?backup_offbox_success` — state, HA, calendar and Actual Budget backups), `RadicaleDown`, `ActualDown`, `FrontendDown`, `CalendarSourceFailing`, `ChatStreamTruncating`, `EmptyRepliesReachingUsers`, `ToolCallsSilentlyDropped`, `BudgetSyncFailing`, `SelfAuditStale`, `SelfAuditFailing`, `HeliosHardwareFault`, `BrainCudaInitFailed`, `BrainOnCPU`, `HeliosGpuDriverBroken`, `HeliosLogsSilent`, `HeliosLogCheckFailing`, `EmbeddingErrors`, `AutoLearnFailing`, `ClosetTemperatureSensorMissing`, `WebPushFailing`, `UPSExporterDown`, `UPSReplaceBattery`, `UPSOverload` — see the matcher in `alertmanager/alertmanager.yml.template`) route to `pushover-quiet` (priority 0 — respects Pushover quiet hours, no emergency retry, so a midnight fire surfaces in the morning instead of waking anyone). `AnnouncementSpeakerUnavailable` (per-`speaker` `increase(bgw_announcement_speaker_unavailable_total[24h]) > 0`, `for: 15m` — HA reports a routed speaker `unavailable`, so announcements skip it) has its own route: `pushover-quiet` with `repeat_interval: 24h` (a dead speaker stays dead for days and is hit all day; the default 4h repeat would nag several times daily). See `docs/VOICE_AND_TTS.md` → Speaker liveness. `ExchangeCalendarSyncStale` has the same `pushover-quiet` + `repeat_interval: 24h` route (the fix is the owner's iPhone Shortcut). So does `HeliosUnreachablePlugBlind` (the outage detector is blind — a config/HA fix). Storage at `/mnt/logging/alertmanager` (owned `65534:65534`).

**`self-investigator` receiver (optional):** the first route copies `HeliosHardwareFault`, `ChatStreamTruncating`, `EmptyRepliesReachingUsers`, `ToolCallsSilentlyDropped`, `EmbeddingErrors`, `AutoLearnFailing`, `SchedulerJobsMissed`, `HeliosLogsSilent` (`continue: true`, so their Pushover route still fires) to a webhook at `http://${NODE_JUPITER_IP}:${SERVICE_ORCHESTRATOR_PORT}/api/self_audit/alert` — the orchestrator's read-only investigator (`jess-features/F-014-self-audit.md`). Bearer from `credentials_file: /etc/alertmanager/self_audit_webhook_token`; the orchestrator accepts that token on that path only. `generate-configs.sh` keeps the `BEGIN/END self-investigator` blocks and writes `alertmanager/self_audit_webhook_token` (644, gitignored, truncated in place) **only when `.env` has `SELF_AUDIT_WEBHOOK_TOKEN` ≥ 24 chars AND `SELF_AUDIT_INVESTIGATE=true`**; otherwise it strips both blocks (every delivery would be a 401/409 that Alertmanager re-sends each group interval). Keep the matcher in sync with `ALERT_TRIGGERS` in `orchestrator/routes_self_audit.py`. Outcomes: `bgw_self_audit_alert_trigger_total{result}`.

`EmbeddingModelBroken` (`bgw_embedding_healthy == 0` for 5m, set by the orchestrator's startup embedding self-test) pages because a broken embedding model silently takes out all memory (search, RAG context, palace writes, auto_learn) — it went unnoticed for ~12 days in Oct 2026. Startup-only signal; its runtime twin `EmbeddingErrors` (quiet, `sum(increase(bgw_embedding_errors_total[1h])) >= 3`, `keep_firing_for: 2h`) catches failures that start after boot. `AutoLearnFailing` (quiet) fires on ≥3 non-empty `bgw_auto_learn_errors_total` in 24h with no successful `bgw_auto_learn_runs_total` run. **Inhibit rules** (`inhibit_rules` in `alertmanager.yml.template`) so one embedding outage sends one notice: `EmbeddingModelBroken` mutes `EmbeddingErrors` + `AutoLearnFailing`; `EmbeddingErrors` mutes `AutoLearnFailing`. See `docs/MEMPALACE.md` → Embedding model + Auto-learn.

Port 9093 binds to **127.0.0.1 only** — the API is unauthenticated (anyone reaching it can create silences or inject fake pushes), so the loopback bind is the exposure control. The web UI is NOT reachable from LAN/tailnet; Prometheus/Grafana reach it in-network as `alertmanager:9093` (a Grafana Alertmanager datasource is the planned single-pane view — see ROADMAP). Inspect from Jupiter:

```bash
curl -s localhost:9093/api/v2/status | jq .
docker exec alertmanager amtool alert query      # active alerts
docker exec alertmanager amtool silence query    # active silences
```

`AlertmanagerUnreachable` in `alert-rules.yml` fires when Prometheus loses its Alertmanager — dashboard-only by design (routing it via alertmanager would be circular).

## Prometheus Targets

Configured in `prometheus/prometheus.yml.template`. After editing, render + reload (see Rendered Configs above) — or just merge to main and let CI do it.

- **`llama-primary`** (added 2026-10-02): scrapes the primary brain's `llama-server --metrics` endpoint at `10.0.0.195:8080/metrics` every 30 s. Useful series: `llamacpp:requests_deferred` / `llamacpp:requests_processing` (the `--parallel 1` queue — the one failure mode the llama.cpp cutover introduced), the spec-decode draft/accepted counters (MTP acceptance), `llamacpp:prompt_tokens_cached_total` vs `llamacpp:prompt_tokens_total` (this build does not export `kv_cache_usage_ratio`). Helios is power-tiered and asleep most of the day, so this target is down by design most of the time — dashboard signal only, no target-down alert (same posture as the Helios node-exporter / gpu-exporter targets). Grafana row **"Brain Server (llama-server)"** (`brain_gateway_sre.py`, after "LLM Performance"): prompt-cache hit ratio = cached / (cached + `prompt_tokens_total`) over 1h — this build's `prompt_tokens_total` excludes cache hits; typical ~0.8 with chat, ~0.5 when background-heavy; decode + prefill tok/s over busy time (15m); MTP draft acceptance overall + per position; "Brain Queue (llama-server)" (moved from the Pantry row). Panels on this row use `span_nulls=False` (`panels.timeseries()` param, default True) so lines don't bridge Helios sleep.
- **`blackbox-http10`** (added 2026-10-14): probes `http://radicale:5232/.web/` with the `http_2xx_http10` blackbox module (Radicale answers HTTP/1.0, which the default `http_2xx` module rejects); labels `service=radicale`, `tier=app`. Drives `RadicaleDown` (quiet). The calendar backup has its own paging `JessCalendarBackupStale` alert (textfile metrics `jess_calendar_backup_{success_timestamp_seconds,offbox_success,items}`). See `docs/CALENDAR.md`.
- **Frontend** blackbox probe (`service=frontend`, no `tier` label so `AppDown` doesn't double up; `http://brain-frontend:3001/login` over `frontend-probe-net` — `/login` is public and app-rendered): drives `FrontendDown` (quiet `pushover-quiet`, warning, 5m). Does NOT cover the Tailscale Serve hop or the publish address. "no such host" in a debug probe = blackbox-exporter is off `frontend-probe-net` → `docker compose --env-file ../.env up -d blackbox-exporter` from `monitoring/`.
- **Actual Budget** blackbox probe (`service=actual`, `http://actual-budget:5006/health` over `monitoring-net`): drives `ActualDown` (quiet, 5m). The Actual backup has its own paging `JessActualBackupStale` (textfile metrics `jess_actual_backup_{success_timestamp_seconds,offbox_success,files}`). See `docs/BACKUP.md` → Actual Budget.
- **Calendar sources (orchestrator metrics, PR #83 — not deployed):** `CalendarSourceFailing` (quiet; replaces the dead Google-only `CalendarPollingFailed`) fires when `bgw_calendar_source_fetch_total{source=~"caldav|google",result=~"error|timeout"}` increases >6 in 1h for 15m — catches what the probe can't (401 after a password change, REPORT errors). `ExchangeCalendarSyncStale` (deadmans group, quiet, daily repeat) fires when `bgw_phone_calendar_last_sync_timestamp_seconds` is >26h old (+30m `for`); silent while 0 (work Shortcut never synced, or only the retired legacy push). Grafana row **"Calendar Sources"** (`brain_gateway_sre.py`): reads by source/result, success ratio, read latency p95, work-sync age, iPhone push rejections by reason, writes by target/result, Radicale probe. The older Google-era panels are renamed "Calendar tools: check/create …".
- **`tts-voxcpm`** blackbox probe: `http://<helios>:8004/health` (VoxCPM2, `tts/voxcpm-tts.service`; `/health` 503s when no voice is usable). `TTSDown` now matches `probe_success{service=~"tts|tts-voxcpm"}` — both Qwen3-TTS (:8002) and VoxCPM2 alert while both run; whichever `TTS_URL` points at is the live voice. Port hardcoded (no `SERVICE_*` template var). See `docs/VOICE_AND_TTS.md` → VoxCPM2 TTS server.
- **Dashboard inbox + Web Push:** `WebPushFailing` (quiet `pushover-quiet`, warning, `for: 30m`) fires when `bgw_webpush_send_total` shows failures (`auth|http_4xx|http_5xx|timeout|redirect|error`) and no `ok` in 6h — Web Push counts as phone delivery while a device is recently healthy, so a rejecting push service would mark reminders delivered into the void. `result=auth` = VAPID key (`WEBPUSH_KEY_PATH`) changed → re-enable on each device. Silent when nothing was sent. Grafana row **"Dashboard Inbox & Web Push"** (`brain_gateway_sre.py`): devices (`bgw_webpush_subscriptions`), cards added by kind, cards resolved by via/resolution (cards climbing with no `dashboard` resolutions = nobody sees the tray), sends by result; web push ok/fail lines also in the Push Channels panels.
- **`nut`** (UPS, Helios :9199): one UPS since 2026-10-21 — Goldenmate 2000VA LiFePO4 (CyberPower CP1500PFCLCD retired). Firmware exposes only status flags + battery charge/runtime (no load, no voltages), so `UPSInputVoltageOutOfRange` is gone and `UPSOverload` uses the `OVER` flag only; the UPS dashboard shows only those metrics. `UPSExporterDown` is gated on `bgw_helios_running == 1` (silent while Helios sleeps). Routing: `UPSOnBattery` + `UPSLowBattery` page; `UPSExporterDown`, `UPSReplaceBattery`, `UPSOverload` → `pushover-quiet`. Before 2026-10-21 no UPS alert was routed (all to `null`) and the exporter sat dead from 2026-07-24 unnoticed. **Known gap:** the exporter still runs from the stale `/opt/gateway_mvp` compose project on Helios (`restart: unless-stopped`); follow-up is its own compose project like `promtail-helios`.
- **HA unknown-entity refusals:** Tools-row panel "HA: Unknown entity_id refusals (/1h)" graphs `bgw_ha_unknown_entity_total`; the metric ships with branch `fix/ha-entity-listing` — no data until that deploys.
- **HA device-setting guard:** Tools-row panels "HA: Guard refusals (/1h)" (all refusals `bgw_ha_call_refused_total` by path + device-setting refusals `bgw_ha_device_setting_refused_total{path,source}`; a `path=tool` burst can mean prompt injection) and stat "HA: Registry entities" (`bgw_ha_registry_entities`; 0 = registry never read, guard falls back to name-only checks). No alerts.

## Loki Ruler (log-based alerts)

Enabled in `loki/loki-config.yml`: local rule storage from `loki/rules/fake/` (`fake` = tenant id when `auth_enabled: false`), mounted read-only as a directory (re-read every minute — no restart for rule edits), `evaluation_interval: 1m`, alerts to `http://alertmanager:9093`, `enable_api: false`, Loki `log_level: warn` (the ruler is chatty at info). Ruler alerts route through the same Alertmanager allow-list but do **not** appear in Prometheus `ALERTS` and are not checked by `reload-monitoring.sh`.

## Helios Health Alerts

`NodeDown` excludes Helios (asleep most of the day by design), so `HeliosPoweredButUnreachable` is the **paging** signal for a hung Helios (`pushover`); `HeliosUnreachablePlugBlind` is `pushover-quiet` with a 24h repeat; the rest route to `pushover-quiet`. Written to stay silent while Helios is deliberately asleep. Paging for Helios outages needs `HELIOS_PLUG_MONITOR_ENABLED` (or `HELIOS_WAKE_ENABLED`) on the orchestrator — with both off, neither plug alert can tell sleep from an outage (PlugBlind fires instead).

| Alert | Source | Fires when | First check |
|-------|--------|-----------|-------------|
| `HeliosHardwareFault` | Loki ruler (`loki/rules/fake/helios.yml`) | Kernel line matching IO_PAGE_FAULT / HC died / host controller not responding / NVRM Xid / PCIe AER / Hardware Error (MCE) in 10 min | `journalctl -k -b \| grep -E "IO_PAGE_FAULT\|xhci\|atlantic\|Xid\|AER"`; plan a reboot before the NIC dies. Backfilled lines after a NIC outage keep old timestamps and won't fire it — `HeliosPoweredButUnreachable` covers that |
| `BrainCudaInitFailed` | Loki ruler | `llama-server-primary` logged a CUDA init failure in 15 min. Auto-resolves after 15 min regardless of state | `nvidia-smi` on Helios; reboot loads the matching module. `HeliosGpuDriverBroken` tracks whether it's fixed |
| `BrainOnCPU` | Prometheus `helios_health` | Mean decode < 30 tok/s over 15 m (normal ~105–125) | `nvidia-smi`; `journalctl -u llama-server-primary -b \| grep -i cuda`; reboot |
| `HeliosGpuDriverBroken` | Prometheus `helios_health` | `nvidia_smi_command_exit_code{node="helios"} != 0` for 5 m (18 = driver/library mismatch, seen 2026-10-15) | Reboot Helios; check `apt-mark showhold` still holds the 580-server packages |
| `HeliosLogsSilent` | Prometheus `helios_health` | `bgw_helios_logs_silent == 1` (orchestrator 5-min check: no `llama-server-primary` lines from Helios in Loki for 30 min) AND `increase(llamacpp:tokens_predicted_total[30m]) >= 1`, for 15 m. Idle Helios is silent by design, so it only counts while the brain is generating. Needs `SELF_AUDIT_ENABLED` + `JESS_ADVANCED` (the check runs with the self-audit) | On Helios `docker logs promtail-helios` (in `/opt/promtail-helios`); on Jupiter `docker logs loki-push-gw`. While it fires, Helios errors are invisible to the self-audit and the Loki ruler |
| `HeliosLogCheckFailing` | Prometheus `helios_health` | ≥ 5 `bgw_helios_log_check_total{result="error"}` in 30 min, for 5 m. Also the only "Loki unreachable from the orchestrator" signal (Loki has no alert of its own); `HeliosLogsSilent` is blind meanwhile | `docker ps --filter name=loki`; `docker logs loki --tail 50`; `SELF_AUDIT_LOKI_URL` (`http://loki:3100`) resolves from `brain-orchestrator` |
| `HeliosPoweredButUnreachable` (**pages**, critical) | Prometheus `helios_health` | Plug > 60 W, `bgw_helios_running == 1`, an ok plug read in 10 m, Helios node-exporter down, for 5 m (`bgw_helios_*` pinned to `job="orchestrator"`) | Hang or dead NIC — attach a monitor; power-cycle via the plug. Needs `HELIOS_PLUG_MONITOR_ENABLED` (or `HELIOS_WAKE_ENABLED`) on the orchestrator, else never fires |
| `HeliosUnreachablePlugBlind` | Prometheus `helios_health` | Helios node-exporter down AND plug reads stale (no ok read in 10 m — poll disabled, HA down, orchestrator down) or incoherent (`running == 0` with watts ≥ 30 = switch unavailable while the sensor reports a draw; or 30–60 W), or unreadable (plug/sensor unavailable/unknown → watts NaN, `running == 0`; also NaN after an orchestrator restart until the first poll), for 30 m. 24h repeat | `HELIOS_PLUG_MONITOR_ENABLED` / `HELIOS_WAKE_ENABLED` on the orchestrator; the plug + power sensor in HA; `docker logs brain-orchestrator \| grep HELIOS`. Switch `off` exports 0 W even with the sensor unreadable. **Remaining gap:** a power sensor stuck at a stale standby number while Helios is up and hung (the 2026-10-17 post-restart blind spot is closed by the NaN export) |

## Architecture

```
Jupiter: Grafana ← Prometheus ← Loki ←── Promtail (Jupiter-local containers)
                        ↑              ↑ │
         node_exporter + gpu_exporter  │ └── ruler ──> Alertmanager
              on all GPU nodes         └── loki-push-gw :3101 (LAN, Helios IP + token)
                                              ↑
                                         Promtail on Helios (journald)

         Prometheus ── alerts ──> Alertmanager (127.0.0.1:9093) ──> Pushover
```
