"""
Read-only client for a self-hosted Actual Budget server (https://actualbudget.org).

Replaces the YNAB API integration (2026-10). Actual has no hosted REST API:
clients sync the whole budget file from the server and query a local SQLite
copy. ``actualpy`` (https://github.com/bvanelli/actualpy) implements that
protocol in Python. This module wraps it in one blocking call,
``fetch_snapshot()``, that downloads the budget, extracts exactly what the
Financial Quest Board needs into plain dataclasses, and throws the local copy
away. Callers run it via ``asyncio.to_thread`` (see ``fetch_snapshot_async``).

Strictly READ-ONLY: this module never calls ``Actual.commit()`` or any
``create_*`` helper, so nothing it does can change the user's budget.
(Verified against actualpy 0.22.4: ``download_budget`` only pulls; the
context manager's ``__exit__`` only closes the session/engine.)

What counts as spending: outflows from ON-BUDGET accounts that are not
transfers. Deliberately excluded: transfers of any kind — including
categorized transfers to off-budget accounts (loan / brokerage payments),
which Actual does count against a category. Those are savings or debt
service, not the discretionary spending the Quest Board health bar tracks.
Don't "fix" this without deciding that on purpose.

Configuration (all via ``orchestrator.config.settings``):
    ACTUAL_SERVER_URL            e.g. http://10.0.0.248:5006 (empty = disabled)
    ACTUAL_PASSWORD              server password
    ACTUAL_BUDGET_FILE           budget name as shown in Actual, or its sync id
    ACTUAL_ENCRYPTION_PASSWORD   only for end-to-end-encrypted budgets
    ACTUAL_FUN_MONEY_CATEGORY    category whose balance drives the health bar
    ACTUAL_SYNC_MONTHS           how many months (incl. current) to mirror
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import glob
import logging
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from orchestrator.config import settings

logger = logging.getLogger(__name__)

# Hard ceiling on one sync (login + budget download + queries). The budget file
# is downloaded in full each time; a few MB on the LAN takes ~1-3 s.
SNAPSHOT_TIMEOUT_SECONDS = 120
# Per-request httpx timeouts inside actualpy (several requests per sync).
_HTTP_TIMEOUT = (5.0, 30.0)  # (connect, read/write/pool)

_TMP_PREFIX = "actual-sync-"

# Dedicated single worker: a hung Actual server can tie up at most this one
# thread, never the default executor every other asyncio.to_thread() shares.
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="actual-sync")


class ActualNotConfigured(RuntimeError):
    """Raised when the Actual integration is not configured."""


class ActualSyncBusy(RuntimeError):
    """A previous download is still running (it outlived its timeout)."""


# The worker thread of the most recent fetch. asyncio.wait_for cannot stop a
# thread, so after a timeout the download may still be running; refusing to
# start another until it finishes keeps "one download at a time" true.
_inflight: asyncio.Future | None = None


@dataclass(frozen=True)
class ActualTransaction:
    """One spendable line: a single transaction or one leg of a split."""

    id: str
    date: str  # ISO YYYY-MM-DD
    amount: Decimal  # signed; negative = outflow
    payee: str
    category: str
    category_group: str
    notes: str


@dataclass(frozen=True)
class ActualCategory:
    name: str
    group: str
    is_income: bool
    hidden: bool
    # This month, as the Actual budget page shows them (0 when unknown).
    budgeted: Decimal = Decimal(0)
    spent: Decimal = Decimal(0)  # positive = money spent
    balance: Decimal = Decimal(0)  # accumulated, incl. rollover


@dataclass
class ActualSnapshot:
    budget_name: str
    window_start: str  # ISO date, inclusive
    transactions: list[ActualTransaction] = field(default_factory=list)
    categories: list[ActualCategory] = field(default_factory=list)
    # Balance of the fun-money category this month as the Actual UI shows it
    # (None when the category was not found).
    fun_money_category: str | None = None
    fun_money_balance: Decimal | None = None


def is_configured() -> bool:
    """True when enough settings are present to attempt a sync."""
    return bool(settings.actual_server_url and settings.actual_password and settings.actual_budget_file)


def sync_window_start(today: _dt.date | None = None, months: int | None = None) -> _dt.date:
    """First day of the oldest month in the mirrored window.

    ``months=3`` on 2026-10-02 -> 2026-08-01 (Aug, Sep, Oct).
    """
    today = today or _dt.date.today()
    months = max(1, int(months if months is not None else settings.actual_sync_months))
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    return _dt.date(y, m, 1)


def _name(obj: Any) -> str:
    return (getattr(obj, "name", None) or "").strip()


def fetch_snapshot(today: _dt.date | None = None) -> ActualSnapshot:
    """Download the budget and return a read-only snapshot. Blocking.

    Raises ``ActualNotConfigured`` when settings are missing; any actualpy /
    network error propagates to the caller (which records it as a failed sync).
    """
    if not is_configured():
        raise ActualNotConfigured("Actual Budget is not configured")

    # Imported lazily so the orchestrator still starts (and finance still
    # works in manual mode) on an install without the dependency.
    from actual import Actual
    from actual.database import Categories
    from actual.queries import get_transactions
    from sqlmodel import select

    today = today or _dt.date.today()
    start = sync_window_start(today)
    fun_name = (settings.actual_fun_money_category or "").strip().lower()

    import httpx

    with (
        tempfile.TemporaryDirectory(prefix=_TMP_PREFIX) as data_dir,
        Actual(
            base_url=settings.actual_server_url.rstrip("/"),
            password=settings.actual_password,
            file=settings.actual_budget_file,
            encryption_password=settings.actual_encryption_password or None,
            data_dir=data_dir,
            timeout=httpx.Timeout(_HTTP_TIMEOUT[1], connect=_HTTP_TIMEOUT[0]),
        ) as actual,
    ):
        s = actual.session
        snap = ActualSnapshot(
            budget_name=_name(getattr(actual, "_file", None)) or settings.actual_budget_file,
            window_start=start.isoformat(),
        )

        # One pass over this month's budget gives budgeted / spent /
        # accumulated balance per category exactly as the Actual budget
        # page shows them (envelope or tracking mode).
        month_stats = _month_category_stats(s, today)

        # Plain select: actualpy's get_categories() eager-joins every
        # transaction of every category, which we don't need.
        for cat in s.exec(select(Categories).where(Categories.tombstone == 0)).all():
            name = _name(cat)
            budgeted, spent, balance = month_stats.get(getattr(cat, "id", None), (Decimal(0),) * 3)
            snap.categories.append(
                ActualCategory(
                    name=name,
                    group=_name(getattr(cat, "group", None)),
                    is_income=bool(getattr(cat, "is_income", 0)),
                    hidden=bool(getattr(cat, "hidden", 0)),
                    budgeted=budgeted,
                    spent=spent,
                    balance=balance,
                )
            )
            # Substring, case-insensitive: Actual users often prefix
            # category names with emoji ("🎮 Fun Money"). Spending
            # categories only.
            if (
                snap.fun_money_category is None
                and fun_name
                and fun_name in name.lower()
                and not getattr(cat, "is_income", 0)
                and not getattr(cat, "hidden", 0)
            ):
                snap.fun_money_category = name
                snap.fun_money_balance = balance if getattr(cat, "id", None) in month_stats else None

        # Spendable lines in the window: on-budget accounts, no transfers.
        # get_transactions() returns split legs individually (each carries
        # its own category) and single transactions as-is; parents of
        # splits are excluded. Deleted rows are excluded by default.
        for t in get_transactions(s, start_date=start, transfer=False, off_budget=False):
            if getattr(t, "starting_balance_flag", 0):
                continue
            payee = getattr(t, "payee", None)
            if payee is None and getattr(t, "parent", None) is not None:
                payee = getattr(t.parent, "payee", None)
            cat = getattr(t, "category", None)
            snap.transactions.append(
                ActualTransaction(
                    id=str(t.id),
                    date=t.get_date().isoformat(),
                    amount=Decimal(t.get_amount()),
                    payee=_name(payee),
                    category=_name(cat),
                    category_group=_name(getattr(cat, "group", None)) if cat is not None else "",
                    notes=(getattr(t, "notes", None) or "").strip(),
                )
            )

    return snap


def _month_category_stats(session: Any, today: _dt.date) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """category_id -> (budgeted, spent, accumulated_balance) for ``today``'s month.

    Uses actualpy's budget-history walk once instead of one query per
    category. Returns {} (all zeros downstream) if the budget layout is one
    actualpy can't evaluate — the sync itself must not fail over display data.
    """
    try:
        from actual.budgets import get_budget_history

        history = get_budget_history(session, today)
        if not history:
            return {}
        month = history[-1]
        out: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
        for group in list(getattr(month, "category_groups", []) or []):
            for bc in list(getattr(group, "categories", []) or []):
                out[bc.id] = (
                    Decimal(bc.budgeted or 0),
                    -Decimal(bc.spent or 0),
                    _balance_of(bc),
                )
        return out
    except Exception as e:  # noqa: BLE001 — display data only
        logger.warning("[ACTUAL] Could not evaluate this month's budget: %s", e)
        return {}


def _balance_of(bc: Any) -> Decimal:
    """Balance the Actual UI shows: accumulated (with rollover) when known.

    Explicit ``is None`` check — an envelope spent to exactly $0.00 is the
    common case and must not fall through to the month-only balance.
    """
    acc = getattr(bc, "accumulated_balance", None)
    if acc is not None:
        return Decimal(acc)
    return Decimal(getattr(bc, "balance", None) or 0)


async def fetch_snapshot_async(today: _dt.date | None = None) -> ActualSnapshot:
    """``fetch_snapshot`` off the event loop, with a hard timeout.

    Raises ``ActualSyncBusy`` if a previous download outlived its timeout and
    is still running, and ``asyncio.TimeoutError`` after
    SNAPSHOT_TIMEOUT_SECONDS (the thread keeps running; see ``_inflight``).
    """
    global _inflight
    if _inflight is not None and not _inflight.done():
        raise ActualSyncBusy("previous Actual Budget download is still running")
    fut = asyncio.get_running_loop().run_in_executor(_EXECUTOR, fetch_snapshot, today)
    _inflight = fut
    return await asyncio.wait_for(asyncio.shield(fut), timeout=SNAPSHOT_TIMEOUT_SECONDS)


_URL_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@")


def safe_error(e: BaseException) -> str:
    """Exception text safe to log/store/return: no URL credentials, bounded."""
    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return _URL_USERINFO_RE.sub(r"\1***@", msg)[:500]


def cleanup_stale_tempdirs() -> int:
    """Remove budget copies left behind by a sync killed mid-download.

    TemporaryDirectory cleans up on normal exit, but an OOM kill or container
    restart mid-sync leaves a full plaintext copy of the budget in /tmp
    (overlay filesystem, survives restarts). Called once at startup, before
    any sync can be running.
    """
    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), _TMP_PREFIX + "*")):
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as e:
            logger.warning("[ACTUAL] Could not remove stale budget copy %s: %s", path, e)
    if removed:
        logger.info("[ACTUAL] Removed %d stale budget temp copies", removed)
    return removed
