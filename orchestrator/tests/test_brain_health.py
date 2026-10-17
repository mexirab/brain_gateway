"""Brain-health observability: embedding error counter, auto_learn error/run
outcome counters, and the auto_learn orphaned-snapshot re-arm.

Counters are read from the default prometheus registry before/after each
action, so the tests are order-independent.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY

from orchestrator import auto_learn, shared
from orchestrator.cloud_brain import CloudBrain
from orchestrator.shared import _CountingEmbedder


def _val(name, **labels):
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _emb_err(caller):
    return _val("bgw_embedding_errors_total", caller=caller)


def _al_err(stage):
    return _val("bgw_auto_learn_errors_total", stage=stage)


def _al_run(outcome):
    return _val("bgw_auto_learn_runs_total", outcome=outcome)


# ---------------------------------------------------------------------------
# shared._CountingEmbedder
# ---------------------------------------------------------------------------


class _FakeModel:
    max_seq_length = 512

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def encode(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result

    def get_sentence_embedding_dimension(self):
        return 768


def test_embedder_encode_passes_through_result_and_args():
    sentinel = object()
    fake = _FakeModel(result=sentinel)
    emb = _CountingEmbedder(fake)
    before = _emb_err("test_brain_health")

    assert emb.encode("hello", normalize_embeddings=True) is sentinel
    assert fake.calls == [(("hello",), {"normalize_embeddings": True})]
    assert _emb_err("test_brain_health") == before


def test_embedder_reraises_same_exception_and_counts_calling_module():
    err = RuntimeError("tokenizer exploded")
    emb = _CountingEmbedder(_FakeModel(exc=err))
    before = _emb_err("test_brain_health")

    with pytest.raises(RuntimeError) as info:
        emb.encode("hello")

    assert info.value is err
    # caller label = short module name of whoever called encode() (this module)
    assert _emb_err("test_brain_health") == before + 1


def test_embedder_delegates_other_attributes():
    emb = _CountingEmbedder(_FakeModel())
    assert emb.max_seq_length == 512
    assert emb.get_sentence_embedding_dimension() == 768
    with pytest.raises(AttributeError):
        _ = emb.no_such_attribute


def test_embedder_without_model_raises_attribute_error_not_recursion():
    emb = _CountingEmbedder.__new__(_CountingEmbedder)  # as copy/unpickle would
    with pytest.raises(AttributeError):
        _ = emb._model
    with pytest.raises(AttributeError):
        _ = emb.encode_multi_process  # any delegated name, no RecursionError


def test_shared_embedding_model_is_wrapped():
    assert isinstance(shared.embedding_model, _CountingEmbedder)


# ---------------------------------------------------------------------------
# auto_learn._is_json_array
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[]", True),
        ('[{"fact": "likes tea"}]', True),
        ('```json\n[{"fact": "likes tea"}]\n```', True),
        ("```\n[]\n```", True),
        ("I could not find any facts in this conversation.", False),
        ("", False),
        (None, False),
        ('{"fact": "likes tea"}', False),
        ("[not json at all", False),
        ("[1, 2,", False),
    ],
)
def test_is_json_array(raw, expected):
    assert auto_learn._is_json_array(raw) is expected


# ---------------------------------------------------------------------------
# extract_facts / store_fact error stages
# ---------------------------------------------------------------------------

_CONV = [
    {"role": "user", "content": "I just moved to a new apartment and I really love cooking on weekends."},
    {"role": "assistant", "content": "That sounds lovely, congrats on the new place!"},
]


def _reply(content):
    return {"choices": [{"message": {"content": content}}]}


def _patch_llm(**kwargs):
    return patch("orchestrator.orchestrator.call_model", AsyncMock(**kwargs))


async def test_extract_llm_failure_counts_llm_stage():
    before = _al_err("llm")
    with _patch_llm(side_effect=TimeoutError()):
        assert await auto_learn.extract_facts(_CONV) == []
    assert _al_err("llm") == before + 1


@pytest.mark.parametrize("content", ["", "   \n", None])
async def test_extract_empty_reply_counts_empty_stage(content):
    before_empty, before_parse = _al_err("empty"), _al_err("parse")
    with _patch_llm(return_value=_reply(content)):
        assert await auto_learn.extract_facts(_CONV) == []
    assert _al_err("empty") == before_empty + 1
    assert _al_err("parse") == before_parse


async def test_extract_non_json_counts_parse_stage():
    before = _al_err("parse")
    with _patch_llm(return_value=_reply("Sure! The user likes cooking.")):
        assert await auto_learn.extract_facts(_CONV) == []
    assert _al_err("parse") == before + 1


async def test_extract_valid_empty_array_is_not_an_error():
    before = {s: _al_err(s) for s in ("llm", "empty", "parse")}
    with _patch_llm(return_value=_reply("[]")):
        assert await auto_learn.extract_facts(_CONV) == []
    assert {s: _al_err(s) for s in before} == before


async def test_store_fact_embedding_failure_counts_store_and_embedding(monkeypatch):
    monkeypatch.setattr(shared, "embedding_model", _CountingEmbedder(_FakeModel(exc=RuntimeError("boom"))))
    monkeypatch.setattr(shared, "PALACE_ENABLED", False)
    add = MagicMock()
    monkeypatch.setattr(shared, "collection", MagicMock(add=add))
    before_store, before_emb = _al_err("store"), _emb_err("auto_learn")

    with patch.object(auto_learn, "encrypt_text", side_effect=lambda t: t):
        result = await auto_learn.store_fact({"fact": "User loves cooking on weekends", "category": "preferences"})

    assert result is None
    add.assert_not_called()
    assert _al_err("store") == before_store + 1
    # encode() runs inside a lambda in auto_learn -> caller label is auto_learn
    assert _emb_err("auto_learn") == before_emb + 1


async def test_store_fact_chroma_failure_counts_store(monkeypatch):
    import numpy as np

    monkeypatch.setattr(shared, "embedding_model", _CountingEmbedder(_FakeModel(result=np.zeros(4))))
    monkeypatch.setattr(shared, "PALACE_ENABLED", False)
    monkeypatch.setattr(shared, "collection", MagicMock(add=MagicMock(side_effect=RuntimeError("disk"))))
    before = _al_err("store")

    with patch.object(auto_learn, "encrypt_text", side_effect=lambda t: t):
        result = await auto_learn.store_fact({"fact": "User loves cooking on weekends", "category": "preferences"})

    assert result is None
    assert _al_err("store") == before + 1


# ---------------------------------------------------------------------------
# run_auto_learn outcomes
# ---------------------------------------------------------------------------

_FACTS_JSON = '[{"fact": "User loves cooking on weekends", "category": "preferences", "confidence": "high"}]'


async def _run(conv=_CONV, content=_FACTS_JSON, llm_exc=None, duplicate=False, store_result="doc1"):
    llm = AsyncMock(side_effect=llm_exc) if llm_exc else AsyncMock(return_value=_reply(content))
    with (
        patch("orchestrator.orchestrator.call_model", llm),
        patch.object(auto_learn, "is_duplicate", AsyncMock(return_value=duplicate)),
        patch.object(auto_learn, "store_fact", AsyncMock(return_value=store_result)),
    ):
        await auto_learn.run_auto_learn(conv)


async def _assert_outcome(expected, **kwargs):
    outcomes = ("stored", "duplicates_only", "no_facts", "opt_out", "error")
    before = {o: _al_run(o) for o in outcomes}
    await _run(**kwargs)
    after = {o: _al_run(o) for o in outcomes}
    deltas = {o: after[o] - before[o] for o in outcomes}
    assert deltas == {o: (1.0 if o == expected else 0.0) for o in outcomes}


async def test_run_outcome_opt_out():
    conv = [{"role": "user", "content": "Don't remember this, but I am thinking about switching jobs soon."}]
    await _assert_outcome("opt_out", conv=conv)


async def test_run_outcome_no_facts_on_valid_empty_array():
    await _assert_outcome("no_facts", content="[]")


async def test_run_outcome_error_on_llm_failure():
    await _assert_outcome("error", llm_exc=ConnectionError())


async def test_run_outcome_error_on_parse_failure():
    await _assert_outcome("error", content="no JSON here, sorry")


async def test_run_outcome_stored():
    await _assert_outcome("stored")


async def test_run_outcome_duplicates_only():
    await _assert_outcome("duplicates_only", duplicate=True)


async def test_run_outcome_error_when_every_store_fails():
    # store_fact returning None without recording an error (e.g. category
    # gate) is not an error; patch _record_error-style failure via real path:
    async def failing_store(fact):
        auto_learn._record_error("store")
        return None

    outcomes = ("stored", "duplicates_only", "error")
    before = {o: _al_run(o) for o in outcomes}
    with (
        patch("orchestrator.orchestrator.call_model", AsyncMock(return_value=_reply(_FACTS_JSON))),
        patch.object(auto_learn, "is_duplicate", AsyncMock(return_value=False)),
        patch.object(auto_learn, "store_fact", failing_store),
    ):
        await auto_learn.run_auto_learn(_CONV)
    assert _al_run("error") == before["error"] + 1
    assert _al_run("stored") == before["stored"]


async def test_run_failure_flag_does_not_leak_between_runs():
    await _assert_outcome("error", llm_exc=ConnectionError())
    await _assert_outcome("no_facts", content="[]")


# ---------------------------------------------------------------------------
# CloudBrain._rearm_if_orphaned + _run_auto_learn_job
# ---------------------------------------------------------------------------

_KEY = "0123456789abcdef"


def _rearmed():
    return _val("bgw_auto_learn_rearmed_total")


@pytest.fixture
def sched(monkeypatch):
    fake = MagicMock()
    fake.get_job.return_value = None
    monkeypatch.setattr(shared, "scheduler", fake)
    monkeypatch.setattr(shared, "AUTO_LEARN_DELAY_MINUTES", 10)
    monkeypatch.setattr(shared, "_auto_learn_conversations", {})
    return fake


def test_rearm_not_cached_does_nothing(sched):
    before = _rearmed()
    CloudBrain._rearm_if_orphaned(_KEY)
    sched.add_job.assert_not_called()
    assert _rearmed() == before


def test_rearm_cached_with_pending_job_does_nothing(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    sched.get_job.return_value = object()
    before = _rearmed()

    CloudBrain._rearm_if_orphaned(_KEY)

    sched.get_job.assert_called_once_with(f"auto_learn_{_KEY}")
    sched.add_job.assert_not_called()
    assert _rearmed() == before


def test_rearm_cached_without_job_adds_job_and_counts(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    before = _rearmed()

    CloudBrain._rearm_if_orphaned(_KEY)

    sched.add_job.assert_called_once()
    kwargs = sched.add_job.call_args.kwargs
    assert kwargs["id"] == f"auto_learn_{_KEY}"
    assert kwargs["args"] == [_KEY]
    assert kwargs["trigger"] == "date"
    assert kwargs["replace_existing"] is True
    assert sched.add_job.call_args.args[0] is CloudBrain._run_auto_learn_job
    assert _rearmed() == before + 1


def test_rearm_add_job_failure_is_swallowed(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    sched.add_job.side_effect = RuntimeError("scheduler shut down")
    before = _rearmed()

    CloudBrain._rearm_if_orphaned(_KEY)  # must not raise

    assert _rearmed() == before


def test_rearm_get_job_failure_is_swallowed(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    sched.get_job.side_effect = RuntimeError("jobstore gone")
    CloudBrain._rearm_if_orphaned(_KEY)  # must not raise
    sched.add_job.assert_not_called()


async def test_job_rearms_conversation_that_continued_during_run(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)

    async def run_and_new_turn(messages):
        # a new turn arrived mid-extraction and re-cached the conversation;
        # its re-armed job was skipped by APScheduler (get_job -> None)
        shared._auto_learn_conversations[_KEY] = list(messages) + [{"role": "user", "content": "one more"}]

    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=True)),
        patch("orchestrator.auto_learn.run_auto_learn", side_effect=run_and_new_turn),
    ):
        await CloudBrain._run_auto_learn_job(_KEY)

    sched.add_job.assert_called_once()
    assert sched.add_job.call_args.kwargs["id"] == f"auto_learn_{_KEY}"


async def test_job_rearm_check_runs_even_when_run_auto_learn_raises(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=True)),
        patch("orchestrator.auto_learn.run_auto_learn", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(CloudBrain, "_rearm_if_orphaned") as rearm,
        pytest.raises(RuntimeError),
    ):
        await CloudBrain._run_auto_learn_job(_KEY)
    rearm.assert_called_once_with(_KEY)


async def test_job_without_new_turns_does_not_rearm(sched):
    shared._auto_learn_conversations[_KEY] = list(_CONV)
    with (
        patch("orchestrator.model_manager.check_model_health", AsyncMock(return_value=True)),
        patch("orchestrator.auto_learn.run_auto_learn", new_callable=AsyncMock) as run,
    ):
        await CloudBrain._run_auto_learn_job(_KEY)
    run.assert_awaited_once()
    sched.add_job.assert_not_called()
    assert shared._auto_learn_conversations == {}
