"""Shared pytest fixtures for the coder and self-audit test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Self-audit (test_jobs_self_audit.py, test_self_investigator.py): Loki is
``SELF_AUDIT_LOKI_URL`` (mocked with respx), ``self_audit_enable`` switches the
audit on for one test, and the Pushover digest and the mempalace store are
stubbed by the ``self_audit_pushover`` / ``self_audit_palace`` fixtures.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest

SELF_AUDIT_LOKI_URL = "http://loki.test:3100"


def self_audit_enable(monkeypatch, **overrides):
    """Turn the self-audit on for one test (the live settings singleton, via
    monkeypatch) and stub the exact per-group counts, a separate Loki metric
    query the run tests don't mock. Returns ``settings``.

    ``run_self_audit()`` needs BOTH ``self_audit_enabled`` and ``jess_advanced``;
    ``jess_advanced`` defaults to False on a clean env.
    """
    from orchestrator import jobs_self_audit
    from orchestrator.config import settings

    values = {
        "self_audit_enabled": True,
        "jess_advanced": True,
        "self_audit_loki_url": SELF_AUDIT_LOKI_URL,
        "self_audit_lookback_hours": 24,
        "self_audit_max_clusters": 10,
        "self_audit_loki_limit_per_group": 1500,
        # Never reach Claude by accident (e.g. a mounted /app/.env with a key):
        # these runs are about the local brain. Claude: test_self_investigator_claude.py.
        "self_audit_claude_enabled": False,
        **overrides,
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value, raising=False)
    monkeypatch.setattr(jobs_self_audit, "_count_group", AsyncMock(return_value=None))
    if jobs_self_audit._AUDIT_LOCK.locked():
        with contextlib.suppress(RuntimeError):
            jobs_self_audit._AUDIT_LOCK.release()
    return settings


@pytest.fixture
def self_audit_pushover(monkeypatch):
    """Stub deliver_pushover_confirm — captures every digest without pushing."""
    from orchestrator import pushover_manager

    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(pushover_manager, "deliver_pushover_confirm", mock, raising=False)
    return mock


@pytest.fixture
def self_audit_palace(monkeypatch):
    """Stub shared.get_palace() so the audit's facts line never touches chroma."""
    from orchestrator import shared

    palace = AsyncMock()
    palace.store = AsyncMock(return_value=None)
    monkeypatch.setattr(shared, "get_palace", lambda: palace, raising=False)
    return palace


__all__: list[str] = ["self_audit_palace", "self_audit_pushover"]
