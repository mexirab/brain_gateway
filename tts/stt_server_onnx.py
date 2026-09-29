"""
Parakeet STT FastAPI Server (ONNX / CPU) for Brain Gateway
==========================================================
Drop-in replacement for stt_server_parakeet.py that runs the SAME NVIDIA
Parakeet-TDT-0.6B weights via ONNX Runtime on CPU (onnx-asr) instead of NeMo.
Same API surface, so the Wyoming bridge, orchestrator proxy, Open WebUI STT
client and Telegram voice path keep working unchanged.

Why: NeMo's runtime balloons a 0.6B model to ~6.3 GB VRAM, which no longer fits
alongside the 27B LLM + TTS on the single 32 GB GPU. ONNX Runtime on CPU uses
~2 GB RAM, 0 GB VRAM, runs ~30x real-time (sub-second on short commands), and
keeps identical accuracy (same weights).

Endpoints (identical to the NeMo server):
- GET  /health                   - Health check
- POST /transcribe               - Simple transcription endpoint
- POST /v1/audio/transcriptions  - OpenAI-compatible transcription

All uploaded audio is normalised to 16 kHz mono PCM WAV via ffmpeg so any
container the browser's MediaRecorder produces (webm/ogg/mp4/wav/...) is handled.
"""

import asyncio
import logging
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

# onnx-asr model id (NOT the HF repo id NeMo uses). v2 = English-only, best on English.
MODEL_NAME = os.getenv("STT_ONNX_MODEL", "nemo-parakeet-tdt-0.6b-v2")
# int8 keeps ~640MB weights; falls back to fp32 automatically if unavailable.
QUANT = os.getenv("STT_ONNX_QUANT", "int8")
BACKEND = "onnx-asr (CPUExecutionProvider)"
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
_active_quant = "none"
_logged_result_type = False


def load_model() -> None:
    global model, _active_quant
    import onnx_asr

    providers = ["CPUExecutionProvider"]
    # Try int8 first; fall back to fp32 (default) if the quantized variant is
    # unavailable for this model or the runtime rejects it.
    attempts = [QUANT, None] if QUANT and QUANT.lower() != "none" else [None]
    last_err = None
    for q in attempts:
        try:
            logger.info(
                "Loading Parakeet ONNX model %s (quantization=%s) on CPU", MODEL_NAME, q
            )
            kwargs = {"providers": providers}
            if q:
                kwargs["quantization"] = q
            model = onnx_asr.load_model(MODEL_NAME, **kwargs)
            _active_quant = q or "fp32"
            logger.info("Parakeet ONNX model loaded (quantization=%s)", _active_quant)
            return
        except Exception as e:  # noqa: BLE001 - retry with next quant
            last_err = e
            logger.warning("Load with quantization=%s failed: %s", q, e)
    raise RuntimeError(f"Failed to load ONNX model {MODEL_NAME}: {last_err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_model()
    yield
    logger.info("Shutting down Parakeet ONNX STT server")


app = FastAPI(
    title="Parakeet STT Server (ONNX/CPU)",
    description="Speech-to-Text API for Brain Gateway (Parakeet-TDT via ONNX Runtime, CPU)",
    version="2.0.0",
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
    """
    wav_tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = wav_tmp.name
    wav_tmp.close()

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-y",
                "-i", "pipe:0",
                "-ac", "1",
                "-ar", str(TARGET_SAMPLE_RATE),
                "-f", "wav", wav_path,
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
    result = model.recognize(wav_path)

    global _logged_result_type
    if not _logged_result_type:
        logger.info("onnx-asr recognize result type: %s", type(result).__name__)
        _logged_result_type = True

    # onnx-asr returns a str for a single input; be defensive about list/obj.
    if isinstance(result, (list, tuple)):
        result = result[0] if result else ""
    text = result if isinstance(result, str) else getattr(result, "text", str(result))
    return text.strip()


async def _run_transcription(wav_path: str) -> str:
    """Wrap the blocking CPU inference in the default threadpool so the event
    loop keeps serving /health and other requests during transcription."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_file, wav_path)


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "model": MODEL_NAME,
        "backend": BACKEND,
        "quantization": _active_quant,
        "device": "cpu",
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
    return {"text": text, "language": language or "en", "segments": segments}


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
