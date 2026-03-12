"""
Shared test fixtures for Brain Gateway test suite.
"""

import os
import sys
import tempfile
import pytest

# Ensure orchestrator modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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
