"""
Budget import + analysis for historical CSV/Excel data.

Intentionally narrow: one-off imports of old YNAB/Mint/bank exports so Jess
can answer questions about past spending without that data ever leaving the
local stack. The live YNAB API integration (finance_manager.py) is separate
and untouched.

Flow:
  1. import_file(path, name) parses CSV/XLSX via pandas, normalizes columns,
     writes transactions to SQLite, generates a markdown summary, and files
     the summary into the document vault (RAG-indexed, locally stored).
  2. query() is the tool-facing entrypoint — bounded aggregation queries
     over SQLite (not raw LLM over raw rows). Answers are composed by the
     primary model; deep analysis can be delegated to ask_expert.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from orchestrator import state_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Column auto-detection
# ---------------------------------------------------------------------------


_DATE_PATTERNS = [r"date", r"posted", r"transaction.?date", r"trans.?date"]
_AMOUNT_PATTERNS = [r"amount", r"transaction.?amount"]
_OUTFLOW_PATTERNS = [r"outflow", r"debit", r"withdrawal"]
_INFLOW_PATTERNS = [r"inflow", r"credit", r"deposit"]
_CATEGORY_PATTERNS = [r"^category$", r"category.?group"]
_PAYEE_PATTERNS = [r"payee", r"merchant", r"description", r"name"]
_MEMO_PATTERNS = [r"memo", r"notes?"]


def _match_col(columns: List[str], patterns: List[str]) -> Optional[str]:
    """Return the first column (lowercased match) hitting any pattern, preserving original case."""
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for col in columns:
            if rx.search(col):
                return col
    return None


def auto_detect_columns(columns: List[str]) -> Dict[str, Optional[str]]:
    """Best-effort match of CSV/XLSX column names to roles.

    Returns a dict with keys: date, amount, outflow, inflow, category, payee, memo.
    amount is set when there's a single signed-amount column; outflow/inflow are
    set when the source uses separate positive columns (YNAB style).
    """
    cols = list(columns)
    detected = {
        "date": _match_col(cols, _DATE_PATTERNS),
        "amount": _match_col(cols, _AMOUNT_PATTERNS),
        "outflow": _match_col(cols, _OUTFLOW_PATTERNS),
        "inflow": _match_col(cols, _INFLOW_PATTERNS),
        "category": _match_col(cols, _CATEGORY_PATTERNS),
        "payee": _match_col(cols, _PAYEE_PATTERNS),
        "memo": _match_col(cols, _MEMO_PATTERNS),
    }
    # If both outflow/inflow are present, ignore a single "amount" column since
    # YNAB's CSV has Outflow + Inflow as the source of truth, not Amount.
    if detected["outflow"] and detected["inflow"]:
        detected["amount"] = None
    return detected


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _read_file(path: str):
    """Load CSV or XLSX into a pandas DataFrame.

    CSV encoding is best-effort: try utf-8-sig (handles Excel BOM), fall back
    to utf-8 with errors="replace" so a single legacy garbled byte in one
    memo doesn't kill the whole import. Multi-year YNAB exports accumulate
    occasional bad bytes from years-old legacy imports.
    """
    import pandas as pd  # local import — keeps module-level import graph light

    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(path)

    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        logger.warning(
            f"[BUDGET] {path} has non-UTF-8 bytes ({e}); retrying with errors=replace — "
            f"some memo characters may become U+FFFD."
        )
        return pd.read_csv(path, encoding="utf-8-sig", encoding_errors="replace")


def _parse_money(val) -> Optional[float]:
    """Parse a money cell ('$1,234.56', '(23.10)', '-', NaN) into a float."""
    import pandas as pd

    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in {"-", "--"}:
        return None
    # Parentheses convention: (123.45) -> -123.45
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace(",", "").replace("$", "").replace(" ", "")
    if s.startswith("-"):
        negative = True
        s = s[1:]
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if negative else val


def _strip_symbol_glyphs(s: Optional[str]) -> Optional[str]:
    """Remove emoji / pictographic symbols from a category or payee string.

    YNAB users often prefix category names with emoji ('🎮 🎲 Gaming',
    '🍽 Dining Out', '💾 Software Subscriptions'). These are visually nice
    in the YNAB UI but poison downstream text matching: an exact filter
    like category='Gaming' never hits '🎮 🎲 Gaming', and even substring
    searches have to know to skip the leading emoji.

    Strategy: drop every codepoint in Unicode category 'So' (Symbol,
    other — covers essentially all emoji including multi-codepoint
    sequences after component-wise filtering) or 'Sk' (Symbol, modifier).
    Then collapse runs of whitespace and trim. Letters, digits, common
    punctuation ('-', '&', '/', etc.), and currency glyphs are preserved.
    """
    if not s:
        return s
    import unicodedata

    cleaned = "".join(c for c in s if unicodedata.category(c) not in ("So", "Sk"))
    return " ".join(cleaned.split()) or None


_TRANSFER_PAYEE_RX = re.compile(r"^\s*transfer\s*:", re.IGNORECASE)
_STARTING_BALANCE_RX = re.compile(r"^\s*starting\s+balance\s*$", re.IGNORECASE)
_RECONCILIATION_RX = re.compile(r"^\s*reconciliation\s+balance\s+adjustment\s*$", re.IGNORECASE)
_INVESTMENT_RX = re.compile(r"^\s*investment\s+(buy|sell)\s*$", re.IGNORECASE)


def _normalize_rows(
    df,
    col_map: Dict[str, Optional[str]],
    invert_amount: bool,
    include_transfers: bool = False,
    include_starting_balances: bool = False,
    exclude_investments: bool = False,
) -> List[Dict[str, Any]]:
    """Turn a DataFrame + column map into a list of normalized transaction dicts.

    Amount convention after normalization: positive = inflow, negative = outflow.

    Noise filters (YNAB-specific):
      - Transfer : <Account> rows — skipped by default (opt-in with include_transfers).
        Inter-account moves net zero but double-count both sides of aggregations.
      - 'Starting Balance' rows — skipped by default (opt-in with include_starting_balances).
        Account-setup placeholders, not spending.
      - 'Reconciliation Balance Adjustment' — ALWAYS skipped. Pure accounting
        artifact when a YNAB balance is forced to match the bank.
      - 'Investment Buy' / 'Investment Sell' — skipped only when exclude_investments=True.
        Kept by default because users split on whether investing counts as spending.
    """
    import pandas as pd

    rows: List[Dict[str, Any]] = []
    skipped_transfers = 0
    skipped_starting = 0
    skipped_reconciliation = 0
    skipped_investments = 0
    date_col = col_map.get("date")
    if not date_col:
        raise ValueError("No date column detected — pass --date-col explicitly.")

    has_split = col_map.get("outflow") and col_map.get("inflow")
    amount_col = col_map.get("amount")
    if not has_split and not amount_col:
        raise ValueError("No amount/outflow/inflow column detected — pass --amount-col or --outflow-col/--inflow-col.")

    for _, r in df.iterrows():
        raw_date = r.get(date_col)
        if pd.isna(raw_date):
            continue
        try:
            date_val = pd.to_datetime(raw_date, errors="coerce")
        except Exception:
            continue
        if pd.isna(date_val):
            continue
        txn_date = date_val.date().isoformat()

        if has_split:
            out = _parse_money(r.get(col_map["outflow"])) or 0.0
            inn = _parse_money(r.get(col_map["inflow"])) or 0.0
            amount = inn - out  # outflow subtracts
        else:
            amount = _parse_money(r.get(amount_col))
            if amount is None:
                continue
            if invert_amount:
                amount = -amount

        category = None
        if col_map.get("category"):
            c = r.get(col_map["category"])
            category = None if pd.isna(c) else str(c).strip() or None
            # Strip YNAB emoji prefixes ('🎮 🎲 Gaming' -> 'Gaming') so that
            # exact-match category filters work downstream.
            category = _strip_symbol_glyphs(category)
        payee = None
        if col_map.get("payee"):
            p = r.get(col_map["payee"])
            payee = None if pd.isna(p) else str(p).strip() or None
            payee = _strip_symbol_glyphs(payee)
        memo = None
        if col_map.get("memo"):
            m = r.get(col_map["memo"])
            memo = None if pd.isna(m) else str(m).strip() or None

        # Drop YNAB noise unless the caller asked to keep it.
        if payee:
            if not include_transfers and _TRANSFER_PAYEE_RX.match(payee):
                skipped_transfers += 1
                continue
            if not include_starting_balances and _STARTING_BALANCE_RX.match(payee):
                skipped_starting += 1
                continue
            if _RECONCILIATION_RX.match(payee):
                skipped_reconciliation += 1
                continue
            if exclude_investments and _INVESTMENT_RX.match(payee):
                skipped_investments += 1
                continue

        rows.append(
            {
                "txn_date": txn_date,
                "amount": round(float(amount), 2),
                "category": category,
                "payee": payee,
                "description": memo,
            }
        )

    noise_bits = []
    if skipped_transfers:
        noise_bits.append(f"{skipped_transfers} transfers")
    if skipped_starting:
        noise_bits.append(f"{skipped_starting} starting balances")
    if skipped_reconciliation:
        noise_bits.append(f"{skipped_reconciliation} reconciliation adjustments")
    if skipped_investments:
        noise_bits.append(f"{skipped_investments} investment buys/sells")
    if noise_bits:
        logger.info(f"[BUDGET] Skipped YNAB noise: {', '.join(noise_bits)}.")
    return rows


async def import_file(
    path: str,
    name: str,
    column_overrides: Optional[Dict[str, str]] = None,
    invert_amount: bool = False,
    include_transfers: bool = False,
    include_starting_balances: bool = False,
    exclude_investments: bool = False,
    append: bool = False,
) -> Dict[str, Any]:
    """Parse a CSV/XLSX file, persist transactions, and build a RAG summary.

    Returns metadata dict with row_count, date range, totals, and summary_doc_id.
    Safe to re-run with the same name (replaces existing transactions).

    Async because the mempalace indexing step is async (embedding + encryption).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    df = _read_file(path)
    cols = list(df.columns)
    detected = auto_detect_columns(cols)
    if column_overrides:
        for k, v in column_overrides.items():
            if v:
                detected[k] = v
    logger.info(f"[BUDGET] Import '{name}' columns detected: {detected}")

    rows = _normalize_rows(
        df,
        detected,
        invert_amount=invert_amount,
        include_transfers=include_transfers,
        include_starting_balances=include_starting_balances,
        exclude_investments=exclude_investments,
    )
    if not rows:
        raise ValueError(
            f"No parseable rows found in {path}. Detected columns: {detected}. "
            f"Use --date-col / --amount-col to override."
        )

    # Stub parent row must exist before transactions — FK constraint.
    existing = state_store.get_budget_import(name) if append else None
    stub = {
        "name": name,
        "source_file": (existing.get("source_file") if existing else os.path.basename(path)),
        "row_count": 0,
        "date_min": None,
        "date_max": None,
        "total_outflow": 0,
        "total_inflow": 0,
        "column_map": detected,
        "summary_doc_id": None,
    }
    if not existing:
        state_store.save_budget_import(stub)
    if not append:
        state_store.clear_budget_transactions(name)
    insert_stats = state_store.save_budget_transactions(name, rows)

    # Recompute metadata from the FULL current dataset (matters for append:
    # existing rows + newly inserted rows). This also keeps the on-disk
    # summary authoritative across multi-file imports.
    all_rows = state_store.query_budget_transactions(name, group_by=None, limit=10_000_000)
    total_outflow = sum(r["amount"] for r in all_rows if r["amount"] < 0)
    total_inflow = sum(r["amount"] for r in all_rows if r["amount"] > 0)
    dates = sorted(r["txn_date"] for r in all_rows)

    summary_doc_id = await _build_and_index_summary(name=name, rows=all_rows)
    meta = {
        **stub,
        "source_file": (
            f"{existing['source_file']} + {os.path.basename(path)}" if existing and append else os.path.basename(path)
        ),
        "row_count": len(all_rows),
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "total_outflow": total_outflow,
        "total_inflow": total_inflow,
        "summary_doc_id": summary_doc_id,
    }
    state_store.save_budget_import(meta)
    mode = "appended" if append else "imported"
    logger.info(
        f"[BUDGET] {mode.capitalize()} '{name}': "
        f"{insert_stats['inserted']} new rows, {insert_stats['skipped_duplicates']} duplicates skipped "
        f"(dataset total now {len(all_rows)}, {dates[0]}..{dates[-1]}, "
        f"outflow={total_outflow:.2f}, inflow={total_inflow:.2f})"
    )
    meta["rows_inserted_this_file"] = insert_stats["inserted"]
    meta["rows_skipped_duplicates"] = insert_stats["skipped_duplicates"]
    return meta


# ---------------------------------------------------------------------------
# Summary generation (Markdown -> document_vault / RAG)
# ---------------------------------------------------------------------------


def _build_summary_markdown(name: str, rows: List[Dict[str, Any]]) -> str:
    """Rich Markdown summary for RAG indexing + human reading."""
    import pandas as pd

    df = pd.DataFrame(rows)
    if df.empty:
        return f"# Budget import: {name}\n\n_(empty)_"

    df["txn_date"] = pd.to_datetime(df["txn_date"])
    df["month"] = df["txn_date"].dt.strftime("%Y-%m")
    outflow = df[df["amount"] < 0].copy()
    inflow = df[df["amount"] > 0].copy()

    lines: List[str] = []
    lines.append(f"# Budget import: {name}")
    lines.append("")
    lines.append(f"- Rows: **{len(df)}**")
    lines.append(f"- Date range: **{df['txn_date'].min().date()}** to **{df['txn_date'].max().date()}**")
    lines.append(f"- Total outflow: **${abs(outflow['amount'].sum()):,.2f}**")
    lines.append(f"- Total inflow: **${inflow['amount'].sum():,.2f}**")
    lines.append(f"- Net: **${df['amount'].sum():,.2f}**")
    lines.append("")

    if not outflow.empty and "category" in outflow.columns:
        by_cat = (
            outflow.assign(category=outflow["category"].fillna("(uncategorized)"))
            .groupby("category")["amount"]
            .agg(["sum", "count"])
            .sort_values("sum")
            .head(15)
        )
        lines.append("## Top spending categories")
        lines.append("")
        lines.append("| Category | Total | Transactions |")
        lines.append("|---|---:|---:|")
        for cat, row in by_cat.iterrows():
            lines.append(f"| {cat} | ${abs(row['sum']):,.2f} | {int(row['count'])} |")
        lines.append("")

    by_month_out = outflow.groupby("month")["amount"].sum().sort_index()
    by_month_in = inflow.groupby("month")["amount"].sum().sort_index()
    if len(by_month_out) > 0:
        lines.append("## Monthly outflow / inflow")
        lines.append("")
        lines.append("| Month | Outflow | Inflow | Net |")
        lines.append("|---|---:|---:|---:|")
        months = sorted(set(by_month_out.index) | set(by_month_in.index))
        for m in months:
            out = float(by_month_out.get(m, 0.0))
            inn = float(by_month_in.get(m, 0.0))
            lines.append(f"| {m} | ${abs(out):,.2f} | ${inn:,.2f} | ${out + inn:,.2f} |")
        lines.append("")

    if not outflow.empty and "payee" in outflow.columns:
        by_payee = (
            outflow.assign(payee=outflow["payee"].fillna("(unknown)"))
            .groupby("payee")["amount"]
            .agg(["sum", "count"])
            .sort_values("sum")
            .head(20)
        )
        lines.append("## Top merchants by outflow")
        lines.append("")
        lines.append("| Payee | Total | Transactions |")
        lines.append("|---|---:|---:|")
        for p, row in by_payee.iterrows():
            lines.append(f"| {p} | ${abs(row['sum']):,.2f} | {int(row['count'])} |")
        lines.append("")

    # Outliers: transactions >2 std dev above mean outflow
    if len(outflow) >= 10:
        mean_abs = outflow["amount"].abs().mean()
        std_abs = outflow["amount"].abs().std()
        threshold = mean_abs + 2 * std_abs
        unusual = outflow[outflow["amount"].abs() >= threshold].nlargest(10, "amount", keep="first")
        # Actually want largest abs outflows:
        unusual = outflow[outflow["amount"].abs() >= threshold].sort_values("amount").head(10)
        if not unusual.empty:
            lines.append(f"## Outlier transactions (|amount| > mean + 2 std, i.e. > ${threshold:,.2f})")
            lines.append("")
            lines.append("| Date | Amount | Category | Payee |")
            lines.append("|---|---:|---|---|")
            for _, r in unusual.iterrows():
                lines.append(
                    f"| {r['txn_date'].date()} | ${abs(r['amount']):,.2f} | "
                    f"{r.get('category') or ''} | {r.get('payee') or ''} |"
                )
            lines.append("")

    return "\n".join(lines)


async def _build_and_index_summary(name: str, rows: List[Dict[str, Any]]) -> Optional[str]:
    """Build the Markdown summary, write a disk copy, and index into the
    mempalace so search_memory can find it. Returns the palace doc_id or None."""
    summary_md = _build_summary_markdown(name, rows)

    # Persist a human-readable copy on disk for offline inspection.
    out_dir = Path("/app/data/budget_summaries")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{name}.md").write_text(summary_md, encoding="utf-8")
    except OSError as e:
        logger.warning(f"[BUDGET] Could not write summary file: {e}")

    # Prepend a routing header so the palace's auto-categorizer and search
    # queries like "2023 budget summary" land on this entry.
    indexed_text = f"Budget summary for dataset '{name}'.\n\n{summary_md}"
    try:
        from orchestrator import shared

        palace = shared.get_palace()
        doc_id = await palace.store(
            text=indexed_text,
            wing="personal",
            room="finance",
            source="budget_import",
            category="finance",
        )
        if doc_id:
            logger.info(f"[BUDGET] Indexed summary into mempalace: {doc_id}")
        else:
            logger.info("[BUDGET] Palace.store returned None (likely duplicate — re-import?)")
        return doc_id
    except Exception as e:
        logger.warning(f"[BUDGET] Failed to index summary into mempalace: {e}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Tool-facing query API
# ---------------------------------------------------------------------------


def strip_symbols_from_existing(dataset: Optional[str] = None) -> Dict[str, int]:
    """One-shot cleanup: strip emoji/symbol glyphs from existing category
    and payee values in the budget_transactions table.

    `dataset=None` cleans all datasets. Returns a dict with counts of rows
    updated. Safe to re-run (idempotent — rows already clean become no-ops).
    """
    # Update by DISTINCT value rather than per-row: there are typically
    # <100 unique categories and <500 unique payees across even a decade
    # of transactions, so one UPDATE per distinct value is both faster
    # (batched) and simpler (no rowid juggling).
    with state_store.get_db() as conn:
        where = "AND dataset = ?" if dataset else ""
        ds_params: list = [dataset] if dataset else []

        cat_updates = 0
        cats = conn.execute(
            f"SELECT DISTINCT category FROM budget_transactions WHERE category IS NOT NULL AND category <> '' {where}",
            ds_params,
        ).fetchall()
        for r in cats:
            orig = r["category"]
            new = _strip_symbol_glyphs(orig)
            if new != orig:
                upd_params = [new, orig] + ds_params
                conn.execute(
                    f"UPDATE budget_transactions SET category = ? WHERE category = ? {where}",
                    upd_params,
                )
                cat_updates += 1

        payee_updates = 0
        payees = conn.execute(
            f"SELECT DISTINCT payee FROM budget_transactions WHERE payee IS NOT NULL AND payee <> '' {where}",
            ds_params,
        ).fetchall()
        for r in payees:
            orig = r["payee"]
            new = _strip_symbol_glyphs(orig)
            if new != orig:
                upd_params = [new, orig] + ds_params
                conn.execute(
                    f"UPDATE budget_transactions SET payee = ? WHERE payee = ? {where}",
                    upd_params,
                )
                payee_updates += 1
        # get_db() context manager commits automatically on clean exit.

    logger.info(
        "[BUDGET] Stripped symbols: dataset=%s categories_updated=%d payees_updated=%d",
        dataset or "(all)",
        cat_updates,
        payee_updates,
    )
    return {"categories_updated": cat_updates, "payees_updated": payee_updates}


def list_datasets() -> List[Dict[str, Any]]:
    return state_store.list_budget_imports()


async def query(
    dataset: Optional[str] = None,
    question_type: str = "list_datasets",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    category_contains: Optional[str] = None,
    payee_contains: Optional[str] = None,
    amount_sign: Optional[str] = None,
    limit: int = 20,
    analysis_question: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatcher called by the query_budget tool. Returns a JSON-serializable dict.

    amount_sign defaults to 'outflow' for by_category/by_payee/by_month so those
    rankings aren't dominated by income rows (YNAB's 'Ready to Assign' category
    routinely dwarfs every spending category). Pass 'both' to include inflow.

    `analysis_question` is used only by question_type="analyze" — the user's
    original intent, passed through to ask_expert along with the aggregated
    data.

    Async because question_type="analyze" internally invokes ask_expert
    (handle_ask_expert). All other branches are synchronous SQL — this
    just makes the dispatcher await-able for the one path that needs it.
    """
    if question_type == "list_datasets":
        items = list_datasets()
        return {
            "datasets": [
                {
                    "name": d["name"],
                    "rows": d["row_count"],
                    "date_range": f"{d['date_min']}..{d['date_max']}",
                    "total_outflow": round(d["total_outflow"], 2),
                    "total_inflow": round(d["total_inflow"], 2),
                }
                for d in items
            ]
        }

    if not dataset:
        return {"error": "dataset is required for this question_type. Call list_datasets first if unsure."}

    if not state_store.get_budget_import(dataset):
        return {"error": f"Unknown dataset: {dataset!r}. Available: {[d['name'] for d in list_datasets()]}"}

    # Aggregation queries default to outflow-only. 'both'/'inflow' are
    # explicit opt-ins via amount_sign. total/outliers/list default to both.
    if amount_sign is None and question_type in ("by_category", "by_payee", "by_month"):
        amount_sign = "outflow"

    common_filters = dict(
        start_date=start_date,
        end_date=end_date,
        category=category,
        category_contains=category_contains,
        payee_contains=payee_contains,
        amount_sign=amount_sign if amount_sign in ("outflow", "inflow") else None,
        limit=limit,
    )

    if question_type == "total":
        # Aggregate in SQL so we don't miss rows past the list limit — prior
        # bug: summing a limit=20 slice in Python understated 2023 totals by
        # ~99% (17k rows in the dataset, 20 were summed).
        total_filters = {k: v for k, v in common_filters.items() if k != "limit"}
        agg_rows = state_store.query_budget_transactions(dataset, group_by="month", limit=10000, **total_filters)
        total_out = sum(g["outflow"] for g in agg_rows)
        total_in = sum(g["inflow"] for g in agg_rows)
        count = sum(g["count"] for g in agg_rows)
        return {
            "dataset": dataset,
            "filters": {k: v for k, v in common_filters.items() if v and k != "limit"},
            "matched_rows": count,
            "total_outflow": round(total_out, 2),
            "total_inflow": round(total_in, 2),
            "net": round(total_out + total_in, 2),
        }

    if question_type in ("by_category", "by_payee", "by_month"):
        group_by = question_type.split("_", 1)[1]
        groups = state_store.query_budget_transactions(dataset, group_by=group_by, **common_filters)
        return {
            "dataset": dataset,
            "group_by": group_by,
            "filters": {k: v for k, v in common_filters.items() if v},
            "groups": [
                {
                    "key": g["group_key"],
                    "total": round(g["total"], 2),
                    "outflow": round(g["outflow"], 2),
                    "inflow": round(g["inflow"], 2),
                    "count": g["count"],
                }
                for g in groups
            ],
        }

    if question_type == "analyze":
        # ONE-call pattern-finder. Gathers multi-dimensional aggregates
        # respecting the caller's filters, then internally invokes
        # ask_expert with the data so the user's question is answered
        # by the reasoning model rather than Qwen3.5-27B's tool-calling
        # loop (which has repeatedly run out of rounds at 5 calls without
        # producing a synthesis). Returns the expert's synthesis AND the
        # underlying data so the caller can see both.
        #
        # All sub-queries respect the same filter args (start_date,
        # end_date, category, payee_contains). amount_sign defaults to
        # 'outflow' for rankings so top-lists aren't dominated by income.
        # Top-N kept tight (5 categories / 5 payees / 3 outliers) so the
        # data payload fits comfortably in the expert's prompt budget.
        # by_month capped at 36 (3 years) so a wide-range analyze doesn't
        # blow the expert's prompt budget or the unified-loop 8KB tool
        # result cap on the return path — for longer horizons the caller
        # should narrow the start_date/end_date filter.
        #
        # `analysis_question` is strongly preferred (better expert synthesis
        # when it has the user's exact intent) but not strictly required.
        # Earlier a "required or error" gate was tried and it caused the
        # primary model to fail with a confusing retry loop when it called
        # analyze without the kwarg — better to succeed with a generic
        # pattern prompt than to force the primary to self-correct mid-turn.
        ranking_sign = amount_sign if amount_sign in ("outflow", "inflow") else "outflow"
        filter_args = dict(
            start_date=start_date,
            end_date=end_date,
            category=category,
            payee_contains=payee_contains,
        )
        # Totals (reuse existing month-rollup trick so we count everything
        # that matches, not just a limit-20 slice).
        totals_groups = state_store.query_budget_transactions(dataset, group_by="month", limit=10000, **filter_args)
        total_out = sum(g["outflow"] for g in totals_groups)
        total_in = sum(g["inflow"] for g in totals_groups)
        matched = sum(g["count"] for g in totals_groups)
        # Top 5 categories + 5 payees (outflow-ranked unless caller passed inflow).
        top_cats = state_store.query_budget_transactions(
            dataset, group_by="category", amount_sign=ranking_sign, limit=5, **filter_args
        )
        top_payees = state_store.query_budget_transactions(
            dataset, group_by="payee", amount_sign=ranking_sign, limit=5, **filter_args
        )
        # Monthly breakdown — cap at 36 months so a wide-range analyze
        # doesn't blow up the expert prompt and the unified-loop 8KB tool
        # result cap. 3 years of months is enough signal for "find
        # patterns"; for deeper history the caller should filter explicitly.
        monthly = state_store.query_budget_transactions(dataset, group_by="month", limit=36, **filter_args)
        # Top 3 outliers within the filter.
        outlier_rows = state_store.query_budget_outliers(dataset, start_date=start_date, end_date=end_date, limit=3)
        overview_data = {
            "dataset": dataset,
            "filters": {k: v for k, v in filter_args.items() if v},
            "ranking_sign": ranking_sign,
            "matched_rows": matched,
            "total_outflow": round(total_out, 2),
            "total_inflow": round(total_in, 2),
            "net": round(total_out + total_in, 2),
            "top_categories": [
                {"key": g["group_key"], "total": round(g["total"], 2), "count": g["count"]} for g in top_cats
            ],
            "top_payees": [
                {"key": g["group_key"], "total": round(g["total"], 2), "count": g["count"]} for g in top_payees
            ],
            "by_month": [
                {
                    "month": g["group_key"],
                    "outflow": round(g["outflow"], 2),
                    "inflow": round(g["inflow"], 2),
                    "net": round(g["total"], 2),
                    "count": g["count"],
                }
                for g in sorted(monthly, key=lambda x: x["group_key"])
            ],
            "outliers": [
                {
                    "date": r["txn_date"],
                    "amount": round(r["amount"], 2),
                    "category": r["category"],
                    "payee": r["payee"],
                }
                for r in outlier_rows
            ],
        }
        # Chain to ask_expert: build a compact prompt containing the
        # user's intent + the aggregated data, call the reasoning model,
        # return BOTH the synthesis and the underlying data.
        user_intent = (analysis_question or "").strip() or (
            "Find the most notable patterns, trends, and outliers in this spending data. Call out anything surprising."
        )
        filter_desc = ", ".join(f"{k}={v}" for k, v in overview_data["filters"].items()) or "no filters"
        import json as _json

        data_block = _json.dumps(overview_data, indent=2, default=str)
        expert_question = (
            f"Question from the user: {user_intent}\n\n"
            f"Dataset: {dataset} (filters: {filter_desc})\n\n"
            f"Aggregated data (totals, top categories, top payees, monthly "
            f"breakdown, outliers):\n```json\n{data_block}\n```\n\n"
            "Produce a tight, readable analysis that directly answers the "
            "user's question. Lead with the 2-3 most important findings. "
            "Quote specific numbers from the data. Don't hedge — if a "
            "pattern is clear, say so."
        )

        # Invoke ask_expert. Circuit breaker / disabled / timeout are all
        # handled inside handle_ask_expert — we always get back a string,
        # never an exception. Failure messages all start with "Expert
        # model" (see `_DISABLED_MSG`, `_UNREACHABLE_MSG`, `_CIRCUIT_OPEN_MSG`
        # in expert_agent.py) — detect and surface as a distinct field so
        # the primary model can tell "expert analyzed" from "expert down,
        # synthesize from the data yourself" and behave accordingly.
        from orchestrator.expert_agent import handle_ask_expert

        logger.info(
            "[BUDGET] analyze: delegating to expert (question=%d chars, data=%d chars)",
            len(expert_question),
            len(data_block),
        )
        expert_reply = await handle_ask_expert({"question": expert_question})
        is_expert_failure = isinstance(expert_reply, str) and expert_reply.startswith("Expert model")

        result_payload: Dict[str, Any] = {
            "question_type": "analyze",
            "dataset": dataset,
            "user_question": user_intent,
            "filters": overview_data["filters"],
            "data": overview_data,
        }
        if is_expert_failure:
            # Expert unreachable / disabled / circuit-open. The primary
            # model should fall back to synthesizing from `data` directly.
            result_payload["expert_synthesis"] = None
            result_payload["expert_error"] = expert_reply
            result_payload["hint"] = (
                "Expert reasoning model was unavailable; synthesize the findings "
                "yourself from the `data` field. Don't call query_budget again — "
                "everything you need to answer the user is already in `data`."
            )
        else:
            result_payload["expert_synthesis"] = expert_reply
        return result_payload

    if question_type == "outliers":
        rows = state_store.query_budget_outliers(dataset, start_date=start_date, end_date=end_date, limit=limit)
        return {
            "dataset": dataset,
            "outliers": [
                {
                    "date": r["txn_date"],
                    "amount": round(r["amount"], 2),
                    "category": r["category"],
                    "payee": r["payee"],
                    "description": r["description"],
                }
                for r in rows
            ],
        }

    if question_type == "list":
        rows = state_store.query_budget_transactions(dataset, group_by=None, **common_filters)
        return {
            "dataset": dataset,
            "filters": {k: v for k, v in common_filters.items() if v},
            "transactions": [
                {
                    "date": r["txn_date"],
                    "amount": round(r["amount"], 2),
                    "category": r["category"],
                    "payee": r["payee"],
                }
                for r in rows[:limit]
            ],
        }

    return {
        "error": f"Unknown question_type: {question_type!r}. Use one of: "
        f"list_datasets, analyze, total, by_category, by_payee, by_month, outliers, list."
    }
