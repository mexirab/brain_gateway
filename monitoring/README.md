# Monitoring Stack

Grafana + Prometheus + Loki for Brain Gateway cluster.

## Quick Start

```bash
cd /opt/helios/gateway_mvp/monitoring
docker compose --env-file ../.env -p monitoring up -d
```

**Grafana:** http://localhost:3000 (admin / see GF_SECURITY_ADMIN_PASSWORD in .env)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Grafana | 3000 | Dashboards |
| Prometheus | 9090 | Metrics |
| Loki | 3100 | Logs |
| Promtail | - | Log shipper |
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

## Loki Queries

```
{container="brain-orchestrator"}                    # All logs
{container="brain-orchestrator"} |~ "tool_call"    # Tool calls
{container="brain-orchestrator"} |~ "(?i)error"    # Errors
```

## Prometheus Targets

Configured in `prometheus/prometheus.yml.template`. After editing, run:
```bash
../scripts/generate-configs.sh
docker compose -p monitoring restart prometheus
```

## Architecture

```
Jupiter: Grafana ← Prometheus ← Loki ← Promtail
                        ↑
         node_exporter + gpu_exporter on all GPU nodes
```
