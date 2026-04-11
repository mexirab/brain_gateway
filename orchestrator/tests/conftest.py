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

import pytest  # noqa: E402

# Ensure orchestrator modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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
    import state_store

    state_store.init_db()
    yield


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary SQLite database path for state_store tests."""
    db_path = str(tmp_path / "test_state.db")
    # Patch the DB_PATH before importing state_store
    import state_store

    original = state_store.DB_PATH
    state_store.DB_PATH = db_path
    state_store.init_db()
    yield db_path
    state_store.DB_PATH = original


@pytest.fixture
def mode_router():
    """Provide a fresh ModeRouter instance."""
    from mode_router import ModeRouter

    return ModeRouter()
