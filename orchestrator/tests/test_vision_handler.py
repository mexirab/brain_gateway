"""
Unit tests for vision_handler.py — image analysis routing to dedicated vision model.
"""

import base64

# ---------------------------------------------------------------------------
# Mock shared module before importing vision_handler
# ---------------------------------------------------------------------------
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

mock_shared = MagicMock()
mock_shared.VISION_ENABLED = True
mock_shared.VISION_MODEL_URL = "http://10.0.0.58:8010/v1"
mock_shared.VISION_MODEL_NAME = "Qwen3-VL-8B-Instruct"
mock_shared.VISION_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
mock_shared.VISION_TIMEOUT = 60
mock_shared._http = None
mock_shared._vision_image_cache = {}
sys.modules["shared"] = mock_shared

mock_metrics = MagicMock()
sys.modules["metrics"] = mock_metrics

# Now import the module under test
from vision_handler import (
    _estimate_image_size,
    _parse_data_uri,
    analyze_image,
    check_vision_health,
    extract_images_from_messages,
)

# ---------------------------------------------------------------------------
# _parse_data_uri
# ---------------------------------------------------------------------------


class TestParseDataUri:
    def test_full_data_uri_jpeg(self):
        mime, data = _parse_data_uri("data:image/jpeg;base64,/9j/4AAQ")
        assert mime == "image/jpeg"
        assert data == "/9j/4AAQ"

    def test_full_data_uri_png(self):
        mime, data = _parse_data_uri("data:image/png;base64,iVBORw0K")
        assert mime == "image/png"
        assert data == "iVBORw0K"

    def test_raw_base64_assumes_jpeg(self):
        mime, data = _parse_data_uri("/9j/4AAQSkZJRg==")
        assert mime == "image/jpeg"
        assert data == "/9j/4AAQSkZJRg=="

    def test_webp_data_uri(self):
        mime, data = _parse_data_uri("data:image/webp;base64,UklGR")
        assert mime == "image/webp"
        assert data == "UklGR"


# ---------------------------------------------------------------------------
# _estimate_image_size
# ---------------------------------------------------------------------------


class TestEstimateImageSize:
    def test_small_image(self):
        b64 = base64.b64encode(b"x" * 100).decode()
        size = _estimate_image_size(b64)
        # Should be approximately 100 bytes
        assert 90 <= size <= 110

    def test_empty_string(self):
        assert _estimate_image_size("") == 0


# ---------------------------------------------------------------------------
# extract_images_from_messages
# ---------------------------------------------------------------------------


class TestExtractImages:
    def test_no_images(self):
        messages = [{"role": "user", "content": "Hello"}]
        assert extract_images_from_messages(messages) == []

    def test_single_image_with_text(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What's in my pantry?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,/9j/test"},
                    },
                ],
            }
        ]
        images = extract_images_from_messages(messages)
        assert len(images) == 1
        assert images[0]["image_url"] == "data:image/jpeg;base64,/9j/test"
        assert images[0]["text"] == "What's in my pantry?"
        assert images[0]["msg_index"] == 0

    def test_multiple_images_in_one_message(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare these"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,img1"}},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,img2"}},
                ],
            }
        ]
        images = extract_images_from_messages(messages)
        assert len(images) == 2
        assert images[0]["text"] == "Compare these"
        assert images[1]["text"] == "Compare these"

    def test_image_url_as_plain_string(self):
        """Some clients send image_url as a string, not a dict."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look at this"},
                    {"type": "image_url", "image_url": "data:image/jpeg;base64,raw_string"},
                ],
            }
        ]
        images = extract_images_from_messages(messages)
        assert len(images) == 1
        assert images[0]["image_url"] == "data:image/jpeg;base64,raw_string"

    def test_assistant_messages_ignored(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,fake"}},
                ],
            }
        ]
        assert extract_images_from_messages(messages) == []

    def test_string_content_ignored(self):
        messages = [{"role": "user", "content": "Just text, no images"}]
        assert extract_images_from_messages(messages) == []

    def test_image_without_text(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/"}},
                ],
            }
        ]
        images = extract_images_from_messages(messages)
        assert len(images) == 1
        assert images[0]["text"] == ""


# ---------------------------------------------------------------------------
# analyze_image
# ---------------------------------------------------------------------------


class TestAnalyzeImage:
    @pytest.mark.asyncio
    async def test_vision_disabled(self):
        mock_shared.VISION_ENABLED = False
        result = await analyze_image("data:image/jpeg;base64,test", "describe")
        assert "disabled" in result.lower()
        mock_shared.VISION_ENABLED = True

    @pytest.mark.asyncio
    async def test_unsupported_mime_type(self):
        result = await analyze_image(
            "data:application/pdf;base64,test",
            "describe",
            http_client=AsyncMock(),
        )
        assert "Unsupported" in result

    @pytest.mark.asyncio
    async def test_image_too_large(self):
        mock_shared.VISION_MAX_IMAGE_SIZE = 100  # 100 bytes
        # Create a base64 string that decodes to >100 bytes
        large_b64 = base64.b64encode(b"x" * 200).decode()
        result = await analyze_image(
            f"data:image/jpeg;base64,{large_b64}",
            "describe",
            http_client=AsyncMock(),
        )
        assert "too large" in result.lower()
        mock_shared.VISION_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # restore

    @pytest.mark.asyncio
    async def test_successful_analysis(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "I see a pantry with canned goods and pasta."}}]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await analyze_image(
            "data:image/jpeg;base64,/9j/4AAQ",
            "What food do you see?",
            http_client=mock_client,
        )
        assert "pantry" in result
        assert "pasta" in result

        # Verify the API was called with correct structure
        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["model"] == "Qwen3-VL-8B-Instruct"
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        # Should have text + image_url parts
        content = payload["messages"][0]["content"]
        assert any(p["type"] == "text" for p in content)
        assert any(p["type"] == "image_url" for p in content)

    @pytest.mark.asyncio
    async def test_timeout_handling(self):
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))

        result = await analyze_image(
            "data:image/jpeg;base64,/9j/4AAQ",
            "describe",
            http_client=mock_client,
        )
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_http_error_handling(self):
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_response
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        result = await analyze_image(
            "data:image/jpeg;base64,/9j/4AAQ",
            "describe",
            http_client=mock_client,
        )
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_no_http_client(self):
        mock_shared._http = None
        result = await analyze_image("data:image/jpeg;base64,test", "describe")
        assert "unavailable" in result.lower()


# ---------------------------------------------------------------------------
# check_vision_health
# ---------------------------------------------------------------------------


class TestCheckVisionHealth:
    @pytest.mark.asyncio
    async def test_disabled(self):
        mock_shared.VISION_ENABLED = False
        assert await check_vision_health() is False
        mock_shared.VISION_ENABLED = True

    @pytest.mark.asyncio
    async def test_healthy(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        assert await check_vision_health(http_client=mock_client) is True

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        assert await check_vision_health(http_client=mock_client) is False
