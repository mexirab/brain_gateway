"""
Tests for F-011 ntfy feedback loop.

Covers:
  - reminder_manager._sign_callback / verify_callback_signature / _build_callback_url
  - reminder_manager.deliver_via_ntfy / deliver_ack_confirm (network-mocked via respx)
  - reminder_manager.infer_selfcare_action_from_text
  - state_store.mark_reminder_acked / increment_snooze_count
  - api_routes: POST /api/reminder/ack/{id}, POST /api/reminder/snooze/{id}, and
    the confirm side-channel both routes fire (ntfy + Pushover)

Mocks settings via monkeypatch on the module-level singleton. Mocks HTTP via
respx so no real network calls leak out of the container.

Notes on the ack route test fakes:
  - `scheduler` is a real AsyncIOScheduler imported from `orchestrator.shared`.
    We use it as-is (never start it); `add_job` / `get_job` work fine on a
    stopped scheduler.
  - `deliver_reminder_job` is the real tool_handlers coroutine. We never let
    the scheduler fire, so it's inert.
"""

import asyncio
import hashlib
import hmac
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import httpx
import pytest
import respx
from httpx import Response

from orchestrator import state_store, tool_handlers
from orchestrator.config import Settings, settings
from orchestrator.metrics import NTFY_PUSH_TOTAL
from orchestrator.reminder_manager import (
    _sign_callback,
    deliver_ack_confirm,
    deliver_via_ntfy,
    infer_selfcare_action_from_text,
    verify_callback_signature,
)
from orchestrator.tests._helpers import metric_value
from orchestrator.tests.fixtures_delivery import NTFY_SECRET, drain_tasks_sync, sign

NTFY_BASE = "http://ntfy.test:8889"


def _pushes(result: str, kind: str) -> float:
    return metric_value(NTFY_PUSH_TOTAL, result=result, kind=kind)


@pytest.fixture
def ntfy_confirm_on(delivery_ntfy_on, monkeypatch):
    """Enable ntfy AND the confirm side-channel."""
    monkeypatch.setattr(settings, "ntfy_confirm_enabled", True, raising=False)
    return settings


# ===========================================================================
# verify_callback_signature (unit)
# ===========================================================================


class TestVerifyCallbackSignature:
    def test_valid_sig_returns_none(self, delivery_ntfy_on):
        exp = int(time.time()) + 300
        sig = _sign_callback("abc", "ack", exp)
        assert verify_callback_signature("abc", "ack", exp, sig) is None

    def test_expired_returns_expired(self, delivery_ntfy_on):
        exp = int(time.time()) - 10
        sig = _sign_callback("abc", "ack", exp)
        assert verify_callback_signature("abc", "ack", exp, sig) == "expired"

    def test_empty_secret_returns_signing_disabled(self, delivery_ntfy_on, monkeypatch):
        monkeypatch.setattr(settings, "ntfy_hmac_secret", "", raising=False)
        exp = int(time.time()) + 300
        # sig itself doesn't matter — we should never reach the compare
        assert verify_callback_signature("abc", "ack", exp, "deadbeef") == "signing_disabled"

    def test_wrong_sig_returns_bad_signature(self, delivery_ntfy_on):
        exp = int(time.time()) + 300
        # Valid shape, wrong bytes
        assert verify_callback_signature("abc", "ack", exp, "0" * 32) == "bad_signature"

    def test_snooze_extra_must_match(self, delivery_ntfy_on):
        exp = int(time.time()) + 300
        sig = _sign_callback("abc", "snooze", exp, extra="10")
        # Same extra verifies
        assert verify_callback_signature("abc", "snooze", exp, sig, extra="10") is None
        # Different extra (tampered minutes) fails
        assert verify_callback_signature("abc", "snooze", exp, sig, extra="20") == "bad_signature"

    def test_sign_callback_matches_hmac_sha256_truncated(self, delivery_ntfy_on):
        """Pin the signing formula (32 hex chars of HMAC-SHA256) so a refactor
        that changes the format fails loud."""
        expected = hmac.new(NTFY_SECRET.encode("utf-8"), b"abc|ack|2000000000|", hashlib.sha256).hexdigest()[:32]
        assert _sign_callback("abc", "ack", 2000000000) == expected


# ===========================================================================
# infer_selfcare_action_from_text (unit)
# ===========================================================================


@pytest.mark.parametrize(
    "text, expected",
    [
        ("take your meds", "medication"),
        ("pill time", "medication"),
        ("eat lunch", "meal"),
        ("snack break", "meal"),
        ("drink water", "water"),
        ("walk for ten min", "movement"),
        ("time to stretch", "movement"),
        ("call mom", None),
        ("", None),
        (None, None),
        # Word-boundary regex: 'premeditated' must NOT match 'med'.
        ("premeditated plans", None),
    ],
)
def test_infer_selfcare_action(text, expected):
    assert infer_selfcare_action_from_text(text) == expected


# ===========================================================================
# state_store.mark_reminder_acked / increment_snooze_count (unit)
# ===========================================================================


class TestMarkReminderAcked:
    def test_ack_new_reminder(self, tmp_db):
        state_store.save_reminder("r1", "take meds", "2026-04-20T09:00:00")
        result = state_store.mark_reminder_acked("r1", via="ntfy")
        assert result is not None
        assert result["already_acked"] is False
        assert result["ack_at"] is not None
        assert result["acked_via"] == "ntfy"
        assert result["status"] == "completed"

        # DB row now has ack_at populated
        row = state_store.get_reminder("r1")
        assert row["ack_at"] is not None
        assert row["status"] == "completed"

    def test_ack_twice_is_idempotent(self, tmp_db):
        state_store.save_reminder("r1", "take meds", "2026-04-20T09:00:00")
        first = state_store.mark_reminder_acked("r1", via="ntfy")
        assert first["already_acked"] is False
        first_ack_at = first["ack_at"]

        second = state_store.mark_reminder_acked("r1", via="ntfy")
        assert second is not None
        assert second["already_acked"] is True
        # DB row unchanged — ack_at preserved from first call
        assert state_store.get_reminder("r1")["ack_at"] == first_ack_at

    def test_ack_unknown_returns_none(self, tmp_db):
        assert state_store.mark_reminder_acked("nope", via="ntfy") is None


class TestIncrementSnoozeCount:
    def test_increments_from_zero(self, tmp_db):
        state_store.save_reminder("r1", "t", "2026-04-20T09:00:00")
        assert state_store.increment_snooze_count("r1") == 1
        assert state_store.increment_snooze_count("r1") == 2

    def test_unknown_returns_none(self, tmp_db):
        assert state_store.increment_snooze_count("nope") is None


# ===========================================================================
# deliver_via_ntfy / deliver_ack_confirm (unit, respx-mocked HTTP)
# ===========================================================================


class TestDeliverViaNtfy:
    async def test_disabled_returns_skipped_without_http(self, delivery_ntfy_off):
        # No routes registered — any real HTTP would blow up here.
        with respx.mock:
            result = await deliver_via_ntfy("r1", "hi")
        assert result == {"success": False, "skipped": True, "reason": "disabled"}

    async def test_enabled_missing_url_skipped(self, delivery_ntfy_on, monkeypatch):
        monkeypatch.setattr(settings, "ntfy_url", "", raising=False)
        with respx.mock:
            result = await deliver_via_ntfy("r1", "hi")
        assert result["success"] is False
        assert result["skipped"] is True
        assert "missing:" in result["reason"]
        assert "NTFY_URL" in result["reason"]

    async def test_success_path_200(self, delivery_ntfy_on):
        before = _pushes("ok", "reminder")

        with respx.mock(base_url=NTFY_BASE) as mock:
            route = mock.post("/jess-reminders").mock(return_value=Response(200))
            result = await deliver_via_ntfy("r1", "drink water")

        assert route.called
        req = route.calls[0].request
        # Action-button header present with Done + Snooze 10m
        actions = req.headers.get("Actions", "")
        assert "Done" in actions
        assert "Snooze 10m" in actions
        # Callback URLs point at the ack/snooze routes with sig/exp
        assert "/api/reminder/ack/r1?sig=" in actions
        assert "/api/reminder/snooze/r1?sig=" in actions
        # Title + priority headers
        assert req.headers.get("Title") == "Jess reminder"
        assert req.headers.get("Priority") == "3"

        assert result["success"] is True
        assert "latency_ms" in result
        assert _pushes("ok", "reminder") == before + 1


@pytest.mark.parametrize(
    "kind,mock_kwargs,expect",
    [
        pytest.param("reminder", {"return_value": Response(500, text="boom")}, 500, id="reminder-http-500"),
        pytest.param("reminder", {"side_effect": httpx.ConnectError("dns")}, "ConnectError", id="reminder-connect"),
        pytest.param("confirm", {"return_value": Response(503, text="boom")}, 503, id="confirm-http-503"),
        pytest.param("confirm", {"side_effect": httpx.ConnectError("dns")}, "ConnectError", id="confirm-connect"),
    ],
)
async def test_send_failure_is_returned_not_raised_and_counted(ntfy_confirm_on, caplog, kind, mock_kwargs, expect):
    """Fire-and-forget contract: a failed push returns a result (never
    raises), bumps the fail counter, and confirm failures log rid=<id> for
    Loki correlation."""
    before = _pushes("fail", kind)
    with (
        respx.mock(base_url=NTFY_BASE) as mock,
        caplog.at_level(logging.WARNING, logger="orchestrator.reminder_manager"),
    ):
        mock.post("/jess-reminders").mock(**mock_kwargs)
        if kind == "reminder":
            result = await deliver_via_ntfy("r1", "hi")
        else:
            result = await deliver_ack_confirm("t", "m", reminder_id="abc-123")

    assert result["success"] is False
    if isinstance(expect, int):
        assert result["status_code"] == expect
    else:
        assert expect in result["error"]
    assert _pushes("fail", kind) == before + 1
    if kind == "confirm":
        assert any("rid=abc-123" in r.getMessage() for r in caplog.records)


class TestDeliverAckConfirm:
    async def test_ntfy_disabled_returns_disabled_no_http_no_metric(self, delivery_ntfy_off, monkeypatch):
        """ntfy_enabled=False → short-circuit before any HTTP or metric bump."""
        monkeypatch.setattr(settings, "ntfy_confirm_enabled", True, raising=False)
        before = {r: _pushes(r, "confirm") for r in ("ok", "skipped", "fail")}

        with respx.mock:
            result = await deliver_ack_confirm("t", "m")

        assert result == {"success": False, "skipped": True, "reason": "disabled"}
        # No metric was touched — side-channel gate is silent
        assert {r: _pushes(r, "confirm") for r in before} == before

    async def test_confirm_flag_off_returns_disabled_no_http(self, delivery_ntfy_on, monkeypatch):
        """ntfy_enabled=True but ntfy_confirm_enabled=False → skipped, no HTTP."""
        monkeypatch.setattr(settings, "ntfy_confirm_enabled", False, raising=False)
        before_skip = _pushes("skipped", "confirm")

        with respx.mock:
            result = await deliver_ack_confirm("t", "m")

        assert result == {"success": False, "skipped": True, "reason": "disabled"}
        # Early-disabled gate does not bump any metric
        assert _pushes("skipped", "confirm") == before_skip

    async def test_missing_url_returns_missing_url_and_bumps_skipped_metric(self, ntfy_confirm_on, monkeypatch):
        """Both flags true but NTFY_URL='' → skipped/missing_url + metric bump."""
        monkeypatch.setattr(settings, "ntfy_url", "", raising=False)
        before = _pushes("skipped", "confirm")

        with respx.mock:
            result = await deliver_ack_confirm("t", "m")

        assert result == {"success": False, "skipped": True, "reason": "missing_url"}
        assert _pushes("skipped", "confirm") == before + 1

    async def test_happy_path_200(self, ntfy_confirm_on):
        """200 → success=True, metric +1, Title/Priority/body exactly as specified."""
        before = _pushes("ok", "confirm")

        with respx.mock(base_url=NTFY_BASE) as mock:
            route = mock.post("/jess-reminders").mock(return_value=Response(200))
            result = await deliver_ack_confirm("✓ Logged", "drink water\n(water logged)", "r1")

        assert route.called
        req = route.calls[0].request
        # Title header is sent as UTF-8 bytes (httpx accepts raw bytes
        # values), which ntfy decodes as UTF-8. The raw header bytes must
        # match the caller's UTF-8 encoding of the original string.
        raw_title = dict(req.headers.raw).get(b"title") or dict(req.headers.raw).get(b"Title")
        assert raw_title == "✓ Logged".encode()
        # And the decoded view matches too
        assert req.headers.get("Title") == "✓ Logged"
        assert req.headers.get("Priority") == "1"
        # Body is raw message bytes (utf-8 encoded)
        assert req.content == b"drink water\n(water logged)"
        # No Actions header on confirms (intentionally different from reminders)
        assert "Actions" not in req.headers

        assert result == {"success": True}
        assert _pushes("ok", "confirm") == before + 1

    async def test_title_sliced_to_120_chars(self, ntfy_confirm_on):
        """200-char title → Title header is exactly 120 chars."""
        with respx.mock(base_url=NTFY_BASE) as mock:
            route = mock.post("/jess-reminders").mock(return_value=Response(200))
            await deliver_ack_confirm("A" * 200, "m")

        assert route.calls[0].request.headers.get("Title") == "A" * 120


# ===========================================================================
# config model_validator — auto-disable on short secret
# ===========================================================================


@pytest.mark.parametrize(
    "secret,enabled",
    [pytest.param("short", False, id="short-secret-disables"), pytest.param("x" * 40, True, id="long-secret-stays-on")],
)
def test_config_ntfy_secret_length_gate(caplog, secret, enabled):
    """NTFY_ENABLED=true with a <32-char secret → auto-disabled, ERROR logged,
    NO raise. A fresh Settings() (no module reload), with the process env
    cleared and no env file, so neither the host env nor a deployed .env
    can change the outcome."""
    env = {
        "NTFY_ENABLED": "true",
        "NTFY_HMAC_SECRET": secret,
        "HA_TOKEN": "x",
        "API_TOKEN": "x",
        "PIHOLE_PASSWORD": "x",
    }
    with (
        patch.dict(os.environ, env, clear=True),
        caplog.at_level(logging.ERROR, logger="orchestrator.config"),
    ):
        s = Settings(_env_file=None)
    assert s.ntfy_enabled is enabled
    assert any("NTFY_ENABLED=true" in r.getMessage() for r in caplog.records) is not enabled


# ===========================================================================
# Integration — POST /api/reminder/ack & /snooze via TestClient
# ===========================================================================


def _exp() -> int:
    return int(time.time()) + 300


class TestAckRoute:
    def test_disabled_returns_404_without_sig_check(
        self, delivery_api_client, delivery_ntfy_off, delivery_pushover_off
    ):
        # No sig params at all — should still 404 because the feature flag
        # check (ntfy AND pushover off) short-circuits before
        # verify_callback_signature.
        r = delivery_api_client.post("/api/reminder/ack/abc")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_unknown_id_with_valid_sig_returns_404_not_found(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        exp = _exp()
        r = delivery_api_client.post(f"/api/reminder/ack/unknown?sig={sign('unknown', 'ack', exp)}&exp={exp}")
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"

    def test_expired_sig_returns_410(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        exp = int(time.time()) - 60
        r = delivery_api_client.post(f"/api/reminder/ack/abc?sig={sign('abc', 'ack', exp)}&exp={exp}")
        assert r.status_code == 410
        assert r.json()["error"] == "expired"

    def test_bad_sig_returns_403(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        r = delivery_api_client.post(f"/api/reminder/ack/abc?sig={'0' * 32}&exp={_exp()}")
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"

    def test_success_fires_selfcare_bridge_for_meds(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        state_store.save_reminder("r1", "take meds now", "2026-04-20T09:00:00")
        exp = _exp()

        with patch("orchestrator.selfcare_manager.record_medication_logged") as mock_med:
            r = delivery_api_client.post(f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}")

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["inferred_action"] == "medication"
        mock_med.assert_called_once()
        label = mock_med.call_args[0][0]
        assert label.startswith("reminder:")
        assert "take meds now" in label

    def test_success_no_selfcare_for_unrelated_text(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        state_store.save_reminder("r1", "call the doctor", "2026-04-20T09:00:00")
        exp = _exp()

        with (
            patch("orchestrator.selfcare_manager.record_medication_logged") as mock_med,
            patch("orchestrator.selfcare_manager.record_meal_logged") as mock_meal,
        ):
            r = delivery_api_client.post(f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}")

        assert r.status_code == 200
        assert r.json()["inferred_action"] is None
        mock_med.assert_not_called()
        mock_meal.assert_not_called()

    def test_replay_returns_already_acked(self, delivery_api_client, delivery_ntfy_on, tmp_db):
        state_store.save_reminder("r1", "walk outside", "2026-04-20T09:00:00")
        exp = _exp()
        url = f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}"

        with patch("orchestrator.selfcare_manager.record_movement_logged"):
            assert delivery_api_client.post(url).status_code == 200
            r2 = delivery_api_client.post(url)
            assert r2.status_code == 200
            assert r2.json().get("already_acked") is True


def _snooze_url(rid: str, minutes: int, sig_minutes: int | None = None) -> str:
    exp = _exp()
    sig = sign(rid, "snooze", exp, extra=str(minutes if sig_minutes is None else sig_minutes))
    return f"/api/reminder/snooze/{rid}?sig={sig}&exp={exp}&minutes={minutes}"


class TestSnoozeRoute:
    def test_disabled_returns_404(self, delivery_api_client, delivery_ntfy_off, delivery_pushover_off):
        r = delivery_api_client.post("/api/reminder/snooze/abc?minutes=10")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_valid_snooze_reschedules_job(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler
    ):
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")

        before = datetime.now(UTC)
        r = delivery_api_client.post(_snooze_url("r1", 10))
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["snooze_count"] == 1

        # Scheduler has a reschedule job for r1. We don't start the scheduler
        # in tests (so next_run_time is None), but trigger.run_date is set.
        job = delivery_clean_scheduler.get_job("reminder_r1")
        assert job is not None
        # run_date is tz-aware in the scheduler's timezone (e.g. America/Chicago).
        # Compare tz-aware against a tz-aware `before` so a UTC CI host and a
        # non-UTC app timezone don't introduce a fixed multi-hour offset.
        run_date = job.trigger.run_date
        if run_date.tzinfo is None:
            run_date = run_date.replace(tzinfo=UTC)
        # ~10 minutes = 600s, tolerate a little timing noise
        assert 540 < (run_date - before).total_seconds() < 660

    def test_minutes_clamped_before_sig_verify(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler
    ):
        """minutes=999 gets clamped to 120; sig must be for 120, not 10 or 999."""
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")

        # Signature for the clamped value (120) works
        assert delivery_api_client.post(_snooze_url("r1", 999, sig_minutes=120)).status_code == 200

        # Signature for minutes=10 does NOT work (server clamps to 120 first,
        # so the HMAC expectation binds to "120", not "10").
        r_bad = delivery_api_client.post(_snooze_url("r1", 999, sig_minutes=10))
        assert r_bad.status_code == 403
        assert r_bad.json()["error"] == "bad_signature"

    def test_max_snoozes_returns_409(self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler):
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        # Pre-populate snooze_count = 5 (= ntfy_max_snooze_count)
        with state_store.get_db() as conn:
            conn.execute("UPDATE reminders SET snooze_count = 5 WHERE id = ?", ("r1",))

        r = delivery_api_client.post(_snooze_url("r1", 10))
        assert r.status_code == 409
        assert r.json()["error"] == "max_snoozes_reached"
        assert r.json()["snooze_count"] == 5
        # Guardrail should have fired BEFORE add_job, so no reminder_r1 job exists
        assert delivery_clean_scheduler.get_job("reminder_r1") is None

    def test_unknown_id_with_valid_sig_returns_404(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler
    ):
        r = delivery_api_client.post(_snooze_url("ghost", 10))
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


# ===========================================================================
# Route wiring — /ack and /snooze fire BOTH confirm side-channels
# ===========================================================================


@pytest.fixture
def confirms():
    """Patch both confirm senders where the routes import them at call time:
    ntfy's deliver_ack_confirm and Pushover's deliver_pushover_confirm."""
    with (
        patch("orchestrator.reminder_manager.deliver_ack_confirm", new_callable=AsyncMock) as ntfy,
        patch("orchestrator.pushover_manager.deliver_pushover_confirm", new_callable=AsyncMock) as pushover,
    ):
        yield SimpleNamespace(ntfy=ntfy, pushover=pushover, both=(ntfy, pushover))


class TestConfirmWiring:
    """The routes dispatch create_task(confirm(title, body, reminder_id)) on
    both channels, unconditionally (each sender is its own gate). Titles stay
    generic: the action category is lock-screen visible, so it may only appear
    in the body (security finding)."""

    def test_ack_fires_both_confirms_with_generic_title(self, delivery_api_client, ntfy_confirm_on, tmp_db, confirms):
        state_store.save_reminder("r1", "take meds now", "2026-04-20T09:00:00")
        exp = _exp()

        with patch("orchestrator.selfcare_manager.record_medication_logged"):
            r = delivery_api_client.post(f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}")
            drain_tasks_sync()

        assert r.status_code == 200
        for mock in confirms.both:
            mock.assert_called_once()
        assert confirms.ntfy.call_args == confirms.pushover.call_args
        title, message, rid = confirms.ntfy.call_args.args[:3]
        assert title == "✓ Logged"
        assert "medication" not in title.lower()
        assert "take meds now" in message
        assert "medication" in message.lower()  # category is fine in the BODY
        assert rid == "r1"

    def test_ack_no_action_omits_action_suffix_in_body(self, delivery_api_client, ntfy_confirm_on, tmp_db, confirms):
        state_store.save_reminder("r1", "call the doctor", "2026-04-20T09:00:00")
        exp = _exp()

        r = delivery_api_client.post(f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}")
        drain_tasks_sync()

        assert r.status_code == 200
        title, message, rid = confirms.ntfy.call_args.args[:3]
        assert title == "✓ Logged"
        assert "call the doctor" in message
        assert "logged)" not in message  # no "(X logged)" suffix when action is None
        assert rid == "r1"

    def test_ack_replay_fires_neither_confirm(self, delivery_api_client, ntfy_confirm_on, tmp_db, confirms):
        """already_acked short-circuits BEFORE both create_task dispatches."""
        state_store.save_reminder("r1", "walk outside", "2026-04-20T09:00:00")
        exp = _exp()
        url = f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}"

        with patch("orchestrator.selfcare_manager.record_movement_logged"):
            assert delivery_api_client.post(url).status_code == 200
            drain_tasks_sync()
            assert [m.call_count for m in confirms.both] == [1, 1]

            r2 = delivery_api_client.post(url)
            drain_tasks_sync()
        assert r2.status_code == 200
        assert r2.json().get("already_acked") is True
        assert [m.call_count for m in confirms.both] == [1, 1]

    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_dispatch_is_unconditional_when_channel_flags_off(
        self,
        delivery_api_client,
        delivery_ntfy_on,
        delivery_pushover_off,
        tmp_db,
        delivery_clean_scheduler,
        monkeypatch,
        confirms,
        action,
    ):
        """ntfy_confirm_enabled=False and pushover_enabled=False: the routes
        still dispatch both — api_routes doesn't know or care about the flags."""
        monkeypatch.setattr(settings, "ntfy_confirm_enabled", False, raising=False)
        state_store.save_reminder("r1", "whatever", "2026-04-20T09:00:00")
        exp = _exp()
        url = (
            f"/api/reminder/ack/r1?sig={sign('r1', 'ack', exp)}&exp={exp}" if action == "ack" else _snooze_url("r1", 10)
        )

        r = delivery_api_client.post(url)
        drain_tasks_sync()

        assert r.status_code == 200
        for mock in confirms.both:
            mock.assert_called_once()

    def test_snooze_fires_both_with_sleep_title_and_count_body(
        self, delivery_api_client, ntfy_confirm_on, tmp_db, delivery_clean_scheduler, confirms
    ):
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")

        r = delivery_api_client.post(_snooze_url("r1", 10))
        drain_tasks_sync()

        assert r.status_code == 200
        for mock in confirms.both:
            mock.assert_called_once()
        assert confirms.ntfy.call_args == confirms.pushover.call_args
        title, message, rid = confirms.ntfy.call_args.args[:3]
        assert title.startswith("\U0001f4a4 Snoozed until")  # U+1F4A4 sleep emoji
        assert "1/5 snoozes used" in message
        assert rid == "r1"


class TestSnoozeAfterDelivery:
    """Regression for the 2026-07-04 audit P0: delivery marks a reminder
    'completed'; the snooze callback used to reschedule the job WITHOUT
    resetting status, so the redelivery hit deliver_reminder_job's
    "already completed, skipping" guard and the reminder never came back —
    while the user held a "Snoozed until 3:10" confirmation."""

    def test_snoozed_after_delivery_actually_redelivers(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler
    ):
        state_store.save_reminder("r1", "take meds", "2026-04-20T09:00:00")
        # Simulate the first delivery having completed the reminder.
        state_store.complete_reminder("r1")
        assert state_store.get_reminder("r1")["status"] == "completed"

        r = delivery_api_client.post(_snooze_url("r1", 10))
        assert r.status_code == 200

        # Snooze must reopen the reminder...
        assert state_store.get_reminder("r1")["status"] == "pending"

        # ...so the rescheduled delivery actually fires instead of skipping.
        with (
            patch.object(tool_handlers, "_announce_voice", AsyncMock(return_value={"success": True})),
            patch.object(tool_handlers, "_send_notification", AsyncMock(return_value={"success": True})) as mock_phone,
        ):
            asyncio.get_event_loop().run_until_complete(tool_handlers.deliver_reminder_job("r1"))

        mock_phone.assert_called_once()
        assert state_store.get_reminder("r1")["status"] == "completed"

    def test_snooze_cancels_pending_tts_retry(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, delivery_clean_scheduler
    ):
        """A snooze supersedes any scheduled TTS-failure retry — otherwise the
        reminder fires twice (retry at +2min, snooze at +10min)."""
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        delivery_clean_scheduler.add_job(
            tool_handlers.deliver_reminder_job,
            trigger="date",
            run_date=datetime.now(UTC) + timedelta(minutes=2),
            args=["r1"],
            id="reminder_r1_retry",
        )

        r = delivery_api_client.post(_snooze_url("r1", 10))
        assert r.status_code == 200
        assert delivery_clean_scheduler.get_job("reminder_r1_retry") is None
        assert delivery_clean_scheduler.get_job("reminder_r1") is not None


class TestRejectionLogSanitized:
    """Unauthenticated reminder ids are logged capped + repr-escaped."""

    @pytest.mark.parametrize("action", ["ack", "snooze"])
    def test_bad_sig_long_id_with_newline_logs_repr(
        self, delivery_api_client, delivery_ntfy_on, tmp_db, caplog, action
    ):
        evil_id = "abc\nFAKE LOG LINE " + "z" * 200
        extra = "&minutes=10" if action == "snooze" else ""
        with caplog.at_level(logging.WARNING, logger="orchestrator.api_routes"):
            r = delivery_api_client.post(
                f"/api/reminder/{action}/{quote(evil_id, safe='')}?sig={'0' * 32}&exp={_exp()}{extra}"
            )

        assert r.status_code == 403
        msgs = [rec.getMessage() for rec in caplog.records if "Rejected" in rec.getMessage()]
        assert len(msgs) == 1
        msg = msgs[0]
        assert "\n" not in msg
        assert repr(evil_id[:64]) in msg
        assert "z" * 100 not in msg  # capped at 64 chars
