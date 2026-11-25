"""
Chat-turn latency and the active-request gauge on real-streamed turns.

- _stream_loop_response: a real-streamed turn counts in bgw_active_requests
  from the stream's first chunk until it ends, and records exactly one
  bgw_request_duration_seconds{mode="unified"} sample timed from started_at
  (from the stream's own start when started_at is None) — on every exit path:
  answered, loop failure, fallback model, client aclose(), consumer task
  cancelled, a tool that turns its cancellation into another error. A body
  that is built but never started records nothing and leaves the gauge alone.
  Voice turns record bgw_voice_pipeline_seconds{path="streamed"} at the same
  moment (when the stream ends, not when the StreamingResponse is returned),
  and every answered turn one bgw_chat_ttft_seconds sample — once, not once
  per model, on the fallback path too.
- _chat_unified: always counts the turn while the inner flow runs, but only
  observes the latency (and the buffered voice latency) when the turn did NOT
  become a real stream (that one is observed by the stream); it hands its
  start time to the inner flow; the exception path still decrements, observes
  once and counts bgw_request_errors_total. Through the real
  _chat_unified_inner a streamed turn is never double-counted.
- _unified_fallback forwards is_voice to the loop (default False), from both
  of _chat_unified_inner's fallback paths.
- REQUEST_LATENCY keeps 10 s and 30 s bucket edges (Grafana thresholds and
  HighRequestLatency read quantiles at those values).

Metrics are process-global: every check is a delta against a baseline read
in the same test. No model, network or HA traffic — loop functions are stubs.
"""

import asyncio
import gc
import time

import pytest

from orchestrator.metrics import (
    ACTIVE_REQUESTS,
    CHAT_TTFT,
    REQUEST_ERRORS,
    REQUEST_LATENCY,
    VOICE_PIPELINE_LATENCY,
)
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_loop import drain, make_brain, stream_brain, stream_response

MESSAGES = [{"role": "user", "content": "hi"}]


def _count():
    return metric_value(REQUEST_LATENCY, "_count", mode="unified")


def _sum():
    return metric_value(REQUEST_LATENCY, "_sum", mode="unified")


def _bucket(le):
    return metric_value(REQUEST_LATENCY, "_bucket", mode="unified", le=le)


def _voice(path="streamed", sample="_count"):
    return metric_value(VOICE_PIPELINE_LATENCY, sample, path=path)


def _ttft(is_voice):
    return metric_value(CHAT_TTFT, "_count", voice="true" if is_voice else "false")


def _loop_errors():
    return metric_value(REQUEST_ERRORS, mode="unified", error_type="tool_loop_failed")


def _active():
    return metric_value(ACTIVE_REQUESTS)


class _Clock:
    """Stands in for cloud_brain's `time` module: time.time() is under test control."""

    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now


async def _hang_in_tool(cancelled: asyncio.Event):
    try:
        await asyncio.Event().wait()  # a tool (e.g. web_search) that never returns
    except asyncio.CancelledError:
        cancelled.set()
        raise


# ---------------------------------------------------------------------------
# _stream_loop_response: gauge + one latency sample per streamed turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_voice", [False, True])
async def test_streamed_turn_counts_active_until_stream_ends(is_voice):
    in_loop = {}

    async def loop_fn(**kw):
        await kw["on_delta"]("Hello")
        in_loop["active"] = _active()
        await asyncio.sleep(0)  # a tool round
        return "Hello"

    base, count0, sum0 = _active(), _count(), _sum()
    voice0, voice_sum0, ttft0 = _voice(), _voice(sample="_sum"), _ttft(is_voice)
    resp = stream_response(stream_brain(loop_fn), is_voice, started_at=time.time() - 2.0)
    body = resp.body_iterator

    # Built, not started: nothing counted, nothing recorded.
    assert _active() == base
    assert _count() == count0
    assert _voice() == voice0

    first = await body.__anext__()
    assert "Hello" in first
    assert _active() == base + 1
    assert _count() == count0  # still running

    rest = [chunk async for chunk in body]
    assert rest[-1] == "data: [DONE]\n\n"
    assert in_loop["active"] == base + 1
    assert _active() == base
    assert _count() == count0 + 1
    assert _sum() - sum0 >= 2.0
    assert _voice() == voice0 + (1 if is_voice else 0)
    if is_voice:
        assert _voice(sample="_sum") - voice_sum0 >= 2.0
    assert _ttft(is_voice) == ttft0 + 1


@pytest.mark.parametrize("started_offset, expected", [(10.0, 12.0), (None, 2.0)])
async def test_streamed_turn_latency_is_whole_turn_from_started_at(monkeypatch, started_offset, expected):
    """12 s from the request reaching _chat_unified (10 s before the stream
    started) to the end of the stream; without started_at the stream's own
    start is the origin. The request and voice histograms get the same value,
    and a 12 s turn lands in the new 13 s bucket, not in 10 s."""
    from orchestrator import cloud_brain

    clock = _Clock(1000.0)
    monkeypatch.setattr(cloud_brain, "time", clock)

    async def loop_fn(**kw):
        await kw["on_delta"]("Hello")
        clock.now += 2.0  # tool rounds + decoding
        return "Hello"

    started_at = None if started_offset is None else clock.now - started_offset
    sum0, voice_sum0 = _sum(), _voice(sample="_sum")
    le10, le13, le3 = _bucket("10.0"), _bucket("13.0"), _bucket("3.0")

    await drain(stream_response(stream_brain(loop_fn), True, started_at=started_at))

    assert _sum() - sum0 == pytest.approx(expected)
    assert _voice(sample="_sum") - voice_sum0 == pytest.approx(expected)
    le10_d, le13_d, le3_d = _bucket("10.0") - le10, _bucket("13.0") - le13, _bucket("3.0") - le3
    if expected == 12.0:
        assert (le10_d, le13_d) == (0, 1)
    else:
        assert (le3_d, le10_d) == (1, 1)


@pytest.mark.parametrize("is_voice", [False, True])
async def test_streamed_loop_failure_without_fallback_records_once(is_voice):
    async def loop_fn(**kw):
        raise RuntimeError("boom")

    base, count0, voice0, errors0 = _active(), _count(), _voice(), _loop_errors()
    body = await drain(stream_response(stream_brain(loop_fn), is_voice, started_at=time.time()))
    assert "Sorry, something went wrong" in body
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice() == voice0 + (1 if is_voice else 0)
    assert _loop_errors() == errors0 + 1


@pytest.mark.parametrize("is_voice", [False, True])
@pytest.mark.parametrize("fallback_ok", [True, False])
async def test_streamed_fallback_model_records_once(fallback_ok, is_voice):
    """Primary loop dies before emitting; the fallback model runs inside the
    same producer. One sample whether the fallback answers or fails too, and
    one TTFT when it answers — not one per model."""

    async def loop_fn(**kw):
        if kw.get("on_delta") is not None:
            raise RuntimeError("primary died")
        if not fallback_ok:
            raise RuntimeError("fallback died")
        return "Fallback answer"

    base, count0, voice0, ttft0 = _active(), _count(), _voice(), _ttft(is_voice)
    brain = stream_brain(loop_fn, fallback_url="http://fb.invalid")
    body = await drain(stream_response(brain, is_voice, started_at=time.time()))
    assert ("Fallback answer" in body) is fallback_ok
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice() == voice0 + (1 if is_voice else 0)
    if fallback_ok:
        assert _ttft(is_voice) == ttft0 + 1


@pytest.mark.parametrize("is_voice", [False, True])
async def test_client_aclose_mid_tool_cancels_producer_and_records_once(is_voice):
    tool_cancelled = asyncio.Event()

    async def loop_fn(**kw):
        await kw["on_delta"]("Hello")
        await _hang_in_tool(tool_cancelled)

    base, count0, voice0 = _active(), _count(), _voice()
    body = stream_response(stream_brain(loop_fn), is_voice, started_at=time.time()).body_iterator
    assert "Hello" in await body.__anext__()
    await asyncio.sleep(0)  # let the producer reach the tool
    assert _active() == base + 1
    await body.aclose()
    assert tool_cancelled.is_set()
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice() == voice0 + (1 if is_voice else 0)


@pytest.mark.parametrize("after_first_chunk", [False, True])
async def test_consumer_cancelled_in_queue_get_records_once(after_first_chunk):
    """Starlette cancels the response task on disconnect while the generator
    waits in queue.get() — before any chunk (tool still running before the
    first word) or after one. A voice call hung up mid-reply."""
    tool_running = asyncio.Event()
    tool_cancelled = asyncio.Event()
    got_first = asyncio.Event()

    async def loop_fn(**kw):
        if after_first_chunk:
            await kw["on_delta"]("Hello")
        tool_running.set()
        await _hang_in_tool(tool_cancelled)

    base, count0, voice0 = _active(), _count(), _voice()
    body = stream_response(stream_brain(loop_fn), True, started_at=time.time()).body_iterator

    async def consume():
        async for _ in body:
            got_first.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(tool_running.wait(), timeout=2)
    if after_first_chunk:
        await asyncio.wait_for(got_first.wait(), timeout=2)
    assert got_first.is_set() is after_first_chunk
    assert _active() == base + 1
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert tool_cancelled.is_set()
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice() == voice0 + 1


async def test_tool_turning_cancel_into_error_still_records_once():
    """A tool that answers its cancellation with another exception: the
    producer swallows it, the stream still closes, the gauge still drops."""

    async def loop_fn(**kw):
        await kw["on_delta"]("Hello")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    base, count0 = _active(), _count()
    body = stream_response(stream_brain(loop_fn), False, started_at=time.time()).body_iterator
    assert "Hello" in await body.__anext__()
    await asyncio.sleep(0)
    await asyncio.wait_for(body.aclose(), timeout=2)
    assert _active() == base
    assert _count() == count0 + 1


async def test_never_started_body_records_nothing():
    """Client gone before the first chunk was pulled: the generator never ran,
    so it never counted itself and has nothing to undo."""
    called = []

    async def loop_fn(**kw):
        called.append(True)
        return "never"

    base, count0 = _active(), _count()
    resp = stream_response(stream_brain(loop_fn), True, started_at=time.time())
    del resp
    gc.collect()
    await asyncio.sleep(0)
    assert called == []
    assert _active() == base
    assert _count() == count0


async def test_concurrent_streams_each_count_once():
    release = asyncio.Event()

    async def loop_fn(**kw):
        await kw["on_delta"]("Hi")
        await release.wait()
        return "Hi"

    base, count0 = _active(), _count()
    brain = stream_brain(loop_fn)
    a = stream_response(brain, False, started_at=time.time()).body_iterator
    b = stream_response(brain, True, started_at=time.time()).body_iterator
    await a.__anext__()
    await b.__anext__()
    assert _active() == base + 2
    release.set()
    _ = [chunk async for chunk in a]
    assert _active() == base + 1
    _ = [chunk async for chunk in b]
    assert _active() == base
    assert _count() == count0 + 2


# ---------------------------------------------------------------------------
# _chat_unified: buffered turns observed here, real streams left to the stream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("is_voice", [False, True])
@pytest.mark.parametrize("streaming, expected", [("real", 0), (True, 1), (False, 1)])
async def test_chat_unified_observes_only_non_real_streams(streaming, expected, is_voice):
    brain = stream_brain(None)
    seen = {}

    async def fake_inner(
        messages, stream, external_tools, ha_client, user_text, routing_info, is_voice, started_at=None
    ):
        seen["active"] = _active()
        seen["started_at"] = started_at
        routing_info["streaming"] = streaming
        return "response"

    brain._chat_unified_inner = fake_inner
    base, count0, buffered0 = _active(), _count(), _voice("buffered")
    t0 = time.time()
    out = await brain._chat_unified(list(MESSAGES), stream=True, is_voice=is_voice)
    assert out == "response"
    assert seen["active"] == base + 1
    assert seen["started_at"] is not None and abs(seen["started_at"] - t0) < 5
    assert _active() == base
    assert _count() == count0 + expected
    assert _voice("buffered") == buffered0 + (expected if is_voice else 0)


async def test_chat_unified_inner_error_still_decrements_and_observes_once():
    class _InnerBoom(Exception):
        pass

    brain = stream_brain(None)

    async def fake_inner(*args, **kwargs):
        raise _InnerBoom("inner failed")

    brain._chat_unified_inner = fake_inner
    base, count0 = _active(), _count()
    errors0 = metric_value(REQUEST_ERRORS, mode="unified", error_type="_InnerBoom")
    with pytest.raises(_InnerBoom):
        await brain._chat_unified(list(MESSAGES), stream=False, is_voice=True)
    assert _active() == base
    assert _count() == count0 + 1
    assert metric_value(REQUEST_ERRORS, mode="unified", error_type="_InnerBoom") == errors0 + 1


@pytest.mark.parametrize("is_voice", [False, True])
async def test_real_stream_through_chat_unified_is_counted_once(monkeypatch, is_voice):
    """The whole path: _chat_unified -> real _chat_unified_inner -> real stream.
    One latency sample (from the stream, not also from _chat_unified), the
    gauge at +1 while the loop runs (not +2), voice timed as streamed only."""
    in_loop = {}

    async def loop_fn(**kw):
        assert kw["on_delta"] is not None
        assert kw["is_voice"] is is_voice
        await kw["on_delta"]("Hello")
        in_loop["active"] = _active()
        return "Hello"

    brain = make_brain(monkeypatch, loop_fn, fallback_url="")
    base, count0, streamed0, buffered0 = _active(), _count(), _voice(), _voice("buffered")

    resp = await brain._chat_unified(list(MESSAGES), stream=True, is_voice=is_voice)
    assert resp.media_type == "text/event-stream"
    assert _active() == base
    assert _count() == count0

    assert "Hello" in await drain(resp)
    assert in_loop["active"] == base + 1
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice() == streamed0 + (1 if is_voice else 0)
    assert _voice("buffered") == buffered0


async def test_buffered_turn_through_chat_unified_is_counted_once(monkeypatch):
    seen = {}

    async def loop_fn(**kw):
        seen["active"] = _active()
        seen["on_delta"] = kw.get("on_delta")
        return "Hello"

    brain = make_brain(monkeypatch, loop_fn, fallback_url="")
    base, count0, buffered0 = _active(), _count(), _voice("buffered")
    resp = await brain._chat_unified(list(MESSAGES), stream=False, is_voice=True)
    assert resp.status_code == 200
    assert seen == {"active": base + 1, "on_delta": None}
    assert _active() == base
    assert _count() == count0 + 1
    assert _voice("buffered") == buffered0 + 1


# ---------------------------------------------------------------------------
# _unified_fallback forwards is_voice
# ---------------------------------------------------------------------------


def _capturing_loop(calls, fail_primary=False):
    async def loop_fn(**kw):
        calls.append(kw)
        if fail_primary and kw["model_url"] == "http://primary.invalid/v1":
            raise RuntimeError("primary loop failed")
        return "fallback answer"

    return loop_fn


@pytest.mark.parametrize("kwargs, expected", [({"is_voice": True}, True), ({"is_voice": False}, False), ({}, False)])
async def test_unified_fallback_forwards_is_voice(kwargs, expected):
    calls = []
    brain = stream_brain(_capturing_loop(calls), fallback_url="http://fb.invalid/v1")
    resp = await brain._unified_fallback(list(MESSAGES), "sys", False, {}, **kwargs)
    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0]["is_voice"] is expected
    assert calls[0]["model_url"] == "http://fb.invalid/v1"


@pytest.mark.parametrize("is_voice", [False, True])
async def test_primary_down_fallback_gets_is_voice(monkeypatch, is_voice):
    """Health check fails and the server can't be started: the fallback model
    runs with the turn's is_voice."""
    calls = []
    brain = make_brain(monkeypatch, _capturing_loop(calls), healthy=False, fallback_url="http://fb.invalid/v1")
    base, count0 = _active(), _count()
    resp = await brain._chat_unified(list(MESSAGES), stream=False, is_voice=is_voice)
    assert resp.status_code == 200
    assert [c["model_url"] for c in calls] == ["http://fb.invalid/v1"]
    assert calls[0]["is_voice"] is is_voice
    assert _active() == base
    assert _count() == count0 + 1


@pytest.mark.parametrize("is_voice", [False, True])
async def test_buffered_loop_failure_fallback_gets_is_voice(monkeypatch, is_voice):
    calls = []
    brain = make_brain(monkeypatch, _capturing_loop(calls, fail_primary=True), fallback_url="http://fb.invalid/v1")
    resp = await brain._chat_unified(list(MESSAGES), stream=False, is_voice=is_voice)
    assert resp.status_code == 200
    assert [c["model_url"] for c in calls] == ["http://primary.invalid/v1", "http://fb.invalid/v1"]
    assert [c["is_voice"] for c in calls] == [is_voice, is_voice]


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


def test_request_latency_bucket_edges():
    REQUEST_LATENCY.labels(mode="unified")  # child exists, no observation
    edges = [
        s.labels["le"]
        for metric in REQUEST_LATENCY.collect()
        for s in metric.samples
        if s.name.endswith("_bucket") and s.labels["mode"] == "unified"
    ]
    # Grafana thresholds (10 / 30) and HighRequestLatency (p95 > 30 s).
    assert "10.0" in edges
    assert "30.0" in edges
    # Whole turns mostly take 2-13 s: resolution between 10 and 30.
    assert {"13.0", "17.0", "22.0"} <= set(edges)
    finite = [float(e) for e in edges if e != "+Inf"]
    assert finite == sorted(finite)
    assert edges[-1] == "+Inf"
