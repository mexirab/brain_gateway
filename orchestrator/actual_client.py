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

Server-supplied SQL is confined to the temp copy. ``download_budget`` fetches
migration files from the Actual server and runs them with
``sqlite3.executescript()`` (``Actual.run_migrations``). Unconfined, a
compromised actual-server image — or anything answering as ``actual-budget``
on gateway-net — could ``ATTACH DATABASE '/app/data/brain_state.db'`` (or
``VACUUM INTO`` a path) and write any file the orchestrator can write.
``_confine_actualpy()`` therefore makes every SQLite connection actualpy opens
— the migration connection and every SQLAlchemy engine connection — refuse to
attach a second database (``SQLITE_LIMIT_ATTACHED`` = 0, which also blocks
``VACUUM INTO`` and plain ``VACUUM``), refuse PRAGMAs (all of them on the
migration connection; on engine connections the process-global ones —
``temp_store_directory``, ``hard_heap_limit`` and friends would change every
SQLite connection in the orchestrator), and interrupt SQL still running after
SNAPSHOT_TIMEOUT_SECONDS. It works by rebinding the two names actualpy's
``actual/__init__.py`` resolves at call time (``sqlite3`` and
``create_engine``) and checks, before every sync, that actualpy still uses
them that way — if a future actualpy opens its connections some other way,
the sync fails closed (``ActualSandboxError``, counted as a failed sync)
instead of running server SQL unconfined.
``tests/test_actual_sqlite_confinement.py`` runs malicious migrations through
the real ``Actual`` download path to pin this; run it on every actualpy bump.

What counts as spending: outflows from ON-BUDGET accounts that are not
transfers. Deliberately excluded: transfers of any kind — including
categorized transfers to off-budget accounts (loan / brokerage payments),
which Actual does count against a category. Those are savings or debt
service, not the discretionary spending the Quest Board health bar tracks.
Don't "fix" this without deciding that on purpose.

Configuration (all via ``orchestrator.config.settings``):
    ACTUAL_SERVER_URL            e.g. http://actual-budget:5006 (empty = disabled)
    ACTUAL_PASSWORD              server password
    ACTUAL_BUDGET_FILE           budget name as shown in Actual, or its sync id
    ACTUAL_ENCRYPTION_PASSWORD   only for end-to-end-encrypted budgets
    ACTUAL_FUN_MONEY_CATEGORY    category whose balance drives the health bar
    ACTUAL_SYNC_MONTHS           how many months (incl. current) to mirror
    ACTUAL_HISTORY_MONTHS        how many months the snapshot fetches for the
                                 query_budget "actual" dataset (>= sync months)
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import dis
import glob
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from orchestrator.config import settings

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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


class ActualSandboxError(RuntimeError):
    """actualpy no longer opens SQLite the way ``_confine_actualpy`` hooks.

    Raised before any network call, so server-supplied migration SQL never
    runs on an unconfined connection. Fix: re-check ``Actual.run_migrations``
    / ``Actual.create_engine`` in the installed actualpy and update the hook.
    """


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
    window_start: str  # ISO date, inclusive — the finance.db (Quest Board) window
    # Oldest date in ``transactions`` (ISO, inclusive). <= window_start: the
    # fetch covers ACTUAL_HISTORY_MONTHS for query_budget, and apply_snapshot
    # only mirrors rows from window_start on.
    history_start: str = ""
    transactions: list[ActualTransaction] = field(default_factory=list)
    categories: list[ActualCategory] = field(default_factory=list)
    # Balance of the fun-money category this month as the Actual UI shows it
    # (None when the category was not found, or this month's figures could
    # not be evaluated — see month_stats_error).
    fun_money_category: str | None = None
    fun_money_balance: Decimal | None = None
    # Exception type name when this month's budget figures could not be
    # evaluated: every category's budgeted/spent/balance is then 0 and
    # fun_money_balance is None. The sync still applies the transactions.
    month_stats_error: str | None = None
    # Why this snapshot is incomplete (bounded labels for
    # bgw_budget_sync_degraded_total): "month_stats_failed" |
    # "fun_money_missing". Empty = complete.
    degraded: list[str] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# SQLite confinement for server-supplied SQL (see the module docstring)
# ---------------------------------------------------------------------------

# Process-global in SQLite: setting one changes every connection in the
# orchestrator (state_store, finance.db, Chroma) until a restart — the temp
# file directory, or a heap cap after which every sqlite3.connect() raises
# MemoryError. Denied on engine connections; the migration connection denies
# every PRAGMA (no Actual migration up to 26.11.0 uses one).
_PROCESS_GLOBAL_PRAGMAS = frozenset(
    {"temp_store_directory", "data_store_directory", "hard_heap_limit", "soft_heap_limit"}
)
# Runaway SQL (e.g. a recursive CTE in a migration) is interrupted once a
# connection is older than this; checked every _PROGRESS_STEPS VM steps.
# Without it one bad migration pins the single sync worker forever.
_SQL_DEADLINE_SECONDS = SNAPSHOT_TIMEOUT_SECONDS
_PROGRESS_STEPS = 10_000


def _make_authorizer(*, allow_pragmas: bool) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    def authorize(action: int, arg1: str | None, _arg2: str | None, _db: str | None, _src: str | None) -> int:
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and (not allow_pragmas or (arg1 or "").lower() in _PROCESS_GLOBAL_PRAGMAS):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


_STRICT_AUTHORIZER = _make_authorizer(allow_pragmas=False)
# SQLAlchemy reflection needs PRAGMA table_xinfo / index_list / foreign_key_list.
_ENGINE_AUTHORIZER = _make_authorizer(allow_pragmas=True)


def confine_sqlite_connection(conn: sqlite3.Connection, *, allow_pragmas: bool = False) -> None:
    """Keep ``conn`` inside its own database file.

    ``SQLITE_LIMIT_ATTACHED`` = 0 is the control: ``ATTACH``, ``VACUUM INTO``
    and plain ``VACUUM`` (both attach internally) fail with "too many attached
    databases". The authorizer is a second layer for ATTACH (it fires first, so
    that is the error you see) and denies PRAGMAs — all of them, or with
    ``allow_pragmas`` only the process-global ones. A progress handler
    interrupts SQL still running ``_SQL_DEADLINE_SECONDS`` after connecting.

    Known consequence: Actual's 2021 migration ``remove_cache.js`` ends in
    ``VACUUM``, so a budget that has never run it (a very old export) fails
    closed on every sync until it is opened once in the Actual web app.
    """
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.set_authorizer(_ENGINE_AUTHORIZER if allow_pragmas else _STRICT_AUTHORIZER)
    deadline = time.monotonic() + _SQL_DEADLINE_SECONDS
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), _PROGRESS_STEPS)


class _ConfinedSqlite3:
    """Stands in for the ``sqlite3`` module inside actualpy.

    ``connect()`` returns a confined connection. Only exception classes and
    plain constants pass through besides it: anything else that could open a
    connection another way (``sqlite3.Connection``, ``dbapi2``) raises
    AttributeError, so a refactored actualpy fails instead of running
    server SQL unconfined.
    """

    def __getattr__(self, name: str) -> Any:
        value = getattr(sqlite3, name)
        if (isinstance(value, type) and issubclass(value, BaseException)) or isinstance(value, (int, str, tuple)):
            return value
        raise AttributeError(f"sqlite3.{name} is not available to actualpy (SQLite confinement)")

    @staticmethod
    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = sqlite3.connect(*args, **kwargs)
        confine_sqlite_connection(conn)
        return conn


_CONFINED_SQLITE3 = _ConfinedSqlite3()
_confine_lock = threading.Lock()


def _confine_engine_connection(dbapi_connection: sqlite3.Connection, _connection_record: object) -> None:
    confine_sqlite_connection(dbapi_connection, allow_pragmas=True)


def _confined_create_engine(real: Callable[..., Engine]) -> Callable[..., Engine]:
    from sqlalchemy import event

    def create_engine(*args: Any, **kwargs: Any) -> Engine:
        engine = real(*args, **kwargs)
        # Registered before anything can connect: actualpy reflects the
        # schema right after creating the engine.
        event.listen(engine, "connect", _confine_engine_connection)
        return engine

    create_engine._bgw_confined = True  # type: ignore[attr-defined]
    return create_engine


def _global_uses(func: Callable[..., Any], name: str) -> list[str | None]:
    """For each load of global ``name`` in ``func``: the attribute read off it
    right after (``sqlite3.connect`` -> "connect"), or None."""
    ins = list(dis.get_instructions(func))
    return [
        ins[i + 1].argval if i + 1 < len(ins) and ins[i + 1].opname in ("LOAD_ATTR", "LOAD_METHOD") else None
        for i, op in enumerate(ins)
        if op.opname == "LOAD_GLOBAL" and op.argval == name
    ]


def _confine_actualpy(actual_cls: type) -> None:
    """Verify, then install (once), the SQLite hook in actualpy.

    actualpy 0.22.x opens the migration connection with ``sqlite3.connect``
    in ``Actual.run_migrations`` and the SQLAlchemy engine with
    ``create_engine`` in ``Actual.create_engine``; both names are module
    globals looked up at call time, so rebinding them in the globals of the
    functions that use them confines every connection without copying
    actualpy code. Raises ``ActualSandboxError`` — before any network call and
    before touching anything — if either method stopped using that global
    that way. The check reads bytecode, so it catches refactors, not every
    possible change: when upgrading actualpy, re-read ``run_migrations`` and
    run ``tests/test_actual_sqlite_confinement.py``.
    """
    run_migrations = actual_cls.run_migrations
    make_engine = actual_cls.create_engine
    problems: list[str] = []
    uses = _global_uses(run_migrations, "sqlite3")
    if not uses or any(attr != "connect" for attr in uses):
        problems.append("Actual.run_migrations no longer opens its connection with sqlite3.connect")
    elif run_migrations.__globals__.get("sqlite3") not in (sqlite3, _CONFINED_SQLITE3):
        problems.append("Actual.run_migrations resolves sqlite3 to something unexpected")
    if not _global_uses(make_engine, "create_engine") or not callable(make_engine.__globals__.get("create_engine")):
        problems.append("Actual.create_engine no longer calls create_engine")
    if problems:
        raise ActualSandboxError(
            "refusing to sync: cannot confine actualpy's SQLite connections (" + "; ".join(problems) + ")"
        )

    with _confine_lock:
        run_migrations.__globals__["sqlite3"] = _CONFINED_SQLITE3
        current = make_engine.__globals__["create_engine"]
        if not getattr(current, "_bgw_confined", False):
            make_engine.__globals__["create_engine"] = _confined_create_engine(current)


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

    # Before Actual(...): its __enter__ downloads the budget and runs the
    # server's migration SQL.
    _confine_actualpy(Actual)

    today = today or _dt.date.today()
    start = sync_window_start(today)
    history_start = min(start, sync_window_start(today, settings.actual_history_months))
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
            history_start=history_start.isoformat(),
        )

        # One pass over this month's budget gives budgeted / spent /
        # accumulated balance per category exactly as the Actual budget
        # page shows them (envelope or tracking mode). A budget layout
        # actualpy can't evaluate must not fail the sync over display data —
        # but it is recorded, so the sync counts as degraded, not clean.
        try:
            month_stats = _month_category_stats(s, today)
        except Exception as e:  # noqa: BLE001 — display data only
            logger.warning(
                "[ACTUAL] Could not evaluate this month's budget: %s — category figures are 0 and the "
                "fun-money balance is not updated this sync",
                safe_error(e),
            )
            snap.month_stats_error = type(e).__name__
            month_stats = {}

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

        # One root cause per snapshot: failed month stats also leave the
        # fun-money balance unknown, so that is not counted twice.
        if snap.month_stats_error:
            snap.degraded.append("month_stats_failed")
        elif fun_name and snap.fun_money_balance is None:
            snap.degraded.append("fun_money_missing")

        # Spendable lines in the history window: on-budget accounts, no transfers.
        # get_transactions() returns split legs individually (each carries
        # its own category) and single transactions as-is; parents of
        # splits are excluded. Deleted rows are excluded by default.
        for t in get_transactions(s, start_date=history_start, transfer=False, off_budget=False):
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
    category. Raises if the budget layout is one actualpy can't evaluate;
    ``fetch_snapshot`` records that as ``month_stats_error`` and carries on.
    """
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
