"""Coder-box gate (coder_gate.py + routes_coder.py + the coder-net guard).

Covers the token store, the request/response filters, the upstream call
(respx), the wait / run-alone / preempt loop, the routes through the real
BearerAuthMiddleware (requests "arrive" on the coder-net address via the
TestClient base_url, which becomes scope["server"]), sessions, the Helios
sleep refusal and the config validator.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import brain_slot, coder_gate, shared, state_store
from orchestrator.config import settings

GATE = "10.213.250.2"
BOX = "10.213.250.140"
API_TOKEN = "test-api-token-0123456789"  # nosec B105 — test-only constant
MODEL_URL = "http://brain.test/v1"
_REAL_CHAT_BUSY = brain_slot.chat_busy  # the autouse fixture stubs it


@pytest.fixture(autouse=True)
def _gate_env(tmp_db, monkeypatch):
    monkeypatch.setattr(settings, "coder_gate_address", GATE)
    monkeypatch.setattr(settings, "coder_subnet", "10.213.250.0/24")
    monkeypatch.setattr(coder_gate, "_admitted", {})
    monkeypatch.setattr(coder_gate, "_bytes_seen", {})
    monkeypatch.setattr(coder_gate, "_rejections", {})
    monkeypatch.setattr(settings, "coder_idle_seconds", 0.0)
    monkeypatch.setattr(settings, "coder_max_wait_seconds", 2.0)
    monkeypatch.setattr(settings, "coder_session_hours", 4.0)
    monkeypatch.setattr(shared, "CODER_ENABLED", True)
    monkeypatch.setattr(shared, "MODEL_NAME", "brain-model")
    monkeypatch.setattr(shared, "MODEL_URL", MODEL_URL)
    monkeypatch.setattr(brain_slot, "_last_chat_activity", 0.0)
    monkeypatch.setattr(brain_slot, "_coder_in_flight", 0)
    monkeypatch.setattr(coder_gate, "PREEMPT_POLL_S", 0.01)
    monkeypatch.setattr(brain_slot, "chat_busy", lambda: False)


def _msgs(text="hi"):
    return [{"role": "user", "content": text}]


# ----------------------------------------------------------------- tokens


class TestTokens:
    def test_mint_and_lookup(self):
        minted = coder_gate.mint_token("session", "fix-bug")
        assert minted["token"].startswith("jcs_") and len(minted["token"]) > 40
        row = coder_gate.lookup_token(minted["token"])
        assert row and row["id"] == minted["id"] and row["label"] == "fix-bug"

    def test_only_the_hash_is_stored(self):
        minted = coder_gate.mint_token("session")
        con = sqlite3.connect(state_store.DB_PATH)
        dump = "\n".join(con.iterdump())
        con.close()
        assert minted["token"] not in dump
        assert coder_gate._hash(minted["token"]) in dump

    @pytest.mark.parametrize("bad", ["", "nope", "jcs_wrong", "x" * 300])
    def test_unknown_tokens_rejected(self, bad):
        coder_gate.mint_token("session")
        assert coder_gate.lookup_token(bad) is None

    def test_expired_token_rejected(self):
        minted = coder_gate.mint_token("session")
        with state_store.get_db() as conn:
            conn.execute(
                "UPDATE coder_tokens SET expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"), minted["id"]),
            )
        assert coder_gate.lookup_token(minted["token"]) is None

    def test_revoked_token_rejected(self):
        minted = coder_gate.mint_token("session")
        assert coder_gate.revoke_token(minted["id"], "session")
        assert coder_gate.lookup_token(minted["token"]) is None
        assert not coder_gate.revoke_token(minted["id"], "session")  # already revoked

    def test_revoke_checks_kind_and_id_shape(self):
        minted = coder_gate.mint_token("job")
        assert not coder_gate.revoke_token(minted["id"], "session")
        assert not coder_gate.revoke_token("../etc", None)

    def test_budget_and_usage(self):
        minted = coder_gate.mint_token("session", max_requests=2, max_tokens=1000)
        assert coder_gate.budget_left(coder_gate.lookup_token(minted["token"]))
        coder_gate.count_request(minted["id"])
        coder_gate.record_usage(minted["id"], 300, 50)
        row = coder_gate.lookup_token(minted["token"])
        assert (row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (1, 300, 50)
        coder_gate.count_request(minted["id"])
        assert not coder_gate.budget_left(coder_gate.lookup_token(minted["token"]))  # 2 requests used

    def test_token_budget(self):
        minted = coder_gate.mint_token("session", max_requests=100, max_tokens=1000)
        coder_gate.record_usage(minted["id"], 990, 20)
        assert not coder_gate.budget_left(coder_gate.lookup_token(minted["token"]))

    def test_list_hides_hash_and_dead_tokens(self):
        live = coder_gate.mint_token("session", "a")
        dead = coder_gate.mint_token("session", "b")
        coder_gate.revoke_token(dead["id"])
        rows = coder_gate.list_tokens("session")
        assert [r["id"] for r in rows] == [live["id"]]
        assert "token_sha256" not in rows[0]

    def test_active_use(self):
        minted = coder_gate.mint_token("session")
        assert not coder_gate.active_use_exists()
        coder_gate.record_usage(minted["id"], 1, 1)
        assert coder_gate.active_use_exists()
        coder_gate.revoke_token(minted["id"])
        assert not coder_gate.active_use_exists()

    def test_old_tokens_pruned_on_mint(self):
        old = coder_gate.mint_token("session")
        with state_store.get_db() as conn:
            conn.execute(
                "UPDATE coder_tokens SET expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds"), old["id"]),
            )
        coder_gate.mint_token("session")
        with state_store.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM coder_tokens WHERE id = ?", (old["id"],)).fetchone()[0] == 0

    @pytest.mark.parametrize("label", ["Bad", "has space", "-lead", "x" * 41, "../x"])
    def test_bad_labels_refused(self, label):
        with pytest.raises(ValueError):
            coder_gate.mint_token("session", label)


# ----------------------------------------------------------------- request filter


class TestSanitize:
    def test_builds_fresh_body(self):
        body = {
            "model": "evil",
            "messages": _msgs(),
            "stream": False,
            "stream_options": {"include_usage": False},
            "grammar": "root ::= x",
            "id_slot": 3,
            "cache_prompt": False,
            "chat_template_kwargs": {"reasoning_effort": "xhigh"},
            "temperature": 0.7,
        }
        up, dropped = coder_gate.sanitize_request(body, 4096)
        assert up["model"] == "brain-model"
        assert up["stream"] is True and up["stream_options"] == {"include_usage": True}
        assert up["repeat_penalty"] == 1.0
        assert up["max_tokens"] == 4096
        for key in ("grammar", "id_slot", "cache_prompt", "chat_template_kwargs"):
            assert key not in up and key in dropped
        assert {"model", "stream_options"} <= set(dropped)

    @pytest.mark.parametrize(
        "field,value,expect",
        [
            ("max_tokens", 99999, 4096),
            ("max_tokens", 0, 1),
            ("max_completion_tokens", 100, 100),
            ("temperature", 1.7, 1.0),
            ("temperature", -1, 0.0),
            ("top_p", 5, 1.0),
        ],
    )
    def test_clamps(self, field, value, expect):
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), field: value}, 4096)
        key = "max_tokens" if field == "max_completion_tokens" else field
        assert up[key] == expect

    @pytest.mark.parametrize(
        "field,value",
        [
            ("temperature", True),
            ("max_tokens", "9"),
            ("top_p", [1]),
            ("temperature", float("nan")),
            ("max_tokens", float("inf")),
            ("top_p", float("-inf")),
        ],
        ids=["bool", "string", "list", "nan", "inf", "neg-inf"],
    )
    def test_non_numbers_refused(self, field, value):
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request({"messages": _msgs(), field: value}, 4096)

    def test_huge_integer_refused(self):
        body = json.loads('{"messages": [{"role": "user", "content": "x"}], "max_tokens": ' + "9" * 400 + "}")
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request(body, 4096)

    def test_nan_from_raw_json_refused(self):
        body = json.loads('{"messages": [{"role": "user", "content": "x"}], "temperature": NaN}')
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request(body, 4096)

    def test_image_parts_never_pass(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "http://10.0.0.248:8123/"}},
                    {"type": "input_audio", "input_audio": {"data": "x"}},
                ],
            }
        ]
        up, dropped = coder_gate.sanitize_request({"messages": msgs}, 4096)
        serialized = json.dumps(up)
        assert "image_url" not in serialized and "10.0.0.248" not in serialized and "input_audio" not in serialized
        assert up["messages"][0]["content"][1]["text"] == coder_gate.NON_TEXT_NOTE
        assert dropped.count("non_text_part") == 2

    def test_tool_round_trip_shapes_pass(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "think",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "file body"}]},
        ]
        up, _ = coder_gate.sanitize_request({"messages": msgs}, 4096)
        assert up["messages"][2]["tool_calls"][0]["function"]["name"] == "read_file"
        assert up["messages"][2]["content"] is None
        assert up["messages"][3]["tool_call_id"] == "c1"

    @pytest.mark.parametrize(
        "arguments,expect",
        [
            ('{"path": "a.py", "content": "x"}', '{"path": "a.py", "content": "x"}'),
            ("", "{}"),
            ("[1, 2]", "{}"),
            ("not json", "{}"),
            ('{"a": ' * 40 + "1" + "}" * 40, "{}"),
            ("[" * 100000 + "]" * 100000, "{}"),
        ],
        ids=["object", "empty", "list", "invalid", "deep", "bracket-bomb"],
    )
    def test_tool_call_arguments_bounded(self, arguments, expect):
        call = {"id": "c", "type": "function", "function": {"name": "write_file", "arguments": arguments}}
        msgs = [{"role": "user", "content": "x"}, {"role": "assistant", "content": None, "tool_calls": [call]}]
        up, dropped = coder_gate.sanitize_request({"messages": msgs}, 4096)
        assert json.loads(up["messages"][1]["tool_calls"][0]["function"]["arguments"]) == json.loads(expect)
        assert ("bad_tool_arguments" in dropped) == (expect == "{}" and arguments != "")

    @pytest.mark.parametrize(
        "messages,reason",
        [
            ([], "bad_messages"),
            ("hi", "bad_messages"),
            ([{"role": "developer", "content": "x"}], "bad_role"),
            ([{"role": "user", "content": 5}], "bad_messages"),
            (
                [{"role": "user", "content": "x", "tool_calls": [{"function": {"name": "a", "arguments": "{}"}}]}],
                "bad_messages",
            ),
            (
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "bad name", "arguments": "{}"}}],
                    }
                ],
                "bad_messages",
            ),
            ([{"role": "tool", "tool_call_id": "x" * 300, "content": "r"}], "bad_messages"),
        ],
        ids=["empty", "not-list", "role", "content-type", "user-tool-calls", "tool-name", "long-id"],
    )
    def test_bad_messages(self, messages, reason):
        with pytest.raises(coder_gate.GateRejected) as exc:
            coder_gate.sanitize_request({"messages": messages}, 4096)
        assert exc.value.reason == reason

    def test_not_a_dict(self):
        with pytest.raises(coder_gate.GateRejected, match="bad_json"):
            coder_gate.sanitize_request([1, 2], 4096)


def _tool(name="read_file", params=None, description="d"):
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params or {"type": "object"}},
    }


def _obj(**props):
    return {"type": "object", "properties": props, "required": list(props)}


class TestToolSchemas:
    def test_normal_tools_pass(self):
        up, _ = coder_gate.sanitize_request(
            {"messages": _msgs(), "tools": [_tool(), _tool("run_shell_command")], "tool_choice": "auto"}, 4096
        )
        assert [t["function"]["name"] for t in up["tools"]] == ["read_file", "run_shell_command"]
        assert up["tool_choice"] == "auto"

    def test_qwen_code_style_schemas_pass(self):
        """Qwen Code 0.25.3's six tools use only type/properties/required/
        description/minimum/maximum (captured 2026-11-21); the rest of the
        allow-list must pass untouched too."""
        params = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "the command"},
                "timeout": {"type": "number", "description": "ms", "minimum": 1, "maximum": 600000},
                "is_background": {"type": "boolean"},
                "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                "mode": {"type": "string", "enum": ["a", "b"]},
                "maybe": {"type": ["string", "null"], "default": None},
            },
            "required": ["command"],
            "additionalProperties": False,
        }
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool("run_shell_command", params)]}, 4096)
        assert up["tools"][0]["function"]["parameters"] == params

    @pytest.mark.parametrize(
        "tools,reason",
        [
            ("x", "bad_tools"),
            ([{"type": "web_search"}], "bad_tools"),
            ([_tool("bad name")], "bad_tools"),
            ([_tool() for _ in range(coder_gate.MAX_TOOLS + 1)], "too_many_tools"),
            ([_tool(description="x" * (coder_gate.MAX_TOOL_SCHEMA_CHARS + 1))], "schema_too_large"),
            ([_tool(f"t{i}", description="x" * 12_000) for i in range(6)], "schema_too_large"),
            ([_tool(params=_obj(v={"type": "array", "items": {"type": "string"}, "minItems": 5000}))], "schema_bounds"),
            ([_tool(params=_obj(v={"type": "string", "maxLength": 100000}))], "schema_bounds"),
            ([_tool(params=_obj(v={"type": "string", "pattern": "^((a?){40}){40}$"}))], "schema_keyword"),
            ([_tool(params=_obj(v={"type": "string", "format": "uri"}))], "schema_keyword"),
            ([_tool(params=_obj(v={"$ref": "#/$defs/a"}) | {"$defs": {"a": {"type": "string"}}})], "schema_keyword"),
            ([_tool(params=_obj(v={"anyOf": [{"type": "string"}, {"type": "string"}]}))], "schema_keyword"),
            ([_tool(params=_obj(v={"type": "string", "oneOf": []}))], "schema_keyword"),
            ([_tool(params=_obj(v={}))], "schema_keyword"),
            ([_tool(params=_obj(v={"type": "array", "items": [{"type": "string"}]}))], "schema_keyword"),
            ([_tool(params=_obj(v={"type": "string", "enum": [str(i) for i in range(65)]}))], "schema_bounds"),
            ([_tool(params=_obj(v={"type": "object", "additionalProperties": {"type": "string"}}))], "schema_keyword"),
            ([_tool(params=_obj(v={"type": "integer", "minimum": "1"}))], "schema_bounds"),
            ([_tool(params={"type": "array", "items": {"type": "string"}})], "schema_keyword"),
        ],
        ids=[
            "not-list",
            "not-function",
            "name",
            "count",
            "one-too-big",
            "total-too-big",
            "min-items",
            "max-length",
            "pattern-bomb",
            "format",
            "ref-defs",
            "any-of",
            "one-of",
            "untyped",
            "tuple-items",
            "big-enum",
            "additional-schema",
            "string-minimum",
            "array-root",
        ],
    )
    def test_refused(self, tools, reason):
        with pytest.raises(coder_gate.GateRejected) as exc:
            coder_gate.sanitize_request({"messages": _msgs(), "tools": tools}, 4096)
        assert exc.value.reason == reason

    def test_deep_schema_refused(self):
        node: dict = {"type": "object"}
        root = node
        for _ in range(coder_gate.MAX_SCHEMA_DEPTH + 2):
            child: dict = {"type": "object"}
            node["properties"] = {"x": child}
            node = child
        with pytest.raises(coder_gate.GateRejected, match="schema_bounds"):
            coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool(params=root)]}, 4096)

    @pytest.mark.parametrize("choice", ["sometimes", {"type": "function", "function": {"name": "x y"}}, 3])
    def test_bad_tool_choice(self, choice):
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool()], "tool_choice": choice}, 4096)

    @pytest.mark.parametrize(
        "stop,ok", [("END", True), (["a", "b"], True), (["a"] * 5, False), ([""], False), (["x" * 65], False)]
    )
    def test_stop(self, stop, ok):
        if ok:
            up, _ = coder_gate.sanitize_request({"messages": _msgs(), "stop": stop}, 4096)
            assert isinstance(up["stop"], list)
        else:
            with pytest.raises(coder_gate.GateRejected, match="bad_params"):
                coder_gate.sanitize_request({"messages": _msgs(), "stop": stop}, 4096)


# ----------------------------------------------------------------- response filter


class TestResponseFilter:
    def test_strip_chunk_removes_cache_signals(self):
        chunk = {
            "choices": [],
            "timings": {"cache_n": 12000, "prompt_n": 4},
            "__verbose": {"prompt": "secret"},
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 9},
            },
        }
        out = coder_gate.strip_chunk(chunk)
        assert "timings" not in out and "__verbose" not in out
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}

    def test_strip_chunk_is_an_allow_list(self):
        chunk = {
            "id": "x",
            "choices": [{"index": 0, "delta": {"content": "a", "future": 1}, "finish_reason": None, "logprobs": {}}],
            "prompt_progress": {"cache": 26608, "total": 26612},
            "slot_id": 0,
            "usage": "weird",
        }
        assert coder_gate.strip_chunk(chunk) == {
            "id": "x",
            "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": None}],
        }

    def test_assemble_merges_content_and_tool_calls(self):
        chunks = [
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"a'}}]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"name": "file", "arguments": '":1}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        out = coder_gate.assemble(chunks, {"prompt_tokens": 5, "completion_tokens": 3})
        msg = out["choices"][0]["message"]
        assert msg["content"] == "Hello"
        assert msg["tool_calls"] == [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"a":1}'}}
        ]
        assert out["choices"][0]["finish_reason"] == "tool_calls"
        assert out["usage"]["total_tokens"] == 8

    def test_sse_ends_with_done(self):
        assert coder_gate.sse_body([{"a": 1}]) == 'data: {"a": 1}\n\ndata: [DONE]\n\n'


# ----------------------------------------------------------------- reach


class TestReach:
    def test_box_on_the_gate(self):
        scope = {"server": (GATE, 8888), "client": (BOX, 4000)}
        assert coder_gate.arrived_on_gate(scope) and coder_gate.on_coder_net(scope)
        assert coder_gate.arrived_on_gate({"server": [GATE, 8888], "client": [BOX, 4000]})

    @pytest.mark.parametrize(
        "scope",
        [
            {"server": (GATE, 8888), "client": ("172.26.0.1", 4000)},  # gate address, outside client
            {"server": ("172.26.0.2", 8888), "client": (BOX, 4000)},  # subnet client, other address (drift)
        ],
        ids=["gate-addr-outside-client", "subnet-client-other-addr"],
    )
    def test_half_matches_are_guarded_but_not_served(self, scope):
        assert coder_gate.on_coder_net(scope)
        assert not coder_gate.arrived_on_gate(scope)

    def test_ipv4_mapped_addresses_match(self):
        """uvicorn on `::` reports IPv4 peers as ::ffff:a.b.c.d; the guard must still see them."""
        scope = {"server": (f"::ffff:{GATE}", 8888), "client": (f"::ffff:{BOX}", 4000)}
        assert coder_gate.arrived_on_gate(scope)
        assert coder_gate.on_coder_net({"server": ("::1", 8888), "client": (f"::ffff:{BOX}", 4000)})

    @pytest.mark.parametrize(
        "scope",
        [
            {"server": ("172.26.0.2", 8888), "client": ("10.0.0.50", 1)},
            {"server": None},
            {},
            {"client": ("testclient", 1)},
        ],
        ids=["lan", "no-server", "empty", "non-ip-client"],
    )
    def test_lan_is_neither(self, scope):
        assert not coder_gate.on_coder_net(scope) and not coder_gate.arrived_on_gate(scope)


# ----------------------------------------------------------------- upstream


def _sse_body(*objs):
    return "".join(f"data: {json.dumps(o)}\n\n" for o in objs) + "data: [DONE]\n\n"


@pytest.fixture
async def upstream(monkeypatch):
    client = httpx.AsyncClient()
    monkeypatch.setattr(shared, "_http", client)
    with respx.mock(assert_all_called=False) as router:
        yield router
    await client.aclose()


class TestStreamOnce:
    async def test_buffers_and_strips(self, upstream):
        route = upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                text=_sse_body(
                    {"choices": [{"delta": {"content": "ok"}}]},
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "timings": {"cache_n": 99},
                        "usage": {
                            "prompt_tokens": 7,
                            "completion_tokens": 1,
                            "prompt_tokens_details": {"cached_tokens": 6},
                        },
                    },
                ),
            )
        )
        up, _ = coder_gate.sanitize_request({"messages": _msgs()}, 4096)
        chunks, usage = await coder_gate._stream_once(up)
        assert usage == {"prompt_tokens": 7, "completion_tokens": 1}
        assert "timings" not in json.dumps(chunks) and "cached_tokens" not in json.dumps(chunks)
        sent = route.calls.last.request
        assert json.loads(sent.content) == up
        assert "authorization" not in {k.lower() for k in sent.headers}  # no client header forwarded

    async def test_context_overflow_passes_through(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(400, json={"error": {"type": "exceed_context_size_error", "message": "x"}})
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.status == 400 and exc.value.err_type == "exceed_context_size_error"

    async def test_other_errors_stay_generic(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(500, json={"error": {"type": "server_error", "message": "prompt text here"}})
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.err_type is None

    async def test_loading_brain_is_unavailable(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(503, json={"error": {"message": "Loading model"}})
        )
        with pytest.raises(coder_gate.BrainUnavailable):
            await coder_gate._stream_once({"messages": []})

    async def test_truncated_stream_is_an_error(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text='data: {"choices": [{"delta": {"content": "par"}}]}\n\n')
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.status == 0

    async def test_finish_reason_without_done_is_ok(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text='data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n')
        )
        chunks, _ = await coder_gate._stream_once({"messages": []})
        assert len(chunks) == 1

    @pytest.mark.parametrize(
        "text,err_type",
        [
            ('error: {"code": 500, "message": "SECRET", "type": "server_error"}\n\n', None),
            (
                'data: {"error": {"code": 400, "type": "exceed_context_size_error", "message": "SECRET"}}\n\n',
                "exceed_context_size_error",
            ),
        ],
        ids=["error-event", "data-error"],
    )
    async def test_mid_stream_errors(self, upstream, text, err_type):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text='data: {"choices": [{"delta": {"content": "x"}}]}\n\n' + text)
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.err_type == err_type

    async def test_odd_usage_values(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(
                200, text=_sse_body({"choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": [1]}})
            )
        )
        with pytest.raises(coder_gate.UpstreamError):
            await coder_gate._stream_once({"messages": []})

    async def test_huge_error_body_not_read_whole(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(500, content=b"x" * (coder_gate.MAX_ERROR_BODY_BYTES * 3))
        )
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert exc.value.err_type is None

    async def test_unreachable_brain(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(coder_gate.BrainUnavailable):
            await coder_gate._stream_once({"messages": []})

    async def test_no_client(self, monkeypatch):
        monkeypatch.setattr(shared, "_http", None)
        with pytest.raises(coder_gate.BrainUnavailable):
            await coder_gate._stream_once({"messages": []})


# ----------------------------------------------------------------- wait / alone / preempt


class TestRunChat:
    async def test_runs_and_marks_coder_busy(self, monkeypatch):
        seen = []

        async def fake(up):
            seen.append(brain_slot.coder_busy())
            return [{"x": 1}], {"prompt_tokens": 1, "completion_tokens": 1}

        monkeypatch.setattr(coder_gate, "_stream_once", fake)
        chunks, usage = await coder_gate.run_chat({})
        assert chunks == [{"x": 1}] and seen == [True] and not brain_slot.coder_busy()

    async def test_preempted_then_resent(self, monkeypatch):
        state = {"calls": 0, "busy": False}

        async def fake(up):
            state["calls"] += 1
            if state["calls"] == 1:
                state["busy"] = True  # chat starts while the first attempt runs
                await asyncio.sleep(5)
            return [{"n": state["calls"]}], {"prompt_tokens": 0, "completion_tokens": 0}

        def busy():
            return bool(state["busy"] and state["calls"] == 1)

        async def turn(idle_s, max_wait_s, poll_s=0.5):
            state["busy"] = False  # chat finished
            return True

        monkeypatch.setattr(coder_gate, "_stream_once", fake)
        monkeypatch.setattr(brain_slot, "chat_busy", busy)
        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", turn)
        chunks, _ = await asyncio.wait_for(coder_gate.run_chat({}), timeout=3)
        assert chunks == [{"n": 2}] and state["calls"] == 2

    async def test_attempt_wall_clock(self, monkeypatch):
        async def endless(up):
            await asyncio.sleep(10)

        monkeypatch.setattr(coder_gate, "_stream_once", endless)
        monkeypatch.setattr(coder_gate, "ATTEMPT_TIMEOUT_S", 0.1)
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await asyncio.wait_for(coder_gate.run_chat({}), 3)
        assert exc.value.status == 504 and not brain_slot.coder_busy()

    async def test_busy_when_chat_never_idles(self, monkeypatch):
        async def never(idle_s, max_wait_s, poll_s=0.5):
            return False

        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", never)
        with pytest.raises(coder_gate.GateBusy):
            await coder_gate.run_chat({})

    async def test_too_many_preemptions(self, monkeypatch):
        async def slow(up):
            await asyncio.sleep(5)

        monkeypatch.setattr(coder_gate, "_stream_once", slow)
        monkeypatch.setattr(brain_slot, "chat_busy", lambda: True)

        async def turn(idle_s, max_wait_s, poll_s=0.5):
            return True

        monkeypatch.setattr(brain_slot, "wait_for_coder_turn", turn)
        monkeypatch.setattr(coder_gate, "MAX_PREEMPTS", 2)
        with pytest.raises(coder_gate.GateBusy):
            await asyncio.wait_for(coder_gate.run_chat({}), timeout=3)

    async def test_one_request_at_a_time(self, monkeypatch):
        live = {"now": 0, "max": 0}

        async def fake(up):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0.05)
            live["now"] -= 1
            return [], {"prompt_tokens": 0, "completion_tokens": 0}

        monkeypatch.setattr(coder_gate, "_stream_once", fake)
        await asyncio.gather(*(coder_gate.run_chat({}) for _ in range(4)))
        assert live["max"] == 1

    async def test_lock_wait_times_out(self, monkeypatch):
        monkeypatch.setattr(settings, "coder_max_wait_seconds", 0.05)
        lock = coder_gate._gate_lock()
        await lock.acquire()
        try:
            with pytest.raises(coder_gate.GateBusy):
                await coder_gate.run_chat({})
        finally:
            lock.release()


# ----------------------------------------------------------------- routes + guard


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

    @application.get("/health")
    async def health():
        return {"ok": True}

    return application


def _box(app, client_ip=BOX):
    return TestClient(app, base_url=f"http://{GATE}:8888", client=(client_ip, 50000))


def _lan(app, client_ip="10.0.0.50"):
    return TestClient(app, base_url="http://10.0.0.248:8888", client=(client_ip, 50000))


def _session_token():
    return coder_gate.mint_token("session")["token"]


def _ok_run(monkeypatch, chunks=None, usage=None):
    async def fake(up, still_usable=None):
        fake.up = up
        return chunks or [{"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}], usage or {
            "prompt_tokens": 11,
            "completion_tokens": 3,
        }

    monkeypatch.setattr(coder_gate, "run_chat", fake)
    return fake


class TestAdmission:
    def test_cap_across_tokens(self):
        admissions = [coder_gate.admit(f"{i:032x}") for i in range(coder_gate.MAX_ADMITTED_TOTAL)]
        with pytest.raises(coder_gate.GateBusy):
            coder_gate.admit("f" * 32)
        assert coder_gate.requests_admitted() == coder_gate.MAX_ADMITTED_TOTAL
        for a in admissions:
            a.release()
        assert coder_gate.requests_admitted() == 0

    def test_same_token_supersedes_instead_of_counting(self):
        first = coder_gate.admit("a" * 32)
        second = coder_gate.admit("a" * 32)
        assert first.superseded and not second.superseded
        assert coder_gate.requests_admitted() == 1
        first.release()  # the old one's release leaves the new one in place
        assert coder_gate.requests_admitted() == 1
        second.release()
        assert coder_gate.requests_admitted() == 0

    async def test_supersede_cancels_a_running_request(self, monkeypatch):
        started = asyncio.Event()

        async def slow(up):
            started.set()
            await asyncio.sleep(10)

        monkeypatch.setattr(coder_gate, "_stream_once", slow)
        first = coder_gate.admit("b" * 32)
        task = asyncio.create_task(first.run({}))
        await asyncio.wait_for(started.wait(), 2)
        second = coder_gate.admit("b" * 32)
        with pytest.raises(coder_gate.Superseded):
            await asyncio.wait_for(task, 2)
        assert not brain_slot.coder_busy()
        first.release()
        second.release()

    async def test_superseded_before_running(self):
        first = coder_gate.admit("c" * 32)
        coder_gate.admit("c" * 32).release()
        with pytest.raises(coder_gate.Superseded):
            await first.run({})

    async def test_token_revoked_while_waiting(self, monkeypatch):
        async def never_called(up):
            raise AssertionError("must not reach the brain")

        monkeypatch.setattr(coder_gate, "_stream_once", never_called)

        async def dead():
            return False

        with pytest.raises(coder_gate.TokenRevoked):
            await coder_gate.run_chat({}, dead)

    def test_token_still_usable(self):
        minted = coder_gate.mint_token("session", max_requests=1)
        assert coder_gate.token_still_usable(minted["id"])
        coder_gate.count_request(minted["id"])
        assert coder_gate.token_still_usable(minted["id"])  # its last allowed request, already counted
        coder_gate.count_request(minted["id"])
        assert not coder_gate.token_still_usable(minted["id"])
        assert not coder_gate.token_still_usable("0" * 32)

    def test_route_answers_429_when_full(self, app, monkeypatch):
        _ok_run(monkeypatch)
        held = [coder_gate.admit(f"{i:032x}") for i in range(coder_gate.MAX_ADMITTED_TOTAL)]
        try:
            r = _box(app).post(
                coder_gate.GATE_CHAT_PATH,
                headers={"Authorization": f"Bearer {_session_token()}"},
                json={"messages": _msgs()},
            )
            assert r.status_code == 429 and r.headers["retry-after"] == "15"
        finally:
            for a in held:
                a.release()


def test_app_has_no_websocket_routes():
    """The coder-net guard lives in a BaseHTTPMiddleware, which websocket
    connections skip. A websocket route would be reachable from a box."""
    from starlette.routing import WebSocketRoute

    import orchestrator.orchestrator as orch

    assert not [r for r in orch.app.routes if isinstance(r, WebSocketRoute)]


class TestGuard:
    @pytest.mark.parametrize(
        "path", ["/api/private", "/health", "/api/coder/sessions", "/api/coder/v1/models/", "/api/coder/v1/models%0A"]
    )
    def test_box_reaches_only_the_gate(self, app, path):
        r = _box(app).get(path, headers={"Authorization": f"Bearer {API_TOKEN}"})
        assert r.status_code == 403

    def test_cors_preflight_not_exempt_on_gate(self, app):
        r = _box(app).options("/api/private", headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"})
        assert r.status_code == 403

    @pytest.mark.parametrize("path", [coder_gate.GATE_CHAT_PATH, coder_gate.GATE_MODELS_PATH])
    def test_gate_paths_404_off_the_gate(self, app, path):
        token = _session_token()
        for auth in (API_TOKEN, token):
            r = _lan(app).request(
                "POST" if path.endswith("completions") else "GET",
                path,
                headers={"Authorization": f"Bearer {auth}"},
                json={},
            )
            assert r.status_code == 404

    def test_drift_fails_closed(self, app, monkeypatch):
        """Compose address and setting out of step: a box (subnet client) on
        another orchestrator address still reaches nothing but a 404 gate."""
        drifted = TestClient(app, base_url="http://10.213.250.7:8888", client=(BOX, 50000))
        assert drifted.get("/health").status_code == 403
        assert drifted.get("/api/private", headers={"Authorization": f"Bearer {API_TOKEN}"}).status_code == 403
        r = drifted.post(coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"Bearer {_session_token()}"}, json={})
        assert r.status_code == 404

    def test_gate_address_from_outside_subnet_not_served(self, app):
        outside = _box(app, client_ip="172.26.0.1")
        assert outside.get("/health").status_code == 403
        r = outside.post(coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"Bearer {_session_token()}"}, json={})
        assert r.status_code == 404

    def test_lan_still_works_normally(self, app):
        assert _lan(app).get("/api/private", headers={"Authorization": f"Bearer {API_TOKEN}"}).status_code == 200
        assert _lan(app).get("/api/private").status_code == 401


class TestChatRoute:
    def test_disabled_gate_404(self, app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": _msgs()},
        )
        assert r.status_code == 404

    @pytest.mark.parametrize("auth", [None, "Bearer nope", f"Bearer {API_TOKEN}", "Basic abc"])
    def test_needs_a_coder_token(self, app, auth):
        headers = {"Authorization": auth} if auth else {}
        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=headers, json={"messages": _msgs()})
        assert r.status_code == 401

    def test_ok_non_stream(self, app, monkeypatch):
        fake = _ok_run(monkeypatch)
        token = _session_token()
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": _msgs(), "model": "x", "grammar": "g"},
        )
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "done"
        assert "grammar" not in fake.up and fake.up["model"] == "brain-model"
        row = coder_gate.lookup_token(token)
        assert (row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (1, 11, 3)

    def test_refused_requests_still_count(self, app):
        token = _session_token()
        bad = {"messages": [{"role": "developer", "content": "x"}]}
        _box(app).post(coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"Bearer {token}"}, json=bad)
        assert coder_gate.lookup_token(token)["requests"] == 1

    def test_repeated_refusals_revoke_the_token(self, app, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_REJECTIONS", 3)
        monkeypatch.setattr(coder_gate, "_rejections", {})
        token = _session_token()
        bad = {"messages": [{"role": "developer", "content": "x"}]}
        codes = [
            _box(app)
            .post(coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"Bearer {token}"}, json=bad)
            .status_code
            for _ in range(4)
        ]
        assert codes == [400, 400, 400, 401]
        assert coder_gate.lookup_token(token) is None

    def test_byte_budget(self, app, monkeypatch):
        _ok_run(monkeypatch)
        monkeypatch.setattr(coder_gate, "MAX_BYTES_PER_MINUTE", 3000)
        monkeypatch.setattr(coder_gate, "_bytes_seen", {})
        token = _session_token()
        body = {"messages": _msgs("x" * 1200)}
        codes = [
            _box(app)
            .post(coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"Bearer {token}"}, json=body)
            .status_code
            for _ in range(3)
        ]
        assert codes[:2] == [200, 200] and codes[2] == 429

    def test_ok_stream_replays_sse(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": _msgs(), "stream": True},
        )
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.text.endswith("data: [DONE]\n\n") and '"done"' in r.text

    def test_models(self, app):
        r = _box(app).get(coder_gate.GATE_MODELS_PATH, headers={"Authorization": f"Bearer {_session_token()}"})
        assert r.status_code == 200 and r.json()["data"][0]["id"] == "brain-model"
        assert _box(app).get(coder_gate.GATE_MODELS_PATH).status_code == 401

    def test_budget_exhausted(self, app, monkeypatch):
        _ok_run(monkeypatch)
        minted = coder_gate.mint_token("session", max_requests=1)
        coder_gate.count_request(minted["id"])
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {minted['token']}"},
            json={"messages": _msgs()},
        )
        assert r.status_code == 403 and r.json()["error"]["type"] == "budget_exhausted"

    def test_filter_refusal(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": [{"role": "developer", "content": "x"}]},
        )
        assert r.status_code == 400 and "bad_role" in r.json()["error"]["message"]

    def test_bad_json(self, app):
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}", "Content-Type": "application/json"},
            content=b"{not json",
        )
        assert r.status_code == 400

    def test_deeply_nested_json(self, app):
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}", "Content-Type": "application/json"},
            content=b"[" * 60000,
        )
        assert r.status_code == 400

    def test_usage_db_failure_still_returns_reply(self, app, monkeypatch):
        _ok_run(monkeypatch)

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(coder_gate, "record_usage", locked)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": _msgs()},
        )
        assert r.status_code == 200

    def test_body_cap(self, app, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_BODY_BYTES", 100)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": _msgs("x" * 500)},
        )
        assert r.status_code == 413

    @pytest.mark.parametrize(
        "exc,status,retry",
        [
            (coder_gate.GateBusy(), 429, "15"),
            (coder_gate.BrainUnavailable(), 503, "30"),
            (coder_gate.UpstreamError(400, "exceed_context_size_error"), 400, None),
            (coder_gate.UpstreamError(422), 400, None),
            (coder_gate.UpstreamError(500), 502, None),
            (coder_gate.UpstreamError(0), 502, None),
            (coder_gate.TokenRevoked(), 401, None),
            (KeyError("surprise"), 502, None),
        ],
        ids=["busy", "unavailable", "context", "client-4xx", "upstream-5xx", "broken-stream", "revoked", "internal"],
    )
    def test_error_mapping(self, app, monkeypatch, exc, status, retry):
        async def boom(up, still_usable=None):
            raise exc

        monkeypatch.setattr(coder_gate, "run_chat", boom)
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {_session_token()}"},
            json={"messages": _msgs()},
        )
        assert r.status_code == status
        assert r.headers.get("retry-after") == retry
        if isinstance(exc, coder_gate.UpstreamError) and exc.err_type:
            assert r.json()["error"]["type"] == "exceed_context_size_error"


class TestSessions:
    def _auth(self):
        return {"Authorization": f"Bearer {API_TOKEN}"}

    def test_mint_use_list_revoke(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _lan(app).post("/api/coder/sessions", headers=self._auth(), json={"label": "fix-it", "hours": 1})
        assert r.status_code == 201
        minted = r.json()
        assert minted["token"].startswith("jcs_") and minted["label"] == "fix-it"

        ok = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {minted['token']}"},
            json={"messages": _msgs()},
        )
        assert ok.status_code == 200

        listed = _lan(app).get("/api/coder/sessions", headers=self._auth()).json()["sessions"]
        assert [s["id"] for s in listed] == [minted["id"]]
        assert minted["token"] not in json.dumps(listed) and "token_sha256" not in json.dumps(listed)

        assert _lan(app).delete(f"/api/coder/sessions/{minted['id']}", headers=self._auth()).status_code == 200
        gone = _box(app).post(
            coder_gate.GATE_CHAT_PATH,
            headers={"Authorization": f"Bearer {minted['token']}"},
            json={"messages": _msgs()},
        )
        assert gone.status_code == 401
        assert _lan(app).delete(f"/api/coder/sessions/{minted['id']}", headers=self._auth()).status_code == 404

    def test_hours_clamped_to_setting(self, app):
        r = _lan(app).post("/api/coder/sessions", headers=self._auth(), json={"hours": 100})
        expires = datetime.fromisoformat(r.json()["expires_at"])
        assert expires.tzinfo is not None
        assert expires <= datetime.now(UTC) + timedelta(hours=4, minutes=1)

    @pytest.mark.parametrize("body", [{"label": "Bad Label"}, {"hours": "x"}, {"hours": True}, [1]])
    def test_bad_requests(self, app, body):
        assert _lan(app).post("/api/coder/sessions", headers=self._auth(), json=body).status_code == 400

    @pytest.mark.parametrize("raw", [b'{"hours": NaN}', b'{"hours": Infinity}', b'{"hours": -Infinity}'])
    def test_non_finite_hours(self, app, raw):
        r = _lan(app).post(
            "/api/coder/sessions", headers={**self._auth(), "Content-Type": "application/json"}, content=raw
        )
        assert r.status_code == 400

    def test_needs_api_token(self, app):
        assert _lan(app).post("/api/coder/sessions", json={}).status_code == 401

    def test_box_cannot_mint(self, app):
        r = _box(app).post("/api/coder/sessions", headers=self._auth(), json={})
        assert r.status_code == 403

    def test_disabled(self, app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        assert _lan(app).post("/api/coder/sessions", headers=self._auth(), json={}).status_code == 404
        assert _lan(app).get("/api/coder/sessions", headers=self._auth()).status_code == 404

    def test_bad_session_id(self, app):
        assert _lan(app).delete("/api/coder/sessions/nothex", headers=self._auth()).status_code == 400


# ----------------------------------------------------------------- Helios sleep refusal


class TestHeliosSleepRefusal:
    @pytest.fixture
    def helios(self, monkeypatch):
        from orchestrator import helios_power

        monkeypatch.setattr(settings, "helios_wake_enabled", True)
        monkeypatch.setattr(settings, "coder_enabled", True)
        monkeypatch.setattr(settings, "ha_url", "http://ha.test")
        monkeypatch.setattr(settings, "ha_token", "t" * 40)
        monkeypatch.setattr(settings, "helios_plug_entity", "switch.helios_plug")
        return helios_power

    async def test_refused_while_session_in_use(self, helios):
        minted = coder_gate.mint_token("session")
        coder_gate.record_usage(minted["id"], 1, 1)
        with respx.mock(assert_all_called=False) as router:
            turn_off = router.post("http://ha.test/api/services/switch/turn_off").mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["skipped"] == "coder_active" and not turn_off.called

    async def test_refused_while_request_in_flight(self, helios):
        with respx.mock(assert_all_called=False) as router, brain_slot.coder_request():
            turn_off = router.post("http://ha.test/api/services/switch/turn_off").mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["skipped"] == "coder_active" and not turn_off.called

    async def test_refused_while_request_admitted(self, helios):
        adm = coder_gate.admit("d" * 32)
        try:
            with respx.mock(assert_all_called=False) as router:
                turn_off = router.post("http://ha.test/api/services/switch/turn_off").mock(
                    return_value=httpx.Response(200)
                )
                result = await helios.sleep_helios()
            assert result["skipped"] == "coder_active" and not turn_off.called
        finally:
            adm.release()

    async def test_allowed_when_idle(self, helios):
        coder_gate.mint_token("session")  # minted but never used
        with respx.mock(assert_all_called=False) as router:
            turn_off = router.post("http://ha.test/api/services/switch/turn_off").mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["ok"] and turn_off.called

    async def test_coder_off_never_blocks(self, helios, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        with respx.mock(assert_all_called=False) as router, brain_slot.coder_request():
            router.post("http://ha.test/api/services/switch/turn_off").mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["ok"]

    async def test_tool_message(self, helios, monkeypatch):
        from orchestrator import tool_handlers

        async def refused():
            return {"ok": False, "skipped": "coder_active", "reason": "x"}

        monkeypatch.setattr(helios, "sleep_helios", refused)
        text = await tool_handlers._reg_helios_power({"action": "sleep"})
        assert "coding session" in text


# ----------------------------------------------------------------- startup + vision


class TestStartupCheck:
    def test_unbindable_address_turns_gate_off(self, monkeypatch):
        monkeypatch.setattr(settings, "coder_gate_address", "10.213.250.2")  # not this test container's
        coder_gate.startup_check()
        assert shared.CODER_ENABLED is False

    def test_bindable_address_keeps_gate_on(self, monkeypatch):
        monkeypatch.setattr(settings, "coder_gate_address", "127.0.0.1")
        coder_gate.startup_check()
        assert shared.CODER_ENABLED is True


def test_vision_call_counts_as_chat(monkeypatch):
    from unittest.mock import MagicMock

    gauge = MagicMock()
    gauge._value.get.return_value = 0
    monkeypatch.setattr(brain_slot, "ACTIVE_REQUESTS", gauge)
    monkeypatch.setattr(brain_slot, "_interactive_in_flight", 0)
    monkeypatch.setattr("orchestrator.research_helper._active", 0)
    assert not _REAL_CHAT_BUSY()
    with brain_slot.interactive_call():
        assert _REAL_CHAT_BUSY()
    assert not _REAL_CHAT_BUSY()


# ----------------------------------------------------------------- config


class TestConfig:
    def _settings(self, monkeypatch, **env):
        from orchestrator.config import Settings

        for key in [k for k in os.environ if k.startswith("CODER_")]:
            monkeypatch.delenv(key)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Settings(_env_file=None)

    def test_defaults(self, monkeypatch):
        s = self._settings(monkeypatch)
        assert s.coder_enabled is False and s.coder_gate_address == "10.213.250.2"

    @pytest.mark.parametrize(
        "env,guarded",
        [
            ({"CODER_GATE_ADDRESS": "8.8.8.8"}, ("10.213.250.0/24", "8.8.8.8")),
            ({"CODER_GATE_ADDRESS": "10.213.250.0"}, ("10.213.250.0/24", "10.213.250.0")),
            ({"CODER_GATE_ADDRESS": "10.9.9.9"}, ("10.213.250.0/24", "10.9.9.9")),
            ({"CODER_SUBNET": "8.8.8.0/24", "CODER_GATE_ADDRESS": "8.8.8.2"}, ("8.8.8.0/24", "8.8.8.2")),
            ({"CODER_SUBNET": "127.0.0.0/8", "CODER_GATE_ADDRESS": "127.0.0.2"}, ("127.0.0.0/8", "127.0.0.2")),
            (
                {"CODER_SUBNET": "169.254.0.0/16", "CODER_GATE_ADDRESS": "169.254.0.2"},
                ("169.254.0.0/16", "169.254.0.2"),
            ),
            ({"CODER_GATE_ADDRESS": "not-an-ip"}, ("10.213.250.0/24", "10.213.250.2")),
        ],
        ids=["public", "network-addr", "outside", "public-subnet", "loopback", "link-local", "garbage"],
    )
    def test_bad_layout_disables_but_keeps_guard(self, monkeypatch, env, guarded):
        """The gate goes off; the guard keeps watching what compose built from
        the same variables, or the default layout if they don't parse."""
        s = self._settings(monkeypatch, CODER_ENABLED="true", **env)
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == guarded

    @pytest.mark.parametrize(
        "box_range", ["10.213.250.0/25", "10.9.0.0/25", "junk"], ids=["covers-gate", "outside", "junk"]
    )
    def test_bad_box_range_disables(self, monkeypatch, box_range):
        s = self._settings(monkeypatch, CODER_ENABLED="true", CODER_BOX_RANGE=box_range)
        assert s.coder_enabled is False

    def test_refused_but_parseable_layout_keeps_guard_on_it(self, monkeypatch):
        """Docker accepts a CGNAT subnet we refuse: the guard must watch the real one."""
        s = self._settings(
            monkeypatch,
            CODER_ENABLED="true",
            CODER_SUBNET="100.64.0.0/24",
            CODER_GATE_ADDRESS="100.64.0.2",
            CODER_BOX_RANGE="100.64.0.128/25",
        )
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == ("100.64.0.0/24", "100.64.0.2")

    def test_custom_layout_kept(self, monkeypatch):
        s = self._settings(
            monkeypatch,
            CODER_ENABLED="true",
            CODER_SUBNET="10.77.0.0/24",
            CODER_GATE_ADDRESS="10.77.0.2",
            CODER_BOX_RANGE="10.77.0.128/25",
        )
        assert s.coder_enabled is True and s.coder_gate_address == "10.77.0.2"

    def test_non_finite_values_reset(self, monkeypatch):
        s = self._settings(monkeypatch, CODER_IDLE_SECONDS="nan", CODER_SESSION_HOURS="inf")
        assert s.coder_idle_seconds == 20.0 and s.coder_session_hours == 4.0

    def test_clamps(self, monkeypatch):
        s = self._settings(
            monkeypatch,
            CODER_MAX_TOKENS_PER_REQUEST="999999",
            CODER_SESSION_HOURS="100",
            CODER_MAX_WAIT_SECONDS="1",
            CODER_IDLE_SECONDS="-5",
        )
        assert s.coder_max_tokens_per_request == 8192
        assert s.coder_session_hours == 12.0
        assert s.coder_max_wait_seconds == 5.0
        assert s.coder_idle_seconds == 0.0
