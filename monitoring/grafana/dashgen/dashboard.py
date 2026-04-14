"""
Top-level dashboard wrapper. Takes a list of panels (already positioned) and
emits the full Grafana dashboard JSON object.
"""

from __future__ import annotations

from .datasources import PROMETHEUS, LOKI


def dashboard(
    *,
    title: str,
    uid: str,
    panels: list[dict],
    tags: list[str] | None = None,
    refresh: str = "30s",
    time_from: str = "now-1h",
    templating: list[dict] | None = None,
    description: str = "",
) -> dict:
    return {
        "title": title,
        "uid": uid,
        "description": description,
        "tags": tags or ["brain-gateway"],
        "schemaVersion": 38,
        "version": 1,
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,  # shared crosshair
        "liveNow": False,
        "timezone": "",
        "weekStart": "",
        "refresh": refresh,
        "time": {"from": time_from, "to": "now"},
        "timepicker": {},
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                }
            ]
        },
        "templating": {"list": templating or []},
        "panels": panels,
    }


# ---------- template variable helpers ---------------------------------------

def var_query(name: str, label: str, query: str, *, multi: bool = True, include_all: bool = True) -> dict:
    """Prometheus label-values template variable."""
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": PROMETHEUS,
        "query": {"query": query, "refId": "StandardVariableQuery"},
        "refresh": 2,
        "multi": multi,
        "includeAll": include_all,
        "allValue": ".*" if include_all else None,
        "current": {"selected": False, "text": "All", "value": "$__all"},
        "options": [],
        "sort": 1,
    }


def var_text(name: str, label: str, default: str = "") -> dict:
    """Free-text template variable (useful for request_id tracing)."""
    return {
        "name": name,
        "label": label,
        "type": "textbox",
        "current": {"selected": False, "text": default, "value": default},
        "query": default,
        "options": [{"selected": True, "text": default, "value": default}],
    }
