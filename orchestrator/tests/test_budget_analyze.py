"""
Tests for the ``question_type="analyze"`` branch of ``budget_manager.query()``.

Scope:
  * Error guards (empty analysis_question, unknown dataset) short-circuit before
    the expert model is called.
  * Happy path returns both the expert synthesis and the underlying overview
    data, and forwards the user question + JSON data block into ask_expert.
  * Expert failure prefixes ("Expert model ...") surface as ``expert_error`` +
    ``expert_synthesis=None`` + a fallback hint, not as the synthesis.
  * Filter arguments (start_date/end_date/category/payee_contains) round-trip
    into overview.filters and into the underlying SQL aggregation.
  * ``by_month`` is capped at 36 entries even for ~50-month datasets.
  * ``amount_sign`` controls the ranking basis of ``top_categories``/``top_payees``.
  * The ``query_budget`` tool handler forwards ``analysis_question`` through.

External calls to ``handle_ask_expert`` are patched via ``unittest.mock.AsyncMock``
so no real expert-model HTTP calls are made.

Fixtures: ``tmp_db`` from conftest.py provides a fresh SQLite schema per test.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_import_stub(name: str) -> None:
    """Create the parent budget_imports row so FK constraints on transactions pass."""
    from orchestrator import state_store

    state_store.save_budget_import(
        {
            "name": name,
            "source_file": "seed.csv",
            "row_count": 0,
            "date_min": None,
            "date_max": None,
            "total_outflow": 0,
            "total_inflow": 0,
            "column_map": {},
            "summary_doc_id": None,
        }
    )


def _seed_small_dataset(name: str = "test_small") -> None:
    """Two months, two categories, two payees, mix of outflow + inflow."""
    from orchestrator import state_store

    _seed_import_stub(name)
    rows = [
        # Gaming outflows — dominates Q1
        {
            "txn_date": "2025-01-10",
            "amount": -200.00,
            "category": "Gaming",
            "payee": "Steam",
            "description": "AAA title",
        },
        {"txn_date": "2025-01-20", "amount": -150.00, "category": "Gaming", "payee": "Steam", "description": "DLC"},
        {
            "txn_date": "2025-02-05",
            "amount": -100.00,
            "category": "Gaming",
            "payee": "GOG",
            "description": "indie bundle",
        },
        # Groceries outflows
        {
            "txn_date": "2025-01-15",
            "amount": -80.00,
            "category": "Groceries",
            "payee": "Whole Foods",
            "description": "weekly",
        },
        {
            "txn_date": "2025-02-16",
            "amount": -90.00,
            "category": "Groceries",
            "payee": "Whole Foods",
            "description": "weekly",
        },
        # Income inflows
        {
            "txn_date": "2025-01-31",
            "amount": 3000.00,
            "category": "Income",
            "payee": "Employer",
            "description": "salary",
        },
        {
            "txn_date": "2025-02-28",
            "amount": 3000.00,
            "category": "Income",
            "payee": "Employer",
            "description": "salary",
        },
    ]
    state_store.save_budget_transactions(name, rows)


def _seed_filterable_dataset(name: str = "test_filter") -> None:
    """Mixed dates + categories so we can verify pass-through filtering."""
    from orchestrator import state_store

    _seed_import_stub(name)
    rows = [
        # Inside 2025 + Gaming + Steam
        {"txn_date": "2025-03-01", "amount": -250.00, "category": "Gaming", "payee": "Steam", "description": "game"},
        {"txn_date": "2025-06-15", "amount": -100.00, "category": "Gaming", "payee": "Steam", "description": "game"},
        # Inside 2025 but wrong category
        {
            "txn_date": "2025-07-10",
            "amount": -40.00,
            "category": "Coffee",
            "payee": "Blue Bottle",
            "description": "latte",
        },
        # Inside 2025 Gaming but different payee (payee_contains=Steam should exclude)
        {"txn_date": "2025-04-01", "amount": -75.00, "category": "Gaming", "payee": "Epic", "description": "game"},
        # Outside date window (2024)
        {"txn_date": "2024-12-20", "amount": -999.00, "category": "Gaming", "payee": "Steam", "description": "old"},
        # Outside date window (2026)
        {"txn_date": "2026-02-01", "amount": -500.00, "category": "Gaming", "payee": "Steam", "description": "future"},
    ]
    state_store.save_budget_transactions(name, rows)


def _seed_50_months(name: str = "test_50mo") -> None:
    """One outflow row per month across 50 consecutive months."""
    from orchestrator import state_store

    _seed_import_stub(name)
    rows = []
    year = 2022
    month = 1
    for i in range(50):
        rows.append(
            {
                "txn_date": f"{year:04d}-{month:02d}-15",
                "amount": -float(10 + i),
                "category": "Misc",
                "payee": "Anywhere",
                "description": f"m{i}",
            }
        )
        month += 1
        if month > 12:
            month = 1
            year += 1
    state_store.save_budget_transactions(name, rows)


def _seed_mixed_sign_dataset(name: str = "test_signs") -> None:
    """Distinct outflow and inflow category/payee rankings so we can verify
    that amount_sign='inflow' flips which keys surface at the top."""
    from orchestrator import state_store

    _seed_import_stub(name)
    rows = [
        # Outflow king: Gaming / Steam
        {"txn_date": "2025-01-10", "amount": -500.00, "category": "Gaming", "payee": "Steam", "description": "big"},
        {"txn_date": "2025-02-10", "amount": -400.00, "category": "Gaming", "payee": "Steam", "description": "big"},
        # Inflow king: Refunds / Amazon
        {"txn_date": "2025-01-20", "amount": 800.00, "category": "Refunds", "payee": "Amazon", "description": "refund"},
        {"txn_date": "2025-02-20", "amount": 700.00, "category": "Refunds", "payee": "Amazon", "description": "refund"},
        # Noise small rows
        {"txn_date": "2025-03-01", "amount": -10.00, "category": "Coffee", "payee": "Local", "description": "latte"},
        {
            "txn_date": "2025-03-02",
            "amount": 5.00,
            "category": "Rebates",
            "payee": "RetailMeNot",
            "description": "rebate",
        },
    ]
    state_store.save_budget_transactions(name, rows)


# ---------------------------------------------------------------------------
# 1. Empty / whitespace analysis_question → falls back to a generic prompt
#    (the "required or error" gate was intentionally removed — see the analyze
#    branch in budget_manager.query: it made the primary model loop when it
#    called analyze without the kwarg)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t "])
async def test_analyze_blank_question_falls_back_to_generic_prompt(tmp_db, monkeypatch, blank):
    """A blank analysis_question is NOT rejected — analyze substitutes a
    generic pattern-finding prompt and still runs the expert synthesis."""
    from orchestrator import budget_manager

    _seed_small_dataset("test_small")

    synth = "Gaming dominated Q1 spending."
    expert_mock = AsyncMock(return_value=synth)
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_small",
        question_type="analyze",
        analysis_question=blank,
    )

    # No rejection; a non-empty generic question is synthesized in place of the
    # blank input and the expert is invoked exactly once.
    assert "error" not in result
    assert result["question_type"] == "analyze"
    assert result["user_question"].strip()
    assert result["expert_synthesis"] == synth
    expert_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Unknown dataset → error, expert NOT called
# ---------------------------------------------------------------------------


async def test_analyze_unknown_dataset_short_circuits(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    expert_mock = AsyncMock(return_value="should-not-be-called")
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="does-not-exist",
        question_type="analyze",
        analysis_question="What did I spend on?",
    )

    assert "error" in result
    assert "Unknown dataset" in result["error"]
    expert_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Happy path: expert synthesizes; data block + user question both forwarded
# ---------------------------------------------------------------------------


async def test_analyze_happy_path_returns_synthesis_and_data(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    _seed_small_dataset("test_small")

    synth = "Gaming was the biggest category in Q1 2025 at $450 across 3 transactions."
    expert_mock = AsyncMock(return_value=synth)
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    user_q = "What did I spend most on?"
    result = await budget_manager.query(
        dataset="test_small",
        question_type="analyze",
        analysis_question=user_q,
    )

    # Top-level shape
    assert result["question_type"] == "analyze"
    assert result["dataset"] == "test_small"
    assert result["user_question"] == user_q
    assert result["filters"] == {}  # no filters passed
    assert result["expert_synthesis"] == synth
    assert "expert_error" not in result
    assert "hint" not in result

    # Underlying data
    data = result["data"]
    assert "top_categories" in data
    assert "top_payees" in data
    assert "by_month" in data
    assert "outliers" in data
    assert "total_outflow" in data
    assert "total_inflow" in data
    assert "net" in data

    # Spot check: Gaming should dominate top_categories (outflow rank by default)
    cat_keys = [c["key"] for c in data["top_categories"]]
    assert "Gaming" in cat_keys
    # Two months of data → by_month length 2
    assert len(data["by_month"]) == 2

    # Expert was called exactly once and the prompt carried both the user's
    # verbatim question and the JSON data block.
    expert_mock.assert_awaited_once()
    call_args = expert_mock.await_args
    # handle_ask_expert takes a dict {"question": ...}
    assert call_args is not None
    arg_dict = call_args.args[0] if call_args.args else call_args.kwargs.get("arguments")
    assert isinstance(arg_dict, dict)
    question_text = arg_dict["question"]
    assert user_q in question_text  # verbatim user intent
    assert "```json" in question_text  # data block present
    # Parse the JSON block back out and sanity-check a couple of fields
    json_start = question_text.find("{", question_text.find("```json"))
    json_end = question_text.rfind("}")
    parsed = json.loads(question_text[json_start : json_end + 1])
    assert parsed["dataset"] == "test_small"
    assert "top_categories" in parsed


# ---------------------------------------------------------------------------
# 4. Expert failure prefixes surface as expert_error + fallback hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_msg",
    [
        # From expert_agent.py: _DISABLED_MSG
        "Expert model is disabled. Set EXPERT_ENABLED=true and EXPERT_MODEL_URL in .env to enable.",
        # From _UNREACHABLE_MSG
        "Expert model is temporarily unavailable. Answering directly.",
        # From _CIRCUIT_OPEN_MSG
        "Expert model circuit breaker is open after repeated failures — answering directly for the next couple of minutes.",
    ],
)
async def test_analyze_expert_failure_surfaces_as_error(tmp_db, monkeypatch, failure_msg):
    from orchestrator import budget_manager

    _seed_small_dataset("test_small")

    expert_mock = AsyncMock(return_value=failure_msg)
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_small",
        question_type="analyze",
        analysis_question="summary please",
    )

    assert result["expert_synthesis"] is None
    assert result["expert_error"] == failure_msg
    assert "synthesize the findings yourself" in result["hint"]
    # Data must still be populated so the primary model can fall back.
    assert result["data"]["dataset"] == "test_small"
    assert result["data"]["total_outflow"] != 0  # we seeded outflows


# ---------------------------------------------------------------------------
# 5. Filter pass-through: start_date / end_date / category / payee_contains
# ---------------------------------------------------------------------------


async def test_analyze_filters_pass_through_and_restrict_aggregation(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    _seed_filterable_dataset("test_filter")

    expert_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_filter",
        question_type="analyze",
        analysis_question="Gaming via Steam in 2025",
        start_date="2025-01-01",
        end_date="2025-12-31",
        category="Gaming",
        payee_contains="Steam",
    )

    filters = result["filters"]
    assert filters["start_date"] == "2025-01-01"
    assert filters["end_date"] == "2025-12-31"
    assert filters["category"] == "Gaming"
    assert filters["payee_contains"] == "Steam"

    # Only the two 2025 Gaming/Steam rows (-250, -100) should be aggregated.
    # Epic, Coffee, the 2024 row, and the 2026 row must all be excluded.
    data = result["data"]
    assert data["matched_rows"] == 2
    assert data["total_outflow"] == pytest.approx(-350.00, abs=0.01)


# ---------------------------------------------------------------------------
# 6. by_month cap at 36
# ---------------------------------------------------------------------------


async def test_analyze_by_month_capped_at_36(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    _seed_50_months("test_50mo")

    expert_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_50mo",
        question_type="analyze",
        analysis_question="patterns",
    )

    assert len(result["data"]["by_month"]) <= 36


# ---------------------------------------------------------------------------
# 7. ranking_sign default = "outflow"; explicit "inflow" flips rankings
# ---------------------------------------------------------------------------


async def test_analyze_ranking_sign_defaults_to_outflow(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    _seed_mixed_sign_dataset("test_signs")

    expert_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_signs",
        question_type="analyze",
        analysis_question="where did I spend",
    )
    data = result["data"]
    assert data["ranking_sign"] == "outflow"
    cat_keys = [c["key"] for c in data["top_categories"]]
    payee_keys = [p["key"] for p in data["top_payees"]]
    # Gaming / Steam (outflows) should dominate; Refunds / Amazon (inflows) should not be #1.
    assert cat_keys[0] == "Gaming"
    assert payee_keys[0] == "Steam"


async def test_analyze_ranking_sign_inflow_flips_rankings(tmp_db, monkeypatch):
    from orchestrator import budget_manager

    _seed_mixed_sign_dataset("test_signs")

    expert_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr("orchestrator.expert_agent.handle_ask_expert", expert_mock)

    result = await budget_manager.query(
        dataset="test_signs",
        question_type="analyze",
        analysis_question="where did my income come from",
        amount_sign="inflow",
    )
    data = result["data"]
    assert data["ranking_sign"] == "inflow"
    cat_keys = [c["key"] for c in data["top_categories"]]
    payee_keys = [p["key"] for p in data["top_payees"]]
    # Inflow king: Refunds / Amazon
    assert cat_keys[0] == "Refunds"
    assert payee_keys[0] == "Amazon"


# ---------------------------------------------------------------------------
# 8. Tool-handler wiring: _reg_query_budget forwards analysis_question
# ---------------------------------------------------------------------------


async def test_reg_query_budget_forwards_analysis_question(monkeypatch):
    """The tool handler must pass analysis_question through verbatim to
    budget_manager.query so the analyze branch receives user intent."""
    from orchestrator import budget_manager, tool_handlers

    query_mock = AsyncMock(
        return_value={
            "question_type": "analyze",
            "dataset": "x",
            "user_question": "foo",
            "filters": {},
            "data": {},
            "expert_synthesis": "stub",
        }
    )
    monkeypatch.setattr(budget_manager, "query", query_mock)

    out = await tool_handlers._reg_query_budget(
        {
            "question_type": "analyze",
            "dataset": "x",
            "analysis_question": "foo",
        }
    )

    # Return value is JSON — must round-trip cleanly
    parsed = json.loads(out)
    assert parsed["dataset"] == "x"

    query_mock.assert_awaited_once()
    kwargs = query_mock.await_args.kwargs
    assert kwargs["dataset"] == "x"
    assert kwargs["question_type"] == "analyze"
    assert kwargs["analysis_question"] == "foo"
