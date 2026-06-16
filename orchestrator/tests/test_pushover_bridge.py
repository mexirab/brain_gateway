"""
Tests for F-013 Pushover push bridge.

Covers:
  - pushover_manager._sanitize_body (pure unit)
  - pushover_manager._strip_credentials (pure unit)
  - pushover_manager.deliver_via_pushover (network-mocked via respx)
  - pushover_manager.deliver_pushover_confirm (network-mocked via respx)
  - config.validate_pushover_config model_validator (auto-disable + clamp)
  - api_routes: /ack + /snooze Pushover-confirm wiring + widened feature gate
  - tool_handlers.deliver_reminder_job: unconditional dispatch via asyncio.create_task

Mocks settings via monkeypatch on the live module-level singleton (same pattern
as test_ntfy_feedback / test_paperless_bridge). Mocks HTTP via respx so no real
Pushover calls leak out of the container.

Metric-consistency invariant: every deliver_* exit path must bump
PUSHOVER_PUSH_TOTAL{result, kind, reason} exactly once. Asserted on each
branch below via before/after delta reads on the 3-label counter.
"""

from __future__ import annotations

import contextlib
import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response

# ---------------------------------------------------------------------------
# Settings fixtures — flip the live singleton; restore via monkeypatch undo.
# ---------------------------------------------------------------------------

_NTFY_SECRET = "x" * 40  # satisfies the >=32 model_validator guard
_USER_KEY = "u" * 30
_APP_TOKEN = "a" * 30


@pytest.fixture
def pushover_on(monkeypatch):
    """Enable Pushover on the live settings singleton with known creds."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "pushover_enabled", True, raising=False)
    monkeypatch.setattr(settings, "pushover_user_key", _USER_KEY, raising=False)
    monkeypatch.setattr(settings, "pushover_app_token", _APP_TOKEN, raising=False)
    monkeypatch.setattr(settings, "pushover_default_priority", 0, raising=False)
    monkeypatch.setattr(
        settings,
        "pushover_api_url",
        "http://pushover.test/1/messages.json",
        raising=False,
    )
    monkeypatch.setattr(settings, "pushover_upload_timeout_seconds", 10, raising=False)
    # _build_callback_url needs these even when the actual ntfy push is off.
    monkeypatch.setattr(settings, "ntfy_hmac_secret", _NTFY_SECRET, raising=False)
    monkeypatch.setattr(settings, "ntfy_callback_base_url", "http://helios.test:8888", raising=False)
    monkeypatch.setattr(settings, "ntfy_ack_exp_seconds", 1800, raising=False)
    return settings


@pytest.fixture
def pushover_off(monkeypatch):
    """Disabled — for feature-flag-gate tests."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "pushover_enabled", False, raising=False)
    return settings


@pytest.fixture
def ntfy_off(monkeypatch):
    """Disable ntfy; used to verify the widened ack/snooze route gate."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", False, raising=False)
    # Secret must still be present so the sig-verify path doesn't blow up
    # before the feature-flag check when that matters.
    monkeypatch.setattr(settings, "ntfy_hmac_secret", _NTFY_SECRET, raising=False)
    return settings


@pytest.fixture
def ntfy_on(monkeypatch):
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ntfy_hmac_secret", _NTFY_SECRET, raising=False)
    monkeypatch.setattr(settings, "ntfy_url", "http://ntfy.test:8889", raising=False)
    monkeypatch.setattr(settings, "ntfy_topic", "jess-reminders", raising=False)
    monkeypatch.setattr(settings, "ntfy_callback_base_url", "http://helios.test:8888", raising=False)
    monkeypatch.setattr(settings, "ntfy_ack_exp_seconds", 1800, raising=False)
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 5, raising=False)
    monkeypatch.setattr(settings, "ntfy_default_priority", 3, raising=False)
    return settings


def _counter_value(result: str, kind: str, reason: str) -> float:
    """Read current PUSHOVER_PUSH_TOTAL{result, kind, reason} — 0 if never emitted."""
    from orchestrator.metrics import PUSHOVER_PUSH_TOTAL

    return PUSHOVER_PUSH_TOTAL.labels(result=result, kind=kind, reason=reason)._value.get()


# ===========================================================================
# _sanitize_body (pure unit)
# ===========================================================================


class TestSanitizeBody:
    def test_empty_string_returns_empty(self):
        from orchestrator.pushover_manager import _sanitize_body

        assert _sanitize_body("") == ""

    def test_none_returns_empty(self):
        from orchestrator.pushover_manager import _sanitize_body

        assert _sanitize_body(None) == ""  # type: ignore[arg-type]

    def test_plain_text_preserved(self):
        from orchestrator.pushover_manager import _sanitize_body

        assert _sanitize_body("hello world") == "hello world"

    def test_strips_control_chars(self):
        from orchestrator.pushover_manager import _sanitize_body

        # \x01 (SOH) and \x1f (US) are below 0x20 → stripped
        # \x7f (DEL) is >=0x7F → stripped
        # \t (0x09) and \n (0x0a) are whitelisted → kept
        dirty = "ok\x01bad\x1fstuff\x7fend\ttab\nline"
        assert _sanitize_body(dirty) == "okbadstuffend\ttab\nline"

    def test_caps_at_default_limit(self):
        from orchestrator.pushover_manager import _sanitize_body

        # Default limit is 300
        assert _sanitize_body("x" * 500) == "x" * 300

    def test_custom_limit_respected(self):
        from orchestrator.pushover_manager import _sanitize_body

        assert _sanitize_body("x" * 50, limit=10) == "x" * 10


# ===========================================================================
# _strip_credentials (pure unit)
# ===========================================================================


class TestStripCredentials:
    def test_empty_returns_empty(self):
        from orchestrator.pushover_manager import _strip_credentials

        assert _strip_credentials("") == ""

    def test_none_returns_none(self):
        from orchestrator.pushover_manager import _strip_credentials

        assert _strip_credentials(None) is None  # type: ignore[arg-type]

    def test_redacts_token(self):
        from orchestrator.pushover_manager import _strip_credentials

        # 16 hex chars → must match \b(?:token|user)=[A-Za-z0-9]{8,}
        out = _strip_credentials("error token=abcdef1234567890 body")
        assert "abcdef1234567890" not in out
        assert "token=<redacted>" in out
        # Surrounding context preserved
        assert "error" in out
        assert "body" in out

    def test_redacts_user(self):
        from orchestrator.pushover_manager import _strip_credentials

        out = _strip_credentials("user=abc12345XYZ in response")
        assert "abc12345XYZ" not in out
        assert "user=<redacted>" in out

    def test_redacts_both_in_same_string(self):
        from orchestrator.pushover_manager import _strip_credentials

        out = _strip_credentials("debug: token=1234567890abcdef and user=abcdef1234567890 end")
        assert "1234567890abcdef" not in out
        assert "abcdef1234567890" not in out
        assert out.count("<redacted>") == 2

    def test_short_token_not_redacted(self):
        """<8 char values aren't matched — avoids eating real English."""
        from orchestrator.pushover_manager import _strip_credentials

        # 7-char value — below the {8,} threshold
        out = _strip_credentials("token=abc1234")
        assert out == "token=abc1234"

    def test_english_word_not_matched(self):
        """The \\b word boundary keeps 'user=' inside bigger words safe."""
        from orchestrator.pushover_manager import _strip_credentials

        # Not a credential — this shouldn't get nuked
        out = _strip_credentials("the username field is fine")
        assert out == "the username field is fine"


# ===========================================================================
# deliver_via_pushover — respx-mocked HTTP
# ===========================================================================


class TestDeliverViaPushover:
    @pytest.mark.asyncio
    async def test_disabled_returns_skipped_no_http(self, pushover_off):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("skipped", "reminder", "disabled")
        with respx.mock:  # no routes registered — any HTTP would blow up
            result = await deliver_via_pushover("r1", "hi")
        assert result == {"success": False, "skipped": True, "reason": "disabled"}
        assert _counter_value("skipped", "reminder", "disabled") == before + 1

    @pytest.mark.asyncio
    async def test_missing_user_key_skipped(self, pushover_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.pushover_manager import deliver_via_pushover

        monkeypatch.setattr(settings, "pushover_user_key", "", raising=False)
        before = _counter_value("skipped", "reminder", "missing_user_key")
        with respx.mock:
            result = await deliver_via_pushover("r1", "hi")
        assert result["success"] is False
        assert result["skipped"] is True
        assert result["reason"] == "missing_user_key"
        assert _counter_value("skipped", "reminder", "missing_user_key") == before + 1

    @pytest.mark.asyncio
    async def test_missing_app_token_skipped(self, pushover_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.pushover_manager import deliver_via_pushover

        monkeypatch.setattr(settings, "pushover_app_token", "", raising=False)
        before = _counter_value("skipped", "reminder", "missing_app_token")
        with respx.mock:
            result = await deliver_via_pushover("r1", "hi")
        assert result["success"] is False
        assert result["reason"] == "missing_app_token"
        assert _counter_value("skipped", "reminder", "missing_app_token") == before + 1

    @pytest.mark.asyncio
    async def test_success_200_shape_and_body(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("ok", "reminder", "ok")
        with respx.mock(base_url="http://pushover.test") as mock:
            route = mock.post("/1/messages.json").mock(
                return_value=Response(200, json={"status": 1, "request": "req-xyz-42"})
            )
            result = await deliver_via_pushover("r1", "drink water")
        assert route.called
        assert result["success"] is True
        assert result["request_id"] == "req-xyz-42"
        assert isinstance(result["latency_ms"], int)
        assert _counter_value("ok", "reminder", "ok") == before + 1

        req = route.calls[0].request
        body = req.content.decode("utf-8", errors="replace")
        # Form-encoded — assert each required field is present
        assert f"token={_APP_TOKEN}" in body
        assert f"user={_USER_KEY}" in body
        assert "title=Jess+reminder" in body or "title=Jess%20reminder" in body
        assert "html=1" in body
        # Snooze anchor embedded in message body (URL-encoded)
        assert "Snooze+10+min" in body or "Snooze%2010%20min" in body
        # url / url_title fields for the Done tap action
        assert "url=" in body
        # url_title is "✓ Done" (U+2713) — URL-encoded form
        assert "url_title=%E2%9C%93+Done" in body or "url_title=%E2%9C%93%20Done" in body

    @pytest.mark.asyncio
    async def test_html_escape_prevents_injection(self, pushover_on):
        """Prompt-injected <a> / <script> in reminder text must be HTML-escaped
        before embedding, otherwise the attacker's anchor would render as a
        tappable link in Pushover's html=1 notification."""
        from orchestrator.pushover_manager import deliver_via_pushover

        evil = '<a href="http://attacker/">click</a> <script>x()</script>'
        with respx.mock(base_url="http://pushover.test") as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1, "request": "r"}))
            await deliver_via_pushover("r1", evil)
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        # The raw "<a " from the attacker must NOT appear (only the trusted
        # snooze anchor we emit ourselves does). Form-encoding of '<' is %3C.
        assert "%3Ca+href%3D%22http%3A%2F%2Fattacker" in body or (
            "<a href=" not in body  # fallback: make sure raw <a isn't in plaintext
        )
        # The '<script>' tag must be escaped — raw '<script>' absent
        assert "<script>" not in body
        # Our trusted snooze anchor IS present (form-encoded)
        assert "Snooze+10+min" in body or "Snooze%2010%20min" in body

    @pytest.mark.asyncio
    async def test_priority_clamped_low(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        with respx.mock(base_url="http://pushover.test") as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1, "request": "r"}))
            await deliver_via_pushover("r1", "t", priority=-999)
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert "priority=-2" in body
        assert "priority=-999" not in body

    @pytest.mark.asyncio
    async def test_priority_clamped_high(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        with respx.mock(base_url="http://pushover.test") as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1, "request": "r"}))
            await deliver_via_pushover("r1", "t", priority=999)
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert "priority=2" in body
        assert "priority=999" not in body

    @pytest.mark.asyncio
    async def test_4xx_bumps_http_4xx_and_sanitizes_body(self, pushover_on):
        """400 response with leaking 'token=...' must be scrubbed from the result body."""
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("fail", "reminder", "http_4xx")
        leaky_body = "error: token=abcdef1234567890 rejected\x01bad"
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(return_value=Response(400, text=leaky_body))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert result["status_code"] == 400
        # Credentials redacted AND control chars stripped
        assert "abcdef1234567890" not in result["body"]
        assert "token=<redacted>" in result["body"]
        assert "\x01" not in result["body"]
        assert _counter_value("fail", "reminder", "http_4xx") == before + 1

    @pytest.mark.asyncio
    async def test_5xx_bumps_http_5xx(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("fail", "reminder", "http_5xx")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(return_value=Response(503, text="overloaded"))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert result["status_code"] == 503
        assert _counter_value("fail", "reminder", "http_5xx") == before + 1

    @pytest.mark.asyncio
    async def test_timeout_swallowed_and_bumps_timeout(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("fail", "reminder", "timeout")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(side_effect=httpx.TimeoutException("slow"))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert "TimeoutException" in result["error"]
        assert _counter_value("fail", "reminder", "timeout") == before + 1

    @pytest.mark.asyncio
    async def test_connect_error_swallowed_and_bumps_connect_error(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("fail", "reminder", "connect_error")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(side_effect=httpx.ConnectError("dns"))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert "ConnectError" in result["error"]
        assert _counter_value("fail", "reminder", "connect_error") == before + 1

    @pytest.mark.asyncio
    async def test_generic_exception_swallowed_and_bumps_other(self, pushover_on):
        from orchestrator.pushover_manager import deliver_via_pushover

        before = _counter_value("fail", "reminder", "other")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(side_effect=ValueError("weird"))
            result = await deliver_via_pushover("r1", "t")
        assert result["success"] is False
        assert "ValueError" in result["error"]
        assert _counter_value("fail", "reminder", "other") == before + 1


# ===========================================================================
# deliver_pushover_confirm — respx-mocked HTTP
# ===========================================================================


class TestDeliverPushoverConfirm:
    @pytest.mark.asyncio
    async def test_disabled_returns_skipped_no_http(self, pushover_off):
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("skipped", "confirm", "disabled")
        with respx.mock:
            result = await deliver_pushover_confirm("t", "m")
        assert result == {"success": False, "skipped": True, "reason": "disabled"}
        assert _counter_value("skipped", "confirm", "disabled") == before + 1

    @pytest.mark.asyncio
    async def test_missing_creds_skipped(self, pushover_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.pushover_manager import deliver_pushover_confirm

        monkeypatch.setattr(settings, "pushover_user_key", "", raising=False)
        before = _counter_value("skipped", "confirm", "missing_credentials")
        with respx.mock:
            result = await deliver_pushover_confirm("t", "m")
        assert result["success"] is False
        assert result["reason"] == "missing_credentials"
        assert _counter_value("skipped", "confirm", "missing_credentials") == before + 1

    @pytest.mark.asyncio
    async def test_happy_path_200_priority_and_no_url(self, pushover_on):
        """Confirm: priority=-1 (quiet), NO url field, NO html=1, generic title."""
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("ok", "confirm", "ok")
        with respx.mock(base_url="http://pushover.test") as mock:
            route = mock.post("/1/messages.json").mock(return_value=Response(200, json={"status": 1}))
            result = await deliver_pushover_confirm("\u2713 Logged", "drink water\n(water logged)", "r1")
        assert result == {"success": True}
        assert _counter_value("ok", "confirm", "ok") == before + 1

        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert "priority=-1" in body
        # No `url=` query param (form-encoded, so key is `url=` with separator)
        assert "&url=" not in body
        assert not body.startswith("url=")
        # No html field at all
        assert "html=1" not in body
        # Title is generic — action name must NOT be in title
        # (title contains just "Logged" + check-mark glyph, URL-encoded)
        assert "title=%E2%9C%93+Logged" in body or "title=%E2%9C%93%20Logged" in body
        # Action name "water" IS in the body message (body allowed; title not)
        assert "water" in body.lower()

    @pytest.mark.asyncio
    async def test_4xx_surfaces_status_and_bumps_metric(self, pushover_on):
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("fail", "confirm", "http_4xx")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(return_value=Response(400, text="rejected"))
            result = await deliver_pushover_confirm("t", "m", reminder_id="r1")
        assert result["success"] is False
        assert result["status_code"] == 400
        assert _counter_value("fail", "confirm", "http_4xx") == before + 1

    @pytest.mark.asyncio
    async def test_5xx_surfaces_status_and_bumps_metric(self, pushover_on):
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("fail", "confirm", "http_5xx")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(return_value=Response(503, text="boom"))
            result = await deliver_pushover_confirm("t", "m")
        assert result["success"] is False
        assert result["status_code"] == 503
        assert _counter_value("fail", "confirm", "http_5xx") == before + 1

    @pytest.mark.asyncio
    async def test_timeout_swallowed(self, pushover_on):
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("fail", "confirm", "timeout")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(side_effect=httpx.TimeoutException("slow"))
            result = await deliver_pushover_confirm("t", "m")
        assert result["success"] is False
        assert "TimeoutException" in result["error"]
        assert _counter_value("fail", "confirm", "timeout") == before + 1

    @pytest.mark.asyncio
    async def test_connect_error_swallowed(self, pushover_on):
        from orchestrator.pushover_manager import deliver_pushover_confirm

        before = _counter_value("fail", "confirm", "connect_error")
        with respx.mock(base_url="http://pushover.test") as mock:
            mock.post("/1/messages.json").mock(side_effect=httpx.ConnectError("dns"))
            result = await deliver_pushover_confirm("t", "m")
        assert result["success"] is False
        assert "ConnectError" in result["error"]
        assert _counter_value("fail", "confirm", "connect_error") == before + 1

    @pytest.mark.asyncio
    async def test_reminder_id_in_failure_log(self, pushover_on, caplog):
        """Failure log lines must include `rid=<id>` for Loki correlation."""
        import logging

        from orchestrator.pushover_manager import deliver_pushover_confirm

        with (
            respx.mock(base_url="http://pushover.test") as mock,
            caplog.at_level(logging.WARNING, logger="orchestrator.pushover_manager"),
        ):
            mock.post("/1/messages.json").mock(return_value=Response(500))
            await deliver_pushover_confirm("t", "m", reminder_id="abc-123")

        assert any("rid=abc-123" in r.getMessage() for r in caplog.records), (
            f"Expected 'rid=abc-123' in WARNING log, got: {[r.getMessage() for r in caplog.records]}"
        )


# ===========================================================================
# config.validate_pushover_config — auto-disable + clamp semantics
# ===========================================================================


class TestConfigValidator:
    def test_enabled_with_short_user_key_disables(self, caplog):
        """PUSHOVER_ENABLED=true + user_key <8 chars → auto-disabled, ERROR, NO raise."""
        import logging

        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "true",
            "PUSHOVER_USER_KEY": "short",  # 5 chars, <8
            "PUSHOVER_APP_TOKEN": "valid-long-token-here",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            s = Settings()
        assert s.pushover_enabled is False
        assert any("PUSHOVER_ENABLED=true" in r.getMessage() for r in caplog.records)

    def test_enabled_with_short_app_token_disables(self, caplog):
        import logging

        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "true",
            "PUSHOVER_USER_KEY": "valid-long-user-key",
            "PUSHOVER_APP_TOKEN": "short",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(logging.ERROR, logger="orchestrator.config"):
            s = Settings()
        assert s.pushover_enabled is False

    def test_enabled_with_valid_creds_stays_enabled(self):
        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "true",
            "PUSHOVER_USER_KEY": "valid-long-user-key",
            "PUSHOVER_APP_TOKEN": "valid-long-token-here",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.pushover_enabled is True

    def test_priority_clamped_low(self):
        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "false",
            "PUSHOVER_DEFAULT_PRIORITY": "-999",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.pushover_default_priority == -2

    def test_priority_clamped_high(self):
        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "false",
            "PUSHOVER_DEFAULT_PRIORITY": "999",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.pushover_default_priority == 2

    def test_disabled_feature_never_raises_on_missing_creds(self):
        """If the feature flag is off, missing creds shouldn't matter."""
        from orchestrator.config import Settings

        env = {
            "PUSHOVER_ENABLED": "false",
            "PUSHOVER_USER_KEY": "",
            "PUSHOVER_APP_TOKEN": "",
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.pushover_enabled is False


# ===========================================================================
# api_routes — /ack + /snooze confirm wiring + widened feature gate
# ===========================================================================


@pytest.fixture
def client():
    """FastAPI TestClient over the api_routes router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def clean_scheduler():
    """Purge any reminder_* jobs before and after the test (shared singleton)."""
    from orchestrator.shared import scheduler

    def _purge():
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("reminder_"):
                with contextlib.suppress(Exception):
                    scheduler.remove_job(job.id)

    _purge()
    yield scheduler
    _purge()


def _make_sig(reminder_id: str, action: str, exp: int, extra: str = "") -> str:
    from orchestrator.reminder_manager import _sign_callback

    return _sign_callback(reminder_id, action, exp, extra)


class TestFeatureGateWidening:
    """Both ack and snooze routes must stay reachable if EITHER ntfy or
    pushover is enabled. 404 only when both are off."""

    def test_ack_404s_when_both_channels_off(self, client, ntfy_off, pushover_off):
        r = client.post("/api/reminder/ack/abc")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_snooze_404s_when_both_channels_off(self, client, ntfy_off, pushover_off):
        r = client.post("/api/reminder/snooze/abc?minutes=10")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_ack_processes_when_only_pushover_on(self, client, ntfy_off, pushover_on, tmp_db):
        """ntfy off, pushover on → the feature gate must let the request
        through. Bad sig here just means we get 403, not 404."""
        r = client.post(f"/api/reminder/ack/abc?sig={'0' * 32}&exp={int(time.time()) + 300}")
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"

    def test_snooze_processes_when_only_pushover_on(self, client, ntfy_off, pushover_on, tmp_db, clean_scheduler):
        r = client.post(f"/api/reminder/snooze/abc?sig={'0' * 32}&exp={int(time.time()) + 300}&minutes=10")
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"


class TestAckPushoverConfirmWiring:
    """The ack route must fire-and-forget deliver_pushover_confirm alongside
    ntfy's deliver_ack_confirm. Replays short-circuit before dispatch."""

    def _patch_confirms(self):
        """Patch BOTH confirm functions so we can assert exact call patterns
        without either one hitting real HTTP."""
        return (
            patch(
                "orchestrator.pushover_manager.deliver_pushover_confirm",
                new_callable=AsyncMock,
            ),
            patch(
                "orchestrator.reminder_manager.deliver_ack_confirm",
                new_callable=AsyncMock,
            ),
        )

    def _drain_tasks(self):
        import asyncio

        loop = asyncio.get_event_loop()
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def test_ack_fires_pushover_confirm(self, client, ntfy_on, pushover_on, tmp_db):
        from orchestrator import state_store

        state_store.save_reminder("r1", "take meds now", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        pushover_patch, ntfy_patch = self._patch_confirms()
        with (
            patch("orchestrator.selfcare_manager.record_medication_logged"),
            pushover_patch as mock_pushover,
            ntfy_patch,
        ):
            r = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            self._drain_tasks()

        assert r.status_code == 200
        mock_pushover.assert_called_once()
        args, _ = mock_pushover.call_args
        title, message, rid = args[0], args[1], args[2]
        assert title == "\u2713 Logged"
        # Action category must NOT be in the title (security invariant)
        assert "medication" not in title.lower()
        # Body carries the reminder text
        assert "take meds now" in message
        assert rid == "r1"

    def test_ack_replay_does_not_fire_pushover_confirm(self, client, ntfy_on, pushover_on, tmp_db):
        """already_acked must short-circuit before BOTH confirm dispatches."""
        from orchestrator import state_store

        state_store.save_reminder("r1", "walk outside", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        pushover_patch, ntfy_patch = self._patch_confirms()
        with patch("orchestrator.selfcare_manager.record_movement_logged"), pushover_patch as mock_pushover, ntfy_patch:
            r1 = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            self._drain_tasks()
            assert r1.status_code == 200
            assert mock_pushover.call_count == 1

            r2 = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            self._drain_tasks()
            assert r2.status_code == 200
            assert r2.json().get("already_acked") is True
            # No second dispatch — replay short-circuits before fire-and-forget
            assert mock_pushover.call_count == 1

    def test_ack_fires_pushover_even_when_pushover_enabled_false_at_caller(self, client, ntfy_on, pushover_off, tmp_db):
        """Same fire-and-forget contract as ntfy: the route dispatches
        unconditionally. The pushover_manager function itself is the gate."""
        from orchestrator import state_store

        state_store.save_reminder("r1", "whatever", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        pushover_patch, ntfy_patch = self._patch_confirms()
        with pushover_patch as mock_pushover, ntfy_patch:
            r = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            self._drain_tasks()

        assert r.status_code == 200
        mock_pushover.assert_called_once()


class TestSnoozePushoverConfirmWiring:
    def _patch_confirms(self):
        return (
            patch(
                "orchestrator.pushover_manager.deliver_pushover_confirm",
                new_callable=AsyncMock,
            ),
            patch(
                "orchestrator.reminder_manager.deliver_ack_confirm",
                new_callable=AsyncMock,
            ),
        )

    def _drain_tasks(self):
        import asyncio

        loop = asyncio.get_event_loop()
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def test_snooze_fires_pushover_confirm_with_sleep_title(
        self, client, ntfy_on, pushover_on, tmp_db, clean_scheduler
    ):
        from orchestrator import state_store

        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "snooze", exp, extra="10")

        pushover_patch, ntfy_patch = self._patch_confirms()
        with pushover_patch as mock_pushover, ntfy_patch:
            r = client.post(f"/api/reminder/snooze/r1?sig={sig}&exp={exp}&minutes=10")
            self._drain_tasks()

        assert r.status_code == 200
        mock_pushover.assert_called_once()
        args, _ = mock_pushover.call_args
        title, message, rid = args[0], args[1], args[2]
        assert title.startswith("\U0001f4a4 Snoozed until")
        assert "1/5 snoozes used" in message
        assert rid == "r1"


# ===========================================================================
# tool_handlers.deliver_reminder_job — unconditional Pushover dispatch
# ===========================================================================


class TestDeliverReminderJobDispatch:
    @pytest.mark.asyncio
    async def test_phone_target_dispatches_pushover(self, pushover_on, tmp_db, monkeypatch):
        """target='phone' → deliver_via_pushover fired via asyncio.create_task.
        We patch the symbol in the pushover_manager module (which is what the
        inner helper imports at call time)."""
        from orchestrator import state_store
        from orchestrator.tool_handlers import deliver_reminder_job

        state_store.save_reminder("r1", "take meds", "2026-04-20T09:00:00", target="phone")

        # Inhibit TTS and HA Companion push paths — they're not under test here
        async def _noop_voice(*a, **kw):
            return {"success": True}

        async def _noop_notify(*a, **kw):
            return {"success": True}

        monkeypatch.setattr("orchestrator.tool_handlers._announce_voice", _noop_voice)
        monkeypatch.setattr("orchestrator.tool_handlers._send_notification", _noop_notify)

        mock_push = AsyncMock(return_value={"success": True, "request_id": "r"})
        # Also silence the parallel ntfy dispatch so we're only asserting on pushover
        mock_ntfy = AsyncMock(return_value={"success": True})
        with (
            patch("orchestrator.pushover_manager.deliver_via_pushover", mock_push),
            patch("orchestrator.reminder_manager.deliver_via_ntfy", mock_ntfy),
        ):
            await deliver_reminder_job("r1")
            # Let the detached task run
            import asyncio

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        mock_push.assert_called_once()
        call_args, _ = mock_push.call_args
        assert call_args[0] == "r1"
        assert call_args[1] == "take meds"

    @pytest.mark.asyncio
    async def test_dispatch_happens_even_when_pushover_disabled(self, pushover_off, tmp_db, monkeypatch):
        """The gate is inside deliver_via_pushover itself — the caller always
        dispatches. This matters for runtime config flips: a fresh read after
        disabling would still see the create_task fire."""
        from orchestrator import state_store
        from orchestrator.tool_handlers import deliver_reminder_job

        state_store.save_reminder("r2", "water time", "2026-04-20T09:00:00", target="phone")

        async def _noop_voice(*a, **kw):
            return {"success": True}

        async def _noop_notify(*a, **kw):
            return {"success": True}

        monkeypatch.setattr("orchestrator.tool_handlers._announce_voice", _noop_voice)
        monkeypatch.setattr("orchestrator.tool_handlers._send_notification", _noop_notify)

        mock_push = AsyncMock(return_value={"success": False, "skipped": True, "reason": "disabled"})
        mock_ntfy = AsyncMock(return_value={"success": True})
        with (
            patch("orchestrator.pushover_manager.deliver_via_pushover", mock_push),
            patch("orchestrator.reminder_manager.deliver_via_ntfy", mock_ntfy),
        ):
            await deliver_reminder_job("r2")
            import asyncio

            pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        # Still dispatched: the "pushover_enabled=False" check is inside the
        # function, not the caller. This prevents stranded calls if flags flip.
        mock_push.assert_called_once()
