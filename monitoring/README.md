# Monitoring Stack

Grafana + Prometheus + Loki for Brain Gateway cluster.

## Quick Start

```bash
cd /opt/gateway_mvp/monitoring
docker compose --env-file ../.env -p monitoring up -d
```

**Grafana:** http://localhost:3000 (admin / see GF_SECURITY_ADMIN_PASSWORD in .env)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Grafana | 3000 | Dashboards |
| Prometheus | 9090 | Metrics |
| Loki | 3100 | Logs |
| Promtail (Jupiter) | - | Log shipper — Jupiter-local containers (Grafana, Prometheus, Loki, Conjure, etc.) |
| Promtail (Helios) | 9080 (internal only) | Log shipper — Helios Docker containers + systemd journal (whitelisted units) → Loki on Jupiter via tailnet |
| Node Exporter | 9100 | System metrics |
| Blackbox | 9115 | HTTP probes |

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
| Helios (`promtail-helios` sidecar in `docker-compose.yml`) | Helios Docker socket + systemd journal (whitelisted units only: `llama-server`, `llama-server-coder`, `qwen-tts`, `brain-gateway`) | Loki on Jupiter via `LOKI_PUSH_URL` over Tailscale MagicDNS | `host=helios` (static) |

Use `host=helios` in LogQL to isolate Helios-origin streams. Loki stream labels set by the Helios promtail: `container`, `host`, `project`, `service`. Extracted JSON fields available via `| json`: `level`, `component`, `tool_name`, `mode`, `intensity`, `model`, `error_type`. `entity_id` and `request_id` are intentionally not labels (cardinality); query them via `| json entity_id request_id`.

**Both promtails share the same security posture:** image pinned by digest (`grafana/promtail:3.4.2@sha256:c6e9a987…`), `cap_drop: [ALL]`, `security_opt: no-new-privileges:true`, `-config.expand-env=true`. When updating the image, pin both to the new digest; don't use a floating tag. The Helios sidecar also runs on an isolated `promtail-net` Docker network — cannot reach `gateway-net` services (orchestrator, redis, open-webui) via Docker DNS.

**Helios sidecar journal scrape (added with F-014 self-audit):** The Helios promtail now also scrapes the systemd journal via new read-only mounts `/var/log/journal:ro` and `/etc/machine-id:ro`. Unit whitelist is limited to `llama-server`, `llama-server-coder`, `qwen-tts`, `brain-gateway` so journald entries from unrelated host services (sshd, cron, kernel, etc.) never enter Loki. The qwen-tts health-check heartbeat is dropped at the agent (regex drop stage) before push to keep Loki ingest sane. **Security trade-off to flag:** these two new mounts widen promtail's read surface beyond the Docker socket — promtail can now read every log line journald has, even for units outside the whitelist (the whitelist is enforced in promtail config, not by the kernel). The container is still `cap_drop: [ALL]` + `no-new-privileges` + isolated `promtail-net`, so an exploited promtail can read journal lines but cannot pivot back into the Docker network. Acceptable for the F-014 self-audit use case, but if untrusted units are ever added to the journal whitelist, revisit this.

**Level detection on non-JSON containers:** the Jupiter promtail uses a `level_fallback` regex group (not `level`) for lines that aren't JSON, so the JSON-extracted `level` label is never overwritten by the regex stage. `entity_id` is intentionally not a label (cardinality) — query it via `| json entity_id`.

## Loki Queries

```
{container="brain-orchestrator"}                          # All Helios orchestrator logs
{container="brain-orchestrator", host="helios"}           # Same, scoped to Helios origin
{container="brain-orchestrator"} |~ "tool_call"          # Tool calls
{container="brain-orchestrator"} | json | level="error"  # Errors (lowercase; orchestrator emits lowercase levels)
```

## Prometheus Targets

Configured in `prometheus/prometheus.yml.template`. After editing, run:
```bash
../scripts/generate-configs.sh
docker compose -p monitoring restart prometheus
```

## Architecture

```
Jupiter: Grafana ← Prometheus ← Loki ←── Promtail (Jupiter-local containers)
                        ↑                   ↑
         node_exporter + gpu_exporter       └── Promtail sidecar on Helios
              on all GPU nodes                    (pushes over Tailscale MagicDNS)
```
