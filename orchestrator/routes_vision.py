"""Vision, STT, and TTS API routes."""

import logging
import os

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from orchestrator import shared

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Vision / Image Recognition
# ---------------------------------------------------------------------------


@router.post("/api/vision/analyze")
async def vision_analyze(request: Request):
    """Analyze an uploaded image using the vision model.

    Accepts multipart form data with:
    - image: Image file (JPEG, PNG, WebP, GIF)
    - prompt: Optional text prompt (default: describe the image)
    """
    import base64

    from orchestrator.vision_handler import SUPPORTED_MIME_TYPES, analyze_image

    if not shared.VISION_ENABLED:
        return JSONResponse({"ok": False, "error": "Vision is disabled"}, status_code=503)

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        # Check Content-Length header before reading body to reject oversized uploads early
        content_length = int(request.headers.get("content-length", "0") or "0")
        if content_length > shared.VISION_MAX_IMAGE_SIZE:
            max_mb = shared.VISION_MAX_IMAGE_SIZE / (1024 * 1024)
            return JSONResponse(
                {"ok": False, "error": f"Upload too large. Maximum: {max_mb:.0f}MB"},
                status_code=413,
            )

        form = await request.form()
        image_file = form.get("image")
        prompt = form.get("prompt", "Describe this image in detail, including any text visible.")

        if not image_file:
            return JSONResponse({"ok": False, "error": "No image file provided"}, status_code=400)

        # Read and validate size
        image_bytes = await image_file.read()
        if len(image_bytes) > shared.VISION_MAX_IMAGE_SIZE:
            max_mb = shared.VISION_MAX_IMAGE_SIZE / (1024 * 1024)
            return JSONResponse(
                {"ok": False, "error": f"Image too large. Maximum: {max_mb:.0f}MB"},
                status_code=413,
            )

        # Detect MIME type from filename or content-type
        file_ct = getattr(image_file, "content_type", "") or ""
        if file_ct not in SUPPORTED_MIME_TYPES:
            filename = getattr(image_file, "filename", "") or ""
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            from orchestrator.vision_handler import _EXT_TO_MIME

            file_ct = _EXT_TO_MIME.get(ext, "")
            if not file_ct:
                return JSONResponse(
                    {"ok": False, "error": "Unsupported image type. Supported: JPEG, PNG, WebP, GIF."},
                    status_code=400,
                )

        b64_data = base64.b64encode(image_bytes).decode("ascii")
        image_data = f"data:{file_ct};base64,{b64_data}"
        logger.info("[VISION_API] Multipart upload: %s, %d bytes", file_ct, len(image_bytes))

    elif "application/json" in content_type:
        body = await request.json()
        image_data = body.get("image", "")
        prompt = body.get("prompt", "Describe this image in detail, including any text visible.")
        if not image_data:
            return JSONResponse({"ok": False, "error": "No image data provided"}, status_code=400)
        # SSRF prevention: only accept data URIs or raw base64, not http:// URLs
        from orchestrator.vision_handler import _is_safe_image_url

        if not _is_safe_image_url(image_data):
            return JSONResponse(
                {"ok": False, "error": "Only base64 image data or data: URIs accepted (not URLs)"},
                status_code=400,
            )
        # Size guard: ~1.33x base64 overhead; check before expensive processing
        max_b64_len = int(shared.VISION_MAX_IMAGE_SIZE * 1.4)
        if len(image_data) > max_b64_len:
            max_mb = shared.VISION_MAX_IMAGE_SIZE / (1024 * 1024)
            return JSONResponse(
                {"ok": False, "error": f"Image data too large. Maximum: {max_mb:.0f}MB"},
                status_code=413,
            )
        logger.info("[VISION_API] JSON request, image data %d chars", len(image_data))
    else:
        return JSONResponse(
            {"ok": False, "error": "Send multipart/form-data with 'image' file, or JSON with 'image' (base64)"},
            status_code=400,
        )

    result = await analyze_image(image_data, prompt)
    logger.info("[VISION_API] Analysis complete, %d chars result", len(result))
    return JSONResponse({"ok": True, "analysis": result})


@router.get("/api/vision/status")
async def vision_status():
    """Check vision model availability."""
    from orchestrator.vision_handler import check_vision_health

    healthy = await check_vision_health()
    return JSONResponse(
        {
            "ok": True,
            "enabled": shared.VISION_ENABLED,
            "healthy": healthy,
            "model": shared.VISION_MODEL_NAME,
        }
    )


# ---------------------------------------------------------------------------
# Voice: STT + TTS proxy endpoints for chat page
# ---------------------------------------------------------------------------

STT_URL = os.environ.get("STT_URL", "")

MAX_AUDIO_UPLOAD = 10 * 1024 * 1024  # 10 MB


@router.post("/api/stt/transcribe")
async def stt_transcribe(file: UploadFile = File(...)):
    """Proxy audio to STT service for transcription."""
    audio_data = await file.read()
    if len(audio_data) > MAX_AUDIO_UPLOAD:
        return JSONResponse({"error": "Audio file too large (max 10 MB)"}, status_code=413)
    r = await shared._http.post(
        f"{STT_URL}/v1/audio/transcriptions",
        files={"file": (file.filename or "audio.webm", audio_data, file.content_type or "audio/webm")},
        data={"model": "whisper-1"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


@router.post("/api/tts/synthesize")
async def tts_synthesize(request: Request):
    """Synthesize text to speech and return WAV audio."""
    body = await request.json()
    text = body.get("text", "")
    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)
    backend = shared.tts_backend
    if not backend:
        return JSONResponse({"error": "TTS not available"}, status_code=503)
    audio_bytes = await backend.synthesize(text)
    return Response(content=audio_bytes, media_type="audio/wav")
