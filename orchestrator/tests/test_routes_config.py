"""
Tests for orchestrator/routes_config.py — the /api/config/* settings page API.

Covers:
- Bearer auth: 401 without token, 200 with.
- GET /api/config/identity shape.
- PUT /api/config/identity happy path round-trip; bad timezone → 400 with
  structured detail; bad tone → 400.
- PUT /api/config/selfcare partial categories merge; unknown category → 400.
- PUT /api/config/quiet_hours happy path; bad day → 400.
- POST /api/config/recurring_reminders happy → 201; impossible cron → 400; missing text → 400.
- PUT /api/config/recurring_reminders/{id} unknown id → 404.
- DELETE happy → 200 + {ok:true}; second delete → 404.
- Malformed JSON body → 400 (NOT 500).
- Audit table: 3 PUTs → 3 rows in config_changes.
"""

from __future__ import annotations

import pytest

API_TOKEN = "test-config-token"  # nosec B105 — test-only constant
AUTH = {"Authorization": f"Bearer {API_TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Build a TestClient over a fresh FastAPI app with the config router mounted
    behind a bearer middleware. Isolates state DB + selfcare schedule + profile
    overrides per test."""
    from unittest.mock import AsyncMock, MagicMock

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    # 1. Isolate persistence paths.
    from orchestrator import (
        announcement_routes,
        background_jobs,
        routine_manager,
        selfcare_schedule,
        shared,
        state_store,
        user_profile,
    )

    db_path = str(tmp_path / "config_routes.db")
    sched_path = str(tmp_path / "selfcare_schedule.yaml")
    overrides_path = str(tmp_path / "user_profile_overrides.yaml")
    routines_base = str(tmp_path / "routines_base.yaml")
    routines_overrides = str(tmp_path / "routines_overrides.yaml")
    ann_routes_path = str(tmp_path / "announcement_routes.yaml")

    monkeypatch.setattr(state_store, "DB_PATH", db_path)
    monkeypatch.setattr(selfcare_schedule, "SCHEDULE_PATH", sched_path)
    monkeypatch.setattr(selfcare_schedule, "_cache", None)
    # PUT /api/config/speakers writes via announcement_routes.save_routes, whose
    # ROUTES_PATH is captured at import (default /app/data/...). Without this it
    # writes to /app — Permission denied on a clean CI runner.
    monkeypatch.setattr(announcement_routes, "ROUTES_PATH", ann_routes_path)
    monkeypatch.setattr(announcement_routes, "_cache", None)
    monkeypatch.setattr(user_profile, "_OVERRIDES_PATH", overrides_path)
    # Force profile reload on each test so cached overrides from prior tests don't bleed.
    monkeypatch.setattr(user_profile, "_profile", None)
    # Routines paths read os.environ.get at call time, so setenv works.
    monkeypatch.setenv("ROUTINES_YAML_PATH", routines_base)
    monkeypatch.setenv("ROUTINES_OVERRIDES_PATH", routines_overrides)

    # Stub the scheduler so reload_routines_and_reschedule doesn't touch a
    # real APScheduler. get_jobs returns [] by default; add_job/remove_job
    # are no-ops returning a MagicMock.
    fake_scheduler = MagicMock()
    fake_scheduler.get_jobs.return_value = []
    monkeypatch.setattr(shared, "scheduler", fake_scheduler)
    # And neutralize the routine_manager.load_routines coroutine + trigger_routine
    monkeypatch.setattr(routine_manager, "load_routines", AsyncMock())
    monkeypatch.setattr(background_jobs, "trigger_routine", AsyncMock())

    state_store.init_db()

    # 2. Build an app with bearer auth identical to BearerAuthMiddleware shape.
    app = FastAPI()

    class TestBearerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if auth == f"Bearer {API_TOKEN}":
                return await call_next(request)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    app.add_middleware(TestBearerMiddleware)

    from orchestrator.routes_config import router as config_router

    app.include_router(config_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_get_identity_without_bearer_returns_401(client):
    r = client.get("/api/config/identity")
    assert r.status_code == 401


def test_get_identity_with_bearer_returns_200(client):
    r = client.get("/api/config/identity", headers=AUTH)
    assert r.status_code == 200


def test_put_selfcare_without_bearer_returns_401(client):
    r = client.put("/api/config/selfcare", json={"categories": {"water": {"enabled": True}}})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------


def test_get_features_without_bearer_returns_401(client):
    r = client.get("/api/config/features")
    assert r.status_code == 401


def test_get_features_reflects_settings(client, monkeypatch):
    """GET /api/config/features returns the three runtime flags read live from
    config.settings — the dashboard nav uses these to hide disabled features."""
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "workouts_enabled", True)
    monkeypatch.setattr(settings, "meals_enabled", False)
    monkeypatch.setattr(settings, "jess_advanced", True)

    r = client.get("/api/config/features", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {
        "workouts_enabled": True,
        "meals_enabled": False,
        "jess_advanced": True,
    }


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_get_identity_returns_expected_fields(client):
    r = client.get("/api/config/identity", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    for key in ("assistant_name", "user_name", "adhd_mode", "tone_preference", "timezone"):
        assert key in body, f"missing {key} in identity response"


def test_put_identity_happy_path_round_trip(client, monkeypatch):
    # The orchestrator container sets TZ in env, which user_profile applies as
    # the highest-priority override on reload. Drop it for this test so the
    # timezone we PUT actually round-trips through the YAML overrides.
    monkeypatch.delenv("TZ", raising=False)

    payload = {
        "user_name": "Tester",
        "tone_preference": "direct",
        "timezone": "America/New_York",
    }
    r = client.put("/api/config/identity", json=payload, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_name"] == "Tester"
    assert body["tone_preference"] == "direct"
    assert body["timezone"] == "America/New_York"

    # GET reflects the same values.
    r2 = client.get("/api/config/identity", headers=AUTH)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["user_name"] == "Tester"
    assert body2["tone_preference"] == "direct"
    assert body2["timezone"] == "America/New_York"


def test_put_identity_bad_timezone_returns_400_with_structured_detail(client):
    r = client.put(
        "/api/config/identity",
        json={"timezone": "Mars/Olympus_Mons"},
        headers=AUTH,
    )
    assert r.status_code == 400
    body = r.json()
    detail = body["detail"]
    assert isinstance(detail, list)
    assert detail
    err = detail[0]
    # Validation error shape: {type, loc, msg, input}
    assert "type" in err and "loc" in err and "msg" in err
    assert "input" in err
    assert "timezone" in err["loc"]


def test_put_identity_bad_tone_returns_400(client):
    r = client.put(
        "/api/config/identity",
        json={"tone_preference": "haunted"},
        headers=AUTH,
    )
    assert r.status_code == 400
    body = r.json()
    detail = body["detail"]
    assert isinstance(detail, list)
    assert any("tone_preference" in err.get("loc", []) for err in detail)


# ---------------------------------------------------------------------------
# Selfcare
# ---------------------------------------------------------------------------


def test_put_selfcare_partial_categories_merge(client):
    """A partial PUT should leave untouched categories present (via load_schedule defaults)."""
    payload = {"categories": {"water": {"enabled": False, "interval_minutes": 120}}}
    r = client.put("/api/config/selfcare", json=payload, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    cats = body["categories"]
    assert cats["water"]["enabled"] is False
    assert cats["water"]["interval_minutes"] == 120
    # Other defaults still present.
    for c in ("meds", "meals", "movement"):
        assert c in cats, f"category {c} dropped after partial save"


def test_put_selfcare_unknown_category_returns_400(client):
    r = client.put(
        "/api/config/selfcare",
        json={"categories": {"haunted_room": {"enabled": True}}},
        headers=AUTH,
    )
    assert r.status_code == 400
    body = r.json()
    assert "haunted_room" in body["detail"] or "Unknown" in body["detail"]


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def test_put_quiet_hours_happy_path(client):
    payload = {"start": "23:00", "end": "06:30", "days": ["mon", "tue", "wed"]}
    r = client.put("/api/config/quiet_hours", json=payload, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"] == "23:00"
    assert body["end"] == "06:30"
    assert set(body["days"]) == {"mon", "tue", "wed"}


def test_put_quiet_hours_bad_day_returns_400(client):
    r = client.put(
        "/api/config/quiet_hours",
        json={"days": ["mon", "funday"]},
        headers=AUTH,
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Recurring reminders
# ---------------------------------------------------------------------------


def test_post_recurring_happy_returns_201(client):
    r = client.post(
        "/api/config/recurring_reminders",
        json={
            "text": "Drink water",
            "cron_expression": "0 9 * * *",
            "target": "both",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri"],
        },
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    rule = r.json()
    assert rule["text"] == "Drink water"
    assert rule["cron_expression"] == "0 9 * * *"
    assert rule["next_fire_at"]
    assert len(rule["id"]) == 12


def test_post_recurring_impossible_cron_returns_400(client):
    """`0 0 30 2 *` (Feb 30) → RecurringReminderError → 400, never 500."""
    r = client.post(
        "/api/config/recurring_reminders",
        json={
            "text": "never fires",
            "cron_expression": "0 0 30 2 *",
        },
        headers=AUTH,
    )
    assert r.status_code == 400, r.text


def test_post_recurring_missing_text_returns_400(client):
    r = client.post(
        "/api/config/recurring_reminders",
        json={"cron_expression": "0 9 * * *"},
        headers=AUTH,
    )
    assert r.status_code == 400


def test_put_recurring_unknown_id_returns_404(client):
    r = client.put(
        "/api/config/recurring_reminders/doesnotexist",
        json={"text": "anything"},
        headers=AUTH,
    )
    assert r.status_code == 404


def test_delete_recurring_happy_then_second_delete_404(client):
    # Create.
    r = client.post(
        "/api/config/recurring_reminders",
        json={"text": "x", "cron_expression": "0 9 * * *"},
        headers=AUTH,
    )
    assert r.status_code == 201
    rule_id = r.json()["id"]

    # First delete.
    r = client.delete(f"/api/config/recurring_reminders/{rule_id}", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "id": rule_id}

    # Second delete.
    r2 = client.delete(f"/api/config/recurring_reminders/{rule_id}", headers=AUTH)
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------


def test_put_identity_malformed_json_returns_400(client):
    r = client.put(
        "/api/config/identity",
        content=b"{not json at all",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text


def test_put_selfcare_malformed_json_returns_400(client):
    r = client.put(
        "/api/config/selfcare",
        content=b"{[",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_put_quiet_hours_malformed_json_returns_400(client):
    r = client.put(
        "/api/config/quiet_hours",
        content=b"not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Audit table — 3 PUTs → 3 rows
# ---------------------------------------------------------------------------


def test_three_puts_produce_three_audit_rows(client):
    from orchestrator import state_store

    # PUT 1: identity.
    r1 = client.put(
        "/api/config/identity",
        json={"user_name": "AuditTester"},
        headers=AUTH,
    )
    assert r1.status_code == 200, r1.text

    # PUT 2: selfcare.
    r2 = client.put(
        "/api/config/selfcare",
        json={"categories": {"water": {"enabled": True, "interval_minutes": 60}}},
        headers=AUTH,
    )
    assert r2.status_code == 200, r2.text

    # PUT 3: quiet_hours.
    r3 = client.put(
        "/api/config/quiet_hours",
        json={"start": "22:00", "end": "07:00"},
        headers=AUTH,
    )
    assert r3.status_code == 200, r3.text

    with state_store.get_db() as conn:
        rows = conn.execute("SELECT panel FROM config_changes ORDER BY id").fetchall()
    panels = [r["panel"] for r in rows]
    assert len(panels) == 3, f"expected 3 audit rows, got {panels}"
    assert panels == ["identity", "selfcare", "quiet_hours"]


# ---------------------------------------------------------------------------
# Routines panel — /api/config/routines
# ---------------------------------------------------------------------------


def _seed_routines_yaml(path: str, data: dict) -> None:
    """Write a routines yaml file to the given path."""
    import yaml as _yaml

    with open(path, "w") as f:
        _yaml.safe_dump(data, f)


def _routines_with_ha_action() -> dict:
    return {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"type": "scheduled", "time": "07:00", "days": ["mon", "tue", "wed"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 10,
                "steps": [
                    {
                        "id": "meds",
                        "label": "Take meds",
                        "est_minutes": 2,
                        "skippable": False,
                        "ha_action": {
                            "entity_id": "light.bedroom",
                            "service": "turn_on",
                            "data": {"brightness": 200},
                        },
                    },
                    {
                        "id": "shower",
                        "label": "Shower",
                        "est_minutes": 15,
                        "skippable": True,
                    },
                ],
            }
        }
    }


def test_get_routines_returns_panel_shape(client):
    """Seed an existing routines.yaml; GET should return the editable shape."""
    import os

    overrides_path = os.environ["ROUTINES_OVERRIDES_PATH"]
    _seed_routines_yaml(overrides_path, _routines_with_ha_action())

    r = client.get("/api/config/routines", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "routines" in body
    morning = body["routines"]["morning"]
    assert morning["display_name"] == "Morning Routine"
    assert morning["trigger"]["time"] == "07:00"
    assert set(morning["trigger"]["days"]) == {"mon", "tue", "wed"}
    assert morning["speaker"] == "media_player.bedroom"
    assert morning["nudge_delay_minutes"] == 10
    # Steps shape — only id/label/est_minutes/skippable, NOT ha_action
    meds_step = morning["steps"][0]
    assert {"id", "label", "est_minutes", "skippable"} <= set(meds_step.keys())
    assert "ha_action" not in meds_step


def test_get_routines_without_bearer_returns_401(client):
    r = client.get("/api/config/routines")
    assert r.status_code == 401


def test_put_routines_happy_path_returns_reload_summary(client):
    """PUT happy path → 200 with response containing _reload.{loaded,rescheduled,removed}."""
    payload = {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"time": "07:00", "days": ["mon", "tue"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 10,
                "steps": [
                    {"id": "meds", "label": "Take meds", "est_minutes": 2, "skippable": False},
                    {"id": "shower", "label": "Shower", "est_minutes": 15, "skippable": True},
                ],
            }
        }
    }
    r = client.put("/api/config/routines", json=payload, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "_reload" in body
    reload = body["_reload"]
    assert "loaded" in reload
    assert "rescheduled" in reload
    assert "removed" in reload
    # And the after-state contains the same routine.
    assert "morning" in body["routines"]


def test_put_routines_bad_time_returns_400(client):
    payload = {
        "routines": {
            "morning": {
                "trigger": {"time": "25:99"},
                "steps": [{"id": "x", "label": "x"}],
            }
        }
    }
    r = client.put("/api/config/routines", json=payload, headers=AUTH)
    assert r.status_code == 400, r.text


def test_put_routines_empty_routines_returns_400(client):
    r = client.put("/api/config/routines", json={"routines": {}}, headers=AUTH)
    assert r.status_code == 400, r.text
    body = r.json()
    detail = body["detail"]
    if isinstance(detail, str):
        assert "non-empty" in detail
    else:
        # Pydantic-formatted error list
        assert detail


def test_put_routines_malformed_json_returns_400(client):
    r = client.put(
        "/api/config/routines",
        content=b"{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text


def test_put_routines_preserves_ha_action_round_trip(client):
    """The killer integration test:
    1. Seed routines.yaml with a step that has ha_action.
    2. PUT a panel-shaped payload (no ha_action mentioned).
    3. Re-GET — ha_action stripped from panel view (sanity).
    4. Inspect on-disk file directly — ha_action MUST still be there.
    """
    import os

    import yaml as _yaml

    overrides_path = os.environ["ROUTINES_OVERRIDES_PATH"]
    _seed_routines_yaml(overrides_path, _routines_with_ha_action())

    # Verify GET strips it (sanity).
    r0 = client.get("/api/config/routines", headers=AUTH)
    assert r0.status_code == 200
    assert "ha_action" not in r0.json()["routines"]["morning"]["steps"][0]

    # PUT panel payload (no ha_action).
    panel_payload = {
        "routines": {
            "morning": {
                "display_name": "Morning Routine",
                "trigger": {"time": "07:30", "days": ["mon", "tue"]},
                "speaker": "media_player.bedroom",
                "nudge_delay_minutes": 12,
                "steps": [
                    {"id": "meds", "label": "Take meds (renamed)", "est_minutes": 3, "skippable": False},
                    {"id": "shower", "label": "Shower", "est_minutes": 15, "skippable": True},
                ],
            }
        }
    }
    r = client.put("/api/config/routines", json=panel_payload, headers=AUTH)
    assert r.status_code == 200, r.text

    # On-disk file still has ha_action on the meds step.
    with open(overrides_path) as _f:
        on_disk = _yaml.safe_load(_f)
    meds = on_disk["routines"]["morning"]["steps"][0]
    assert meds["ha_action"] == {
        "entity_id": "light.bedroom",
        "service": "turn_on",
        "data": {"brightness": 200},
    }
    # The label edit was applied.
    assert meds["label"] == "Take meds (renamed)"
    assert meds["est_minutes"] == 3
    # The trigger.time edit was applied.
    assert on_disk["routines"]["morning"]["trigger"]["time"] == "07:30"
    # And the GET still strips ha_action from the panel view.
    r2 = client.get("/api/config/routines", headers=AUTH)
    assert "ha_action" not in r2.json()["routines"]["morning"]["steps"][0]


# ---------------------------------------------------------------------------
# /api/config/speakers — Speakers panel
# ---------------------------------------------------------------------------


def test_get_speakers_returns_routes_and_categories(client):
    r = client.get("/api/config/speakers", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "routes" in body
    assert "effective" in body
    assert "categories" in body
    # All known categories should be present in the response routes + effective
    for cat in ("selfcare", "reminder", "calendar", "ambient", "progress", "focus", "briefing"):
        assert cat in body["routes"]
        assert cat in body["effective"]
        assert cat in body["categories"]
    # The `default` catch-all is exposed in the categories list (empty by default)
    assert "default" in body["categories"]


def test_get_speakers_without_bearer_returns_401(client):
    r = client.get("/api/config/speakers")
    assert r.status_code == 401


def test_put_speakers_happy_path(client):
    r = client.put(
        "/api/config/speakers",
        json={"routes": {"selfcare": "media_player.office_max"}},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["routes"]["selfcare"] == "media_player.office_max"


def test_put_speakers_unknown_category_returns_400(client):
    r = client.put(
        "/api/config/speakers",
        json={"routes": {"haunted_kitchen": "media_player.x"}},
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "haunted_kitchen" in r.json()["detail"]


def test_put_speakers_bad_value_returns_400(client):
    r = client.put(
        "/api/config/speakers",
        json={"routes": {"selfcare": "no_dot_here"}},
        headers=AUTH,
    )
    assert r.status_code == 400
    assert "must look like" in r.json()["detail"]


def test_put_speakers_malformed_json_returns_400(client):
    r = client.put(
        "/api/config/speakers",
        data=b"{not json",
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_put_speakers_empty_value_falls_back_to_legacy(client):
    """User clears a field → routes carries the empty value, effective
    carries the post-fallback resolved value (so the panel can render
    the fallback as placeholder text)."""
    # First set a custom value
    client.put(
        "/api/config/speakers",
        json={"routes": {"selfcare": "media_player.office_max"}},
        headers=AUTH,
    )
    # Then clear it
    r = client.put(
        "/api/config/speakers",
        json={"routes": {"selfcare": ""}},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    # Raw routes preserves the user's empty
    assert body["routes"]["selfcare"] == ""
    # Effective view shows the fallback that would be used at dispatch time
    assert body["effective"]["selfcare"] != ""


def test_get_speakers_discover_returns_list(client):
    r = client.get("/api/config/speakers/discover", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert "speakers" in body
    assert isinstance(body["speakers"], list)
