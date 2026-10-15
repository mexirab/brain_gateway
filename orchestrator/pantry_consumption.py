"""
Consumption tracking (F-015 phase 4).

The pantry stays roughly right without anyone logging every egg:

1. Cook event — a planned prep or dinner marked cooked (dashboard button,
   "I made the lentil soup" in chat, or Done on its cooking reminder) deducts
   the recipe's ingredients: counts go down by the number in the amount
   ("2 cans" → 2, else 1), levels step full → half → low, staples are left
   alone. A batch prep also becomes prepped portions. One transaction; undo
   restores every item from the ledger's before-snapshots.
2. Portion eaten — "had a bowl" counts a prepped batch down by one and logs
   the meal with the selfcare tracker (raw ingredients were already deducted
   at cook time).
3. Decay — a daily pass marks unconfirmed perishables past their shelf life
   "probably gone" (the inventory asks "still have these?") and expires prep
   batches past their good-until date.
4. The weekly fridge check — a Saturday-evening nudge to photograph the
   shelves; the stock-take then also lists what Jess expected but didn't see.

Logs carry ids and counts only (docs/PRIVACY.md).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from orchestrator import diet_profiles, shared, state_store
from orchestrator import pantry_manager as pm
from orchestrator.metrics import (
    PANTRY_COOK_EVENTS,
    PANTRY_DECAY,
    PANTRY_DECAY_LAST_RUN,
    PANTRY_FRIDGE_CHECK,
    PANTRY_PORTIONS_EATEN,
    PANTRY_REMINDER_DONE,
)

logger = logging.getLogger(__name__)

PREP_FRIDGE_DAYS = 4  # how long cooked batch portions stay good in the fridge
MATCH_THRESHOLD = 88  # recipe ingredient ↔ inventory item (receipt_scanner.name_score)
FRIDGE_CHECK_PREFIX = "pc-"  # reminder ids; reminder_manager skips them for selfcare
FRIDGE_CHECK_WINDOW = timedelta(hours=3)
_LEVEL_DOWN = {"full": "half", "half": "low", "low": "low", "out": "out"}
_LEADING_NUMBER = re.compile(r"^\s*(\d{1,2})(?:\s|$|[a-zA-Z])")
# "12oz", "10 oz spinach", "200 g" are amounts of a thing, not how many to take.
_UNIT_AFTER_NUMBER = re.compile(
    r"^\s*\d+(?:[.,/]\d+)?\s*(?:oz|ounces?|g|grams?|kg|lbs?|pounds?|ml|l|liters?|litres?|cups?|tbsp|tsp)\b",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _entry(conn, entry_id: int) -> Optional[Dict[str, Any]]:
    r = conn.execute("SELECT * FROM meal_plan_entries WHERE id = ?", (entry_id,)).fetchone()
    return dict(r) if r else None


def _deduct_count(amount: str) -> int:
    """ "2 cans" → 2, "3 eggs" → 3, "1/2 cup" / "a handful" / "" → 1."""
    if _UNIT_AFTER_NUMBER.match(amount or ""):
        return 1
    m = _LEADING_NUMBER.match(amount or "")
    n = int(m.group(1)) if m else 1
    return max(1, min(n, 12))


def _match_inventory(conn, name: str, used: set) -> Optional[Dict[str, Any]]:
    from orchestrator.receipt_scanner import name_score

    key = pm._norm(name)
    best, best_score = None, 0.0
    for r in conn.execute("SELECT * FROM pantry_items WHERE status != 'gone'"):
        item = dict(r)
        if item["id"] in used or pm.is_out(item):
            continue
        score = name_score(key, pm._norm(item["name"]))
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= MATCH_THRESHOLD else None


# ---------------------------------------------------------------------------
# Cook / undo / skip
# ---------------------------------------------------------------------------


def cook_entry(entry_id: int, source: str = "dashboard") -> Dict[str, Any]:
    """Mark a planned prep or dinner as cooked and deduct its ingredients.
    Returns a summary the UI/tool can read back to the user."""
    with state_store.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        entry = _entry(conn, entry_id)
        if not entry:
            return {"ok": False, "error": "entry not found", "status_code": 404}
        if entry["slot"] not in ("prep", "dinner"):
            return {"ok": False, "error": "only a batch prep or a dinner can be cooked", "status_code": 400}
        plan = conn.execute("SELECT status FROM meal_plans WHERE id = ?", (entry["plan_id"],)).fetchone()
        if not plan or plan["status"] != "active":
            return {"ok": False, "error": "accept the plan before marking meals cooked", "status_code": 409}
        cur = conn.execute(
            "UPDATE meal_plan_entries SET status = 'cooked' WHERE id = ? AND status IN ('planned', 'skipped')",
            (entry_id,),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": f"already marked {entry['status']}", "status_code": 409}

        recipe = conn.execute("SELECT name FROM recipes WHERE id = ?", (entry["recipe_id"],)).fetchone()
        used: set = set()
        deducted: List[str] = []
        not_tracked: List[str] = []
        running_low: List[str] = []
        for ing in conn.execute(
            "SELECT item_name, amount, optional FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id",
            (entry["recipe_id"],),
        ).fetchall():
            item = _match_inventory(conn, ing["item_name"], used)
            if not item:
                if not ing["optional"]:
                    not_tracked.append(ing["item_name"])
                continue
            used.add(item["id"])
            before = dict(item)
            if item["track_mode"] == "staple":
                continue  # have/out only: cooking never empties a staple
            if item["track_mode"] == "count":
                new_qty = max((item["qty"] or 0) - _deduct_count(ing["amount"] or ""), 0)
                conn.execute(
                    "UPDATE pantry_items SET qty = ?, updated_at = ? WHERE id = ?", (new_qty, _now(), item["id"])
                )
            else:
                new_level = _LEVEL_DOWN.get(item["level"] or "full", "half")
                conn.execute(
                    "UPDATE pantry_items SET level = ?, updated_at = ? WHERE id = ?", (new_level, _now(), item["id"])
                )
            after = pm._row(conn, item["id"])
            pm._log_event(conn, item["id"], "consume", "cook", before, after, "cook", entry_id)
            deducted.append(after["name"])
            if pm.is_out(after) or after.get("level") == "low":
                running_low.append(after["name"])

        batch_id = None
        if entry["slot"] == "prep":
            good_until = (datetime.now() + timedelta(days=PREP_FRIDGE_DAYS)).date().isoformat()
            portions = int(entry["portions"] or 1)
            cur = conn.execute(
                """INSERT INTO prep_batches (plan_id, entry_id, recipe_id, cooked_at, portions_made, portions_left,
                       good_until, location, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'fridge', 'active')""",
                (entry["plan_id"], entry_id, entry["recipe_id"], _now(), portions, portions, good_until),
            )
            batch_id = cur.lastrowid
            conn.execute(
                "UPDATE meal_plan_entries SET prep_batch_id = ? WHERE plan_id = ? AND slot = 'lunch' AND recipe_id = ?",
                (batch_id, entry["plan_id"], entry["recipe_id"]),
            )
        conn.execute("UPDATE recipes SET times_cooked = times_cooked + 1 WHERE id = ?", (entry["recipe_id"],))

    _cancel_entry_reminders(entry_id)
    if entry["slot"] == "dinner":
        _record_meal(f"cooked:{entry_id}")
    PANTRY_COOK_EVENTS.labels(slot=entry["slot"], source=source).inc()
    logger.info(
        "[PANTRY] Entry %s cooked (%s, %s): %d deducted, %d not tracked, batch=%s",
        entry_id,
        entry["slot"],
        source,
        len(deducted),
        len(not_tracked),
        batch_id,
    )
    return {
        "ok": True,
        "recipe": recipe["name"] if recipe else "",
        "slot": entry["slot"],
        "deducted": deducted,
        "running_low": running_low,
        "not_tracked": not_tracked,
        "prep_batch_id": batch_id,
        "portions": int(entry["portions"] or 1) if entry["slot"] == "prep" else None,
    }


def undo_cook(entry_id: int) -> Dict[str, Any]:
    """A mis-tap: put every deducted item back exactly as it was and drop the
    prep batch (only while none of its portions has been eaten)."""
    with state_store.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        entry = _entry(conn, entry_id)
        if not entry:
            return {"ok": False, "error": "entry not found", "status_code": 404}
        if entry["status"] != "cooked":
            return {"ok": False, "error": "that meal isn't marked cooked", "status_code": 409}
        batch = conn.execute(
            "SELECT * FROM prep_batches WHERE entry_id = ? AND status != 'undone' ORDER BY id DESC LIMIT 1",
            (entry_id,),
        ).fetchone()
        if batch and batch["portions_left"] < batch["portions_made"]:
            return {"ok": False, "error": "some portions were already eaten — fix amounts by hand", "status_code": 409}
        # Only this cook's deductions: a cook → undo → cook cycle leaves the
        # first cook's events in the ledger too.
        last_undo = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM pantry_events WHERE ref_type = 'uncook' AND ref_id = ?", (entry_id,)
        ).fetchone()[0]
        events = conn.execute(
            "SELECT * FROM pantry_events WHERE ref_type = 'cook' AND ref_id = ? AND kind = 'consume' AND id > ? "
            "ORDER BY id DESC",
            (entry_id, last_undo),
        ).fetchall()
        restored = 0
        for ev in events:
            before = json.loads(ev["before_json"] or "{}")
            after = json.loads(ev["after_json"] or "{}")
            item = pm._row(conn, ev["item_id"])
            if not item or not before:
                continue
            # Give back what the cook took, on top of anything changed since
            # (a restock after cooking must survive the undo).
            if item["track_mode"] == "count":
                taken = max((before.get("qty") or 0) - (after.get("qty") or 0), 0)
                conn.execute(
                    "UPDATE pantry_items SET qty = COALESCE(qty, 0) + ?, updated_at = ? WHERE id = ?",
                    (taken, _now(), item["id"]),
                )
            elif item.get("level") == after.get("level"):
                conn.execute(
                    "UPDATE pantry_items SET level = ?, updated_at = ? WHERE id = ?",
                    (before.get("level"), _now(), item["id"]),
                )
            else:
                continue  # level changed by hand since the cook — leave it
            pm._log_event(conn, item["id"], "adjust", "cook", item, pm._row(conn, item["id"]), "uncook", entry_id)
            restored += 1
        conn.execute(
            "INSERT INTO pantry_events (item_id, kind, source, ref_type, ref_id, note, at) "
            "VALUES (NULL, 'adjust', 'cook', 'uncook', ?, 'undo marker', ?)",
            (entry_id, _now()),
        )
        if batch:
            conn.execute("UPDATE prep_batches SET status = 'undone', portions_left = 0 WHERE id = ?", (batch["id"],))
            conn.execute("UPDATE meal_plan_entries SET prep_batch_id = NULL WHERE prep_batch_id = ?", (batch["id"],))
        conn.execute("UPDATE meal_plan_entries SET status = 'planned' WHERE id = ?", (entry_id,))
        conn.execute("UPDATE recipes SET times_cooked = MAX(times_cooked - 1, 0) WHERE id = ?", (entry["recipe_id"],))
    PANTRY_COOK_EVENTS.labels(slot=entry["slot"], source="undo").inc()
    logger.info("[PANTRY] Entry %s cook undone: %d item(s) restored", entry_id, restored)
    return {"ok": True, "restored": restored}


def _cancel_entry_reminders(entry_id: int) -> None:
    """A skipped or already-cooked meal shouldn't still ping at dinner time.
    Only pending reminders go (delivered ones keep their buttons)."""
    try:
        from orchestrator.meal_planner import _cancel_reminders

        with state_store.get_db() as conn:
            ids = [
                r["reminder_id"]
                for r in conn.execute("SELECT reminder_id FROM meal_plan_reminders WHERE entry_id = ?", (entry_id,))
            ]
        _cancel_reminders(ids)
    except Exception as e:
        logger.warning("[PANTRY] Could not cancel reminders for entry %s: %s", entry_id, type(e).__name__)


def skip_entry(entry_id: int) -> Dict[str, Any]:
    with state_store.get_db() as conn:
        cur = conn.execute(
            "UPDATE meal_plan_entries SET status = 'skipped' WHERE id = ? AND status = 'planned' "
            "AND slot IN ('prep', 'dinner')",
            (entry_id,),
        )
        if cur.rowcount != 1:
            e = _entry(conn, entry_id)
            if not e:
                return {"ok": False, "error": "entry not found", "status_code": 404}
            return {"ok": False, "error": f"can't skip a {e['slot']} that is {e['status']}", "status_code": 409}
    _cancel_entry_reminders(entry_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Portions
# ---------------------------------------------------------------------------


def _record_meal(label: str) -> None:
    """Tell the selfcare tracker a meal happened (calms the meal nudges)."""
    try:
        from orchestrator import selfcare_manager

        selfcare_manager.record_meal_logged(label)
    except Exception as e:
        logger.warning("[PANTRY] Selfcare meal bridge failed: %s", type(e).__name__)


def list_batches(include_finished: bool = False) -> List[Dict[str, Any]]:
    where = "" if include_finished else "WHERE b.status = 'active'"
    with state_store.get_db() as conn:
        rows = conn.execute(
            f"""SELECT b.*, r.name AS recipe_name FROM prep_batches b
                JOIN recipes r ON r.id = b.recipe_id {where} ORDER BY b.good_until, b.id""",
        ).fetchall()
    today = date.today().isoformat()
    out = []
    for r in rows:
        d = dict(r)
        d["expired"] = d["good_until"] < today
        out.append(d)
    return out


def eat_portion(batch_id: Optional[int] = None, source: str = "dashboard") -> Dict[str, Any]:
    """One prepped portion eaten. Without an id: the active batch that goes
    off soonest."""
    with state_store.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if batch_id is None:
            row = conn.execute(
                "SELECT * FROM prep_batches WHERE status = 'active' AND portions_left > 0 "
                "ORDER BY good_until, id LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM prep_batches WHERE id = ?", (batch_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "no prepped portions to eat", "status_code": 404}
        if row["status"] != "active" or row["portions_left"] <= 0:
            return {"ok": False, "error": "that batch is finished", "status_code": 409}
        left = row["portions_left"] - 1
        conn.execute(
            "UPDATE prep_batches SET portions_left = ?, status = ? WHERE id = ?",
            (left, "active" if left > 0 else "finished", row["id"]),
        )
        # Tick today's planned lunch from this batch, if there is one.
        conn.execute(
            "UPDATE meal_plan_entries SET status = 'eaten' WHERE id = ("
            " SELECT id FROM meal_plan_entries WHERE prep_batch_id = ? AND slot = 'lunch' AND status = 'planned'"
            " ORDER BY (day = ?) DESC, day LIMIT 1)",
            (row["id"], date.today().isoformat()),
        )
        name = conn.execute("SELECT name FROM recipes WHERE id = ?", (row["recipe_id"],)).fetchone()
    _record_meal(f"portion:{row['id']}")
    PANTRY_PORTIONS_EATEN.labels(source=source).inc()
    logger.info("[PANTRY] Portion eaten from batch %s (%d left)", row["id"], left)
    return {
        "ok": True,
        "batch_id": row["id"],
        "recipe": name["name"] if name else "",
        "portions_left": left,
        "good_until": row["good_until"],
    }


def finish_batch(batch_id: int) -> Dict[str, Any]:
    """'The rest went in the bin' / 'we finished it'."""
    with state_store.get_db() as conn:
        cur = conn.execute(
            "UPDATE prep_batches SET status = 'finished', portions_left = 0 WHERE id = ? AND status = 'active'",
            (batch_id,),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "no active batch with that id", "status_code": 404}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Cooking-reminder Done taps (ntfy / Pushover / Telegram)
# ---------------------------------------------------------------------------


def link_reminder(conn, reminder_id: str, entry_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meal_plan_reminders (reminder_id, entry_id) VALUES (?, ?)", (reminder_id, entry_id)
    )


def on_reminder_done(reminder: Optional[Dict[str, Any]]) -> Optional[str]:
    """Done on a meal-plan cooking reminder = "I cooked it". Returns a short
    status for logs/confirmations, or None when the reminder isn't ours."""
    if not reminder or not str(reminder.get("id", "")).startswith("mp-"):
        return None
    if not getattr(shared, "PANTRY_ENABLED", False):
        return None
    try:
        with state_store.get_db() as conn:
            r = conn.execute(
                "SELECT entry_id FROM meal_plan_reminders WHERE reminder_id = ?", (reminder["id"],)
            ).fetchone()
        if not r:
            result = "unlinked"
        else:
            res = cook_entry(r["entry_id"], source="push")
            if res.get("ok"):
                result = "cooked"
            elif str(res.get("error", "")).startswith("already"):
                result = "already"
            else:
                result = "refused"  # plan no longer active / entry gone
    except Exception as e:
        PANTRY_REMINDER_DONE.labels(result="error").inc()
        logger.error(
            "[PANTRY] Cooking-reminder Done failed for %s: %s", reminder["id"], type(e).__name__, exc_info=True
        )
        return "error"
    PANTRY_REMINDER_DONE.labels(result=result).inc()
    logger.info("[PANTRY] Cooking-reminder Done %s: %s", reminder["id"], result)
    return result


# ---------------------------------------------------------------------------
# Decay + "use today"
# ---------------------------------------------------------------------------


DECAY_LAST_RUN_KEY = "pantry_decay_last_run_ts"


def seed_decay_gauge() -> None:
    """Startup: show the real last run, so a restart can't make a dead job look fresh."""
    try:
        entry = state_store.get_app_state_entry(DECAY_LAST_RUN_KEY)
        if entry and entry.get("value"):
            PANTRY_DECAY_LAST_RUN.set(float(entry["value"]))
    except Exception as e:
        logger.warning("[PANTRY] Could not seed the decay heartbeat: %s", type(e).__name__)


def run_decay() -> Dict[str, int]:
    """Daily job wrapper — errors are logged, never raised into the scheduler."""
    try:
        return _run_decay()
    except Exception as e:
        logger.error("[PANTRY] Decay failed: %s", type(e).__name__, exc_info=True)
        return {"items": 0, "batches": 0}


def _run_decay() -> Dict[str, int]:
    """Perishables unconfirmed past their shelf life → probably_gone;
    prep batches past good_until → expired."""
    now = datetime.now()
    items = 0
    with state_store.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM pantry_items WHERE status = 'active' AND shelf_life_days IS NOT NULL"
        ).fetchall()
        for r in rows:
            item = dict(r)
            if pm.is_out(item):
                continue
            try:
                confirmed = datetime.fromisoformat(item["last_confirmed_at"])
            except (TypeError, ValueError):
                continue
            if confirmed + timedelta(days=item["shelf_life_days"]) < now:
                cur = conn.execute(
                    "UPDATE pantry_items SET status = 'probably_gone', updated_at = ? "
                    "WHERE id = ? AND status = 'active' AND last_confirmed_at = ?",
                    (_now(), item["id"], item["last_confirmed_at"]),
                )
                if cur.rowcount != 1:
                    continue  # re-confirmed by a scan meanwhile
                pm._log_event(conn, item["id"], "adjust", "decay", item, pm._row(conn, item["id"]), note="decay")
                items += 1
        cur = conn.execute(
            "UPDATE prep_batches SET status = 'expired' WHERE status = 'active' AND good_until < ?",
            (now.date().isoformat(),),
        )
        batches = cur.rowcount
    PANTRY_DECAY.labels(kind="item").inc(items)
    PANTRY_DECAY.labels(kind="batch").inc(batches)
    stamp = time.time()
    PANTRY_DECAY_LAST_RUN.set(stamp)
    state_store.set_app_state(DECAY_LAST_RUN_KEY, str(stamp))
    logger.info("[PANTRY] Decay: %d item(s) probably gone, %d batch(es) expired", items, batches)
    return {"items": items, "batches": batches}


def use_soon_line() -> Optional[str]:
    """One spoken line for the morning briefing: perishables at their last
    day and prep batches to finish today. None when there's nothing."""
    names: List[str] = []
    today = date.today()
    for item in pm.list_items("active"):
        if pm.is_out(item) or not item.get("shelf_life_days"):
            continue
        try:
            confirmed = datetime.fromisoformat(item["last_confirmed_at"])
        except (TypeError, ValueError):
            continue
        left = item["shelf_life_days"] - (datetime.now() - confirmed).days
        if -2 <= left <= 1:  # older than that is decay's job, not "use today"
            names.append(item["name"].lower())
    for b in list_batches():
        if b["good_until"] <= (today + timedelta(days=1)).isoformat() and b["portions_left"] > 0:
            names.append(f"the {b['recipe_name'].lower()}")
    if not names:
        return None
    names = names[:4]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"Use today if you can: {joined}."


# ---------------------------------------------------------------------------
# Weekly fridge check nudge
# ---------------------------------------------------------------------------


def fridge_check_tick() -> str:
    """Runs every 15 min: on the configured day, once the configured time has
    passed, send the weekly fridge-check reminder (at most once per day)."""
    try:
        fc = diet_profiles.load().get("fridge_check") or {}
        hh, mm = (int(x) for x in str(fc.get("time", "18:00")).split(":"))
    except Exception as e:
        PANTRY_FRIDGE_CHECK.labels(result="error").inc()
        logger.error("[PANTRY] Fridge-check settings unreadable: %s", type(e).__name__)
        return "error"
    if not fc.get("enabled"):
        return "disabled"
    now = datetime.now()
    if diet_profiles.WEEKDAYS[now.weekday()] != fc.get("day", "sat"):
        return "not_today"
    due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < due:
        return "too_early"
    if now > due + FRIDGE_CHECK_WINDOW:
        return "missed"  # box was down at the set time — no late-night push
    today = now.date().isoformat()
    try:
        sent = state_store.get_app_state_entry("pantry_fridge_check_sent")
        if sent and sent.get("value") == today:
            return "already"
        # Claim the day before sending: a failure after this point skips one
        # week's nudge rather than repeating it every 15 minutes.
        state_store.set_app_state("pantry_fridge_check_sent", today)
    except Exception as e:
        PANTRY_FRIDGE_CHECK.labels(result="error").inc()
        logger.error("[PANTRY] Fridge-check state unavailable: %s", type(e).__name__)
        return "error"
    try:
        from orchestrator.reminder_manager import add_reminder
        from orchestrator.tool_handlers import deliver_reminder_job

        rid = f"{FRIDGE_CHECK_PREFIX}{uuid.uuid4().hex[:10]}"
        when = now + timedelta(seconds=30)
        add_reminder(
            rid,
            "Weekly fridge check: 3 shelf photos, about 2 minutes. Pantry page, Stock-take, Fridge.",
            when,
            "phone",
        )
        shared.scheduler.add_job(
            deliver_reminder_job, trigger="date", run_date=when, args=[rid], id=f"reminder_{rid}", replace_existing=True
        )
    except Exception as e:
        with contextlib.suppress(Exception):
            state_store.delete_app_state("pantry_fridge_check_sent")  # retry next tick
        PANTRY_FRIDGE_CHECK.labels(result="error").inc()
        logger.error("[PANTRY] Fridge-check nudge failed: %s", type(e).__name__, exc_info=True)
        return "error"
    PANTRY_FRIDGE_CHECK.labels(result="sent").inc()
    logger.info("[PANTRY] Weekly fridge-check nudge scheduled")
    return "sent"
