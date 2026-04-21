#!/usr/bin/env python3
"""
Import a historical budget CSV/XLSX into Brain Gateway.

Runs inside the brain-orchestrator container so it uses the same pandas +
SQLite + mempalace that the query_budget tool reads from.

Usage (from Helios):
    docker exec brain-orchestrator python /app/scripts/import_budget.py \
        --file /app/data/imports/2023-ynab.csv \
        --name 2023-ynab

Pre-step (from Mac):
    scp ~/Downloads/2023-ynab.csv labadmin@helios:/opt/gateway_mvp/data/app/imports/

The file lands at /app/data/imports/2023-ynab.csv inside the container
(via the bind mount defined in docker-compose.yml).

Auto-detects common column names from YNAB, Mint, and plain bank exports.
Override with --date-col, --amount-col, --outflow-col, --inflow-col,
--category-col, --payee-col, or --memo-col if detection is wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Container path setup — /app is the orchestrator's working dir.
sys.path.insert(0, "/app")

from orchestrator import budget_manager, state_store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_budget")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import a historical budget CSV/XLSX.")
    p.add_argument(
        "--file", required=True, help="Path to CSV or XLSX inside the container (e.g. /app/data/imports/2023.csv)"
    )
    p.add_argument("--name", required=True, help="Dataset name (used to reference this import later, e.g. '2023-ynab')")
    p.add_argument("--date-col", help="Override date column name")
    p.add_argument("--amount-col", help="Override single signed amount column name")
    p.add_argument("--outflow-col", help="Override outflow column name (YNAB style)")
    p.add_argument("--inflow-col", help="Override inflow column name (YNAB style)")
    p.add_argument("--category-col", help="Override category column name")
    p.add_argument("--payee-col", help="Override payee/merchant column name")
    p.add_argument("--memo-col", help="Override memo/description column name")
    p.add_argument(
        "--invert-amount",
        action="store_true",
        help="Flip sign of --amount-col (e.g. if source uses positive=debit).",
    )
    p.add_argument(
        "--include-transfers",
        action="store_true",
        help="Keep 'Transfer : <Account>' rows (default: skipped — they double-count as both outflow and inflow).",
    )
    p.add_argument(
        "--include-starting-balances",
        action="store_true",
        help="Keep 'Starting Balance' rows (default: skipped — they are account-setup placeholders, not spending).",
    )
    p.add_argument(
        "--exclude-investments",
        action="store_true",
        help="Drop 'Investment Buy' / 'Investment Sell' rows (default: kept — some users count investing as spending, some as savings).",
    )
    p.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing dataset instead of replacing it. Use when splitting a multi-year import across several YNAB export files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print summary without writing to the database.",
    )
    return p.parse_args()


async def _run() -> int:
    args = _parse_args()

    if not Path(args.file).is_file():
        logger.error(f"File not found: {args.file}")
        return 2

    overrides = {
        k: v
        for k, v in {
            "date": args.date_col,
            "amount": args.amount_col,
            "outflow": args.outflow_col,
            "inflow": args.inflow_col,
            "category": args.category_col,
            "payee": args.payee_col,
            "memo": args.memo_col,
        }.items()
        if v
    }

    if args.dry_run:
        df = budget_manager._read_file(args.file)
        detected = budget_manager.auto_detect_columns(list(df.columns))
        detected.update(overrides)
        rows = budget_manager._normalize_rows(
            df,
            detected,
            invert_amount=args.invert_amount,
            include_transfers=args.include_transfers,
            include_starting_balances=args.include_starting_balances,
            exclude_investments=args.exclude_investments,
            append=args.append,
        )
        print(f"Would import {len(rows)} rows into '{args.name}'")
        print(f"Columns detected: {json.dumps(detected, indent=2)}")
        if rows:
            dates_sorted = sorted(r["txn_date"] for r in rows)
            print(f"Date range: {dates_sorted[0]}..{dates_sorted[-1]}")
            print("First 3 rows:")
            for r in rows[:3]:
                print(f"  {r}")
        return 0

    # init_db is safe to call repeatedly; it's a no-op if the schema already exists.
    state_store.init_db()

    meta = await budget_manager.import_file(
        path=args.file,
        name=args.name,
        column_overrides=overrides,
        invert_amount=args.invert_amount,
        include_transfers=args.include_transfers,
        include_starting_balances=args.include_starting_balances,
        exclude_investments=args.exclude_investments,
        append=args.append,
    )
    print(json.dumps(meta, indent=2, default=str))
    print()
    print(f"✓ Imported {meta['row_count']} rows into dataset '{meta['name']}' ({meta['date_min']}..{meta['date_max']})")
    print(f"  Total outflow: ${abs(meta['total_outflow']):,.2f}   Total inflow: ${meta['total_inflow']:,.2f}")
    if meta.get("summary_doc_id"):
        print(f"  Summary indexed in mempalace: {meta['summary_doc_id']}")
    print()
    print(
        "Ask Jess: 'query the {name} budget — top categories' or 'what was unusual about my {name} spending?'".format(
            name=meta["name"]
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
