"""
Recurring reminders — settings-page CRUD + APScheduler expansion.

Recurring rules live in the `recurring_reminders` SQLite table (see
`state_store.SCHEMA_SQL`). They never deliver directly; instead an
APScheduler job (`expand_due_reminders`) runs every 5 minutes, finds
rules whose `next_fire_at` falls within the next 5 minutes, and
materializes one-shot rows in the existing `reminders` table — same
dispatch path as `tool_set_reminder` (TTS + ntfy + Pushover).

Why this design: keeps delivery logic untouched. Recurring just
schedules the next concrete fire, then `deliver_reminder_job` does
the heavy lifting.

Cron strings: standard 5-field UNIX cron (`m h dom mon dow`). Validated
via `croniter.is_valid` at write time so bad rules are rejected before
they ever schedule.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from croniter import CroniterBadDateError, croniter

from orchestrator import state_store

logger = logging.getLogger(__name__)

EXPANSION_WINDOW_MINUTES = 5
ALLOWED_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ISO_TO_DAY = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class RecurringReminderError(ValueError):
    """Raised when a recurring rule fails validation."""


def validate_cron(expression: str) -> None:
    if not isinstance(expression, str) or not expression.strip():
        raise RecurringReminderError("cron_expression is required")
    if not croniter.is_valid(expression):
        raise RecurringReminderError(f"Invalid cron expression: {expression!r}")


def normalize_days(value: Any) -> str:
    """Coerce input (list or comma-string) to canonical 'mon,tue,...' string."""
    if value is None:
        return ",".join(ALLOWED_DAYS)
    if isinstance(value, str):
        items = [s.strip().lower() for s in value.split(",") if s.strip()]
    elif isinstance(value, list):
        items = [str(s).strip().lower() for s in value if str(s).strip()]
    else:
        raise RecurringReminderError("days_of_week must be list or comma string")

    if not items:
        return ",".join(ALLOWED_DAYS)

    bad = [d for d in items if d not in ALLOWED_DAYS]
    if bad:
        raise RecurringReminderError(f"days_of_week entries {bad} not in {list(ALLOWED_DAYS)}")
    # Preserve canonical order
    seen = set(items)
    return ",".join(d for d in ALLOWED_DAYS if d in seen)


def compute_next_fire(cron: str, after: datetime, days_of_week: str) -> datetime:
    """Return the next datetime the rule should fire, respecting the
    day-of-week filter (cron alone covers everything else).

    Iterates croniter forward until it lands on an allowed day. Caps the
    search at 14 days to avoid an infinite loop on a malformed combo
    (e.g. `0 9 * * mon` filtered to `tue,wed`).

    Raises RecurringReminderError on impossible crons (e.g. `0 0 30 2 *`)
    so callers can return a clean 400 / auto-disable rather than letting
    croniter's CroniterBadDateError bubble as a 500.
    """
    allowed = {d for d in days_of_week.split(",") if d}
    itr = croniter(cron, after)
    cap = after + timedelta(days=14)
    for _ in range(2000):  # belt and braces in addition to the date cap
        try:
            candidate = itr.get_next(datetime)
        except CroniterBadDateError as e:
            raise RecurringReminderError(f"Cron {cron!r} has no future fire date: {e}") from None
        if candidate > cap:
            break
        if not allowed:
            return candidate
        if ISO_TO_DAY.get(candidate.isoweekday()) in allowed:
            return candidate
    raise RecurringReminderError(
        f"Could not find a fire time for cron={cron!r} within 14 days of {after.isoformat()} (days={days_of_week})"
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_rules() -> List[Dict[str, Any]]:
    with state_store.get_db() as conn:
        rows = conn.execute("SELECT * FROM recurring_reminders ORDER BY enabled DESC, next_fire_at").fetchall()
    return [dict(r) for r in rows]


def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    with state_store.get_db() as conn:
        row = conn.execute("SELECT * FROM recurring_reminders WHERE id = ?", (rule_id,)).fetchone()
    return dict(row) if row else None


def _now_naive() -> datetime:
    """Naive `now()` matching how datetimes are persisted in this module.

    APScheduler is constructed with a tz-aware scheduler, but every datetime
    we hand it via `trigger="date"` is naive — APScheduler interprets naive
    `run_date` in the scheduler's tz, which on Helios matches `TZ`. We keep
    everything naive-local for storage consistency; if the deployment ever
    moves to a server whose system TZ != configured TZ, this is the single
    spot to switch to `datetime.now(scheduler.timezone).replace(tzinfo=None)`.
    """
    return datetime.now()


def _strip_control_chars(text: str) -> str:
    """Drop ASCII C0 + DEL control bytes (BEL, ESC, NUL, etc.) that the TTS
    pipeline might mis-render. Tabs / newlines are preserved.
    """
    return "".join(c for c in text if c == "\t" or c == "\n" or 32 <= ord(c) < 127 or ord(c) >= 160)


def create_rule(
    text: str,
    cron_expression: str,
    target: str = "both",
    days_of_week: Any = None,
    enabled: bool = True,
) -> Dict[str, Any]:
    if not text or not text.strip():
        raise RecurringReminderError("text is required")
    text = _strip_control_chars(text.strip())[:500]
    if not text:
        raise RecurringReminderError("text contains no usable characters")

    validate_cron(cron_expression)
    if target not in ("tts", "push", "both"):
        raise RecurringReminderError("target must be one of: tts, push, both")

    days = normalize_days(days_of_week)

    rule_id = uuid.uuid4().hex[:12]
    now = _now_naive()
    next_fire = compute_next_fire(cron_expression, now, days)

    iso_now = now.isoformat()
    with state_store.get_db() as conn:
        conn.execute(
            """INSERT INTO recurring_reminders
               (id, text, cron_expression, target, enabled, days_of_week,
                next_fire_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rule_id,
                text,
                cron_expression,
                target,
                1 if enabled else 0,
                days,
                next_fire.isoformat(),
                iso_now,
                iso_now,
            ),
        )
    logger.info(
        f"[RECURRING] Created rule {rule_id}: cron={cron_expression!r} days={days} next={next_fire.isoformat()}"
    )
    return get_rule(rule_id)  # type: ignore[return-value]


def update_rule(rule_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    existing = get_rule(rule_id)
    if not existing:
        return None

    text = updates.get("text", existing["text"])
    cron_expression = updates.get("cron_expression", existing["cron_expression"])
    target = updates.get("target", existing["target"])
    enabled_in = updates.get("enabled", bool(existing["enabled"]))
    days_in = updates.get("days_of_week", existing["days_of_week"])

    if not text or not str(text).strip():
        raise RecurringReminderError("text cannot be empty")
    text = _strip_control_chars(str(text).strip())[:500]
    if not text:
        raise RecurringReminderError("text contains no usable characters")

    validate_cron(cron_expression)
    if target not in ("tts", "push", "both"):
        raise RecurringReminderError("target must be one of: tts, push, both")

    days = normalize_days(days_in)

    # Recompute next_fire_at if scheduling fields changed
    schedule_changed = (
        cron_expression != existing["cron_expression"]
        or days != existing["days_of_week"]
        or (bool(enabled_in) and not bool(existing["enabled"]))
    )
    if schedule_changed:
        next_fire = compute_next_fire(cron_expression, _now_naive(), days)
        next_fire_iso = next_fire.isoformat()
    else:
        next_fire_iso = existing["next_fire_at"]

    iso_now = _now_naive().isoformat()
    with state_store.get_db() as conn:
        conn.execute(
            """UPDATE recurring_reminders
                  SET text = ?, cron_expression = ?, target = ?, enabled = ?,
                      days_of_week = ?, next_fire_at = ?, updated_at = ?
                WHERE id = ?""",
            (
                text,
                cron_expression,
                target,
                1 if enabled_in else 0,
                days,
                next_fire_iso,
                iso_now,
                rule_id,
            ),
        )
    logger.info(f"[RECURRING] Updated rule {rule_id}: schedule_changed={schedule_changed}")
    return get_rule(rule_id)


def delete_rule(rule_id: str) -> bool:
    with state_store.get_db() as conn:
        cursor = conn.execute("DELETE FROM recurring_reminders WHERE id = ?", (rule_id,))
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Expansion job (APScheduler)
# ---------------------------------------------------------------------------


async def expand_due_reminders() -> int:
    """Materialize one-shot reminders for any rule whose next_fire_at falls
    within the next EXPANSION_WINDOW_MINUTES.

    Returns the number of one-shot reminders created — useful for tests
    and the metric counter.

    Safety properties:
    - Idempotent: the UPDATE is guarded by `next_fire_at = <observed>` so
      a parallel run that already advanced the row is a no-op (rowcount=0).
      We skip dispatching if our update was rejected.
    - Strict forward progress: next_fire is computed from
      `fire_at + 1s` so the same minute can't fire twice on consecutive
      ticks under any clock drift.
    - Auto-disable on impossible cron: if `compute_next_fire` raises
      `RecurringReminderError` (e.g. `0 0 30 2 *`), the rule's `enabled`
      flag flips to 0 to stop the loop from spamming logs forever.
    """
    from orchestrator.metrics import (
        RECURRING_REMINDERS_EXPAND_ERRORS,
        RECURRING_REMINDERS_EXPANDED,
    )
    from orchestrator.reminder_manager import add_reminder
    from orchestrator.shared import scheduler
    from orchestrator.tool_handlers import deliver_reminder_job

    now = _now_naive()
    horizon = now + timedelta(minutes=EXPANSION_WINDOW_MINUTES)
    horizon_iso = horizon.isoformat()

    with state_store.get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM recurring_reminders
                WHERE enabled = 1 AND next_fire_at IS NOT NULL AND next_fire_at <= ?""",
            (horizon_iso,),
        ).fetchall()

    if not rows:
        return 0

    created = 0
    for row in rows:
        rule = dict(row)
        observed_next = rule["next_fire_at"]
        try:
            fire_at = datetime.fromisoformat(observed_next)
            # If a long downtime made us miss the original fire, schedule for
            # `now + 1s` so APScheduler doesn't reject a past run_date.
            if fire_at < now:
                fire_at = now + timedelta(seconds=1)

            # Compute the next fire BEFORE materializing — if the cron has
            # become impossible, we want to fail loudly without leaving a
            # one-shot row dangling. `+1s` is the strict-forward-progress guard.
            try:
                next_fire = compute_next_fire(
                    rule["cron_expression"],
                    fire_at + timedelta(seconds=1),
                    rule.get("days_of_week") or ",".join(ALLOWED_DAYS),
                )
            except RecurringReminderError as e:
                with state_store.get_db() as conn:
                    conn.execute(
                        "UPDATE recurring_reminders SET enabled = 0, updated_at = ? WHERE id = ?",
                        (now.isoformat(), rule["id"]),
                    )
                logger.warning(
                    f"[RECURRING] Auto-disabled rule {rule['id']} — impossible cron {rule['cron_expression']!r}: {e}"
                )
                RECURRING_REMINDERS_EXPAND_ERRORS.inc()
                continue

            # Idempotency guard: only advance the row if next_fire_at is still
            # what we observed. Two concurrent expansions can't both win.
            with state_store.get_db() as conn:
                cursor = conn.execute(
                    """UPDATE recurring_reminders
                          SET last_fired_at = ?, next_fire_at = ?, updated_at = ?
                        WHERE id = ? AND next_fire_at = ?""",
                    (
                        fire_at.isoformat(),
                        next_fire.isoformat(),
                        now.isoformat(),
                        rule["id"],
                        observed_next,
                    ),
                )
                if cursor.rowcount == 0:
                    # Somebody else already materialized this fire. Skip.
                    logger.debug(f"[RECURRING] Skip rule {rule['id']} — next_fire_at advanced under us")
                    continue

            reminder_id = f"rec_{rule['id']}_{int(fire_at.timestamp())}"
            add_reminder(reminder_id, rule["text"], fire_at, rule.get("target", "both"))
            scheduler.add_job(
                deliver_reminder_job,
                trigger="date",
                run_date=fire_at,
                args=[reminder_id],
                id=f"reminder_{reminder_id}",
                replace_existing=True,
            )
            created += 1
            RECURRING_REMINDERS_EXPANDED.inc()
            logger.info(
                f"[RECURRING] Expanded rule {rule['id']} -> reminder {reminder_id} at {fire_at.isoformat()}; "
                f"next at {next_fire.isoformat()}"
            )
        except Exception as e:  # noqa: BLE001
            RECURRING_REMINDERS_EXPAND_ERRORS.inc()
            logger.error(f"[RECURRING] Expansion failed for rule {rule.get('id')}: {e}", exc_info=True)

    return created
