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
os.environ.setdefault("CHROMA_PERSIST", tempfile.mkdtemp(prefix="chroma_test_"))
# Required secret that would otherwise cause Settings() to fail validation on
# import of config.py. Tests never hit real Home Assistant.
os.environ.setdefault("HA_TOKEN", "test-ha-token")
os.environ.setdefault("API_TOKEN", "test-api-token")
os.environ.setdefault("PIHOLE_PASSWORD", "test-pihole-password")

import pytest  # noqa: E402

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
