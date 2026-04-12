"""
Background jobs: YNAB transaction sync, weekly spending summary,
mid-month budget warning.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from orchestrator.reminder_manager import _announce_voice
from orchestrator.shared import TIMEZONE, profile

logger = logging.getLogger(__name__)


async def sync_ynab_transactions():
    """Background job: sync transactions from YNAB."""
    from orchestrator.finance_manager import _is_ynab_configured, ynab_sync_transactions

    if not _is_ynab_configured():
        return

    try:
        result = await ynab_sync_transactions()
        if result.get("synced", 0) > 0:
            logger.info(f"[YNAB_POLL] Synced {result['synced']} transactions")
    except Exception as e:
        logger.error(f"[YNAB_POLL] Error: {e}")


async def weekly_spending_summary():
    """Sunday evening: announce weekly spending summary via TTS."""
    from orchestrator.finance_manager import (
        _ensure_budget_period,
        _get_level_info,
        get_db,
    )

    try:
        with get_db() as conn:
            ym = _ensure_budget_period(conn)
            budget = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())
            _config = dict(conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone())
            game = dict(conn.execute("SELECT * FROM game_state WHERE id = 1").fetchone())

            spent = budget["discretionary_spent"]
            limit = budget["discretionary_budget"]
            remaining = max(0, limit - spent)
            pct = (spent / limit * 100) if limit > 0 else 0
            level_info = _get_level_info(game["level"])

        parts = [f"Hey {profile.user_name}, here's your weekly spending update."]

        if spent > limit:
            overspend = spent - limit
            parts.append(f"You're over budget by ${overspend:.0f}.")
            parts.append("Time to tighten up for the rest of the month!")
        elif pct >= 75:
            parts.append(
                f"You've spent ${spent:.0f} of your ${limit:.0f} budget. "
                f"That's {pct:.0f} percent with only ${remaining:.0f} left."
            )
            parts.append("Getting close! Keep an eye on it this week.")
        elif pct >= 50:
            parts.append(f"You've spent ${spent:.0f} of ${limit:.0f}. ${remaining:.0f} remaining. You're on track!")
        else:
            parts.append(f"Only ${spent:.0f} spent out of ${limit:.0f}. ${remaining:.0f} left. Looking great!")

        parts.append(
            f"You're Level {game['level']}, {level_info['title']}, "
            f"with {game['total_xp']} total XP "
            f"and a {game['streak_months']} month streak."
        )

        await _announce_voice(" ".join(parts), announcement_type="email")
        logger.info(f"[WEEKLY_SUMMARY] Delivered: ${spent:.2f}/{limit:.2f} ({pct:.0f}%)")

    except Exception as e:
        logger.error(f"[WEEKLY_SUMMARY] Error: {e}")


async def midmonth_budget_warning():
    """Mid-month check: if over 60% of discretionary spent, announce warning via TTS."""
    from orchestrator.finance_manager import _ensure_budget_period, get_db

    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz)

    # Only fire the actual warning between the 13th and 17th
    if today.day < 13 or today.day > 17:
        return

    try:
        with get_db() as conn:
            ym = _ensure_budget_period(conn)
            budget = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())

            spent = budget["discretionary_spent"]
            limit = budget["discretionary_budget"]
            if limit <= 0:
                return

            pct = spent / limit * 100
            remaining = max(0, limit - spent)

        if pct < 60:
            logger.info(f"[MIDMONTH] Budget at {pct:.0f}% — no warning needed")
            return

        if pct >= 100:
            overspend = spent - limit
            message = (
                f"Heads up {profile.user_name}. You're already over your monthly budget "
                f"by ${overspend:.0f} and we're only halfway through the month. "
                f"Future you is taking damage!"
            )
        elif pct >= 80:
            message = (
                f"Budget warning {profile.user_name}. You've used {pct:.0f} percent of your "
                f"monthly budget with half the month still to go. "
                f"Only ${remaining:.0f} left. Be careful!"
            )
        else:
            message = (
                f"Mid-month check. You've spent {pct:.0f} percent of your budget. "
                f"${remaining:.0f} left for the rest of the month. Keep it steady!"
            )

        await _announce_voice(message, announcement_type="finance")
        logger.info(f"[MIDMONTH] Warning delivered: {pct:.0f}% spent")

    except Exception as e:
        logger.error(f"[MIDMONTH] Error: {e}")
