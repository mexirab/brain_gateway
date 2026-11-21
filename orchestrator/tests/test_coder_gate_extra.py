"""Coder gate: gap tests on top of test_coder_gate.py.

Covers what that file leaves thin: the coder-net rate limit, vision entering
``brain_slot.interactive_call`` (and preempting a running coder attempt),
``startup_check``'s log branches, cancellation (an outer cancel must stay a
cancel, and the lock + in-flight mark must be released), response-filter edge
shapes, the token store's UTC stamps, route failure paths and log hygiene,
supersede through the real route, sessions, the Helios sleep check's error path
and more of ``validate_coder_config``.

Unlike test_coder_gate.py, ``brain_slot.chat_busy`` is the real one here (with
the chat gauge and lookup counter held at zero), so interactive calls count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import brain_slot, coder_gate, shared, state_store, vision_handler
from orchestrator.config import settings

GATE = "10.213.250.2"
BOX = "10.213.250.140"
API_TOKEN = "test-api-token-0123456789"  # nosec B105 — test-only constant
MODEL_URL = "http://brain.test/v1"
IMG = "data:image/jpeg;base64,/9j/4AAQ"


@pytest.fixture(autouse=True)
def _gate_env(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "coder_gate_address", GATE)
    monkeypatch.setattr(settings, "coder_subnet", "10.213.250.0/24")
    monkeypatch.setattr(settings, "coder_idle_seconds", 0.0)
    monkeypatch.setattr(settings, "coder_max_wait_seconds", 2.0)
    monkeypatch.setattr(settings, "coder_session_hours", 4.0)
    monkeypatch.setattr(coder_gate, "_admitted", {})
    monkeypatch.setattr(coder_gate, "_bytes_seen", {})
    monkeypatch.setattr(coder_gate, "_rejections", {})
    monkeypatch.setattr(coder_gate, "PREEMPT_POLL_S", 0.01)
    monkeypatch.setattr(shared, "CODER_ENABLED", True)
    monkeypatch.setattr(shared, "MODEL_NAME", "brain-model")
    monkeypatch.setattr(shared, "MODEL_URL", MODEL_URL)
    monkeypatch.setattr(brain_slot, "_last_chat_activity", 0.0)
    monkeypatch.setattr(brain_slot, "_coder_in_flight", 0)
    monkeypatch.setattr(brain_slot, "_interactive_in_flight", 0)
    monkeypatch.setattr("orchestrator.research_helper._active", 0)
    gauge = MagicMock()
    gauge._value.get.return_value = 0
    monkeypatch.setattr(brain_slot, "ACTIVE_REQUESTS", gauge)


def _msgs(text="hi"):
    return [{"role": "user", "content": text}]


def _reply(text="done", usage=None):
    return (
        [{"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}],
        usage or {"prompt_tokens": 11, "completion_tokens": 3},
    )


async def _until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


def _set_column(token_id, column, value):
    assert column in {"created_at", "expires_at", "last_used_at"}
    with state_store.get_db() as conn:
        conn.execute(f"UPDATE coder_tokens SET {column} = ? WHERE id = ?", (value, token_id))  # nosec B608


def _utc(delta=timedelta(0)):
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


# ----------------------------------------------------------------- apps


@pytest.fixture
def app(monkeypatch):
    import orchestrator.orchestrator as orch
    from orchestrator.routes_coder import router

    monkeypatch.setattr(orch, "API_TOKEN", API_TOKEN)
    application = FastAPI()
    application.add_middleware(orch.BearerAuthMiddleware)
    application.include_router(router)

    @application.get("/api/private")
    async def private():
        return {"ok": True}

    return application


def _box(app, client_ip=BOX, server=GATE):
    return TestClient(app, base_url=f"http://{server}:8888", client=(client_ip, 50000))


def _lan(app, client_ip="10.0.0.50"):
    return TestClient(app, base_url="http://10.0.0.248:8888", client=(client_ip, 50000))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _session():
    return coder_gate.mint_token("session")


# ----------------------------------------------------------------- rate limit


@pytest.fixture
def rate_app(monkeypatch):
    """The real RateLimitMiddleware in front of a catch-all route."""
    import orchestrator.orchestrator as orch

    monkeypatch.setattr(orch, "RATE_LIMIT_MAX", 2)
    monkeypatch.setattr(orch, "CODER_RATE_LIMIT_MAX", 5)
    monkeypatch.setattr(orch, "_rate_limit_store", {})
    application = FastAPI()
    application.add_middleware(orch.RateLimitMiddleware)

    @application.api_route("/{rest:path}", methods=["GET", "POST"])
    async def served(rest: str):
        return {"ok": True}

    return application


def _codes(client, path, n):
    return [client.get(path).status_code for _ in range(n)]


class TestRateLimit:
    def test_box_gets_the_coder_limit(self, rate_app):
        assert _codes(_box(rate_app), coder_gate.GATE_MODELS_PATH, 6) == [200] * 5 + [429]

    def test_lan_keeps_the_low_limit(self, rate_app):
        assert _codes(_lan(rate_app), "/api/private", 3) == [200, 200, 429]

    @pytest.mark.parametrize(
        "client_ip,server",
        [(BOX, "172.26.0.2"), ("172.26.0.1", GATE)],
        ids=["subnet-client-other-addr", "gate-addr-outside-client"],
    )
    def test_half_match_counts_as_coder_net(self, rate_app, client_ip, server):
        """on_coder_net is the OR of the two checks, for the limit as for the guard."""
        assert _codes(_box(rate_app, client_ip, server), "/x", 6) == [200] * 5 + [429]

    def test_buckets_are_per_client(self, rate_app):
        assert _codes(_box(rate_app), "/x", 6)[-1] == 429
        assert _codes(_box(rate_app, client_ip="10.213.250.141"), "/x", 5) == [200] * 5
        assert _codes(_lan(rate_app), "/x", 3) == [200, 200, 429]

    def test_gate_off_does_not_lift_the_lan_limit(self, rate_app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        assert _codes(_lan(rate_app), "/x", 3) == [200, 200, 429]


# ----------------------------------------------------------------- vision + brain_slot


@pytest.fixture
def vision_shared():
    mock = MagicMock()
    mock.VISION_ENABLED = True
    mock.VISION_MODEL_URL = "http://vision.test/v1"
    mock.VISION_MODEL_NAME = "vision-model"
    mock.VISION_MAX_IMAGE_SIZE = 10 * 1024 * 1024
    mock.VISION_TIMEOUT = 60
    mock._http = None
    with patch("orchestrator.vision_handler.shared", mock):
        yield mock


def _vision_response(text="a cat on a chair"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": text}}]}
    return resp


class TestVisionCountsAsChat:
    async def test_post_runs_inside_interactive_call(self, vision_shared):
        seen = {}

        async def post(url, json=None, timeout=None):
            seen["in_flight"] = brain_slot._interactive_in_flight
            seen["busy"] = brain_slot.chat_busy()
            return _vision_response()

        client = MagicMock()
        client.post = post
        assert await vision_handler.analyze_image(IMG, "describe", http_client=client) == "a cat on a chair"
        assert seen == {"in_flight": 1, "busy": True}
        assert brain_slot._interactive_in_flight == 0 and not brain_slot.chat_busy()
        assert brain_slot.seconds_since_chat() < 5  # the coder's idle clock restarts after a photo

    async def test_failed_call_still_unmarks(self, vision_shared):
        async def post(url, json=None, timeout=None):
            assert brain_slot._interactive_in_flight == 1
            raise httpx.ReadTimeout("slow")

        client = MagicMock()
        client.post = post
        result = await vision_handler.analyze_image(IMG, "describe", http_client=client)
        assert "timed out" in result.lower()
        assert brain_slot._interactive_in_flight == 0 and not brain_slot.chat_busy()

    async def test_refused_image_never_touches_the_slot(self, vision_shared):
        client = MagicMock()
        result = await vision_handler.analyze_image("data:image/bmp;base64,AAAA", "describe", http_client=client)
        assert "unsupported" in result.lower()
        assert not client.post.called
        assert brain_slot.seconds_since_chat() == float("inf")  # no chat activity recorded

    async def test_photo_preempts_a_running_coder_attempt(self, vision_shared, monkeypatch):
        attempts = []

        async def stream(up):
            attempts.append(asyncio.current_task())
            if len(attempts) == 1:
                await asyncio.sleep(10)
            return _reply(f"attempt {len(attempts)}")

        real_turn = brain_slot.wait_for_coder_turn
        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", lambda idle, wait: real_turn(idle, wait, poll_s=0.01))
        photo_may_finish = asyncio.Event()

        async def post(url, json=None, timeout=None):
            await photo_may_finish.wait()
            return _vision_response()

        client = MagicMock()
        client.post = post
        coder = asyncio.create_task(coder_gate.run_chat({}))
        await _until(lambda: len(attempts) == 1)
        photo = asyncio.create_task(vision_handler.analyze_image(IMG, "describe", http_client=client))
        await _until(lambda: attempts[0].done())
        assert attempts[0].cancelled()  # the coder's connection was closed for the photo
        await asyncio.sleep(0.05)
        assert len(attempts) == 1  # and it is not re-sent while the photo is still running
        photo_may_finish.set()
        chunks, _ = await asyncio.wait_for(coder, 3)
        assert chunks[0]["choices"][0]["delta"]["content"] == "attempt 2"
        assert await photo == "a cat on a chair"
        assert not brain_slot.coder_busy()


# ----------------------------------------------------------------- startup_check


class TestStartupCheckLogs:
    @pytest.mark.parametrize(
        "enabled,local,level,text,still_on",
        [
            (False, True, logging.INFO, "guard active", False),
            (False, False, logging.WARNING, "not an address of this container", False),
            (True, False, logging.ERROR, "coder gate OFF", False),
            (True, True, logging.INFO, "Gate enabled", True),
        ],
        ids=["off-local", "off-not-local", "on-not-local", "on-local"],
    )
    def test_branches(self, monkeypatch, caplog, enabled, local, level, text, still_on):
        monkeypatch.setattr(shared, "CODER_ENABLED", enabled)
        monkeypatch.setattr(coder_gate, "_address_is_local", lambda address: local)
        with caplog.at_level(logging.INFO, logger="orchestrator.coder_gate"):
            coder_gate.startup_check()
        records = [r for r in caplog.records if r.name == "orchestrator.coder_gate"]
        assert len(records) == 1
        assert records[0].levelno == level and text in records[0].getMessage()
        assert shared.CODER_ENABLED is still_on

    @pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
    def test_empty_address_is_never_bound(self, monkeypatch, caplog, enabled):
        def must_not_bind(address):
            raise AssertionError("an empty address must not be probed")

        monkeypatch.setattr(shared, "CODER_ENABLED", enabled)
        monkeypatch.setattr(settings, "coder_gate_address", "")
        monkeypatch.setattr(coder_gate, "_address_is_local", must_not_bind)
        coder_gate.startup_check()
        assert shared.CODER_ENABLED is False

    def test_real_bind_probe(self):
        assert coder_gate._address_is_local("127.0.0.1")
        assert not coder_gate._address_is_local("192.0.2.77")  # TEST-NET-1, never ours


# ----------------------------------------------------------------- cancellation


class TestCancellation:
    async def test_outer_cancel_of_an_attempt_is_not_swallowed(self, monkeypatch):
        inner = {}

        async def stream(up):
            inner["task"] = asyncio.current_task()
            await asyncio.sleep(10)

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        task = asyncio.create_task(coder_gate._run_preemptable({}))
        await _until(lambda: "task" in inner)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done and task.cancelled()
        assert inner["task"].cancelled()  # the upstream attempt was closed too

    async def test_outer_cancel_survives_a_noisy_attempt(self, monkeypatch):
        """An attempt that raises on its way out (an httpx error while closing)
        must not turn the outer cancel into a normal return."""
        started = asyncio.Event()

        async def stream(up):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise httpx.ReadError("closed") from None

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        task = asyncio.create_task(coder_gate._run_preemptable({}))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done and task.cancelled()

    async def test_preemption_is_not_a_cancel(self, monkeypatch):
        inner = {}

        async def stream(up):
            inner["task"] = asyncio.current_task()
            await asyncio.sleep(10)

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        task = asyncio.create_task(coder_gate._run_preemptable({}))
        await _until(lambda: "task" in inner)
        brain_slot._interactive_in_flight = 1  # chat (here: a photo) starts
        with pytest.raises(coder_gate._Preempted):
            await asyncio.wait_for(task, 2)
        assert inner["task"].cancelled()

    async def test_cancelled_run_chat_releases_lock_and_slot(self, monkeypatch):
        calls = []

        async def stream(up):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(10)
            return _reply()

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        first = asyncio.create_task(coder_gate.run_chat({}))
        await _until(lambda: calls and brain_slot.coder_busy())
        first.cancel()
        done, _ = await asyncio.wait({first}, timeout=2)
        assert first in done and first.cancelled()
        assert not brain_slot.coder_busy() and not coder_gate._gate_lock().locked()
        chunks, _ = await asyncio.wait_for(coder_gate.run_chat({}), 2)
        assert chunks

    async def test_cancel_while_queued_for_the_lock(self, monkeypatch):
        async def stream(up):
            return _reply()

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        lock = coder_gate._gate_lock()
        await lock.acquire()
        queued = asyncio.create_task(coder_gate.run_chat({}))
        await asyncio.sleep(0.02)
        queued.cancel()
        done, _ = await asyncio.wait({queued}, timeout=2)
        assert queued in done and queued.cancelled()
        lock.release()
        assert not lock.locked()
        await asyncio.wait_for(coder_gate.run_chat({}), 2)

    async def _running_admission(self, monkeypatch):
        started = asyncio.Event()

        async def stream(up):
            started.set()
            await asyncio.sleep(10)

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        adm = coder_gate.admit("a" * 32)
        route = asyncio.create_task(adm.run({}))
        await asyncio.wait_for(started.wait(), 2)
        return adm, route

    async def test_route_cancel_stays_a_cancel(self, monkeypatch):
        adm, route = await self._running_admission(monkeypatch)
        route.cancel()
        done, _ = await asyncio.wait({route}, timeout=2)
        assert route in done and route.cancelled()  # not Superseded (that would be a 409 answer)
        assert adm._task.cancelled()
        assert not brain_slot.coder_busy() and not coder_gate._gate_lock().locked()
        adm.release()
        assert coder_gate.requests_admitted() == 0

    async def test_route_cancel_wins_over_a_simultaneous_supersede(self, monkeypatch):
        adm, route = await self._running_admission(monkeypatch)
        adm.supersede()
        route.cancel()
        done, _ = await asyncio.wait({route}, timeout=2)
        assert route in done and route.cancelled()
        adm.release()

    async def test_superseded_request_frees_the_lock_for_the_newer_one(self, monkeypatch):
        adm, route = await self._running_admission(monkeypatch)
        newer = coder_gate.admit("a" * 32)
        with pytest.raises(coder_gate.Superseded):
            await asyncio.wait_for(route, 2)

        async def fast(up):
            return _reply("newer")

        monkeypatch.setattr(coder_gate, "_stream_once", fast)
        chunks, _ = await asyncio.wait_for(newer.run({}), 2)
        assert chunks[0]["choices"][0]["delta"]["content"] == "newer"
        adm.release()
        assert coder_gate.requests_admitted() == 1  # the old release leaves the newer slot alone
        newer.release()
        assert coder_gate.requests_admitted() == 0


# ----------------------------------------------------------------- run_chat


class TestRunChatExtra:
    async def test_token_rechecked_after_a_preemption(self, monkeypatch):
        state = {"attempts": 0, "checks": 0}

        async def stream(up):
            state["attempts"] += 1
            brain_slot._interactive_in_flight = 1  # chat starts mid-attempt
            await asyncio.sleep(10)

        async def turn(idle_s, max_wait_s, poll_s=0.5):
            brain_slot._interactive_in_flight = 0
            return True

        async def still_usable():
            state["checks"] += 1
            return state["checks"] == 1  # revoked while the first attempt ran

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", turn)
        with pytest.raises(coder_gate.TokenRevoked):
            await asyncio.wait_for(coder_gate.run_chat({}, still_usable), 3)
        assert state == {"attempts": 1, "checks": 2}  # never re-sent for a dead token

    async def test_hold_budget_bounds_preempt_retries(self, monkeypatch):
        attempts = []

        async def stream(up):
            attempts.append(1)
            await asyncio.sleep(10)

        async def turn(idle_s, max_wait_s, poll_s=0.5):
            return True

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", turn)
        monkeypatch.setattr(brain_slot, "_interactive_in_flight", 1)  # chat busy for good
        monkeypatch.setattr(coder_gate, "MAX_PREEMPTS", 10_000)
        monkeypatch.setattr(coder_gate, "MAX_HOLD_S", 0.3)
        with pytest.raises(coder_gate.GateBusy):
            await asyncio.wait_for(coder_gate.run_chat({}), 3)
        assert len(attempts) >= 2 and not brain_slot.coder_busy()

    async def test_no_hold_budget_means_busy_without_the_brain(self, monkeypatch):
        async def stream(up):
            raise AssertionError("must not reach the brain")

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        monkeypatch.setattr(coder_gate, "MAX_HOLD_S", 0.0)
        with pytest.raises(coder_gate.GateBusy):
            await coder_gate.run_chat({})
        assert not coder_gate._gate_lock().locked()

    @pytest.mark.parametrize(
        "exc", [coder_gate.BrainUnavailable(), coder_gate.UpstreamError(500)], ids=["unavailable", "upstream"]
    )
    async def test_upstream_failures_are_not_retried(self, monkeypatch, exc):
        calls = []

        async def stream(up):
            calls.append(1)
            raise exc

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        with pytest.raises(type(exc)):
            await coder_gate.run_chat({})
        assert calls == [1] and not coder_gate._gate_lock().locked() and not brain_slot.coder_busy()


# ----------------------------------------------------------------- admission


class TestAdmissionExtra:
    def test_admitted_token_may_supersede_at_the_cap(self):
        held = [coder_gate.admit(f"{i:032x}") for i in range(coder_gate.MAX_ADMITTED_TOTAL)]
        newer = coder_gate.admit(f"{0:032x}")  # no free slot needed: it replaces its own
        assert held[0].superseded and coder_gate.requests_admitted() == coder_gate.MAX_ADMITTED_TOTAL
        for a in held + [newer]:
            a.release()
        assert coder_gate.requests_admitted() == 0

    def test_release_is_idempotent(self):
        adm = coder_gate.admit("e" * 32)
        adm.release()
        adm.release()
        assert coder_gate.requests_admitted() == 0


# ----------------------------------------------------------------- response filter


class TestResponseFilterExtra:
    def test_message_choices_are_cleaned(self):
        chunk = {
            "object": "chat.completion",
            "timings": {"cache_n": 4000},
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "logprobs": {"content": []},
                    "message": {
                        "role": "assistant",
                        "content": "x",
                        "reasoning_content": "r",
                        "timings": {"cache_n": 4000},
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                                "debug": {"slot": 0},
                            },
                            "junk",
                            7,
                        ],
                    },
                },
                "not-a-choice",
            ],
        }
        assert coder_gate.strip_chunk(chunk) == {
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "x",
                        "reasoning_content": "r",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "read_file", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ],
        }

    def test_tool_call_deltas_are_filtered(self):
        chunk = {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "{"}, "cache_n": 9}]}}]}
        out = coder_gate.strip_chunk(chunk)
        assert out["choices"][0]["delta"]["tool_calls"] == [{"index": 1, "function": {"arguments": "{"}}]

    @pytest.mark.parametrize(
        "chunk,expect",
        [
            ({"choices": None}, {"choices": []}),
            ({"choices": [{"delta": "text"}]}, {"choices": [{"delta": {}}]}),
            ({"usage": None, "id": "a"}, {"id": "a"}),
            ({"usage": {"prompt_tokens": 3, "extra": 1}}, {"usage": {"prompt_tokens": 3}}),
            ({}, {}),
        ],
        ids=["choices-none", "delta-not-dict", "usage-none", "usage-partial", "empty"],
    )
    def test_odd_shapes(self, chunk, expect):
        assert coder_gate.strip_chunk(chunk) == expect

    def test_assemble_with_no_chunks(self):
        out = coder_gate.assemble([], {"prompt_tokens": 4, "completion_tokens": 0})
        assert out["object"] == "chat.completion" and out["model"] == "brain-model"
        assert out["id"].startswith("coder-")
        assert out["choices"] == [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}]
        assert out["usage"] == {"prompt_tokens": 4, "completion_tokens": 0, "total_tokens": 4}

    def test_assemble_without_usage_keys(self):
        assert coder_gate.assemble([], {})["usage"] == {"total_tokens": 0}

    def test_assemble_orders_tool_calls_by_index(self):
        chunks = [
            {"choices": None},
            {"choices": [{"delta": None}]},
            {
                "choices": [
                    {"delta": {"content": None, "tool_calls": [{"index": 1, "id": "b", "function": {"name": "two"}}]}}
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"id": "a", "function": {"name": "one", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "ignored", "function": None}]}}]},
            {"choices": [{"finish_reason": "tool_calls"}]},
            {"choices": [{"finish_reason": None}]},  # a later None doesn't erase it
        ]
        msg = coder_gate.assemble(chunks, {"prompt_tokens": 0, "completion_tokens": 0})["choices"][0]
        assert msg["finish_reason"] == "tool_calls"
        calls = msg["message"]["tool_calls"]
        assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in calls] == [
            ("a", "one", "{}"),
            ("b", "two", ""),
        ]
        assert msg["message"]["content"] == ""


# ----------------------------------------------------------------- upstream


def _sse(*lines):
    return "".join(line + "\n\n" for line in lines)


@pytest.fixture
async def upstream(monkeypatch):
    client = httpx.AsyncClient()
    monkeypatch.setattr(shared, "_http", client)
    with respx.mock(assert_all_called=False) as router:
        yield router
    await client.aclose()


class TestStreamOnceExtra:
    async def test_noise_lines_skipped_and_done_ends_the_read(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                text=_sse(
                    ": keep-alive",
                    "data:",
                    "data: [1, 2]",
                    "event: message",
                    'data: {"choices": [{"delta": {"content": "a"}}], "usage": {"prompt_tokens": 1}}',
                    'data: {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}',
                    "data: [DONE]",
                    'data: {"choices": [{"delta": {"content": "after"}}]}',
                ),
            )
        )
        chunks, usage = await coder_gate._stream_once({"messages": []})
        assert len(chunks) == 2 and "after" not in str(chunks)
        assert usage == {"prompt_tokens": 9, "completion_tokens": 2}  # the last usage seen wins

    async def test_trailing_slash_in_model_url(self, upstream, monkeypatch):
        monkeypatch.setattr(shared, "MODEL_URL", MODEL_URL + "/")
        route = upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text=_sse("data: [DONE]"))
        )
        await coder_gate._stream_once({"messages": []})
        assert route.called

    @pytest.mark.parametrize(
        "text,status",
        [
            ("error:\n\n", 0),
            ('error: {"error": {"code": true, "type": "exceed_context_size_error"}}\n\n', 0),
            ('error: {"error": {"code": 413, "type": "something_else"}}\n\n', 413),
        ],
        ids=["empty", "bool-code", "wrapped"],
    )
    async def test_error_event_status(self, upstream, text, status):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(return_value=httpx.Response(200, text=text))
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.status == status
        assert exc.value.err_type == ("exceed_context_size_error" if "exceed" in text else None)

    async def test_bad_json_mid_stream(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text=_sse('data: {"choices": [', "data: [DONE]"))
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.status == 0

    async def test_non_json_error_body_keeps_status(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(return_value=httpx.Response(429, text="<html>busy</html>"))
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert (exc.value.status, exc.value.err_type) == (429, None)

    @pytest.mark.parametrize(
        "error,expect",
        [
            (httpx.ConnectTimeout("t"), coder_gate.BrainUnavailable),
            (httpx.ReadTimeout("t"), coder_gate.UpstreamError),
            (httpx.RemoteProtocolError("t"), coder_gate.UpstreamError),
        ],
        ids=["connect-timeout", "read-timeout", "protocol"],
    )
    async def test_transport_errors(self, upstream, error, expect):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(side_effect=error)
        with pytest.raises(expect):
            await coder_gate._stream_once({"messages": []})

    async def test_non_dict_choice_is_an_upstream_error(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text=_sse('data: {"choices": ["x"]}', "data: [DONE]"))
        )
        with pytest.raises(coder_gate.UpstreamError):
            await coder_gate._stream_once({"messages": []})


# ----------------------------------------------------------------- token store


class TestTokenStoreExtra:
    def test_stamps_are_utc(self):
        minted = _session()
        assert minted["expires_at"].endswith("+00:00")
        coder_gate.count_request(minted["id"])
        row = coder_gate.lookup_token(minted["token"])
        assert row["created_at"].endswith("+00:00") and row["last_used_at"].endswith("+00:00")

    def test_list_newest_first_and_by_kind(self):
        ids = [_session()["id"] for _ in range(3)]
        job = coder_gate.mint_token("job")
        for n, token_id in enumerate(ids):
            _set_column(token_id, "created_at", _utc(timedelta(minutes=n - 10)))
        assert [r["id"] for r in coder_gate.list_tokens("session")] == ids[::-1]
        assert [r["id"] for r in coder_gate.list_tokens("job")] == [job["id"]]

    def test_list_skips_expired_but_unrevoked(self):
        live, expired = _session(), _session()
        _set_column(expired["id"], "expires_at", _utc(timedelta(seconds=-1)))
        assert [r["id"] for r in coder_gate.list_tokens("session")] == [live["id"]]

    def test_active_use_window(self):
        minted = _session()
        _set_column(minted["id"], "last_used_at", _utc(timedelta(seconds=-(coder_gate.ACTIVE_WITHIN_S + 60))))
        assert not coder_gate.active_use_exists()
        assert coder_gate.active_use_exists(within_s=coder_gate.ACTIVE_WITHIN_S + 120)
        _set_column(minted["id"], "last_used_at", _utc())
        _set_column(minted["id"], "expires_at", _utc(timedelta(seconds=-1)))
        assert not coder_gate.active_use_exists()  # used just now, but expired

    @pytest.mark.parametrize("expires", ["2099-01-01T00:00:00", "never", ""], ids=["naive", "garbage", "empty"])
    def test_unreadable_expiry_fails_closed(self, expires):
        minted = _session()
        _set_column(minted["id"], "expires_at", expires)
        assert coder_gate.lookup_token(minted["token"]) is None
        assert not coder_gate.token_still_usable(minted["id"])
        assert coder_gate.list_tokens("session") == []

    @pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf")], ids=["zero", "negative", "nan", "inf"])
    def test_mint_refuses_bad_hours(self, hours):
        with pytest.raises(ValueError):
            coder_gate.mint_token("session", hours=hours)

    def test_mint_refuses_unknown_kind(self):
        with pytest.raises(ValueError):
            coder_gate.mint_token("admin")

    def test_job_tokens(self):
        minted = coder_gate.mint_token("job", "nightly", max_requests=5)
        assert minted["token"].startswith("jcj_")
        row = coder_gate.lookup_token(minted["token"])
        assert row["kind"] == "job" and row["max_requests"] == 5
        assert coder_gate.revoke_token(minted["id"])  # no kind: any kind

    def test_overlong_prefixed_token_short_circuits(self, monkeypatch):
        def no_db():
            raise AssertionError("must not query")

        monkeypatch.setattr(state_store, "get_db", no_db)
        assert coder_gate.lookup_token("jcs_" + "a" * 300) is None

    def test_negative_usage_never_lowers_the_count(self):
        minted = _session()
        coder_gate.record_usage(minted["id"], 50, 5)
        coder_gate.record_usage(minted["id"], -1000, -1000)
        row = coder_gate.lookup_token(minted["token"])
        assert (row["prompt_tokens"], row["completion_tokens"]) == (50, 5)

    @pytest.mark.parametrize(
        "token_id", [None, "", "A" * 32, "0" * 31, "0" * 33], ids=["none", "empty", "upper", "short", "long"]
    )
    def test_token_id_shape(self, token_id):
        assert not coder_gate.valid_token_id(token_id)


class TestAbuseLimits:
    def test_byte_window_slides(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(coder_gate, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
        monkeypatch.setattr(coder_gate, "MAX_BYTES_PER_MINUTE", 1000)
        coder_gate.note_bytes("t", 700)
        assert coder_gate.bytes_allowance("t") == 300
        coder_gate.note_bytes("t", 700)
        assert coder_gate.bytes_allowance("t") == 0  # never negative
        assert coder_gate.bytes_allowance("other") == 1000  # per token
        clock["now"] += 61
        assert coder_gate.bytes_allowance("t") == 1000

    def test_rejections_per_token_and_reset_after_revoke(self, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_REJECTIONS", 2)
        a, b = _session(), _session()
        assert not coder_gate.note_rejection(a["id"])
        assert not coder_gate.note_rejection(b["id"])
        assert coder_gate.note_rejection(a["id"])
        assert coder_gate.lookup_token(a["token"]) is None and coder_gate.lookup_token(b["token"]) is not None
        assert a["id"] not in coder_gate._rejections

    @pytest.mark.parametrize(
        "key,label",
        [
            ("grammar", "grammar"),
            ("non_text_part", "non_text_part"),
            ("x" * 500, "other"),
            ("__proto__", "other"),
        ],
        ids=["known", "non-text", "long", "unknown"],
    )
    def test_dropped_key_labels_are_bounded(self, key, label):
        assert coder_gate.dropped_key_label(key) == label


# ----------------------------------------------------------------- request filter


class TestSanitizeExtra:
    def test_tool_choice_without_tools_is_not_forwarded(self):
        up, dropped = coder_gate.sanitize_request({"messages": _msgs(), "tool_choice": "required"}, 4096)
        assert "tool_choice" not in up and "tool_choice" not in dropped

    def test_named_tool_choice_is_rebuilt(self):
        choice = {"type": "function", "function": {"name": "read_file", "extra": 1}, "x": 2}
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool()], "tool_choice": choice}, 4096)
        assert up["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}

    @pytest.mark.parametrize(
        "body,key,expect",
        [
            ({"max_tokens": 100.9}, "max_tokens", 100),
            ({"max_tokens": 50, "max_completion_tokens": 999}, "max_tokens", 50),
            ({"top_p": 0}, "top_p", 0.01),
            ({"temperature": 0.25}, "temperature", 0.25),
        ],
        ids=["float-max", "max-tokens-wins", "top-p-floor", "temp-kept"],
    )
    def test_numbers(self, body, key, expect):
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), **body}, 4096)
        assert up[key] == expect

    def test_message_cap(self, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_MESSAGES", 3)
        coder_gate.sanitize_request({"messages": _msgs() * 3}, 4096)
        with pytest.raises(coder_gate.GateRejected, match="bad_messages"):
            coder_gate.sanitize_request({"messages": _msgs() * 4}, 4096)

    def test_message_fields(self):
        msgs = [
            {"role": "user", "content": None, "name": "me", "extra": "dropped"},
            {"role": "assistant", "content": None},
            {"role": "assistant", "content": "x", "tool_calls": []},
        ]
        up, _ = coder_gate.sanitize_request({"messages": msgs}, 4096)
        assert up["messages"] == [
            {"role": "user", "content": "", "name": "me"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "x"},
        ]

    @pytest.mark.parametrize(
        "message",
        [
            {"role": "user", "content": "x", "name": 5},
            {"role": "assistant", "content": "x", "reasoning_content": ["r"]},
            {"role": "assistant", "content": None, "tool_calls": {"function": {"name": "a"}}},
            {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "a", "arguments": {}}}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": 5, "function": {"name": "a"}}]},
            {"role": "assistant", "content": None, "tool_calls": ["call"]},
            "not-a-message",
        ],
        ids=["name-int", "reasoning-list", "calls-dict", "args-dict", "id-int", "call-str", "message-str"],
    )
    def test_bad_message_fields(self, message):
        with pytest.raises(coder_gate.GateRejected, match="bad_messages"):
            coder_gate.sanitize_request({"messages": [message]}, 4096)

    def test_too_many_tool_calls(self):
        call = {"id": "c", "function": {"name": "a", "arguments": "{}"}}
        msgs = [
            {"role": "assistant", "content": None, "tool_calls": [call] * (coder_gate.MAX_TOOL_CALLS_PER_MESSAGE + 1)}
        ]
        with pytest.raises(coder_gate.GateRejected, match="bad_messages"):
            coder_gate.sanitize_request({"messages": msgs}, 4096)

    def test_argument_depth_boundary(self):
        def nested(levels):  # `levels` dicts around a scalar
            return '{"a": ' * levels + "1" + "}" * levels

        ok, dropped = [], []
        coder_gate._clean_arguments(nested(coder_gate.MAX_ARGUMENTS_DEPTH - 1), ok)
        coder_gate._clean_arguments(nested(coder_gate.MAX_ARGUMENTS_DEPTH), dropped)
        assert ok == [] and dropped == ["bad_tool_arguments"]

    def test_argument_length_boundary(self, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_ARGUMENTS_CHARS", 20)
        dropped: list = []
        assert coder_gate._clean_arguments('{"a": "' + "x" * 11 + '"}', dropped) != "{}"  # exactly 20
        assert coder_gate._clean_arguments('{"a": "' + "x" * 12 + '"}', dropped) == "{}"
        assert dropped == ["bad_tool_arguments"]

    @pytest.mark.parametrize("stop", [5, "", {"a": 1}, [1]], ids=["int", "empty-str", "dict", "list-int"])
    def test_bad_stop(self, stop):
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request({"messages": _msgs(), "stop": stop}, 4096)


def _tool(name="read_file", params=None, **fn):
    function = {"name": name, "parameters": params or {"type": "object"}, **fn}
    return {"type": "function", "function": function}


def _prop(**node):
    return {"type": "object", "properties": {"v": node}}


class TestSchemaExtra:
    def test_depth_boundary_passes(self):
        node: dict = {"type": "object"}
        root = node
        for _ in range(coder_gate.MAX_SCHEMA_DEPTH):
            child: dict = {"type": "object"}
            node["properties"] = {"x": child}
            node = child
        coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool(params=root)]}, 4096)

    def test_bound_boundary_passes(self):
        params = _prop(type="string", maxLength=coder_gate.MAX_SCHEMA_BOUND, minLength=0)
        coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool(params=params)]}, 4096)

    def test_missing_parameters_default_to_an_empty_object(self):
        tool = {"type": "function", "function": {"name": "ping"}}
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), "tools": [tool]}, 4096)
        assert up["tools"][0]["function"] == {
            "name": "ping",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        }

    def test_unknown_function_keys_dropped(self):
        up, _ = coder_gate.sanitize_request(
            {"messages": _msgs(), "tools": [_tool(strict=True, grammar="root ::= x")]}, 4096
        )
        assert set(up["tools"][0]["function"]) == {"name", "description", "parameters"}

    @pytest.mark.parametrize(
        "params,reason",
        [
            (_prop(type="string", maxLength=coder_gate.MAX_SCHEMA_BOUND + 1), "schema_bounds"),
            (_prop(type="string", minLength=-1), "schema_bounds"),
            (_prop(type="array", items={"type": "string"}, maxItems=True), "schema_bounds"),
            (_prop(type="string", maxLength=5.0), "schema_bounds"),
            (_prop(type="number", minimum=float("nan")), "schema_bounds"),
            (_prop(type="number", maximum=True), "schema_bounds"),
            (_prop(type="string", enum=[]), "schema_bounds"),
            (_prop(type="string", enum=[{"a": 1}]), "schema_bounds"),
            (_prop(type="string", enum="a"), "schema_bounds"),
            (_prop(type=[]), "schema_keyword"),
            (_prop(type=["string", "date"]), "schema_keyword"),
            (_prop(type="string", default=["a"]), "schema_keyword"),
            (_prop(type="string", description=5), "schema_keyword"),
            (_prop(type="string", title=None), "schema_keyword"),
            ({"type": "object", "properties": [], "required": []}, "schema_keyword"),
            ({"type": "object", "properties": {"": {"type": "string"}}}, "schema_keyword"),
            ({"type": "object", "properties": {"x" * 65: {"type": "string"}}}, "schema_keyword"),
            ({"type": "object", "required": "x"}, "schema_keyword"),
            ({"type": "object", "required": [1]}, "schema_keyword"),
            ({"type": "object", "additionalProperties": "no"}, "schema_keyword"),
            ({"type": "object", "properties": {"v": "string"}}, "schema_keyword"),
        ],
        ids=[
            "bound-over",
            "bound-negative",
            "bound-bool",
            "bound-float",
            "minimum-nan",
            "maximum-bool",
            "enum-empty",
            "enum-dict",
            "enum-str",
            "type-empty",
            "type-unknown",
            "default-list",
            "description-int",
            "title-none",
            "properties-list",
            "prop-name-empty",
            "prop-name-long",
            "required-str",
            "required-int",
            "additional-str",
            "child-not-dict",
        ],
    )
    def test_refused(self, params, reason):
        with pytest.raises(coder_gate.GateRejected) as exc:
            coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool(params=params)]}, 4096)
        assert exc.value.reason == reason

    @pytest.mark.parametrize(
        "tool",
        [
            {"type": "function", "function": {"name": "a", "description": 5}},
            {"type": "function", "function": {"name": "a", "parameters": "object"}},
            {"type": "function", "function": "a"},
            "a",
        ],
        ids=["description-int", "parameters-str", "function-str", "tool-str"],
    )
    def test_bad_tool_shapes(self, tool):
        with pytest.raises(coder_gate.GateRejected, match="bad_tools"):
            coder_gate.sanitize_request({"messages": _msgs(), "tools": [tool]}, 4096)


# ----------------------------------------------------------------- routes


def _post(client, token, body=None, **kw):
    return client.post(coder_gate.GATE_CHAT_PATH, headers=_auth(token), json=body or {"messages": _msgs()}, **kw)


def _stub_run(monkeypatch, usage=None):
    async def fake(up, still_usable=None):
        return _reply(usage=usage)

    monkeypatch.setattr(coder_gate, "run_chat", fake)


class TestRouteExtra:
    def test_count_failure_is_logged_and_request_served(self, app, monkeypatch, caplog):
        _stub_run(monkeypatch)

        def locked(token_id):
            raise sqlite3.OperationalError("database is locked near SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "count_request", locked)
        with caplog.at_level(logging.ERROR, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session()["token"])
        assert r.status_code == 200
        lines = [rec.getMessage() for rec in caplog.records if "not counted" in rec.getMessage()]
        assert len(lines) == 1 and "OperationalError" in lines[0]
        assert "SECRET-CONTENT" not in caplog.text

    def test_internal_error_log_has_no_exception_text(self, app, monkeypatch, caplog):
        async def boom(up, still_usable=None):
            raise KeyError("prompt text SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "run_chat", boom)
        with caplog.at_level(logging.INFO, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session()["token"], {"messages": _msgs("SECRET-CONTENT")})
        assert r.status_code == 502 and "SECRET-CONTENT" not in r.text
        assert "KeyError" in caplog.text and "test_coder_gate_extra.py:" in caplog.text
        assert "SECRET-CONTENT" not in caplog.text

    def test_refusal_log_and_answer_carry_no_content(self, app, caplog):
        with caplog.at_level(logging.INFO, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session()["token"], {"messages": [{"role": "SECRET-ROLE", "content": "x"}]})
        assert r.status_code == 400 and "SECRET-ROLE" not in r.text and "SECRET-ROLE" not in caplog.text

    def test_revoked_after_admission_never_reaches_the_brain(self, app, monkeypatch):
        """The route wires the token re-check into run_chat: revoke the token
        right after it was counted (a DELETE while it waits) and the brain
        never sees the request."""
        real_count = coder_gate.count_request

        def count_then_revoke(token_id):
            real_count(token_id)
            coder_gate.revoke_token(token_id)

        async def stream(up):
            raise AssertionError("must not reach the brain")

        monkeypatch.setattr(coder_gate, "count_request", count_then_revoke)
        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        r = _post(_box(app), _session()["token"])
        assert r.status_code == 401 and "revoked" in r.json()["error"]["message"]

    def test_last_request_of_the_budget_is_served(self, app, monkeypatch):
        async def stream(up):
            return _reply()

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        minted = coder_gate.mint_token("session", max_requests=2)
        codes = [_post(_box(app), minted["token"]).status_code for _ in range(3)]
        assert codes == [200, 200, 403]

    def test_real_run_chat_through_the_route(self, app, monkeypatch):
        async def stream(up):
            assert brain_slot.coder_busy()
            return _reply("hello", {"prompt_tokens": 20, "completion_tokens": 2})

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        minted = _session()
        r = _post(_box(app), minted["token"])
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hello"
        row = coder_gate.lookup_token(minted["token"])
        assert (row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (1, 20, 2)
        assert coder_gate.requests_admitted() == 0 and not brain_slot.coder_busy()

    def test_odd_upstream_usage_still_answers(self, app, monkeypatch):
        """Usage numbers come from llama-server; a negative one must not make
        the route throw away a finished reply (Counter.inc refuses negatives)."""
        monkeypatch.setattr(shared, "_http", httpx.AsyncClient())
        body = _sse(
            'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],'
            ' "usage": {"prompt_tokens": -5, "completion_tokens": 1}}',
            "data: [DONE]",
        )
        client = TestClient(app, base_url=f"http://{GATE}:8888", client=(BOX, 50000), raise_server_exceptions=False)
        with respx.mock(assert_all_called=False) as router:
            router.post(f"{MODEL_URL}/chat/completions").mock(return_value=httpx.Response(200, text=body))
            r = _post(client, _session()["token"])
        assert r.status_code == 200

    def test_chunked_body_over_the_cap(self, app, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_BODY_BYTES", 100)

        def chunks():
            for _ in range(10):
                yield b"x" * 50

        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=_auth(_session()["token"]), content=chunks())
        assert r.status_code == 413

    def test_empty_body_is_bad_json(self, app):
        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=_auth(_session()["token"]), content=b"")
        assert r.status_code == 400 and "bad_json" in r.json()["error"]["message"]

    def test_non_boolean_stream_gets_json(self, app, monkeypatch):
        _stub_run(monkeypatch)
        r = _post(_box(app), _session()["token"], {"messages": _msgs(), "stream": "true"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER"], ids=["lower", "upper"])
    def test_bearer_scheme_case_and_spaces(self, app, monkeypatch, scheme):
        _stub_run(monkeypatch)
        token = _session()["token"]
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"{scheme}   {token} "}, json={"messages": _msgs()}
        )
        assert r.status_code == 200

    def test_models_disabled(self, app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        assert _box(app).get(coder_gate.GATE_MODELS_PATH, headers=_auth(_session()["token"])).status_code == 404

    def test_where_without_traceback(self):
        from orchestrator.routes_coder import _where

        assert _where(KeyError("x")) == "?"

    async def test_newer_request_supersedes_through_the_route(self, app, monkeypatch):
        calls = []
        first_started = asyncio.Event()

        async def fake(up, still_usable=None):
            calls.append(up)
            if len(calls) == 1:
                first_started.set()
                await asyncio.sleep(10)
            return _reply("second")

        monkeypatch.setattr(coder_gate, "run_chat", fake)
        minted = _session()
        transport = httpx.ASGITransport(app=app, client=(BOX, 50000))
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{GATE}:8888") as client:
            first = asyncio.create_task(_post_async(client, minted["token"]))
            await asyncio.wait_for(first_started.wait(), 5)
            second = await asyncio.wait_for(_post_async(client, minted["token"]), 5)
            older = await asyncio.wait_for(first, 5)
        assert older.status_code == 409 and older.json()["error"]["type"] == "superseded"
        assert second.status_code == 200 and second.json()["choices"][0]["message"]["content"] == "second"
        assert coder_gate.requests_admitted() == 0
        assert coder_gate.lookup_token(minted["token"])["requests"] == 2


async def _post_async(client, token):
    return await client.post(coder_gate.GATE_CHAT_PATH, headers=_auth(token), json={"messages": _msgs()})


class TestGuardExtra:
    @pytest.mark.parametrize(
        "path",
        ["/api/audio/clip.wav", "/api/reminder/ack/abc", "/api/coder/sessions/" + "0" * 32, "/metrics"],
        ids=["audio", "reminder-ack", "session-delete", "metrics"],
    )
    def test_public_paths_closed_from_coder_net(self, app, path):
        assert _box(app).get(path).status_code == 403

    def test_guard_stays_on_with_the_gate_off(self, app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        assert _box(app).get("/api/private", headers=_auth(API_TOKEN)).status_code == 403

    def test_blocked_requests_are_counted(self, app):
        from orchestrator.metrics import CODER_GUARD_BLOCKED_TOTAL

        before = CODER_GUARD_BLOCKED_TOTAL._value.get()
        _box(app).get("/api/private")
        _box(app).get(coder_gate.GATE_MODELS_PATH)  # the gate itself is not "blocked"
        assert CODER_GUARD_BLOCKED_TOTAL._value.get() == before + 1

    def test_newline_suffix_from_lan_does_not_reach_the_gate(self, app, monkeypatch):
        """'/…/completions\\n' is not in GATE_PATHS, so the middleware doesn't
        404 it, but the router's '$' still matches it: the route's own
        arrived_on_gate check must refuse it."""
        called = []

        async def fake(up, still_usable=None):
            called.append(1)
            return _reply()

        monkeypatch.setattr(coder_gate, "run_chat", fake)
        token = _session()["token"]
        for auth in (API_TOKEN, token):
            r = _lan(app).post(coder_gate.GATE_CHAT_PATH + "%0A", headers=_auth(auth), json={"messages": _msgs()})
            assert r.status_code in (401, 404)
        assert not called


class TestSessionsExtra:
    def _lan_post(self, app, body=None, raw=None):
        headers = _auth(API_TOKEN)
        if raw is not None:
            return _lan(app).post("/api/coder/sessions", headers=headers, content=raw)
        return _lan(app).post("/api/coder/sessions", headers=headers, json=body)

    def test_tiny_hours_clamped_up(self, app):
        r = self._lan_post(app, {"hours": 0.01})
        expires = datetime.fromisoformat(r.json()["expires_at"])
        now = datetime.now(UTC)
        assert now + timedelta(minutes=14) <= expires <= now + timedelta(minutes=16)

    def test_defaults(self, app):
        r = self._lan_post(app, raw=b"")
        assert r.status_code == 201 and r.json()["label"] == ""
        expires = datetime.fromisoformat(r.json()["expires_at"])
        assert expires >= datetime.now(UTC) + timedelta(hours=3, minutes=59)

    @pytest.mark.parametrize(
        "body",
        [{"label": "a", "role": "admin"}, {"hours": 0}, {"hours": -1}, {"hours": 1001}, {"label": 5}, "x"],
        ids=["extra-key", "zero", "negative", "too-many", "label-int", "string"],
    )
    def test_refused(self, app, body):
        assert self._lan_post(app, body).status_code == 400

    def test_session_routes_ignore_job_tokens(self, app):
        job = coder_gate.mint_token("job")
        assert _lan(app).get("/api/coder/sessions", headers=_auth(API_TOKEN)).json()["sessions"] == []
        assert _lan(app).delete(f"/api/coder/sessions/{job['id']}", headers=_auth(API_TOKEN)).status_code == 404
        assert coder_gate.lookup_token(job["token"]) is not None

    def test_box_cannot_list_or_revoke(self, app):
        minted = _session()
        assert _box(app).get("/api/coder/sessions", headers=_auth(API_TOKEN)).status_code == 403
        r = _box(app).delete(f"/api/coder/sessions/{minted['id']}", headers=_auth(API_TOKEN))
        assert r.status_code == 403
        assert coder_gate.lookup_token(minted["token"]) is not None

    def test_revoke_disabled(self, app, monkeypatch):
        minted = _session()
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        r = _lan(app).delete(f"/api/coder/sessions/{minted['id']}", headers=_auth(API_TOKEN))
        assert r.status_code == 404
        assert coder_gate.lookup_token(minted["token"]) is not None


# ----------------------------------------------------------------- Helios sleep check


class TestCoderUsingBrain:
    async def test_lookup_error_allows_sleep(self, monkeypatch, caplog):
        from orchestrator import helios_power

        def broken(within_s=coder_gate.ACTIVE_WITHIN_S):
            raise sqlite3.OperationalError("locked SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "active_use_exists", broken)
        with caplog.at_level(logging.WARNING, logger="orchestrator.helios_power"):
            assert await helios_power._coder_using_brain() is False
        assert "OperationalError" in caplog.text and "SECRET-CONTENT" not in caplog.text

    async def test_gate_off_ignores_everything(self, monkeypatch):
        from orchestrator import helios_power

        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        adm = coder_gate.admit("f" * 32)
        try:
            with brain_slot.coder_request():
                assert await helios_power._coder_using_brain() is False
        finally:
            adm.release()

    @pytest.mark.parametrize("what", ["in_flight", "admitted", "recent"], ids=["in-flight", "admitted", "recent"])
    async def test_each_signal_blocks(self, monkeypatch, what):
        from orchestrator import helios_power

        if what == "in_flight":
            monkeypatch.setattr(brain_slot, "_coder_in_flight", 1)
        elif what == "admitted":
            monkeypatch.setattr(coder_gate, "_admitted", {"a" * 32: object()})
        else:
            coder_gate.count_request(_session()["id"])
        assert await helios_power._coder_using_brain() is True


# ----------------------------------------------------------------- config


class TestConfigExtra:
    def _settings(self, monkeypatch, **env):
        from orchestrator.config import Settings

        for key in [k for k in os.environ if k.startswith("CODER_")]:
            monkeypatch.delenv(key)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings(_env_file=None)

    def test_whitespace_is_normalized(self, monkeypatch):
        s = self._settings(
            monkeypatch,
            CODER_ENABLED="true",
            CODER_SUBNET=" 10.77.0.0/24 ",
            CODER_GATE_ADDRESS=" 10.77.0.2 ",
            CODER_BOX_RANGE=" 10.77.0.128/25 ",
        )
        assert s.coder_enabled is True
        assert (s.coder_subnet, s.coder_gate_address) == ("10.77.0.0/24", "10.77.0.2")

    def test_broadcast_gate_address_disables(self, monkeypatch):
        s = self._settings(monkeypatch, CODER_ENABLED="true", CODER_GATE_ADDRESS="10.213.250.255")
        assert s.coder_enabled is False and s.coder_gate_address == "10.213.250.255"

    def test_box_range_outside_subnet_with_gate_off_keeps_guard(self, monkeypatch):
        s = self._settings(monkeypatch, CODER_BOX_RANGE="10.9.0.0/25")
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == ("10.213.250.0/24", "10.213.250.2")

    def test_integer_clamps_keep_their_type(self, monkeypatch):
        s = self._settings(
            monkeypatch,
            CODER_MAX_TOKENS_PER_REQUEST="10",
            CODER_SESSION_MAX_REQUESTS="0",
            CODER_SESSION_MAX_TOKENS="5",
            CODER_MAX_WAIT_SECONDS="10000",
            CODER_IDLE_SECONDS="10000",
            CODER_SESSION_HOURS="0.01",
        )
        assert (s.coder_max_tokens_per_request, s.coder_session_max_requests, s.coder_session_max_tokens) == (
            256,
            1,
            10_000,
        )
        assert all(
            type(v) is int
            for v in (s.coder_max_tokens_per_request, s.coder_session_max_requests, s.coder_session_max_tokens)
        )
        assert (s.coder_max_wait_seconds, s.coder_idle_seconds, s.coder_session_hours) == (240.0, 300.0, 0.25)

    def test_bad_layout_is_logged(self, monkeypatch, caplog):
        with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            self._settings(monkeypatch, CODER_ENABLED="true", CODER_GATE_ADDRESS="8.8.8.8")
        assert "coder gate disabled" in caplog.text
