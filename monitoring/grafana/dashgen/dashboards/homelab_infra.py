"""
homelab-infra — cluster / GPU / disk / network / temps.

Not Brain Gateway specific. Applies to every node in the homelab cluster
(Helios, Jupiter, Saturn, Uranus). Lives alongside Brain Gateway dashboards
but will be reused for future workloads.

Metrics come from node_exporter, nvidia-gpu-exporter (or whatever gpu
exporter is in use), and the bgw_temperature_* gauges which the Brain
Gateway orchestrator exposes from the server-closet sensors.
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
            'count(up{job=~"node|node_exporter"} == 1)',
            unit="none",
            graph_mode="none",
            thresholds=[(None, "red"), (3, "yellow"), (4, "green")],
        ),
        bargauge(
            "CPU Usage by Node",
            '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (70, "yellow"), (90, "red")],
        ),
        bargauge(
            "Memory Usage by Node",
            "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
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

    gpu_row = [
        timeseries(
            "GPU Temperature",
            [("nvidia_gpu_temperature_celsius", "{{instance}} GPU{{gpu}}")],
            unit="celsius",
            thresholds=[(None, "green"), (75, "yellow"), (85, "red")],
        ),
        timeseries(
            "GPU Utilization",
            [("nvidia_gpu_duty_cycle", "{{instance}} GPU{{gpu}}")],
            unit="percent",
            max_value=100,
            fill=30,
        ),
        bargauge(
            "VRAM Usage",
            "(nvidia_gpu_memory_used_bytes / nvidia_gpu_memory_total_bytes) * 100",
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
            '(1 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"})) * 100',
            unit="percent",
            max_value=100,
            thresholds=[(None, "green"), (80, "yellow"), (92, "red")],
        ),
        timeseries(
            "CPU / Chipset Temps",
            [("node_hwmon_temp_celsius", "{{instance}} {{chip}}/{{sensor}}")],
            unit="celsius",
            thresholds=[(None, "green"), (75, "yellow"), (85, "red")],
        ),
        timeseries(
            "Disk I/O",
            [
                ("sum by (instance) (rate(node_disk_read_bytes_total[5m]))", "{{instance}} read"),
                ("sum by (instance) (rate(node_disk_written_bytes_total[5m]))", "{{instance}} write"),
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
                    'sum by (instance) (rate(node_network_receive_bytes_total{device!~"lo|docker.*|br-.*|veth.*"}[5m]))',
                    "{{instance}}",
                )
            ],
            unit="Bps",
        ),
        timeseries(
            "Network Transmit by Node",
            [
                (
                    'sum by (instance) (rate(node_network_transmit_bytes_total{device!~"lo|docker.*|br-.*|veth.*"}[5m]))',
                    "{{instance}}",
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
