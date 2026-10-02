"""
Tests for the Actual Budget sync that replaced YNAB (2026-10).

Network-free: ``apply_snapshot`` is driven with hand-built snapshots, and
``sync_budget_transactions`` with a patched ``fetch_snapshot_async``. A live
round-trip against a real actual-server was run separately during development
(see the PR description); these pin the DB semantics.
"""

import asyncio
import datetime as dt
import sqlite3
import types
from decimal import Decimal

import pytest
from pydantic import ValidationError

from orchestrator import actual_client, finance_manager
from orchestrator.actual_client import ActualCategory, ActualSnapshot, ActualTransaction


@pytest.fixture
def fdb(tmp_path, monkeypatch):
    """Fresh finance.db per test."""
    path = str(tmp_path / "finance.db")
    monkeypatch.setattr(finance_manager, "DB_PATH", path)
    finance_manager._category_cache = None
    finance_manager.init_db()
    return path


def _q(path, sql, args=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _month(offset=0):
    d = dt.date.today().replace(day=1)
    for _ in range(abs(offset)):
        d = (d - dt.timedelta(days=1)).replace(day=1) if offset < 0 else (d + dt.timedelta(days=32)).replace(day=1)
    return d


def _tx(tid, amount, *, date=None, payee="Shop", category="Dining Out", group="Wants", notes=""):
    return ActualTransaction(
        id=tid,
        date=(date or dt.date.today()).isoformat(),
        amount=Decimal(str(amount)),
        payee=payee,
        category=category,
        category_group=group,
        notes=notes,
    )


def _snap(txns, *, fun_balance="120.01", window_months=3):
    start = _month(-(window_months - 1))
    return ActualSnapshot(
        budget_name="Test Budget",
        window_start=start.isoformat(),
        transactions=list(txns),
        categories=[
            ActualCategory("🎮 Fun Money", "Wants", False, False, Decimal(200), Decimal("79.99"), Decimal("120.01")),
            ActualCategory("Dining Out", "Wants", False, False, Decimal(300), Decimal("72.5"), Decimal("227.5")),
            ActualCategory("Rent", "Bills", False, False, Decimal(0), Decimal(1500), Decimal(-1500)),
            ActualCategory("Income", "Income", True, False),
        ],
        fun_money_category="🎮 Fun Money" if fun_balance is not None else None,
        fun_money_balance=Decimal(fun_balance) if fun_balance is not None else None,
    )


def _map(path, **cats):
    conn = sqlite3.connect(path)
    with conn:
        for name, disc in cats.items():
            conn.execute(
                "INSERT OR REPLACE INTO category_mapping (category_name, is_discretionary) VALUES (?, ?)",
                (name.replace("_", " "), 1 if disc else 0),
            )
    conn.close()


def _spent(path, ym):
    rows = _q(path, "SELECT discretionary_spent, discretionary_budget FROM budget_periods WHERE year_month = ?", (ym,))
    return (rows[0]["discretionary_spent"], rows[0]["discretionary_budget"]) if rows else (None, None)


# --------------------------------------------------------------------------- apply_snapshot


def test_inserts_outflows_skips_inflows_and_flags_discretionary(fdb):
    _map(fdb, Dining_Out=True, Rent=False)
    res = finance_manager.apply_snapshot(
        _snap(
            [
                _tx("a", "-42.50", payee="Pizza Place"),
                _tx("b", "-1500", payee="Landlord", category="Rent", group="Bills"),
                _tx("c", "3000", payee="Employer", category=""),
            ]
        )
    )
    assert res["inserted"] == 2 and res["skipped_inflows"] == 1 and res["deleted"] == 0
    rows = {r["external_id"]: r for r in _q(fdb, "SELECT * FROM transactions")}
    assert set(rows) == {"actual:a", "actual:b"}
    assert rows["actual:a"]["amount"] == 42.5 and rows["actual:a"]["is_discretionary"] == 1
    assert rows["actual:a"]["source"] == "actual" and rows["actual:a"]["subcategory"] == "Wants"
    assert rows["actual:b"]["is_discretionary"] == 0
    ym = dt.date.today().strftime("%Y-%m")
    spent, budget = _spent(fdb, ym)
    assert spent == pytest.approx(42.5)
    # Health bar: budget - spent == Actual's fun-money balance.
    assert budget - spent == pytest.approx(120.01)


def test_resync_is_idempotent_and_updates_in_place(fdb):
    _map(fdb, Dining_Out=True)
    finance_manager.apply_snapshot(_snap([_tx("a", "-10")]))
    res = finance_manager.apply_snapshot(_snap([_tx("a", "-25", payee="Edited")]))
    assert res["inserted"] == 0 and res["updated"] == 1
    rows = _q(fdb, "SELECT amount, name FROM transactions WHERE external_id='actual:a'")
    assert len(rows) == 1 and rows[0]["amount"] == 25 and rows[0]["name"] == "Edited"
    assert _spent(fdb, dt.date.today().strftime("%Y-%m"))[0] == pytest.approx(25)


def test_rows_deleted_upstream_are_removed_and_totals_recomputed(fdb):
    _map(fdb, Dining_Out=True)
    finance_manager.apply_snapshot(_snap([_tx("a", "-10"), _tx("b", "-5")]))
    res = finance_manager.apply_snapshot(_snap([_tx("a", "-10")]))
    assert res["deleted"] == 1
    assert {r["external_id"] for r in _q(fdb, "SELECT external_id FROM transactions")} == {"actual:a"}
    assert _spent(fdb, dt.date.today().strftime("%Y-%m"))[0] == pytest.approx(10)


def test_redated_into_previous_month_moves_spend_between_periods(fdb):
    _map(fdb, Dining_Out=True)
    this, prev = _month(0), _month(-1)
    finance_manager.apply_snapshot(_snap([_tx("a", "-30", date=this)]))
    finance_manager.apply_snapshot(_snap([_tx("a", "-30", date=prev.replace(day=15))]))
    assert _spent(fdb, this.strftime("%Y-%m"))[0] == pytest.approx(0)
    assert _spent(fdb, prev.strftime("%Y-%m"))[0] == pytest.approx(30)


def test_rows_older_than_window_are_left_alone(fdb):
    _map(fdb, Dining_Out=True)
    old = _month(-6).replace(day=10)
    conn = sqlite3.connect(fdb)
    with conn:
        conn.execute(
            "INSERT INTO transactions (external_id, date, amount, name, category, is_discretionary, budget_period, source)"
            " VALUES ('actual:old', ?, 9, 'Old', 'Dining Out', 1, ?, 'actual')",
            (old.isoformat(), old.strftime("%Y-%m")),
        )
    conn.close()
    res = finance_manager.apply_snapshot(_snap([]))
    assert res["deleted"] == 0
    assert _q(fdb, "SELECT 1 FROM transactions WHERE external_id='actual:old'")


def test_manual_entries_survive_sync_and_count_toward_spend(fdb):
    _map(fdb, Dining_Out=True)
    ym = dt.date.today().strftime("%Y-%m")
    conn = sqlite3.connect(fdb)
    with conn:
        conn.execute(
            "INSERT INTO transactions (date, amount, name, is_discretionary, budget_period, source)"
            " VALUES (?, 7, 'Cash coffee', 1, ?, 'manual')",
            (dt.date.today().isoformat(), ym),
        )
    conn.close()
    finance_manager.apply_snapshot(_snap([_tx("a", "-10")]))
    finance_manager.apply_snapshot(_snap([]))  # upstream row removed
    assert _q(fdb, "SELECT 1 FROM transactions WHERE source='manual'")
    assert _spent(fdb, ym)[0] == pytest.approx(7)


def test_missing_fun_money_category_leaves_budget_untouched(fdb):
    ym = dt.date.today().strftime("%Y-%m")
    finance_manager.apply_snapshot(_snap([], fun_balance=None))
    _, budget = _spent(fdb, ym)
    assert budget == pytest.approx(1000.0)  # seeded monthly_discretionary default


def test_sync_state_recorded(fdb):
    finance_manager.apply_snapshot(_snap([_tx("a", "-1")]))
    st = finance_manager._status_sync()
    assert st["provider"] == "actual" and st["budget_name"] == "Test Budget"
    assert st["last_synced_at"] and st["last_error"] is None and st["last_result"]["inserted"] == 1


# --------------------------------------------------------------------------- migration


def test_legacy_ynab_schema_is_migrated(tmp_path, monkeypatch):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(
            """
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ynab_transaction_id TEXT UNIQUE, date TEXT NOT NULL,
                amount REAL NOT NULL, name TEXT NOT NULL, merchant_name TEXT, category TEXT, subcategory TEXT,
                is_discretionary INTEGER NOT NULL DEFAULT 1, budget_period TEXT,
                source TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE ynab_category_mapping (category_name TEXT PRIMARY KEY, is_discretionary INTEGER NOT NULL DEFAULT 0);
            INSERT INTO transactions (ynab_transaction_id, date, amount, name, budget_period, source)
                VALUES ('y1', '2026-01-05', 12, 'Old YNAB row', '2026-01', 'ynab');
            INSERT INTO ynab_category_mapping VALUES ('Dining Out', 1);
            """
        )
    conn.close()
    monkeypatch.setattr(finance_manager, "DB_PATH", path)
    finance_manager.init_db()
    finance_manager.init_db()  # idempotent
    assert _q(path, "SELECT external_id FROM transactions")[0]["external_id"] == "ynab:y1"
    assert _q(path, "SELECT is_discretionary FROM category_mapping WHERE category_name='Dining Out'")[0][0] == 1
    # New syncs work against the migrated table (needs the unique index for ON CONFLICT).
    finance_manager.apply_snapshot(_snap([_tx("a", "-3")]))
    assert _q(path, "SELECT 1 FROM transactions WHERE external_id='actual:a'")


# --------------------------------------------------------------------------- orchestration / client


@pytest.mark.asyncio
async def test_sync_not_configured_is_a_noop(fdb, monkeypatch):
    monkeypatch.setattr(actual_client.settings, "actual_server_url", "")
    res = await finance_manager.sync_budget_transactions()
    assert res["synced"] == 0 and "not configured" in res["error"]


@pytest.mark.asyncio
async def test_fetch_failure_recorded_without_raising(fdb, monkeypatch):
    monkeypatch.setattr(actual_client.settings, "actual_server_url", "http://127.0.0.1:1")
    monkeypatch.setattr(actual_client.settings, "actual_password", "x")
    monkeypatch.setattr(actual_client.settings, "actual_budget_file", "B")

    async def boom(today=None):
        raise ConnectionError("refused")

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", boom)
    res = await finance_manager.sync_budget_transactions()
    assert res["synced"] == 0 and "refused" in res["error"]
    st = finance_manager._status_sync()
    assert st["connected"] is False and "refused" in st["last_error"]


@pytest.mark.asyncio
async def test_successful_sync_populates_category_cache(fdb, monkeypatch):
    monkeypatch.setattr(actual_client.settings, "actual_server_url", "http://127.0.0.1:1")
    monkeypatch.setattr(actual_client.settings, "actual_password", "x")
    monkeypatch.setattr(actual_client.settings, "actual_budget_file", "B")

    async def ok(today=None):
        return _snap([_tx("a", "-4")])

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", ok)
    res = await finance_manager.sync_budget_transactions()
    assert res["inserted"] == 1
    cats = await finance_manager.get_budget_categories()
    names = {c["name"] for g in cats["groups"] for c in g["categories"]}
    assert "Dining Out" in names and "Income" not in names  # income categories hidden


@pytest.mark.parametrize(
    "today,months,expected",
    [
        (dt.date(2026, 10, 2), 3, dt.date(2026, 8, 1)),
        (dt.date(2026, 2, 28), 3, dt.date(2025, 12, 1)),
        (dt.date(2026, 1, 1), 1, dt.date(2026, 1, 1)),
        (dt.date(2026, 1, 15), 13, dt.date(2025, 1, 1)),
    ],
)
def test_sync_window_start(today, months, expected):
    assert actual_client.sync_window_start(today, months) == expected


def test_config_validator_disables_incomplete_actual_config(monkeypatch):
    from orchestrator.config import Settings

    s = Settings(actual_server_url="http://10.0.0.248:5006", actual_password="", actual_budget_file="X")
    assert s.actual_server_url == ""
    s = Settings(actual_server_url="ftp://x", actual_password="p", actual_budget_file="X")
    assert s.actual_server_url == ""
    s = Settings(
        actual_server_url="http://10.0.0.248:5006",
        actual_password="p",
        actual_budget_file="X",
        actual_sync_interval=1,
        actual_sync_months=99,
    )
    assert s.actual_server_url and s.actual_sync_interval == 5 and s.actual_sync_months == 24


# --------------------------------------------------------------------------- review fixes (2026-10-02)


def test_legacy_ynab_rows_in_window_are_replaced_not_double_counted(fdb):
    _map(fdb, Dining_Out=True)
    ym = dt.date.today().strftime("%Y-%m")
    old = _month(-6).replace(day=3)
    conn = sqlite3.connect(fdb)
    with conn:
        conn.execute(
            "INSERT INTO transactions (external_id, date, amount, name, category, is_discretionary, budget_period, source)"
            " VALUES ('ynab:x', ?, 42.5, 'Pizza (YNAB)', 'Dining Out', 1, ?, 'ynab')",
            (dt.date.today().isoformat(), ym),
        )
        conn.execute(
            "INSERT INTO transactions (external_id, date, amount, name, category, is_discretionary, budget_period, source)"
            " VALUES ('ynab:old', ?, 9, 'History', 'Dining Out', 1, ?, 'ynab')",
            (old.isoformat(), old.strftime("%Y-%m")),
        )
    conn.close()
    res = finance_manager.apply_snapshot(_snap([_tx("a", "-42.50", payee="Pizza")]))
    assert res["legacy_removed"] == 1
    assert _spent(fdb, ym)[0] == pytest.approx(42.5)  # not 85
    assert _q(fdb, "SELECT 1 FROM transactions WHERE external_id='ynab:old'")  # pre-window history kept


def test_row_redated_from_before_window_recomputes_old_month(fdb):
    _map(fdb, Dining_Out=True)
    old = _month(-6).replace(day=10)
    conn = sqlite3.connect(fdb)
    with conn:
        conn.execute(
            "INSERT INTO budget_periods (year_month, discretionary_budget, discretionary_spent) VALUES (?, 1000, 30)",
            (old.strftime("%Y-%m"),),
        )
        conn.execute(
            "INSERT INTO transactions (external_id, date, amount, name, category, is_discretionary, budget_period,"
            " source, created_at) VALUES ('actual:a', ?, 30, 'X', 'Dining Out', 1, ?, 'actual', datetime('now','-1 day'))",
            (old.isoformat(), old.strftime("%Y-%m")),
        )
    conn.close()
    res = finance_manager.apply_snapshot(_snap([_tx("a", "-30")]))  # now dated today
    assert res["updated"] == 1 and res["inserted"] == 0
    assert _spent(fdb, old.strftime("%Y-%m"))[0] == pytest.approx(0)
    assert _spent(fdb, dt.date.today().strftime("%Y-%m"))[0] == pytest.approx(30)


# --------------------------------------------------------------------------- client mapping (fake actualpy)


class _FakeCat:
    def __init__(self, cid, name, group, is_income=0, hidden=0):
        self.id, self.name, self.is_income, self.hidden = cid, name, is_income, hidden
        self.group = types.SimpleNamespace(name=group)


class _FakeTx:
    def __init__(self, tid, date, amount, payee=None, cat=None, parent=None, start=0, notes=None):
        self.id, self._d, self._a, self.category, self.parent = tid, date, Decimal(amount), cat, parent
        self.payee = types.SimpleNamespace(name=payee) if payee else None
        self.starting_balance_flag, self.notes = start, notes

    def get_date(self):
        return self._d

    def get_amount(self):
        return self._a


def _install_fake_actual(monkeypatch, cats, txns, stats):
    import sys

    class FakeActual:
        def __init__(self, **kw):
            self.session = types.SimpleNamespace(
                exec=lambda q: types.SimpleNamespace(all=lambda: cats),
            )
            self._file = types.SimpleNamespace(name="Fake Budget")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    queries = types.SimpleNamespace(get_transactions=lambda s, **kw: txns)
    month = types.SimpleNamespace(
        category_groups=[types.SimpleNamespace(categories=[types.SimpleNamespace(id=k, **v) for k, v in stats.items()])]
    )
    budgets = types.SimpleNamespace(get_budget_history=lambda s, d: [month])
    database = types.SimpleNamespace(Categories=types.SimpleNamespace(tombstone=0))
    sqlmodel = types.SimpleNamespace(select=lambda m: types.SimpleNamespace(where=lambda *_: None))
    monkeypatch.setitem(sys.modules, "actual", types.SimpleNamespace(Actual=FakeActual))
    monkeypatch.setitem(sys.modules, "actual.queries", queries)
    monkeypatch.setitem(sys.modules, "actual.budgets", budgets)
    monkeypatch.setitem(sys.modules, "actual.database", database)
    monkeypatch.setitem(sys.modules, "sqlmodel", sqlmodel)
    monkeypatch.setattr(actual_client.settings, "actual_server_url", "http://fake:5006")
    monkeypatch.setattr(actual_client.settings, "actual_password", "p")
    monkeypatch.setattr(actual_client.settings, "actual_budget_file", "Fake Budget")
    monkeypatch.setattr(actual_client.settings, "actual_fun_money_category", "fun money")


def test_fetch_snapshot_mapping(monkeypatch):
    today = dt.date(2026, 10, 2)
    fun = _FakeCat("c1", "🎮 Fun Money", "Wants")
    income_fun = _FakeCat("c0", "Fun Money Income", "Income", is_income=1)  # must not win the match
    din = _FakeCat("c2", "Dining Out", "Wants")
    parent = types.SimpleNamespace(payee=types.SimpleNamespace(name="Target"))
    txns = [
        _FakeTx("t1", today, "-20", cat=fun, parent=parent),  # split leg: payee from parent
        _FakeTx("t2", today, "5000", payee="Starting Balance", start=1),  # skipped
        _FakeTx("t3", today, "-12.5", payee="Cafe", cat=din, notes="  latte "),
    ]
    stats = {
        # Envelope spent to exactly $0 with rollover: accumulated 0 must win over month-only -30.
        "c1": {
            "budgeted": Decimal(100),
            "spent": Decimal(-130),
            "balance": Decimal(-30),
            "accumulated_balance": Decimal(0),
        },
        "c2": {
            "budgeted": Decimal(50),
            "spent": Decimal("-12.5"),
            "balance": Decimal("37.5"),
            "accumulated_balance": None,
        },
    }
    _install_fake_actual(monkeypatch, [income_fun, fun, din], txns, stats)
    snap = actual_client.fetch_snapshot(today)
    assert snap.budget_name == "Fake Budget" and snap.window_start == "2026-08-01"
    assert snap.fun_money_category == "🎮 Fun Money"
    assert snap.fun_money_balance == Decimal(0)
    by_id = {t.id: t for t in snap.transactions}
    assert set(by_id) == {"t1", "t3"}
    assert by_id["t1"].payee == "Target" and by_id["t1"].category == "🎮 Fun Money"
    assert by_id["t3"].notes == "latte" and by_id["t3"].category_group == "Wants"
    din_cat = next(c for c in snap.categories if c.name == "Dining Out")
    assert din_cat.spent == Decimal("12.5") and din_cat.balance == Decimal("37.5")  # falls back to month balance


def test_safe_error_strips_url_credentials():
    e = ConnectionError("failed for url 'http://user:s3cret@10.0.0.248:5006/sync'")
    out = actual_client.safe_error(e)
    assert "s3cret" not in out and "***@" in out and out.startswith("ConnectionError")


def test_cleanup_stale_tempdirs(tmp_path, monkeypatch):
    monkeypatch.setattr(actual_client.tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "actual-sync-abc").mkdir()
    (tmp_path / "actual-sync-abc" / "db.sqlite").write_text("x")
    (tmp_path / "unrelated").mkdir()
    assert actual_client.cleanup_stale_tempdirs() == 1
    assert not (tmp_path / "actual-sync-abc").exists() and (tmp_path / "unrelated").exists()


# --------------------------------------------------------------------------- routes / concurrency


def _configure(monkeypatch):
    monkeypatch.setattr(actual_client.settings, "actual_server_url", "http://127.0.0.1:1")
    monkeypatch.setattr(actual_client.settings, "actual_password", "x")
    monkeypatch.setattr(actual_client.settings, "actual_budget_file", "B")


@pytest.mark.asyncio
async def test_trigger_sync_returns_502_on_failure_and_respects_cooldown(fdb, monkeypatch):
    _configure(monkeypatch)
    calls = {"n": 0}

    async def boom(today=None):
        calls["n"] += 1
        raise ConnectionError("refused")

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", boom)
    r = await finance_manager.trigger_sync()
    assert r.status_code == 502 and calls["n"] == 1
    r2 = await finance_manager.trigger_sync()  # within cooldown: no new download
    assert r2.status_code == 502 and calls["n"] == 1


@pytest.mark.asyncio
async def test_trigger_sync_ok_envelope(fdb, monkeypatch):
    _configure(monkeypatch)

    async def ok(today=None):
        return _snap([_tx("a", "-4")])

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", ok)
    r = await finance_manager.trigger_sync()
    assert r["ok"] is True and r["inserted"] == 1
    r2 = await finance_manager.trigger_sync()
    assert r2["ok"] is True and r2["cooldown"] is True


@pytest.mark.asyncio
async def test_concurrent_sync_reports_busy_instead_of_queueing(fdb, monkeypatch):
    _configure(monkeypatch)
    gate = asyncio.Event()

    async def slow(today=None):
        await gate.wait()
        return _snap([])

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", slow)
    first = asyncio.create_task(finance_manager.sync_budget_transactions())
    await asyncio.sleep(0)
    second = await finance_manager.sync_budget_transactions()
    assert second.get("busy") is True
    gate.set()
    assert "error" not in await first


@pytest.mark.asyncio
async def test_categories_negative_cache(fdb, monkeypatch):
    _configure(monkeypatch)
    calls = {"n": 0}

    async def boom(today=None):
        calls["n"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", boom)
    monkeypatch.setattr(finance_manager, "_category_fail", None)
    r1 = await finance_manager.get_budget_categories()
    r2 = await finance_manager.get_budget_categories()
    assert r1.status_code == 502 and r2.status_code == 502 and calls["n"] == 1


@pytest.mark.asyncio
async def test_apply_failure_marks_status_disconnected(fdb, monkeypatch):
    _configure(monkeypatch)
    finance_manager.apply_snapshot(_snap([_tx("a", "-1")]))  # a prior success
    assert finance_manager._status_sync()["connected"] is True

    async def ok(today=None):
        return _snap([])

    def broken(snapshot):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(actual_client, "fetch_snapshot_async", ok)
    monkeypatch.setattr(finance_manager, "apply_snapshot", broken)
    res = await finance_manager.sync_budget_transactions()
    assert "local database error" in res["error"]
    st = finance_manager._status_sync()
    assert st["connected"] is False and "local database error" in st["last_error"]


@pytest.mark.asyncio
async def test_mapping_route_reflags_and_recomputes(fdb):
    finance_manager.apply_snapshot(_snap([_tx("a", "-10"), _tx("b", "-5", category="Rent")]))
    ym = dt.date.today().strftime("%Y-%m")
    assert _spent(fdb, ym)[0] == pytest.approx(0)
    req = finance_manager.CategoryMappingRequest(mappings={"Dining Out": True})
    r = await finance_manager.update_category_mapping(req)
    assert r["ok"] is True and r["updated"] == 1
    assert _spent(fdb, ym)[0] == pytest.approx(10)


def test_mapping_request_rejects_string_booleans_and_bad_names():
    with pytest.raises(ValidationError):
        finance_manager.CategoryMappingRequest(mappings={"Rent": "false"})
    with pytest.raises(ValidationError):
        finance_manager.CategoryMappingRequest(mappings={})
    with pytest.raises(ValidationError):
        finance_manager.CategoryMappingRequest(mappings={"x" * 201: True})


@pytest.mark.asyncio
async def test_reset_route_clears_synced_rows(fdb):
    _map(fdb, Dining_Out=True)
    finance_manager.apply_snapshot(_snap([_tx("a", "-10")]))
    r = await finance_manager.reset_sync()
    assert r["ok"] is True
    assert not _q(fdb, "SELECT 1 FROM transactions WHERE source='actual'")
    assert _spent(fdb, dt.date.today().strftime("%Y-%m"))[0] == pytest.approx(0)
    assert finance_manager._status_sync()["last_synced_at"] is None
