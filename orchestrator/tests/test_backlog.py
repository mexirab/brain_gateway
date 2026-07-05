"""
Tests for the durable task backlog (state_store tasks table + backlog_manager).

Covers the store CRUD + surfacing order, and the manager's fuzzy match / one-
thing-at-a-time / no-guilt behaviors.
"""

import time

from orchestrator import backlog_manager, state_store


def _add(text, **kw):
    """Add via the manager and return the created task id (newest open task)."""
    backlog_manager.add_task(text, **kw)
    # newest by created_at within same priority — grab by text
    for t in state_store.list_tasks(None):
        if t["text"] == text:
            return t["id"]
    raise AssertionError("task not found after add")


# ---------------------------------------------------------------------------
# state_store CRUD + ordering
# ---------------------------------------------------------------------------


class TestStore:
    def test_add_get_and_defaults(self, tmp_db):
        state_store.add_task("t1", "call dentist")
        t = state_store.get_task("t1")
        assert t["text"] == "call dentist"
        assert t["status"] == "open"
        assert t["priority"] == "normal"
        assert t["source"] == "chat"
        assert t["done_at"] is None

    def test_complete_is_idempotent_and_open_only(self, tmp_db):
        state_store.add_task("t1", "x")
        assert state_store.complete_task("t1") is True
        assert state_store.get_task("t1")["status"] == "done"
        # second complete is a no-op (already done)
        assert state_store.complete_task("t1") is False

    def test_drop(self, tmp_db):
        state_store.add_task("t1", "x")
        assert state_store.drop_task("t1") is True
        assert state_store.get_task("t1")["status"] == "dropped"

    def test_open_count(self, tmp_db):
        state_store.add_task("a", "x")
        state_store.add_task("b", "y")
        state_store.complete_task("a")
        assert state_store.open_task_count() == 1

    def test_list_order_high_then_oldest(self, tmp_db):
        # insert normal (old), then high (new), then low
        state_store.add_task("n", "normal task", priority="normal")
        time.sleep(0.01)
        state_store.add_task("h", "high task", priority="high")
        state_store.add_task("l", "low task", priority="low")
        order = [t["id"] for t in state_store.list_tasks("open")]
        assert order == ["h", "n", "l"]  # high first, then normal (older), then low

    def test_update_task_partial(self, tmp_db):
        state_store.add_task("t1", "x", priority="normal")
        assert state_store.update_task("t1", priority="high") is True
        assert state_store.get_task("t1")["priority"] == "high"
        assert state_store.get_task("t1")["text"] == "x"  # unchanged


# ---------------------------------------------------------------------------
# backlog_manager
# ---------------------------------------------------------------------------


class TestAdd:
    def test_add_returns_confirmation_and_persists(self, tmp_db):
        msg = backlog_manager.add_task("buy milk")
        assert "buy milk" in msg
        assert state_store.open_task_count() == 1

    def test_add_empty_prompts(self, tmp_db):
        assert "task" in backlog_manager.add_task("   ").lower()
        assert state_store.open_task_count() == 0

    def test_priority_aliases(self, tmp_db):
        backlog_manager.add_task("urgent thing", priority="urgent")
        backlog_manager.add_task("someday thing", priority="someday")
        prios = {t["text"]: t["priority"] for t in state_store.list_tasks("open")}
        assert prios["urgent thing"] == "high"
        assert prios["someday thing"] == "low"


class TestSurfacing:
    def test_list_open_empty(self, tmp_db):
        assert "clear" in backlog_manager.list_open().lower()

    def test_pick_next_returns_single_high_first(self, tmp_db):
        backlog_manager.add_task("older normal")
        backlog_manager.add_task("newer high", priority="high")
        out = backlog_manager.pick_next()
        assert "newer high" in out
        assert "older normal" not in out  # only ONE surfaced

    def test_pick_next_empty(self, tmp_db):
        assert "clear" in backlog_manager.pick_next().lower()

    def test_backlog_context_summarizes(self, tmp_db):
        for i in range(7):
            backlog_manager.add_task(f"task {i}")
        ctx = backlog_manager.backlog_context()
        assert "Open tasks (7)" in ctx
        assert "+2 more" in ctx  # shows first 5 + count


class TestCompleteAndDrop:
    def test_complete_exact_text(self, tmp_db):
        backlog_manager.add_task("call the dentist")
        out = backlog_manager.complete("call the dentist")
        assert "Done" in out
        assert state_store.open_task_count() == 0

    def test_complete_fuzzy_text(self, tmp_db):
        backlog_manager.add_task("call the dentist about the crown")
        out = backlog_manager.complete("dentist")
        assert "Done" in out
        assert state_store.open_task_count() == 0

    def test_complete_by_id(self, tmp_db):
        tid = _add("submit the expense report")
        out = backlog_manager.complete(tid)
        assert "Done" in out

    def test_complete_no_match(self, tmp_db):
        backlog_manager.add_task("water the plants")
        out = backlog_manager.complete("file taxes")
        assert "couldn't find" in out.lower()
        assert state_store.open_task_count() == 1  # nothing completed

    def test_complete_ambiguous_asks(self, tmp_db):
        backlog_manager.add_task("email Sarah about the invoice")
        backlog_manager.add_task("email Sarah about the party")
        out = backlog_manager.complete("email Sarah")
        assert "which" in out.lower() or "could be" in out.lower()
        assert state_store.open_task_count() == 2  # nothing completed on ambiguity

    def test_complete_clears_list_message(self, tmp_db):
        backlog_manager.add_task("last thing")
        out = backlog_manager.complete("last thing")
        assert "clear" in out.lower()

    def test_drop_is_no_guilt(self, tmp_db):
        backlog_manager.add_task("reorganize the garage")
        out = backlog_manager.drop("garage")
        assert "no worries" in out.lower() or "off your list" in out.lower()
        assert state_store.get_task(state_store.list_tasks(None)[0]["id"])["status"] == "dropped"

    def test_empty_ref_prompts(self, tmp_db):
        backlog_manager.add_task("x")
        assert "which" in backlog_manager.complete("").lower()
