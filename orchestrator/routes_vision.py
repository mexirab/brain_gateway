"""Vision, STT, and TTS API routes."""

import io
import logging
import os
import time
import wave

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from orchestrator import shared
from orchestrator.metrics import VOICE_TTS_LATENCY

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
    shared._last_voice_at = time.time()
    shared.mark_voice_activity()
    r = await shared._http.post(
        f"{STT_URL}/v1/audio/transcriptions",
        files={"file": (file.filename or "audio.webm", audio_data, file.content_type or "audio/webm")},
        data={"model": "whisper-1"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


@router.post("/v1/audio/transcriptions")
async def openai_audio_transcriptions(
    file: UploadFile = File(...),
    model: str = "whisper-1",
):
    """OpenAI-compatible STT endpoint for OWUI to point at.

    Sets the voice beacon before proxying to Whisper so the next chat
    request within VOICE_FLAG_WINDOW_SEC is tagged as a voice turn.
    """
    audio_data = await file.read()
    if len(audio_data) > MAX_AUDIO_UPLOAD:
        return JSONResponse({"error": "Audio file too large (max 10 MB)"}, status_code=413)
    shared._last_voice_at = time.time()
    shared.mark_voice_activity()
    r = await shared._http.post(
        f"{STT_URL}/v1/audio/transcriptions",
        files={"file": (file.filename or "audio.webm", audio_data, file.content_type or "audio/webm")},
        data={"model": model},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


TTS_URL = os.environ.get("TTS_URL", "")
TTS_SILENCE_PAD_MS = int(os.environ.get("TTS_SILENCE_PAD_MS", "150"))


def _prepend_silence_wav(wav_bytes: bytes, ms: int) -> bytes:
    """Prepend N milliseconds of silence to a PCM WAV audio blob.

    Voice-cloned neural TTS stutters on the first phonemes of each call because
    the audio buffer and prosody model haven't stabilized. A short silent
    pre-roll lets the player warm up before the real speech starts.
    """
    if ms <= 0:
        return wav_bytes
    with io.BytesIO(wav_bytes) as src_io, wave.open(src_io, "rb") as src:
        nchannels = src.getnchannels()
        sampwidth = src.getsampwidth()
        framerate = src.getframerate()
        audio_data = src.readframes(src.getnframes())
    silence_frames = int(framerate * ms / 1000)
    silence_bytes = b"\x00" * (silence_frames * nchannels * sampwidth)
    out_io = io.BytesIO()
    with wave.open(out_io, "wb") as out:
        out.setnchannels(nchannels)
        out.setsampwidth(sampwidth)
        out.setframerate(framerate)
        out.writeframes(silence_bytes + audio_data)
    return out_io.getvalue()


@router.post("/v1/audio/speech")
async def openai_audio_speech(request: Request):
    """OpenAI-compatible TTS endpoint for OWUI to point at.

    Proxies to the real Qwen3-TTS server and prepends ~150ms of silence to
    WAV responses so the browser's audio buffer and the TTS prosody model
    have time to settle — fixes the per-sentence first-word stutter.
    """
    if not TTS_URL:
        return JSONResponse({"error": "TTS not configured"}, status_code=503)

    body = await request.json()
    # Force WAV so the silence pad stays lossless and we avoid an MP3 decode.
    body["response_format"] = "wav"

    _t0 = time.time()
    try:
        r = await shared._http.post(f"{TTS_URL}/v1/audio/speech", json=body, timeout=60)
        r.raise_for_status()
    finally:
        VOICE_TTS_LATENCY.observe(time.time() - _t0)

    audio = r.content
    content_type = r.headers.get("content-type", "audio/wav")
    if "wav" in content_type.lower():
        try:
            audio = _prepend_silence_wav(audio, TTS_SILENCE_PAD_MS)
        except Exception as e:
            logger.warning("[TTS] silence pre-pad failed (%s) — returning raw audio", e)
    else:
        logger.debug("[TTS] non-WAV response (%s) — silence pre-pad skipped", content_type)

    return Response(content=audio, media_type=content_type)


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
