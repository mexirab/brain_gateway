"""
Financial Quest Board — SQLite persistence, game logic, and API routes.

Gamified finance tracking for ADHD support:
- Health bar (discretionary budget tracking)
- XP / leveling system
- Streak tracking
- Side quests (savings goals)
- Future Self Damage calculator
- Boss battles (windfall months)
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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
    ynab_transaction_id TEXT UNIQUE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,
    name TEXT NOT NULL,
    merchant_name TEXT,
    category TEXT,
    subcategory TEXT,
    is_discretionary INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS ynab_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    budget_id TEXT,
    last_knowledge_of_server INTEGER,
    last_synced_at TEXT
);
"""

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


@contextmanager
def get_db():
    """Get a SQLite connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize database schema and seed data."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_year_month():
    return datetime.now().strftime("%Y-%m")


def _ensure_budget_period(conn, year_month=None):
    """Create budget period for the given month if it doesn't exist."""
    ym = year_month or _current_year_month()
    existing = conn.execute(
        "SELECT id FROM budget_periods WHERE year_month = ?", (ym,)
    ).fetchone()
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


def _get_level_info(level):
    """Get level info from thresholds table."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM level_thresholds WHERE level = ?", (level,)
        ).fetchone()
        if row:
            return dict(row)
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
        "monthly_discretionary", "monthly_investing", "monthly_buffer",
        "retirement_current", "retirement_target_age", "current_age",
        "savings_rate", "expected_return",
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
        period = dict(conn.execute(
            "SELECT * FROM budget_periods WHERE year_month = ?", (ym,)
        ).fetchone())

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
        period = conn.execute(
            "SELECT * FROM budget_periods WHERE year_month = ?", (year_month,)
        ).fetchone()
        if not period:
            return JSONResponse({"error": f"No budget period for {year_month}"}, status_code=404)
        return dict(period)


@router.post("/budget/manual-entry")
async def add_manual_entry(req: Request):
    body = await req.json()
    amount = body.get("amount", 0)
    name = body.get("name", "Expense")
    category = body.get("category")
    is_discretionary = body.get("is_discretionary", True)

    if amount <= 0:
        return JSONResponse({"error": "Amount must be positive"}, status_code=400)

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
        period = dict(conn.execute(
            "SELECT * FROM budget_periods WHERE year_month = ?", (ym,)
        ).fetchone())

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
async def reclassify_transaction(req: Request):
    body = await req.json()
    txn_id = body.get("id")
    is_discretionary = body.get("is_discretionary")

    if txn_id is None or is_discretionary is None:
        return JSONResponse({"error": "id and is_discretionary required"}, status_code=400)

    with get_db() as conn:
        txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if not txn:
            return JSONResponse({"error": "Transaction not found"}, status_code=404)

        old_disc = bool(txn["is_discretionary"])
        new_disc = bool(is_discretionary)

        if old_disc != new_disc:
            conn.execute(
                "UPDATE transactions SET is_discretionary = ? WHERE id = ?",
                (1 if new_disc else 0, txn_id),
            )
            # Update budget period spending
            delta = txn["amount"] if new_disc else -txn["amount"]
            conn.execute(
                "UPDATE budget_periods SET discretionary_spent = discretionary_spent + ? WHERE year_month = ?",
                (delta, txn["budget_period"]),
            )

    return {"success": True, "id": txn_id, "is_discretionary": new_disc}


# ---- Side Quests ----

@router.get("/side-quests")
async def get_side_quests():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM side_quests ORDER BY status ASC, created_at DESC"
        ).fetchall()
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

        conn.execute(
            "UPDATE side_quests SET status = 'abandoned' WHERE id = ?", (quest_id,)
        )

    logger.info(f"[FINANCE] Side quest abandoned: {quest['name']}")
    return {"success": True, "id": quest_id, "name": quest["name"]}


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
        conn.execute(
            "UPDATE budget_periods SET boss_defeated = 1 WHERE year_month = ?", (ym,)
        )

    logger.info(f"[FINANCE] Windfall logged: {wf_type} ${amount} (invest: ${invest_amount:.0f}, spend: ${spend_amount:.0f})")
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
        rows = conn.execute(
            "SELECT * FROM xp_events ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"events": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def setup_finance():
    """Initialize finance module. Called from orchestrator startup."""
    try:
        init_db()
        logger.info("[FINANCE] Finance Quest Board module initialized")
    except Exception as e:
        logger.error(f"[FINANCE] Failed to initialize: {e}")
