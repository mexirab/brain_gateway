"""auto_learn scheduling: one extraction job per conversation.

Before: the job key included the user-message count, so every chat turn
queued its own extraction on the single LLM slot (a 20-turn voice call queued
20, each over the whole history). Now each turn re-arms the conversation's one
job; a different conversation that opens with the same words keeps its own.
"""

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import shared
from orchestrator.cloud_brain import CloudBrain, _continues_conversation


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, run_date, args, id, replace_existing):  # noqa: A002
        assert replace_existing
        self.jobs[id] = (func, args)

    def remove_job(self, job_id):
        self.jobs.pop(job_id)


@pytest.fixture
def sched(monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(shared, "scheduler", fake)
    monkeypatch.setattr(shared, "AUTO_LEARN_ENABLED", True)
    monkeypatch.setattr(shared, "AUTO_LEARN_DELAY_MINUTES", 10)
    monkeypatch.setattr(shared, "_auto_learn_conversations", {})
    return fake


def _conv(*user_turns):
    msgs = []
    for i, t in enumerate(user_turns):
        msgs.append({"role": "user", "content": t})
        msgs.append({"role": "assistant", "content": f"reply {i}"})
    return msgs[:-1]  # ends on the user turn being answered


def _schedule(messages):
    CloudBrain._schedule_auto_learn(CloudBrain, messages)


def test_multi_turn_conversation_gets_one_job_with_latest_history(sched):
    _schedule(_conv("plan my day"))
    _schedule(_conv("plan my day", "add a walk"))
    _schedule(_conv("plan my day", "add a walk", "and lunch"))

    assert len(sched.jobs) == 1
    assert len(shared._auto_learn_conversations) == 1
    (cached,) = shared._auto_learn_conversations.values()
    assert [m["content"] for m in cached if m["role"] == "user"] == ["plan my day", "add a walk", "and lunch"]


def test_different_conversation_with_same_opener_is_not_lost(sched):
    _schedule(_conv("hi", "what is on today"))
    _schedule(_conv("hi", "set a timer"))  # new chat, same opening line

    assert len(sched.jobs) == 2
    users = sorted(
        tuple(m["content"] for m in msgs if m["role"] == "user") for msgs in shared._auto_learn_conversations.values()
    )
    assert users == [("hi", "set a timer"), ("hi", "what is on today")]


def test_disabled_schedules_nothing(sched, monkeypatch):
    monkeypatch.setattr(shared, "AUTO_LEARN_ENABLED", False)
    _schedule(_conv("plan my day"))
    assert sched.jobs == {}


async def test_job_extracts_once_and_clears_cache(sched):
    _schedule(_conv("plan my day", "add a walk"))
    (func, args) = next(iter(sched.jobs.values()))
    with patch("orchestrator.auto_learn.run_auto_learn", new_callable=AsyncMock) as run:
        await func(*args)
        await func(*args)  # a second firing finds nothing left to do
    run.assert_awaited_once()
    assert shared._auto_learn_conversations == {}


def test_cache_stays_capped(sched):
    for i in range(30):
        _schedule(_conv(f"conversation {i}"))
    assert len(shared._auto_learn_conversations) <= 20


@pytest.mark.parametrize(
    ("prev", "new", "expected"),
    [
        (["a"], ["a", "b"], True),
        (["a", "b"], ["a", "b"], True),
        (["a", "b"], ["a", "c"], False),
        (["a", "b"], ["a"], False),
    ],
)
def test_continues_conversation(prev, new, expected):
    assert _continues_conversation(_conv(*prev), _conv(*new)) is expected


def test_continues_conversation_reads_multipart_text():
    prev = [{"role": "user", "content": [{"type": "text", "text": "look at this"}, {"type": "image_url"}]}]
    new = prev + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "and?"}]
    assert _continues_conversation(prev, new) is True
