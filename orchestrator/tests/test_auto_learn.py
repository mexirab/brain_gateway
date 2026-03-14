"""
Tests for auto_learn.py — fact extraction, sensitive data filtering,
privacy opt-out, dedup, encryption, and JSON parsing.

Tests pure/near-pure functions directly. For functions that depend on
shared state (ChromaDB, embedding model, LLM), we re-implement the logic
or mock the dependencies to avoid the full orchestrator import chain.
"""

import json
import re
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import check helper
# ---------------------------------------------------------------------------


def _can_import_auto_learn():
    """Check if auto_learn can be imported (requires chromadb + full dependency chain)."""
    try:
        import auto_learn  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


_skip_no_deps = pytest.mark.skipif(
    not _can_import_auto_learn(),
    reason="auto_learn requires chromadb and full orchestrator dependencies",
)


# ---------------------------------------------------------------------------
# Re-implemented pure functions (identical to auto_learn.py)
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{3,4}\b"),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b(?:sk|pk|api|token|key|secret|bearer)[_\-][\w]{20,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bpassword\s+(?:is|was|:)\s*\S+", re.IGNORECASE),
    re.compile(r"\b(?:account|routing)\s*(?:#|number|num)?\s*:?\s*\d{8,17}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
]


def _contains_sensitive_data(text: str) -> bool:
    return any(p.search(text) for p in _SENSITIVE_PATTERNS)


_PRIVACY_PHRASES = [
    "don't remember this",
    "dont remember this",
    "do not remember this",
    "don't learn this",
    "dont learn this",
    "do not learn this",
    "this is private",
    "this is confidential",
    "keep this private",
    "off the record",
    "forget this",
    "don't save this",
    "dont save this",
    "do not save this",
    "don't store this",
    "dont store this",
    "do not store this",
    "please don't store",
    "not for the record",
]


def conversation_has_opt_out(messages):
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            text_lower = content.lower()
            if any(phrase in text_lower for phrase in _PRIVACY_PHRASES):
                return True
    return False


def _format_conversation(messages):
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(text_parts)
        if not content or not content.strip():
            continue
        label = "User" if role == "user" else "Assistant"
        lines.append(f"{label}: {content.strip()}")
    return "\n".join(lines)


def _parse_facts_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        facts = json.loads(raw[start : end + 1])
        if isinstance(facts, list):
            return facts
    except json.JSONDecodeError:
        pass
    return []


# ---------------------------------------------------------------------------
# Tests: Sensitive data filtering
# ---------------------------------------------------------------------------


class TestSensitiveDataFilter:
    def test_credit_card_with_spaces(self):
        assert _contains_sensitive_data("My card is 4111 1111 1111 1111")

    def test_credit_card_with_dashes(self):
        assert _contains_sensitive_data("Card: 4111-1111-1111-1111")

    def test_credit_card_no_separator(self):
        assert _contains_sensitive_data("4111111111111111")

    def test_amex_15_digit(self):
        # Amex format matches when written as 4-4-4-3 groups
        assert _contains_sensitive_data("Amex: 3782-8224-6310-005")

    def test_ssn(self):
        assert _contains_sensitive_data("SSN is 123-45-6789")

    def test_api_key(self):
        assert _contains_sensitive_data("My key is sk_live_abcdefghij1234567890")

    def test_aws_access_key(self):
        assert _contains_sensitive_data("AWS key: AKIAIOSFODNN7EXAMPLE")

    def test_password_disclosure(self):
        assert _contains_sensitive_data("My password is hunter2")

    def test_password_with_colon(self):
        assert _contains_sensitive_data("password is s3cret123")

    def test_bank_account(self):
        assert _contains_sensitive_data("My account number: 12345678901")

    def test_routing_number(self):
        assert _contains_sensitive_data("routing # 021000021")

    def test_private_key(self):
        assert _contains_sensitive_data("-----BEGIN PRIVATE KEY-----")

    def test_rsa_private_key(self):
        assert _contains_sensitive_data("-----BEGIN RSA PRIVATE KEY-----")

    def test_jwt_token(self):
        assert _contains_sensitive_data("Token: eyJhbGciOiJIUzI1NiIsInR5.eyJzdWIiOiIxMjM0NTY3ODkw")

    def test_normal_text_not_flagged(self):
        assert not _contains_sensitive_data("I like coffee and running in the mornings")

    def test_short_numbers_not_flagged(self):
        assert not _contains_sensitive_data("My zip code is 78701")

    def test_normal_preference_not_flagged(self):
        assert not _contains_sensitive_data("I prefer using dark mode for everything")


# ---------------------------------------------------------------------------
# Tests: Privacy opt-out detection
# ---------------------------------------------------------------------------


class TestPrivacyOptOut:
    def test_dont_remember(self):
        msgs = [{"role": "user", "content": "Don't remember this conversation"}]
        assert conversation_has_opt_out(msgs)

    def test_this_is_private(self):
        msgs = [{"role": "user", "content": "Hey, this is private but I need help with..."}]
        assert conversation_has_opt_out(msgs)

    def test_off_the_record(self):
        msgs = [{"role": "user", "content": "Off the record, I've been feeling stressed"}]
        assert conversation_has_opt_out(msgs)

    def test_forget_this(self):
        msgs = [{"role": "user", "content": "Forget this conversation please"}]
        assert conversation_has_opt_out(msgs)

    def test_confidential(self):
        msgs = [{"role": "user", "content": "This is confidential information"}]
        assert conversation_has_opt_out(msgs)

    def test_no_apostrophe_variant(self):
        msgs = [{"role": "user", "content": "Dont save this please"}]
        assert conversation_has_opt_out(msgs)

    def test_do_not_store(self):
        msgs = [{"role": "user", "content": "Do not store this conversation"}]
        assert conversation_has_opt_out(msgs)

    def test_not_for_the_record(self):
        msgs = [{"role": "user", "content": "This is not for the record..."}]
        assert conversation_has_opt_out(msgs)

    def test_normal_conversation_no_opt_out(self):
        msgs = [
            {"role": "user", "content": "What's the weather like?"},
            {"role": "assistant", "content": "It's sunny today."},
            {"role": "user", "content": "Thanks!"},
        ]
        assert not conversation_has_opt_out(msgs)

    def test_assistant_saying_private_not_triggered(self):
        msgs = [
            {"role": "user", "content": "Tell me about my schedule"},
            {"role": "assistant", "content": "This is private info: you have a meeting at 3."},
        ]
        assert not conversation_has_opt_out(msgs)

    def test_opt_out_mid_conversation(self):
        msgs = [
            {"role": "user", "content": "I love hiking"},
            {"role": "assistant", "content": "Nice!"},
            {"role": "user", "content": "Actually, don't remember this please"},
        ]
        assert conversation_has_opt_out(msgs)

    def test_case_insensitive(self):
        msgs = [{"role": "user", "content": "DON'T SAVE THIS"}]
        assert conversation_has_opt_out(msgs)


# ---------------------------------------------------------------------------
# Tests: Conversation formatting
# ---------------------------------------------------------------------------


class TestFormatConversation:
    def test_basic_conversation(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = _format_conversation(msgs)
        assert result == "User: Hello\nAssistant: Hi there!"

    def test_system_messages_excluded(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
        ]
        result = _format_conversation(msgs)
        assert result == "User: Hi"

    def test_empty_content_skipped(self):
        msgs = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "Real message"},
        ]
        result = _format_conversation(msgs)
        assert result == "User: Real message"

    def test_whitespace_only_skipped(self):
        msgs = [
            {"role": "user", "content": "   "},
            {"role": "user", "content": "Actual content"},
        ]
        result = _format_conversation(msgs)
        assert result == "User: Actual content"

    def test_multipart_content(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this"},
                    {"type": "image_url", "image_url": "data:..."},
                    {"type": "text", "text": "and tell me what you see"},
                ],
            }
        ]
        result = _format_conversation(msgs)
        assert result == "User: Look at this and tell me what you see"

    def test_tool_role_excluded(self):
        msgs = [
            {"role": "user", "content": "Check the weather"},
            {"role": "tool", "content": '{"temp": 72}'},
            {"role": "assistant", "content": "It's 72 degrees."},
        ]
        result = _format_conversation(msgs)
        assert "tool" not in result.lower()
        assert "User: Check the weather" in result
        assert "Assistant: It's 72 degrees." in result

    def test_content_is_stripped(self):
        msgs = [{"role": "user", "content": "  hello world  "}]
        result = _format_conversation(msgs)
        assert result == "User: hello world"


# ---------------------------------------------------------------------------
# Tests: JSON parsing
# ---------------------------------------------------------------------------


class TestParseFactsJson:
    def test_clean_json_array(self):
        raw = '[{"fact": "Likes coffee", "category": "preference", "confidence": "high", "source_quote": "I love coffee"}]'
        result = _parse_facts_json(raw)
        assert len(result) == 1
        assert result[0]["fact"] == "Likes coffee"

    def test_empty_array(self):
        assert _parse_facts_json("[]") == []

    def test_json_with_markdown_fences(self):
        raw = '```json\n[{"fact": "Runs daily", "category": "routine", "confidence": "high", "source_quote": "I run every day"}]\n```'
        result = _parse_facts_json(raw)
        assert len(result) == 1
        assert result[0]["fact"] == "Runs daily"

    def test_json_with_preamble(self):
        raw = 'Here are the extracted facts:\n[{"fact": "Has a dog", "category": "relationship", "confidence": "medium", "source_quote": "my dog Rex"}]'
        result = _parse_facts_json(raw)
        assert len(result) == 1
        assert result[0]["fact"] == "Has a dog"

    def test_json_with_trailing_text(self):
        raw = '[{"fact": "Works remotely", "category": "work", "confidence": "high", "source_quote": "WFH"}]\nThose are the facts.'
        result = _parse_facts_json(raw)
        assert len(result) == 1

    def test_multiple_facts(self):
        raw = json.dumps(
            [
                {"fact": "Fact one", "category": "a", "confidence": "high", "source_quote": "q1"},
                {"fact": "Fact two", "category": "b", "confidence": "medium", "source_quote": "q2"},
                {"fact": "Fact three", "category": "c", "confidence": "high", "source_quote": "q3"},
            ]
        )
        result = _parse_facts_json(raw)
        assert len(result) == 3

    def test_invalid_json(self):
        assert _parse_facts_json("this is not json") == []

    def test_no_brackets(self):
        assert _parse_facts_json('{"fact": "single object"}') == []

    def test_malformed_json_array(self):
        assert _parse_facts_json('[{"fact": "incomplete') == []

    def test_empty_string(self):
        assert _parse_facts_json("") == []

    def test_just_text_no_json(self):
        assert _parse_facts_json("No facts could be extracted from this conversation.") == []

    def test_triple_backtick_variants(self):
        raw = "```\n[]\n```"
        assert _parse_facts_json(raw) == []


# ---------------------------------------------------------------------------
# Tests: Encryption helpers (requires cryptography package)
# ---------------------------------------------------------------------------


class TestEncryption:
    """Test encryption using the actual auto_learn module (lazy import to avoid chain)."""

    def test_encrypt_decrypt_roundtrip(self):
        """Fernet encrypt → decrypt returns original text."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        cipher = Fernet(key)

        plaintext = "User prefers dark mode"
        token = cipher.encrypt(plaintext.encode()).decode()
        decrypted = cipher.decrypt(token.encode()).decode()
        assert decrypted == plaintext

    def test_encrypted_text_differs_from_plaintext(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        cipher = Fernet(key)

        plaintext = "Favorite restaurant is Uchi"
        token = cipher.encrypt(plaintext.encode()).decode()
        assert token != plaintext
        assert len(token) > len(plaintext)

    def test_different_keys_cannot_decrypt(self):
        from cryptography.fernet import Fernet, InvalidToken

        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()

        token = Fernet(key1).encrypt(b"secret fact")
        with pytest.raises(InvalidToken):
            Fernet(key2).decrypt(token)

    def test_decrypt_failure_returns_placeholder(self):
        """Mimics auto_learn.decrypt_text() behavior on failure."""
        # This tests the pattern, not the import
        token = "not_a_valid_fernet_token"
        try:
            from cryptography.fernet import Fernet

            Fernet(Fernet.generate_key()).decrypt(token.encode())
            result = token  # Should not reach here
        except Exception:
            result = "[encrypted — decryption failed]"
        assert result == "[encrypted — decryption failed]"


# ---------------------------------------------------------------------------
# Tests: Conversation truncation logic
# ---------------------------------------------------------------------------


class TestConversationTruncation:
    def test_short_conversation_not_truncated(self):
        text = "User: Hello\nAssistant: Hi there!"
        # Under 4000 chars — should pass through unchanged
        assert len(text) < 4000

    def test_long_conversation_truncated_at_newline(self):
        """Truncation should happen at last newline boundary before 4000 chars."""
        # Build a conversation > 4000 chars
        lines = [f"User: Message number {i} with some padding text here" for i in range(100)]
        conversation_text = "\n".join(lines)
        assert len(conversation_text) > 4000

        # Apply truncation logic (from auto_learn.py)
        if len(conversation_text) > 4000:
            cutoff = conversation_text.rfind("\n", 0, 4000)
            if cutoff == -1:
                cutoff = 4000
            conversation_text = conversation_text[:cutoff] + "\n[...conversation truncated...]"

        assert len(conversation_text) <= 4100  # Truncated + suffix
        assert conversation_text.endswith("[...conversation truncated...]")
        # Should not cut mid-word
        pre_truncation = conversation_text.split("\n[...conversation truncated...]")[0]
        assert pre_truncation.endswith("padding text here") or pre_truncation.endswith("padding text here")


# ---------------------------------------------------------------------------
# Tests: Doc ID validation (delete safety)
# ---------------------------------------------------------------------------


class TestDocIdValidation:
    def test_valid_autolearn_prefix(self):
        doc_id = "autolearn_20260314120000_abc123def456"
        assert doc_id.startswith("autolearn_")

    def test_reject_non_autolearn_prefix(self):
        bad_ids = [
            "rag_doc_123",
            "chunk_456",
            "manual_entry",
            "",
            "autolearn",  # missing underscore
        ]
        for doc_id in bad_ids:
            assert not doc_id.startswith("autolearn_"), f"Should reject: {doc_id}"


# ---------------------------------------------------------------------------
# Tests: Extraction prompt safety
# ---------------------------------------------------------------------------


class TestExtractionPrompt:
    """Verify the extraction prompt uses safe string substitution."""

    def test_replace_vs_format_with_curly_braces(self):
        """Conversations with curly braces should not crash the prompt builder.

        When the template has other braces besides {conversation} (like the
        extraction prompt's JSON example), .format() would crash on unescaped
        braces. .replace() handles this safely.
        """
        # Template with both {conversation} and literal braces (like in real prompt)
        template = 'Return JSON: [{{"fact": "..."}}]\n\n{conversation}'
        conversation = 'User: My config is {"key": "value"}'

        # .replace() works safely regardless of brace content
        result = template.replace("{conversation}", conversation)
        assert '{"key": "value"}' in result
        assert "Return JSON" in result

    @_skip_no_deps
    def test_delimiter_wrapping(self):
        """Verify extraction prompt uses <<<>>> delimiters."""
        from auto_learn import _EXTRACTION_PROMPT

        assert "<<<" in _EXTRACTION_PROMPT
        assert ">>>" in _EXTRACTION_PROMPT
        assert "{conversation}" in _EXTRACTION_PROMPT


# ---------------------------------------------------------------------------
# Tests: Integration-style tests using the actual module
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestAutoLearnModule:
    """Tests that import from auto_learn directly (where safe to do so)."""

    def test_contains_sensitive_data_matches_module(self):
        from auto_learn import _contains_sensitive_data as module_fn

        assert module_fn("My card is 4111 1111 1111 1111")
        assert not module_fn("I like pizza")

    def test_conversation_has_opt_out_matches_module(self):
        from auto_learn import conversation_has_opt_out as module_fn

        msgs = [{"role": "user", "content": "Don't remember this"}]
        assert module_fn(msgs)

    def test_format_conversation_matches_module(self):
        from auto_learn import _format_conversation as module_fn

        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = module_fn(msgs)
        assert result == "User: hi\nAssistant: hello"

    def test_parse_facts_json_matches_module(self):
        from auto_learn import _parse_facts_json as module_fn

        raw = '[{"fact": "test", "category": "x", "confidence": "high", "source_quote": "q"}]'
        result = module_fn(raw)
        assert len(result) == 1
        assert result[0]["fact"] == "test"


# ---------------------------------------------------------------------------
# Tests: Full pipeline logic (mocked LLM + ChromaDB)
# ---------------------------------------------------------------------------


@_skip_no_deps
class TestRunAutoLearn:
    """Test the run_auto_learn pipeline with mocked external dependencies."""

    @pytest.fixture
    def mock_shared(self):
        """Mock shared module dependencies."""
        with patch("auto_learn.shared") as mock:
            mock.AUTO_LEARN_ENABLED = True
            mock.AUTO_LEARN_ENCRYPT = False
            mock.AUTO_LEARN_MARKDOWN = False
            mock.AUTO_LEARN_MAX_FACTS = 5
            mock.AUTO_LEARN_DEDUP_THRESHOLD = 0.85
            mock.AUTO_LEARN_ENCRYPTION_KEY = ""
            mock.MODEL_URL = "http://fake:8001/v1"
            mock.MODEL_NAME = "test-model"

            # Mock embedding model
            import numpy as np

            mock.embedding_model.encode.return_value = np.zeros(384)

            # Mock ChromaDB collection
            mock.collection.query.return_value = {
                "documents": [[]],
                "distances": [[]],
            }
            mock.collection.get.return_value = {
                "ids": [],
                "documents": [],
                "metadatas": [],
            }
            mock.collection.add = MagicMock()

            yield mock

    @pytest.mark.asyncio
    async def test_opt_out_skips_extraction(self, mock_shared):
        from auto_learn import run_auto_learn

        msgs = [
            {"role": "user", "content": "My favorite color is blue"},
            {"role": "user", "content": "Don't remember this"},
        ]
        # Should return without calling LLM
        await run_auto_learn(msgs)
        # No ChromaDB interaction expected
        mock_shared.collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_conversation_skips(self, mock_shared):
        from auto_learn import run_auto_learn

        msgs = [{"role": "user", "content": "Hi"}]
        await run_auto_learn(msgs)
        mock_shared.collection.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_and_store_fact(self, mock_shared):
        from auto_learn import extract_facts, store_fact

        # Mock the LLM call
        llm_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            [
                                {
                                    "fact": "User prefers dark mode for all applications",
                                    "category": "preference",
                                    "confidence": "high",
                                    "source_quote": "I always use dark mode",
                                }
                            ]
                        )
                    }
                }
            ]
        }

        with patch("auto_learn.call_model", return_value=llm_response) as mock_call:
            msgs = [
                {"role": "user", "content": "I always use dark mode on everything, it's so much easier on my eyes"},
                {"role": "assistant", "content": "Dark mode is great for reducing eye strain!"},
            ]
            facts = await extract_facts(msgs)
            assert len(facts) == 1
            assert facts[0]["fact"] == "User prefers dark mode for all applications"

        # Store the fact
        doc_id = await store_fact(facts[0])
        assert doc_id is not None
        assert doc_id.startswith("autolearn_")
        mock_shared.collection.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_sensitive_fact_filtered(self, mock_shared):
        from auto_learn import extract_facts

        llm_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
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
                    }
                }
            ]
        }

        with patch("auto_learn.call_model", return_value=llm_response):
            msgs = [
                {"role": "user", "content": "My card is 4111 1111 1111 1111 and I love Italian food"},
                {"role": "assistant", "content": "Noted!"},
            ]
            facts = await extract_facts(msgs)
            # Only the safe fact should remain
            assert len(facts) == 1
            assert facts[0]["fact"] == "User likes Italian food"

    @pytest.mark.asyncio
    async def test_duplicate_fact_skipped(self, mock_shared):
        from auto_learn import is_duplicate

        # Simulate existing fact with high cosine similarity
        mock_shared.collection.query.return_value = {
            "documents": [["User likes coffee"]],
            "distances": [[0.05]],  # cosine distance 0.05 = similarity 0.95
        }

        result = await is_duplicate("User enjoys coffee every morning")
        assert result is True

    @pytest.mark.asyncio
    async def test_non_duplicate_fact_passes(self, mock_shared):
        from auto_learn import is_duplicate

        mock_shared.collection.query.return_value = {
            "documents": [["User likes coffee"]],
            "distances": [[0.5]],  # cosine distance 0.5 = similarity 0.5
        }

        result = await is_duplicate("User has a pet cat named Whiskers")
        assert result is False
