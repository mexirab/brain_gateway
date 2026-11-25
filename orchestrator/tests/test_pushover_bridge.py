"""
Tests for F-013 Pushover push bridge.

Covers:
  - pushover_manager._sanitize_body (pure unit)
  - pushover_manager._strip_credentials (pure unit)
  - pushover_manager.deliver_via_pushover (network-mocked via respx)
  - pushover_manager.deliver_pushover_confirm (network-mocked via respx)
  - config.validate_pushover_config model_validator (auto-disable + clamp)
  - api_routes: widened /ack + /snooze feature gate (the confirm wiring both
    routes fire is in test_ntfy_feedback.TestConfirmWiring)
  - tool_handlers.deliver_reminder_job: unconditional dispatch via asyncio.create_task

Mocks settings via monkeypatch on the live module-level singleton (same pattern
as test_ntfy_feedback / test_paperless_bridge). Mocks HTTP via respx so no real
Pushover calls leak out of the container.

Metric-consistency invariant: every deliver_* exit path must bump
PUSHOVER_PUSH_TOTAL{result, kind, reason} exactly once. Asserted on each
branch below via before/after delta reads on the 3-label counter.
"""

from __future__ import annotations

import logging
import os
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from httpx import Response

from orchestrator import state_store
from orchestrator.config import Settings, settings
from orchestrator.metrics import PUSHOVER_PUSH_TOTAL
from orchestrator.pushover_manager import (
    _sanitize_body,
    _strip_credentials,
    deliver_pushover_confirm,
    deliver_via_pushover,
)
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_delivery import NTFY_SECRET, drain_tasks

_USER_KEY = "u" * 30
_APP_TOKEN = "a" * 30
PUSHOVER_BASE = "http://pushover.test"


@pytest.fixture
def pushover_on(monkeypatch):
    """Enable Pushover on the live settings singleton with known creds."""
    monkeypatch.setattr(settings, "pushover_enabled", True, raising=False)
    monkeypatch.setattr(settings, "pushover_user_key", _USER_KEY, raising=False)
    monkeypatch.setattr(settings, "pushover_app_token", _APP_TOKEN, raising=False)
    monkeypatch.setattr(settings, "pushover_default_priority", 0, raising=False)
    monkeypatch.setattr(settings, "pushover_api_url", f"{PUSHOVER_BASE}/1/messages.json", raising=False)
    monkeypatch.setattr(settings, "pushover_upload_timeout_seconds", 10, raising=False)
    # _build_callback_url needs these even when the actual ntfy push is off.
    monkeypatch.setattr(settings, "ntfy_hmac_secret", NTFY_SECRET, raising=False)
    monkeypatch.setattr(settings, "ntfy_callback_base_url", "http://helios.test:8888", raising=False)
    monkeypatch.setattr(settings, "ntfy_ack_exp_seconds", 1800, raising=False)
    return settings


def _pushes(result: str, kind: str, reason: str) -> float:
    return metric_value(PUSHOVER_PUSH_TOTAL, result=result, kind=kind, reason=reason)


async def _send(kind: str, **kw):
    if kind == "reminder":
        return await deliver_via_pushover("r1", "t", **kw)
    return await deliver_pushover_confirm("t", "m", reminder_id="abc-123")


def _form(route) -> dict:
    """The form-encoded body of the first call, decoded to {field: value}."""
    body = route.calls[0].request.content.decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


# ===========================================================================
# _sanitize_body / _strip_credentials (pure unit)
# ===========================================================================


@pytest.mark.parametrize(
    "text,kwargs,expected",
    [
        pytest.param("", {}, "", id="empty"),
        pytest.param(None, {}, "", id="none"),
        pytest.param("hello world", {}, "hello world", id="plain"),
        # \x01 (SOH) and \x1f (US) are below 0x20, \x7f (DEL) is >=0x7F →
        # stripped; \t and \n are whitelisted → kept.
        pytest.param("ok\x01bad\x1fstuff\x7fend\ttab\nline", {}, "okbadstuffend\ttab\nline", id="control-chars"),
        pytest.param("x" * 500, {}, "x" * 300, id="default-cap-300"),
        pytest.param("x" * 50, {"limit": 10}, "x" * 10, id="custom-limit"),
    ],
)
def test_sanitize_body(text, kwargs, expected):
    assert _sanitize_body(text, **kwargs) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
        # <8 char values aren't matched — avoids eating real English.
        pytest.param("token=abc1234", id="short-token-kept"),
        # The \b word boundary keeps 'user=' inside bigger words safe.
        pytest.param("the username field is fine", id="english-word-kept"),
    ],
)
def test_strip_credentials_leaves_non_credentials(text):
    assert _strip_credentials(text) == text


@pytest.mark.parametrize(
    "text,secrets,kept",
    [
        pytest.param(
            "error token=abcdef1234567890 body",
            ["abcdef1234567890"],
            ["token=<redacted>", "error", "body"],
            id="token",
        ),
        pytest.param("user=abc12345XYZ in response", ["abc12345XYZ"], ["user=<redacted>"], id="user"),
        pytest.param(
            "debug: token=1234567890abcdef and user=abcdef1234567890 end",
            ["1234567890abcdef", "abcdef1234567890"],
            [],
            id="both",
        ),
    ],
)
def test_strip_credentials_redacts(text, secrets, kept):
    out = _strip_credentials(text)
    assert not any(s in out for s in secrets)
    assert all(k in out for k in kept)
    assert out.count("<redacted>") == len(secrets)


# ===========================================================================
# deliver_via_pushover / deliver_pushover_confirm — respx-mocked HTTP
# ===========================================================================


@pytest.mark.parametrize(
    "kind,enabled,blank,reason",
    [
        pytest.param("reminder", False, None, "disabled", id="reminder-disabled"),
        pytest.param("reminder", True, "pushover_user_key", "missing_user_key", id="reminder-no-user-key"),
        pytest.param("reminder", True, "pushover_app_token", "missing_app_token", id="reminder-no-app-token"),
        pytest.param("confirm", False, None, "disabled", id="confirm-disabled"),
        pytest.param("confirm", True, "pushover_user_key", "missing_credentials", id="confirm-no-creds"),
    ],
)
async def test_skipped_without_http(pushover_on, monkeypatch, kind, enabled, blank, reason):
    monkeypatch.setattr(settings, "pushover_enabled", enabled, raising=False)
    if blank:
        monkeypatch.setattr(settings, blank, "", raising=False)
    before = _pushes("skipped", kind, reason)
    with respx.mock:  # no routes registered — any HTTP would blow up
        result = await _send(kind)
    assert result == {"success": False, "skipped": True, "reason": reason}
    assert _pushes("skipped", kind, reason) == before + 1


class TestDeliverViaPushover:
    async def test_success_200_shape_and_body(self, pushover_on):
        before = _pushes("ok", "reminder", "ok")
        with respx.mock(base_url=PUSHOVER_BASE) as mock:
            route = mock.post("/1/messages.json").mock(
                return_value=Response(200, json={"status": 1, "request": "req-xyz-42"})
            )
            result = await deliver_via_pushover("r1", "drink water")
        assert route.called
        assert result["success"] is True
        assert result["request_id"] == "req-xyz-42"
        assert isinstance(result["latency_ms"], int)
        assert _pushes("ok", "reminder", "ok") == before + 1

        form = _form(route)
        assert form["token"] == _APP_TOKEN
        assert form["user"] == _USER_KEY
        assert form["title"] == "Jess reminder"
        assert form["html"] == "1"
        # Snooze anchor embedded in the message body; Done is the tap url.
        assert "Snooze 10 min" in form["message"]
        assert form["url"]
        assert form["url_title"] == "✓ Done"

    async def test_html_escape_prevents_injection(self, pushover_on):
        """Prompt-injected <a> / <script> in reminder text must be HTML-escaped
        before embedding, otherwise the attacker's anchor would render as a
        tappable link in Pushover's html=1 notification."""
        evil = '<a href="http://attacker/">click</a> <script>x()</script>'
        with respx.mock(base_url=PUSHOVER_BASE) as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1, "request": "r"}))
            await deliver_via_pushover("r1", evil)
        message = _form(route)["message"]
        # Decoded form field: the attacker's tags arrive as entities...
        assert '&lt;a href="http://attacker/"&gt;click&lt;/a&gt; &lt;script&gt;x()&lt;/script&gt;' in message
        assert "<script>" not in message
        # ...and the only live anchor is the trusted snooze link we build.
        assert message.count("<a ") == 1
        assert "Snooze 10 min</a>" in message

    @pytest.mark.parametrize("given,sent", [(-999, "-2"), (999, "2")], ids=["low", "high"])
    async def test_priority_clamped(self, pushover_on, given, sent):
        with respx.mock(base_url=PUSHOVER_BASE) as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1, "request": "r"}))
            await deliver_via_pushover("r1", "t", priority=given)
        assert _form(route)["priority"] == sent

    async def test_4xx_bumps_http_4xx_and_sanitizes_body(self, pushover_on):
        """400 response with leaking 'token=...' must be scrubbed from the result body."""
        before = _pushes("fail", "reminder", "http_4xx")
        leaky_body = "error: token=abcdef1234567890 rejected\x01bad"
        with respx.mock(base_url=PUSHOVER_BASE) as mock:
            mock.post("/1/messages.json").mock(return_value=Response(400, text=leaky_body))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert result["status_code"] == 400
        # Credentials redacted AND control chars stripped
        assert "abcdef1234567890" not in result["body"]
        assert "token=<redacted>" in result["body"]
        assert "\x01" not in result["body"]
        assert _pushes("fail", "reminder", "http_4xx") == before + 1


@pytest.mark.parametrize(
    "kind,mock_kwargs,reason,expect",
    [
        pytest.param("reminder", {"return_value": Response(503, text="overloaded")}, "http_5xx", 503, id="r-5xx"),
        pytest.param("reminder", {"side_effect": httpx.TimeoutException("slow")}, "timeout", "Timeout", id="r-timeout"),
        pytest.param(
            "reminder", {"side_effect": httpx.ConnectError("dns")}, "connect_error", "Connect", id="r-connect"
        ),
        pytest.param("reminder", {"side_effect": ValueError("weird")}, "other", "ValueError", id="r-other"),
        pytest.param("confirm", {"return_value": Response(400, text="rejected")}, "http_4xx", 400, id="c-4xx"),
        pytest.param("confirm", {"return_value": Response(503, text="boom")}, "http_5xx", 503, id="c-5xx"),
        pytest.param("confirm", {"side_effect": httpx.TimeoutException("slow")}, "timeout", "Timeout", id="c-timeout"),
        pytest.param("confirm", {"side_effect": httpx.ConnectError("dns")}, "connect_error", "Connect", id="c-connect"),
    ],
)
async def test_send_failure_is_returned_not_raised_and_counted(pushover_on, caplog, kind, mock_kwargs, reason, expect):
    """Every failure path returns a result (never raises) and bumps
    fail/<reason> once; confirm failures log rid=<id> for Loki correlation."""
    before = _pushes("fail", kind, reason)
    with (
        respx.mock(base_url=PUSHOVER_BASE) as mock,
        caplog.at_level(logging.WARNING, logger="orchestrator.pushover_manager"),
    ):
        mock.post("/1/messages.json").mock(**mock_kwargs)
        result = await _send(kind)
    assert result["success"] is False
    if isinstance(expect, int):
        assert result["status_code"] == expect
    else:
        assert expect in result["error"]
    assert _pushes("fail", kind, reason) == before + 1
    if kind == "confirm":
        assert any("rid=abc-123" in r.getMessage() for r in caplog.records)


async def test_confirm_happy_path_200_priority_and_no_url(pushover_on):
    """Confirm: priority=-1 (quiet), NO url field, NO html=1, generic title."""
    before = _pushes("ok", "confirm", "ok")
    with respx.mock(base_url=PUSHOVER_BASE) as mock:
        route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1}))
        result = await deliver_pushover_confirm("✓ Logged", "drink water\n(water logged)", "r1")
    assert result == {"success": True}
    assert _pushes("ok", "confirm", "ok") == before + 1

    form = _form(route)
    assert form["priority"] == "-1"
    assert "url" not in form
    assert "html" not in form
    # Title is generic — the action name must NOT be in it; the body may carry it.
    assert form["title"] == "✓ Logged"
    assert "water" in form["message"].lower()


# ===========================================================================
# config.validate_pushover_config — auto-disable + clamp semantics
# ===========================================================================


@pytest.mark.parametrize(
    "env,attr,expected,logged",
    [
        pytest.param(
            {"PUSHOVER_ENABLED": "true", "PUSHOVER_USER_KEY": "short", "PUSHOVER_APP_TOKEN": "valid-long-token-here"},
            "pushover_enabled",
            False,
            True,
            id="short-user-key-disables",
        ),
        pytest.param(
            {"PUSHOVER_ENABLED": "true", "PUSHOVER_USER_KEY": "valid-long-user-key", "PUSHOVER_APP_TOKEN": "short"},
            "pushover_enabled",
            False,
            None,
            id="short-app-token-disables",
        ),
        pytest.param(
            {
                "PUSHOVER_ENABLED": "true",
                "PUSHOVER_USER_KEY": "valid-long-user-key",
                "PUSHOVER_APP_TOKEN": "valid-long-token-here",
            },
            "pushover_enabled",
            True,
            None,
            id="valid-creds-stay-on",
        ),
        pytest.param(
            {"PUSHOVER_ENABLED": "false", "PUSHOVER_DEFAULT_PRIORITY": "-999"},
            "pushover_default_priority",
            -2,
            None,
            id="priority-clamped-low",
        ),
        pytest.param(
            {"PUSHOVER_ENABLED": "false", "PUSHOVER_DEFAULT_PRIORITY": "999"},
            "pushover_default_priority",
            2,
            None,
            id="priority-clamped-high",
        ),
        # Feature off: missing creds must not matter (and never raise).
        pytest.param(
            {"PUSHOVER_ENABLED": "false", "PUSHOVER_USER_KEY": "", "PUSHOVER_APP_TOKEN": ""},
            "pushover_enabled",
            False,
            None,
            id="disabled-missing-creds",
        ),
    ],
)
def test_config_validator(caplog, env, attr, expected, logged):
    """Auto-disable logs an ERROR, never raises. A fresh Settings() with the
    process env cleared and no env file, so the host can't change the outcome."""
    env = {**env, "HA_TOKEN": "x", "API_TOKEN": "x", "PIHOLE_PASSWORD": "x"}
    with (
        patch.dict(os.environ, env, clear=True),
        caplog.at_level(logging.ERROR, logger="orchestrator.config"),
    ):
        s = Settings(_env_file=None)
    assert getattr(s, attr) == expected
    if logged:
        assert any("PUSHOVER_ENABLED=true" in r.getMessage() for r in caplog.records)


# ===========================================================================
# api_routes — widened /ack + /snooze feature gate
# ===========================================================================


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/reminder/ack/abc?sig={sig}&exp={exp}", id="ack"),
        pytest.param("/api/reminder/snooze/abc?sig={sig}&exp={exp}&minutes=10", id="snooze"),
    ],
)
def test_routes_process_when_only_pushover_on(
    delivery_api_client, delivery_ntfy_off, pushover_on, tmp_db, delivery_clean_scheduler, path
):
    """ntfy off, pushover on → the feature gate (404 only when BOTH are off,
    see test_ntfy_feedback) lets the request through: a bad sig is 403, not 404."""
    r = delivery_api_client.post(path.format(sig="0" * 32, exp=int(time.time()) + 300))
    assert r.status_code == 403
    assert r.json()["error"] == "bad_signature"


# ===========================================================================
# tool_handlers.deliver_reminder_job — unconditional Pushover dispatch
# ===========================================================================


@pytest.mark.parametrize(
    "enabled,rid,text",
    [pytest.param(True, "r1", "take meds", id="enabled"), pytest.param(False, "r2", "water time", id="disabled")],
)
async def test_deliver_reminder_job_dispatches_pushover(tmp_db, delivery_quiet, monkeypatch, enabled, rid, text):
    """target='phone' → deliver_via_pushover is fired via asyncio.create_task,
    even with pushover disabled: the gate is inside deliver_via_pushover, so a
    runtime config flip can't strand a call. Patched where the inner helper
    imports it at call time."""
    from orchestrator.tool_handlers import deliver_reminder_job

    monkeypatch.setattr(settings, "pushover_enabled", enabled, raising=False)
    state_store.save_reminder(rid, text, "2026-04-20T09:00:00", target="phone")

    mock_push = AsyncMock(return_value={"success": True, "request_id": "r"})
    with patch("orchestrator.pushover_manager.deliver_via_pushover", mock_push):
        await deliver_reminder_job(rid)
        await drain_tasks()

    mock_push.assert_called_once()
    assert mock_push.call_args.args[:2] == (rid, text)
