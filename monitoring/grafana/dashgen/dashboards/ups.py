"""
ups — UPS power chain visibility for the Helios cluster.

Both UPSes are USB-attached to Helios; the host NUT service feeds the
nut_exporter Docker container on Helios:9199, which Prometheus scrapes
with one target per UPS (relabel turns the static target into the
`?ups=<name>` URL param the multi-target exporter requires).

Metric prefix: `network_ups_tools_*`. Labels: `job="nut"`,
`node="helios"`, `ups="cyberpower"|"goldenmate"`.

Per-UPS coverage:
  - CyberPower CP1500PFCLCD (1000W): full data — load %, real power,
    input/output voltage, battery charge/runtime/voltage, status flags.
  - Goldenmate 2000VA LiFePO4 (iDowell chip): partial — status, battery
    charge, battery runtime. No load watts (firmware doesn't expose).

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
            'count(network_ups_tools_ups_status{flag="OL"} == 1)',
            unit="none",
            graph_mode="none",
            thresholds=[(None, "red"), (1, "yellow"), (2, "green")],
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
            thresholds=[(None, "red"), (300, "yellow"), (900, "green")],
        ),
        stat(
            "CyberPower draw",
            # `ups.load` is a percent of `ups.realpower.nominal` watts.
            # (network_ups_tools_ups_load{ups="cyberpower"} / 100) * realpower_nominal
            '(network_ups_tools_ups_load{ups="cyberpower"} / 100) * '
            'network_ups_tools_ups_realpower_nominal{ups="cyberpower"}',
            unit="watt",
            graph_mode="area",
            thresholds=[(None, "green"), (700, "yellow"), (900, "red")],
        ),
    ]
    row, y = grid_row(overview_row, y, heights=[5, 5, 5, 5, 5])
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
            description="Both UPSes report battery.charge percent.",
        ),
        timeseries(
            "Battery runtime remaining",
            [
                ("network_ups_tools_battery_runtime", "{{ups}}"),
            ],
            unit="s",
            thresholds=[(None, "red"), (300, "yellow"), (900, "green")],
            description="Seconds at current load. Drops fast on grid loss.",
        ),
        timeseries(
            "Battery voltage (CyberPower)",
            [
                (
                    'network_ups_tools_battery_voltage{ups="cyberpower"}',
                    "actual",
                ),
                (
                    'network_ups_tools_battery_voltage_nominal{ups="cyberpower"}',
                    "nominal",
                ),
            ],
            unit="volt",
            description="Goldenmate iDowell firmware doesn't expose battery.voltage.",
        ),
    ]
    row, y = grid_row(battery_row, y, heights=[8, 8, 8])
    panels.extend(row)

    # ------------------------------------------------------------- Power chain
    r, y = row_divider("Power Chain (CyberPower only — Goldenmate firmware limited)", y)
    panels.append(r)

    power_row = [
        timeseries(
            "Real power draw",
            [
                (
                    '(network_ups_tools_ups_load{ups="cyberpower"} / 100) * '
                    'network_ups_tools_ups_realpower_nominal{ups="cyberpower"}',
                    "{{ups}}",
                ),
            ],
            unit="watt",
            thresholds=[(None, "green"), (700, "yellow"), (900, "red")],
            description="Computed from ups.load (%) × ups.realpower.nominal (1000W).",
        ),
        timeseries(
            "UPS load %",
            [
                ('network_ups_tools_ups_load{ups="cyberpower"}', "{{ups}}"),
            ],
            unit="percent",
            min_value=0,
            max_value=100,
            thresholds=[(None, "green"), (70, "yellow"), (90, "red")],
        ),
        timeseries(
            "Input voltage",
            [
                (
                    'network_ups_tools_input_voltage{ups="cyberpower"}',
                    "actual",
                ),
                (
                    'network_ups_tools_input_voltage_nominal{ups="cyberpower"}',
                    "nominal",
                ),
            ],
            unit="volt",
            description="Wall voltage. Sustained < 110V or > 130V is a brownout / surge signal.",
        ),
    ]
    row, y = grid_row(power_row, y, heights=[8, 8, 8])
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
            "CyberPower CP1500PFCLCD + Goldenmate 2000VA LiFePO4 power Helios. "
            "Source: druggeri/nut_exporter via NUT on the Helios host."
        ),
        tags=["brain-gateway", "infra", "ups"],
        panels=assign_ids(panels),
        time_from="now-6h",
    )
