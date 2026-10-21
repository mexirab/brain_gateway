"""
ups — UPS power chain visibility for Helios.

One UPS is USB-attached to Helios (since the CyberPower CP1500PFCLCD was
retired, 2026-10-21): the host NUT service feeds the nut_exporter container
on Helios:9199, which Prometheus scrapes per UPS (relabel turns the static
target into the `?ups=<name>` URL param the multi-target exporter requires).

Metric prefix: `network_ups_tools_*`. Labels: `job="nut"`, `node="helios"`,
`ups="goldenmate"`.

Goldenmate 2000VA LiFePO4 (BMS "Smart-Battery" USB chip) exposes only
status flags, battery charge and battery runtime — no load watts, input
voltage or battery voltage, so the old CyberPower power-chain panels are
gone. If a full-data UPS comes back, restore them from git history.

Status flags (`ups_status{flag=...}`):
  OL = on-line, OB = on battery, LB = low battery, CHRG = charging,
  DISCHRG = discharging, RB = replace battery, OVER = overload.
"""

from __future__ import annotations

from ..dashboard import dashboard
from ..layout import assign_ids, grid_row, row_divider
from ..panels import bargauge, stat, timeseries


def build() -> dict:
    panels: list[dict] = []
    y = 0

    # ------------------------------------------------------------- Overview
    r, y = row_divider("Overview", y)
    panels.append(r)

    overview_row = [
        stat(
            "UPSes Online",
            # `or vector(0)`: count() of nothing is "No data", not 0, so without
            # it the panel went blank exactly when the UPS dropped off line power.
            'count(network_ups_tools_ups_status{flag="OL"} == 1) or vector(0)',
            unit="none",
            graph_mode="none",
            thresholds=[(None, "red"), (1, "green")],
        ),
        stat(
            "On Battery (now)",
            'count(network_ups_tools_ups_status{flag="OB"} == 1) or vector(0)',
            unit="none",
            graph_mode="none",
            thresholds=[(None, "green"), (1, "red")],
            mappings=[
                {"type": "value", "options": {"0": {"text": "No", "color": "green"}}},
            ],
        ),
        stat(
            "Lowest battery %",
            "min(network_ups_tools_battery_charge)",
            unit="percent",
            graph_mode="area",
            thresholds=[(None, "red"), (20, "yellow"), (50, "green")],
        ),
        stat(
            "Lowest runtime remaining",
            "min(network_ups_tools_battery_runtime)",
            unit="s",
            graph_mode="area",
            thresholds=[(None, "red"), (180, "yellow"), (300, "green")],  # Goldenmate reads ~420 s at full charge
        ),
    ]
    row, y = grid_row(overview_row, y, heights=[5, 5, 5, 5])
    panels.extend(row)

    # ------------------------------------------------------------- Battery
    r, y = row_divider("Battery State", y)
    panels.append(r)

    battery_row = [
        timeseries(
            "Battery charge %",
            [
                ("network_ups_tools_battery_charge", "{{ups}}"),
            ],
            unit="percent",
            min_value=0,
            max_value=100,
            thresholds=[(None, "red"), (20, "yellow"), (50, "green")],
            description="battery.charge percent.",
        ),
        timeseries(
            "Battery runtime remaining",
            [
                ("network_ups_tools_battery_runtime", "{{ups}}"),
            ],
            unit="s",
            thresholds=[(None, "red"), (180, "yellow"), (300, "green")],  # Goldenmate reads ~420 s at full charge
            description="Seconds at current load. Drops fast on grid loss.",
        ),
    ]
    row, y = grid_row(battery_row, y, heights=[8, 8])
    panels.extend(row)

    # ------------------------------------------------------------- Status flags
    r, y = row_divider("Status Flags", y)
    panels.append(r)

    # NUT exposes a 1/0 series per flag per UPS. We surface the meaningful
    # ones as bargauges (anything > 0 is unusual or actionable).
    status_row = [
        bargauge(
            "Active status flags (per UPS)",
            # Sum each UPS's set flags. OL is normal so we exclude it from
            # the alert-attention view; everything else trending non-zero
            # is worth a glance.
            'sum by (ups) (network_ups_tools_ups_status{flag!~"OL"})',
            unit="none",
            thresholds=[(None, "green"), (1, "yellow"), (2, "red")],
        ),
        bargauge(
            "On Battery flag",
            'network_ups_tools_ups_status{flag="OB"}',
            unit="none",
            thresholds=[(None, "green"), (1, "red")],
        ),
        bargauge(
            "Low Battery flag",
            'network_ups_tools_ups_status{flag="LB"}',
            unit="none",
            thresholds=[(None, "green"), (1, "red")],
        ),
        bargauge(
            "Replace Battery flag",
            'network_ups_tools_ups_status{flag="RB"}',
            unit="none",
            thresholds=[(None, "green"), (1, "red")],
        ),
    ]
    row, y = grid_row(status_row, y, heights=[6, 6, 6, 6])
    panels.extend(row)

    # ------------------------------------------------------------- Exporter health
    r, y = row_divider("Exporter Health", y)
    panels.append(r)

    health_row = [
        timeseries(
            "Scrape success",
            [
                ('up{job="nut"}', "{{ups}}"),
            ],
            unit="none",
            min_value=0,
            max_value=1,
            thresholds=[(None, "red"), (1, "green")],
            description="0 = exporter unreachable or NUT server down. Sub-15s gaps are normal.",
        ),
        timeseries(
            "Scrape duration",
            [
                ('scrape_duration_seconds{job="nut"}', "{{ups}}"),
            ],
            unit="s",
            description="Time for Prometheus to fetch /ups_metrics?ups=<name> from the exporter.",
        ),
    ]
    row, y = grid_row(health_row, y, heights=[8, 8])
    panels.extend(row)

    return dashboard(
        title="UPS / Power Chain",
        uid="ups-power-chain",
        description=(
            "Goldenmate 2000VA LiFePO4 powers Helios (CyberPower retired 2026-10-21). "
            "Source: druggeri/nut_exporter via NUT on the Helios host."
        ),
        tags=["brain-gateway", "infra", "ups"],
        panels=assign_ids(panels),
        time_from="now-6h",
    )
