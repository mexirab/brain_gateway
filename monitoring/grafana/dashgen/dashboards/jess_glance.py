"""
jess-glance — kiosk + phone dashboard.

Goal: single screen, no scroll at 1920x1080. What Nadim actually looks at
day-to-day. Four zones top-to-bottom:

  1. Stat strip (8 stats, h=4)          — health-at-a-glance
  2. Hero timeseries row 1 (2 panels, h=8) — request latency + tool calls
  3. Hero timeseries row 2 (2 panels, h=8) — LLM errors + temperatures
  4. Logs row (3 Loki panels, h=8)       — flow / tool exec / errors

Total grid height: 4 + 8 + 8 + 8 = 28 rows. At default Grafana zoom that
fits comfortably on 1080p with the Grafana chrome on top.
"""

from __future__ import annotations

from ..dashboard import dashboard
from ..layout import assign_ids, grid_row
from ..panels import (
    GREEN_RED_BINARY,
    GREEN_YELLOW_RED,
    LATENCY_THRESHOLDS_S,
    TEMP_THRESHOLDS,
    logs,
    stat,
    timeseries,
)


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # ------------------------------------------------------------------ Stats
    # 8 stats across the top, each 3 columns wide, 4 tall.
    stats = [
        stat(
            "Request Rate",
            "sum(rate(bgw_requests_total[5m])) * 60",
            unit="none",
            decimals=1,
            graph_mode="area",
            thresholds=[(None, "green"), (30, "yellow"), (120, "red")],
        ),
        stat(
            "p95 Latency",
            "histogram_quantile(0.95, sum by (le) (rate(bgw_request_duration_seconds_bucket[5m])))",
            unit="s",
            decimals=1,
            thresholds=LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Error %",
            "(sum(rate(bgw_request_errors_total[5m])) / clamp_min(sum(rate(bgw_requests_total[5m])), 0.001)) * 100",
            unit="percent",
            decimals=1,
            thresholds=GREEN_YELLOW_RED,
        ),
        stat(
            "Primary Model",
            "bgw_helios_online",
            text_mode="value_and_name",
            graph_mode="none",
            color_mode="background",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": "red", "index": 0},
                        "1": {"text": "UP", "color": "green", "index": 1},
                    },
                }
            ],
            thresholds=GREEN_RED_BINARY,
        ),
        stat(
            "Focus Active",
            "bgw_focus_active",
            text_mode="value_and_name",
            graph_mode="none",
            color_mode="background",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "idle", "color": "dark-blue", "index": 0},
                        "1": {"text": "FOCUS", "color": "purple", "index": 1},
                    },
                }
            ],
        ),
        stat(
            "Pending Reminders",
            "bgw_reminders_pending",
            unit="none",
            graph_mode="none",
            thresholds=[(None, "green"), (10, "yellow"), (25, "orange")],
        ),
        stat(
            "Closet Temp",
            'bgw_temperature_fahrenheit{location="closet"}',
            unit="fahrenheit",
            decimals=1,
            thresholds=TEMP_THRESHOLDS,
            color_mode="background",
        ),
        stat(
            "Tool Calls / min",
            "sum(rate(bgw_tool_calls_total[5m])) * 60",
            unit="none",
            decimals=1,
            graph_mode="area",
        ),
    ]
    row, y = grid_row(stats, y, heights=[4] * 8)
    panels.extend(row)

    # ------------------------------------------------------------ Hero row 1
    hero_1 = [
        timeseries(
            "Request Latency (p50 / p95 / p99)",
            [
                (
                    "histogram_quantile(0.50, sum by (le) (rate(bgw_request_duration_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(bgw_request_duration_seconds_bucket[5m])))",
                    "p95",
                ),
                (
                    "histogram_quantile(0.99, sum by (le) (rate(bgw_request_duration_seconds_bucket[5m])))",
                    "p99",
                ),
            ],
            unit="s",
            fill=15,
        ),
        timeseries(
            "Tool Calls by Tool",
            [("sum by (tool) (rate(bgw_tool_calls_total[5m])) * 60", "{{tool}}")],
            unit="none",
            stack=True,
            fill=60,
            description="Tool invocations per minute, stacked by tool name.",
        ),
    ]
    row, y = grid_row(hero_1, y, heights=[8, 8])
    panels.extend(row)

    # ------------------------------------------------------------ Hero row 2
    hero_2 = [
        timeseries(
            "LLM Errors by Type",
            [
                (
                    "sum by (error_type) (rate(bgw_llm_call_errors_total[5m])) * 60",
                    "{{error_type}}",
                )
            ],
            unit="none",
            stack=True,
            fill=60,
            description="LLM call errors per minute. A flat zero line is the goal.",
        ),
        timeseries(
            "Temperatures",
            [
                (
                    'bgw_temperature_fahrenheit{location="closet"}',
                    "closet",
                ),
                (
                    'bgw_temperature_fahrenheit{location="kitchen"}',
                    "ambient",
                ),
                ("bgw_temperature_delta_fahrenheit", "delta"),
            ],
            unit="fahrenheit",
            fill=5,
            thresholds=[(None, "green"), (80, "orange"), (85, "red")],
        ),
    ]
    row, y = grid_row(hero_2, y, heights=[8, 8])
    panels.extend(row)

    # ----------------------------------------------------------------- Logs
    log_panels = [
        logs(
            "Orchestrator Flow",
            '{container="brain-orchestrator"} | json | level=~"INFO|WARNING" | logger=~"orchestrator\\\\..*"',
        ),
        logs(
            "Tool Execution",
            '{container="brain-orchestrator"} |~ "\\\\[UNIFIED_LOOP\\\\]|\\\\[TOOL\\\\]"',
        ),
        logs(
            "Errors",
            '{container="brain-orchestrator"} | json | level=~"ERROR|CRITICAL"',
        ),
    ]
    row, y = grid_row(log_panels, y, heights=[8, 8, 8])
    panels.extend(row)

    assign_ids(panels)

    return dashboard(
        title="Jess Glance",
        uid="jess-glance",
        description=(
            "Kiosk + phone dashboard. Single screen, no scroll at 1920x1080. "
            "Health at a glance, four hero timeseries, three log streams. "
            "Generated — edit monitoring/grafana/dashgen/dashboards/jess_glance.py, "
            "not the JSON."
        ),
        tags=["brain-gateway", "kiosk"],
        refresh="30s",
        time_from="now-1h",
        panels=panels,
    )
