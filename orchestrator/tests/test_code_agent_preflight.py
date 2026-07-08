"""
Tests for code_agent's fast-fail preflight (branch fix/code-agent-preflight).

The code model runs on Helios, which is powered down most of the time. Before
this preflight, an asleep Helios meant every one of up to CODE_AGENT_MAX_ROUNDS
agent rounds hung on connect (~120s each). handle_code_agent now probes
`GET {model_url}/models` with a short timeout and bails out fast with an
actionable message if the box is unreachable, incrementing a metric.
"""

from unittest.mock import AsyncMock

import httpx

from orchestrator import code_agent, shared
from orchestrator.metrics import CODE_AGENT_PREFLIGHT_FAILURES


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeHttp:
    """Stand-in for shared._http: an async .get returning a canned response."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self._exc is not None:
            raise self._exc
        return self._resp


# ---------------------------------------------------------------------------
# _code_model_reachable
# ---------------------------------------------------------------------------


class TestCodeModelReachable:
    async def test_returns_true_on_200(self, monkeypatch):
        fake = _FakeHttp(resp=_FakeResp(200))
        monkeypatch.setattr(shared, "_http", fake, raising=False)
        assert await code_agent._code_model_reachable("http://helios:8000/v1") is True
        # builds "{url}/models" against the shared pooled client
        assert fake.calls[0][0] == "http://helios:8000/v1/models"

    async def test_returns_true_on_404(self, monkeypatch):
        # A responding server (even 4xx) means the box is up — intentional.
        fake = _FakeHttp(resp=_FakeResp(404))
        monkeypatch.setattr(shared, "_http", fake, raising=False)
        assert await code_agent._code_model_reachable("http://helios:8000/v1") is True

    async def test_returns_false_on_503(self, monkeypatch):
        fake = _FakeHttp(resp=_FakeResp(503))
        monkeypatch.setattr(shared, "_http", fake, raising=False)
        assert await code_agent._code_model_reachable("http://helios:8000/v1") is False

    async def test_returns_false_when_get_raises(self, monkeypatch):
        # The fail-fast path: connect error / timeout -> unreachable.
        fake = _FakeHttp(exc=httpx.ConnectError("connection refused"))
        monkeypatch.setattr(shared, "_http", fake, raising=False)
        assert await code_agent._code_model_reachable("http://helios:8000/v1") is False

    async def test_trailing_slash_stripped(self, monkeypatch):
        fake = _FakeHttp(resp=_FakeResp(200))
        monkeypatch.setattr(shared, "_http", fake, raising=False)
        await code_agent._code_model_reachable("http://helios:8000/v1/")
        assert fake.calls[0][0] == "http://helios:8000/v1/models"

    async def test_one_off_client_branch_when_http_none(self, monkeypatch):
        # shared._http is None (probe runs before startup initialized the pool):
        # falls back to a one-off httpx.AsyncClient. Patch it to a stub so no
        # real network call happens, and assert we still get a bool out.
        monkeypatch.setattr(shared, "_http", None, raising=False)

        class _StubClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return _FakeResp(200)

        monkeypatch.setattr(code_agent.httpx, "AsyncClient", _StubClient)
        result = await code_agent._code_model_reachable("http://helios:8000/v1")
        assert result is True
        assert isinstance(result, bool)

    async def test_one_off_client_exception_is_swallowed(self, monkeypatch):
        # Even on the one-off branch, any exception -> False (never raises).
        monkeypatch.setattr(shared, "_http", None, raising=False)

        class _BoomClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(code_agent.httpx, "AsyncClient", _BoomClient)
        assert await code_agent._code_model_reachable("http://helios:8000/v1") is False


# ---------------------------------------------------------------------------
# handle_code_agent — preflight wiring
# ---------------------------------------------------------------------------


class TestHandleCodeAgentPreflight:
    async def test_unreachable_returns_message_increments_counter_and_skips_loop(self, monkeypatch):
        monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
        monkeypatch.setattr(shared, "CODE_AGENT_MODEL_URL", "http://helios:8000/v1", raising=False)
        monkeypatch.setattr(code_agent, "_code_model_reachable", AsyncMock(return_value=False))
        loop_tripwire = AsyncMock(side_effect=AssertionError("loop must not run when unreachable"))
        monkeypatch.setattr(code_agent, "_run_code_agent_loop", loop_tripwire)

        before = CODE_AGENT_PREFLIGHT_FAILURES._value.get()
        result = await code_agent.handle_code_agent({"task": "investigate the bug"})
        after = CODE_AGENT_PREFLIGHT_FAILURES._value.get()

        assert result.startswith("The code model isn't reachable right now")
        assert after - before == 1
        loop_tripwire.assert_not_called()

    async def test_reachable_runs_loop_and_returns_its_result(self, monkeypatch):
        monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
        monkeypatch.setattr(shared, "CODE_AGENT_MODEL_URL", "http://helios:8000/v1", raising=False)
        monkeypatch.setattr(code_agent, "_code_model_reachable", AsyncMock(return_value=True))
        sentinel = "the loop ran and produced this"
        loop_mock = AsyncMock(return_value=sentinel)
        monkeypatch.setattr(code_agent, "_run_code_agent_loop", loop_mock)

        before = CODE_AGENT_PREFLIGHT_FAILURES._value.get()
        result = await code_agent.handle_code_agent({"task": "do the thing"})
        after = CODE_AGENT_PREFLIGHT_FAILURES._value.get()

        assert result == sentinel
        loop_mock.assert_awaited_once()
        assert after == before  # reachable path does not touch the failure counter

    async def test_disabled_returns_disabled_message_and_never_probes(self, monkeypatch):
        monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", False, raising=False)
        probe_tripwire = AsyncMock(side_effect=AssertionError("must not probe when disabled"))
        monkeypatch.setattr(code_agent, "_code_model_reachable", probe_tripwire)

        result = await code_agent.handle_code_agent({"task": "anything"})

        assert "disabled" in result.lower()
        probe_tripwire.assert_not_called()

    async def test_empty_model_url_returns_not_configured_and_never_probes(self, monkeypatch):
        monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
        monkeypatch.setattr(shared, "CODE_AGENT_MODEL_URL", "", raising=False)
        probe_tripwire = AsyncMock(side_effect=AssertionError("must not probe with no model_url"))
        monkeypatch.setattr(code_agent, "_code_model_reachable", probe_tripwire)

        result = await code_agent.handle_code_agent({"task": "anything"})

        assert "not configured" in result.lower()
        probe_tripwire.assert_not_called()

    async def test_empty_task_short_circuits_before_probe(self, monkeypatch):
        # The `if not task:` guard sits before the model_url/preflight block.
        monkeypatch.setattr(shared, "CODE_AGENT_ENABLED", True, raising=False)
        monkeypatch.setattr(shared, "CODE_AGENT_MODEL_URL", "http://helios:8000/v1", raising=False)
        probe_tripwire = AsyncMock(side_effect=AssertionError("must not probe with empty task"))
        monkeypatch.setattr(code_agent, "_code_model_reachable", probe_tripwire)

        result = await code_agent.handle_code_agent({"task": ""})

        assert "No task provided" in result
        probe_tripwire.assert_not_called()
