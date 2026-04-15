"""
Parakeet STT FastAPI Server for Brain Gateway
==============================================
Drop-in replacement for stt_server.py (Whisper) using NVIDIA Parakeet TDT V3
via NeMo. Preserves the same API surface so Open WebUI's OpenAI STT client and
the Wyoming bridge keep working unchanged.

Endpoints (identical to the Whisper server):
- GET  /health                   - Health check
- POST /transcribe               - Simple transcription endpoint
- POST /v1/audio/transcriptions  - OpenAI-compatible transcription

All uploaded audio is normalised to 16 kHz mono PCM WAV via ffmpeg so any
container the browser's MediaRecorder produces (webm/ogg/mp4/wav/...) is
handled. ffmpeg is already a host dependency for the Whisper server.

Parakeet is English-only and roughly 10x faster than Whisper medium with lower
WER, but does not return per-word segments by default. For `verbose_json` we
synthesise a single segment spanning the audio so the OpenAI schema is honoured.
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

MODEL_NAME = os.getenv("PARAKEET_MODEL", "nvidia/parakeet-tdt-0.6b-v3")
DEVICE = os.getenv("PARAKEET_DEVICE", "cuda:1")
HOST = os.getenv("PARAKEET_HOST", "0.0.0.0")
PORT = int(os.getenv("PARAKEET_PORT", "8003"))
TARGET_SAMPLE_RATE = 16_000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB, matches OpenAI Whisper API
FFMPEG_TIMEOUT_SEC = 30
UPLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

model = None
_logged_result_type = False


def load_model() -> None:
    global model
    logger.info("Loading Parakeet model: %s on %s", MODEL_NAME, DEVICE)

    if DEVICE.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA not available")
        device_id = int(DEVICE.split(":", 1)[1]) if ":" in DEVICE else 0
        if device_id >= torch.cuda.device_count():
            raise RuntimeError(
                f"CUDA device {device_id} not found. Available: {torch.cuda.device_count()}"
            )
        logger.info("Using GPU: %s", torch.cuda.get_device_name(device_id))

    try:
        import nemo.collections.asr as nemo_asr
    except ImportError as e:
        logger.error(
            "NeMo import failed — is nemo_toolkit[asr] installed in the venv? %s", e
        )
        raise

    loaded = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
    loaded = loaded.to(DEVICE)
    loaded.eval()
    model = loaded
    logger.info("Parakeet model loaded successfully")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    yield
    logger.info("Shutting down Parakeet STT server")


app = FastAPI(
    title="Parakeet STT Server",
    description="Speech-to-Text API for Brain Gateway (NeMo Parakeet TDT V3)",
    version="1.0.0",
    lifespan=lifespan,
)


async def _read_capped_upload(upload: UploadFile) -> bytes:
    """Read an UploadFile in chunks, rejecting anything over MAX_UPLOAD_BYTES."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"audio file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _prepare_audio(audio_bytes: bytes) -> tuple[str, float]:
    """Decode arbitrary input to a 16kHz mono PCM wav tempfile via ffmpeg.

    Returns (wav_path, duration_seconds). Caller must unlink wav_path.
    Cleans up the tempfile itself on any failure path so callers don't leak.
    """
    wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = wav_tmp.name
    wav_tmp.close()

    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-i",
                "pipe:0",
                "-ac",
                "1",
                "-ar",
                str(TARGET_SAMPLE_RATE),
                "-f",
                "wav",
                wav_path,
            ],
            input=audio_bytes,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace")[:500]
            logger.error("ffmpeg decode failed: %s", stderr)
            raise HTTPException(status_code=400, detail=f"audio decode failed: {stderr}")

        info = sf.info(wav_path)
        duration = float(info.frames) / float(info.samplerate)
        return wav_path, duration
    except Exception:
        if os.path.exists(wav_path):
            os.unlink(wav_path)
        raise


def _transcribe_file(wav_path: str) -> str:
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    with torch.inference_mode():
        results = model.transcribe([wav_path])
    if not results:
        return ""

    global _logged_result_type
    if not _logged_result_type:
        logger.info("NeMo transcribe result type: %s", type(results[0]).__name__)
        _logged_result_type = True

    first = results[0]
    text = first.text if hasattr(first, "text") else str(first)
    return text.strip()


async def _run_transcription(wav_path: str) -> str:
    """Wrap the blocking GPU call in the default threadpool so the event loop
    keeps serving /health and other requests during transcription."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_file, wav_path)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "model": MODEL_NAME,
        "device": DEVICE,
        "model_loaded": model is not None,
    }


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: Optional[str] = Form(default=None),
):
    audio_bytes = await _read_capped_upload(audio)
    wav_path, duration = _prepare_audio(audio_bytes)
    try:
        text = await _run_transcription(wav_path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Transcription failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    segments = [{"start": 0.0, "end": duration, "text": text}] if text else []
    return {
        "text": text,
        "language": language or "en",
        "segments": segments,
    }


@app.post("/v1/audio/transcriptions")
async def openai_transcribe(
    file: UploadFile = File(...),
    model_name: str = Form(default="whisper-1", alias="model"),
    language: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
):
    audio_bytes = await _read_capped_upload(file)
    wav_path, duration = _prepare_audio(audio_bytes)
    try:
        text = await _run_transcription(wav_path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Transcription failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    if response_format == "text":
        return JSONResponse(content=text, media_type="text/plain")
    if response_format == "verbose_json":
        segments = [{"start": 0.0, "end": duration, "text": text}] if text else []
        return {
            "task": "transcribe",
            "language": language or "en",
            "duration": duration,
            "text": text,
            "segments": segments,
        }
    return {"text": text}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
