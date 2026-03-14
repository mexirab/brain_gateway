"""
Whisper STT FastAPI Server for Brain Gateway
=============================================
Provides speech-to-text endpoints for the Brain Gateway voice pipeline.
Runs on Uranus GPU 0 (cuda:0) alongside TTS. GPU 1 reserved for ComfyUI.

Endpoints:
- POST /v1/audio/transcriptions  - OpenAI-compatible transcription
- POST /transcribe               - Simple transcription endpoint
- GET  /health                   - Health check
"""

import os
import logging
import tempfile
from typing import Optional
from contextlib import asynccontextmanager

import torch
import whisper
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse

# Configuration from environment
MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "cuda:0")
HOST = os.getenv("WHISPER_HOST", "0.0.0.0")
PORT = int(os.getenv("WHISPER_PORT", "8003"))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Global model reference
model = None


def load_model():
    """Load the Whisper model."""
    global model

    logger.info(f"Loading Whisper model: {MODEL_SIZE} on {DEVICE}")

    try:
        # Check if CUDA device is available
        if DEVICE.startswith("cuda"):
            device_id = int(DEVICE.split(":")[1]) if ":" in DEVICE else 0
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA not available")
            if device_id >= torch.cuda.device_count():
                raise RuntimeError(f"CUDA device {device_id} not found. Available: {torch.cuda.device_count()}")
            logger.info(f"Using GPU: {torch.cuda.get_device_name(device_id)}")

        model = whisper.load_model(MODEL_SIZE, device=DEVICE)
        logger.info("Whisper model loaded successfully")

    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage model lifecycle."""
    load_model()
    yield
    logger.info("Shutting down STT server")


app = FastAPI(
    title="Whisper STT Server",
    description="Speech-to-Text API for Brain Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "model": MODEL_SIZE,
        "device": DEVICE,
        "model_loaded": model is not None,
    }


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: Optional[str] = Form(default=None),
):
    """
    Transcribe audio to text.

    Example:
    ```
    curl -X POST http://localhost:8003/transcribe \
      -F "audio=@recording.wav" \
      -F "language=en"
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        # Save uploaded file to temp location
        audio_bytes = await audio.read()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            # Transcribe
            options = {}
            if language:
                options["language"] = language

            result = model.transcribe(tmp_path, **options)

            return {
                "text": result["text"].strip(),
                "language": result.get("language", language),
                "segments": [
                    {
                        "start": seg["start"],
                        "end": seg["end"],
                        "text": seg["text"].strip(),
                    }
                    for seg in result.get("segments", [])
                ],
            }
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/audio/transcriptions")
async def openai_transcribe(
    file: UploadFile = File(...),
    model_name: str = Form(default="whisper-1", alias="model"),
    language: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
):
    """
    OpenAI-compatible transcription endpoint.

    Example:
    ```
    curl -X POST http://localhost:8003/v1/audio/transcriptions \
      -F "file=@recording.wav" \
      -F "model=whisper-1"
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        audio_bytes = await file.read()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            options = {"temperature": temperature}
            if language:
                options["language"] = language

            result = model.transcribe(tmp_path, **options)
            text = result["text"].strip()

            if response_format == "text":
                return JSONResponse(content=text, media_type="text/plain")
            elif response_format == "verbose_json":
                return {
                    "task": "transcribe",
                    "language": result.get("language", language or "en"),
                    "duration": result.get("segments", [{}])[-1].get("end", 0) if result.get("segments") else 0,
                    "text": text,
                    "segments": result.get("segments", []),
                }
            else:  # json
                return {"text": text}

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
