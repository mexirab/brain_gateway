"""Tests for the self-investigator's Claude backend (orchestrator/self_investigator.py).

- the request it sends (shape, no sampling params, prompt cache, fallback beta);
- replay of the assistant turns (thinking blocks, tool results, per-round cap);
- the budget-forced final report and the salvage after a later failed round;
- falling back to the local brain when the FIRST call fails or Claude is skipped;
- spend accounting, the monthly / alert / daily caps, failing closed;
- pricing across a server-side fallback;
- task redaction (nothing secret-shaped leaves the house);
- config validation, the digest note, SECRET_PATTERNS and logger quieting.

No network: the SDK runs on httpx2 (respx does NOT intercept it), so every test
gets an ``AsyncAnthropic`` whose transport is an ``httpx2.MockTransport``. The
local brain is a patched ``call_model``; app_state is an in-memory dict. Keys
are fake strings.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anthropic
import httpx2
import pytest
import respx
from httpx import Response
from prometheus_client import REGISTRY

_FAKE_KEY = "sk-ant-api03-" + "x" * 40  # not a real key
_API = "https://api.anthropic.com"
_LOKI_URL = "http://loki.test:3100"
_BUDGET_TEXT = "Your investigation budget is used up."
_FINAL = (
    "Summary: boom.\n\n"
    "## orchestrator · HIGH\n"
    "- **Count:** 3x in last 24h\n"
    "- **First/Last:** a -> b\n"
    "- **Evidence:** orchestrator/boom.py:2\n"
    "- **Likely cause:** f raises.\n"
    "- **Suggestion:** INVESTIGATE: docker logs brain-orchestrator --tail 50\n\n"
    "**Overall:** 0 critical, 1 high, 0 medium, 0 low\n"
)
_EXPECTED_KEYS = {"model", "max_tokens", "system", "tools", "messages", "cache_control", "output_config", "fallbacks"}


# ---------------------------------------------------------------------------
# Response / block builders
# ---------------------------------------------------------------------------


def _usage(inp: int = 10, out: int = 5, read: int = 0, write: int = 0, iterations: list | None = None) -> dict:
    u = {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
    }
    if iterations is not None:
        u["iterations"] = iterations
    return u


def _message(content: list, stop: str = "end_turn", *, model: str = "claude-opus-5-5", usage: dict | None = None):
    return httpx2.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage or _usage(),
        },
    )


def _error(status: int, etype: str, code: str | None = None) -> httpx2.Response:
    err: dict = {"type": etype, "message": "x"}
    if code:
        err["details"] = {"error_code": code}
    return httpx2.Response(status, json={"type": "error", "error": err})


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _thinking(t: str, sig: str) -> dict:
    return {"type": "thinking", "thinking": t, "signature": sig}


def _tool(name: str, args: dict, tid: str) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": args}


def _fallback(src: str, dst: str) -> dict:
    return {
        "type": "fallback",
        "from": {"model": src},
        "to": {"model": dst},
        "trigger": {"type": "refusal", "category": "cyber"},
    }


def _final(**kw) -> httpx2.Response:
    return _message([_text(_FINAL)], **kw)


def _grep_round(tid: str, pattern: str) -> httpx2.Response:
    return _message([_tool("grep", {"pattern": pattern}, tid)], stop="tool_use")


def _local_msg(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


def _connect_error(request):
    return httpx2.ConnectError("down", request=request)


def _read_timeout(request):
    return httpx2.ReadTimeout("slow", request=request)


class _FakeAPI:
    """MockTransport handler: replays the queued replies in order, records requests.

    A reply is an ``httpx2.Response``, or a callable ``(request) -> Response |
    Exception`` (an exception is raised, as a transport error). Running out of
    replies answers 418 and sets ``overflow`` (an exception here would only
    turn into an APIConnectionError and hide the extra call).
    """

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []
        self.overflow = False

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            self.overflow = True
            return _error(418, "invalid_request_error")
        reply = self.replies.pop(0)
        if callable(reply) and not isinstance(reply, httpx2.Response):
            reply = reply(request)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def _metric(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _fallback_count(reason: str) -> float:
    return _metric("bgw_self_investigator_claude_fallback_total", {"reason": reason})


def _runs(result: str, backend: str, trigger: str = "daily") -> float:
    return _metric("bgw_self_investigator_runs_total", {"trigger": trigger, "result": result, "backend": backend})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_on(monkeypatch, tmp_path):
    """Audit + investigator on, Claude enabled with a fake key, a tiny repo,
    app_state in a dict, slot etiquette patched out, no Claude client yet."""
    from orchestrator import jobs_self_audit, self_investigator, state_store
    from orchestrator.config import settings

    root = tmp_path / "repo"
    (root / "orchestrator").mkdir(parents=True)
    (root / "orchestrator" / "boom.py").write_text("def f():\n    raise ValueError('boom failed')\n")
    for k, v in {
        "self_audit_enabled": True,
        "jess_advanced": True,
        "self_audit_investigate": True,
        "self_audit_repo_path": str(root),
        "self_audit_loki_url": _LOKI_URL,
        "self_audit_lookback_hours": 24,
        "self_audit_alert_lookback_hours": 3,
        "self_audit_max_clusters": 10,
        "self_audit_output_dir": str(tmp_path / "audits"),
        "self_audit_loki_limit_per_group": 1500,
        "self_audit_investigate_max_rounds": 6,
        "self_audit_investigate_max_tool_calls": 4,
        "self_audit_investigate_max_seconds": 600,
        "self_audit_claude_enabled": True,
        "self_audit_claude_api_key": _FAKE_KEY,
        "self_audit_claude_model": "claude-opus-5-5",
        "self_audit_claude_effort": "medium",
        "self_audit_claude_monthly_usd": 40.0,
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)

    store: dict[str, str] = {}
    monkeypatch.setattr(state_store, "get_app_state", lambda key: store.get(key))
    monkeypatch.setattr(state_store, "set_app_state", lambda key, value: store.__setitem__(key, value))

    monkeypatch.setattr(self_investigator, "_claude_client", None)
    monkeypatch.setattr(self_investigator, "_spend_seen", {})
    monkeypatch.setattr(self_investigator, "_spend_unwritten", {})
    brain_ready = AsyncMock(return_value=True)
    monkeypatch.setattr(self_investigator, "brain_ready", brain_ready)
    monkeypatch.setattr(self_investigator, "_wait_for_idle_chat", AsyncMock(return_value=None))
    monkeypatch.setattr(self_investigator, "_wait_for_quiet_period", AsyncMock(return_value=True))
    monkeypatch.setattr(jobs_self_audit, "_count_group", AsyncMock(return_value=None))
    if jobs_self_audit._AUDIT_LOCK.locked():
        with contextlib.suppress(RuntimeError):
            jobs_self_audit._AUDIT_LOCK.release()
    return SimpleNamespace(settings=settings, store=store, repo=root, brain_ready=brain_ready)


@pytest.fixture
def install_api(monkeypatch):
    """Point the investigator at a mocked Messages API; returns the _FakeAPI."""
    from orchestrator import self_investigator

    def _install(*replies) -> _FakeAPI:
        fake = _FakeAPI(*replies)
        client = anthropic.AsyncAnthropic(
            api_key=_FAKE_KEY,
            base_url=_API,
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake)),
        )
        monkeypatch.setattr(self_investigator, "_claude_client", client)
        return fake

    return _install


@pytest.fixture
def local_brain():
    """The local brain (call_model): answers with the final report."""
    cm = AsyncMock(return_value=_local_msg(_FINAL))
    with patch("orchestrator.orchestrator.call_model", new=cm):
        yield cm


def _month_key() -> str:
    from orchestrator import self_investigator

    return self_investigator._month_key()


def _day_key() -> str:
    from orchestrator import self_investigator

    return self_investigator._day_key()


async def _investigate(**kw):
    from orchestrator import self_investigator

    kw.setdefault("clusters", [])
    kw.setdefault("groups", None)
    kw.setdefault("lookback_hours", 24)
    return await self_investigator.investigate(**kw)


# ===========================================================================
# Request shape
# ===========================================================================


class TestRequestShape:
    async def test_body_keys_no_sampling_params_and_headers(self, claude_on, install_api, local_brain):
        from orchestrator.self_investigator import CLAUDE_TOOLS, SYSTEM_PROMPT

        api = install_api(_final())
        out = await _investigate()
        assert out["result"] == "ok" and out["backend"] == "claude" and out["report"] == _FINAL
        assert local_brain.await_count == 0
        (req,) = api.requests
        body = api.bodies[0]
        assert set(body) == _EXPECTED_KEYS
        for knob in ("temperature", "top_p", "top_k", "thinking", "mcp_servers", "stream"):
            assert knob not in body
        assert body["model"] == "claude-opus-5-5"
        assert body["max_tokens"] == 16000
        assert body["system"] == SYSTEM_PROMPT
        assert body["tools"] == json.loads(json.dumps(CLAUDE_TOOLS))
        assert {t["name"] for t in body["tools"]} == {"list_dir", "read_file", "grep", "loki_query"}
        assert all(set(t) == {"name", "description", "input_schema"} for t in body["tools"])  # client tools only
        assert body["cache_control"] == {"type": "ephemeral"}
        assert body["output_config"] == {"effort": "medium"}
        assert body["fallbacks"] == "default"
        assert body["messages"][0]["role"] == "user"
        # Transport: the beta endpoint, the fallback beta header, our key.
        assert req.url.host == "api.anthropic.com" and req.url.path == "/v1/messages"
        betas = [b.strip() for b in req.headers["anthropic-beta"].split(",")]
        assert "server-side-fallback-2026-07-01" in betas
        assert req.headers["authorization"] == f"Bearer {_FAKE_KEY}"  # SDK 2.x: Bearer, not x-api-key

    @pytest.mark.parametrize("effort", ["low", "high", "max"])
    async def test_effort_setting_reaches_output_config(self, claude_on, install_api, local_brain, monkeypatch, effort):
        monkeypatch.setattr(claude_on.settings, "self_audit_claude_effort", effort, raising=False)
        api = install_api(_final())
        await _investigate()
        assert api.bodies[0]["output_config"] == {"effort": effort}

    async def test_model_setting_is_sent(self, claude_on, install_api, local_brain, monkeypatch):
        monkeypatch.setattr(claude_on.settings, "self_audit_claude_model", "claude-sonnet-5-5", raising=False)
        api = install_api(_final(model="claude-sonnet-5-5"))
        await _investigate()
        assert api.bodies[0]["model"] == "claude-sonnet-5-5"

    async def test_system_and_tools_stable_and_history_append_only(self, claude_on, install_api, local_brain):
        api = install_api(_grep_round("toolu_1", "boom"), _grep_round("toolu_2", "raise"), _final())
        out = await _investigate()
        assert out["result"] == "ok" and out["tool_calls"] == 2
        bodies = api.bodies
        assert len(bodies) == 3
        assert len({b["system"] for b in bodies}) == 1
        assert len({json.dumps(b["tools"]) for b in bodies}) == 1  # same bytes, same order
        assert len({json.dumps(b["cache_control"]) for b in bodies}) == 1
        for prev, nxt in zip(bodies, bodies[1:], strict=False):
            assert nxt["messages"][: len(prev["messages"])] == prev["messages"]  # never edits a sent turn
        assert all("tool_choice" not in b for b in bodies)  # only the forced final report sets it

    async def test_client_built_lazily_with_pinned_base_url(self, claude_on, monkeypatch):
        from orchestrator import self_investigator

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://attacker.invalid")
        monkeypatch.setattr(self_investigator, "_claude_client", None)
        client = self_investigator._get_claude_client()
        assert str(client.base_url).rstrip("/") == _API
        assert client.api_key == _FAKE_KEY
        assert client.max_retries == 2
        assert self_investigator._get_claude_client() is client  # built once


# ===========================================================================
# Replay of assistant turns
# ===========================================================================


class TestReplay:
    async def test_thinking_blocks_come_back_unchanged(self, claude_on, install_api, local_brain):
        sig = "EqQBCkYIAxgCKkB+/abc==" + "Zm9v" * 20
        thinking = _thinking("I should read boom.py first.", sig)
        redacted = {"type": "redacted_thinking", "data": "RVJBU0VE" * 10}
        tool = _tool("read_file", {"path": "orchestrator/boom.py"}, "toolu_01")
        api = install_api(_message([thinking, redacted, _text("Looking."), tool], stop="tool_use"), _final())
        out = await _investigate()
        assert out["result"] == "ok"
        assistant = api.bodies[1]["messages"][1]
        assert assistant["role"] == "assistant"
        replayed = assistant["content"]
        assert replayed[0] == thinking  # byte-for-byte: text and signature untouched
        assert replayed[1] == redacted
        assert replayed[2]["type"] == "text" and replayed[2]["text"] == "Looking."
        assert {k: replayed[3][k] for k in ("type", "id", "name", "input")} == tool
        assert sig in api.requests[1].content.decode()

    async def test_one_user_turn_holds_all_results_of_a_round(self, claude_on, install_api, local_brain):
        calls = [
            _tool("read_file", {"path": "orchestrator/boom.py"}, "toolu_a"),
            _tool("grep", {"pattern": "boom failed"}, "toolu_b"),
            _tool("list_dir", {"path": "orchestrator"}, "toolu_c"),
        ]
        api = install_api(_message(calls, stop="tool_use"), _final())
        out = await _investigate()
        assert out["tool_calls"] == 3
        msgs = api.bodies[1]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        results = msgs[2]["content"]
        assert [r["type"] for r in results] == ["tool_result"] * 3
        assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b", "toolu_c"]
        assert all(r["content"].startswith("BEGIN UNTRUSTED LOG DATA") for r in results)
        assert "boom failed" in results[0]["content"]
        assert not any(r.get("is_error") for r in results)

    async def test_calls_past_the_round_cap_get_error_results(self, claude_on, install_api, local_brain, monkeypatch):
        from orchestrator.self_investigator import MAX_CALLS_PER_ROUND

        monkeypatch.setattr(claude_on.settings, "self_audit_investigate_max_tool_calls", 30, raising=False)
        calls = [_tool("grep", {"pattern": f"p{i}"}, f"toolu_{i}") for i in range(MAX_CALLS_PER_ROUND + 2)]
        api = install_api(_message(calls, stop="tool_use"), _final())
        out = await _investigate()
        assert out["result"] == "ok"
        assert out["tool_calls"] == MAX_CALLS_PER_ROUND
        results = api.bodies[1]["messages"][2]["content"]
        # Every tool_use in the replayed turn has a result, in order.
        assert [r["tool_use_id"] for r in results] == [c["id"] for c in calls]
        kept, extra = results[:MAX_CALLS_PER_ROUND], results[MAX_CALLS_PER_ROUND:]
        assert not any(r.get("is_error") for r in kept)
        assert all(r["is_error"] is True for r in extra)
        assert all(f"at most {MAX_CALLS_PER_ROUND} tool calls" in r["content"] for r in extra)

    async def test_fallback_block_replayed_with_its_wire_keys(self, claude_on, install_api, local_brain):
        """The `from` field is `from_` in the SDK model: it must go back as `from`."""
        fb = _fallback("claude-opus-5-5", "claude-opus-4-8")
        tool = _tool("read_file", {"path": "orchestrator/boom.py"}, "toolu_01")
        api = install_api(_message([fb, tool], stop="tool_use"), _final())
        await _investigate()
        replayed = api.bodies[1]["messages"][1]["content"][0]
        assert replayed["type"] == "fallback"
        assert replayed["from"] == {"model": "claude-opus-5-5"}
        assert replayed["to"] == {"model": "claude-opus-4-8"}
        assert "from_" not in replayed

    async def test_only_calls_after_the_last_fallback_are_run(self, claude_on, install_api, local_brain):
        before = _tool("grep", {"pattern": "declined-call"}, "toolu_before")
        after = _tool("grep", {"pattern": "boom failed"}, "toolu_after")
        fb = _fallback("claude-opus-5-5", "claude-opus-4-8")
        api = install_api(_message([before, fb, after], stop="tool_use"), _final())
        out = await _investigate()
        assert out["tool_calls"] == 1
        results = api.bodies[1]["messages"][2]["content"]
        assert [r["tool_use_id"] for r in results if r["type"] == "tool_result"] == ["toolu_after"]
        assert "boom failed" in results[0]["content"]

    async def test_replay_drops_the_declined_part_before_the_last_fallback(self, claude_on, install_api, local_brain):
        """Before the final fallback block only text and the fallback block go
        back; the declined thinking and tool_use are left out (and the left-out
        tool_use gets no tool_result). After it, everything goes back."""
        turn = [
            _thinking("", "SIG_BEFORE"),
            _text("partial from the declining model"),
            _tool("grep", {"pattern": "declined-call"}, "toolu_before"),
            _fallback("claude-opus-5-5", "claude-opus-4-8"),
            _thinking("", "SIG_AFTER"),
            _tool("grep", {"pattern": "boom failed"}, "toolu_after"),
        ]
        api = install_api(_message(turn, stop="tool_use"), _final())
        await _investigate()
        replayed = api.bodies[1]["messages"][1]["content"]
        assert [b["type"] for b in replayed] == ["text", "fallback", "thinking", "tool_use"]
        assert replayed[0]["text"] == "partial from the declining model"
        assert replayed[2]["signature"] == "SIG_AFTER" and replayed[3]["id"] == "toolu_after"
        results = api.bodies[1]["messages"][2]["content"]
        assert [r["tool_use_id"] for r in results if r["type"] == "tool_result"] == ["toolu_after"]

    async def test_turn_without_fallback_goes_back_unchanged(self, claude_on, install_api, local_brain):
        turn = [_thinking("", "SIG1"), _text("looking"), _tool("grep", {"pattern": "boom"}, "toolu_1")]
        api = install_api(_message(turn, stop="tool_use"), _final())
        await _investigate()
        replayed = api.bodies[1]["messages"][1]["content"]
        assert [b["type"] for b in replayed] == ["thinking", "text", "tool_use"]
        assert replayed[0]["signature"] == "SIG1"


# ===========================================================================
# Budget-forced final report
# ===========================================================================


class TestBudgetFinal:
    async def test_budget_forces_final_with_tool_choice_none(self, claude_on, install_api, local_brain):
        from orchestrator.self_investigator import CLAUDE_TOOLS

        # max_tool_calls is 4 in the fixture: 4 tool rounds, then the forced report.
        api = install_api(*[_grep_round(f"toolu_{i}", f"p{i}") for i in range(4)], _final())
        out = await _investigate()
        assert out["result"] == "budget" and out["report"] == _FINAL and out["backend"] == "claude"
        assert out["tool_calls"] == 4
        last = api.bodies[-1]
        assert len(api.bodies) == 5
        assert last["tool_choice"] == {"type": "none"}
        assert last["tools"] == json.loads(json.dumps(CLAUDE_TOOLS))  # history holds tool_use blocks
        assert set(last) == _EXPECTED_KEYS | {"tool_choice"}
        roles = [m["role"] for m in last["messages"]]
        assert roles == ["user", "assistant"] * 4 + ["user"]  # strictly alternating
        final_turn = last["messages"][-1]["content"]
        assert [b["type"] for b in final_turn] == ["tool_result", "text"]  # text AFTER the results
        assert final_turn[0]["tool_use_id"] == "toolu_3"
        assert final_turn[1]["text"].startswith(_BUDGET_TEXT)
        assert local_brain.await_count == 0
        assert all("tool_choice" not in b for b in api.bodies[:-1])

    async def test_rounds_budget_also_forces_final(self, claude_on, install_api, local_brain, monkeypatch):
        monkeypatch.setattr(claude_on.settings, "self_audit_investigate_max_rounds", 2, raising=False)
        monkeypatch.setattr(claude_on.settings, "self_audit_investigate_max_tool_calls", 30, raising=False)
        api = install_api(_grep_round("toolu_1", "a"), _grep_round("toolu_2", "b"), _final())
        out = await _investigate()
        assert out["result"] == "budget" and out["rounds"] == 2 and out["report"] == _FINAL
        assert api.bodies[-1]["tool_choice"] == {"type": "none"}


# ===========================================================================
# Backend choice: Helios asleep, first-call failures
# ===========================================================================


class TestBackendChoice:
    async def test_helios_asleep_investigates_on_claude(self, claude_on, install_api, local_brain):
        claude_on.brain_ready.return_value = False
        before = _runs("ok", "claude")
        api = install_api(_final())
        out = await _investigate()
        assert out["result"] == "ok" and out["backend"] == "claude" and out["report"] == _FINAL
        assert out["claude_skipped"] is None
        assert claude_on.brain_ready.await_count == 0  # Claude never needs Helios
        assert local_brain.await_count == 0
        assert len(api.requests) == 1
        assert _runs("ok", "claude") == before + 1

    async def test_claude_off_uses_local_and_never_builds_a_client(self, claude_on, local_brain, monkeypatch):
        from orchestrator import self_investigator

        monkeypatch.setattr(claude_on.settings, "self_audit_claude_enabled", False, raising=False)
        out = await _investigate()
        assert out["backend"] == "local" and out["result"] == "ok"
        assert out["claude_skipped"] is None and out["claude_usd"] == 0.0
        assert self_investigator._claude_client is None
        assert _day_key() not in claude_on.store

    @pytest.mark.parametrize(
        "reply,reason",
        [
            pytest.param(_error(401, "authentication_error"), "auth", id="401"),
            pytest.param(_error(403, "permission_error"), "auth", id="403"),
            pytest.param(_error(402, "billing_error"), "billing", id="402"),
            pytest.param(_error(400, "billing_error"), "billing", id="400-billing_error"),
            pytest.param(
                _error(429, "rate_limit_error", "enforced_spend_limit_reached"), "billing", id="429-spend-limit"
            ),
            pytest.param(_error(400, "invalid_request_error", "organization_on_hold"), "billing", id="400-on-hold"),
            pytest.param(_error(429, "rate_limit_error"), "rate_limit", id="429"),
            pytest.param(_error(404, "not_found_error"), "model", id="404"),
            pytest.param(_error(500, "api_error"), "server", id="500"),
            pytest.param(_error(529, "overloaded_error"), "server", id="529"),
            pytest.param(_error(400, "invalid_request_error"), "bad_request", id="400"),
            pytest.param(_error(422, "invalid_request_error"), "other", id="422"),
            pytest.param(_connect_error, "connection", id="connect-error"),
            pytest.param(_read_timeout, "connection", id="read-timeout"),
        ],
    )
    async def test_first_call_failure_reruns_on_local_brain(self, claude_on, install_api, local_brain, reply, reason):
        before_fb = _fallback_count(reason)
        before_runs = _runs("ok", "local")
        api = install_api(reply)
        out = await _investigate()
        assert out["backend"] == "local" and out["result"] == "ok" and out["report"] == _FINAL
        assert out["claude_skipped"] == reason
        assert local_brain.await_count == 1
        assert len(api.requests) == 1 and not api.overflow
        assert _fallback_count(reason) == before_fb + 1
        assert _runs("ok", "local") == before_runs + 1
        assert float(claude_on.store[_day_key()]) == 1  # the attempt counts as a paid run

    async def test_first_call_refusal_reruns_on_local_brain(self, claude_on, install_api, local_brain):
        before = _fallback_count("refusal")
        api = install_api(_message([_text("I can't help with that.")], stop="refusal", usage=_usage(100, 50)))
        out = await _investigate()
        assert out["backend"] == "local" and out["claude_skipped"] == "refusal" and out["report"] == _FINAL
        assert "can't help" not in (out["report"] or "")
        assert out["claude_usd"] == pytest.approx((100 * 4.0 + 50 * 20.0) / 1e6)  # a decline is still billed
        assert _fallback_count("refusal") == before + 1
        assert len(api.requests) == 1

    @pytest.mark.parametrize("stop", ["pause_turn", "model_context_window_exceeded", "something_new"])
    async def test_first_call_odd_stop_reason_reruns_on_local_brain(self, claude_on, install_api, local_brain, stop):
        install_api(_message([_text("partial")], stop=stop))
        out = await _investigate()
        assert out["backend"] == "local" and out["claude_skipped"] == "other" and out["report"] == _FINAL

    async def test_first_call_failure_with_helios_asleep(self, claude_on, install_api, local_brain):
        claude_on.brain_ready.return_value = False
        before_asleep = _runs("brain_asleep", "local")
        before_fb = _fallback_count("auth")
        install_api(_error(401, "authentication_error"))
        out = await _investigate()
        assert out["result"] == "brain_asleep" and out["report"] is None
        assert out["backend"] == "local" and out["claude_skipped"] == "auth"
        assert local_brain.await_count == 0
        assert _runs("brain_asleep", "local") == before_asleep + 1
        assert _fallback_count("auth") == before_fb + 1

    async def test_crash_in_the_first_round_reruns_on_local_brain(
        self, claude_on, install_api, local_brain, monkeypatch
    ):
        from orchestrator import self_investigator

        monkeypatch.setattr(self_investigator, "run_tool", AsyncMock(side_effect=RuntimeError("bug")))
        install_api(_grep_round("toolu_1", "boom"))
        out = await _investigate()
        assert out["backend"] == "local" and out["claude_skipped"] == "other" and out["report"] == _FINAL

    async def test_crash_in_a_later_round_is_a_claude_error(self, claude_on, install_api, local_brain, monkeypatch):
        from orchestrator import self_investigator

        real = self_investigator.run_tool
        seen = {"n": 0}

        async def flaky(name, args):
            seen["n"] += 1
            if seen["n"] > 1:
                raise RuntimeError("bug")
            return await real(name, args)

        monkeypatch.setattr(self_investigator, "run_tool", flaky)
        install_api(_grep_round("toolu_1", "a"), _grep_round("toolu_2", "b"))
        out = await _investigate()
        assert out["backend"] == "claude" and out["result"] == "error" and out["claude_skipped"] is None
        assert local_brain.await_count == 0

    async def test_alert_run_falls_back_with_the_alert_budget(self, claude_on, install_api, local_brain):
        from orchestrator import self_investigator

        install_api(_error(500, "api_error"))
        out = await _investigate(trigger="alert", alert={"alertname": "BrainOnCPU"}, lookback_hours=3)
        assert out["backend"] == "local" and out["claude_skipped"] == "server"
        assert self_investigator._wait_for_quiet_period.await_count == 1  # local alert etiquette still applies


# ===========================================================================
# Later-round failures
# ===========================================================================


class TestLaterRoundFailure:
    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(_error(500, "api_error"), id="500"),
            pytest.param(_error(529, "overloaded_error"), id="529"),
            pytest.param(_error(429, "rate_limit_error"), id="429"),
            pytest.param(_connect_error, id="connect-error"),
        ],
    )
    async def test_transient_failure_salvages_a_final_report(self, claude_on, install_api, local_brain, failure):
        api = install_api(_grep_round("toolu_1", "boom"), failure, _final())
        out = await _investigate()
        assert out["result"] == "budget" and out["report"] == _FINAL
        assert out["backend"] == "claude" and out["claude_skipped"] is None
        assert local_brain.await_count == 0
        b2, b3 = api.bodies[1], api.bodies[2]
        assert b3["tool_choice"] == {"type": "none"}
        # The failed round's tool results were already sent: request 3 adds a
        # new user turn instead of editing that one.
        assert b3["messages"][: len(b2["messages"])] == b2["messages"]
        assert b3["messages"][-1]["role"] == "user"
        assert b3["messages"][-1]["content"].startswith(_BUDGET_TEXT)

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(_error(401, "authentication_error"), id="401"),
            pytest.param(_error(400, "invalid_request_error"), id="400"),
            pytest.param(_error(402, "billing_error"), id="402"),
            pytest.param(_message([_text("no")], stop="refusal"), id="refusal"),
        ],
    )
    async def test_non_transient_failure_is_an_error_without_local_rerun(
        self, claude_on, install_api, local_brain, failure
    ):
        before_err = _runs("error", "claude")
        before_auth = _fallback_count("auth")
        api = install_api(_grep_round("toolu_1", "boom"), failure)
        out = await _investigate()
        assert out["result"] == "error" and out["report"] is None
        assert out["backend"] == "claude" and out["claude_skipped"] is None
        assert local_brain.await_count == 0
        assert len(api.requests) == 2 and not api.overflow
        assert _runs("error", "claude") == before_err + 1
        assert _fallback_count("auth") == before_auth  # not a fallback

    async def test_salvage_call_failing_too_is_an_error(self, claude_on, install_api, local_brain):
        api = install_api(_grep_round("toolu_1", "boom"), _error(500, "api_error"), _error(500, "api_error"))
        out = await _investigate()
        assert out["result"] == "error" and out["report"] is None and out["backend"] == "claude"
        assert len(api.requests) == 3 and not api.overflow
        assert local_brain.await_count == 0


# ===========================================================================
# Spend accounting + caps
# ===========================================================================


class TestSpend:
    async def test_cost_of_one_call(self, claude_on, install_api, local_brain):
        kinds = ("input", "output", "cache_read", "cache_write")
        before = {k: _metric("bgw_self_investigator_claude_tokens_total", {"kind": k}) for k in kinds}
        install_api(_final(usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        # opus-5-5: 100*4 + 50*20 + 1000*0.20 + 200*5 = 2600 per MTok
        assert out["claude_usd"] == pytest.approx(0.0026)
        assert claude_on.store[_month_key()] == "0.002600"
        after = {k: _metric("bgw_self_investigator_claude_tokens_total", {"kind": k}) for k in kinds}
        assert {k: after[k] - before[k] for k in kinds} == {
            "input": 100,
            "output": 50,
            "cache_read": 1000,
            "cache_write": 200,
        }

    async def test_spend_accumulates_across_calls_and_runs(self, claude_on, install_api, local_brain):
        from orchestrator import self_investigator

        claude_on.store[_month_key()] = "1.5"
        u = _usage(100, 50, 1000, 200)
        install_api(_message([_tool("grep", {"pattern": "x"}, "toolu_1")], stop="tool_use", usage=u), _final(usage=u))
        out = await _investigate()
        assert out["claude_usd"] == pytest.approx(0.0052)
        install_api(_final(usage=u))
        await _investigate()
        assert float(claude_on.store[_month_key()]) == pytest.approx(1.5 + 3 * 0.0026)
        assert self_investigator.claude_spend_this_month() == pytest.approx(1.5078)

    async def test_month_rollover_starts_at_zero(self, claude_on, install_api, local_brain, monkeypatch):
        from orchestrator import self_investigator

        class _Clock(datetime):
            at = datetime(2026, 11, 30, 23, 50, tzinfo=UTC)

            @classmethod
            def now(cls, tz=None):
                return cls.at

        monkeypatch.setattr(self_investigator, "datetime", _Clock)
        claude_on.store["self_audit_claude_usd_2026-11"] = "40.0"
        api = install_api(_final(usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        assert out["claude_skipped"] == "cap" and len(api.requests) == 0  # November is spent
        _Clock.at = datetime(2026, 12, 1, 0, 5, tzinfo=UTC)
        out = await _investigate()
        assert out["backend"] == "claude" and out["claude_skipped"] is None
        assert claude_on.store["self_audit_claude_usd_2026-12"] == "0.002600"
        assert claude_on.store["self_audit_claude_usd_2026-11"] == "40.0"  # last month untouched
        assert claude_on.store["self_audit_claude_runs_2026-12-01"] == "1.000000"

    async def test_cap_reached_runs_local_without_calling_claude(self, claude_on, install_api, local_brain):
        claude_on.store[_month_key()] = "40"
        before = _fallback_count("cap")
        api = install_api(_final())
        out = await _investigate()
        assert out["backend"] == "local" and out["claude_skipped"] == "cap" and out["report"] == _FINAL
        assert out["claude_usd"] == 0.0
        assert len(api.requests) == 0
        assert _day_key() not in claude_on.store  # a skipped run isn't a paid run
        assert _fallback_count("cap") == before + 1
        assert claude_on.store[_month_key()] == "40"

    @pytest.mark.parametrize(
        "spent,trigger,expect_claude",
        [
            pytest.param("30", "alert", False, id="alert-at-75pct"),
            pytest.param("29.99", "alert", True, id="alert-below-75pct"),
            pytest.param("30", "daily", True, id="daily-at-75pct"),
            pytest.param("39.99", "daily", True, id="daily-below-cap"),
            pytest.param("40.0", "daily", False, id="daily-at-cap"),
        ],
    )
    async def test_alert_runs_only_use_75_percent_of_the_cap(
        self, claude_on, install_api, local_brain, spent, trigger, expect_claude
    ):
        claude_on.store[_month_key()] = spent
        api = install_api(_final())
        alert = {"alertname": "BrainOnCPU"} if trigger == "alert" else None
        out = await _investigate(trigger=trigger, alert=alert)
        assert (out["backend"] == "claude") is expect_claude
        assert len(api.requests) == (1 if expect_claude else 0)
        assert out["claude_skipped"] == (None if expect_claude else "cap")

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param("RAISE", id="read-raises"),
            pytest.param("garbage", id="garbage"),
            pytest.param("-1", id="negative"),
            pytest.param("nan", id="nan"),
            pytest.param("inf", id="inf"),
        ],
    )
    async def test_unreadable_spend_counter_fails_closed(
        self, claude_on, install_api, local_brain, monkeypatch, stored
    ):
        from orchestrator import state_store

        if stored == "RAISE":

            def boom(key):
                raise RuntimeError("db locked")

            monkeypatch.setattr(state_store, "get_app_state", boom)
        else:
            claude_on.store[_month_key()] = stored
        api = install_api(_final())
        out = await _investigate()
        assert out["claude_skipped"] == "cap" and out["backend"] == "local"
        assert len(api.requests) == 0
        if stored != "RAISE":
            assert claude_on.store[_month_key()] == stored  # never "repaired" to a smaller number

    async def test_failed_read_never_overwrites_the_stored_total(
        self, claude_on, install_api, local_brain, monkeypatch
    ):
        """The skip check reads the month total fine; the read after the call
        fails. The new cost must not be written over the stored 10.0."""
        from orchestrator import self_investigator, state_store

        key = _month_key()
        claude_on.store[key] = "10.0"
        reads = {"n": 0}

        def flaky(k):
            if k == key:
                reads["n"] += 1
                if reads["n"] > 1:
                    raise RuntimeError("db locked")
            return claude_on.store.get(k)

        monkeypatch.setattr(state_store, "get_app_state", flaky)
        install_api(_final(usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        assert out["backend"] == "claude"
        assert claude_on.store[key] == "10.0"
        # Once readable again the stored total counts, and the unwritten cost
        # isn't lost: it is added on top and written with the next call.
        monkeypatch.setattr(state_store, "get_app_state", lambda k: claude_on.store.get(k))
        assert self_investigator.claude_spend_this_month() == pytest.approx(10.0026)
        self_investigator._add_claude_spend(0.0)
        assert float(claude_on.store[key]) == pytest.approx(10.0026)

    async def test_failed_write_keeps_the_in_process_floor(self, claude_on, install_api, local_brain, monkeypatch):
        from orchestrator import self_investigator, state_store

        claude_on.store[_month_key()] = "39.999"

        def refuse_month_writes(k, v):
            if k.startswith("self_audit_claude_usd_"):
                raise RuntimeError("disk full")
            claude_on.store[k] = v

        monkeypatch.setattr(state_store, "set_app_state", refuse_month_writes)
        install_api(_final(usage=_usage(0, 100)))  # $0.002 -> past the $40 cap
        await _investigate()
        assert claude_on.store[_month_key()] == "39.999"
        assert self_investigator.claude_spend_this_month() == pytest.approx(40.001)
        api = install_api(_final())
        out = await _investigate()
        assert out["claude_skipped"] == "cap" and len(api.requests) == 0

    @pytest.mark.parametrize(
        "runs,expect_claude",
        [
            pytest.param("8", False, id="at-limit"),
            pytest.param("7", True, id="below-limit"),
            pytest.param("junk", False, id="unreadable"),
        ],
    )
    async def test_daily_run_cap(self, claude_on, install_api, local_brain, runs, expect_claude):
        claude_on.store[_day_key()] = runs
        before = _fallback_count("daily_cap")
        api = install_api(_final())
        out = await _investigate()
        assert (out["backend"] == "claude") is expect_claude
        assert len(api.requests) == (1 if expect_claude else 0)
        if expect_claude:
            assert claude_on.store[_day_key()] == "8.000000"
        else:
            assert out["claude_skipped"] == "daily_cap"
            assert _fallback_count("daily_cap") == before + 1
            assert claude_on.store[_day_key()] == runs

    async def test_eight_runs_a_day_then_local(self, claude_on, install_api, local_brain):
        from orchestrator.self_investigator import CLAUDE_MAX_RUNS_PER_DAY

        backends = []
        for _ in range(CLAUDE_MAX_RUNS_PER_DAY + 1):
            install_api(_final())
            backends.append((await _investigate())["backend"])
        assert backends == ["claude"] * CLAUDE_MAX_RUNS_PER_DAY + ["local"]

    async def test_timeout_on_first_call_charges_a_full_round(self, claude_on, install_api, local_brain):
        before = _metric(
            "bgw_background_llm_errors_total", {"caller": "self_investigator_claude", "error_type": "APITimeoutError"}
        )
        install_api(_read_timeout)
        out = await _investigate()
        assert out["claude_skipped"] == "connection" and out["backend"] == "local"
        assert out["claude_usd"] == pytest.approx(16000 * 20.0 / 1e6)  # CLAUDE_MAX_TOKENS at the output price
        assert claude_on.store[_month_key()] == "0.320000"
        after = _metric(
            "bgw_background_llm_errors_total", {"caller": "self_investigator_claude", "error_type": "APITimeoutError"}
        )
        assert after == before + 1

    async def test_timeout_on_later_round_is_charged_and_salvaged(self, claude_on, install_api, local_brain):
        u = _usage(100, 50, 1000, 200)
        install_api(
            _message([_tool("grep", {"pattern": "x"}, "toolu_1")], stop="tool_use", usage=u),
            _read_timeout,
            _final(usage=u),
        )
        out = await _investigate()
        assert out["result"] == "budget" and out["report"] == _FINAL
        assert out["claude_usd"] == pytest.approx(2 * 0.0026 + 0.32)

    async def test_connection_error_is_not_charged(self, claude_on, install_api, local_brain):
        install_api(_connect_error)
        out = await _investigate()
        assert out["claude_usd"] == 0.0
        assert _month_key() not in claude_on.store

    async def test_unreadable_usage_charges_a_full_round(self, claude_on):
        from orchestrator.self_investigator import _ClaudeConversation

        conv = _ClaudeConversation("task")
        resp = SimpleNamespace(model="claude-opus-5-5", content=[], usage=SimpleNamespace(input_tokens="lots"))
        conv._account(resp)
        # Never under: the higher of the model's and the unknown-model output price.
        assert conv.cost_usd == pytest.approx(16000 * 50.0 / 1e6)
        assert float(claude_on.store[_month_key()]) == pytest.approx(0.8)

    async def test_spend_and_cap_gauges(self, claude_on, monkeypatch):
        from orchestrator import self_investigator

        claude_on.store[_month_key()] = "2.5"
        self_investigator.refresh_claude_spend_gauge()
        assert _metric("bgw_self_investigator_claude_spend_usd") == pytest.approx(2.5)
        assert _metric("bgw_self_investigator_claude_cap_usd") == pytest.approx(40.0)
        claude_on.store[_month_key()] = "3.75"  # read on every scrape, no refresh needed
        assert _metric("bgw_self_investigator_claude_spend_usd") == pytest.approx(3.75)
        claude_on.store[_month_key()] = "garbage"
        assert math.isnan(REGISTRY.get_sample_value("bgw_self_investigator_claude_spend_usd"))
        monkeypatch.setattr(claude_on.settings, "self_audit_claude_enabled", False, raising=False)
        self_investigator.refresh_claude_spend_gauge()
        assert _metric("bgw_self_investigator_claude_cap_usd") == 0.0


# ===========================================================================
# Pricing across a server-side fallback
# ===========================================================================


class TestFallbackPricing:
    async def test_fallback_block_prices_at_the_dearest_model_and_hides_partial_text(
        self, claude_on, install_api, local_brain
    ):
        content = [_text("PARTIAL-FROM-DECLINER"), _fallback("claude-opus-5-5", "claude-opus-4-8"), _text(_FINAL)]
        install_api(_message(content, usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        assert out["result"] == "ok" and out["report"] == _FINAL
        assert "PARTIAL-FROM-DECLINER" not in out["report"]
        # max((4,20,.2,5), (5,25,.5,6.25)) = (5,25,.5,6.25): 500 + 1250 + 500 + 1250
        assert out["claude_usd"] == pytest.approx(0.0035)

    async def test_usage_iterations_model_is_priced(self, claude_on, install_api, local_brain):
        it = {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        iterations = [{**it, "type": "message", "model": "claude-opus-5-5"}]
        iterations.append({**it, "type": "fallback_message", "model": "claude-opus-4-8"})
        install_api(_final(usage=_usage(100, 50, 1000, 200, iterations=iterations)))
        out = await _investigate()
        assert out["claude_usd"] == pytest.approx(0.0035)

    async def test_served_by_unknown_model_prices_high(self, claude_on, install_api, local_brain):
        install_api(_final(model="claude-mystery-9", usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        # unknown row (10, 50, 1, 12.5): 1000 + 2500 + 1000 + 2500
        assert out["claude_usd"] == pytest.approx(0.007)

    async def test_cheaper_fallback_never_lowers_the_price(self, claude_on, install_api, local_brain):
        content = [_fallback("claude-opus-5-5", "claude-haiku-5-5"), _text(_FINAL)]
        install_api(_message(content, model="claude-haiku-5-5", usage=_usage(100, 50, 1000, 200)))
        out = await _investigate()
        assert out["claude_usd"] == pytest.approx(0.0026)  # still the requested opus-5-5 row

    def test_models_in_collects_every_side(self, claude_on):
        from orchestrator.self_investigator import _models_in

        resp = SimpleNamespace(
            model="claude-b",
            usage=SimpleNamespace(iterations=[SimpleNamespace(model="claude-c"), SimpleNamespace(model=None)]),
            content=[
                SimpleNamespace(
                    type="fallback", from_=SimpleNamespace(model="claude-d"), to=SimpleNamespace(model="claude-e")
                ),
                SimpleNamespace(type="text"),
            ],
        )
        assert _models_in(resp) == {"claude-opus-5-5", "claude-b", "claude-c", "claude-d", "claude-e"}


# ===========================================================================
# Task redaction
# ===========================================================================


class TestRedaction:
    async def test_secret_shaped_text_never_reaches_the_api(self, claude_on, install_api, local_brain):
        key_in_log = "sk-ant-api03-" + "Ab9_-" * 10
        key_in_alert = "sk-proj-" + "Zz8_-" * 8
        key_in_repo = "sk-ant-admin01-" + "Qq7_-" * 8
        (claude_on.repo / "orchestrator" / "leaky.py").write_text(f"# leaked: {key_in_repo}\nx = 1\n")
        now = datetime.now(UTC)
        clusters = [
            {
                "service": "orchestrator",
                "level": "error",
                "count": 3,
                "first_seen": now,
                "last_seen": now,
                "sample": f"auth failed for key {key_in_log} (401)",
            }
        ]
        alert = {"alertname": "BrainAuthRejected", "annotations": {"summary": f"bad key {key_in_alert}"}}
        api = install_api(
            _message([_tool("read_file", {"path": "orchestrator/leaky.py"}, "toolu_1")], stop="tool_use"),
            _final(),
        )
        out = await _investigate(clusters=clusters, trigger="alert", alert=alert, lookback_hours=3)
        assert out["backend"] == "claude"
        raw = b"".join(r.content for r in api.requests).decode()
        for secret in (key_in_log, key_in_alert, key_in_repo):
            assert secret not in raw
            assert secret[:20] not in raw  # no usable prefix either
        task = api.bodies[0]["messages"][0]["content"]
        assert task.count("<REDACTED-SECRET>") >= 2
        assert "<REDACTED-SECRET>" in api.bodies[1]["messages"][2]["content"][0]["content"]

    async def test_local_backend_gets_the_redacted_task_too(self, claude_on, local_brain, monkeypatch):
        monkeypatch.setattr(claude_on.settings, "self_audit_claude_enabled", False, raising=False)
        key = "sk-ant-api03-" + "Ab9_-" * 10
        now = datetime.now(UTC)
        clusters = [
            {
                "service": "orchestrator",
                "level": "error",
                "count": 1,
                "first_seen": now,
                "last_seen": now,
                "sample": key,
            }
        ]
        await _investigate(clusters=clusters)
        sent = json.dumps(local_brain.await_args.kwargs["messages"])
        assert key not in sent and "<REDACTED-SECRET>" in sent


# ===========================================================================
# _claude_failure_reason
# ===========================================================================


def _status_error(cls, status: int, body: dict | None = None):
    req = httpx2.Request("POST", f"{_API}/v1/messages")
    return cls("x", response=httpx2.Response(status, request=req), body=body)


def _req():
    return httpx2.Request("POST", f"{_API}/v1/messages")


@pytest.mark.parametrize(
    "make,reason",
    [
        pytest.param(lambda: TimeoutError(), "connection", id="timeout"),  # == asyncio.TimeoutError on 3.11+
        pytest.param(lambda: anthropic.APIConnectionError(request=_req()), "connection", id="connection"),
        pytest.param(lambda: anthropic.APITimeoutError(request=_req()), "connection", id="api-timeout"),
        pytest.param(lambda: _status_error(anthropic.AuthenticationError, 401), "auth", id="401"),
        pytest.param(lambda: _status_error(anthropic.PermissionDeniedError, 403), "auth", id="403"),
        pytest.param(lambda: _status_error(anthropic.PaymentRequiredError, 402), "billing", id="402"),
        pytest.param(
            lambda: _status_error(anthropic.BadRequestError, 400, {"error": {"type": "billing_error"}}),
            "billing",
            id="400-billing_error",
        ),
        pytest.param(
            lambda: _status_error(anthropic.EnforcedSpendLimitReachedError, 429), "billing", id="enforced-spend"
        ),
        pytest.param(
            lambda: _status_error(anthropic.ContractSpendLimitReachedError, 429), "billing", id="contract-spend"
        ),
        pytest.param(lambda: _status_error(anthropic.RampLimitExceededError, 429), "billing", id="ramp-limit"),
        pytest.param(lambda: _status_error(anthropic.OrganizationOnHoldError, 400), "billing", id="on-hold"),
        pytest.param(
            lambda: _status_error(anthropic.LinkedOrganizationSpendLimitReachedError, 402), "billing", id="linked-org"
        ),
        pytest.param(lambda: _status_error(anthropic.NotFoundError, 404), "model", id="404"),
        pytest.param(lambda: _status_error(anthropic.RateLimitError, 429), "rate_limit", id="429"),
        pytest.param(lambda: _status_error(anthropic.InternalServerError, 500), "server", id="500"),
        pytest.param(lambda: _status_error(anthropic.OverloadedError, 529), "server", id="529"),
        pytest.param(lambda: _status_error(anthropic.BadRequestError, 400), "bad_request", id="400"),
        pytest.param(lambda: _status_error(anthropic.ConflictError, 409), "other", id="409"),
        pytest.param(lambda: ValueError("x"), "other", id="value-error"),
        pytest.param(lambda: anthropic.AnthropicError("x"), "other", id="anthropic-base"),
    ],
)
def test_claude_failure_reason(make, reason):
    from orchestrator.self_investigator import _claude_failure_reason

    assert _claude_failure_reason(make()) == reason


def test_every_billing_class_name_exists_in_the_sdk():
    """A renamed SDK class would silently drop out of the billing tuple."""
    from orchestrator.self_investigator import _BILLING_ERRORS

    for name in _BILLING_ERRORS:
        assert isinstance(getattr(anthropic, name, None), type), name


@pytest.mark.parametrize(
    "make,expected",
    [
        pytest.param(lambda: TimeoutError(), True, id="asyncio"),
        pytest.param(lambda: anthropic.APITimeoutError(request=_req()), True, id="sdk"),
        pytest.param(lambda: anthropic.APIConnectionError(request=_req()), False, id="connection"),
        pytest.param(lambda: ValueError(), False, id="other"),
    ],
)
def test_is_timeout(make, expected):
    from orchestrator.self_investigator import _is_timeout

    assert _is_timeout(make()) is expected


def test_visible_blocks():
    from orchestrator.self_investigator import _visible_blocks

    b = [SimpleNamespace(type=t) for t in ("text", "fallback", "thinking", "fallback", "text", "tool_use")]
    assert _visible_blocks(b) == b[4:]
    assert _visible_blocks(b[:1]) == b[:1]
    assert _visible_blocks([]) == []


# ===========================================================================
# Config validator
# ===========================================================================


def _settings(**kw):
    from orchestrator.config import Settings

    clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("SELF_AUDIT_CLAUDE")}
    with patch.dict(os.environ, clean, clear=True):
        return Settings(_env_file=None, **kw)


class TestConfig:
    def test_valid_key_enables_claude(self):
        s = _settings(self_audit_claude_api_key=_FAKE_KEY)
        assert s.self_audit_claude_enabled is True
        assert s.self_audit_claude_api_key == _FAKE_KEY
        assert s.self_audit_claude_model == "claude-opus-5-5"
        assert s.self_audit_claude_effort == "medium"
        assert s.self_audit_claude_monthly_usd == 40.0

    def test_no_key_means_local(self):
        assert _settings().self_audit_claude_enabled is False

    def test_enabled_flag_is_derived_not_configurable(self):
        assert _settings(self_audit_claude_enabled=True).self_audit_claude_enabled is False

    def test_enabled_flag_from_env_is_ignored(self):
        from orchestrator.config import Settings

        clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("SELF_AUDIT_CLAUDE")}
        clean["SELF_AUDIT_CLAUDE_ENABLED"] = "true"
        with patch.dict(os.environ, clean, clear=True):
            assert Settings(_env_file=None).self_audit_claude_enabled is False

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("sk-ant-short", id="short"),
            pytest.param("sk-proj-" + "x" * 40, id="openai-key"),
            pytest.param("x" * 40, id="no-prefix"),
            pytest.param("SK-ANT-" + "x" * 40, id="wrong-case"),
        ],
    )
    def test_bad_key_is_dropped(self, key, caplog):
        with caplog.at_level(logging.DEBUG, logger="orchestrator.config"):
            s = _settings(self_audit_claude_api_key=key)
        assert s.self_audit_claude_enabled is False
        assert s.self_audit_claude_api_key == ""
        assert key not in caplog.text  # the key itself is never logged

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(_FAKE_KEY + "\n", id="newline"),
            pytest.param("  " + _FAKE_KEY + " \r\n", id="spaces-crlf"),
        ],
    )
    def test_key_whitespace_is_stripped(self, raw):
        s = _settings(self_audit_claude_api_key=raw)
        assert s.self_audit_claude_api_key == _FAKE_KEY
        assert s.self_audit_claude_enabled is True

    def test_whitespace_only_key_means_local(self):
        s = _settings(self_audit_claude_api_key="   \n")
        assert s.self_audit_claude_enabled is False and s.self_audit_claude_api_key == ""

    @pytest.mark.parametrize(
        "cap",
        [
            pytest.param(0.0, id="zero"),
            pytest.param(-5.0, id="negative"),
            pytest.param(float("inf"), id="inf"),
            pytest.param(float("nan"), id="nan"),
        ],
    )
    def test_bad_cap_turns_claude_off(self, cap):
        s = _settings(self_audit_claude_api_key=_FAKE_KEY, self_audit_claude_monthly_usd=cap)
        assert s.self_audit_claude_enabled is False
        assert s.self_audit_claude_monthly_usd == 0.0

    def test_bad_effort_falls_back_to_medium(self):
        s = _settings(self_audit_claude_api_key=_FAKE_KEY, self_audit_claude_effort="turbo")
        assert s.self_audit_claude_effort == "medium"
        assert s.self_audit_claude_enabled is True

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
    def test_good_effort_kept(self, effort):
        assert _settings(self_audit_claude_effort=effort).self_audit_claude_effort == effort

    @pytest.mark.parametrize(
        "model",
        [
            pytest.param("gpt-5", id="not-claude"),
            pytest.param("Claude-Opus-5-5", id="upper-case"),
            pytest.param("claude-opus 5", id="space"),
            pytest.param("claude-", id="bare-prefix"),
            pytest.param("claude-" + "a" * 61, id="too-long"),
            pytest.param("", id="empty"),
            pytest.param("claude-opus-5-5/../x", id="path"),
        ],
    )
    def test_bad_model_turns_claude_off(self, model):
        s = _settings(self_audit_claude_api_key=_FAKE_KEY, self_audit_claude_model=model)
        assert s.self_audit_claude_enabled is False

    def test_other_claude_model_ids_accepted(self):
        s = _settings(self_audit_claude_api_key=_FAKE_KEY, self_audit_claude_model="claude-sonnet-5-5")
        assert s.self_audit_claude_enabled is True


# ===========================================================================
# Digest note (jobs_self_audit._investigator_note)
# ===========================================================================


class TestInvestigatorNote:
    @pytest.mark.parametrize(
        "inv,spent,expected",
        [
            pytest.param(
                {"result": "ok", "backend": "claude", "claude_skipped": None},
                "1.234",
                "Investigated by Claude · ~$1.23 of $40 this month",
                id="claude-ok",
            ),
            pytest.param(
                {"result": "budget", "backend": "claude", "claude_skipped": None},
                "0",
                "Investigated by Claude · ~$0.00 of $40 this month",
                id="claude-budget",
            ),
            pytest.param(
                {"result": "ok", "backend": "claude", "claude_skipped": None},
                "garbage",
                "Investigated by Claude",
                id="claude-ok-spend-unreadable",
            ),
            pytest.param(
                {"result": "error", "backend": "claude", "claude_skipped": None},
                "2.5",
                "Claude investigation failed partway · ~$2.50 of $40 this month",
                id="claude-error",
            ),
            pytest.param(
                {"result": "ok", "backend": "local", "claude_skipped": "cap"},
                "40",
                "monthly Claude cap reached — investigated by the local brain",
                id="cap-skip",
            ),
            pytest.param(
                {"result": "ok", "backend": "local", "claude_skipped": "daily_cap"},
                "1",
                "daily Claude run limit reached — investigated by the local brain",
                id="daily-cap-skip",
            ),
            pytest.param(
                {"result": "ok", "backend": "local", "claude_skipped": "auth"},
                "1",
                "Claude key rejected — investigated by the local brain",
                id="auth-local",
            ),
            pytest.param(
                {"result": "ok", "backend": "local", "claude_skipped": "brand_new_reason"},
                "1",
                "Claude unavailable — investigated by the local brain",
                id="unknown-reason",
            ),
            pytest.param(
                {"result": "brain_asleep", "backend": "local", "claude_skipped": None},
                "1",
                "Helios asleep — log digest only, no investigation",
                id="asleep-no-skip",
            ),
            pytest.param(
                {"result": "brain_asleep", "backend": "local", "claude_skipped": "billing"},
                "1",
                "Claude credit or spend limit used up and Helios asleep — log digest only",
                id="asleep-with-skip",
            ),
            pytest.param({"result": "ok", "backend": "local", "claude_skipped": None}, "1", "", id="local-only"),
        ],
    )
    def test_note(self, claude_on, inv, spent, expected):
        from orchestrator.jobs_self_audit import _investigator_note

        claude_on.store[_month_key()] = spent
        assert _investigator_note(inv) == expected

    def test_every_fallback_reason_has_its_own_text(self):
        """Each metric reason (except 'other') reads as its own line in the digest."""
        from orchestrator.jobs_self_audit import _CLAUDE_SKIP_TEXT

        reasons = {"cap", "daily_cap", "auth", "billing", "model", "rate_limit", "server", "connection"}
        reasons |= {"bad_request", "refusal"}
        assert reasons <= set(_CLAUDE_SKIP_TEXT)

    @respx.mock
    async def test_digest_names_claude_end_to_end(self, claude_on, install_api, local_brain, monkeypatch):
        from orchestrator import inbox, jobs_self_audit, pushover_manager, shared

        pushover = AsyncMock(return_value=None)
        monkeypatch.setattr(pushover_manager, "deliver_pushover_confirm", pushover, raising=False)
        monkeypatch.setattr(inbox, "post_message", AsyncMock(return_value=None))
        palace = AsyncMock()
        palace.store = AsyncMock(return_value=None)
        monkeypatch.setattr(shared, "get_palace", lambda: palace, raising=False)
        now_ns = int(datetime.now(UTC).timestamp() * 1e9)
        streams = {
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"service": "orchestrator", "level": "error"},
                        "values": [[str(now_ns - i * 1_000_000_000), "boom failed"] for i in range(3)],
                    }
                ],
            }
        }
        respx.get(f"{_LOKI_URL}/loki/api/v1/query_range").mock(return_value=Response(200, json=streams))
        api = install_api(_final(usage=_usage(100, 50, 1000, 200)))
        out = await jobs_self_audit.run_self_audit()
        assert out["result"] == "ok"
        assert len(api.requests) == 1 and local_brain.await_count == 0
        message = pushover.await_args.kwargs["message"]
        assert "Investigated by Claude · ~$0.00 of $40 this month" in message
        assert Path(out["report_path"]).exists()


# ===========================================================================
# SECRET_PATTERNS (jobs_training_corpus + scripts/build_embedding_corpus.py)
# ===========================================================================

_FAKE_SECRETS = [
    pytest.param("sk-ant-api03-" + "aB3_-" * 12, id="anthropic-api03"),
    pytest.param("sk-ant-admin01-" + "Zx9-_" * 10, id="anthropic-admin"),
    pytest.param("sk-proj-" + "Qw2_-" * 10, id="openai-proj"),
    pytest.param("sk-svcacct-" + "Er4-_" * 10, id="openai-svcacct"),
    pytest.param("sk-admin-" + "Ty6__" * 10, id="openai-admin"),
    pytest.param("sk-" + "A1b2C3d4" * 4, id="openai-legacy"),
]

_BENIGN = [
    pytest.param("the risk-assessment for the deploy", id="risk-assessment"),
    pytest.param("ask-ant colony", id="ask-ant"),
    pytest.param("sk-ant-short", id="short-ant"),
    pytest.param("desk-proj is small", id="desk-proj"),
    pytest.param("whisk-admin-x panel", id="whisk-admin"),
    pytest.param("task-svcacct rotation", id="task-svcacct"),
]


def _embedding_corpus_patterns():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_embedding_corpus.py"
    if not path.exists():
        pytest.skip("scripts/ not mounted")
    spec = importlib.util.spec_from_file_location("_bec_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SECRET_PATTERNS


def _redact_with(patterns, text: str) -> str:
    for p in patterns:
        text = p.sub("<REDACTED-SECRET>", text)
    return text


class TestSecretPatterns:
    @pytest.mark.parametrize("secret", _FAKE_SECRETS)
    def test_training_corpus_patterns_redact(self, secret):
        from orchestrator.jobs_self_audit import _redact_secrets

        out = _redact_secrets(f"key={secret} trailing")
        assert secret not in out and "<REDACTED-SECRET>" in out
        assert out.endswith(" trailing")
        assert secret[:16] not in out  # nothing usable left over

    @pytest.mark.parametrize("secret", _FAKE_SECRETS)
    def test_embedding_corpus_patterns_redact(self, secret):
        out = _redact_with(_embedding_corpus_patterns(), f"key {secret} trailing")
        assert secret not in out and "<REDACTED-SECRET>" in out

    @pytest.mark.parametrize("text", _BENIGN)
    def test_ordinary_words_survive(self, text):
        from orchestrator.jobs_self_audit import _redact_secrets

        assert _redact_secrets(text) == text
        assert _redact_with(_embedding_corpus_patterns(), text) == text

    def test_new_pattern_is_in_both_lists(self):
        from orchestrator.jobs_training_corpus import SECRET_PATTERNS

        src = r"sk-(?:ant|proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"
        assert src in [p.pattern for p in SECRET_PATTERNS]
        assert src in [p.pattern for p in _embedding_corpus_patterns()]


# ===========================================================================
# Logger quieting (log_config)
# ===========================================================================


@pytest.fixture
def restore_logger_levels():
    names = ("anthropic", "httpx2", "httpcore2", "anthropic._base_client")
    saved = {n: logging.getLogger(n).level for n in names}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


class TestLogQuieting:
    @pytest.mark.parametrize("name", ["anthropic", "httpx2", "httpcore2"])
    def test_sdk_loggers_quieted_to_warning(self, name, restore_logger_levels, monkeypatch):
        from orchestrator import log_config

        monkeypatch.delenv("ANTHROPIC_LOG", raising=False)
        logging.getLogger(name).setLevel(logging.DEBUG)
        log_config.quiet_url_logging_libs()
        assert logging.getLogger(name).level == logging.WARNING
        # The SDK's own import-time logging setup doesn't undo it.
        anthropic._setup_logging()
        assert logging.getLogger(name).level == logging.WARNING
        assert not logging.getLogger(name + ".child").isEnabledFor(logging.INFO)

    def test_payload_filter_drops_sdk_record_args(self):
        from orchestrator.log_config import DropPayloadArgsFilter

        for name in ("anthropic._base_client", "httpx2", "httpcore2.connection"):
            rec = logging.LogRecord(name, logging.WARNING, __file__, 1, "Request options: %s", ({"body": "x"},), None)
            assert DropPayloadArgsFilter().filter(rec) is True
            assert rec.args is None and "[args dropped]" in rec.getMessage()
            assert "body" not in rec.getMessage()

    def test_payload_filter_leaves_our_records_alone(self):
        from orchestrator.log_config import DropPayloadArgsFilter

        rec = logging.LogRecord(
            "orchestrator.self_investigator", logging.WARNING, __file__, 1, "Claude call failed: %s", ("X",), None
        )
        DropPayloadArgsFilter().filter(rec)
        assert rec.getMessage() == "Claude call failed: X"


# ===========================================================================
# Logs from a Claude run carry no content
# ===========================================================================


async def test_claude_run_logs_no_model_text_or_paths(claude_on, install_api, local_brain, caplog):
    secret_words = "UNIQUE-MODEL-PROSE-7731"
    tool = _tool("read_file", {"path": "orchestrator/boom.py"}, "toolu_1")
    install_api(
        _message([_text(secret_words), tool], stop="tool_use"),
        _error(401, "authentication_error"),
    )
    with caplog.at_level(logging.DEBUG, logger="orchestrator"):
        await _investigate()
    # Our own records (the SDK's are quieted separately, see TestLogQuieting).
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("orchestrator"))
    assert "[INVESTIGATOR] Claude call failed: AuthenticationError status=401 reason=auth" in ours
    for leak in (secret_words, "orchestrator/boom.py", "boom failed", _FAKE_KEY, '"x"'):
        assert leak not in ours
