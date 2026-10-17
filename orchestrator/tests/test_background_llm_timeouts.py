"""
Tests for the background-LLM robustness changes (fix/background-llm-timeouts):

- task_decomposition / session_miner / email→calendar pass timeout=120 to
  call_model (single llama slot: background calls queue behind chat).
- Model failures increment bgw_background_llm_errors_total{caller,error_type}.
- session_miner: a failed extraction is an error (None), not "0 insights".
- email→calendar: an email is only marked processed after the model answered
  (or for the <30-char skip); LLM failure leaves it for the next run.
- POST /api/palace/mine is single-flight (409 on overlap) and its 500 body
  never echoes the exception text.
- config: vision_timeout default is 120.

All external calls (model, Gmail, calendar, palace) are mocked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

from orchestrator.metrics import BACKGROUND_LLM_ERRORS


def _err_count(caller: str, error_type: str) -> float:
    return BACKGROUND_LLM_ERRORS.labels(caller=caller, error_type=error_type)._value.get()


def _model_resp(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_background_llm_errors_metric_shape():
    from prometheus_client import REGISTRY

    names = {m.name for m in REGISTRY.collect()}
    # prometheus_client strips the _total suffix from the family name
    assert "bgw_background_llm_errors" in names
    assert BACKGROUND_LLM_ERRORS._labelnames == ("caller", "error_type")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_vision_timeout_default_is_120():
    from orchestrator.config import Settings

    assert Settings.model_fields["vision_timeout"].default == 120


# ---------------------------------------------------------------------------
# task_decomposition
# ---------------------------------------------------------------------------


@pytest.fixture
def td():
    with (
        patch("orchestrator.task_decomposition.TASK_DECOMP_TASKS_CREATED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_STEPS_COMPLETED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_STEPS_SKIPPED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_TASKS_ABANDONED", MagicMock()),
        patch("orchestrator.task_decomposition.TASK_DECOMP_ERRORS", MagicMock()),
    ):
        from orchestrator import task_decomposition

        task_decomposition._active_tasks.clear()
        yield task_decomposition
        task_decomposition._active_tasks.clear()


class TestTaskDecomposition:
    async def test_passes_120s_timeout(self, td):
        steps = '[{"description": "Open the drawer", "est_minutes": 5}]'
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp(steps)
            await td.decompose_task("Clean the desk")
        assert mock_call.await_args.kwargs["timeout"] == 120

    async def test_model_exception_counts_and_apologises(self, td):
        before = _err_count("task_decomp", "ReadTimeout")
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = httpx.ReadTimeout("slow")
            result = await td.decompose_task("Clean the desk")
        assert result.startswith("Sorry")
        assert _err_count("task_decomp", "ReadTimeout") == before + 1
        assert td._active_tasks == {} or not td._active_tasks

    async def test_error_type_is_exception_class_name(self, td):
        before = _err_count("task_decomp", "ConnectError")
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = httpx.ConnectError("down")
            await td.decompose_task("Clean the desk")
        assert _err_count("task_decomp", "ConnectError") == before + 1

    async def test_bad_json_is_not_a_background_llm_error(self, td):
        """Unparseable output falls back to one step; the model DID answer."""
        before = _err_count("task_decomp", "JSONDecodeError")
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp("not json at all")
            result = await td.decompose_task("Clean the desk")
        assert not result.startswith("Sorry")
        assert _err_count("task_decomp", "JSONDecodeError") == before


# ---------------------------------------------------------------------------
# session_miner
# ---------------------------------------------------------------------------


class TestSessionMinerExtract:
    async def test_passes_120s_timeout_and_parses(self):
        from orchestrator import session_miner

        payload = '[{"insight": "Uses port 8080", "category": "configuration"}]'
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp(payload)
            out = await session_miner._extract_insights(["a", "b", "c"])
        assert mock_call.await_args.kwargs["timeout"] == 120
        assert isinstance(out, list) and len(out) == 1

    async def test_model_failure_returns_none_and_counts(self):
        from orchestrator import session_miner

        before = _err_count("session_miner", "ReadTimeout")
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = httpx.ReadTimeout("slow")
            out = await session_miner._extract_insights(["a", "b", "c"])
        assert out is None
        assert _err_count("session_miner", "ReadTimeout") == before + 1

    async def test_empty_success_returns_empty_list(self):
        from orchestrator import session_miner

        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp("[]")
            out = await session_miner._extract_insights(["a", "b", "c"])
        assert out == []


class TestMineSessions:
    def _patch_files(self, session_miner, n=2):
        files = [f"/fake/proj/s{i}.jsonl" for i in range(n)]
        return (
            patch.object(session_miner, "_find_session_files", return_value=files),
            patch.object(session_miner, "_parse_session_file", return_value=["m1", "m2", "m3"]),
            patch.object(session_miner, "_detect_project", return_value="proj"),
        )

    async def test_failed_extraction_counts_as_error(self):
        from orchestrator import session_miner

        p1, p2, p3 = self._patch_files(session_miner, n=2)
        with p1, p2, p3, patch.object(session_miner, "_extract_insights", AsyncMock(return_value=None)):
            stats = await session_miner.mine_sessions()
        assert stats == {"total_sessions": 2, "extracted": 0, "stored": 0, "errors": 2}

    async def test_empty_extraction_is_not_an_error(self):
        from orchestrator import session_miner

        p1, p2, p3 = self._patch_files(session_miner, n=2)
        with p1, p2, p3, patch.object(session_miner, "_extract_insights", AsyncMock(return_value=[])):
            stats = await session_miner.mine_sessions()
        assert stats["errors"] == 0
        assert stats["extracted"] == 0

    async def test_mixed_failure_and_success(self):
        from orchestrator import session_miner

        palace = MagicMock()
        palace.store = AsyncMock(return_value="doc-1")
        p1, p2, p3 = self._patch_files(session_miner, n=2)
        extract = AsyncMock(side_effect=[None, [{"insight": "x", "category": "pattern"}]])
        with (
            p1,
            p2,
            p3,
            patch.object(session_miner, "_extract_insights", extract),
            patch("orchestrator.shared.get_palace", return_value=palace),
        ):
            stats = await session_miner.mine_sessions()
        assert stats == {"total_sessions": 2, "extracted": 1, "stored": 1, "errors": 1}


# ---------------------------------------------------------------------------
# jobs_calendar.process_emails_for_events
# ---------------------------------------------------------------------------


def _msg(mid: str, body: str):
    return SimpleNamespace(
        id=mid,
        subject="subj",
        sender="sender@example.com",
        date=datetime(2026, 10, 17, 9, 0),
        body_text=body,
        snippet="",
    )


@pytest.fixture
def e2c():
    """Patch Gmail, calendar, state_store for process_emails_for_events."""
    from orchestrator import jobs_calendar

    marked: list[str] = []
    gmail = MagicMock()
    gmail.is_configured = True
    gmail.list_messages = AsyncMock()
    cal = MagicMock()
    cal.list_events = AsyncMock(return_value=SimpleNamespace(success=True, events=[]))
    cal.create_event = AsyncMock(return_value=SimpleNamespace(success=True, error=None))

    with (
        patch.object(jobs_calendar, "get_gmail_client", return_value=gmail),
        patch.object(jobs_calendar, "get_calendar", return_value=cal),
        patch.object(jobs_calendar.calendar_sources, "writable", return_value=True),
        patch.object(jobs_calendar.state_store, "is_notified", return_value=False),
        patch.object(jobs_calendar.state_store, "mark_notified", side_effect=marked.append),
        patch.object(jobs_calendar.state_store, "clear_stale_notifications"),
    ):
        yield SimpleNamespace(mod=jobs_calendar, gmail=gmail, cal=cal, marked=marked)


def _set_messages(ctx, msgs):
    ctx.gmail.list_messages.return_value = SimpleNamespace(success=True, messages=msgs, error=None)


LONG_BODY = "Your dentist appointment is confirmed for next Tuesday at 10am. " * 2


class TestEmailToCalendar:
    async def test_llm_failure_leaves_email_unmarked_and_counts(self, e2c):
        _set_messages(e2c, [_msg("m1", LONG_BODY)])
        before = _err_count("email_to_calendar", "ReadTimeout")
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = httpx.ReadTimeout("slow")
            await e2c.mod.process_emails_for_events()
        assert e2c.marked == []
        assert _err_count("email_to_calendar", "ReadTimeout") == before + 1
        e2c.cal.create_event.assert_not_awaited()

    async def test_success_marks_after_answer_with_120s_timeout(self, e2c):
        _set_messages(e2c, [_msg("m1", LONG_BODY)])
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp("[]")
            await e2c.mod.process_emails_for_events()
        assert e2c.marked == ["e2c:m1"]
        assert mock_call.await_args.kwargs["timeout"] == 120

    async def test_short_body_marked_without_llm_call(self, e2c):
        _set_messages(e2c, [_msg("short", "hi")])
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            await e2c.mod.process_emails_for_events()
        assert e2c.marked == ["e2c:short"]
        mock_call.assert_not_awaited()

    async def test_one_failure_does_not_burn_the_batch(self, e2c):
        """First email times out, second succeeds: only the second is marked."""
        _set_messages(e2c, [_msg("bad", LONG_BODY), _msg("good", LONG_BODY)])
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = [httpx.ReadTimeout("slow"), _model_resp("[]")]
            await e2c.mod.process_emails_for_events()
        assert e2c.marked == ["e2c:good"]
        assert mock_call.await_count == 2

    async def test_unparseable_answer_is_still_marked(self, e2c):
        """The model answered — garbage output must not be retried forever."""
        _set_messages(e2c, [_msg("m1", LONG_BODY)])
        with patch("orchestrator.orchestrator.call_model", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = _model_resp("no json here")
            await e2c.mod.process_emails_for_events()
        assert e2c.marked == ["e2c:m1"]


# ---------------------------------------------------------------------------
# routes_palace POST /api/palace/mine
# ---------------------------------------------------------------------------


@pytest.fixture
def palace_app():
    from orchestrator import routes_palace

    app = FastAPI()
    app.include_router(routes_palace.router)
    with patch("orchestrator.shared.PALACE_ENABLED", True):
        yield app, routes_palace


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestPalaceMineRoute:
    async def test_happy_path(self, palace_app):
        app, _ = palace_app
        stats = {"total_sessions": 1, "extracted": 2, "stored": 2, "errors": 0}
        with patch("orchestrator.session_miner.mine_sessions", AsyncMock(return_value=stats)):
            async with _client(app) as c:
                r = await c.post("/api/palace/mine", json={})
        assert r.status_code == 200
        assert r.json() == {"ok": True, **stats}

    async def test_exception_returns_generic_500(self, palace_app):
        app, routes_palace = palace_app
        secret = "/home/someone/secret/path token=abc123"
        with patch("orchestrator.session_miner.mine_sessions", AsyncMock(side_effect=RuntimeError(secret))):
            async with _client(app) as c:
                r = await c.post("/api/palace/mine", json={})
        assert r.status_code == 500
        body = r.json()
        assert body == {"ok": False, "error": "session mining failed (see orchestrator logs)"}
        assert secret not in r.text
        # Lock released after the failure
        assert not routes_palace._MINE_LOCK.locked()

    async def test_concurrent_request_gets_409(self, palace_app):
        app, routes_palace = palace_app
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_mine(session_path=""):
            started.set()
            await release.wait()
            return {"total_sessions": 0, "extracted": 0, "stored": 0, "errors": 0}

        with patch("orchestrator.session_miner.mine_sessions", slow_mine):
            async with _client(app) as c:
                first = asyncio.create_task(c.post("/api/palace/mine", json={}))
                await asyncio.wait_for(started.wait(), timeout=5)
                second = await c.post("/api/palace/mine", json={})
                release.set()
                first_resp = await asyncio.wait_for(first, timeout=5)

        assert second.status_code == 409
        assert second.json()["ok"] is False
        assert first_resp.status_code == 200
        assert not routes_palace._MINE_LOCK.locked()

    async def test_sequential_requests_both_succeed(self, palace_app):
        app, _ = palace_app
        stats = {"total_sessions": 0, "extracted": 0, "stored": 0, "errors": 0}
        with patch("orchestrator.session_miner.mine_sessions", AsyncMock(return_value=stats)):
            async with _client(app) as c:
                r1 = await c.post("/api/palace/mine", json={})
                r2 = await c.post("/api/palace/mine", json={})
        assert (r1.status_code, r2.status_code) == (200, 200)
