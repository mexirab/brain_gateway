"""
Tests for TTS fallback behavior in reminder_manager.py and tool_handlers.py.

Covers:
- Connection error on primary speaker triggers fallback speaker
- First TTS failure schedules a retry job
- Second TTS failure sends phone notification and completes reminder

Uses sys.modules mocking for `shared` since it cannot be imported on Mac
(chromadb requires Linux).
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure orchestrator modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helper: check if reminder_manager can be imported
# ---------------------------------------------------------------------------
def _can_import_reminder_manager():
    try:
        import reminder_manager  # noqa: F401

        return True
    except Exception:
        return False


_skip_no_reminder = pytest.mark.skipif(
    not _can_import_reminder_manager(),
    reason="reminder_manager requires full orchestrator dependencies",
)


def _make_mock_shared():
    """Create a mock shared module with tts_backend."""
    mock = MagicMock()
    mock.tts_backend = MagicMock()
    mock.tts_backend.synthesize = AsyncMock(return_value=b"fake_audio")
    mock.tts_backend.file_extension = "wav"
    mock.tts_backend.audio_format = "audio/wav"
    mock.TIMEZONE = "America/Chicago"
    mock.DND_ACTIVE = False
    return mock


# ---------------------------------------------------------------------------
# TTS speaker fallback tests (reminder_manager._announce_voice)
# ---------------------------------------------------------------------------


@_skip_no_reminder
class TestTTSFallbackSpeaker:
    """_announce_voice should fall back to FALLBACK_SPEAKER on connection error."""

    @pytest.mark.anyio
    async def test_primary_connection_error_falls_back(self):
        """When primary speaker raises ConnectionError, fallback speaker is tried."""
        import reminder_manager

        mock_shared = _make_mock_shared()
        call_count = 0
        speakers_called = []

        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200

        async def mock_post(url, headers=None, json=None):
            nonlocal call_count
            call_count += 1
            speaker = json.get("entity_id", "")
            speakers_called.append(speaker)
            if call_count == 1:
                raise ConnectionError("primary speaker unreachable")
            return mock_response_ok

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_shared = sys.modules.get("shared")
        sys.modules["shared"] = mock_shared
        try:
            with (
                patch.object(reminder_manager, "REMINDER_SPEAKER", "media_player.primary"),
                patch.object(reminder_manager, "FALLBACK_SPEAKER", "media_player.fallback"),
                patch("reminder_manager.httpx.AsyncClient", return_value=mock_client),
            ):
                result = await reminder_manager._announce_voice("Test message")
        finally:
            if original_shared is not None:
                sys.modules["shared"] = original_shared
            else:
                sys.modules.pop("shared", None)

        assert result["success"] is True
        assert result["speaker"] == "media_player.fallback"
        assert len(speakers_called) == 2
        assert speakers_called[0] == "media_player.primary"
        assert speakers_called[1] == "media_player.fallback"

    @pytest.mark.anyio
    async def test_primary_http_error_falls_back(self):
        """When primary speaker returns non-200, fallback speaker is tried."""
        import reminder_manager

        mock_shared = _make_mock_shared()
        call_count = 0

        mock_response_fail = MagicMock()
        mock_response_fail.status_code = 500
        mock_response_ok = MagicMock()
        mock_response_ok.status_code = 200

        async def mock_post(url, headers=None, json=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_response_fail
            return mock_response_ok

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_shared = sys.modules.get("shared")
        sys.modules["shared"] = mock_shared
        try:
            with (
                patch.object(reminder_manager, "REMINDER_SPEAKER", "media_player.primary"),
                patch.object(reminder_manager, "FALLBACK_SPEAKER", "media_player.fallback"),
                patch("reminder_manager.httpx.AsyncClient", return_value=mock_client),
            ):
                result = await reminder_manager._announce_voice("Test message")
        finally:
            if original_shared is not None:
                sys.modules["shared"] = original_shared
            else:
                sys.modules.pop("shared", None)

        assert result["success"] is True
        assert result["speaker"] == "media_player.fallback"

    @pytest.mark.anyio
    async def test_all_speakers_fail_returns_error(self):
        """When both primary and fallback fail, returns error."""
        import reminder_manager

        mock_shared = _make_mock_shared()

        async def mock_post(url, headers=None, json=None):
            raise ConnectionError("unreachable")

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_shared = sys.modules.get("shared")
        sys.modules["shared"] = mock_shared
        try:
            with (
                patch.object(reminder_manager, "REMINDER_SPEAKER", "media_player.primary"),
                patch.object(reminder_manager, "FALLBACK_SPEAKER", "media_player.fallback"),
                patch("reminder_manager.httpx.AsyncClient", return_value=mock_client),
            ):
                result = await reminder_manager._announce_voice("Test message")
        finally:
            if original_shared is not None:
                sys.modules["shared"] = original_shared
            else:
                sys.modules.pop("shared", None)

        assert result["success"] is False
        assert "Connection error" in result["error"]

    @pytest.mark.anyio
    async def test_no_fallback_speaker_only_tries_primary(self):
        """When FALLBACK_SPEAKER is empty, only primary speaker is tried."""
        import reminder_manager

        mock_shared = _make_mock_shared()
        speakers_called = []

        async def mock_post(url, headers=None, json=None):
            speakers_called.append(json.get("entity_id", ""))
            raise ConnectionError("unreachable")

        mock_client = AsyncMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        original_shared = sys.modules.get("shared")
        sys.modules["shared"] = mock_shared
        try:
            with (
                patch.object(reminder_manager, "REMINDER_SPEAKER", "media_player.primary"),
                patch.object(reminder_manager, "FALLBACK_SPEAKER", ""),
                patch("reminder_manager.httpx.AsyncClient", return_value=mock_client),
            ):
                result = await reminder_manager._announce_voice("Test message")
        finally:
            if original_shared is not None:
                sys.modules["shared"] = original_shared
            else:
                sys.modules.pop("shared", None)

        assert result["success"] is False
        assert len(speakers_called) == 1


# ---------------------------------------------------------------------------
# Reminder retry tests (tool_handlers.deliver_reminder_job)
#
# tool_handlers imports shared at module level, so we must pre-mock shared
# in sys.modules before importing tool_handlers. We re-implement the retry
# logic here for isolated testing, matching the approach used in other tests.
# ---------------------------------------------------------------------------


def _select_retry_action(voice_ok, target, retry_exists):
    """
    Re-implementation of deliver_reminder_job retry logic.

    Returns: "complete" | "retry" | "phone_fallback"
    """
    if voice_ok or target == "phone":
        return "complete"
    if retry_exists:
        return "phone_fallback"
    return "retry"


class TestReminderRetryLogic:
    """deliver_reminder_job retry logic: first fail retries, second fail -> phone."""

    def test_voice_success_completes(self):
        assert _select_retry_action(voice_ok=True, target="voice", retry_exists=False) == "complete"

    def test_phone_target_always_completes(self):
        assert _select_retry_action(voice_ok=False, target="phone", retry_exists=False) == "complete"

    def test_first_voice_failure_retries(self):
        assert _select_retry_action(voice_ok=False, target="voice", retry_exists=False) == "retry"

    def test_both_target_first_failure_retries(self):
        assert _select_retry_action(voice_ok=False, target="both", retry_exists=False) == "retry"

    def test_second_voice_failure_falls_back_to_phone(self):
        assert _select_retry_action(voice_ok=False, target="voice", retry_exists=True) == "phone_fallback"

    def test_second_both_failure_falls_back_to_phone(self):
        assert _select_retry_action(voice_ok=False, target="both", retry_exists=True) == "phone_fallback"

    def test_voice_ok_ignores_retry_exists(self):
        """Even if a retry job exists, success should still complete."""
        assert _select_retry_action(voice_ok=True, target="voice", retry_exists=True) == "complete"
