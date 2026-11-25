"""
Web search observability + voice budget.

- SearXNG client: every search gets an outcome (ok / empty / error / timeout),
  unresponsive engines become bounded label pairs, the timeout is a
  whole-request deadline, and logs never carry the query or the URL.
- tool_web_search: counts bgw_web_search_total{result,voice}, records 0
  results on empty, and uses the 5 s voice deadline on voice turns.
- unified_loop: shared.voice_turn_var is True only while a voice turn's tool
  runs (also across asyncio.gather), never after; the voice deadline reaches
  the SearXNG request end to end.

When real-streamed voice turns record bgw_voice_pipeline_seconds (at the end
of the stream, once, fallback and hang-up included) is pinned in
test_streamed_request_latency.py.

No real SearXNG traffic: respx and stubs only (a burst of real searches got
the upstream engines suspended on 2026-10-25).
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from prometheus_client import REGISTRY

from orchestrator import shared
from orchestrator.tests.fixtures_loop import reply, run_loop, tool_call, tools_named
from orchestrator.unified_loop import run_unified_tool_loop

SX = "http://searxng-test:8080"
QUERY = "zebra crossing opening hours"  # a sentinel that must never reach logs


@pytest.fixture(autouse=True)
def _fresh_engine_labels(monkeypatch):
    """Each test starts with no engine labels seen (the cap is process-wide)."""
    from orchestrator import web_search

    monkeypatch.setattr(web_search, "_seen_engine_labels", set())


def _sample(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _client():
    from orchestrator.web_search import SearXNGClient

    return SearXNGClient(url=SX, max_results=5)


def _assert_no_query_in_logs(caplog):
    for rec in caplog.records:
        line = rec.getMessage()
        assert QUERY not in line
        assert "q=" not in line
        assert "searxng-test" not in line


# ---------------------------------------------------------------------------
# SearXNG client
# ---------------------------------------------------------------------------
@respx.mock
async def test_client_ok_with_partial_engine_failures():
    respx.get(f"{SX}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"title": "T", "url": "https://example.org", "content": "c", "engine": "bing"}],
                "unresponsive_engines": [["duckduckgo", "timeout"]],
            },
        )
    )
    r = await _client().search(QUERY)
    assert r.success and r.outcome == "ok"
    assert len(r.results) == 1
    assert r.unresponsive_engines == [("duckduckgo", "timeout")]


@respx.mock
async def test_client_empty_with_suspended_engines():
    respx.get(f"{SX}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [],
                "unresponsive_engines": [
                    ["Brave", "Suspended: too many requests"],
                    ["startpage", "Suspended: CAPTCHA"],
                    ["duckduckgo", "CAPTCHA"],
                    ["qwant", "access denied"],
                    ["mojeek", "too many requests"],
                    ["wiki", "unexpected crash"],
                ],
            },
        )
    )
    r = await _client().search(QUERY)
    assert r.success and r.outcome == "empty"
    assert r.results == []
    assert dict(r.unresponsive_engines) == {
        "brave": "suspended",
        "startpage": "suspended",
        "duckduckgo": "captcha",
        "qwant": "access_denied",
        "mojeek": "too_many_requests",
        "wiki": "other",
    }


@respx.mock
async def test_client_no_results_when_every_engine_answered():
    respx.get(f"{SX}/search").mock(return_value=httpx.Response(200, json={"results": [], "unresponsive_engines": []}))
    r = await _client().search(QUERY)
    assert r.success and r.outcome == "no_results"


@respx.mock
async def test_client_http_error_is_error_and_logs_no_url(caplog):
    respx.get(f"{SX}/search").mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.DEBUG, logger="orchestrator.web_search"):
        r = await _client().search(QUERY)
    assert not r.success and r.outcome == "error"
    assert r.error == "Search service error: 503"
    _assert_no_query_in_logs(caplog)


@respx.mock
async def test_client_read_timeout_is_timeout(caplog):
    respx.get(f"{SX}/search").mock(side_effect=httpx.ReadTimeout(""))
    with caplog.at_level(logging.DEBUG, logger="orchestrator.web_search"):
        r = await _client().search(QUERY, timeout=5)
    assert not r.success and r.outcome == "timeout"
    assert r.error == "Search timed out after 5 seconds"
    # Was an empty "SearXNG error: " line before — now names the exception.
    assert any("ReadTimeout" in rec.getMessage() for rec in caplog.records)
    _assert_no_query_in_logs(caplog)


@respx.mock
async def test_client_transport_error_message_never_leaks(caplog):
    leaky = f"connect failed for {SX}/search?q={QUERY}"
    respx.get(f"{SX}/search").mock(side_effect=httpx.ConnectError(leaky))
    with caplog.at_level(logging.DEBUG, logger="orchestrator.web_search"):
        r = await _client().search(QUERY)
    assert not r.success and r.outcome == "error"
    assert r.error == "Search service unavailable (ConnectError)"
    assert QUERY not in r.error
    _assert_no_query_in_logs(caplog)


@respx.mock
async def test_client_bad_json_is_error():
    respx.get(f"{SX}/search").mock(return_value=httpx.Response(200, content=b"<html>not json</html>"))
    r = await _client().search(QUERY)
    assert not r.success and r.outcome == "error"


@respx.mock
async def test_client_deadline_bounds_whole_request():
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"results": []})

    respx.get(f"{SX}/search").mock(side_effect=slow)
    t0 = time.monotonic()
    r = await _client().search(QUERY, timeout=0.1)
    assert time.monotonic() - t0 < 1.0
    assert r.outcome == "timeout"


@respx.mock
async def test_client_passes_timeout_to_shared_http_client():
    from orchestrator.web_search import DEFAULT_TIMEOUT_SECONDS, SearXNGClient

    route = respx.get(f"{SX}/search").mock(return_value=httpx.Response(200, json={"results": []}))
    async with httpx.AsyncClient() as http:
        c = SearXNGClient(url=SX, http_client=http)
        await c.search(QUERY, timeout=5)
        assert route.calls.last.request.extensions["timeout"]["read"] == 5
        await c.search(QUERY)
        assert route.calls.last.request.extensions["timeout"]["read"] == DEFAULT_TIMEOUT_SECONDS


def test_search_response_derives_outcome():
    from orchestrator.web_search import SearchResponse, SearchResult

    hit = SearchResult(title="T", url="https://example.org", content="")
    assert SearchResponse(success=True, results=[hit]).outcome == "ok"
    assert SearchResponse(success=True).outcome == "no_results"
    assert SearchResponse(success=True, unresponsive_engines=[("brave", "suspended")]).outcome == "empty"
    assert SearchResponse(success=False).outcome == "error"
    assert SearchResponse(success=False, outcome="timeout").outcome == "timeout"


def test_parse_unresponsive_is_bounded_and_tolerant():
    from orchestrator.web_search import _parse_unresponsive

    assert _parse_unresponsive(None) == []
    assert _parse_unresponsive("brave") == []
    assert _parse_unresponsive({"brave": "timeout"}) == []
    assert _parse_unresponsive([["brave"], [1, "timeout"], ["bing", None], "x"]) == []
    # Odd names fold into "other"; duplicates collapse.
    assert _parse_unresponsive([["evil\n{label}", "timeout"], ["x" * 40, "timeout"]]) == [("other", "timeout")]
    assert _parse_unresponsive([["bing", "timeout"], ["Bing", "timeout"]]) == [("bing", "timeout")]
    # At most 20 entries are read.
    many = [[f"engine{i}", "timeout"] for i in range(50)]
    assert len(_parse_unresponsive(many)) == 20


def test_engine_labels_capped_by_count(monkeypatch):
    from orchestrator import web_search

    monkeypatch.setattr(web_search, "_seen_engine_labels", set())
    for batch in range(3):  # 60 distinct names, 20 per response
        web_search._parse_unresponsive([[f"e{batch}-{i}", "timeout"] for i in range(20)])
    assert len(web_search._seen_engine_labels) == web_search._MAX_ENGINE_LABELS
    # Known names keep their label; new ones past the cap fold into "other".
    assert web_search._parse_unresponsive([["e0-0", "timeout"]]) == [("e0-0", "timeout")]
    assert web_search._parse_unresponsive([["brand-new", "timeout"]]) == [("other", "timeout")]


# ---------------------------------------------------------------------------
# tool_web_search
# ---------------------------------------------------------------------------


def _fake_client(response):
    class _Fake:
        def __init__(self):
            self.search = AsyncMock(return_value=response)

    return _Fake()


def _resp(**kw):
    from orchestrator.web_search import SearchResponse

    return SearchResponse(query=QUERY, **kw)


async def test_tool_counts_ok_typed_turn_with_default_timeout():
    from orchestrator import tool_handlers
    from orchestrator.web_search import DEFAULT_TIMEOUT_SECONDS, SearchResult

    fake = _fake_client(
        _resp(success=True, outcome="ok", results=[SearchResult(title="T", url="https://example.org", content="c")])
    )
    before = _sample("bgw_web_search_total", {"result": "ok", "voice": "false"})
    with patch.object(tool_handlers, "get_search_client", return_value=fake):
        out = await tool_handlers.tool_web_search(QUERY)
    assert "https://example.org" in out
    assert fake.search.await_args.kwargs["timeout"] == DEFAULT_TIMEOUT_SECONDS
    assert _sample("bgw_web_search_total", {"result": "ok", "voice": "false"}) == before + 1


async def test_tool_voice_turn_uses_voice_deadline_and_counts_timeout():
    from orchestrator import tool_handlers
    from orchestrator.web_search import VOICE_TIMEOUT_SECONDS

    fake = _fake_client(_resp(success=False, outcome="timeout", error="Search timed out after 5 seconds"))
    before = _sample("bgw_web_search_total", {"result": "timeout", "voice": "true"})
    token = shared.voice_turn_var.set(True)
    try:
        with patch.object(tool_handlers, "get_search_client", return_value=fake):
            out = await tool_handlers.tool_web_search(QUERY)
    finally:
        shared.voice_turn_var.reset(token)
    assert out.startswith("Web search failed:")
    assert fake.search.await_args.kwargs["timeout"] == VOICE_TIMEOUT_SECONDS
    assert _sample("bgw_web_search_total", {"result": "timeout", "voice": "true"}) == before + 1


async def test_tool_empty_counts_zero_results_and_engines(caplog):
    from orchestrator import tool_handlers

    fake = _fake_client(_resp(success=True, outcome="empty", unresponsive_engines=[("brave", "suspended")]))
    empty_before = _sample("bgw_web_search_total", {"result": "empty", "voice": "false"})
    zero_before = _sample("bgw_web_search_results_count_bucket", {"le": "0.0"})
    eng_before = _sample("bgw_web_search_engine_unresponsive_total", {"engine": "brave", "reason": "suspended"})
    with caplog.at_level(logging.DEBUG), patch.object(tool_handlers, "get_search_client", return_value=fake):
        out = await tool_handlers.tool_web_search(QUERY)
    assert out.startswith("No results found")
    assert _sample("bgw_web_search_total", {"result": "empty", "voice": "false"}) == empty_before + 1
    assert _sample("bgw_web_search_results_count_bucket", {"le": "0.0"}) == zero_before + 1
    assert (
        _sample("bgw_web_search_engine_unresponsive_total", {"engine": "brave", "reason": "suspended"})
        == eng_before + 1
    )
    assert any("brave=suspended" in rec.getMessage() for rec in caplog.records)
    _assert_no_query_in_logs(caplog)


async def test_tool_no_results_is_not_a_failure(caplog):
    from orchestrator import tool_handlers

    fake = _fake_client(_resp(success=True))
    before = _sample("bgw_web_search_total", {"result": "no_results", "voice": "false"})
    zero_before = _sample("bgw_web_search_results_count_bucket", {"le": "0.0"})
    with caplog.at_level(logging.DEBUG), patch.object(tool_handlers, "get_search_client", return_value=fake):
        out = await tool_handlers.tool_web_search(QUERY)
    assert out.startswith("No results found")
    assert _sample("bgw_web_search_total", {"result": "no_results", "voice": "false"}) == before + 1
    assert _sample("bgw_web_search_results_count_bucket", {"le": "0.0"}) == zero_before + 1
    # Not a broken search: nothing at WARNING+ (those lines feed the self-audit).
    assert not [r for r in caplog.records if r.name.startswith("orchestrator") and r.levelno >= logging.WARNING]


async def test_tool_ok_still_counts_unresponsive_engines(caplog):
    """Results came back but some engines are down: still "ok", yet the engines
    are counted — the early sign of a suspension before searches go empty."""
    from orchestrator import tool_handlers
    from orchestrator.web_search import SearchResult

    hit = SearchResult(title="T", url="https://example.org", content="c")
    fake = _fake_client(_resp(success=True, results=[hit], unresponsive_engines=[("duckduckgo", "captcha")]))
    ok_before = _sample("bgw_web_search_total", {"result": "ok", "voice": "false"})
    eng_before = _sample("bgw_web_search_engine_unresponsive_total", {"engine": "duckduckgo", "reason": "captcha"})
    with caplog.at_level(logging.DEBUG), patch.object(tool_handlers, "get_search_client", return_value=fake):
        out = await tool_handlers.tool_web_search(QUERY)
    assert "https://example.org" in out
    assert _sample("bgw_web_search_total", {"result": "ok", "voice": "false"}) == ok_before + 1
    assert (
        _sample("bgw_web_search_engine_unresponsive_total", {"engine": "duckduckgo", "reason": "captcha"})
        == eng_before + 1
    )
    assert not [r for r in caplog.records if r.name.startswith("orchestrator") and r.levelno >= logging.WARNING]


async def test_tool_empty_query_is_not_a_search():
    from orchestrator import tool_handlers

    fake = _fake_client(_resp(success=True, outcome="ok"))
    with patch.object(tool_handlers, "get_search_client", return_value=fake):
        out = await tool_handlers.tool_web_search("")
    assert out == "No search query provided"
    fake.search.assert_not_awaited()


@pytest.mark.parametrize(
    "args, expected",
    [
        ({"query": "q", "category": "news", "time_range": "week"}, ("q", "news", "week")),
        ({"query": "q"}, ("q", "general", None)),
        ({"query": "q", "category": "free text from the model", "time_range": "since 1999"}, ("q", "general", None)),
    ],
)
async def test_registered_tool_clamps_category_and_time_range(args, expected):
    from orchestrator import tool_handlers

    inner = AsyncMock(return_value="ok")
    with patch.object(tool_handlers, "tool_web_search", inner):
        await tool_handlers.execute_tool("web_search", args)
    assert inner.await_args.args == expected


def test_web_search_total_is_preseeded():
    import orchestrator.metrics  # noqa: F401 — registers the series

    for result in ("ok", "no_results", "empty", "error", "timeout"):
        for voice in ("true", "false"):
            assert REGISTRY.get_sample_value("bgw_web_search_total", {"result": result, "voice": voice}) is not None
    for voice in ("true", "false"):
        assert REGISTRY.get_sample_value("bgw_chat_ttft_seconds_count", {"voice": voice}) is not None
    for path in ("streamed", "buffered"):
        assert REGISTRY.get_sample_value("bgw_voice_pipeline_seconds_count", {"path": path}) is not None


# ---------------------------------------------------------------------------
# unified_loop: voice flag reaches the tool, then is cleared
# ---------------------------------------------------------------------------

_TOOLS = tools_named("web_search", "search_memory")


def _llm(content="", tool_calls=None):
    return reply(content, tool_calls, finish_reason="tool_calls" if tool_calls else "stop")


async def test_loop_voice_flag_reaches_parallel_tool_calls():
    seen = []

    async def fake_execute(name, args):
        await asyncio.sleep(0)
        seen.append((name, shared.voice_turn_var.get()))
        return "result"

    calls = [tool_call("web_search", {"query": "a"}, "c1"), tool_call("search_memory", {"query": "b"}, "c2")]
    call_model = AsyncMock(side_effect=[_llm(tool_calls=calls), _llm("done")])
    out = await run_loop(
        call_model,
        fake_execute,
        messages=[{"role": "user", "content": "look it up"}],
        tools=_TOOLS,
        max_rounds=3,
        is_voice=True,
    )
    assert out == "done"
    assert sorted(seen) == [("search_memory", True), ("web_search", True)]
    assert shared.voice_turn_var.get() is False


@pytest.mark.parametrize("is_voice, deadline", [(True, 5.0), (False, 15.0)])
async def test_voice_deadline_reaches_searxng_through_the_loop(monkeypatch, is_voice, deadline):
    """End to end, nothing stubbed between the loop and httpx: unified loop →
    voice_turn_var → execute_tool("web_search") → SearXNGClient → the request
    to (mocked) SearXNG carries the voice or the default timeout."""
    from orchestrator import web_search

    monkeypatch.setattr(shared, "_http", None)
    monkeypatch.setattr(web_search, "_search_client", _client())
    voice = "true" if is_voice else "false"
    before = _sample("bgw_web_search_total", {"result": "ok", "voice": voice})
    call_model = AsyncMock(
        side_effect=[_llm(tool_calls=[tool_call("web_search", {"query": QUERY}, "c1")]), _llm("done")]
    )
    # assert_all_mocked (the default): any other request fails, never leaves the box.
    with respx.mock(assert_all_called=True) as sx, patch("orchestrator.orchestrator.call_model", call_model):
        route = sx.get(f"{SX}/search").mock(
            return_value=httpx.Response(200, json={"results": [{"title": "T", "url": "https://example.org"}]})
        )
        out = await run_unified_tool_loop(
            messages=[{"role": "user", "content": "look it up"}],
            system_prompt="sys",
            tools=_TOOLS,
            model_url="http://fake:8080/v1",
            model_name="m",
            http_client=None,
            max_rounds=3,
            is_voice=is_voice,
        )
    assert out == "done"
    assert route.call_count == 1
    assert route.calls.last.request.extensions["timeout"]["read"] == deadline
    assert _sample("bgw_web_search_total", {"result": "ok", "voice": voice}) == before + 1
    assert shared.voice_turn_var.get() is False
