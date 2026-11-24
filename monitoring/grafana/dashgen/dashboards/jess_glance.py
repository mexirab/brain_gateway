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
    CHAT_TURN_LATENCY_THRESHOLDS_S,
    CLOSET_TEMP_THRESHOLDS,
    GREEN_RED_BINARY,
    GREEN_YELLOW_RED,
    TEMP_THRESHOLDS,
    logs,
    stat,
    timeseries,
)


# A kiosk screen (scripts/jupiter-kiosk) should read "0", not "No data", when nobody has chatted in
# the last hour or the orchestrator just restarted (its counters start over). `or on() X` adds X only
# when the left side is empty, so a real series always wins. Latency over an idle window is NaN
# (0/0), so it is filtered with `>= 0` first. Temperature panels keep "No data": there it means the
# sensor is missing, and a fake 0 F would hide that.
def or_zero(expr: str) -> str:
    return f"({expr}) or on() vector(0)"


def latency_or_zero(quantile: float) -> str:
    q = f"histogram_quantile({quantile}, sum by (le) (rate(bgw_request_duration_seconds_bucket[5m])))"
    return f"({q} >= 0) or on() vector(0)"


def by_label_or_zero(expr: str, label: str) -> str:
    """A per-label series, or one 0 series labelled `none` when there are none."""
    return f'({expr}) or on() label_replace(vector(0), "{label}", "none", "", "")'


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # ------------------------------------------------------------------ Stats
    # 8 stats across the top, each 3 columns wide, 4 tall.
    stats = [
        stat(
            "Request Rate",
            or_zero("sum(rate(bgw_requests_total[5m])) * 60"),
            unit="none",
            decimals=1,
            graph_mode="area",
            thresholds=[(None, "green"), (30, "yellow"), (120, "red")],
        ),
        stat(
            "p95 Latency",
            latency_or_zero(0.95),
            unit="s",
            decimals=1,
            thresholds=CHAT_TURN_LATENCY_THRESHOLDS_S,
        ),
        stat(
            "Error %",
            or_zero("(sum(rate(bgw_request_errors_total[5m])) / clamp_min(sum(rate(bgw_requests_total[5m])), 0.001)) * 100"),
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
            thresholds=CLOSET_TEMP_THRESHOLDS,
            color_mode="background",
        ),
        stat(
            "Tool Calls / min",
            or_zero("sum(rate(bgw_tool_calls_total[5m])) * 60"),
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
                    latency_or_zero(0.50),
                    "p50",
                ),
                (
                    latency_or_zero(0.95),
                    "p95",
                ),
                (
                    latency_or_zero(0.99),
                    "p99",
                ),
            ],
            unit="s",
            fill=15,
        ),
        timeseries(
            "Tool Calls by Tool",
            [(by_label_or_zero("sum by (tool) (rate(bgw_tool_calls_total[5m])) * 60", "tool"), "{{tool}}")],
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
                    by_label_or_zero("sum by (error_type) (rate(bgw_llm_call_errors_total[5m])) * 60", "error_type"),
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
            '{container="brain-orchestrator"} | json | level=~"info|warning" | logger=~"orchestrator\\\\..*"',
        ),
        logs(
            "Tool Execution",
            '{container="brain-orchestrator"} |~ "\\\\[UNIFIED_LOOP\\\\]|\\\\[TOOL\\\\]"',
        ),
        logs(
            "Errors",
            '{container="brain-orchestrator"} | json | level=~"error|critical"',
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
