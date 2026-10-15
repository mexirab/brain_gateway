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
    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=True)),
        patch("orchestrator.auto_learn.run_auto_learn", new_callable=AsyncMock) as run,
    ):
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


# --- conversation_id from the client (dashboard / Telegram) -----------------

from orchestrator.cloud_brain import conversation_id_var  # noqa: E402


@pytest.fixture
def cid():
    token = conversation_id_var.set("telegram:12345:1700000000")
    yield
    conversation_id_var.reset(token)


def _window(first, last):
    """Telegram-style rolling window over a conversation of turns t1..tN."""
    msgs = []
    for i in range(1, last + 1):
        msgs += [{"role": "user", "content": f"t{i}"}, {"role": "assistant", "content": f"r{i}"}]
    return msgs[(first - 1) * 2 : -1]


def test_conversation_id_merges_a_sliding_window(sched, cid):
    # Telegram sends only the last N messages: the opening turns slide out,
    # but they must still be extracted.
    _schedule(_window(1, 3))
    _schedule(_window(2, 4))
    _schedule(_window(3, 5))
    assert len(sched.jobs) == 1
    (cached,) = shared._auto_learn_conversations.values()
    assert [m["content"] for m in cached if m["role"] == "user"] == ["t1", "t2", "t3", "t4", "t5"]


def test_conversation_id_edit_replaces_instead_of_flushing(sched, cid):
    _schedule(_conv("my appointment is Tuesday"))
    _schedule(_conv("my appointment is Wednesday"))
    assert len(sched.jobs) == 1
    (cached,) = shared._auto_learn_conversations.values()
    assert cached[0]["content"] == "my appointment is Wednesday"


def test_cached_copy_is_not_aliased_to_the_callers_list(sched, cid):
    msgs = _conv("plan my day")
    _schedule(msgs)
    msgs.append({"role": "user", "content": "The action(s) above are complete."})  # loop mutation
    (cached,) = shared._auto_learn_conversations.values()
    assert len(cached) == 1


def test_different_conversation_ids_get_different_jobs(sched):
    for c in ("dashboard:a", "dashboard:b"):
        token = conversation_id_var.set(c)
        _schedule(_conv("hi"))
        conversation_id_var.reset(token)
    assert len(sched.jobs) == 2


# --- fallback heuristic refinements -----------------------------------------


def test_fallback_edit_of_last_turn_is_extracted_separately(sched):
    # Without a conversation id an edit is indistinguishable from a new chat
    # with the same opener; keep both rather than risk losing one.
    _schedule(_conv("plan my day", "add a walk"))
    _schedule(_conv("plan my day", "add a run"))
    assert len(sched.jobs) == 2


def test_fallback_ignores_regenerated_image_analysis(sched):
    first = [{"role": "user", "content": "look\n\n[Image 1 Analysis]\nA red mug on a desk."}]
    second = [
        {"role": "user", "content": "look\n\n[Image 1 Analysis]\nA red cup on a table."},
        {"role": "assistant", "content": "nice"},
        {"role": "user", "content": "thanks"},
    ]
    _schedule(first)
    _schedule(second)
    assert len(sched.jobs) == 1


def test_chat_request_conversation_id_validation():
    from orchestrator.schemas import ChatRequest

    ok = ChatRequest(messages=[], conversation_id="telegram:-100123:1700000000")
    assert ok.conversation_id == "telegram:-100123:1700000000"
    assert ChatRequest(messages=[]).conversation_id is None
    # A malformed id is dropped, never a reason to fail the chat turn.
    for bad in ("has space", "x" * 129, "semi;colon", "a\nb", 42, ["x"]):
        assert ChatRequest(messages=[], conversation_id=bad).conversation_id is None


async def test_extraction_keeps_the_end_of_long_conversations():
    from orchestrator import auto_learn

    msgs = []
    for i in range(80):
        msgs.append({"role": "user", "content": f"turn {i} with some filler text to make it long enough"})
        msgs.append({"role": "assistant", "content": "a long reply " * 60})
    msgs.append({"role": "user", "content": "LAST TURN marker"})
    captured = {}

    async def fake_call_model(*args, **kwargs):
        captured["prompt"] = str(args) + str(kwargs)
        raise RuntimeError("stop after capturing the prompt")

    with patch("orchestrator.orchestrator.call_model", fake_call_model):
        await auto_learn.extract_facts(msgs)
    prompt = captured.get("prompt", "")
    assert "LAST TURN marker" in prompt
    # Assistant replies are shortened before any user turn is dropped.
    assert "[...]" in prompt
    assert "turn 0 with" not in prompt
    assert len(prompt) < 16000


def test_conversation_id_without_overlap_replaces(sched, cid):
    _schedule(_conv("my appointment is Tuesday", "thanks"))
    _schedule(_conv("my appointment is Wednesday", "thanks"))  # edited earlier turn
    (cached,) = shared._auto_learn_conversations.values()
    assert cached[0]["content"] == "my appointment is Wednesday"


def test_eviction_is_counted(sched):
    from orchestrator.metrics import AUTO_LEARN_DROPPED

    before = AUTO_LEARN_DROPPED.labels(reason="evicted")._value.get()
    for i in range(25):
        _schedule(_conv(f"conversation {i}"))
    assert AUTO_LEARN_DROPPED.labels(reason="evicted")._value.get() - before == 5


# --- brain asleep at fire time ----------------------------------------------


async def test_job_retries_when_model_is_unreachable(sched):
    _schedule(_conv("plan my day"))
    (job_id, (func, args)) = next(iter(sched.jobs.items()))
    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=False)),
        patch("orchestrator.auto_learn.run_auto_learn", new_callable=AsyncMock) as run,
    ):
        await func(*args)
    run.assert_not_awaited()
    assert len(shared._auto_learn_conversations) == 1  # kept for the retry
    _, retry_args = sched.jobs[job_id]
    assert retry_args[1] == 1  # attempt counter advanced

    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=True)),
        patch("orchestrator.auto_learn.run_auto_learn", new_callable=AsyncMock) as run,
    ):
        await func(*retry_args)
    run.assert_awaited_once()
    assert shared._auto_learn_conversations == {}


async def test_job_gives_up_after_max_attempts(sched):
    from orchestrator.cloud_brain import _AUTO_LEARN_MAX_ATTEMPTS
    from orchestrator.metrics import AUTO_LEARN_DROPPED

    _schedule(_conv("plan my day"))
    (func, args) = next(iter(sched.jobs.values()))
    before = AUTO_LEARN_DROPPED.labels(reason="model_unavailable")._value.get()
    with patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=False)):
        await func(args[0], _AUTO_LEARN_MAX_ATTEMPTS - 1)
    assert shared._auto_learn_conversations == {}
    assert AUTO_LEARN_DROPPED.labels(reason="model_unavailable")._value.get() - before == 1


# --- streaming path: the loop mutates `messages` in place --------------------


async def test_streaming_schedules_the_client_transcript_not_the_loop_mutated_list(sched):
    def make_brain():
        brain = CloudBrain.__new__(CloudBrain)
        brain._fallback_model_url = ""
        brain._fallback_model_name = "fb"
        brain._get_all_tools = lambda: []

        async def loop_fn(**kwargs):
            kwargs["messages"].append({"role": "user", "content": "The action(s) above are complete."})
            return "Done"

        brain._run_unified_loop = loop_fn
        return brain

    for turn in (_conv("turn on the lamp"), _conv("turn on the lamp", "and the fan")):
        resp = make_brain()._stream_loop_response(
            messages=list(turn), system_prompt="s", tools=[], model_url="u", model_name="m", is_voice=False
        )
        async for _ in resp.body_iterator:
            pass

    assert len(sched.jobs) == 1
    (cached,) = shared._auto_learn_conversations.values()
    assert [m["content"] for m in cached if m["role"] == "user"] == ["turn on the lamp", "and the fan"]
