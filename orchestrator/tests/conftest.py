"""
Shared test fixtures for Brain Gateway test suite.
"""

import os
import sys
import tempfile

# ---------------------------------------------------------------------------
# Environment setup — MUST run before any orchestrator module import
# ---------------------------------------------------------------------------
# shared.py creates a chromadb.PersistentClient at module load time using
# settings.chroma_persist (default /app/data/chroma). That path doesn't exist
# on CI runners or in typical dev environments, so we redirect it to a fresh
# temp dir. setdefault respects any value already in the environment (e.g. if
# you're running tests against a real Chroma instance intentionally).
_test_data_dir = tempfile.mkdtemp(prefix="bgw_test_")
os.environ.setdefault("CHROMA_PERSIST", os.path.join(_test_data_dir, "chroma"))
# SQLite DBs that would otherwise default to /app/data/* paths that don't
# exist on CI runners / fresh dev boxes. state_store, selfcare_manager,
# progress_tracker, and finance_manager all read these env vars directly.
os.environ.setdefault("STATE_DB_PATH", os.path.join(_test_data_dir, "brain_state.db"))
os.environ.setdefault("PROGRESS_DB_PATH", os.path.join(_test_data_dir, "progress.db"))
os.environ.setdefault("FINANCE_DB_PATH", os.path.join(_test_data_dir, "finance.db"))
# Required secrets that would otherwise cause Settings() to fail validation
# on import of config.py. Tests never hit real external services.
os.environ.setdefault("HA_TOKEN", "test-ha-token")
os.environ.setdefault("API_TOKEN", "test-api-token")
os.environ.setdefault("PIHOLE_PASSWORD", "test-pihole-password")

# Make the repo root importable so `import orchestrator.xxx` resolves when
# pytest is invoked from inside orchestrator/ (as it is in CI).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Guarantee a usable "current" event loop for every test.

    pytest-asyncio 1.x manages a fresh loop per async test and leaves the main
    thread with no current loop afterward. Sync tests that reach for
    ``asyncio.get_event_loop()`` (legacy ``run_until_complete`` /
    fire-and-forget-drain patterns) then fail with
    ``RuntimeError: There is no current event loop`` — and which sync test runs
    right after an async one is ordering-dependent, so the failures are flaky
    across environments. Re-establish an open loop at the start of each test;
    pytest-asyncio still installs its own loop for async tests, so this only
    affects the sync callers.
    """
    import asyncio

    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        needs_new = loop.is_closed()
    except RuntimeError:
        needs_new = True
    if needs_new:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture(autouse=True)
def _reset_announcement_routes_cache():
    """Clear the announcement_routes module-level route cache around each test.

    ``announcement_routes._cache`` is a process-global memoization of the
    effective route map. The first announcement in any test (via
    ``_announce_voice`` → ``route_for``) populates it from whatever
    ``REMINDER_SPEAKER`` / on-disk YAML is current at that moment, and it then
    survives into later tests — so a test that monkeypatches ``REMINDER_SPEAKER``
    afterwards silently gets the stale cached speaker. Reset before and after
    each test so ``route_for`` always rebuilds from the current state.
    """
    try:
        from orchestrator import announcement_routes
    except Exception:
        yield
        return
    announcement_routes._cache = None
    yield
    announcement_routes._cache = None


@pytest.fixture(scope="session", autouse=True)
def _init_state_store_schema():
    """
    Initialize the state_store SQLite schema once per test session.

    test_selfcare_manager (and anything else that writes to state_store's DB)
    expects the `selfcare_log` table to exist. In production this happens via
    orchestrator.py's startup path; in unit tests we call init_db() directly.
    Runs after the env-var setup at the top of this file, so STATE_DB_PATH
    is already pointed at a temp file.
    """
    from orchestrator import state_store

    state_store.init_db()
    yield


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite database path for state_store tests."""
    db_path = str(tmp_path / "test_state.db")
    from orchestrator import state_store

    original = state_store.DB_PATH
    state_store.DB_PATH = db_path
    state_store.init_db()
    yield db_path
    state_store.DB_PATH = original


@pytest.fixture
def mode_router():
    """Provide a fresh ModeRouter instance."""
    from orchestrator.mode_router import ModeRouter

    return ModeRouter()
