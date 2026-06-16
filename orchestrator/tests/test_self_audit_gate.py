"""
Tests for the JESS_ADVANCED + SELF_AUDIT_ENABLED gate on the self-audit job.

`run_self_audit()` must require BOTH flags. A previous bypass let the manual
`POST /api/self_audit/run` route fire even when `JESS_ADVANCED=false`
because the gate only checked `self_audit_enabled`.

Source under test: orchestrator/jobs_self_audit.py:568-575 (run_self_audit gate).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# run_self_audit() gate
# ---------------------------------------------------------------------------


class TestRunSelfAuditGate:
    @pytest.mark.asyncio
    async def test_skipped_when_jess_advanced_false(self, monkeypatch):
        """JESS_ADVANCED=false must skip even if SELF_AUDIT_ENABLED=true."""
        from orchestrator import jobs_self_audit
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "self_audit_enabled", True, raising=False)
        monkeypatch.setattr(settings, "jess_advanced", False, raising=False)

        # If the gate leaks, _run_self_audit_locked would be invoked and try
        # to hit Loki. Patch it as a tripwire — it must not be awaited.
        tripwire = AsyncMock(return_value={"result": "ok"})
        monkeypatch.setattr(jobs_self_audit, "_run_self_audit_locked", tripwire, raising=True)

        result = await jobs_self_audit.run_self_audit()

        assert result == {"result": "skipped", "reason": "JESS_ADVANCED=false"}
        tripwire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skipped_when_self_audit_enabled_false(self, monkeypatch):
        """SELF_AUDIT_ENABLED=false skips regardless of jess_advanced."""
        from orchestrator import jobs_self_audit
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "self_audit_enabled", False, raising=False)
        monkeypatch.setattr(settings, "jess_advanced", True, raising=False)

        tripwire = AsyncMock(return_value={"result": "ok"})
        monkeypatch.setattr(jobs_self_audit, "_run_self_audit_locked", tripwire, raising=True)

        result = await jobs_self_audit.run_self_audit()

        assert result == {"result": "skipped", "reason": "SELF_AUDIT_ENABLED=false"}
        tripwire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_proceeds_past_gate_when_both_flags_true(self, monkeypatch):
        """Both flags true → gate falls through to _run_self_audit_locked."""
        from orchestrator import jobs_self_audit
        from orchestrator.config import settings

        monkeypatch.setattr(settings, "self_audit_enabled", True, raising=False)
        monkeypatch.setattr(settings, "jess_advanced", True, raising=False)

        sentinel = {"result": "ok", "reason": "sentinel"}
        locked_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(jobs_self_audit, "_run_self_audit_locked", locked_mock, raising=True)

        # Lock must be free, otherwise we'd fast-return "busy".
        assert not jobs_self_audit._AUDIT_LOCK.locked()

        result = await jobs_self_audit.run_self_audit()

        assert result is sentinel
        locked_mock.assert_awaited_once()
