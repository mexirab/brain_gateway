"""
Tests for auto_learn.py — sensitive-data filter, privacy opt-out, conversation
formatting, JSON parsing, the extraction pipeline (LLM + ChromaDB mocked), the
structured-domain gate and the delete guard. Every test calls the real module.

Conversation windows, per-window extraction and store routing are covered in
test_memory_recall.py; key-file loading in test_non_root.py.
"""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from orchestrator import auto_learn, shared
from orchestrator.auto_learn import (
    _contains_sensitive_data,
    _format_conversation,
    _parse_facts_json,
    conversation_has_opt_out,
)

# ---------------------------------------------------------------------------
# Sensitive data filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,flagged",
    [
        pytest.param("My card is 4111 1111 1111 1111", True, id="card-spaces"),
        pytest.param("Card: 4111-1111-1111-1111", True, id="card-dashes"),
        pytest.param("4111111111111111", True, id="card-no-separator"),
        # Amex format matches when written as 4-4-4-3 groups
        pytest.param("Amex: 3782-8224-6310-005", True, id="amex-15"),
        pytest.param("SSN is 123-45-6789", True, id="ssn"),
        pytest.param("My key is sk_live_abcdefghij1234567890", True, id="api-key"),
        pytest.param("AWS key: AKIAIOSFODNN7EXAMPLE", True, id="aws-key"),
        pytest.param("My password is hunter2", True, id="password"),
        pytest.param("password is s3cret123", True, id="password-bare"),
        pytest.param("My account number: 12345678901", True, id="bank-account"),
        pytest.param("routing # 021000021", True, id="routing-number"),
        pytest.param("-----BEGIN PRIVATE KEY-----", True, id="private-key"),
        pytest.param("-----BEGIN RSA PRIVATE KEY-----", True, id="rsa-private-key"),
        pytest.param("Token: eyJhbGciOiJIUzI1NiIsInR5.eyJzdWIiOiIxMjM0NTY3ODkw", True, id="jwt"),
        pytest.param("I like coffee and running in the mornings", False, id="normal-text"),
        pytest.param("My zip code is 78701", False, id="short-number"),
        pytest.param("I prefer using dark mode for everything", False, id="preference"),
    ],
)
def test_sensitive_data_filter(text, flagged):
    assert _contains_sensitive_data(text) is flagged


# ---------------------------------------------------------------------------
# Privacy opt-out detection
# ---------------------------------------------------------------------------


def _user(text):
    return [{"role": "user", "content": text}]


@pytest.mark.parametrize(
    "msgs,opted_out",
    [
        pytest.param(_user("Don't remember this conversation"), True, id="dont-remember"),
        pytest.param(_user("Hey, this is private but I need help with..."), True, id="this-is-private"),
        pytest.param(_user("Off the record, I've been feeling stressed"), True, id="off-the-record"),
        pytest.param(_user("Forget this conversation please"), True, id="forget-this"),
        pytest.param(_user("This is confidential information"), True, id="confidential"),
        pytest.param(_user("Dont save this please"), True, id="no-apostrophe"),
        pytest.param(_user("Do not store this conversation"), True, id="do-not-store"),
        pytest.param(_user("This is not for the record..."), True, id="not-for-the-record"),
        pytest.param(_user("DON'T SAVE THIS"), True, id="case-insensitive"),
        pytest.param(
            [
                {"role": "user", "content": "I love hiking"},
                {"role": "assistant", "content": "Nice!"},
                {"role": "user", "content": "Actually, don't remember this please"},
            ],
            True,
            id="mid-conversation",
        ),
        pytest.param(
            [
                {"role": "user", "content": "What's the weather like?"},
                {"role": "assistant", "content": "It's sunny today."},
                {"role": "user", "content": "Thanks!"},
            ],
            False,
            id="normal-conversation",
        ),
        pytest.param(
            [
                {"role": "user", "content": "Tell me about my schedule"},
                {"role": "assistant", "content": "This is private info: you have a meeting at 3."},
            ],
            False,
            id="assistant-says-private",
        ),
    ],
)
def test_privacy_opt_out(msgs, opted_out):
    assert conversation_has_opt_out(msgs) is opted_out


# ---------------------------------------------------------------------------
# Conversation formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msgs,expected",
    [
        pytest.param(
            [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi there!"}],
            "User: Hello\nAssistant: Hi there!",
            id="basic",
        ),
        pytest.param(
            [{"role": "system", "content": "You are helpful"}, {"role": "user", "content": "Hi"}],
            "User: Hi",
            id="system-excluded",
        ),
        pytest.param(
            [{"role": "user", "content": ""}, {"role": "user", "content": "Real message"}],
            "User: Real message",
            id="empty-skipped",
        ),
        pytest.param(
            [{"role": "user", "content": "   "}, {"role": "user", "content": "Actual content"}],
            "User: Actual content",
            id="whitespace-skipped",
        ),
        pytest.param(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look at this"},
                        {"type": "image_url", "image_url": "data:..."},
                        {"type": "text", "text": "and tell me what you see"},
                    ],
                }
            ],
            "User: Look at this and tell me what you see",
            id="multipart",
        ),
        pytest.param(
            [
                {"role": "user", "content": "Check the weather"},
                {"role": "tool", "content": '{"temp": 72}'},
                {"role": "assistant", "content": "It's 72 degrees."},
            ],
            "User: Check the weather\nAssistant: It's 72 degrees.",
            id="tool-excluded",
        ),
        pytest.param([{"role": "user", "content": "  hello world  "}], "User: hello world", id="stripped"),
    ],
)
def test_format_conversation(msgs, expected):
    assert _format_conversation(msgs) == expected


# ---------------------------------------------------------------------------
# JSON parsing of the model's reply
# ---------------------------------------------------------------------------


def _facts_json(*facts):
    return json.dumps([{"fact": f, "category": "x", "confidence": "high", "source_quote": "q"} for f in facts])


@pytest.mark.parametrize(
    "raw,facts",
    [
        pytest.param(_facts_json("Likes coffee"), ["Likes coffee"], id="clean"),
        pytest.param("[]", [], id="empty-array"),
        pytest.param(f"```json\n{_facts_json('Runs daily')}\n```", ["Runs daily"], id="markdown-fences"),
        pytest.param(f"Here are the extracted facts:\n{_facts_json('Has a dog')}", ["Has a dog"], id="preamble"),
        pytest.param(f"{_facts_json('Works remotely')}\nThose are the facts.", ["Works remotely"], id="trailing"),
        pytest.param(
            _facts_json("Fact one", "Fact two", "Fact three"), ["Fact one", "Fact two", "Fact three"], id="three"
        ),
        pytest.param("this is not json", [], id="not-json"),
        pytest.param('{"fact": "single object"}', [], id="no-brackets"),
        pytest.param('[{"fact": "incomplete', [], id="malformed-array"),
        pytest.param("", [], id="empty-string"),
        pytest.param("No facts could be extracted from this conversation.", [], id="just-text"),
        pytest.param("```\n[]\n```", [], id="bare-fences"),
    ],
)
def test_parse_facts_json(raw, facts):
    assert [f["fact"] for f in _parse_facts_json(raw)] == facts


def test_extraction_prompt_wraps_conversation_in_delimiters():
    assert "<<<" in auto_learn._EXTRACTION_PROMPT
    assert ">>>" in auto_learn._EXTRACTION_PROMPT
    assert "{conversation}" in auto_learn._EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# Pipeline (mocked LLM + ChromaDB)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_shared():
    with patch("orchestrator.auto_learn.shared") as mock:
        mock.AUTO_LEARN_ENABLED = True
        mock.AUTO_LEARN_ENCRYPT = False
        mock.AUTO_LEARN_MARKDOWN = False
        mock.AUTO_LEARN_MAX_FACTS = 5
        mock.AUTO_LEARN_DEDUP_THRESHOLD = 0.85
        mock.AUTO_LEARN_ENCRYPTION_KEY = ""
        mock.MODEL_URL = "http://fake:8001/v1"
        mock.MODEL_NAME = "test-model"
        mock.embedding_model.encode.return_value = np.zeros(384)
        mock.collection.query.return_value = {"documents": [[]], "distances": [[]]}
        mock.collection.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        mock.collection.add = MagicMock()
        yield mock


def _llm_reply(facts):
    return {"choices": [{"message": {"content": json.dumps(facts)}}]}


@pytest.mark.parametrize(
    "msgs",
    [
        pytest.param(
            [
                {"role": "user", "content": "My favorite color is blue"},
                {"role": "user", "content": "Don't remember this"},
            ],
            id="opt-out",
        ),
        pytest.param([{"role": "user", "content": "Hi"}], id="too-short"),
    ],
)
async def test_run_auto_learn_skips(mock_shared, msgs):
    await auto_learn.run_auto_learn(msgs)
    mock_shared.collection.add.assert_not_called()


async def test_extract_and_store_fact(mock_shared):
    reply = _llm_reply(
        [
            {
                "fact": "User prefers dark mode for all applications",
                "category": "preference",
                "confidence": "high",
                "source_quote": "I always use dark mode",
            }
        ]
    )
    with patch("orchestrator.orchestrator.call_model", return_value=reply):
        msgs = [
            {"role": "user", "content": "I always use dark mode on everything, it's so much easier on my eyes"},
            {"role": "assistant", "content": "Dark mode is great for reducing eye strain!"},
        ]
        facts = await auto_learn.extract_facts(msgs)
    assert [f["fact"] for f in facts] == ["User prefers dark mode for all applications"]

    doc_id = await auto_learn.store_fact(facts[0])
    assert doc_id is not None
    assert doc_id.startswith("autolearn_")
    mock_shared.collection.add.assert_called_once()


async def test_sensitive_fact_filtered(mock_shared):
    reply = _llm_reply(
        [
            {
                "fact": "User's credit card is 4111 1111 1111 1111",
                "category": "financial",
                "confidence": "high",
                "source_quote": "my card is 4111...",
            },
            {
                "fact": "User likes Italian food",
                "category": "preference",
                "confidence": "high",
                "source_quote": "I love Italian",
            },
        ]
    )
    with patch("orchestrator.orchestrator.call_model", return_value=reply):
        msgs = [
            {"role": "user", "content": "My card is 4111 1111 1111 1111 and I love Italian food"},
            {"role": "assistant", "content": "Noted!"},
        ]
        facts = await auto_learn.extract_facts(msgs)
    assert [f["fact"] for f in facts] == ["User likes Italian food"]


@pytest.mark.parametrize(
    "existing_distance,duplicate",
    [
        pytest.param(0.05, True, id="cos-0.975"),
        pytest.param(0.5, False, id="cos-0.75"),
    ],
)
async def test_is_duplicate(mock_shared, existing_distance, duplicate):
    # ChromaDB returns squared L2 distance; cos = 1 - dist/2.
    mock_shared.collection.query.return_value = {
        "documents": [["User likes coffee"]],
        "distances": [[existing_distance]],
        "metadatas": [[{"source": "auto_learn"}]],
    }
    text = "User enjoys coffee every morning" if duplicate else "User has a pet cat named Whiskers"
    assert await auto_learn.is_duplicate(text) is duplicate


# ---------------------------------------------------------------------------
# Structured-domain gate — auto-learn must never shadow YAML-owned facts
# ---------------------------------------------------------------------------


async def test_store_fact_blocks_structured_category():
    """A health/medication fact must be refused by store_fact BEFORE any
    ChromaDB write — the guard against re-poisoning the meds store with a
    conversation-extracted (possibly hallucinated) claim."""
    assert "health" in shared.AUTO_LEARN_BLOCKED_CATEGORIES
    result = await auto_learn.store_fact(
        {"fact": "takes Naltrexone in the morning now", "category": "health", "confidence": "high"}
    )
    assert result is None


def test_blocked_categories_cover_structured_domains():
    for cat in ("health", "medication", "meds", "schedule", "routine"):
        assert cat in shared.AUTO_LEARN_BLOCKED_CATEGORIES
    # A normal conversational category must NOT be gated.
    assert "preference" not in shared.AUTO_LEARN_BLOCKED_CATEGORIES


# ---------------------------------------------------------------------------
# delete_learned_fact only ever deletes auto-learn documents
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc_id",
    [
        "rag_doc_123",
        "chunk_456",
        "manual_entry",
        "",
        pytest.param("autolearn", id="no-underscore"),
    ],
)
def test_delete_refuses_non_autolearn_ids_before_touching_chroma(monkeypatch, doc_id):
    col = MagicMock()
    monkeypatch.setattr(shared, "collection", col)
    assert auto_learn.delete_learned_fact(doc_id) is False
    col.get.assert_not_called()
    col.delete.assert_not_called()


def test_delete_autolearn_id_reaches_chroma(monkeypatch):
    col = MagicMock()
    col.get.return_value = {"metadatas": [{"source": "auto_learn"}]}
    monkeypatch.setattr(shared, "collection", col)
    assert auto_learn.delete_learned_fact("autolearn_20260314120000_abc123def456") is True
    col.delete.assert_called_once_with(ids=["autolearn_20260314120000_abc123def456"])
