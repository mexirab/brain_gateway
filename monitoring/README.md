# Brain Gateway Monitoring Stack

Grafana + Prometheus + Loki monitoring for the Brain Gateway cluster.

## Quick Start (Voyager)

```bash
cd /opt/voyager/gateway_mvp/monitoring
docker-compose -p monitoring up -d
```

Access Grafana at: **http://localhost:3000**
- Username: `admin`
- Password: `braingw`

## What's Included

| Service | Port | Purpose |
|---------|------|---------|
| Grafana | 3000 | Dashboard UI |
| Prometheus | 9090 | Metrics collection |
| Loki | 3100 | Log aggregation |
| Promtail | 9080 | Ships Docker logs to Loki |
| Node Exporter | 9100 | System metrics (Voyager) |

## Setup Node Exporter on Remote Nodes

Run this on **Uranus, Helios, Saturn, Neptune** to enable system metrics:

```bash
# Download and install node_exporter
cd /tmp
wget https://github.com/prometheus/node_exporter/releases/download/v1.7.0/node_exporter-1.7.0.linux-amd64.tar.gz
tar xzf node_exporter-1.7.0.linux-amd64.tar.gz
sudo mv node_exporter-1.7.0.linux-amd64/node_exporter /usr/local/bin/

# Create systemd service
sudo tee /etc/systemd/system/node_exporter.service > /dev/null <<EOF
[Unit]
Description=Node Exporter
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/node_exporter
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable node_exporter
sudo systemctl start node_exporter

# Verify
curl http://localhost:9100/metrics | head
```

## Setup GPU Exporter on GPU Nodes

Run this on **Uranus, Helios, Saturn, Neptune** (all nodes with GPUs):

### Option A: DCGM Exporter (Recommended for data centers)

```bash
# Requires NVIDIA driver and Docker with GPU support
docker run -d --gpus all --rm \
  -p 9400:9400 \
  --name dcgm-exporter \
  nvcr.io/nvidia/k8s/dcgm-exporter:3.3.5-3.4.0-ubuntu22.04
```

### Option B: nvidia-smi Exporter (Simpler, works everywhere)

```bash
# Download nvidia_gpu_exporter
cd /tmp
wget https://github.com/utkuozdemir/nvidia_gpu_exporter/releases/download/v1.2.0/nvidia_gpu_exporter_1.2.0_linux_amd64.tar.gz
tar xzf nvidia_gpu_exporter_1.2.0_linux_amd64.tar.gz
sudo mv nvidia_gpu_exporter /usr/local/bin/

# Create systemd service
sudo tee /etc/systemd/system/nvidia_gpu_exporter.service > /dev/null <<EOF
[Unit]
Description=NVIDIA GPU Exporter
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/nvidia_gpu_exporter
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable nvidia_gpu_exporter
sudo systemctl start nvidia_gpu_exporter

# Verify (port 9835 for nvidia_gpu_exporter)
curl http://localhost:9835/metrics | head
```

**Note:** If using nvidia_gpu_exporter, update `monitoring/prometheus/prometheus.yml` to use port `9835` instead of `9400`.

## Update Prometheus for Missing Nodes

Edit `monitoring/prometheus/prometheus.yml` and uncomment/update the Saturn and Neptune entries with their IPs:

```yaml
# Saturn (Medium models, RTX 5080)
- targets: ['10.0.0.XXX:9100']  # <-- Update IP
  labels:
    node: 'saturn'
    role: 'batch'
    gpu: 'rtx5080'
```

Then reload Prometheus:
```bash
curl -X POST http://localhost:9090/-/reload
```

## Grafana Dashboards

### Pre-built Dashboard
The **Brain Gateway Overview** dashboard is auto-loaded and shows:
- Cluster node status (online/offline)
- CPU/Memory/Disk usage per node
- GPU VRAM, utilization, temperature
- Orchestrator logs (all logs + filtered tool calls)

### Useful Log Queries (Loki)

**All orchestrator logs:**
```
{container="brain-orchestrator"}
```

**Tool calls only:**
```
{container="brain-orchestrator"} |~ "tool_call|<tool_call>"
```

**Home Assistant calls:**
```
{container="brain-orchestrator"} |~ "home_assistant"
```

**Expert model calls:**
```
{container="brain-orchestrator"} |~ "ask_expert"
```

**RAG/Memory searches:**
```
{container="brain-orchestrator"} |~ "search_memory"
```

**Errors only:**
```
{container="brain-orchestrator"} |~ "(?i)error|exception|failed"
```

## Troubleshooting

### Prometheus can't reach node exporters
```bash
# Check if firewall is blocking port 9100
sudo ufw status
sudo ufw allow 9100/tcp  # If needed
```

### Loki not receiving logs
```bash
# Check promtail is running
docker logs promtail --tail 20

# Verify Docker socket access
docker exec promtail ls -la /var/run/docker.sock
```

### Dashboard shows "No data"
1. Check Prometheus targets: http://localhost:9090/targets
2. Check Loki is receiving data: http://localhost:3100/ready
3. Verify time range in Grafana (default is last 1 hour)

## Ports Summary

| Port | Service | Firewall Needed |
|------|---------|-----------------|
| 3000 | Grafana | Only Voyager |
| 9090 | Prometheus | Only Voyager |
| 3100 | Loki | Only Voyager |
| 9100 | Node Exporter | All nodes |
| 9400 | DCGM Exporter | GPU nodes |
| 9835 | nvidia_gpu_exporter | GPU nodes (alt) |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                      VOYAGER                            │
│  ┌─────────┐  ┌────────────┐  ┌──────┐  ┌──────────┐   │
│  │ Grafana │◄─│ Prometheus │◄─│ Loki │◄─│ Promtail │   │
│  └─────────┘  └────────────┘  └──────┘  └──────────┘   │
│       │              │                        │         │
│       │              │                   Docker logs    │
│       └──────────────┼────────────────────────┘         │
│                      │                                  │
└──────────────────────┼──────────────────────────────────┘
                       │ scrape metrics
       ┌───────────────┼───────────────┬──────────────────┐
       ▼               ▼               ▼                  ▼
┌────────────┐  ┌────────────┐  ┌────────────┐    ┌────────────┐
│   URANUS   │  │   HELIOS   │  │   SATURN   │    │  NEPTUNE   │
│ node:9100  │  │ node:9100  │  │ node:9100  │    │ node:9100  │
│ gpu:9400   │  │ gpu:9400   │  │ gpu:9400   │    │ gpu:9400   │
│ (Nemotron) │  │ (Expert)   │  │ (Batch)    │    │ (Backup)   │
└────────────┘  └────────────┘  └────────────┘    └────────────┘
```
