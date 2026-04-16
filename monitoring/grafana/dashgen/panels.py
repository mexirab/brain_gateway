"""
Panel-emitter helpers.

Each helper takes a short spec and returns a Grafana panel dict. The caller
doesn't set `id` or `gridPos` — those are assigned by `layout.grid()` in
build.py so you don't have to hand-manage coordinates.

Panel types covered: stat, timeseries, bargauge, gauge, logs, piechart, row.

Design rule: if you find yourself writing more than 3 lines to emit a panel,
add a kwarg to the helper. Boilerplate goes here, not in dashboard files.
"""

from __future__ import annotations

from typing import Any

from .datasources import LOKI, PROMETHEUS


# ---------- target builders -------------------------------------------------

def prom(expr: str, legend: str = "", ref_id: str = "A", instant: bool = False) -> dict:
    """Build a Prometheus query target."""
    return {
        "datasource": PROMETHEUS,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend or "__auto",
        "range": not instant,
        "instant": instant,
        "refId": ref_id,
    }


def logql(expr: str, ref_id: str = "A") -> dict:
    """Build a Loki query target."""
    return {
        "datasource": LOKI,
        "editorMode": "code",
        "expr": expr,
        "queryType": "range",
        "refId": ref_id,
    }


# ---------- panel builders --------------------------------------------------

def stat(
    title: str,
    query: str,
    *,
    unit: str = "none",
    decimals: int = 0,
    thresholds: list[tuple[float, str]] | None = None,
    color_mode: str = "value",
    graph_mode: str = "area",
    text_mode: str = "auto",
    mappings: list[dict] | None = None,
) -> dict:
    """Single big-number stat panel.

    thresholds: list of (value, color). Use None for the base. e.g.
        [(None, "green"), (5, "yellow"), (20, "red")]
    mappings: Grafana value mappings, e.g. `[{"options": {"0": {"text": "DOWN", "color": "red"}}, "type": "value"}]`
    """
    steps = []
    if thresholds:
        for value, color in thresholds:
            steps.append({"color": color, "value": value})
    else:
        steps = [{"color": "green", "value": None}]

    return {
        "type": "stat",
        "title": title,
        "datasource": PROMETHEUS,
        "targets": [prom(query, instant=True)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": steps},
                "mappings": mappings or [],
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "colorMode": color_mode,
            "graphMode": graph_mode,
            "justifyMode": "auto",
            "orientation": "auto",
            "textMode": text_mode,
            "reduceOptions": {
                "calcs": ["lastNotNull"],
                "fields": "",
                "values": False,
            },
        },
    }


def timeseries(
    title: str,
    queries: list[tuple[str, str]],
    *,
    unit: str = "short",
    stack: bool = False,
    fill: int = 10,
    legend_mode: str = "list",
    legend_placement: str = "bottom",
    thresholds: list[tuple[float, str]] | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    draw_style: str = "line",
    description: str | None = None,
) -> dict:
    """Timeseries panel. queries is a list of (promql, legend) tuples."""
    targets = [prom(expr, legend, ref_id=chr(ord("A") + i)) for i, (expr, legend) in enumerate(queries)]

    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps = [{"color": c, "value": v} for v, c in thresholds]

    defaults: dict[str, Any] = {
        "unit": unit,
        "thresholds": {"mode": "absolute", "steps": steps},
        "color": {"mode": "palette-classic"},
        "custom": {
            "drawStyle": draw_style,
            "lineInterpolation": "smooth",
            "lineWidth": 2,
            "fillOpacity": fill,
            "gradientMode": "opacity",
            "spanNulls": True,
            "showPoints": "never",
            "stacking": {"mode": "normal" if stack else "none", "group": "A"},
            "axisPlacement": "auto",
            "axisLabel": "",
            "scaleDistribution": {"type": "linear"},
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "thresholdsStyle": {"mode": "off"},
        },
    }
    if min_value is not None:
        defaults["min"] = min_value
    if max_value is not None:
        defaults["max"] = max_value

    panel = {
        "type": "timeseries",
        "title": title,
        "datasource": PROMETHEUS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {
                "displayMode": legend_mode,
                "placement": legend_placement,
                "showLegend": True,
                "calcs": [],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if description:
        panel["description"] = description
    return panel


def bargauge(
    title: str,
    query: str,
    *,
    unit: str = "none",
    orientation: str = "horizontal",
    display_mode: str = "gradient",
    thresholds: list[tuple[float, str]] | None = None,
    max_value: float | None = None,
) -> dict:
    """Bar gauge for ranked comparisons (e.g. VRAM usage per GPU)."""
    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps = [{"color": c, "value": v} for v, c in thresholds]

    defaults: dict[str, Any] = {
        "unit": unit,
        "thresholds": {"mode": "absolute", "steps": steps},
        "color": {"mode": "thresholds"},
    }
    if max_value is not None:
        defaults["max"] = max_value

    return {
        "type": "bargauge",
        "title": title,
        "datasource": PROMETHEUS,
        "targets": [prom(query)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "orientation": orientation,
            "displayMode": display_mode,
            "showUnfilled": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def gauge(
    title: str,
    query: str,
    *,
    unit: str = "percent",
    min_value: float = 0,
    max_value: float = 100,
    thresholds: list[tuple[float, str]] | None = None,
) -> dict:
    steps = [{"color": "green", "value": None}]
    if thresholds:
        steps = [{"color": c, "value": v} for v, c in thresholds]

    return {
        "type": "gauge",
        "title": title,
        "datasource": PROMETHEUS,
        "targets": [prom(query, instant=True)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": min_value,
                "max": max_value,
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "orientation": "auto",
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def logs(
    title: str,
    query: str,
    *,
    wrap: bool = True,
    dedup: str = "none",
) -> dict:
    """Loki log panel. query is LogQL."""
    return {
        "type": "logs",
        "title": title,
        "datasource": LOKI,
        "targets": [logql(query)],
        "options": {
            "showTime": True,
            "showLabels": False,
            "showCommonLabels": False,
            "wrapLogMessage": wrap,
            "prettifyLogMessage": False,
            "enableLogDetails": True,
            "dedupStrategy": dedup,
            "sortOrder": "Descending",
        },
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def piechart(
    title: str,
    query: str,
    *,
    legend_mode: str = "list",
    legend_placement: str = "right",
) -> dict:
    return {
        "type": "piechart",
        "title": title,
        "datasource": PROMETHEUS,
        "targets": [prom(query)],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {"hideFrom": {"legend": False, "tooltip": False, "viz": False}},
                "mappings": [],
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "pieType": "donut",
            "tooltip": {"mode": "single", "sort": "none"},
            "legend": {
                "displayMode": legend_mode,
                "placement": legend_placement,
                "showLegend": True,
                "values": ["percent"],
            },
            "displayLabels": ["name", "percent"],
        },
    }


def row(title: str, collapsed: bool = False) -> dict:
    """Row separator. Use for SRE dashboard, not kiosk."""
    return {
        "type": "row",
        "title": title,
        "collapsed": collapsed,
        "panels": [],
    }


# ---------- common threshold presets ---------------------------------------

GREEN_YELLOW_RED = [(None, "green"), (5, "yellow"), (20, "red")]
GREEN_RED_BINARY = [(None, "red"), (1, "green")]  # for up/down (0=down, 1=up)
TEMP_THRESHOLDS = [(None, "green"), (75, "yellow"), (80, "orange"), (85, "red")]
LATENCY_THRESHOLDS_S = [(None, "green"), (1, "yellow"), (5, "orange"), (15, "red")]
# Voice-pipeline specific: green at/under current p50 target (13s), yellow
# when p95 slips, orange past prior floor (17s), red past the old 22s regime.
# Snaps to histogram bucket edges so threshold colors match quantile bins.
VOICE_LATENCY_THRESHOLDS_S = [(None, "green"), (13, "yellow"), (17, "orange"), (22, "red")]
# TTS synthesis is fast (<1s typical for a single sentence). Warn past 1.5s,
# red past 3s where the silence pre-roll can't mask user-perceived delay.
VOICE_TTS_THRESHOLDS_S = [(None, "green"), (1.5, "yellow"), (3, "red")]
# Expert agent is thinking-mode Qwen3-32B on Saturn; 30-150s is "normal" for
# hard reasoning. Green up to 60s, yellow past 90s, red past the 180s timeout.
EXPERT_LATENCY_THRESHOLDS_S = [(None, "green"), (60, "yellow"), (90, "orange"), (180, "red")]
# Expert circuit breaker: 0 = closed (healthy), 1 = open (tripping).
EXPERT_CIRCUIT_THRESHOLDS = [(None, "green"), (1, "red")]
