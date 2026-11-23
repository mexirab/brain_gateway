"""brain_slot: who gets the single llama slot (chat first, coder gate last)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import brain_slot


@pytest.fixture(autouse=True)
def _fresh_slot(monkeypatch):
    monkeypatch.setattr(brain_slot, "_last_chat_activity", 0.0)
    monkeypatch.setattr(brain_slot, "_coder_in_flight", 0)
    monkeypatch.setattr(brain_slot, "_interactive_in_flight", 0)
    monkeypatch.setattr("orchestrator.research_helper._active", 0)


def _gauge(monkeypatch, value):
    gauge = MagicMock()
    gauge._value.get.return_value = value
    monkeypatch.setattr(brain_slot, "ACTIVE_REQUESTS", gauge)
    return gauge


class TestBusy:
    def test_idle_by_default(self, monkeypatch):
        _gauge(monkeypatch, 0)
        assert not brain_slot.chat_busy()
        assert not brain_slot.coder_busy()
        assert brain_slot.seconds_since_chat() == float("inf")

    def test_chat_turn_is_busy(self, monkeypatch):
        _gauge(monkeypatch, 1)
        assert brain_slot.chat_busy()

    def test_lookup_is_busy(self, monkeypatch):
        _gauge(monkeypatch, 0)
        monkeypatch.setattr("orchestrator.research_helper._active", 1)
        assert brain_slot.chat_busy()

    def test_gauge_error_counts_as_idle(self, monkeypatch):
        gauge = MagicMock()
        gauge._value.get.side_effect = AttributeError("prometheus internals changed")
        monkeypatch.setattr(brain_slot, "ACTIVE_REQUESTS", gauge)
        assert not brain_slot.chat_busy()

    def test_coder_request_marks_and_unmarks(self):
        with brain_slot.coder_request():
            assert brain_slot.coder_busy()
        assert not brain_slot.coder_busy()

    def test_coder_request_unmarks_on_error(self):
        with pytest.raises(RuntimeError), brain_slot.coder_request():
            raise RuntimeError("boom")
        assert not brain_slot.coder_busy()

    def test_note_chat_activity_resets_clock(self):
        brain_slot.note_chat_activity()
        assert brain_slot.seconds_since_chat() < 1.0


class TestInteractiveCall:
    def test_busy_even_when_the_gauge_breaks(self, monkeypatch):
        gauge = MagicMock()
        gauge._value.get.side_effect = AttributeError("prometheus internals changed")
        monkeypatch.setattr(brain_slot, "ACTIVE_REQUESTS", gauge)
        with brain_slot.interactive_call():
            assert brain_slot.chat_busy()
        assert not brain_slot.chat_busy()

    def test_nested_calls_count(self, monkeypatch):
        _gauge(monkeypatch, 0)
        with brain_slot.interactive_call():
            with brain_slot.interactive_call():
                assert brain_slot._interactive_in_flight == 2
            assert brain_slot.chat_busy()  # the outer call still runs
        assert brain_slot._interactive_in_flight == 0 and not brain_slot.chat_busy()

    def test_unmarks_on_error(self, monkeypatch):
        _gauge(monkeypatch, 0)
        with pytest.raises(RuntimeError), brain_slot.interactive_call():
            raise RuntimeError("vision failed")
        assert brain_slot._interactive_in_flight == 0 and not brain_slot.chat_busy()

    def test_stamps_the_idle_clock_on_entry_and_exit(self, monkeypatch):
        """A coder request must wait the idle period after a photo, not just during it."""
        with brain_slot.interactive_call():
            assert brain_slot.seconds_since_chat() < 1.0
            monkeypatch.setattr(brain_slot, "_last_chat_activity", 0.0)
        assert brain_slot.seconds_since_chat() < 1.0

    def test_not_a_coder_request(self):
        with brain_slot.interactive_call():
            assert not brain_slot.coder_busy()


class TestWaitForCoderTurn:
    async def test_goes_at_once_when_quiet(self, monkeypatch):
        _gauge(monkeypatch, 0)
        assert await brain_slot.wait_for_coder_turn(idle_s=20, max_wait_s=1, poll_s=0.01)

    async def test_waits_for_idle_period_after_chat(self, monkeypatch):
        _gauge(monkeypatch, 0)
        brain_slot.note_chat_activity()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert await brain_slot.wait_for_coder_turn(idle_s=0.15, max_wait_s=2, poll_s=0.01)
        assert loop.time() - t0 >= 0.1

    async def test_gives_up_while_chat_stays_busy(self, monkeypatch):
        _gauge(monkeypatch, 1)
        assert not await brain_slot.wait_for_coder_turn(idle_s=0, max_wait_s=0.1, poll_s=0.01)

    async def test_waits_out_an_interactive_call(self, monkeypatch):
        _gauge(monkeypatch, 0)
        done = asyncio.Event()

        async def photo():
            with brain_slot.interactive_call():
                await done.wait()

        task = asyncio.create_task(photo())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(brain_slot.wait_for_coder_turn(idle_s=0, max_wait_s=2, poll_s=0.01))
        await asyncio.sleep(0.05)
        assert not waiter.done()  # held while the photo runs
        done.set()
        assert await asyncio.wait_for(waiter, 2)
        await task

    async def test_negative_wait_checks_once(self, monkeypatch):
        _gauge(monkeypatch, 1)
        sleep = AsyncMock()
        monkeypatch.setattr(brain_slot, "asyncio", SimpleNamespace(sleep=sleep))
        assert not await brain_slot.wait_for_coder_turn(idle_s=0, max_wait_s=-5)
        assert sleep.await_count == 0

    async def test_busy_poll_resets_idle_clock(self, monkeypatch):
        gauge = _gauge(monkeypatch, 0)
        gauge._value.get.side_effect = [1, 0, 0, 0, 0, 0, 0, 0, 0, 0] + [0] * 500
        assert await brain_slot.wait_for_coder_turn(idle_s=0.05, max_wait_s=2, poll_s=0.01)
        # the busy poll was recorded as chat activity
        assert brain_slot.seconds_since_chat() < 1.0


class TestBackgroundWaitersSeeTheCoder:
    """auto_learn / meal planner / investigator treat a coder request as busy."""

    async def test_auto_learn_waits_for_coder(self, monkeypatch):
        from orchestrator import auto_learn

        gauge = MagicMock()
        gauge._value.get.return_value = 0
        sleep = AsyncMock()
        clock = iter([0.0, 0.0, 30.0, auto_learn._CHAT_IDLE_WAIT_S + 1.0])
        monkeypatch.setattr(auto_learn, "ACTIVE_REQUESTS", gauge)
        monkeypatch.setattr(auto_learn, "asyncio", SimpleNamespace(sleep=sleep))
        monkeypatch.setattr(auto_learn, "time", SimpleNamespace(monotonic=lambda: next(clock)))
        monkeypatch.setattr(brain_slot, "_coder_in_flight", 1)
        await auto_learn._wait_for_idle_chat()
        assert sleep.await_count == 2

    async def test_meal_planner_waits_for_coder(self, monkeypatch):
        from orchestrator import meal_planner

        gauge = MagicMock()
        gauge._value.get.return_value = 0
        monkeypatch.setattr(meal_planner, "ACTIVE_REQUESTS", gauge)
        monkeypatch.setattr(meal_planner, "CHAT_IDLE_WAIT_S", 0.05)
        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s):
            sleeps.append(s)
            await real_sleep(0.02)

        # Swap the module reference, not asyncio.sleep itself (that is global).
        monkeypatch.setattr(meal_planner, "asyncio", SimpleNamespace(sleep=fake_sleep))
        monkeypatch.setattr(brain_slot, "_coder_in_flight", 1)
        await meal_planner._wait_for_idle_chat()
        assert sleeps  # it waited instead of returning at once

    async def test_investigator_slot_busy_sees_coder(self, monkeypatch):
        from orchestrator import self_investigator

        gauge = MagicMock()
        gauge._value.get.return_value = 0
        monkeypatch.setattr(self_investigator, "ACTIVE_REQUESTS", gauge)
        monkeypatch.setattr(brain_slot, "_coder_in_flight", 1)
        assert await self_investigator._slot_busy() is True
