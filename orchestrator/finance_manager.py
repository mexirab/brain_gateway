"""
Financial Quest Board — SQLite persistence, game logic, budget sync, and API routes.

Gamified finance tracking for ADHD support:
- Health bar (discretionary budget tracking)
- XP / leveling system
- Streak tracking
- Side quests (savings goals)
- Future Self Damage calculator
- Boss battles (windfall months)
- Read-only sync from a self-hosted Actual Budget server for real spending
  (replaced the YNAB integration 2026-10; see orchestrator/actual_client.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator.schemas import CategoryMappingRequest, ReclassifyTransactionRequest

if TYPE_CHECKING:
    from orchestrator.actual_client import ActualSnapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/finance", tags=["finance"])

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("FINANCE_DB_PATH", "/app/data/finance.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS finance_config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_discretionary REAL NOT NULL DEFAULT 1000.00,
    monthly_investing REAL NOT NULL DEFAULT 400.00,
    monthly_buffer REAL NOT NULL DEFAULT 68.75,
    retirement_current REAL NOT NULL DEFAULT 518500.00,
    retirement_target_age INTEGER NOT NULL DEFAULT 62,
    current_age INTEGER NOT NULL DEFAULT 48,
    savings_rate REAL NOT NULL DEFAULT 0.20,
    expected_return REAL NOT NULL DEFAULT 0.07,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS game_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_xp INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    streak_months INTEGER NOT NULL DEFAULT 0,
    streak_best INTEGER NOT NULL DEFAULT 0,
    last_streak_month TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS xp_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    xp_amount INTEGER NOT NULL,
    description TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS budget_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year_month TEXT NOT NULL UNIQUE,
    discretionary_budget REAL NOT NULL,
    discretionary_spent REAL NOT NULL DEFAULT 0.00,
    investing_actual REAL NOT NULL DEFAULT 0.00,
    boss_battle_active INTEGER NOT NULL DEFAULT 0,
    boss_defeated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS side_quests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    target_amount REAL NOT NULL,
    saved_amount REAL NOT NULL DEFAULT 0.00,
    monthly_carve REAL NOT NULL DEFAULT 0.00,
    icon TEXT DEFAULT 'trophy',
    status TEXT NOT NULL DEFAULT 'active',
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- "<provider>:<id>" for synced rows (e.g. "actual:<uuid>"), NULL for manual.
    -- Unique via idx_transactions_external_id (created in _migrate_schema so
    -- legacy YNAB-era tables get it too).
    external_id TEXT,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    name TEXT NOT NULL,
    merchant_name TEXT,
    category TEXT,
    subcategory TEXT,
    is_discretionary INTEGER NOT NULL DEFAULT 1,
    -- Set by the dashboard dot (reclassify). NULL = follow category_mapping;
    -- 0/1 wins over the mapping on every sync and mapping change.
    discretionary_override INTEGER,
    budget_period TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS windfalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    amount REAL NOT NULL,
    invest_amount REAL,
    spend_amount REAL,
    budget_period TEXT,
    boss_defeated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS level_thresholds (
    level INTEGER PRIMARY KEY,
    retirement_min REAL NOT NULL,
    title TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider TEXT,
    budget_name TEXT,
    last_synced_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    last_result TEXT
);

CREATE TABLE IF NOT EXISTS category_mapping (
    category_name TEXT PRIMARY KEY,
    is_discretionary INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# Budget sync provider
# ---------------------------------------------------------------------------

# Value written to transactions.source and the external_id prefix for synced
# rows. Manual entries use source='manual'; legacy YNAB rows keep 'ynab'.
SYNC_PROVIDER = "actual"

LEVELS = [
    (1, 0, "Copper Adventurer"),
    (2, 525000, "Bronze Scout"),
    (3, 550000, "Silver Ranger"),
    (4, 575000, "Gold Knight"),
    (5, 600000, "Platinum Warden"),
    (6, 650000, "Diamond Guardian"),
    (7, 700000, "Emerald Champion"),
    (8, 750000, "Sapphire Sovereign"),
    (9, 800000, "Ruby Archmage"),
    (10, 900000, "Obsidian Legend"),
    (11, 1000000, "Millionaire Ascendant"),
]

XP_AWARDS = {
    "budget_under": 100,
    "investment_transfer": 50,
    "espp_split": 200,
    "bonus_split": 200,
    "boss_defeated": 200,
    "side_quest_complete": 150,
    "quarterly_review": 75,
    "streak_milestone": 50,
    "perfect_month": 50,
}

WINDFALL_MONTHS = {"03": "bonus", "06": "espp", "10": "bonus", "12": "espp"}


def get_db():
    """Get a SQLite connection with row factory."""
    from orchestrator.db import get_db as _get_db

    return _get_db(DB_PATH, foreign_keys=False)


def init_db():
    """Initialize database schema and seed data."""
    from orchestrator.db import init_db as _init_db

    _init_db(DB_PATH, SCHEMA_SQL, foreign_keys=False)
    with get_db() as conn:
        _migrate_schema(conn)

        # Seed default config if empty
        row = conn.execute("SELECT COUNT(*) FROM finance_config").fetchone()
        if row[0] == 0:
            conn.execute("INSERT INTO finance_config (id) VALUES (1)")
            logger.info("[FINANCE] Seeded default finance_config")

        # Seed default game state if empty
        row = conn.execute("SELECT COUNT(*) FROM game_state").fetchone()
        if row[0] == 0:
            conn.execute("INSERT INTO game_state (id) VALUES (1)")
            logger.info("[FINANCE] Seeded default game_state")

        # Seed level thresholds
        row = conn.execute("SELECT COUNT(*) FROM level_thresholds").fetchone()
        if row[0] == 0:
            conn.executemany(
                "INSERT INTO level_thresholds (level, retirement_min, title) VALUES (?, ?, ?)",
                LEVELS,
            )
            logger.info(f"[FINANCE] Seeded {len(LEVELS)} level thresholds")

    logger.info(f"[FINANCE] Database initialized at {DB_PATH}")


def _migrate_schema(conn) -> None:
    """Bring a YNAB-era finance.db forward to the provider-neutral schema.

    Idempotent. On a fresh DB it only creates the unique index. On a legacy
    DB it adds ``transactions.external_id`` (backfilled as ``ynab:<id>`` so the
    rows survive as history) and copies the old category mappings. Any DB
    without ``transactions.discretionary_override`` gets it (NULL = no override).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "external_id" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN external_id TEXT")
        if "ynab_transaction_id" in cols:
            conn.execute(
                "UPDATE transactions SET external_id = 'ynab:' || ynab_transaction_id "
                "WHERE ynab_transaction_id IS NOT NULL AND external_id IS NULL"
            )
            logger.info("[FINANCE] Migrated YNAB transaction ids to external_id")
    if "discretionary_override" not in cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN discretionary_override INTEGER")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_external_id ON transactions(external_id)")
    has_old_map = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ynab_category_mapping'"
    ).fetchone()
    if has_old_map:
        conn.execute(
            "INSERT OR IGNORE INTO category_mapping (category_name, is_discretionary) "
            "SELECT category_name, is_discretionary FROM ynab_category_mapping"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_year_month():
    return datetime.now().strftime("%Y-%m")


def _ensure_budget_period(conn, year_month=None):
    """Create budget period for the given month if it doesn't exist."""
    ym = year_month or _current_year_month()
    existing = conn.execute("SELECT id FROM budget_periods WHERE year_month = ?", (ym,)).fetchone()
    if existing:
        return ym

    config = conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone()
    month = ym.split("-")[1]
    boss = 1 if month in WINDFALL_MONTHS else 0

    # Calculate effective discretionary (subtract side quest carves)
    total_carve = conn.execute(
        "SELECT COALESCE(SUM(monthly_carve), 0) FROM side_quests WHERE status = 'active'"
    ).fetchone()[0]
    effective_budget = config["monthly_discretionary"] - total_carve

    conn.execute(
        "INSERT INTO budget_periods (year_month, discretionary_budget, boss_battle_active) VALUES (?, ?, ?)",
        (ym, effective_budget, boss),
    )
    logger.info(f"[FINANCE] Created budget period {ym} (budget: ${effective_budget:.2f}, boss: {bool(boss)})")
    return ym


def _get_level_for_xp(total_xp):
    """Simple level: level N requires N * 200 XP."""
    level = max(1, total_xp // 200)
    return min(level, 50)  # cap at 50


# level_thresholds is seeded once at init and never mutated at runtime —
# cache lookups so callers (often already inside a get_db() context) don't
# open a second connection per call.
_level_cache: dict = {}


def _get_level_info(level):
    """Get level info from thresholds table."""
    cached = _level_cache.get(level)
    if cached is not None:
        return dict(cached)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM level_thresholds WHERE level = ?", (level,)).fetchone()
        if row:
            info = dict(row)
            _level_cache[level] = info
            return dict(info)
        # Above max defined level
        return {"level": level, "retirement_min": 0, "title": f"Legend {level}"}


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

# ---- Config ----


@router.get("/config")
async def get_config():
    with get_db() as conn:
        row = conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone()
        return dict(row)


@router.put("/config")
async def update_config(req: Request):
    body = await req.json()
    allowed = [
        "monthly_discretionary",
        "monthly_investing",
        "monthly_buffer",
        "retirement_current",
        "retirement_target_age",
        "current_age",
        "savings_rate",
        "expected_return",
    ]
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return JSONResponse({"error": "No valid fields to update"}, status_code=400)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [datetime.now().isoformat()]

    with get_db() as conn:
        conn.execute(
            f"UPDATE finance_config SET {set_clause}, updated_at = ? WHERE id = 1",
            values,
        )
    return {"success": True, "updated": list(updates.keys())}


# ---- Game State ----


@router.get("/game-state")
async def get_game_state():
    with get_db() as conn:
        state = dict(conn.execute("SELECT * FROM game_state WHERE id = 1").fetchone())
        level_info = _get_level_info(state["level"])
        xp_for_next = (state["level"] + 1) * 200
        xp_for_current = state["level"] * 200

        return {
            **state,
            "level_title": level_info["title"],
            "xp_for_next_level": xp_for_next,
            "xp_in_level": state["total_xp"] - xp_for_current,
            "xp_needed": xp_for_next - xp_for_current,
        }


@router.post("/award-xp")
async def award_xp(req: Request):
    body = await req.json()
    event_type = body.get("event_type", "")
    description = body.get("description", "")

    xp_amount = XP_AWARDS.get(event_type)
    if xp_amount is None:
        return JSONResponse(
            {"error": f"Unknown event type: {event_type}", "valid_types": list(XP_AWARDS.keys())},
            status_code=400,
        )

    with get_db() as conn:
        # Log the XP event
        conn.execute(
            "INSERT INTO xp_events (event_type, xp_amount, description) VALUES (?, ?, ?)",
            (event_type, xp_amount, description),
        )

        # Update game state
        state = conn.execute("SELECT * FROM game_state WHERE id = 1").fetchone()
        new_xp = state["total_xp"] + xp_amount
        new_level = _get_level_for_xp(new_xp)
        leveled_up = new_level > state["level"]

        conn.execute(
            "UPDATE game_state SET total_xp = ?, level = ?, updated_at = ? WHERE id = 1",
            (new_xp, new_level, datetime.now().isoformat()),
        )

        level_info = _get_level_info(new_level)

    result = {
        "success": True,
        "xp_awarded": xp_amount,
        "total_xp": new_xp,
        "level": new_level,
        "level_title": level_info["title"],
        "leveled_up": leveled_up,
    }

    if leveled_up:
        logger.info(f"[FINANCE] Level up! {state['level']} → {new_level} ({level_info['title']})")

    return result


# ---- Budget ----


@router.get("/budget/current")
async def get_current_budget():
    with get_db() as conn:
        ym = _ensure_budget_period(conn)
        period = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())

        config = dict(conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone())

        # Side quest carves
        total_carve = conn.execute(
            "SELECT COALESCE(SUM(monthly_carve), 0) FROM side_quests WHERE status = 'active'"
        ).fetchone()[0]

        remaining = period["discretionary_budget"] - period["discretionary_spent"]
        overspend = max(0, period["discretionary_spent"] - period["discretionary_budget"])

        # Future self damage
        years = config["retirement_target_age"] - config["current_age"]
        future_damage = overspend * ((1 + config["expected_return"]) ** years) if overspend > 0 else 0

        return {
            **period,
            "remaining": remaining,
            "overspend": overspend,
            "future_damage": round(future_damage, 2),
            "side_quest_carve": total_carve,
            "effective_budget": period["discretionary_budget"],
            "boss_battle_active": bool(period["boss_battle_active"]),
            "boss_defeated": bool(period["boss_defeated"]),
        }


@router.get("/budget/{year_month}")
async def get_budget_period(year_month: str):
    with get_db() as conn:
        period = conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (year_month,)).fetchone()
        if not period:
            return JSONResponse({"error": f"No budget period for {year_month}"}, status_code=404)
        return dict(period)


@router.post("/budget/manual-entry")
async def add_manual_entry(req: Request):
    body = await req.json()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Amount must be a number"}, status_code=400)
    name = str(body.get("name", "Expense"))[:200]
    category = body.get("category")
    is_discretionary = body.get("is_discretionary", True)

    if amount <= 0 or amount > 100_000:
        return JSONResponse({"error": "Amount must be between 0 and 100,000"}, status_code=400)
    if not __import__("math").isfinite(amount):
        return JSONResponse({"error": "Amount must be a finite number"}, status_code=400)

    with get_db() as conn:
        ym = _ensure_budget_period(conn)
        date = datetime.now().strftime("%Y-%m-%d")

        conn.execute(
            """INSERT INTO transactions (date, amount, name, category, is_discretionary, budget_period, source)
               VALUES (?, ?, ?, ?, ?, ?, 'manual')""",
            (date, amount, name, category, 1 if is_discretionary else 0, ym),
        )

        if is_discretionary:
            conn.execute(
                "UPDATE budget_periods SET discretionary_spent = discretionary_spent + ? WHERE year_month = ?",
                (amount, ym),
            )

        # Get updated budget
        period = dict(conn.execute("SELECT * FROM budget_periods WHERE year_month = ?", (ym,)).fetchone())

    return {
        "success": True,
        "transaction": {"name": name, "amount": amount, "date": date},
        "budget": {
            "spent": period["discretionary_spent"],
            "budget": period["discretionary_budget"],
            "remaining": period["discretionary_budget"] - period["discretionary_spent"],
        },
    }


# ---- Transactions ----


@router.get("/transactions")
async def get_transactions(month: str = None, limit: int = 50):
    ym = month or _current_year_month()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions WHERE budget_period = ? ORDER BY date DESC, id DESC LIMIT ?",
            (ym, limit),
        ).fetchall()
        return {"month": ym, "transactions": [dict(r) for r in rows]}


@router.post("/transactions/reclassify")
async def reclassify_transaction(body: ReclassifyTransactionRequest):
    """Flip one transaction's discretionary flag from the dashboard dot.

    For synced rows the choice is stored as a per-transaction override so the
    next budget sync (and any category-mapping change) keeps it. Flipping a
    row back to what its category mapping says clears the override, so the
    dot is its own undo. Manual rows are never re-derived and need none.
    """
    new_disc = 1 if body.is_discretionary else 0

    def _apply() -> bool:
        with get_db() as conn:
            txn = conn.execute(
                "SELECT category, source, budget_period FROM transactions WHERE id = ?", (body.id,)
            ).fetchone()
            if not txn:
                return False
            override: int | None = None
            if txn["source"] != "manual":
                mapped = conn.execute(
                    "SELECT is_discretionary FROM category_mapping WHERE category_name = ?", (txn["category"],)
                ).fetchone()
                if new_disc != (1 if mapped and mapped["is_discretionary"] else 0):
                    override = new_disc
            period = txn["budget_period"]
            conn.execute(
                "UPDATE transactions SET is_discretionary = ?, discretionary_override = ? WHERE id = ?",
                (new_disc, override, body.id),
            )
            _recalculate_periods(conn, [period])
            # With a synced Fun Money balance, the current month's health bar
            # is budget = balance + spent (see apply_snapshot), so remaining
            # mirrors Actual. Re-derive budget the same way so remaining
            # doesn't jump now and snap back on the next sync. Absolute, not a
            # before/after delta: concurrent clicks must not compound.
            balance = _synced_fun_money_balance(conn)
            if period == _current_year_month() and balance is not None:
                conn.execute(
                    "UPDATE budget_periods SET discretionary_budget = ? + discretionary_spent WHERE year_month = ?",
                    (balance, period),
                )
            return True

    if not await asyncio.to_thread(_apply):
        return JSONResponse({"ok": False, "error": "Transaction not found"}, status_code=404)
    logger.info("[FINANCE] Transaction %d reclassified (discretionary=%s)", body.id, bool(new_disc))
    return {"ok": True, "success": True, "id": body.id, "is_discretionary": bool(new_disc)}


# ---- Side Quests ----


@router.get("/side-quests")
async def get_side_quests():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM side_quests ORDER BY status ASC, created_at DESC").fetchall()
        return {"quests": [dict(r) for r in rows]}


@router.post("/side-quests")
async def create_side_quest(req: Request):
    body = await req.json()
    name = body.get("name", "").strip()
    target = body.get("target_amount", 0)
    monthly_carve = body.get("monthly_carve", 0)
    description = body.get("description")
    icon = body.get("icon", "trophy")

    if not name or target <= 0:
        return JSONResponse({"error": "name and positive target_amount required"}, status_code=400)

    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO side_quests (name, description, target_amount, monthly_carve, icon)
               VALUES (?, ?, ?, ?, ?)""",
            (name, description, target, monthly_carve, icon),
        )
        quest_id = cursor.lastrowid

    logger.info(f"[FINANCE] Side quest created: {name} (${target}, ${monthly_carve}/mo)")
    return {"success": True, "id": quest_id, "name": name}


@router.put("/side-quests/{quest_id}")
async def update_side_quest(quest_id: int, req: Request):
    body = await req.json()

    with get_db() as conn:
        quest = conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone()
        if not quest:
            return JSONResponse({"error": "Quest not found"}, status_code=404)

        # Handle contribution
        contribute = body.get("contribute", 0)
        if contribute > 0:
            new_saved = quest["saved_amount"] + contribute
            completed = new_saved >= quest["target_amount"]

            conn.execute(
                "UPDATE side_quests SET saved_amount = ?, status = ?, completed_at = ? WHERE id = ?",
                (
                    new_saved,
                    "completed" if completed else "active",
                    datetime.now().isoformat() if completed else None,
                    quest_id,
                ),
            )

            if completed:
                logger.info(f"[FINANCE] Side quest completed: {quest['name']}")

            return {
                "success": True,
                "saved_amount": new_saved,
                "completed": completed,
                "quest_name": quest["name"],
            }

        # Handle other updates
        allowed = ["name", "description", "monthly_carve", "icon"]
        updates = {k: v for k, v in body.items() if k in allowed}
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE side_quests SET {set_clause} WHERE id = ?",
                list(updates.values()) + [quest_id],
            )

        return {"success": True, "updated": list(updates.keys())}


@router.delete("/side-quests/{quest_id}")
async def abandon_side_quest(quest_id: int):
    with get_db() as conn:
        quest = conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone()
        if not quest:
            return JSONResponse({"error": "Quest not found"}, status_code=404)

        conn.execute("UPDATE side_quests SET status = 'abandoned' WHERE id = ?", (quest_id,))

    logger.info(f"[FINANCE] Side quest abandoned: {quest['name']}")
    return {"success": True, "id": quest_id, "name": quest["name"]}


@router.post("/side-quests/{quest_id}/contribute")
async def contribute_to_quest(quest_id: int, req: Request):
    """Contribute an amount toward a side quest."""
    body = await req.json()
    amount = body.get("amount", 0)
    if amount <= 0:
        return JSONResponse({"error": "Positive amount required"}, status_code=400)

    with get_db() as conn:
        quest = conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone()
        if not quest:
            return JSONResponse({"error": "Quest not found"}, status_code=404)
        if quest["status"] != "active":
            return JSONResponse({"error": "Quest is not active"}, status_code=400)

        new_saved = quest["saved_amount"] + amount
        completed = new_saved >= quest["target_amount"]

        conn.execute(
            "UPDATE side_quests SET saved_amount = ?, status = ?, completed_at = ? WHERE id = ?",
            (
                new_saved,
                "completed" if completed else "active",
                datetime.now().isoformat() if completed else None,
                quest_id,
            ),
        )

        # Also deduct from health bar (counts as spending allocation)
        ym = _ensure_budget_period(conn)
        conn.execute(
            "UPDATE budget_periods SET discretionary_spent = discretionary_spent + ? WHERE year_month = ?",
            (amount, ym),
        )

        # Log as a transaction
        conn.execute(
            """INSERT INTO transactions (date, amount, name, category, is_discretionary, budget_period, source)
               VALUES (?, ?, ?, 'side_quest', 1, ?, 'manual')""",
            (datetime.now().strftime("%Y-%m-%d"), amount, f"Side Quest: {quest['name']}", ym),
        )

        updated = dict(conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone())

    if completed:
        logger.info(f"[FINANCE] Side quest completed via contribution: {quest['name']}")

    return {**updated, "completed": completed}


@router.post("/side-quests/{quest_id}/complete")
async def complete_quest(quest_id: int):
    """Force-complete a side quest (e.g., purchase made)."""
    with get_db() as conn:
        quest = conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone()
        if not quest:
            return JSONResponse({"error": "Quest not found"}, status_code=404)

        conn.execute(
            "UPDATE side_quests SET status = 'completed', completed_at = ? WHERE id = ?",
            (datetime.now().isoformat(), quest_id),
        )
        updated = dict(conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone())

    logger.info(f"[FINANCE] Side quest force-completed: {quest['name']}")
    return updated


@router.post("/side-quests/{quest_id}/abandon")
async def abandon_quest_post(quest_id: int):
    """Abandon a side quest via POST (returns saved amount to general budget)."""
    with get_db() as conn:
        quest = conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone()
        if not quest:
            return JSONResponse({"error": "Quest not found"}, status_code=404)

        conn.execute("UPDATE side_quests SET status = 'abandoned' WHERE id = ?", (quest_id,))
        updated = dict(conn.execute("SELECT * FROM side_quests WHERE id = ?", (quest_id,)).fetchone())

    logger.info(f"[FINANCE] Side quest abandoned: {quest['name']}")
    return updated


# ---- Future Self Damage ----


@router.get("/future-damage")
async def calculate_future_damage(amount: float = 0):
    if amount <= 0:
        return {"amount": 0, "damage": 0, "years": 0}

    with get_db() as conn:
        config = conn.execute("SELECT * FROM finance_config WHERE id = 1").fetchone()
        years = config["retirement_target_age"] - config["current_age"]
        damage = amount * ((1 + config["expected_return"]) ** years)

    return {
        "amount": amount,
        "damage": round(damage, 2),
        "years": years,
        "rate": config["expected_return"],
    }


# ---- Windfalls / Boss Battles ----


@router.get("/windfalls")
async def get_windfalls(year: int = None):
    y = year or datetime.now().year
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM windfalls WHERE budget_period LIKE ? ORDER BY created_at DESC",
            (f"{y}-%",),
        ).fetchall()
        return {"year": y, "windfalls": [dict(r) for r in rows]}


@router.post("/windfalls")
async def log_windfall(req: Request):
    body = await req.json()
    wf_type = body.get("type")  # 'bonus' or 'espp'
    amount = body.get("amount", 0)
    invest_pct = body.get("invest_percent", 67 if wf_type == "espp" else 0)

    if wf_type not in ("bonus", "espp") or amount <= 0:
        return JSONResponse({"error": "type (bonus/espp) and positive amount required"}, status_code=400)

    invest_amount = amount * (invest_pct / 100)
    spend_amount = amount - invest_amount

    with get_db() as conn:
        ym = _ensure_budget_period(conn)
        conn.execute(
            """INSERT INTO windfalls (type, amount, invest_amount, spend_amount, budget_period, boss_defeated)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (wf_type, amount, invest_amount, spend_amount, ym),
        )
        conn.execute("UPDATE budget_periods SET boss_defeated = 1 WHERE year_month = ?", (ym,))

    logger.info(
        f"[FINANCE] Windfall logged: {wf_type} ${amount} (invest: ${invest_amount:.0f}, spend: ${spend_amount:.0f})"
    )
    return {
        "success": True,
        "type": wf_type,
        "amount": amount,
        "invest_amount": round(invest_amount, 2),
        "spend_amount": round(spend_amount, 2),
        "boss_defeated": True,
    }


# ---- XP History ----


@router.get("/xp-history")
async def get_xp_history(limit: int = 20):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM xp_events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return {"events": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Budget sync (Actual Budget)
# ---------------------------------------------------------------------------
#
# Each sync downloads the budget read-only (actual_client.fetch_snapshot), then
# mirrors the last ACTUAL_SYNC_MONTHS of spendable outflows into
# `transactions`: upsert by external_id, delete synced rows that disappeared
# upstream (deleted, re-dated out of the window, turned into a transfer), and
# recompute discretionary_spent from rows for every touched month. Recomputing
# instead of applying deltas keeps manual entries, reclassifications and
# upstream edits consistent by construction.

# Last successful snapshot's categories, reused by the settings page so
# opening it doesn't re-download the budget. (monotonic_ts, snapshot)
_category_cache: tuple[float, ActualSnapshot] | None = None
_CATEGORY_CACHE_TTL = 15 * 60
# Last failed fetch, so a down server / wrong password isn't re-hit on every
# settings-page load. (monotonic_ts, error)
_category_fail: tuple[float, str] | None = None
_CATEGORY_FAIL_TTL = 2 * 60
# Manual "Sync now" cooldown: within this many seconds of the last attempt the
# route returns the stored result instead of downloading the budget again.
_MANUAL_SYNC_COOLDOWN = 60

# One sync at a time (scheduler + manual button + settings page). Callers that
# find it held get a "busy" result instead of queueing behind it.
_sync_lock = asyncio.Lock()


def _is_sync_configured() -> bool:
    """True when the Actual Budget sync has enough config to run."""
    from orchestrator.actual_client import is_configured

    return is_configured()


def _month_of(date_iso: str) -> str:
    return date_iso[:7] if len(date_iso) >= 7 else _current_year_month()


def _recalculate_periods(conn: sqlite3.Connection, periods: Iterable[str | None]) -> None:
    """Set discretionary_spent = sum of discretionary rows for each period."""
    for ym in sorted({p for p in periods if p}):
        _ensure_budget_period(conn, ym)
        total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE budget_period = ? AND is_discretionary = 1",
            (ym,),
        ).fetchone()[0]
        conn.execute("UPDATE budget_periods SET discretionary_spent = ? WHERE year_month = ?", (total, ym))


def _synced_fun_money_balance(conn: sqlite3.Connection) -> float | None:
    """Fun Money balance from the last successful sync, or None if it set none."""
    row = conn.execute("SELECT last_result FROM budget_sync_state WHERE id = 1").fetchone()
    if not row or not row["last_result"]:
        return None
    try:
        balance = json.loads(row["last_result"]).get("fun_money_balance")
        return float(balance) if balance is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _record_sync_state(
    conn: sqlite3.Connection, *, ok: bool, budget_name: str | None, error: str | None, result: dict | None
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO budget_sync_state (id, provider, budget_name, last_synced_at, last_attempt_at, last_error, last_result)
           VALUES (1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             provider = excluded.provider,
             budget_name = COALESCE(excluded.budget_name, budget_sync_state.budget_name),
             last_synced_at = COALESCE(excluded.last_synced_at, budget_sync_state.last_synced_at),
             last_attempt_at = excluded.last_attempt_at,
             last_error = excluded.last_error,
             last_result = COALESCE(excluded.last_result, budget_sync_state.last_result)""",
        (
            SYNC_PROVIDER,
            budget_name,
            now if ok else None,
            now,
            None if ok else (error or "unknown error")[:500],
            json.dumps(result) if result is not None else None,
        ),
    )


def _record_failure(error: str) -> None:
    """Best-effort: persist a failed attempt so status stops saying 'connected'."""
    try:
        with get_db() as conn:
            _record_sync_state(conn, ok=False, budget_name=None, error=error, result=None)
    except Exception as e:  # noqa: BLE001 — never mask the original failure
        logger.warning("[ACTUAL] Could not record sync failure: %s", e)


def apply_snapshot(snapshot: ActualSnapshot) -> dict:
    """Mirror a snapshot into finance.db. Synchronous; pure DB work.

    Split out from the network fetch so it can be unit-tested with a
    hand-built snapshot. Returns counts for logging/metrics.
    """
    prefix = f"{SYNC_PROVIDER}:"
    window_start = snapshot.window_start
    outflows = [t for t in snapshot.transactions if t.amount < 0]
    skipped_inflows = len(snapshot.transactions) - len(outflows)

    with get_db() as conn:
        mapping = {
            r["category_name"]: bool(r["is_discretionary"])
            for r in conn.execute("SELECT category_name, is_discretionary FROM category_mapping").fetchall()
        }
        existing = {
            r["external_id"]: r["budget_period"]
            for r in conn.execute(
                "SELECT external_id, budget_period FROM transactions WHERE source = ? AND date >= ?",
                (SYNC_PROVIDER, window_start),
            ).fetchall()
        }
        touched = set(existing.values())

        # Rows re-dated upstream from before the window into it are not in
        # `existing`, but their old month's total must be recomputed too.
        incoming = [prefix + t.id for t in outflows]
        outside = [ext for ext in incoming if ext not in existing]
        for k in range(0, len(outside), 500):
            chunk = outside[k : k + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT budget_period FROM transactions WHERE external_id IN ({marks})",  # noqa: S608 — placeholders only
                chunk,
            ).fetchall():
                touched.add(r["budget_period"])

        # Actual is now the source of truth for every month in the window:
        # legacy YNAB-synced rows there would double-count the same purchases.
        legacy = conn.execute(
            "SELECT DISTINCT budget_period FROM transactions WHERE source = 'ynab' AND date >= ?", (window_start,)
        ).fetchall()
        touched.update(r["budget_period"] for r in legacy)
        legacy_removed = conn.execute(
            "DELETE FROM transactions WHERE source = 'ynab' AND date >= ?", (window_start,)
        ).rowcount

        seen = set()
        inserted = updated = 0
        for t in outflows:
            ext = prefix + t.id
            seen.add(ext)
            period = _month_of(t.date)
            touched.add(period)
            name = (t.payee or t.notes or t.category or "Actual transaction")[:200]
            values = (
                t.date,
                float(-t.amount),
                name,
                t.payee or None,
                t.category or None,
                t.category_group or None,
                1 if mapping.get(t.category, False) else 0,
                period,
            )
            if ext in existing:
                conn.execute(
                    """UPDATE transactions SET date=?, amount=?, name=?, merchant_name=?, category=?,
                       subcategory=?, is_discretionary=COALESCE(discretionary_override, ?), budget_period=?
                       WHERE external_id=?""",
                    (*values, ext),
                )
                updated += 1
            else:
                cur = conn.execute(
                    """INSERT INTO transactions
                       (date, amount, name, merchant_name, category, subcategory, is_discretionary,
                        budget_period, external_id, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(external_id) DO UPDATE SET
                         date=excluded.date, amount=excluded.amount, name=excluded.name,
                         merchant_name=excluded.merchant_name, category=excluded.category,
                         subcategory=excluded.subcategory,
                         is_discretionary=COALESCE(transactions.discretionary_override, excluded.is_discretionary),
                         budget_period=excluded.budget_period
                       RETURNING (created_at < datetime('now', '-1 second')) AS pre_existing""",
                    (*values, ext, SYNC_PROVIDER),
                )
                row = cur.fetchone()
                if ext in outside and row is not None and row["pre_existing"]:
                    updated += 1
                else:
                    inserted += 1

        stale = [ext for ext in existing if ext not in seen]
        for ext in stale:
            conn.execute("DELETE FROM transactions WHERE external_id = ?", (ext,))

        # Every month in the window gets a period row + recomputed total, plus
        # any month a row moved out of.
        y, m = int(window_start[:4]), int(window_start[5:7])
        current = _current_year_month()
        while f"{y:04d}-{m:02d}" <= current:
            touched.add(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                m, y = 1, y + 1
        _recalculate_periods(conn, touched)

        # Health bar: discretionary_budget = fun-money balance + spent, so
        # budget - spent == the balance Actual shows for that category.
        if snapshot.fun_money_balance is not None:
            spent = conn.execute(
                "SELECT discretionary_spent FROM budget_periods WHERE year_month = ?", (current,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE budget_periods SET discretionary_budget = ? WHERE year_month = ?",
                (float(snapshot.fun_money_balance) + float(spent), current),
            )

        result = {
            "synced": inserted + updated,
            "inserted": inserted,
            "updated": updated,
            "deleted": len(stale),
            "legacy_removed": legacy_removed,
            "skipped_inflows": skipped_inflows,
            "window_start": window_start,
            "fun_money_category": snapshot.fun_money_category,
            "fun_money_balance": float(snapshot.fun_money_balance) if snapshot.fun_money_balance is not None else None,
        }
        _record_sync_state(conn, ok=True, budget_name=snapshot.budget_name, error=None, result=result)
    return result


async def sync_budget_transactions() -> dict:
    """Pull spending from Actual Budget into finance.db. Never raises.

    Called by the APScheduler job, the dashboard Sync button and the settings
    page. Returns ``{"busy": True, ...}`` instead of queueing when another
    sync holds the lock or a timed-out download is still running.
    """
    from orchestrator import actual_client
    from orchestrator.config import settings
    from orchestrator.metrics import BUDGET_SYNC_LAST_SUCCESS, BUDGET_SYNC_TOTAL

    global _category_cache, _category_fail

    if not _is_sync_configured():
        return {"synced": 0, "error": "Actual Budget not configured"}
    if _sync_lock.locked():
        BUDGET_SYNC_TOTAL.labels(result="busy").inc()
        return {"synced": 0, "busy": True, "error": "a sync is already running"}

    async with _sync_lock:
        t0 = time.monotonic()
        try:
            snapshot = await actual_client.fetch_snapshot_async()
        except actual_client.ActualSyncBusy as e:
            BUDGET_SYNC_TOTAL.labels(result="busy").inc()
            logger.warning("[ACTUAL] %s — skipping this sync", e)
            return {"synced": 0, "busy": True, "error": str(e)}
        except Exception as e:  # noqa: BLE001 — record, never raise into the scheduler
            msg = actual_client.safe_error(e)
            logger.error("[ACTUAL] Sync failed fetching budget: %s", msg)
            BUDGET_SYNC_TOTAL.labels(result="error").inc()
            _category_fail = (time.monotonic(), msg)
            await asyncio.to_thread(_record_failure, msg)
            return {"synced": 0, "error": msg}

        try:
            result = await asyncio.to_thread(apply_snapshot, snapshot)
        except Exception as e:  # noqa: BLE001
            msg = f"local database error: {type(e).__name__}"
            logger.error("[ACTUAL] Sync failed writing finance.db: %s", e, exc_info=True)
            BUDGET_SYNC_TOTAL.labels(result="error").inc()
            await asyncio.to_thread(_record_failure, msg)
            return {"synced": 0, "error": msg}

        _category_cache = (time.monotonic(), snapshot)
        _category_fail = None
        BUDGET_SYNC_TOTAL.labels(result="ok").inc()
        BUDGET_SYNC_LAST_SUCCESS.set(time.time())
        logger.info(
            "[ACTUAL] Synced '%s' in %.1fs: +%d new, %d updated, %d removed, %d inflows skipped%s%s",
            snapshot.budget_name,
            time.monotonic() - t0,
            result["inserted"],
            result["updated"],
            result["deleted"],
            result["skipped_inflows"],
            f", {result['legacy_removed']} legacy YNAB rows replaced" if result["legacy_removed"] else "",
            ""
            if snapshot.fun_money_balance is not None
            else f" — fun-money category '{settings.actual_fun_money_category}' not found",
        )
        return result


# ---- Budget sync API routes ----
#
# All return {"ok": bool, ...}. Failures use real status codes: 400 not
# configured, 409 busy, 422 bad body, 502 upstream (Actual) failure.


def _status_sync() -> dict:
    with get_db() as conn:
        state = conn.execute("SELECT * FROM budget_sync_state WHERE id = 1").fetchone()
        cat_count = conn.execute("SELECT COUNT(*) FROM category_mapping").fetchone()[0]
        disc_count = conn.execute("SELECT COUNT(*) FROM category_mapping WHERE is_discretionary = 1").fetchone()[0]
    state = dict(state) if state else {}
    last_result = None
    if state.get("last_result"):
        try:
            last_result = json.loads(state["last_result"])
        except ValueError:
            last_result = None
    return {
        "ok": True,
        "provider": SYNC_PROVIDER,
        "configured": _is_sync_configured(),
        # "connected" = the most recent attempt succeeded.
        "connected": bool(state.get("last_synced_at")) and not state.get("last_error"),
        "budget_name": state.get("budget_name"),
        "last_synced_at": state.get("last_synced_at"),
        "last_attempt_at": state.get("last_attempt_at"),
        "last_error": state.get("last_error"),
        "last_result": last_result,
        "category_count": cat_count,
        "discretionary_count": disc_count,
    }


def _not_configured() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "Actual Budget not configured. Set ACTUAL_SERVER_URL, ACTUAL_PASSWORD and ACTUAL_BUDGET_FILE.",
        },
        status_code=400,
    )


def _sync_response(result: dict) -> dict | JSONResponse:
    if result.get("busy"):
        return JSONResponse({"ok": False, "busy": True, "error": result["error"]}, status_code=409)
    if result.get("error"):
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=502)
    return {"ok": True, **result}


@router.get("/sync/status")
async def sync_status() -> dict:
    """Budget sync configuration + last sync outcome (no network call)."""
    return await asyncio.to_thread(_status_sync)


@router.post("/sync", response_model=None)
async def trigger_sync() -> dict | JSONResponse:
    """Manually trigger a budget sync (rate-limited by a short cooldown)."""
    if not _is_sync_configured():
        return _not_configured()
    status = await asyncio.to_thread(_status_sync)
    last = status.get("last_attempt_at")
    if last:
        try:
            age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            age = _MANUAL_SYNC_COOLDOWN
        if 0 <= age < _MANUAL_SYNC_COOLDOWN:
            if status.get("last_error"):
                return JSONResponse({"ok": False, "error": status["last_error"], "cooldown": True}, status_code=502)
            return {"ok": True, "cooldown": True, **(status.get("last_result") or {"synced": 0})}
    return _sync_response(await sync_budget_transactions())


@router.get("/categories", response_model=None)
async def get_budget_categories() -> dict | JSONResponse:
    """Budget categories grouped as in Actual, with discretionary mapping and
    this month's budgeted / spent / balance. Uses the last sync's snapshot when
    fresh; otherwise syncs first. Recent failures are served from a short
    negative cache instead of re-contacting the server."""
    if not _is_sync_configured():
        return _not_configured()

    cached = _category_cache
    if cached is None or time.monotonic() - cached[0] > _CATEGORY_CACHE_TTL:
        failed = _category_fail
        if failed is not None and time.monotonic() - failed[0] < _CATEGORY_FAIL_TTL and cached is None:
            return JSONResponse({"ok": False, "error": failed[1]}, status_code=502)
        result = await sync_budget_transactions()
        if result.get("error") and _category_cache is None:
            return _sync_response(result)
        cached = _category_cache
    snapshot = cached[1]

    def _mapping() -> dict[str, bool]:
        with get_db() as conn:
            return {
                r["category_name"]: bool(r["is_discretionary"])
                for r in conn.execute("SELECT category_name, is_discretionary FROM category_mapping").fetchall()
            }

    existing_map = await asyncio.to_thread(_mapping)
    groups: dict[str, list] = {}
    for c in snapshot.categories:
        if c.is_income or c.hidden:
            continue
        groups.setdefault(c.group or "Uncategorized", []).append(
            {
                "name": c.name,
                "is_discretionary": existing_map.get(c.name, False),
                "budgeted": float(c.budgeted),
                "activity": float(c.spent),
                "balance": float(c.balance),
            }
        )
    return {
        "ok": True,
        "budget_name": snapshot.budget_name,
        "groups": [{"group_name": g, "categories": cats} for g, cats in groups.items()],
    }


@router.post("/categories/mapping")
async def update_category_mapping(body: CategoryMappingRequest) -> dict:
    """Update which budget categories count as discretionary.

    Body: { "mappings": { "Dining Out": true, "Rent": false, ... } }
    """
    mappings = body.mappings

    def _apply() -> None:
        with get_db() as conn:
            for cat_name, is_disc in mappings.items():
                conn.execute(
                    """INSERT INTO category_mapping (category_name, is_discretionary) VALUES (?, ?)
                       ON CONFLICT(category_name) DO UPDATE SET is_discretionary = excluded.is_discretionary""",
                    (cat_name, 1 if is_disc else 0),
                )
            # Re-flag every synced row from the new mapping (per-transaction
            # dashboard overrides win), then recompute.
            current = {
                r["category_name"]: bool(r["is_discretionary"])
                for r in conn.execute("SELECT category_name, is_discretionary FROM category_mapping").fetchall()
            }
            rows = conn.execute(
                "SELECT id, category, budget_period FROM transactions WHERE source != 'manual'"
            ).fetchall()
            periods = set()
            for r in rows:
                conn.execute(
                    "UPDATE transactions SET is_discretionary = COALESCE(discretionary_override, ?) WHERE id = ?",
                    (1 if current.get(r["category"], False) else 0, r["id"]),
                )
                periods.add(r["budget_period"])
            _recalculate_periods(conn, periods)

    await asyncio.to_thread(_apply)
    logger.info("[FINANCE] Updated %d category mappings", len(mappings))
    return {"ok": True, "success": True, "updated": len(mappings)}


@router.post("/sync/reset")
async def reset_sync() -> dict:
    """Delete all synced rows and sync state; the next sync re-imports.

    Per-transaction discretionary overrides (dashboard dot) go with the rows.
    """

    def _reset() -> None:
        with get_db() as conn:
            periods = {
                r["budget_period"]
                for r in conn.execute(
                    "SELECT DISTINCT budget_period FROM transactions WHERE source = ?", (SYNC_PROVIDER,)
                ).fetchall()
            }
            conn.execute("DELETE FROM transactions WHERE source = ?", (SYNC_PROVIDER,))
            conn.execute("DELETE FROM budget_sync_state WHERE id = 1")
            _recalculate_periods(conn, periods)

    await asyncio.to_thread(_reset)
    global _category_cache, _category_fail
    _category_cache = None
    _category_fail = None
    logger.info("[FINANCE] Budget sync reset — next sync re-imports")
    return {"ok": True, "success": True, "message": "Sync state reset. Trigger sync to re-import."}


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def setup_finance():
    """Initialize the finance DB. Called unconditionally at orchestrator startup.

    The dashboard finance pages, manual entries and the finance_status tool
    all need the tables even when no budget sync is configured; before
    2026-10 this was never called and every /api/finance/* request 500'd.
    """
    try:
        init_db()
        from orchestrator.actual_client import cleanup_stale_tempdirs
        from orchestrator.config import settings

        cleanup_stale_tempdirs()

        if _is_sync_configured():
            logger.info(f"[FINANCE] Actual Budget sync configured — every {settings.actual_sync_interval}m")
        else:
            logger.info("[FINANCE] Budget sync not configured (manual mode; set ACTUAL_SERVER_URL to enable)")
    except Exception as e:
        logger.error(f"[FINANCE] Failed to initialize: {e}", exc_info=True)
