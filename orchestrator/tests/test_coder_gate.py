"""Coder-box gate (coder_gate.py + routes_coder.py + the coder-net guard).

Covers the token store, the request/response filters, the upstream call
(respx), the wait / run-alone / preempt loop (cancellation included: an outer
cancel must stay a cancel, and the lock + in-flight mark must be released),
vision entering ``brain_slot.interactive_call`` (and preempting a running coder
attempt), the coder-net rate limit, the routes through the real
BearerAuthMiddleware (requests "arrive" on the coder-net address via the
TestClient base_url, which becomes scope["server"]) incl. failure paths and log
hygiene, sessions, the Helios sleep refusal, ``startup_check`` and the config
validator.

``brain_slot.chat_busy`` is the real one (with the chat gauge and lookup
counter held at zero), so interactive calls count. Only tests that touch the
token store get a fresh database (``tmp_db``: the ``app`` fixture, the
Helios fixture and the token tests); its init_db() costs ~1.5 s a test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
HA_TURN_OFF = "http://ha.test/api/services/switch/turn_off"


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch):
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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _session():
    return coder_gate.mint_token("session")


def _session_token():
    return _session()["token"]


# ----------------------------------------------------------------- tokens


@pytest.mark.usefixtures("tmp_db")
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
        _set_column(minted["id"], "expires_at", _utc(timedelta(seconds=-1)))
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

    def test_list_skips_expired_but_unrevoked(self):
        live, expired = _session(), _session()
        _set_column(expired["id"], "expires_at", _utc(timedelta(seconds=-1)))
        assert [r["id"] for r in coder_gate.list_tokens("session")] == [live["id"]]

    def test_list_newest_first_and_by_kind(self):
        ids = [_session()["id"] for _ in range(3)]
        job = coder_gate.mint_token("job")
        for n, token_id in enumerate(ids):
            _set_column(token_id, "created_at", _utc(timedelta(minutes=n - 10)))
        assert [r["id"] for r in coder_gate.list_tokens("session")] == ids[::-1]
        assert [r["id"] for r in coder_gate.list_tokens("job")] == [job["id"]]

    def test_active_use(self):
        minted = coder_gate.mint_token("session")
        assert not coder_gate.active_use_exists()
        coder_gate.record_usage(minted["id"], 1, 1)
        assert coder_gate.active_use_exists()
        coder_gate.revoke_token(minted["id"])
        assert not coder_gate.active_use_exists()

    def test_active_use_window(self):
        minted = _session()
        _set_column(minted["id"], "last_used_at", _utc(timedelta(seconds=-(coder_gate.ACTIVE_WITHIN_S + 60))))
        assert not coder_gate.active_use_exists()
        assert coder_gate.active_use_exists(within_s=coder_gate.ACTIVE_WITHIN_S + 120)
        _set_column(minted["id"], "last_used_at", _utc())
        _set_column(minted["id"], "expires_at", _utc(timedelta(seconds=-1)))
        assert not coder_gate.active_use_exists()  # used just now, but expired

    def test_old_tokens_pruned_on_mint(self):
        old = coder_gate.mint_token("session")
        _set_column(old["id"], "expires_at", _utc(timedelta(days=-40)))
        coder_gate.mint_token("session")
        with state_store.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM coder_tokens WHERE id = ?", (old["id"],)).fetchone()[0] == 0

    @pytest.mark.parametrize("label", ["Bad", "has space", "-lead", "x" * 41, "../x"])
    def test_bad_labels_refused(self, label):
        with pytest.raises(ValueError):
            coder_gate.mint_token("session", label)

    def test_stamps_are_utc(self):
        minted = _session()
        assert minted["expires_at"].endswith("+00:00")
        coder_gate.count_request(minted["id"])
        row = coder_gate.lookup_token(minted["token"])
        assert row["created_at"].endswith("+00:00") and row["last_used_at"].endswith("+00:00")

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

    @pytest.mark.usefixtures("tmp_db")
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
        "body,key,expect",
        [
            pytest.param({"max_tokens": 99999}, "max_tokens", 4096, id="max-clamped"),
            pytest.param({"max_tokens": 0}, "max_tokens", 1, id="max-floor"),
            pytest.param({"max_completion_tokens": 100}, "max_tokens", 100, id="max-completion"),
            pytest.param({"temperature": 1.7}, "temperature", 1.0, id="temp-high"),
            pytest.param({"temperature": -1}, "temperature", 0.0, id="temp-low"),
            pytest.param({"top_p": 5}, "top_p", 1.0, id="top-p-high"),
            pytest.param({"max_tokens": 100.9}, "max_tokens", 100, id="float-max"),
            pytest.param({"max_tokens": 50, "max_completion_tokens": 999}, "max_tokens", 50, id="max-tokens-wins"),
            pytest.param({"top_p": 0}, "top_p", 0.01, id="top-p-floor"),
            pytest.param({"temperature": 0.25}, "temperature", 0.25, id="temp-kept"),
        ],
    )
    def test_numbers(self, body, key, expect):
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), **body}, 4096)
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

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param('"max_tokens": ' + "9" * 400, id="huge-integer"),
            pytest.param('"temperature": NaN', id="raw-nan"),
        ],
    )
    def test_raw_json_numbers_refused(self, raw):
        body = json.loads('{"messages": [{"role": "user", "content": "x"}], ' + raw + "}")
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

    @pytest.mark.parametrize(
        "messages,reason",
        [
            pytest.param([], "bad_messages", id="empty"),
            pytest.param("hi", "bad_messages", id="not-list"),
            pytest.param([{"role": "developer", "content": "x"}], "bad_role", id="role"),
            pytest.param([{"role": "user", "content": 5}], "bad_messages", id="content-type"),
            pytest.param(
                [{"role": "user", "content": "x", "tool_calls": [{"function": {"name": "a", "arguments": "{}"}}]}],
                "bad_messages",
                id="user-tool-calls",
            ),
            pytest.param(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "bad name", "arguments": "{}"}}],
                    }
                ],
                "bad_messages",
                id="tool-name",
            ),
            pytest.param([{"role": "tool", "tool_call_id": "x" * 300, "content": "r"}], "bad_messages", id="long-id"),
            pytest.param([{"role": "user", "content": "x", "name": 5}], "bad_messages", id="name-int"),
            pytest.param(
                [{"role": "assistant", "content": "x", "reasoning_content": ["r"]}], "bad_messages", id="reasoning-list"
            ),
            pytest.param(
                [{"role": "assistant", "content": None, "tool_calls": {"function": {"name": "a"}}}],
                "bad_messages",
                id="calls-dict",
            ),
            pytest.param(
                [{"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "a", "arguments": {}}}]}],
                "bad_messages",
                id="args-dict",
            ),
            pytest.param(
                [{"role": "assistant", "content": None, "tool_calls": [{"id": 5, "function": {"name": "a"}}]}],
                "bad_messages",
                id="id-int",
            ),
            pytest.param(
                [{"role": "assistant", "content": None, "tool_calls": ["call"]}], "bad_messages", id="call-str"
            ),
            pytest.param(["not-a-message"], "bad_messages", id="message-str"),
        ],
    )
    def test_bad_messages(self, messages, reason):
        with pytest.raises(coder_gate.GateRejected) as exc:
            coder_gate.sanitize_request({"messages": messages}, 4096)
        assert exc.value.reason == reason

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

    def test_too_many_tool_calls(self):
        call = {"id": "c", "function": {"name": "a", "arguments": "{}"}}
        msgs = [
            {"role": "assistant", "content": None, "tool_calls": [call] * (coder_gate.MAX_TOOL_CALLS_PER_MESSAGE + 1)}
        ]
        with pytest.raises(coder_gate.GateRejected, match="bad_messages"):
            coder_gate.sanitize_request({"messages": msgs}, 4096)

    def test_not_a_dict(self):
        with pytest.raises(coder_gate.GateRejected, match="bad_json"):
            coder_gate.sanitize_request([1, 2], 4096)

    @pytest.mark.parametrize(
        "stop,ok",
        [
            pytest.param("END", True, id="str"),
            pytest.param(["a", "b"], True, id="list"),
            pytest.param(["a"] * 5, False, id="too-many"),
            pytest.param([""], False, id="empty-item"),
            pytest.param(["x" * 65], False, id="too-long"),
            pytest.param(5, False, id="int"),
            pytest.param("", False, id="empty-str"),
            pytest.param({"a": 1}, False, id="dict"),
            pytest.param([1], False, id="list-int"),
        ],
    )
    def test_stop(self, stop, ok):
        if ok:
            up, _ = coder_gate.sanitize_request({"messages": _msgs(), "stop": stop}, 4096)
            assert isinstance(up["stop"], list)
        else:
            with pytest.raises(coder_gate.GateRejected, match="bad_params"):
                coder_gate.sanitize_request({"messages": _msgs(), "stop": stop}, 4096)


def _tool(name="read_file", params=None, description="d", **fn):
    function = {"name": name, "description": description, "parameters": params or {"type": "object"}, **fn}
    return {"type": "function", "function": function}


def _obj(**props):
    return {"type": "object", "properties": props, "required": list(props)}


def _prop(**node):
    return {"type": "object", "properties": {"v": node}}


def _nested_schema(depth):
    node: dict = {"type": "object"}
    root = node
    for _ in range(depth):
        child: dict = {"type": "object"}
        node["properties"] = {"x": child}
        node = child
    return root


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
        "depth,ok",
        [
            pytest.param(coder_gate.MAX_SCHEMA_DEPTH, True, id="at-the-limit"),
            pytest.param(coder_gate.MAX_SCHEMA_DEPTH + 2, False, id="too-deep"),
        ],
    )
    def test_schema_depth(self, depth, ok):
        body = {"messages": _msgs(), "tools": [_tool(params=_nested_schema(depth))]}
        if ok:
            coder_gate.sanitize_request(body, 4096)
        else:
            with pytest.raises(coder_gate.GateRejected, match="schema_bounds"):
                coder_gate.sanitize_request(body, 4096)

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
        "tools,reason",
        [
            pytest.param("x", "bad_tools", id="not-list"),
            pytest.param([{"type": "web_search"}], "bad_tools", id="not-function"),
            pytest.param([_tool("bad name")], "bad_tools", id="name"),
            pytest.param(
                [{"type": "function", "function": {"name": "a", "description": 5}}], "bad_tools", id="description-int"
            ),
            pytest.param(
                [{"type": "function", "function": {"name": "a", "parameters": "object"}}],
                "bad_tools",
                id="parameters-str",
            ),
            pytest.param([{"type": "function", "function": "a"}], "bad_tools", id="function-str"),
            pytest.param(["a"], "bad_tools", id="tool-str"),
            pytest.param([_tool() for _ in range(coder_gate.MAX_TOOLS + 1)], "too_many_tools", id="count"),
            pytest.param(
                [_tool(description="x" * (coder_gate.MAX_TOOL_SCHEMA_CHARS + 1))], "schema_too_large", id="one-too-big"
            ),
            pytest.param(
                [_tool(f"t{i}", description="x" * 12_000) for i in range(6)], "schema_too_large", id="total-too-big"
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "array", "items": {"type": "string"}, "minItems": 5000}))],
                "schema_bounds",
                id="min-items",
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "string", "maxLength": 100000}))], "schema_bounds", id="max-length"
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "string", "pattern": "^((a?){40}){40}$"}))],
                "schema_keyword",
                id="pattern-bomb",
            ),
            pytest.param([_tool(params=_obj(v={"type": "string", "format": "uri"}))], "schema_keyword", id="format"),
            pytest.param(
                [_tool(params=_obj(v={"$ref": "#/$defs/a"}) | {"$defs": {"a": {"type": "string"}}})],
                "schema_keyword",
                id="ref-defs",
            ),
            pytest.param(
                [_tool(params=_obj(v={"anyOf": [{"type": "string"}, {"type": "string"}]}))],
                "schema_keyword",
                id="any-of",
            ),
            pytest.param([_tool(params=_obj(v={"type": "string", "oneOf": []}))], "schema_keyword", id="one-of"),
            pytest.param([_tool(params=_obj(v={}))], "schema_keyword", id="untyped"),
            pytest.param(
                [_tool(params=_obj(v={"type": "array", "items": [{"type": "string"}]}))],
                "schema_keyword",
                id="tuple-items",
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "string", "enum": [str(i) for i in range(65)]}))],
                "schema_bounds",
                id="big-enum",
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "object", "additionalProperties": {"type": "string"}}))],
                "schema_keyword",
                id="additional-schema",
            ),
            pytest.param(
                [_tool(params=_obj(v={"type": "integer", "minimum": "1"}))], "schema_bounds", id="string-minimum"
            ),
            pytest.param(
                [_tool(params={"type": "array", "items": {"type": "string"}})], "schema_keyword", id="array-root"
            ),
            pytest.param(
                [_tool(params=_prop(type="string", maxLength=coder_gate.MAX_SCHEMA_BOUND + 1))],
                "schema_bounds",
                id="bound-over",
            ),
            pytest.param([_tool(params=_prop(type="string", minLength=-1))], "schema_bounds", id="bound-negative"),
            pytest.param(
                [_tool(params=_prop(type="array", items={"type": "string"}, maxItems=True))],
                "schema_bounds",
                id="bound-bool",
            ),
            pytest.param([_tool(params=_prop(type="string", maxLength=5.0))], "schema_bounds", id="bound-float"),
            pytest.param([_tool(params=_prop(type="number", minimum=float("nan")))], "schema_bounds", id="minimum-nan"),
            pytest.param([_tool(params=_prop(type="number", maximum=True))], "schema_bounds", id="maximum-bool"),
            pytest.param([_tool(params=_prop(type="string", enum=[]))], "schema_bounds", id="enum-empty"),
            pytest.param([_tool(params=_prop(type="string", enum=[{"a": 1}]))], "schema_bounds", id="enum-dict"),
            pytest.param([_tool(params=_prop(type="string", enum="a"))], "schema_bounds", id="enum-str"),
            pytest.param([_tool(params=_prop(type=[]))], "schema_keyword", id="type-empty"),
            pytest.param([_tool(params=_prop(type=["string", "date"]))], "schema_keyword", id="type-unknown"),
            pytest.param([_tool(params=_prop(type="string", default=["a"]))], "schema_keyword", id="default-list"),
            pytest.param(
                [_tool(params=_prop(type="string", description=5))], "schema_keyword", id="description-int-prop"
            ),
            pytest.param([_tool(params=_prop(type="string", title=None))], "schema_keyword", id="title-none"),
            pytest.param(
                [_tool(params={"type": "object", "properties": [], "required": []})],
                "schema_keyword",
                id="properties-list",
            ),
            pytest.param(
                [_tool(params={"type": "object", "properties": {"": {"type": "string"}}})],
                "schema_keyword",
                id="prop-name-empty",
            ),
            pytest.param(
                [_tool(params={"type": "object", "properties": {"x" * 65: {"type": "string"}}})],
                "schema_keyword",
                id="prop-name-long",
            ),
            pytest.param([_tool(params={"type": "object", "required": "x"})], "schema_keyword", id="required-str"),
            pytest.param([_tool(params={"type": "object", "required": [1]})], "schema_keyword", id="required-int"),
            pytest.param(
                [_tool(params={"type": "object", "additionalProperties": "no"})], "schema_keyword", id="additional-str"
            ),
            pytest.param(
                [_tool(params={"type": "object", "properties": {"v": "string"}})], "schema_keyword", id="child-not-dict"
            ),
        ],
    )
    def test_refused(self, tools, reason):
        with pytest.raises(coder_gate.GateRejected) as exc:
            coder_gate.sanitize_request({"messages": _msgs(), "tools": tools}, 4096)
        assert exc.value.reason == reason

    @pytest.mark.parametrize("choice", ["sometimes", {"type": "function", "function": {"name": "x y"}}, 3])
    def test_bad_tool_choice(self, choice):
        with pytest.raises(coder_gate.GateRejected, match="bad_params"):
            coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool()], "tool_choice": choice}, 4096)

    def test_tool_choice_without_tools_is_not_forwarded(self):
        up, dropped = coder_gate.sanitize_request({"messages": _msgs(), "tool_choice": "required"}, 4096)
        assert "tool_choice" not in up and "tool_choice" not in dropped

    def test_named_tool_choice_is_rebuilt(self):
        choice = {"type": "function", "function": {"name": "read_file", "extra": 1}, "x": 2}
        up, _ = coder_gate.sanitize_request({"messages": _msgs(), "tools": [_tool()], "tool_choice": choice}, 4096)
        assert up["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}


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


def _sse(*lines):
    return "".join(line + "\n\n" for line in lines)


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

    async def test_non_json_error_body_keeps_status(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(return_value=httpx.Response(429, text="<html>busy</html>"))
        with pytest.raises(coder_gate.UpstreamError) as exc:
            await coder_gate._stream_once({"messages": []})
        assert (exc.value.status, exc.value.err_type) == (429, None)

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

    async def test_non_dict_choice_is_an_upstream_error(self, upstream):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(
            return_value=httpx.Response(200, text=_sse('data: {"choices": ["x"]}', "data: [DONE]"))
        )
        with pytest.raises(coder_gate.UpstreamError):
            await coder_gate._stream_once({"messages": []})

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

    @pytest.mark.parametrize(
        "error,expect",
        [
            (httpx.ConnectError("down"), coder_gate.BrainUnavailable),
            (httpx.ConnectTimeout("t"), coder_gate.BrainUnavailable),
            (httpx.ReadTimeout("t"), coder_gate.UpstreamError),
            (httpx.RemoteProtocolError("t"), coder_gate.UpstreamError),
        ],
        ids=["unreachable", "connect-timeout", "read-timeout", "protocol"],
    )
    async def test_transport_errors(self, upstream, error, expect):
        upstream.post(f"{MODEL_URL}/chat/completions").mock(side_effect=error)
        with pytest.raises(expect):
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

    async def test_supersede_cancels_the_running_request_and_frees_the_lock(self, monkeypatch):
        adm, route = await self._running_admission(monkeypatch)
        newer = coder_gate.admit("a" * 32)
        with pytest.raises(coder_gate.Superseded):
            await asyncio.wait_for(route, 2)
        assert not brain_slot.coder_busy()

        async def fast(up):
            return _reply("newer")

        monkeypatch.setattr(coder_gate, "_stream_once", fast)
        chunks, _ = await asyncio.wait_for(newer.run({}), 2)
        assert chunks[0]["choices"][0]["delta"]["content"] == "newer"
        adm.release()
        assert coder_gate.requests_admitted() == 1  # the old release leaves the newer slot alone
        newer.release()
        assert coder_gate.requests_admitted() == 0


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
        assert not brain_slot.chat_busy()
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


# ----------------------------------------------------------------- routes + guard


@pytest.fixture
def app(tmp_db, monkeypatch):
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


def _box(app, client_ip=BOX, server=GATE):
    return TestClient(app, base_url=f"http://{server}:8888", client=(client_ip, 50000))


def _lan(app, client_ip="10.0.0.50"):
    return TestClient(app, base_url="http://10.0.0.248:8888", client=(client_ip, 50000))


def _post(client, token, body=None, **kw):
    return client.post(
        coder_gate.GATE_CHAT_PATH, headers=_auth(token), json={"messages": _msgs()} if body is None else body, **kw
    )


async def _post_async(client, token):
    return await client.post(coder_gate.GATE_CHAT_PATH, headers=_auth(token), json={"messages": _msgs()})


def _ok_run(monkeypatch, usage=None):
    """Stub run_chat with a finished reply; ``fake.up`` is the body the route forwarded."""

    async def fake(up, still_usable=None):
        fake.up = up
        return _reply(usage=usage)

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

    @pytest.mark.usefixtures("tmp_db")
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
            r = _post(_box(app), _session_token())
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
        r = _box(app).get(path, headers=_auth(API_TOKEN))
        assert r.status_code == 403

    @pytest.mark.parametrize(
        "path",
        ["/api/audio/clip.wav", "/api/reminder/ack/abc", "/api/coder/sessions/" + "0" * 32, "/metrics"],
        ids=["audio", "reminder-ack", "session-delete", "metrics"],
    )
    def test_public_paths_closed_from_coder_net(self, app, path):
        assert _box(app).get(path).status_code == 403

    def test_cors_preflight_not_exempt_on_gate(self, app):
        r = _box(app).options("/api/private", headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"})
        assert r.status_code == 403

    def test_guard_stays_on_with_the_gate_off(self, app, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        assert _box(app).get("/api/private", headers=_auth(API_TOKEN)).status_code == 403

    def test_blocked_requests_are_counted(self, app):
        from orchestrator.metrics import CODER_GUARD_BLOCKED_TOTAL

        before = CODER_GUARD_BLOCKED_TOTAL._value.get()
        _box(app).get("/api/private")
        _box(app).get(coder_gate.GATE_MODELS_PATH)  # the gate itself is not "blocked"
        assert CODER_GUARD_BLOCKED_TOTAL._value.get() == before + 1

    @pytest.mark.parametrize("path", [coder_gate.GATE_CHAT_PATH, coder_gate.GATE_MODELS_PATH])
    def test_gate_paths_404_off_the_gate(self, app, path):
        token = _session_token()
        for auth in (API_TOKEN, token):
            r = _lan(app).request(
                "POST" if path.endswith("completions") else "GET",
                path,
                headers=_auth(auth),
                json={},
            )
            assert r.status_code == 404

    def test_newline_suffix_from_lan_does_not_reach_the_gate(self, app, monkeypatch):
        """'/…/completions\\n' is not in GATE_PATHS, so the middleware doesn't
        404 it, but the router's '$' still matches it: the route's own
        arrived_on_gate check must refuse it."""
        called = []

        async def fake(up, still_usable=None):
            called.append(1)
            return _reply()

        monkeypatch.setattr(coder_gate, "run_chat", fake)
        token = _session_token()
        for auth in (API_TOKEN, token):
            r = _lan(app).post(coder_gate.GATE_CHAT_PATH + "%0A", headers=_auth(auth), json={"messages": _msgs()})
            assert r.status_code in (401, 404)
        assert not called

    def test_drift_fails_closed(self, app, monkeypatch):
        """Compose address and setting out of step: a box (subnet client) on
        another orchestrator address still reaches nothing but a 404 gate."""
        drifted = _box(app, server="10.213.250.7")
        assert drifted.get("/health").status_code == 403
        assert drifted.get("/api/private", headers=_auth(API_TOKEN)).status_code == 403
        assert _post(drifted, _session_token(), {}).status_code == 404

    def test_gate_address_from_outside_subnet_not_served(self, app):
        outside = _box(app, client_ip="172.26.0.1")
        assert outside.get("/health").status_code == 403
        assert _post(outside, _session_token(), {}).status_code == 404

    def test_lan_still_works_normally(self, app):
        assert _lan(app).get("/api/private", headers=_auth(API_TOKEN)).status_code == 200
        assert _lan(app).get("/api/private").status_code == 401


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


@pytest.mark.parametrize(
    "where,method,path,body",
    [
        pytest.param("box", "POST", coder_gate.GATE_CHAT_PATH, {"messages": _msgs()}, id="chat"),
        pytest.param("box", "GET", coder_gate.GATE_MODELS_PATH, None, id="models"),
        pytest.param("lan", "POST", "/api/coder/sessions", {}, id="mint"),
        pytest.param("lan", "GET", "/api/coder/sessions", None, id="list"),
        pytest.param("lan", "DELETE", "/api/coder/sessions/{id}", None, id="revoke"),
    ],
)
def test_gate_off_answers_404(app, monkeypatch, where, method, path, body):
    minted = _session()
    monkeypatch.setattr(shared, "CODER_ENABLED", False)
    client, token = (_box(app), minted["token"]) if where == "box" else (_lan(app), API_TOKEN)
    kw = {"json": body} if body is not None else {}
    r = client.request(method, path.format(id=minted["id"]), headers=_auth(token), **kw)
    assert r.status_code == 404
    assert coder_gate.lookup_token(minted["token"]) is not None


class TestChatRoute:
    @pytest.mark.parametrize("auth", [None, "Bearer nope", f"Bearer {API_TOKEN}", "Basic abc"])
    def test_needs_a_coder_token(self, app, auth):
        headers = {"Authorization": auth} if auth else {}
        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=headers, json={"messages": _msgs()})
        assert r.status_code == 401

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER"], ids=["lower", "upper"])
    def test_bearer_scheme_case_and_spaces(self, app, monkeypatch, scheme):
        _ok_run(monkeypatch)
        token = _session_token()
        r = _box(app).post(
            coder_gate.GATE_CHAT_PATH, headers={"Authorization": f"{scheme}   {token} "}, json={"messages": _msgs()}
        )
        assert r.status_code == 200

    def test_ok_non_stream(self, app, monkeypatch):
        fake = _ok_run(monkeypatch)
        token = _session_token()
        r = _post(_box(app), token, {"messages": _msgs(), "model": "x", "grammar": "g"})
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "done"
        assert "grammar" not in fake.up and fake.up["model"] == "brain-model"
        row = coder_gate.lookup_token(token)
        assert (row["requests"], row["prompt_tokens"], row["completion_tokens"]) == (1, 11, 3)

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

    def test_ok_stream_replays_sse(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _post(_box(app), _session_token(), {"messages": _msgs(), "stream": True})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.text.endswith("data: [DONE]\n\n") and '"done"' in r.text

    def test_non_boolean_stream_gets_json(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _post(_box(app), _session_token(), {"messages": _msgs(), "stream": "true"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")

    def test_models(self, app):
        r = _box(app).get(coder_gate.GATE_MODELS_PATH, headers=_auth(_session_token()))
        assert r.status_code == 200 and r.json()["data"][0]["id"] == "brain-model"
        assert _box(app).get(coder_gate.GATE_MODELS_PATH).status_code == 401

    def test_refused_requests_still_count(self, app):
        token = _session_token()
        _post(_box(app), token, {"messages": [{"role": "developer", "content": "x"}]})
        assert coder_gate.lookup_token(token)["requests"] == 1

    def test_repeated_refusals_revoke_the_token(self, app, monkeypatch):
        monkeypatch.setattr(coder_gate, "MAX_REJECTIONS", 3)
        token = _session_token()
        bad = {"messages": [{"role": "developer", "content": "x"}]}
        assert [_post(_box(app), token, bad).status_code for _ in range(4)] == [400, 400, 400, 401]
        assert coder_gate.lookup_token(token) is None

    def test_byte_budget(self, app, monkeypatch):
        _ok_run(monkeypatch)
        monkeypatch.setattr(coder_gate, "MAX_BYTES_PER_MINUTE", 3000)
        token = _session_token()
        body = {"messages": _msgs("x" * 1200)}
        codes = [_post(_box(app), token, body).status_code for _ in range(3)]
        assert codes[:2] == [200, 200] and codes[2] == 429

    def test_last_request_of_the_budget_is_served(self, app, monkeypatch):
        async def stream(up):
            return _reply()

        monkeypatch.setattr(coder_gate, "_stream_once", stream)
        minted = coder_gate.mint_token("session", max_requests=2)
        answers = [_post(_box(app), minted["token"]) for _ in range(3)]
        assert [r.status_code for r in answers] == [200, 200, 403]
        assert answers[2].json()["error"]["type"] == "budget_exhausted"

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
        r = _post(_box(app), _session_token())
        assert r.status_code == 401 and "revoked" in r.json()["error"]["message"]

    def test_filter_refusal_carries_no_content(self, app, caplog):
        with caplog.at_level(logging.INFO, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session_token(), {"messages": [{"role": "developer", "content": "x"}]})
            secret = _post(_box(app), _session_token(), {"messages": [{"role": "SECRET-ROLE", "content": "x"}]})
        assert r.status_code == 400 and "bad_role" in r.json()["error"]["message"]
        assert secret.status_code == 400 and "SECRET-ROLE" not in secret.text and "SECRET-ROLE" not in caplog.text

    @pytest.mark.parametrize(
        "content",
        [pytest.param(b"{not json", id="bad-json"), pytest.param(b"[" * 60000, id="deeply-nested")],
    )
    def test_unparseable_body(self, app, content):
        headers = {**_auth(_session_token()), "Content-Type": "application/json"}
        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=headers, content=content)
        assert r.status_code == 400

    def test_empty_body_is_bad_json(self, app):
        r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=_auth(_session_token()), content=b"")
        assert r.status_code == 400 and "bad_json" in r.json()["error"]["message"]

    def test_usage_db_failure_still_returns_reply(self, app, monkeypatch):
        _ok_run(monkeypatch)

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(coder_gate, "record_usage", locked)
        assert _post(_box(app), _session_token()).status_code == 200

    def test_count_failure_is_logged_and_request_served(self, app, monkeypatch, caplog):
        _ok_run(monkeypatch)

        def locked(token_id):
            raise sqlite3.OperationalError("database is locked near SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "count_request", locked)
        with caplog.at_level(logging.ERROR, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session_token())
        assert r.status_code == 200
        lines = [rec.getMessage() for rec in caplog.records if "not counted" in rec.getMessage()]
        assert len(lines) == 1 and "OperationalError" in lines[0]
        assert "SECRET-CONTENT" not in caplog.text

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
            r = _post(client, _session_token())
        assert r.status_code == 200

    @pytest.mark.parametrize("chunked", [pytest.param(False, id="whole"), pytest.param(True, id="chunked")])
    def test_body_cap(self, app, monkeypatch, chunked):
        monkeypatch.setattr(coder_gate, "MAX_BODY_BYTES", 100)

        def chunks():
            for _ in range(10):
                yield b"x" * 50

        if chunked:
            r = _box(app).post(coder_gate.GATE_CHAT_PATH, headers=_auth(_session_token()), content=chunks())
        else:
            r = _post(_box(app), _session_token(), {"messages": _msgs("x" * 500)})
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
        r = _post(_box(app), _session_token())
        assert r.status_code == status
        assert r.headers.get("retry-after") == retry
        if isinstance(exc, coder_gate.UpstreamError) and exc.err_type:
            assert r.json()["error"]["type"] == "exceed_context_size_error"

    def test_internal_error_log_has_no_exception_text(self, app, monkeypatch, caplog):
        async def boom(up, still_usable=None):
            raise KeyError("prompt text SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "run_chat", boom)
        with caplog.at_level(logging.INFO, logger="orchestrator.routes_coder"):
            r = _post(_box(app), _session_token(), {"messages": _msgs("SECRET-CONTENT")})
        assert r.status_code == 502 and "SECRET-CONTENT" not in r.text
        assert "KeyError" in caplog.text and f"{Path(__file__).name}:" in caplog.text
        assert "SECRET-CONTENT" not in caplog.text

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


def _lan_post(app, body=None, raw=None):
    if raw is not None:
        return _lan(app).post("/api/coder/sessions", headers=_auth(API_TOKEN), content=raw)
    return _lan(app).post("/api/coder/sessions", headers=_auth(API_TOKEN), json=body)


class TestSessions:
    def test_mint_use_list_revoke(self, app, monkeypatch):
        _ok_run(monkeypatch)
        r = _lan_post(app, {"label": "fix-it", "hours": 1})
        assert r.status_code == 201
        minted = r.json()
        assert minted["token"].startswith("jcs_") and minted["label"] == "fix-it"

        assert _post(_box(app), minted["token"]).status_code == 200

        listed = _lan(app).get("/api/coder/sessions", headers=_auth(API_TOKEN)).json()["sessions"]
        assert [s["id"] for s in listed] == [minted["id"]]
        assert minted["token"] not in json.dumps(listed) and "token_sha256" not in json.dumps(listed)

        assert _lan(app).delete(f"/api/coder/sessions/{minted['id']}", headers=_auth(API_TOKEN)).status_code == 200
        assert _post(_box(app), minted["token"]).status_code == 401
        assert _lan(app).delete(f"/api/coder/sessions/{minted['id']}", headers=_auth(API_TOKEN)).status_code == 404

    def test_defaults(self, app):
        r = _lan_post(app, raw=b"")
        assert r.status_code == 201 and r.json()["label"] == ""
        expires = datetime.fromisoformat(r.json()["expires_at"])
        assert expires >= datetime.now(UTC) + timedelta(hours=3, minutes=59)

    def test_hours_clamped_to_setting(self, app):
        expires = datetime.fromisoformat(_lan_post(app, {"hours": 100}).json()["expires_at"])
        assert expires.tzinfo is not None
        assert expires <= datetime.now(UTC) + timedelta(hours=4, minutes=1)

    def test_tiny_hours_clamped_up(self, app):
        expires = datetime.fromisoformat(_lan_post(app, {"hours": 0.01}).json()["expires_at"])
        now = datetime.now(UTC)
        assert now + timedelta(minutes=14) <= expires <= now + timedelta(minutes=16)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"label": "Bad Label"}, id="bad-label"),
            pytest.param({"hours": "x"}, id="hours-str"),
            pytest.param({"hours": True}, id="hours-bool"),
            pytest.param([1], id="list"),
            pytest.param({"label": "a", "role": "admin"}, id="extra-key"),
            pytest.param({"hours": 0}, id="zero"),
            pytest.param({"hours": -1}, id="negative"),
            pytest.param({"hours": 1001}, id="too-many"),
            pytest.param({"label": 5}, id="label-int"),
            pytest.param("x", id="string"),
        ],
    )
    def test_bad_requests(self, app, body):
        assert _lan_post(app, body).status_code == 400

    @pytest.mark.parametrize("raw", [b'{"hours": NaN}', b'{"hours": Infinity}', b'{"hours": -Infinity}'])
    def test_non_finite_hours(self, app, raw):
        r = _lan(app).post(
            "/api/coder/sessions", headers={**_auth(API_TOKEN), "Content-Type": "application/json"}, content=raw
        )
        assert r.status_code == 400

    def test_needs_api_token(self, app):
        assert _lan(app).post("/api/coder/sessions", json={}).status_code == 401

    @pytest.mark.parametrize(
        "method,path",
        [
            pytest.param("POST", "/api/coder/sessions", id="mint"),
            pytest.param("GET", "/api/coder/sessions", id="list"),
            pytest.param("DELETE", "/api/coder/sessions/{id}", id="revoke"),
        ],
    )
    def test_box_cannot_manage_sessions(self, app, method, path):
        minted = _session()
        kw = {"json": {}} if method == "POST" else {}
        r = _box(app).request(method, path.format(id=minted["id"]), headers=_auth(API_TOKEN), **kw)
        assert r.status_code == 403
        assert coder_gate.lookup_token(minted["token"]) is not None

    def test_session_routes_ignore_job_tokens(self, app):
        job = coder_gate.mint_token("job")
        assert _lan(app).get("/api/coder/sessions", headers=_auth(API_TOKEN)).json()["sessions"] == []
        assert _lan(app).delete(f"/api/coder/sessions/{job['id']}", headers=_auth(API_TOKEN)).status_code == 404
        assert coder_gate.lookup_token(job["token"]) is not None

    def test_bad_session_id(self, app):
        assert _lan(app).delete("/api/coder/sessions/nothex", headers=_auth(API_TOKEN)).status_code == 400


# ----------------------------------------------------------------- Helios sleep refusal


class TestHeliosSleepRefusal:
    @pytest.fixture
    def helios(self, tmp_db, monkeypatch):
        from orchestrator import helios_power

        monkeypatch.setattr(settings, "helios_wake_enabled", True)
        monkeypatch.setattr(settings, "coder_enabled", True)
        monkeypatch.setattr(settings, "ha_url", "http://ha.test")
        monkeypatch.setattr(settings, "ha_token", "t" * 40)
        monkeypatch.setattr(settings, "helios_plug_entity", "switch.helios_plug")
        return helios_power

    @staticmethod
    def _coder_active(stack, signal):
        if signal in ("recent", "all"):
            coder_gate.record_usage(coder_gate.mint_token("session")["id"], 1, 1)
        if signal in ("in_flight", "all"):
            stack.enter_context(brain_slot.coder_request())
        if signal in ("admitted", "all"):
            stack.callback(coder_gate.admit("d" * 32).release)

    @pytest.mark.parametrize("signal", ["recent", "in_flight", "admitted"])
    async def test_refused_while_the_coder_uses_the_brain(self, helios, signal):
        with contextlib.ExitStack() as stack:
            self._coder_active(stack, signal)
            router = stack.enter_context(respx.mock(assert_all_called=False))
            turn_off = router.post(HA_TURN_OFF).mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["skipped"] == "coder_active" and not turn_off.called

    async def test_allowed_when_idle(self, helios):
        coder_gate.mint_token("session")  # minted but never used
        with respx.mock(assert_all_called=False) as router:
            turn_off = router.post(HA_TURN_OFF).mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["ok"] and turn_off.called

    async def test_coder_off_never_blocks(self, helios, monkeypatch):
        monkeypatch.setattr(shared, "CODER_ENABLED", False)
        with contextlib.ExitStack() as stack:
            self._coder_active(stack, "all")
            router = stack.enter_context(respx.mock(assert_all_called=False))
            router.post(HA_TURN_OFF).mock(return_value=httpx.Response(200))
            result = await helios.sleep_helios()
        assert result["ok"]

    async def test_lookup_error_allows_sleep(self, helios, monkeypatch, caplog):
        def broken(within_s=coder_gate.ACTIVE_WITHIN_S):
            raise sqlite3.OperationalError("locked SECRET-CONTENT")

        monkeypatch.setattr(coder_gate, "active_use_exists", broken)
        with caplog.at_level(logging.WARNING, logger="orchestrator.helios_power"):
            assert await helios._coder_using_brain() is False
        assert "OperationalError" in caplog.text and "SECRET-CONTENT" not in caplog.text

    async def test_tool_message(self, helios, monkeypatch):
        from orchestrator import tool_handlers

        async def refused():
            return {"ok": False, "skipped": "coder_active", "reason": "x"}

        monkeypatch.setattr(helios, "sleep_helios", refused)
        text = await tool_handlers._reg_helios_power({"action": "sleep"})
        assert "coding session" in text


# ----------------------------------------------------------------- startup_check


class TestStartupCheck:
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

    @pytest.mark.parametrize(
        "address,still_on",
        [pytest.param("127.0.0.1", True, id="bindable"), pytest.param("192.0.2.77", False, id="not-ours")],
    )
    def test_real_bind_probe(self, monkeypatch, address, still_on):
        """The real probe, end to end; 192.0.2.77 is TEST-NET-1, never an address of ours."""
        monkeypatch.setattr(settings, "coder_gate_address", address)
        assert coder_gate._address_is_local(address) is still_on
        coder_gate.startup_check()
        assert shared.CODER_ENABLED is still_on


# ----------------------------------------------------------------- config


def _coder_settings(monkeypatch, **env):
    from orchestrator.config import Settings

    for key in [k for k in os.environ if k.startswith("CODER_")]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


class TestConfig:
    def test_defaults(self, monkeypatch):
        s = _coder_settings(monkeypatch)
        assert s.coder_enabled is False and s.coder_gate_address == "10.213.250.2"

    @pytest.mark.parametrize(
        "env,guarded",
        [
            ({"CODER_GATE_ADDRESS": "8.8.8.8"}, ("10.213.250.0/24", "8.8.8.8")),
            ({"CODER_GATE_ADDRESS": "10.213.250.0"}, ("10.213.250.0/24", "10.213.250.0")),
            ({"CODER_GATE_ADDRESS": "10.213.250.255"}, ("10.213.250.0/24", "10.213.250.255")),
            ({"CODER_GATE_ADDRESS": "10.9.9.9"}, ("10.213.250.0/24", "10.9.9.9")),
            ({"CODER_SUBNET": "8.8.8.0/24", "CODER_GATE_ADDRESS": "8.8.8.2"}, ("8.8.8.0/24", "8.8.8.2")),
            ({"CODER_SUBNET": "127.0.0.0/8", "CODER_GATE_ADDRESS": "127.0.0.2"}, ("127.0.0.0/8", "127.0.0.2")),
            (
                {"CODER_SUBNET": "169.254.0.0/16", "CODER_GATE_ADDRESS": "169.254.0.2"},
                ("169.254.0.0/16", "169.254.0.2"),
            ),
            ({"CODER_GATE_ADDRESS": "not-an-ip"}, ("10.213.250.0/24", "10.213.250.2")),
        ],
        ids=["public", "network-addr", "broadcast", "outside", "public-subnet", "loopback", "link-local", "garbage"],
    )
    def test_bad_layout_disables_but_keeps_guard(self, monkeypatch, env, guarded):
        """The gate goes off; the guard keeps watching what compose built from
        the same variables, or the default layout if they don't parse."""
        s = _coder_settings(monkeypatch, CODER_ENABLED="true", **env)
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == guarded

    def test_bad_layout_is_logged(self, monkeypatch, caplog):
        with caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            _coder_settings(monkeypatch, CODER_ENABLED="true", CODER_GATE_ADDRESS="8.8.8.8")
        assert "coder gate disabled" in caplog.text

    @pytest.mark.parametrize(
        "box_range", ["10.213.250.0/25", "10.9.0.0/25", "junk"], ids=["covers-gate", "outside", "junk"]
    )
    def test_bad_box_range_disables(self, monkeypatch, box_range):
        s = _coder_settings(monkeypatch, CODER_ENABLED="true", CODER_BOX_RANGE=box_range)
        assert s.coder_enabled is False

    def test_box_range_outside_subnet_with_gate_off_keeps_guard(self, monkeypatch):
        s = _coder_settings(monkeypatch, CODER_BOX_RANGE="10.9.0.0/25")
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == ("10.213.250.0/24", "10.213.250.2")

    def test_refused_but_parseable_layout_keeps_guard_on_it(self, monkeypatch):
        """Docker accepts a CGNAT subnet we refuse: the guard must watch the real one."""
        s = _coder_settings(
            monkeypatch,
            CODER_ENABLED="true",
            CODER_SUBNET="100.64.0.0/24",
            CODER_GATE_ADDRESS="100.64.0.2",
            CODER_BOX_RANGE="100.64.0.128/25",
        )
        assert s.coder_enabled is False
        assert (s.coder_subnet, s.coder_gate_address) == ("100.64.0.0/24", "100.64.0.2")

    @pytest.mark.parametrize("pad", [pytest.param("", id="plain"), pytest.param(" ", id="whitespace-normalized")])
    def test_custom_layout_kept(self, monkeypatch, pad):
        s = _coder_settings(
            monkeypatch,
            CODER_ENABLED="true",
            CODER_SUBNET=f"{pad}10.77.0.0/24{pad}",
            CODER_GATE_ADDRESS=f"{pad}10.77.0.2{pad}",
            CODER_BOX_RANGE=f"{pad}10.77.0.128/25{pad}",
        )
        assert s.coder_enabled is True
        assert (s.coder_subnet, s.coder_gate_address) == ("10.77.0.0/24", "10.77.0.2")

    def test_non_finite_values_reset(self, monkeypatch):
        s = _coder_settings(monkeypatch, CODER_IDLE_SECONDS="nan", CODER_SESSION_HOURS="inf")
        assert s.coder_idle_seconds == 20.0 and s.coder_session_hours == 4.0

    def test_clamps(self, monkeypatch):
        s = _coder_settings(
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

    def test_integer_clamps_keep_their_type(self, monkeypatch):
        s = _coder_settings(
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
