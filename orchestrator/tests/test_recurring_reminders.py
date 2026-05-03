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


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Use a fresh state_store DB for each test so rules don't leak."""
    from orchestrator import state_store

    db_path = str(tmp_path / "recurring.db")
    monkeypatch.setattr(state_store, "DB_PATH", db_path)
    state_store.init_db()
    return db_path


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


def test_validate_cron_rejects_empty_or_non_string():
    from orchestrator.recurring_reminders import RecurringReminderError, validate_cron

    with pytest.raises(RecurringReminderError):
        validate_cron("")
    with pytest.raises(RecurringReminderError):
        validate_cron("   ")
    with pytest.raises(RecurringReminderError):
        validate_cron(None)  # type: ignore[arg-type]
    with pytest.raises(RecurringReminderError):
        validate_cron(12345)  # type: ignore[arg-type]


def test_validate_cron_rejects_invalid_syntax():
    from orchestrator.recurring_reminders import RecurringReminderError, validate_cron

    with pytest.raises(RecurringReminderError):
        validate_cron("not a cron expression at all")
    with pytest.raises(RecurringReminderError):
        validate_cron("99 99 99 99 99")


def test_validate_cron_accepts_valid_expressions():
    from orchestrator.recurring_reminders import validate_cron

    # Should not raise.
    validate_cron("0 9 * * *")
    validate_cron("*/15 9-17 * * 1-5")


# ---------------------------------------------------------------------------
# compute_next_fire
# ---------------------------------------------------------------------------


def test_compute_next_fire_standard_cron():
    from orchestrator.recurring_reminders import compute_next_fire

    after = datetime(2026, 4, 29, 8, 0, 0)  # Wed 08:00
    nxt = compute_next_fire("0 9 * * *", after, "mon,tue,wed,thu,fri,sat,sun")
    assert nxt == datetime(2026, 4, 29, 9, 0, 0)


def test_compute_next_fire_day_of_week_filter():
    """`0 9 * * *` filtered to `mon` should return the next Monday at 09:00."""
    from orchestrator.recurring_reminders import compute_next_fire

    after = datetime(2026, 4, 29, 8, 0, 0)  # Wed
    nxt = compute_next_fire("0 9 * * *", after, "mon")
    assert nxt.isoweekday() == 1, f"expected Monday, got isoweekday={nxt.isoweekday()}"
    assert nxt.hour == 9 and nxt.minute == 0
    # Next Monday after Wed 2026-04-29 is 2026-05-04.
    assert nxt.date() == datetime(2026, 5, 4).date()


def test_compute_next_fire_impossible_cron_raises_recurring_error():
    """Impossible cron must raise RecurringReminderError, NOT
    croniter.CroniterBadDateError."""
    from croniter import CroniterBadDateError

    from orchestrator.recurring_reminders import RecurringReminderError, compute_next_fire

    after = datetime(2026, 4, 29, 8, 0, 0)
    with pytest.raises(RecurringReminderError):
        # Feb 30 doesn't exist → croniter would raise CroniterBadDateError;
        # we expect it to be wrapped.
        compute_next_fire("0 0 30 2 *", after, "mon,tue,wed,thu,fri,sat,sun")

    # Sanity: verify it is NOT propagating the raw croniter exception.
    try:
        compute_next_fire("0 0 30 2 *", after, "mon,tue,wed,thu,fri,sat,sun")
    except RecurringReminderError:
        pass
    except CroniterBadDateError:
        pytest.fail("Raw CroniterBadDateError leaked — should be wrapped in RecurringReminderError")


def test_compute_next_fire_impossible_cron_filter_combo_raises_after_cap():
    """`0 9 * * mon` (Mondays only) filtered to `tue,wed` (no overlap)
    must hit the 14-day cap and raise."""
    from orchestrator.recurring_reminders import RecurringReminderError, compute_next_fire

    after = datetime(2026, 4, 29, 8, 0, 0)
    with pytest.raises(RecurringReminderError):
        compute_next_fire("0 9 * * mon", after, "tue,wed")


# ---------------------------------------------------------------------------
# normalize_days
# ---------------------------------------------------------------------------


def test_normalize_days_list_input():
    from orchestrator.recurring_reminders import normalize_days

    assert normalize_days(["mon", "wed"]) == "mon,wed"


def test_normalize_days_comma_string():
    from orchestrator.recurring_reminders import normalize_days

    assert normalize_days("mon,wed,fri") == "mon,wed,fri"


def test_normalize_days_mixed_case_and_whitespace():
    from orchestrator.recurring_reminders import normalize_days

    assert normalize_days(["Mon", "WED", " fri "]) == "mon,wed,fri"
    assert normalize_days("MON, Wed,FRI") == "mon,wed,fri"


def test_normalize_days_dedupes_preserving_canonical_order():
    from orchestrator.recurring_reminders import normalize_days

    # Output is canonical order (mon..sun), not insertion order.
    assert normalize_days(["fri", "mon", "fri", "wed"]) == "mon,wed,fri"


def test_normalize_days_empty_list_returns_all_days():
    from orchestrator.recurring_reminders import normalize_days

    assert normalize_days([]) == "mon,tue,wed,thu,fri,sat,sun"


def test_normalize_days_none_returns_all_days():
    from orchestrator.recurring_reminders import normalize_days

    assert normalize_days(None) == "mon,tue,wed,thu,fri,sat,sun"


def test_normalize_days_rejects_bad_day_name():
    from orchestrator.recurring_reminders import RecurringReminderError, normalize_days

    with pytest.raises(RecurringReminderError):
        normalize_days(["mon", "funday"])


def test_normalize_days_rejects_wrong_type():
    from orchestrator.recurring_reminders import RecurringReminderError, normalize_days

    with pytest.raises(RecurringReminderError):
        normalize_days(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# create_rule / update_rule / delete_rule
# ---------------------------------------------------------------------------


def test_create_rule_stores_correctly_and_computes_next_fire(fresh_db):
    from orchestrator.recurring_reminders import create_rule, get_rule

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


def test_create_rule_strips_control_chars(fresh_db):
    from orchestrator.recurring_reminders import RecurringReminderError, create_rule

    # BEL=\x07, ESC=\x1b, NUL=\x00 — all ASCII C0.
    text = "\x07\x1b\x00"
    with pytest.raises(RecurringReminderError):
        # Strip → empty → "no usable characters"
        create_rule(text=text, cron_expression="0 9 * * *")


def test_create_rule_strips_control_but_keeps_real_text(fresh_db):
    from orchestrator.recurring_reminders import create_rule

    # Mix of garbage + real text.
    rule = create_rule(text="\x07hello\x00world\x1b", cron_expression="0 9 * * *")
    assert rule["text"] == "helloworld"


def test_create_rule_rejects_empty_text(fresh_db):
    from orchestrator.recurring_reminders import RecurringReminderError, create_rule

    with pytest.raises(RecurringReminderError):
        create_rule(text="", cron_expression="0 9 * * *")
    with pytest.raises(RecurringReminderError):
        create_rule(text="   ", cron_expression="0 9 * * *")


def test_create_rule_rejects_bad_target(fresh_db):
    from orchestrator.recurring_reminders import RecurringReminderError, create_rule

    with pytest.raises(RecurringReminderError):
        create_rule(text="x", cron_expression="0 9 * * *", target="haunted")


def test_update_rule_unknown_id_returns_none(fresh_db):
    from orchestrator.recurring_reminders import update_rule

    assert update_rule("doesnotexist", {"text": "anything"}) is None


def test_update_rule_schedule_change_recomputes_next_fire(fresh_db):
    from orchestrator.recurring_reminders import create_rule, update_rule

    rule = create_rule(text="x", cron_expression="0 9 * * *")
    original_next = rule["next_fire_at"]

    updated = update_rule(rule["id"], {"cron_expression": "0 17 * * *"})
    assert updated is not None
    assert updated["cron_expression"] == "0 17 * * *"
    # Schedule changed → next_fire_at recomputed (different cron, different hour).
    new_next = datetime.fromisoformat(updated["next_fire_at"])
    assert new_next.hour == 17
    assert updated["next_fire_at"] != original_next


def test_update_rule_text_only_preserves_next_fire(fresh_db):
    from orchestrator.recurring_reminders import create_rule, update_rule

    rule = create_rule(text="old text", cron_expression="0 9 * * *")
    original_next = rule["next_fire_at"]

    updated = update_rule(rule["id"], {"text": "new text"})
    assert updated is not None
    assert updated["text"] == "new text"
    assert updated["next_fire_at"] == original_next


def test_delete_rule_unknown_returns_false(fresh_db):
    from orchestrator.recurring_reminders import delete_rule

    assert delete_rule("doesnotexist") is False


def test_delete_rule_known_returns_true(fresh_db):
    from orchestrator.recurring_reminders import create_rule, delete_rule, get_rule

    rule = create_rule(text="x", cron_expression="0 9 * * *")
    assert delete_rule(rule["id"]) is True
    assert get_rule(rule["id"]) is None


# ---------------------------------------------------------------------------
# expand_due_reminders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_no_rules_returns_zero(fresh_db, expansion_mocks):
    from orchestrator.recurring_reminders import expand_due_reminders

    n = await expand_due_reminders()
    assert n == 0
    expansion_mocks["add_reminder"].assert_not_called()
    expansion_mocks["scheduler"].add_job.assert_not_called()


def _seed_rule_with_next_fire(rule_id: str, cron: str, next_fire_at: datetime, text: str = "x"):
    """Insert a rule directly with a controlled next_fire_at."""
    from orchestrator import state_store

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


@pytest.mark.asyncio
async def test_expand_materializes_due_rule(fresh_db, expansion_mocks):
    from orchestrator import state_store
    from orchestrator.recurring_reminders import expand_due_reminders

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


@pytest.mark.asyncio
async def test_expand_is_idempotent(fresh_db, expansion_mocks):
    """Calling expand twice in a row should NOT re-materialize the same fire."""
    from orchestrator.recurring_reminders import expand_due_reminders

    next_fire = datetime.now() + timedelta(seconds=30)
    _seed_rule_with_next_fire("r1", "0 9 * * *", next_fire)

    n1 = await expand_due_reminders()
    assert n1 == 1
    n2 = await expand_due_reminders()
    # Second call: next_fire_at has been advanced way past horizon, so no rules
    # are returned by the SELECT — count is 0.
    assert n2 == 0
    assert expansion_mocks["add_reminder"].call_count == 1


@pytest.mark.asyncio
async def test_expand_auto_disables_impossible_cron(fresh_db, expansion_mocks):
    """A rule with an impossible cron (Feb 30) and a past next_fire_at must
    be auto-disabled (enabled=0), NOT loop forever."""
    from orchestrator import state_store
    from orchestrator.recurring_reminders import expand_due_reminders

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


@pytest.mark.asyncio
async def test_expand_downtime_recovery_clamps_to_now_plus_one_second(fresh_db, expansion_mocks):
    """A long downtime that left next_fire_at in the past must produce a SINGLE
    catch-up fire at now+1s — not a flood."""
    from orchestrator.recurring_reminders import expand_due_reminders

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
