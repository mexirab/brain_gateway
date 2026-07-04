#!/usr/bin/env python3
"""
Dashboard generator entry point.

Runs each dashboard builder, writes the resulting JSON to Grafana's
provisioning directory. Grafana's file-provisioning reloader picks up changes
automatically — no restart needed unless you change the datasource UIDs.

Usage:
    python monitoring/grafana/dashgen/build.py

From repo root. Writes to:
    monitoring/grafana/provisioning/dashboards/json/

Add a new dashboard:
    1. Create monitoring/grafana/dashgen/dashboards/my_dashboard.py with a
       top-level build() -> dict function that returns a dashboard JSON dict.
    2. Add it to DASHBOARDS below.
    3. Re-run this script.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a script from anywhere in the repo.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "monitoring" / "grafana"))

from dashgen.dashboards import brain_gateway_sre, homelab_infra, jess_glance  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "monitoring" / "grafana" / "provisioning" / "dashboards" / "json"

# Map of (output filename, builder module).
DASHBOARDS = [
    ("jess-glance.json", jess_glance),
    ("brain-gateway.json", brain_gateway_sre),
    ("homelab-infra.json", homelab_infra),
]


def main() -> int:
    if not OUTPUT_DIR.exists():
        print(f"ERROR: output dir does not exist: {OUTPUT_DIR}", file=sys.stderr)
        return 1

    for filename, module in DASHBOARDS:
        dash = module.build()
        # Minimal structural validation — catch obvious bugs before Grafana does.
        assert "panels" in dash, f"{filename}: missing panels"
        assert "uid" in dash and dash["uid"], f"{filename}: missing uid"
        assert "title" in dash and dash["title"], f"{filename}: missing title"
        # Every panel needs an id and gridPos (unless it's a row).
        for i, p in enumerate(dash["panels"]):
            assert "id" in p, f"{filename}: panel {i} ({p.get('title')}) missing id"
            if p.get("type") != "row":
                assert "gridPos" in p, f"{filename}: panel {i} ({p.get('title')}) missing gridPos"

        out_path = OUTPUT_DIR / filename
        with out_path.open("w") as f:
            json.dump(dash, f, indent=2)
            f.write("\n")
        print(f"wrote {out_path.relative_to(REPO_ROOT)} ({len(dash['panels'])} panels)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
