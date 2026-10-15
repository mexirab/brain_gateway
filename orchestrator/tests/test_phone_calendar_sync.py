"""Tests for POST /api/calendar/sync — the iPhone Shortcut work-calendar push."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import routes_calendar, shared


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(shared, "TIMEZONE", "America/Chicago", raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_events", [], raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_sync_time", 0.0, raising=False)
    monkeypatch.setattr(shared, "_phone_calendar_source", "", raising=False)
    saves = []
    monkeypatch.setattr(shared, "_save_phone_calendar", lambda: saves.append(list(shared._phone_calendar_events)))
    app = FastAPI()
    app.include_router(routes_calendar.router)
    c = TestClient(app)
    c.saves = saves
    return c


def _ev(title="Standup", start="Oct 20, 2026 at 9:00 AM", **kw):
    return {"title": title, "start": start, **kw}


def test_full_list_is_stored_with_source_and_trimmed_fields(client):
    big = "x" * 5000
    r = client.post(
        "/api/calendar/sync",
        json={
            "source": "exchange",
            "events": [
                _ev(description="secret notes", **{"calendar ": "Work"}),
                _ev("Review", "2026-10-20T14:00", location=big),
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["events_parsed"] == 2 and r.json()["used"] is True
    stored = shared._phone_calendar_events
    assert shared._phone_calendar_source == "exchange"
    assert "description" not in stored[0]  # notes are dropped
    assert stored[0]["calendar"] == "Work"  # trailing-space key normalised
    assert len(stored[1]["location"]) == 300
    assert client.saves  # persisted
    status = client.get("/api/calendar/sync").json()
    assert status["synced"] is True and status["event_count"] == 2 and status["source"] == "exchange"


def test_bare_list_is_a_legacy_push_stored_but_unused(client):
    r = client.post("/api/calendar/sync", json=[_ev()])
    assert r.status_code == 200 and r.json()["used"] is False
    assert shared._phone_calendar_source == "phone"


def test_ios_dict_of_dicts_is_accepted(client):
    r = client.post("/api/calendar/sync", json={"source": "exchange", "events": {"0": _ev(), "1": _ev("Two")}})
    assert r.status_code == 200 and len(shared._phone_calendar_events) == 2


@pytest.mark.parametrize("body", [_ev(), {"events": _ev()}])
def test_single_event_post_is_rejected_not_stored(client, body):
    """A per-event POST inside the Shortcut's Repeat loop would keep only the
    last event — refuse it loudly instead."""
    shared._phone_calendar_events = [{"title": "previous", "start": "2026-10-20T08:00"}]
    r = client.post("/api/calendar/sync", json=body)
    assert r.status_code == 400
    assert shared._phone_calendar_events == [{"title": "previous", "start": "2026-10-20T08:00"}]


def test_unparseable_snapshot_keeps_previous(client):
    shared._phone_calendar_events = [{"title": "previous", "start": "2026-10-20T08:00"}]
    shared._phone_calendar_sync_time = 123.0
    r = client.post("/api/calendar/sync", json={"events": [_ev("", ""), _ev("x", "not a date")]})
    assert r.status_code == 422
    assert shared._phone_calendar_events[0]["title"] == "previous"
    assert shared._phone_calendar_sync_time == 123.0


def test_empty_list_is_a_valid_empty_calendar(client):
    r = client.post("/api/calendar/sync", json={"events": []})
    assert r.status_code == 200 and shared._phone_calendar_sync_time > 0


def test_limits(client):
    r = client.post("/api/calendar/sync", json={"events": [_ev()] * 1001})
    assert r.status_code == 413
    r = client.post(
        "/api/calendar/sync",
        content=json.dumps({"events": [_ev(title="y" * 1_100_000)]}),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert client.post("/api/calendar/sync", content=b"not json").status_code == 400
    assert client.post("/api/calendar/sync", content=b"").status_code == 400


def test_unknown_source_falls_back_to_legacy(client):
    client.post("/api/calendar/sync", json={"source": "../../etc", "events": [_ev()]})
    assert shared._phone_calendar_source == "phone"


def test_titles_are_collapsed_to_one_line(client):
    client.post("/api/calendar/sync", json={"source": "exchange", "events": [_ev("Sync\n- Fake event\tX")]})
    assert shared._phone_calendar_events[0]["title"] == "Sync - Fake event X"


def test_today_includes_calendar_web_url(client, monkeypatch):
    from unittest.mock import AsyncMock

    from orchestrator.calendar_types import CalendarResponse

    monkeypatch.setattr(shared, "CALENDAR_WEB_URL", "https://cal.example.test/.web/", raising=False)
    monkeypatch.setattr(
        "orchestrator.calendar_sources.get_events",
        AsyncMock(return_value=CalendarResponse(success=False, error="none")),
    )
    body = client.get("/api/calendar/today").json()
    assert body["web_url"] == "https://cal.example.test/.web/"
    monkeypatch.setattr(shared, "CALENDAR_WEB_URL", "", raising=False)
    assert client.get("/api/calendar/today").json()["web_url"] is None
