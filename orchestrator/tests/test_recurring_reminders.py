"""
Tests for orchestrator/recurring_reminders.py.

Covers:
1. validate_cron — empty / non-string / invalid syntax rejected; valid accepted.
2. compute_next_fire — standard cron, day-of-week filter, impossible cron raises
   RecurringReminderError (NOT raw CroniterBadDateError), filter+cron impossible combo.
3. normalize_days — list, comma string, mixed case, dupes, empty, None, bad name.
4. create_rule — stores correctly, computes next_fire_at, strips ASCII control chars,
   12-char hex id.
5. update_rule — unknown id None; schedule-changed flag recomputes; text-only preserves.
6. delete_rule — unknown id False.
7. expand_due_reminders (the meat) — no rules, materialize one, idempotency,
   auto-disable on impossible cron, downtime recovery clamps to now+1s.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import state_store
from orchestrator.recurring_reminders import (
    RecurringReminderError,
    compute_next_fire,
    create_rule,
    delete_rule,
    expand_due_reminders,
    get_rule,
    normalize_days,
    update_rule,
    validate_cron,
)


@pytest.fixture
def expansion_mocks(monkeypatch):
    """Patch the late-imported scheduler / add_reminder / deliver_reminder_job
    used inside expand_due_reminders so the test doesn't touch real APScheduler
    or HA / TTS pipelines.
    """
    from orchestrator import reminder_manager, shared, tool_handlers

    fake_add = MagicMock(return_value={"id": "stub"})
    fake_scheduler = MagicMock()
    fake_deliver = AsyncMock()

    monkeypatch.setattr(reminder_manager, "add_reminder", fake_add)
    monkeypatch.setattr(shared, "scheduler", fake_scheduler)
    monkeypatch.setattr(tool_handlers, "deliver_reminder_job", fake_deliver)

    return {
        "add_reminder": fake_add,
        "scheduler": fake_scheduler,
        "deliver_reminder_job": fake_deliver,
    }


# ---------------------------------------------------------------------------
# validate_cron
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(None, id="none"),
        pytest.param(12345, id="int"),
        pytest.param("not a cron expression at all", id="words"),
        pytest.param("99 99 99 99 99", id="out-of-range"),
    ],
)
def test_validate_cron_rejects(expr):
    with pytest.raises(RecurringReminderError):
        validate_cron(expr)  # type: ignore[arg-type]


@pytest.mark.parametrize("expr", ["0 9 * * *", "*/15 9-17 * * 1-5"])
def test_validate_cron_accepts_valid_expressions(expr):
    validate_cron(expr)  # should not raise


# ---------------------------------------------------------------------------
# compute_next_fire
# ---------------------------------------------------------------------------


def test_compute_next_fire_standard_cron():
    after = datetime(2026, 4, 29, 8, 0, 0)  # Wed 08:00
    nxt = compute_next_fire("0 9 * * *", after, "mon,tue,wed,thu,fri,sat,sun")
    assert nxt == datetime(2026, 4, 29, 9, 0, 0)


def test_compute_next_fire_day_of_week_filter():
    """`0 9 * * *` filtered to `mon` should return the next Monday at 09:00."""

    after = datetime(2026, 4, 29, 8, 0, 0)  # Wed
    nxt = compute_next_fire("0 9 * * *", after, "mon")
    assert nxt.isoweekday() == 1, f"expected Monday, got isoweekday={nxt.isoweekday()}"
    assert nxt.hour == 9 and nxt.minute == 0
    # Next Monday after Wed 2026-04-29 is 2026-05-04.
    assert nxt.date() == datetime(2026, 5, 4).date()


def test_compute_next_fire_impossible_cron_raises_recurring_error():
    """Impossible cron (Feb 30) must raise RecurringReminderError — croniter's
    raw CroniterBadDateError is wrapped (it is not a subclass, so
    pytest.raises would let it through and fail)."""
    after = datetime(2026, 4, 29, 8, 0, 0)
    with pytest.raises(RecurringReminderError):
        compute_next_fire("0 0 30 2 *", after, "mon,tue,wed,thu,fri,sat,sun")


def test_compute_next_fire_impossible_cron_filter_combo_raises_after_cap():
    """`0 9 * * mon` (Mondays only) filtered to `tue,wed` (no overlap)
    must hit the 14-day cap and raise."""

    after = datetime(2026, 4, 29, 8, 0, 0)
    with pytest.raises(RecurringReminderError):
        compute_next_fire("0 9 * * mon", after, "tue,wed")


# ---------------------------------------------------------------------------
# normalize_days
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "days,expected",
    [
        pytest.param(["mon", "wed"], "mon,wed", id="list"),
        pytest.param("mon,wed,fri", "mon,wed,fri", id="comma-string"),
        pytest.param(["Mon", "WED", " fri "], "mon,wed,fri", id="mixed-case-list"),
        pytest.param("MON, Wed,FRI", "mon,wed,fri", id="mixed-case-string"),
        # Output is canonical order (mon..sun), not insertion order.
        pytest.param(["fri", "mon", "fri", "wed"], "mon,wed,fri", id="dedupe-canonical-order"),
        pytest.param([], "mon,tue,wed,thu,fri,sat,sun", id="empty-list-all-days"),
        pytest.param(None, "mon,tue,wed,thu,fri,sat,sun", id="none-all-days"),
    ],
)
def test_normalize_days(days, expected):
    assert normalize_days(days) == expected


@pytest.mark.parametrize("days", [pytest.param(["mon", "funday"], id="bad-name"), pytest.param(42, id="int")])
def test_normalize_days_rejects(days):
    with pytest.raises(RecurringReminderError):
        normalize_days(days)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_rule / update_rule / delete_rule
# ---------------------------------------------------------------------------


def test_create_rule_stores_correctly_and_computes_next_fire(tmp_db):
    rule = create_rule(
        text="Drink water",
        cron_expression="0 9 * * *",
        target="both",
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
        enabled=True,
    )
    assert rule["text"] == "Drink water"
    assert rule["cron_expression"] == "0 9 * * *"
    assert rule["target"] == "both"
    assert rule["enabled"] == 1
    assert rule["days_of_week"] == "mon,tue,wed,thu,fri"
    assert rule["next_fire_at"]
    # Round-trip the next_fire_at through datetime.
    nxt = datetime.fromisoformat(rule["next_fire_at"])
    assert nxt > datetime.now() - timedelta(days=1)

    # 12-char hex id.
    assert len(rule["id"]) == 12
    assert all(c in "0123456789abcdef" for c in rule["id"])

    # Round-trip via get_rule.
    fetched = get_rule(rule["id"])
    assert fetched is not None
    assert fetched["text"] == "Drink water"


def test_create_rule_strips_control_chars(tmp_db):
    # BEL=\x07, ESC=\x1b, NUL=\x00 — all ASCII C0.
    text = "\x07\x1b\x00"
    with pytest.raises(RecurringReminderError):
        # Strip → empty → "no usable characters"
        create_rule(text=text, cron_expression="0 9 * * *")


def test_create_rule_strips_control_but_keeps_real_text(tmp_db):
    # Mix of garbage + real text.
    rule = create_rule(text="\x07hello\x00world\x1b", cron_expression="0 9 * * *")
    assert rule["text"] == "helloworld"


def test_create_rule_rejects_empty_text(tmp_db):
    with pytest.raises(RecurringReminderError):
        create_rule(text="", cron_expression="0 9 * * *")
    with pytest.raises(RecurringReminderError):
        create_rule(text="   ", cron_expression="0 9 * * *")


def test_create_rule_rejects_bad_target(tmp_db):
    with pytest.raises(RecurringReminderError):
        create_rule(text="x", cron_expression="0 9 * * *", target="haunted")


def test_update_rule_unknown_id_returns_none(tmp_db):
    assert update_rule("doesnotexist", {"text": "anything"}) is None


def test_update_rule_schedule_change_recomputes_next_fire(tmp_db):
    rule = create_rule(text="x", cron_expression="0 9 * * *")
    original_next = rule["next_fire_at"]

    updated = update_rule(rule["id"], {"cron_expression": "0 17 * * *"})
    assert updated is not None
    assert updated["cron_expression"] == "0 17 * * *"
    # Schedule changed → next_fire_at recomputed (different cron, different hour).
    new_next = datetime.fromisoformat(updated["next_fire_at"])
    assert new_next.hour == 17
    assert updated["next_fire_at"] != original_next


def test_update_rule_text_only_preserves_next_fire(tmp_db):
    rule = create_rule(text="old text", cron_expression="0 9 * * *")
    original_next = rule["next_fire_at"]

    updated = update_rule(rule["id"], {"text": "new text"})
    assert updated is not None
    assert updated["text"] == "new text"
    assert updated["next_fire_at"] == original_next


def test_delete_rule_unknown_returns_false(tmp_db):
    assert delete_rule("doesnotexist") is False


def test_delete_rule_known_returns_true(tmp_db):
    rule = create_rule(text="x", cron_expression="0 9 * * *")
    assert delete_rule(rule["id"]) is True
    assert get_rule(rule["id"]) is None


# ---------------------------------------------------------------------------
# expand_due_reminders
# ---------------------------------------------------------------------------
async def test_expand_no_rules_returns_zero(tmp_db, expansion_mocks):
    n = await expand_due_reminders()
    assert n == 0
    expansion_mocks["add_reminder"].assert_not_called()
    expansion_mocks["scheduler"].add_job.assert_not_called()


def _seed_rule_with_next_fire(rule_id: str, cron: str, next_fire_at: datetime, text: str = "x"):
    """Insert a rule directly with a controlled next_fire_at."""

    iso_now = datetime.now().isoformat()
    with state_store.get_db() as conn:
        conn.execute(
            """INSERT INTO recurring_reminders
               (id, text, cron_expression, target, enabled, days_of_week,
                next_fire_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_id,
                text,
                cron,
                "both",
                1,
                "mon,tue,wed,thu,fri,sat,sun",
                next_fire_at.isoformat(),
                iso_now,
                iso_now,
            ),
        )


async def test_expand_materializes_due_rule(tmp_db, expansion_mocks):
    next_fire = datetime.now() + timedelta(seconds=30)
    _seed_rule_with_next_fire("r1", "0 9 * * *", next_fire)

    n = await expand_due_reminders()
    assert n == 1

    # add_reminder + scheduler.add_job both called once.
    assert expansion_mocks["add_reminder"].call_count == 1
    assert expansion_mocks["scheduler"].add_job.call_count == 1

    # next_fire_at advanced past the original.
    with state_store.get_db() as conn:
        row = conn.execute("SELECT next_fire_at, last_fired_at FROM recurring_reminders WHERE id = 'r1'").fetchone()
    new_next = datetime.fromisoformat(row["next_fire_at"])
    assert new_next > next_fire
    assert row["last_fired_at"] is not None


async def test_expand_is_idempotent(tmp_db, expansion_mocks):
    """Calling expand twice in a row should NOT re-materialize the same fire."""

    next_fire = datetime.now() + timedelta(seconds=30)
    _seed_rule_with_next_fire("r1", "0 9 * * *", next_fire)

    n1 = await expand_due_reminders()
    assert n1 == 1
    n2 = await expand_due_reminders()
    # Second call: next_fire_at has been advanced way past horizon, so no rules
    # are returned by the SELECT — count is 0.
    assert n2 == 0
    assert expansion_mocks["add_reminder"].call_count == 1


async def test_expand_auto_disables_impossible_cron(tmp_db, expansion_mocks):
    """A rule with an impossible cron (Feb 30) and a past next_fire_at must
    be auto-disabled (enabled=0), NOT loop forever."""

    past_next = datetime.now() - timedelta(minutes=10)
    _seed_rule_with_next_fire("bad", "0 0 30 2 *", past_next)

    n = await expand_due_reminders()
    # No reminder was materialized — the impossible cron raised before insert.
    assert n == 0
    expansion_mocks["add_reminder"].assert_not_called()
    expansion_mocks["scheduler"].add_job.assert_not_called()

    # Rule auto-disabled.
    with state_store.get_db() as conn:
        row = conn.execute("SELECT enabled FROM recurring_reminders WHERE id = 'bad'").fetchone()
    assert row["enabled"] == 0


async def test_expand_downtime_recovery_clamps_to_now_plus_one_second(tmp_db, expansion_mocks):
    """A long downtime that left next_fire_at in the past must produce a SINGLE
    catch-up fire at now+1s — not a flood."""

    past_next = datetime.now() - timedelta(hours=12)
    _seed_rule_with_next_fire("dr", "0 9 * * *", past_next)

    before = datetime.now()
    n = await expand_due_reminders()
    after = datetime.now()
    assert n == 1

    # add_reminder called exactly once with a trigger time ~ now+1s (inside
    # the [before, after + 5s] envelope).
    assert expansion_mocks["add_reminder"].call_count == 1
    args, _kwargs = expansion_mocks["add_reminder"].call_args
    # Signature: add_reminder(reminder_id, text, trigger_time, target)
    trigger_time = args[2]
    assert isinstance(trigger_time, datetime)
    # The clamped fire should be ~now+1s, comfortably between before and after+5s.
    assert before <= trigger_time <= after + timedelta(seconds=5)
