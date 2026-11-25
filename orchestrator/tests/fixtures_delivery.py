"""Shared pytest fixtures for the delivery test files.

conftest.py star-imports this module, so every fixture listed in ``__all__``
is available to every test file. Give fixtures names unique across the suite
(prefix with the area) — a clash with another fixtures_*.py module is caught
by conftest at import time. Plain helper functions can be imported directly.

Covers reminders and their delivery channels (ntfy, Pushover, Telegram, Web
Push, dashboard inbox), the calendar and the briefings that read it.
"""

import asyncio
import contextlib

import pytest

__all__ = [
    "delivery_api_client",
    "delivery_capture_push",
    "delivery_channels_off",
    "delivery_clean_scheduler",
    "delivery_ntfy_off",
    "delivery_ntfy_on",
    "delivery_phone_cache",
    "delivery_pushover_off",
    "delivery_quiet",
    "delivery_reset_shared",
]

NTFY_SECRET = "x" * 40  # passes the >=32-char model_validator guard


# ---------------------------------------------------------------------------
# Plain helpers (import directly)
# ---------------------------------------------------------------------------


def sign(reminder_id: str, action: str, exp: int, extra: str = "") -> str:
    """HMAC signature for an ack/snooze callback URL."""
    from orchestrator.reminder_manager import _sign_callback

    return _sign_callback(reminder_id, action, exp, extra)


def drain_tasks_sync() -> None:
    """Run every pending task on the current loop to completion.

    For TestClient tests: a route that fires ``asyncio.create_task(...)``
    leaves the task pending on the loop; drive it so its mock is called."""
    loop = asyncio.get_event_loop()
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


async def drain_tasks() -> None:
    """Await every other pending task (fire-and-forget deliveries)."""
    pending = [t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def delivery_clean_scheduler():
    """The shared AsyncIOScheduler (never started) with reminder_* jobs
    purged before and after the test — it is a module-level singleton, so
    jobs would otherwise leak between tests."""
    from orchestrator.shared import scheduler

    def _purge():
        for job in list(scheduler.get_jobs()):
            if job.id.startswith("reminder_"):
                with contextlib.suppress(Exception):
                    scheduler.remove_job(job.id)

    _purge()
    yield scheduler
    _purge()


@pytest.fixture
def delivery_api_client():
    """FastAPI TestClient over the api_routes router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.api_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def delivery_channels_off(monkeypatch):
    """Every push channel off, so phone delivery rides on _send_notification
    alone (a host with e.g. TELEGRAM_ENABLED=true must not change results)."""
    from orchestrator.config import settings

    for flag in ("ntfy_enabled", "pushover_enabled", "telegram_enabled", "webpush_enabled"):
        monkeypatch.setattr(settings, flag, False, raising=False)
    return settings


@pytest.fixture
def delivery_quiet(delivery_channels_off, monkeypatch):
    """Channels off, and the speaker + HA phone push both succeed."""

    async def _ok(*a, **kw):
        return {"success": True}

    monkeypatch.setattr("orchestrator.tool_handlers._announce_voice", _ok)
    monkeypatch.setattr("orchestrator.tool_handlers._send_notification", _ok)
    return delivery_channels_off


@pytest.fixture
def delivery_ntfy_on(monkeypatch):
    """ntfy enabled on the live settings singleton with a known secret."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ntfy_hmac_secret", NTFY_SECRET, raising=False)
    monkeypatch.setattr(settings, "ntfy_url", "http://ntfy.test:8889", raising=False)
    monkeypatch.setattr(settings, "ntfy_topic", "jess-reminders", raising=False)
    monkeypatch.setattr(settings, "ntfy_callback_base_url", "http://helios.test:8888", raising=False)
    monkeypatch.setattr(settings, "ntfy_ack_exp_seconds", 1800, raising=False)
    monkeypatch.setattr(settings, "ntfy_max_snooze_count", 5, raising=False)
    monkeypatch.setattr(settings, "ntfy_default_priority", 3, raising=False)
    return settings


@pytest.fixture
def delivery_ntfy_off(monkeypatch):
    """ntfy disabled, secret still present (so a feature-flag gate can be
    shown to fire BEFORE the HMAC check)."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "ntfy_enabled", False, raising=False)
    monkeypatch.setattr(settings, "ntfy_hmac_secret", NTFY_SECRET, raising=False)
    return settings


@pytest.fixture
def delivery_pushover_off(monkeypatch):
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "pushover_enabled", False, raising=False)
    return settings


@pytest.fixture
def delivery_capture_push(monkeypatch):
    """Replace webpush_manager.send_to_all; returns the list of sends."""
    from orchestrator import webpush_manager

    calls = []

    async def _fake(title, body, url="/chat", tag=None):
        calls.append({"title": title, "body": body, "url": url, "tag": tag})
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(webpush_manager, "send_to_all", _fake)
    return calls


@pytest.fixture
def delivery_phone_cache(monkeypatch):
    """Empty, never-synced phone (work-calendar Shortcut) push cache."""
    from orchestrator import shared

    monkeypatch.setattr(shared, "_phone_calendar_events", [], raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_sync_time", 0.0, raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_source", "exchange", raising=False)
    return shared


@pytest.fixture
def delivery_reset_shared(delivery_phone_cache, monkeypatch):
    """Shared state the briefings and wind-down read: empty phone cache, DND
    off, no focus session (restored afterwards). Returns ``shared``."""
    shared = delivery_phone_cache
    monkeypatch.setattr(shared, "DND_ACTIVE", False)
    orig_focus = shared.current_focus_session.to_dict()
    shared.current_focus_session.reset()
    yield shared
    shared.current_focus_session.update(orig_focus)
