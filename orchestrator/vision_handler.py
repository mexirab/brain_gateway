"""
Vision handler for Brain Gateway.

Routes images to a dedicated vision model (Qwen3-VL-8B on Saturn) for analysis.
Returns text descriptions that feed back into the main conversation loop on Helios.
"""

import logging
import time
from typing import Optional

import httpx

import shared
from metrics import VISION_IMAGE_SIZE, VISION_REQUEST_COUNT, VISION_REQUEST_LATENCY

logger = logging.getLogger(__name__)

# Supported image MIME types
SUPPORTED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# Map file extensions to MIME types for base64 data without explicit MIME
_EXT_TO_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _parse_data_uri(data_uri: str) -> tuple[str, str]:
    """Parse a data URI into (mime_type, base64_data).

    Handles formats:
    - data:image/jpeg;base64,/9j/4AAQ...
    - /9j/4AAQ... (raw base64, assumes JPEG)
    """
    if data_uri.startswith("data:"):
        # data:image/jpeg;base64,/9j/4AAQ...
        header, _, b64_data = data_uri.partition(",")
        mime = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
        return mime, b64_data
    # Raw base64 — assume JPEG
    return "image/jpeg", data_uri


def _estimate_image_size(b64_data: str) -> int:
    """Estimate decoded image size from base64 string length."""
    return len(b64_data) * 3 // 4


MAX_IMAGES_PER_REQUEST = 3


def _is_safe_image_url(url: str) -> bool:
    """Validate that an image URL is a data URI or raw base64, not an HTTP URL (SSRF prevention)."""
    if url.startswith("data:"):
        return True
    # Raw base64 — no protocol prefix — is safe
    return not url.startswith(("http://", "https://", "ftp://", "file://"))


def extract_images_from_messages(messages: list[dict]) -> list[dict]:
    """Extract image_url content parts from OpenAI-format messages.

    Returns list of dicts: [{"image_url": "data:...", "text": "user prompt", "msg_index": 0}]
    Only accepts data URIs and raw base64 — rejects http:// URLs to prevent SSRF.
    Caps at MAX_IMAGES_PER_REQUEST images to prevent resource exhaustion.
    """
    images = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url_data = part.get("image_url", {})
                    url = url_data.get("url", "") if isinstance(url_data, dict) else url_data
                    if url and _is_safe_image_url(url):
                        images.append(
                            {
                                "image_url": url,
                                "text": "",  # filled after collecting all text parts
                                "msg_index": i,
                            }
                        )
                    elif url:
                        logger.warning("[VISION] Rejected non-data-URI image URL (SSRF prevention)")
        # Attach collected text to all images from this message
        combined_text = " ".join(text_parts).strip()
        for img in images:
            if img["msg_index"] == i and not img["text"]:
                img["text"] = combined_text
    if len(images) > MAX_IMAGES_PER_REQUEST:
        logger.warning("[VISION] Capping images from %d to %d", len(images), MAX_IMAGES_PER_REQUEST)
        images = images[:MAX_IMAGES_PER_REQUEST]
    return images


async def analyze_image(
    image_data: str,
    prompt: str = "Describe this image in detail.",
    http_client: Optional[httpx.AsyncClient] = None,
) -> str:
    """Send an image to the vision model for analysis.

    Args:
        image_data: Base64 image data or data URI (data:image/jpeg;base64,...).
        prompt: Text prompt to guide the analysis.
        http_client: Optional httpx client (falls back to shared._http).

    Returns:
        Text description/analysis from the vision model.
    """
    if not shared.VISION_ENABLED:
        VISION_REQUEST_COUNT.labels(status="disabled").inc()
        return "[Vision is disabled. Enable VISION_ENABLED=true to analyze images.]"

    client = http_client or shared._http
    if not client:
        logger.error("[VISION] No HTTP client available")
        VISION_REQUEST_COUNT.labels(status="error").inc()
        return "[Vision unavailable: no HTTP client.]"

    # Parse and validate image
    mime_type, b64_data = _parse_data_uri(image_data)
    if mime_type not in SUPPORTED_MIME_TYPES:
        logger.warning("[VISION] Unsupported MIME type: %s", mime_type)
        VISION_REQUEST_COUNT.labels(status="rejected_mime").inc()
        return f"[Unsupported image type: {mime_type}. Supported: JPEG, PNG, WebP, GIF.]"

    img_size = _estimate_image_size(b64_data)
    if img_size == 0:
        VISION_REQUEST_COUNT.labels(status="rejected_empty").inc()
        return "[No image data provided. The image appears to be empty.]"

    # Validate base64 encoding (catch corrupt/truncated data early)
    import base64 as _b64

    try:
        _b64.b64decode(b64_data, validate=True)
    except Exception:
        VISION_REQUEST_COUNT.labels(status="rejected_corrupt").inc()
        return "[Invalid image data. The image appears to be corrupted or not properly encoded.]"

    VISION_IMAGE_SIZE.observe(img_size)

    if img_size > shared.VISION_MAX_IMAGE_SIZE:
        max_mb = shared.VISION_MAX_IMAGE_SIZE / (1024 * 1024)
        logger.warning("[VISION] Image too large: %d bytes (max %d)", img_size, shared.VISION_MAX_IMAGE_SIZE)
        VISION_REQUEST_COUNT.labels(status="rejected_size").inc()
        return f"[Image too large ({img_size / (1024 * 1024):.1f}MB). Maximum: {max_mb:.0f}MB.]"

    # Build OpenAI-compatible vision request
    data_uri = f"data:{mime_type};base64,{b64_data}"
    payload = {
        "model": shared.VISION_MODEL_NAME,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_uri},
                    },
                ],
            }
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }

    t0 = time.time()
    try:
        resp = await client.post(
            f"{shared.VISION_MODEL_URL}/chat/completions",
            json=payload,
            timeout=shared.VISION_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        elapsed = time.time() - t0

        VISION_REQUEST_COUNT.labels(status="success").inc()
        VISION_REQUEST_LATENCY.observe(elapsed)

        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.info("[VISION] Analysis complete in %.1fs (%d chars)", elapsed, len(content))
        return content

    except httpx.TimeoutException:
        elapsed = time.time() - t0
        VISION_REQUEST_COUNT.labels(status="error").inc()
        VISION_REQUEST_LATENCY.observe(elapsed)
        logger.error("[VISION] Timeout after %.1fs", elapsed)
        return "[Vision model timed out. The image may be too complex or the model may be busy.]"

    except httpx.HTTPStatusError as e:
        VISION_REQUEST_COUNT.labels(status="error").inc()
        logger.error("[VISION] HTTP error %d: %s", e.response.status_code, (e.response.text or "")[:200])
        return "[Vision model returned an error. Please try again.]"

    except Exception as e:
        VISION_REQUEST_COUNT.labels(status="error").inc()
        logger.error("[VISION] Unexpected error: %s", e, exc_info=True)
        return "[Vision analysis failed. Please try again.]"


async def check_vision_health(http_client: Optional[httpx.AsyncClient] = None) -> bool:
    """Check if the vision model is reachable."""
    if not shared.VISION_ENABLED:
        return False
    client = http_client or shared._http
    if not client:
        return False
    try:
        resp = await client.get(f"{shared.VISION_MODEL_URL}/models", timeout=5)
        return resp.status_code == 200
    except Exception as e:
        logger.debug("[VISION] Health check failed: %s", e)
        return False
