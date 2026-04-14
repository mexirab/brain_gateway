"""
homelab-infra — cluster / GPU / disk / network / temps.

Not Brain Gateway specific. Applies to every node in the homelab cluster
(Helios, Jupiter, Saturn, Uranus). Lives alongside Brain Gateway dashboards
but will be reused for future workloads.

Metrics come from:
  - node-exporter on port 9100 (job="node-exporter") — CPU, memory, disk,
    network, hwmon. The scrape config in monitoring/prometheus/prometheus.yml
    adds a friendly `node` label (helios/jupiter/saturn/uranus) so we use
    that instead of `instance` for legends.
  - nvidia_gpu_exporter v1.4.1 on port 9400 (job="gpu-exporter") — GPU temp,
    utilization, VRAM, power. Metric names are `nvidia_smi_*`. The only
    differentiating label is `uuid`, so every GPU panel joins on
    `nvidia_smi_gpu_info` to pull in a readable `name` label.
  - bgw_temperature_* gauges from the orchestrator for the server-closet
    Sonoff sensors.
"""

from __future__ import annotations

from ..dashboard import dashboard
from ..layout import assign_ids, grid_row, row_divider
from ..panels import TEMP_THRESHOLDS, bargauge, stat, timeseries


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # --------------------------------------------------------------- Cluster
    r, y = row_divider("Cluster Overview", y)
    panels.append(r)

    cluster_row = [
        stat(
            "Nodes Online",
            'count(up{job="node-exporter"} == 1)',
            unit="none",
            graph_mode="none",
            thresholds=[(None, "red"), (3, "yellow"), (4, "green")],
        ),
        bargauge(
            "CPU Usage by Node",
            '100 - (avg by (node) (rate(node_cpu_seconds_total{mode="idle", job="node-exporter"}[5m])) * 100)',
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (70, "yellow"), (90, "red")],
        ),
        bargauge(
            "Memory Usage by Node",
            '(1 - (node_memory_MemAvailable_bytes{job="node-exporter"} / node_memory_MemTotal_bytes{job="node-exporter"})) * 100',
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (75, "yellow"), (90, "red")],
        ),
    ]
    row, y = grid_row(cluster_row, y, heights=[6, 6, 6])
    panels.extend(row)

    # ------------------------------------------------------------------ GPUs
    r, y = row_divider("GPUs", y)
    panels.append(r)

    # GPU panels join nvidia_smi_* metrics with nvidia_smi_gpu_info on uuid
    # to pull in a readable `name` label (e.g. "NVIDIA GeForce RTX 5090").
    # The `node` label comes from the prometheus.yml scrape config.
    gpu_row = [
        timeseries(
            "GPU Temperature",
            [
                (
                    "nvidia_smi_temperature_gpu * on (uuid) group_left(name) nvidia_smi_gpu_info",
                    "{{node}} {{name}}",
                )
            ],
            unit="celsius",
            thresholds=[(None, "green"), (75, "yellow"), (85, "red")],
        ),
        timeseries(
            "GPU Utilization",
            [
                (
                    "nvidia_smi_utilization_gpu_ratio * on (uuid) group_left(name) nvidia_smi_gpu_info",
                    "{{node}} {{name}}",
                )
            ],
            unit="percentunit",  # ratio is 0-1, percentunit handles the *100 + % suffix
            max_value=1,
            fill=30,
        ),
        bargauge(
            "VRAM Usage",
            "((nvidia_smi_memory_used_bytes / nvidia_smi_memory_total_bytes) * 100) "
            "* on (uuid) group_left(name) nvidia_smi_gpu_info",
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (80, "yellow"), (95, "red")],
        ),
    ]
    row, y = grid_row(gpu_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # ------------------------------------------------------- Disk / CPU temp
    r, y = row_divider("Disk + CPU Temperatures", y)
    panels.append(r)

    disk_row = [
        bargauge(
            "Disk Usage",
            '(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs", job="node-exporter"} '
            '/ node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs", job="node-exporter"})) * 100',
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (80, "yellow"), (92, "red")],
        ),
        timeseries(
            "CPU / Chipset Temps",
            [('node_hwmon_temp_celsius{job="node-exporter"}', "{{node}} {{chip}}/{{sensor}}")],
            unit="celsius",
            thresholds=[(None, "green"), (75, "yellow"), (85, "red")],
        ),
        timeseries(
            "Disk I/O",
            [
                (
                    'sum by (node) (rate(node_disk_read_bytes_total{job="node-exporter"}[5m]))',
                    "{{node}} read",
                ),
                (
                    'sum by (node) (rate(node_disk_written_bytes_total{job="node-exporter"}[5m]))',
                    "{{node}} write",
                ),
            ],
            unit="Bps",
        ),
    ]
    row, y = grid_row(disk_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # --------------------------------------------------- Server closet temps
    r, y = row_divider("Server Closet (bgw temp sensors)", y)
    panels.append(r)

    closet_row = [
        timeseries(
            "Closet vs Ambient",
            [
                ('bgw_temperature_fahrenheit{location="closet"}', "closet"),
                ('bgw_temperature_fahrenheit{location="kitchen"}', "ambient"),
            ],
            unit="fahrenheit",
            thresholds=TEMP_THRESHOLDS,
            fill=10,
        ),
        timeseries(
            "Closet − Ambient Delta",
            [("bgw_temperature_delta_fahrenheit", "delta")],
            unit="fahrenheit",
            fill=20,
        ),
    ]
    row, y = grid_row(closet_row, y, heights=[8, 8])
    panels.extend(row)

    # ----------------------------------------------------------------- Network
    r, y = row_divider("Network", y)
    panels.append(r)

    network_row = [
        timeseries(
            "Network Receive by Node",
            [
                (
                    "sum by (node) (rate(node_network_receive_bytes_total{"
                    'device!~"lo|docker.*|br-.*|veth.*", job="node-exporter"}[5m]))',
                    "{{node}}",
                )
            ],
            unit="Bps",
        ),
        timeseries(
            "Network Transmit by Node",
            [
                (
                    "sum by (node) (rate(node_network_transmit_bytes_total{"
                    'device!~"lo|docker.*|br-.*|veth.*", job="node-exporter"}[5m]))',
                    "{{node}}",
                )
            ],
            unit="Bps",
        ),
    ]
    row, y = grid_row(network_row, y, heights=[8, 8])
    panels.extend(row)

    assign_ids(panels)

    return dashboard(
        title="Homelab Infrastructure",
        uid="homelab-infra",
        description=(
            "Cluster-wide hardware telemetry — CPU, memory, GPU, disk, "
            "network, temperatures. Not Brain Gateway specific. Use for "
            "capacity planning and hardware health checks across the homelab. "
            "Generated — edit monitoring/grafana/dashgen/dashboards/homelab_infra.py, not the JSON."
        ),
        tags=["homelab", "infrastructure"],
        refresh="30s",
        time_from="now-3h",
        panels=panels,
    )
