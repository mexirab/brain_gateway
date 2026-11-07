"""Server-supplied SQL stays inside the temp copy of the budget.

actualpy's ``download_budget`` fetches migration files from the Actual server
and runs them with ``sqlite3.executescript()``. These tests drive the REAL
actualpy ``Actual`` class through ``actual_client.fetch_snapshot`` — only its
network calls are stubbed (a fake "server" hands out a small budget built from
actualpy's own table models plus a migration of our choosing) — so they pin
the exact code path a compromised actual-server would use.

The control under test is ``actual_client._confine_actualpy``: no ATTACH, no
VACUUM INTO, no process-global directory pragmas, on both the migration
connection and the SQLAlchemy engine.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile

import pytest

from orchestrator import actual_client

MIGRATION = "migrations/1799999999999_test.sql"
# The authorizer rejects ATTACH at prepare time; the attach limit (checked
# at run time) is the layer underneath — see test_attach_limit_holds_without_the_authorizer.
BLOCKED = "too many attached databases|not authorized|authorization denied"


def _budget_zip(tmp_path) -> bytes:
    """A budget file as the Actual server serves it: zip of db.sqlite + metadata.json."""
    import actual.database  # noqa: F401 — registers actualpy's table models on SQLModel.metadata
    from sqlmodel import SQLModel, create_engine

    src = tmp_path / "server"
    src.mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{src}/db.sqlite")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(src / "db.sqlite")
    conn.execute("CREATE TABLE IF NOT EXISTS __migrations__ (id INT PRIMARY KEY NOT NULL)")
    conn.commit()
    conn.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(src / "db.sqlite", "db.sqlite")
        z.writestr("metadata.json", json.dumps({"id": "test-budget", "budgetName": "Test"}))
    return buf.getvalue()


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """Real ``Actual``, fake network. Returns a dict: set ``sql`` to the migration
    body, and ``migration`` to its file name (a ``.js`` name takes actualpy's JS path)."""
    from actual import Actual
    from actual.api.models import RemoteFileListDTO

    zip_bytes = _budget_zip(tmp_path)
    server = {"sql": "", "calls": [], "migration": MIGRATION}

    def login(self, *a, **k):
        server["calls"].append("login")
        self._token = "test-token"

    def set_file(self, file_id):
        self._file = RemoteFileListDTO(name="Test", fileId="f1", groupId="g1", deleted=0, encryptKeyId=None)
        return self._file

    monkeypatch.setattr(Actual, "login", login)
    monkeypatch.setattr(Actual, "set_file", set_file)
    monkeypatch.setattr(Actual, "download_user_file", lambda self, fid: zip_bytes)
    monkeypatch.setattr(Actual, "validate", lambda self: None)
    monkeypatch.setattr(Actual, "data_file_index", lambda self: ["default-db.sqlite", server["migration"]])
    monkeypatch.setattr(Actual, "data_file", lambda self, path: server["sql"].encode())
    monkeypatch.setattr(Actual, "sync", lambda self: [])

    monkeypatch.setattr(actual_client.settings, "actual_server_url", "http://actual-budget:5006")
    monkeypatch.setattr(actual_client.settings, "actual_password", "p")
    monkeypatch.setattr(actual_client.settings, "actual_budget_file", "Test")
    monkeypatch.setattr(actual_client.settings, "actual_encryption_password", "")

    # The budget's temp copy lands under work/; anything else is "outside".
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))
    return server


@pytest.fixture
def victim(tmp_path):
    """Stand-in for /app/data: an existing DB the orchestrator owns."""
    data = tmp_path / "app_data"
    data.mkdir()
    db = data / "brain_state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT)")
    conn.commit()
    conn.close()
    return data


def _tables(db) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_malicious_attach_migration_fails_and_writes_nothing_outside(fake_server, victim):
    fake_server["sql"] = (
        f"ATTACH DATABASE '{victim}/brain_state.db' AS victim;"
        " CREATE TABLE victim.pwned (x); INSERT INTO victim.pwned VALUES (1);"
        f" ATTACH DATABASE '{victim}/new.db' AS fresh; CREATE TABLE fresh.t (x);"
    )
    with pytest.raises(sqlite3.DatabaseError, match=BLOCKED):
        actual_client.fetch_snapshot()
    assert sorted(p.name for p in victim.iterdir()) == ["brain_state.db"]
    assert _tables(victim / "brain_state.db") == {"reminders"}


def test_vacuum_into_outside_path_is_blocked(fake_server, victim):
    fake_server["sql"] = f"VACUUM INTO '{victim}/budget-copy.db';"
    with pytest.raises(sqlite3.DatabaseError, match=BLOCKED):
        actual_client.fetch_snapshot()
    assert not (victim / "budget-copy.db").exists()


def test_process_global_temp_dir_pragma_is_denied(fake_server, victim):
    fake_server["sql"] = f"PRAGMA temp_store_directory = '{victim}';"
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        actual_client.fetch_snapshot()


def test_benign_migration_still_runs_and_sync_completes(fake_server, tmp_path):
    """The guard must not break real migrations (CREATE/ALTER/INSERT in the budget itself)."""
    fake_server["sql"] = (
        "CREATE TABLE test_new_feature (id TEXT PRIMARY KEY); INSERT INTO test_new_feature VALUES ('a');"
        " ALTER TABLE test_new_feature ADD COLUMN note TEXT;"
        " INSERT INTO categories (id, name, is_income, hidden, tombstone) VALUES ('mig', 'Added By Migration', 0, 0, 0);"
    )
    snap = actual_client.fetch_snapshot()
    assert snap.budget_name == "Test" and snap.transactions == []
    # The migration really ran on the copy the snapshot was read from.
    assert "Added By Migration" in {c.name for c in snap.categories}
    # Temp copy cleaned up afterwards, as before.
    assert list((tmp_path / "work").iterdir()) == []


def test_engine_connections_are_confined_too(tmp_path, victim):
    """Defense in depth: actualpy's SQLAlchemy engine (reflection, sync messages,
    our queries) gets the same limits as the migration connection."""
    from actual import Actual

    actual_client._confine_actualpy(Actual)
    budget = tmp_path / "budget"
    with zipfile.ZipFile(io.BytesIO(_budget_zip(tmp_path))) as z:
        z.extractall(budget)
    a = Actual.__new__(Actual)
    a._data_dir = budget
    a.create_engine()
    try:
        with a.engine.connect() as conn:
            raw = conn.connection.dbapi_connection
            assert raw.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
            with pytest.raises(Exception, match=BLOCKED):
                conn.exec_driver_sql(f"ATTACH DATABASE '{victim}/via_engine.db' AS x")
    finally:
        a.engine.dispose()
    assert not (victim / "via_engine.db").exists()


def test_harness_reaches_the_vulnerable_code_without_the_guard(fake_server, victim, monkeypatch):
    """Proves the tests above exercise the real hole: with actualpy's own
    sqlite3 / create_engine restored, the same migration DOES write outside."""
    from actual import Actual
    from sqlmodel import create_engine

    monkeypatch.setattr(actual_client, "_confine_actualpy", lambda cls: None)
    monkeypatch.setitem(Actual.run_migrations.__globals__, "sqlite3", sqlite3)
    monkeypatch.setitem(Actual.create_engine.__globals__, "create_engine", create_engine)
    fake_server["sql"] = f"ATTACH DATABASE '{victim}/pwned.db' AS p; CREATE TABLE p.owned (x);"
    actual_client.fetch_snapshot()
    assert (victim / "pwned.db").exists()


def test_guard_fails_closed_before_any_network_call_if_actualpy_changes(fake_server, monkeypatch):
    """A future actualpy that opens its migration connection some other way
    must stop the sync, not run server SQL unconfined."""
    from actual import Actual

    def run_migrations(self, migration_files):  # e.g. an upstream refactor
        import sqlite3 as _sq

        with _sq.connect(self.data_dir / "db.sqlite") as conn:
            for f in migration_files[:0]:
                conn.executescript(f)

    monkeypatch.setattr(Actual, "run_migrations", run_migrations)
    with pytest.raises(actual_client.ActualSandboxError, match="run_migrations"):
        actual_client.fetch_snapshot()
    assert fake_server["calls"] == []  # never logged in, never downloaded


def test_guard_fails_closed_if_engine_creation_changes(fake_server, monkeypatch):
    from actual import Actual

    def create_engine(self):
        from sqlalchemy import create_engine as _ce

        self.engine = _ce(f"sqlite:///{self._data_dir}/db.sqlite")

    monkeypatch.setattr(Actual, "create_engine", create_engine)
    with pytest.raises(actual_client.ActualSandboxError, match="create_engine"):
        actual_client.fetch_snapshot()
    assert fake_server["calls"] == []


def test_guard_install_is_idempotent():
    from actual import Actual

    actual_client._confine_actualpy(Actual)
    first = Actual.create_engine.__globals__["create_engine"]
    actual_client._confine_actualpy(Actual)
    assert Actual.create_engine.__globals__["create_engine"] is first  # not wrapped twice
    assert Actual.run_migrations.__globals__["sqlite3"] is actual_client._CONFINED_SQLITE3


def test_confined_sqlite3_proxy_exposes_only_connect_exceptions_and_constants():
    proxy = actual_client._CONFINED_SQLITE3
    assert proxy.OperationalError is sqlite3.OperationalError and proxy.DatabaseError is sqlite3.DatabaseError
    assert proxy.sqlite_version == sqlite3.sqlite_version and proxy.PARSE_DECLTYPES == sqlite3.PARSE_DECLTYPES
    for other_way_in in ("Connection", "dbapi2", "Row", "Cursor"):
        with pytest.raises(AttributeError, match="not available to actualpy"):
            getattr(proxy, other_way_in)
    conn = proxy.connect(":memory:")
    try:
        assert conn.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
        conn.executescript("CREATE TEMP TABLE t (a); INSERT INTO t VALUES (1);")
        with pytest.raises(sqlite3.DatabaseError, match="not authorized|authorization denied"):
            conn.execute("PRAGMA table_info('t')")  # migration connections get no PRAGMA at all
    finally:
        conn.close()


@pytest.mark.parametrize("sql", ["ATTACH DATABASE '{d}/x.db' AS x", "VACUUM INTO '{d}/copy.db'"])
def test_attach_limit_holds_without_the_authorizer(tmp_path, sql):
    """SQLITE_LIMIT_ATTACHED = 0 alone stops both escapes (Python 3.11+)."""
    conn = sqlite3.connect(tmp_path / "budget.sqlite")
    try:
        actual_client.confine_sqlite_connection(conn)
        conn.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match="too many attached databases"):
            conn.executescript(sql.format(d=tmp_path))
    finally:
        conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["budget.sqlite"]


@pytest.mark.parametrize(
    "pragma",
    ["PRAGMA hard_heap_limit = 1", "PRAGMA soft_heap_limit = 1", "PRAGMA temp_store_directory = '/tmp'"],
)
def test_process_global_pragmas_denied_on_migration_and_engine_connections(tmp_path, pragma):
    """A heap cap set from one connection makes every sqlite3.connect() in the
    process raise MemoryError — state_store, finance.db, Chroma."""
    migration = actual_client._CONFINED_SQLITE3.connect(tmp_path / "m.sqlite")
    engine_like = sqlite3.connect(tmp_path / "e.sqlite")
    actual_client._confine_engine_connection(engine_like, None)
    try:
        for conn in (migration, engine_like):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized|authorization denied"):
                conn.executescript(pragma + ";")
        # Engine connections keep the read-only pragmas SQLAlchemy reflection needs.
        engine_like.execute("CREATE TABLE t (a)")
        assert engine_like.execute("PRAGMA table_xinfo('t')").fetchall()
    finally:
        migration.close()
        engine_like.close()
    probe = sqlite3.connect(":memory:")  # still works: no global limit was set
    probe.close()


def test_plain_vacuum_is_blocked_too(fake_server):
    """Known, accepted consequence (see confine_sqlite_connection): VACUUM
    attaches internally, so a migration ending in VACUUM fails closed."""
    fake_server["sql"] = "CREATE TABLE x (a); VACUUM;"
    with pytest.raises(sqlite3.DatabaseError, match=BLOCKED):
        actual_client.fetch_snapshot()


def test_runaway_migration_sql_is_interrupted(fake_server, monkeypatch):
    """A migration that never finishes must not pin the single sync worker."""
    monkeypatch.setattr(actual_client, "_SQL_DEADLINE_SECONDS", 0.3)
    fake_server["sql"] = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c;"
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        actual_client.fetch_snapshot()


def test_failed_check_writes_nothing_into_the_replacement_globals(fake_server, monkeypatch):
    """Verify before install: a refactored run_migrations defined elsewhere must
    not get our proxy written into its module's globals."""
    from actual import Actual

    def run_migrations(self, migration_files):
        return None

    monkeypatch.setattr(Actual, "run_migrations", run_migrations)
    with pytest.raises(actual_client.ActualSandboxError):
        actual_client.fetch_snapshot()
    assert globals()["sqlite3"] is sqlite3


def test_guard_rejects_other_sqlite3_entry_points(fake_server, monkeypatch):
    """sqlite3.Connection(path) would bypass connect(): the bytecode check
    requires every sqlite3 use in run_migrations to be .connect."""
    from actual import Actual

    def run_migrations(self, migration_files):
        conn = sqlite3.Connection(self.data_dir / "db.sqlite")
        conn.close()

    monkeypatch.setattr(Actual, "run_migrations", run_migrations)
    with pytest.raises(actual_client.ActualSandboxError, match="run_migrations"):
        actual_client.fetch_snapshot()
    assert fake_server["calls"] == []


def test_malicious_js_migration_fails_and_writes_nothing_outside(fake_server, victim):
    """actualpy runs the db.execQuery(`...`) bodies of a .js migration through
    the same executescript() on the same connection — same confinement."""
    from actual.migrations import js_migration_statements

    fake_server["migration"] = "migrations/1799999999999_test.js"
    fake_server["sql"] = (
        "export default async function runMigration(db) {\n"
        f"  db.execQuery(`ATTACH DATABASE '{victim}/brain_state.db' AS victim; CREATE TABLE victim.pwned (x);`);\n"
        f"  db.execQuery(`ATTACH DATABASE '{victim}/new.db' AS fresh; CREATE TABLE fresh.t (x);`);\n"
        "}\n"
    )
    assert len(js_migration_statements(fake_server["sql"])) == 2  # actualpy really extracts both bodies
    # BLOCKED (not a JS syntax error) also proves the .js branch was taken.
    with pytest.raises(sqlite3.DatabaseError, match=BLOCKED):
        actual_client.fetch_snapshot()
    assert sorted(p.name for p in victim.iterdir()) == ["brain_state.db"]
    assert _tables(victim / "brain_state.db") == {"reminders"}


def test_engine_connection_interrupts_runaway_sql_after_the_deadline(monkeypatch):
    """Engine connections (reflection, sync messages, our queries) get the same
    deadline as the migration connection; normal queries before it still run."""
    monkeypatch.setattr(actual_client, "_SQL_DEADLINE_SECONDS", 0.3)
    cte = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c{stop}) SELECT count(*) FROM c"
    conn = sqlite3.connect(":memory:")
    actual_client._confine_engine_connection(conn, None)
    try:
        # Long enough to call the progress handler, finishes before the deadline.
        assert conn.execute(cte.format(stop=" WHERE x < 5000")).fetchone() == (5000,)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute(cte.format(stop="")).fetchone()
    finally:
        conn.close()


def test_global_uses_reports_the_attribute_read_off_each_load():
    def opens(path):
        return sqlite3.connect(path)

    def opens_and_reads_row(path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def hands_the_module_on(f):
        return f(sqlite3)

    def shadows(sqlite3):  # a parameter, not the module global
        return sqlite3.connect(":memory:")

    assert actual_client._global_uses(opens, "sqlite3") == ["connect"]
    assert actual_client._global_uses(opens_and_reads_row, "sqlite3") == ["connect", "Row"]
    assert actual_client._global_uses(hands_the_module_on, "sqlite3") == [None]
    assert actual_client._global_uses(shadows, "sqlite3") == []


def test_nested_code_the_bytecode_check_cannot_see_still_meets_the_proxy(fake_server, monkeypatch):
    """_global_uses reads only the method's own code object, so a lambda inside
    run_migrations passes the check; the sqlite3 proxy is what stops it."""
    from actual import Actual

    ns = {"sqlite3": sqlite3}  # its own module globals, so the install can't touch ours
    exec(
        "def run_migrations(self, migration_files):\n"
        "    side_door = lambda p: sqlite3.Connection(p)\n"
        "    with sqlite3.connect(self.data_dir / 'db.sqlite'):\n"
        "        side_door(self.data_dir / 'db.sqlite')\n",
        ns,
    )
    monkeypatch.setattr(Actual, "run_migrations", ns["run_migrations"])
    assert actual_client._global_uses(ns["run_migrations"], "sqlite3") == ["connect"]
    with pytest.raises(AttributeError, match="not available to actualpy"):
        actual_client.fetch_snapshot()
    assert ns["sqlite3"] is actual_client._CONFINED_SQLITE3
