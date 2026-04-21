"""
Tests for F-011 ntfy feedback loop.

Covers:
  - reminder_manager._sign_callback / verify_callback_signature / _build_callback_url
  - reminder_manager.deliver_via_ntfy (network-mocked via respx)
  - reminder_manager.infer_selfcare_action_from_text
  - state_store.mark_reminder_acked / increment_snooze_count
  - api_routes: POST /api/reminder/ack/{id}, POST /api/reminder/snooze/{id}

Mocks settings via monkeypatch on the module-level singleton. Mocks HTTP via
respx so no real network calls leak out of the container.

Notes on the ack route test fakes:
  - `scheduler` is a real AsyncIOScheduler imported from `orchestrator.shared`.
    We use it as-is (never start it); `add_job` / `get_job` work fine on a
    stopped scheduler.
  - `deliver_reminder_job` is the real tool_handlers coroutine. We never let
    the scheduler fire, so it's inert.
"""

import hashlib
import hmac
import time
from datetime import datetime
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Settings shim — every test needs a known ntfy secret on the live singleton.
# ---------------------------------------------------------------------------

_SECRET_32 = "x" * 40  # 40 chars, passes the >=32 model_validator guard


@pytest.fixture
def ntfy_on(monkeypatch):
    """Enable ntfy on the live settings singleton with a known secret."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ntfy_hmac_secret", _SECRET_32, raising=False)
    monkeypatch.setattr(settings, "ntfy_url", "http://ntfy.test:8889", raising=False)
    monkeypatch.setattr(settings, "ntfy_topic", "jess-reminders", raising=False)
    monkeypatch.setattr(
        settings, "ntfy_callback_base_url", "http://helios.test:8888", raising=False
    )
    monkeypatch.setattr(settings, "ntfy_ack_exp_seconds", 1800, raising=False)
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 5, raising=False)
    monkeypatch.setattr(settings, "ntfy_default_priority", 3, raising=False)
    return settings


@pytest.fixture
def ntfy_off(monkeypatch):
    """Disabled ntfy — secret still present so sig-specific tests can verify
    the feature-flag gate fires BEFORE the HMAC check."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", False, raising=False)
    monkeypatch.setattr(settings, "ntfy_hmac_secret", _SECRET_32, raising=False)
    return settings


# ===========================================================================
# verify_callback_signature (unit)
# ===========================================================================


class TestVerifyCallbackSignature:
    def test_valid_sig_returns_none(self, ntfy_on):
        from orchestrator.reminder_manager import _sign_callback, verify_callback_signature

        exp = int(time.time()) + 300
        sig = _sign_callback("abc", "ack", exp)
        assert verify_callback_signature("abc", "ack", exp, sig) is None

    def test_expired_returns_expired(self, ntfy_on):
        from orchestrator.reminder_manager import _sign_callback, verify_callback_signature

        exp = int(time.time()) - 10
        sig = _sign_callback("abc", "ack", exp)
        assert verify_callback_signature("abc", "ack", exp, sig) == "expired"

    def test_empty_secret_returns_signing_disabled(self, ntfy_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.reminder_manager import verify_callback_signature

        monkeypatch.setattr(settings, "ntfy_hmac_secret", "", raising=False)
        exp = int(time.time()) + 300
        # sig itself doesn't matter — we should never reach the compare
        assert verify_callback_signature("abc", "ack", exp, "deadbeef") == "signing_disabled"

    def test_wrong_sig_returns_bad_signature(self, ntfy_on):
        from orchestrator.reminder_manager import verify_callback_signature

        exp = int(time.time()) + 300
        # Valid shape, wrong bytes
        assert (
            verify_callback_signature("abc", "ack", exp, "0" * 32) == "bad_signature"
        )

    def test_snooze_extra_must_match(self, ntfy_on):
        from orchestrator.reminder_manager import _sign_callback, verify_callback_signature

        exp = int(time.time()) + 300
        sig = _sign_callback("abc", "snooze", exp, extra="10")
        # Same extra verifies
        assert verify_callback_signature("abc", "snooze", exp, sig, extra="10") is None
        # Different extra (tampered minutes) fails
        assert (
            verify_callback_signature("abc", "snooze", exp, sig, extra="20")
            == "bad_signature"
        )

    def test_sign_callback_is_32_hex_chars(self, ntfy_on):
        from orchestrator.reminder_manager import _sign_callback

        sig = _sign_callback("abc", "ack", int(time.time()) + 300)
        assert len(sig) == 32
        # hex
        int(sig, 16)

    def test_sign_callback_matches_hmac_sha256_truncated(self, ntfy_on):
        """Pin the signing formula so a refactor that changes the format fails loud."""
        from orchestrator.reminder_manager import _sign_callback

        exp = 2000000000
        expected = hmac.new(
            _SECRET_32.encode("utf-8"),
            b"abc|ack|2000000000|",
            hashlib.sha256,
        ).hexdigest()[:32]
        assert _sign_callback("abc", "ack", exp) == expected


# ===========================================================================
# infer_selfcare_action_from_text (unit)
# ===========================================================================


class TestInferSelfcareAction:
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
        ],
    )
    def test_cases(self, text, expected):
        from orchestrator.reminder_manager import infer_selfcare_action_from_text

        assert infer_selfcare_action_from_text(text) == expected

    def test_none_text(self):
        from orchestrator.reminder_manager import infer_selfcare_action_from_text

        assert infer_selfcare_action_from_text(None) is None  # type: ignore[arg-type]

    def test_word_boundary_avoids_false_match(self):
        """'premeditated' should NOT match 'med'. Word-boundary regex guards this."""
        from orchestrator.reminder_manager import infer_selfcare_action_from_text

        assert infer_selfcare_action_from_text("premeditated plans") is None


# ===========================================================================
# state_store.mark_reminder_acked / increment_snooze_count (unit)
# ===========================================================================


class TestMarkReminderAcked:
    def test_ack_new_reminder(self, tmp_db):
        from orchestrator import state_store

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
        from orchestrator import state_store

        state_store.save_reminder("r1", "take meds", "2026-04-20T09:00:00")
        first = state_store.mark_reminder_acked("r1", via="ntfy")
        assert first["already_acked"] is False
        first_ack_at = first["ack_at"]

        second = state_store.mark_reminder_acked("r1", via="ntfy")
        assert second is not None
        assert second["already_acked"] is True
        # DB row unchanged — ack_at preserved from first call
        row = state_store.get_reminder("r1")
        assert row["ack_at"] == first_ack_at

    def test_ack_unknown_returns_none(self, tmp_db):
        from orchestrator import state_store

        assert state_store.mark_reminder_acked("nope", via="ntfy") is None


class TestIncrementSnoozeCount:
    def test_increments_from_zero(self, tmp_db):
        from orchestrator import state_store

        state_store.save_reminder("r1", "t", "2026-04-20T09:00:00")
        assert state_store.increment_snooze_count("r1") == 1
        assert state_store.increment_snooze_count("r1") == 2

    def test_unknown_returns_none(self, tmp_db):
        from orchestrator import state_store

        assert state_store.increment_snooze_count("nope") is None


# ===========================================================================
# deliver_via_ntfy (unit, respx-mocked HTTP)
# ===========================================================================


class TestDeliverViaNtfy:
    @pytest.mark.asyncio
    async def test_disabled_returns_skipped_without_http(self, ntfy_off):
        import respx

        from orchestrator.reminder_manager import deliver_via_ntfy

        # No routes registered — any real HTTP would blow up here.
        with respx.mock:
            result = await deliver_via_ntfy("r1", "hi")
        assert result == {"success": False, "skipped": True, "reason": "disabled"}

    @pytest.mark.asyncio
    async def test_enabled_missing_url_skipped(self, ntfy_on, monkeypatch):
        from orchestrator.config import settings
        from orchestrator.reminder_manager import deliver_via_ntfy

        monkeypatch.setattr(settings, "ntfy_url", "", raising=False)

        import respx

        with respx.mock:
            result = await deliver_via_ntfy("r1", "hi")
        assert result["success"] is False
        assert result["skipped"] is True
        assert "missing:" in result["reason"]
        assert "NTFY_URL" in result["reason"]

    @pytest.mark.asyncio
    async def test_success_path_200(self, ntfy_on):
        import respx
        from httpx import Response

        from orchestrator.metrics import NTFY_PUSH_TOTAL
        from orchestrator.reminder_manager import deliver_via_ntfy

        before = NTFY_PUSH_TOTAL.labels(result="ok")._value.get()

        with respx.mock(base_url="http://ntfy.test:8889") as mock:
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
        after = NTFY_PUSH_TOTAL.labels(result="ok")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_server_500_is_fail_not_raise(self, ntfy_on):
        import respx
        from httpx import Response

        from orchestrator.metrics import NTFY_PUSH_TOTAL
        from orchestrator.reminder_manager import deliver_via_ntfy

        before = NTFY_PUSH_TOTAL.labels(result="fail")._value.get()
        with respx.mock(base_url="http://ntfy.test:8889") as mock:
            mock.post("/jess-reminders").mock(return_value=Response(500, text="boom"))
            result = await deliver_via_ntfy("r1", "hi")

        assert result["success"] is False
        assert result["status_code"] == 500
        after = NTFY_PUSH_TOTAL.labels(result="fail")._value.get()
        assert after == before + 1

    @pytest.mark.asyncio
    async def test_connect_error_is_swallowed(self, ntfy_on):
        import httpx
        import respx

        from orchestrator.metrics import NTFY_PUSH_TOTAL
        from orchestrator.reminder_manager import deliver_via_ntfy

        before = NTFY_PUSH_TOTAL.labels(result="fail")._value.get()
        with respx.mock(base_url="http://ntfy.test:8889") as mock:
            mock.post("/jess-reminders").mock(
                side_effect=httpx.ConnectError("dns")
            )
            # Must NOT raise — fire-and-forget contract
            result = await deliver_via_ntfy("r1", "hi")

        assert result["success"] is False
        assert "error" in result
        assert "ConnectError" in result["error"]
        after = NTFY_PUSH_TOTAL.labels(result="fail")._value.get()
        assert after == before + 1


# ===========================================================================
# config model_validator — auto-disable on short secret
# ===========================================================================


class TestConfigAutoDisable:
    def test_short_secret_disables_ntfy(self, caplog):
        """NTFY_ENABLED=true but secret <32 chars → auto-disabled, ERROR logged, NO raise.

        Constructs a fresh Settings() directly (no module reload) so the
        module-level singleton stays intact and downstream references held by
        reminder_manager / api_routes aren't broken mid-suite.
        """
        import logging
        import os

        from orchestrator.config import Settings

        env = {
            "NTFY_ENABLED": "true",
            "NTFY_HMAC_SECRET": "short",  # 5 chars, <32
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False), caplog.at_level(
            logging.ERROR, logger="orchestrator.config"
        ):
            s = Settings()
        assert s.ntfy_enabled is False
        # Error was logged but no exception raised
        assert any(
            "NTFY_ENABLED=true" in r.getMessage() for r in caplog.records
        )

    def test_long_secret_keeps_ntfy_enabled(self):
        """32+ char secret → ntfy stays on."""
        import os

        from orchestrator.config import Settings

        env = {
            "NTFY_ENABLED": "true",
            "NTFY_HMAC_SECRET": "x" * 40,
            "HA_TOKEN": "x",
            "API_TOKEN": "x",
            "PIHOLE_PASSWORD": "x",
        }
        with patch.dict(os.environ, env, clear=False):
            s = Settings()
        assert s.ntfy_enabled is True


# ===========================================================================
# Integration — POST /api/reminder/ack & /snooze via TestClient
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


def _make_sig(reminder_id: str, action: str, exp: int, extra: str = "") -> str:
    from orchestrator.reminder_manager import _sign_callback

    return _sign_callback(reminder_id, action, exp, extra)


class TestAckRoute:
    def test_disabled_returns_404_without_sig_check(self, client, ntfy_off):
        # No sig params at all — should still 404 because feature flag check
        # short-circuits before verify_callback_signature
        r = client.post("/api/reminder/ack/abc")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_unknown_id_with_valid_sig_returns_404_not_found(
        self, client, ntfy_on, tmp_db
    ):
        exp = int(time.time()) + 300
        sig = _make_sig("unknown", "ack", exp)
        r = client.post(f"/api/reminder/ack/unknown?sig={sig}&exp={exp}")
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"

    def test_expired_sig_returns_410(self, client, ntfy_on, tmp_db):
        exp = int(time.time()) - 60
        sig = _make_sig("abc", "ack", exp)
        r = client.post(f"/api/reminder/ack/abc?sig={sig}&exp={exp}")
        assert r.status_code == 410
        assert r.json()["error"] == "expired"

    def test_bad_sig_returns_403(self, client, ntfy_on, tmp_db):
        exp = int(time.time()) + 300
        r = client.post(f"/api/reminder/ack/abc?sig={'0' * 32}&exp={exp}")
        assert r.status_code == 403
        assert r.json()["error"] == "bad_signature"

    def test_success_fires_selfcare_bridge_for_meds(
        self, client, ntfy_on, tmp_db
    ):
        from orchestrator import state_store

        state_store.save_reminder("r1", "take meds now", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        with patch(
            "orchestrator.selfcare_manager.record_medication_logged"
        ) as mock_med:
            r = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["inferred_action"] == "medication"
        mock_med.assert_called_once()
        label = mock_med.call_args[0][0]
        assert label.startswith("reminder:")
        assert "take meds now" in label

    def test_success_no_selfcare_for_unrelated_text(
        self, client, ntfy_on, tmp_db
    ):
        from orchestrator import state_store

        state_store.save_reminder("r1", "call the doctor", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        with patch(
            "orchestrator.selfcare_manager.record_medication_logged"
        ) as mock_med, patch(
            "orchestrator.selfcare_manager.record_meal_logged"
        ) as mock_meal:
            r = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")

        assert r.status_code == 200
        assert r.json()["inferred_action"] is None
        mock_med.assert_not_called()
        mock_meal.assert_not_called()

    def test_replay_returns_already_acked(self, client, ntfy_on, tmp_db):
        from orchestrator import state_store

        state_store.save_reminder("r1", "walk outside", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "ack", exp)

        with patch("orchestrator.selfcare_manager.record_movement_logged"):
            r1 = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            assert r1.status_code == 200
            r2 = client.post(f"/api/reminder/ack/r1?sig={sig}&exp={exp}")
            assert r2.status_code == 200
            assert r2.json().get("already_acked") is True


@pytest.fixture
def clean_scheduler():
    """Remove any reminder_* jobs from the shared scheduler before and after the test.

    The AsyncIOScheduler is a module-level singleton (orchestrator.shared.scheduler),
    so jobs leak across tests. We scrub anything starting with 'reminder_' so
    tests get a clean slate and don't pollute each other.
    """
    from orchestrator.shared import scheduler

    def _purge():
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("reminder_"):
                try:
                    scheduler.remove_job(job.id)
                except Exception:
                    pass

    _purge()
    yield scheduler
    _purge()


class TestSnoozeRoute:
    def test_disabled_returns_404(self, client, ntfy_off):
        r = client.post("/api/reminder/snooze/abc?minutes=10")
        assert r.status_code == 404
        assert r.json()["error"] == "disabled"

    def test_valid_snooze_reschedules_job(
        self, client, ntfy_on, tmp_db, clean_scheduler
    ):
        from orchestrator import state_store

        scheduler = clean_scheduler
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300
        sig = _make_sig("r1", "snooze", exp, extra="10")

        before = datetime.now()
        r = client.post(
            f"/api/reminder/snooze/r1?sig={sig}&exp={exp}&minutes=10"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["snooze_count"] == 1

        # Scheduler has a reschedule job for r1. We don't start the scheduler
        # in tests (so next_run_time is None), but trigger.run_date is set.
        job = scheduler.get_job("reminder_r1")
        assert job is not None
        run_date = job.trigger.run_date.replace(tzinfo=None)
        delta = (run_date - before).total_seconds()
        # ~10 minutes = 600s, tolerate a little timing noise
        assert 540 < delta < 660

    def test_minutes_clamped_before_sig_verify(
        self, client, ntfy_on, tmp_db, clean_scheduler
    ):
        """minutes=999 gets clamped to 120; sig must be for 120, not 10 or 999."""
        from orchestrator import state_store

        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        exp = int(time.time()) + 300

        # Signature for the clamped value (120) works
        sig_120 = _make_sig("r1", "snooze", exp, extra="120")
        r_ok = client.post(
            f"/api/reminder/snooze/r1?sig={sig_120}&exp={exp}&minutes=999"
        )
        assert r_ok.status_code == 200

        # Signature for minutes=10 does NOT work (server clamps to 120 first,
        # so the HMAC expectation binds to "120", not "10").
        sig_10 = _make_sig("r1", "snooze", exp, extra="10")
        r_bad = client.post(
            f"/api/reminder/snooze/r1?sig={sig_10}&exp={exp}&minutes=999"
        )
        assert r_bad.status_code == 403
        assert r_bad.json()["error"] == "bad_signature"

    def test_max_snoozes_returns_409(
        self, client, ntfy_on, tmp_db, monkeypatch, clean_scheduler
    ):
        from orchestrator import state_store
        from orchestrator.config import settings

        scheduler = clean_scheduler
        state_store.save_reminder("r1", "tick", "2026-04-20T09:00:00")
        monkeypatch.setattr(settings, "ntfy_max_snooze_count", 5, raising=False)

        # Pre-populate snooze_count = 5 directly
        with state_store.get_db() as conn:
            conn.execute(
                "UPDATE reminders SET snooze_count = 5 WHERE id = ?", ("r1",)
            )

        exp = int(time.time()) + 300
        sig = _make_sig("r1", "snooze", exp, extra="10")
        r = client.post(
            f"/api/reminder/snooze/r1?sig={sig}&exp={exp}&minutes=10"
        )
        assert r.status_code == 409
        assert r.json()["error"] == "max_snoozes_reached"
        assert r.json()["snooze_count"] == 5
        # Guardrail should have fired BEFORE add_job, so no reminder_r1 job exists
        assert scheduler.get_job("reminder_r1") is None

    def test_unknown_id_with_valid_sig_returns_404(
        self, client, ntfy_on, tmp_db, clean_scheduler
    ):
        exp = int(time.time()) + 300
        sig = _make_sig("ghost", "snooze", exp, extra="10")
        r = client.post(
            f"/api/reminder/snooze/ghost?sig={sig}&exp={exp}&minutes=10"
        )
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"
