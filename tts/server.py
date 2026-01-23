"""
Qwen3-TTS FastAPI Server for Brain Gateway
==========================================
Provides TTS endpoints for the Brain Gateway voice pipeline.
Designed to run on Uranus (10.0.0.173) with RTX 5080 GPUs.

Endpoints:
- POST /tts          - Text-to-speech with voice selection
- POST /tts/clone    - Voice cloning from reference audio
- POST /tts/design   - Voice design from text description
- GET  /voices       - List available voices
- GET  /health       - Health check
"""

import os
import io
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List
from contextlib import asynccontextmanager

import torch
import soundfile as sf
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

# Configuration from environment
MODEL_PATH = os.getenv("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
DEVICE = os.getenv("QWEN_TTS_DEVICE", "cuda:0")
HOST = os.getenv("QWEN_TTS_HOST", "0.0.0.0")
PORT = int(os.getenv("QWEN_TTS_PORT", "8002"))
DTYPE = os.getenv("QWEN_TTS_DTYPE", "bfloat16")
USE_FLASH_ATTN = os.getenv("QWEN_TTS_FLASH_ATTN", "true").lower() == "true"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# Thread pool for blocking inference calls
executor = ThreadPoolExecutor(max_workers=2)

# Global model reference
model = None


# =============================================================================
# Pydantic Models
# =============================================================================

class TTSRequest(BaseModel):
    """Request for basic text-to-speech."""
    text: str = Field(..., description="Text to synthesize")
    voice: str = Field(default="aiden", description="Voice name (see /voices)")
    language: str = Field(default="English", description="Language code")
    emotion: str = Field(default="", description="Emotion/style instruction (e.g., 'warm and friendly')")
    format: str = Field(default="wav", description="Output format: wav, mp3, ogg")


class TTSCloneRequest(BaseModel):
    """Request for voice cloning."""
    text: str = Field(..., description="Text to synthesize")
    ref_audio_url: Optional[str] = Field(None, description="URL to reference audio")
    ref_text: str = Field(..., description="Transcript of reference audio")
    language: str = Field(default="English", description="Language code")


class TTSDesignRequest(BaseModel):
    """Request for voice design from description."""
    text: str = Field(..., description="Text to synthesize")
    voice_description: str = Field(..., description="Description of desired voice (e.g., 'warm female voice with slight British accent')")
    language: str = Field(default="English", description="Language code")


class VoiceInfo(BaseModel):
    """Information about an available voice."""
    name: str
    languages: List[str]
    description: str


# =============================================================================
# Available Voices (Qwen3-TTS-CustomVoice preset voices)
# =============================================================================

VOICES = {
    # Actual voices from Qwen3-TTS-12Hz-1.7B-CustomVoice
    "aiden": {"languages": ["English", "Chinese"], "description": "Male, warm and professional"},
    "dylan": {"languages": ["English", "Chinese"], "description": "Male, energetic and youthful"},
    "eric": {"languages": ["English", "Chinese"], "description": "Male, confident and clear"},
    "ryan": {"languages": ["English", "Chinese"], "description": "Male, casual and approachable"},
    "ono_anna": {"languages": ["Japanese", "English"], "description": "Female, clear and friendly"},
    "serena": {"languages": ["Chinese", "English"], "description": "Female, elegant and refined"},
    "sohee": {"languages": ["Korean", "English"], "description": "Female, bright and expressive"},
    "uncle_fu": {"languages": ["Chinese", "English"], "description": "Male, mature and warm"},
    "vivian": {"languages": ["Chinese", "English"], "description": "Female, expressive and dynamic"},
}

SUPPORTED_LANGUAGES = [
    "Chinese", "English", "Japanese", "Korean",
    "German", "French", "Russian", "Portuguese",
    "Spanish", "Italian"
]


# =============================================================================
# Model Loading
# =============================================================================

def load_model():
    """Load the Qwen3-TTS model."""
    global model

    logger.info(f"Loading Qwen3-TTS model: {MODEL_PATH}")
    logger.info(f"Device: {DEVICE}, Dtype: {DTYPE}, FlashAttn: {USE_FLASH_ATTN}")

    try:
        from qwen_tts import Qwen3TTSModel

        dtype_map = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }

        model_kwargs = {
            "device_map": DEVICE,
            "dtype": dtype_map.get(DTYPE, torch.bfloat16),
        }

        if USE_FLASH_ATTN:
            model_kwargs["attn_implementation"] = "flash_attention_2"

        model = Qwen3TTSModel.from_pretrained(MODEL_PATH, **model_kwargs)
        logger.info("Model loaded successfully")

    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage model lifecycle."""
    load_model()
    yield
    logger.info("Shutting down TTS server")


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Qwen3-TTS Server",
    description="Text-to-Speech API for Brain Gateway",
    version="1.0.0",
    lifespan=lifespan,
)


# =============================================================================
# Helper Functions
# =============================================================================

def generate_audio_sync(
    text: str,
    voice: str,
    language: str,
    emotion: str = ""
) -> tuple:
    """Synchronous audio generation (runs in thread pool)."""
    wavs, sr = model.generate_custom_voice(
        text=text,
        language=language,
        speaker=voice,
        instruct=emotion if emotion else None,
    )
    return wavs[0], sr


def generate_clone_sync(
    text: str,
    ref_audio: str,
    ref_text: str,
    language: str
) -> tuple:
    """Synchronous voice clone generation."""
    wavs, sr = model.generate_voice_clone(
        text=text,
        language=language,
        ref_audio=ref_audio,
        ref_text=ref_text,
    )
    return wavs[0], sr


def generate_design_sync(
    text: str,
    voice_description: str,
    language: str
) -> tuple:
    """Synchronous voice design generation."""
    wavs, sr = model.generate_voice_design(
        text=text,
        language=language,
        instruct=voice_description,
    )
    return wavs[0], sr


def audio_to_bytes(audio_data, sample_rate: int, format: str = "wav") -> bytes:
    """Convert audio numpy array to bytes."""
    buffer = io.BytesIO()
    sf.write(buffer, audio_data, sample_rate, format=format.upper())
    buffer.seek(0)
    return buffer.read()


# =============================================================================
# Endpoints
# =============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "model": MODEL_PATH,
        "device": DEVICE,
        "model_loaded": model is not None,
    }


@app.get("/voices", response_model=List[VoiceInfo])
async def list_voices():
    """List available voices."""
    return [
        VoiceInfo(name=name, languages=info["languages"], description=info["description"])
        for name, info in VOICES.items()
    ]


@app.get("/languages")
async def list_languages():
    """List supported languages."""
    return {"languages": SUPPORTED_LANGUAGES}


@app.post("/tts")
async def text_to_speech(request: TTSRequest):
    """
    Convert text to speech using a preset voice.

    Example:
    ```
    curl -X POST http://localhost:8002/tts \
      -H "Content-Type: application/json" \
      -d '{"text": "Hello Nadim!", "voice": "Ethan", "emotion": "warm and friendly"}' \
      --output speech.wav
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if request.voice not in VOICES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice: {request.voice}. Available: {list(VOICES.keys())}"
        )

    if request.language not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language: {request.language}. Supported: {SUPPORTED_LANGUAGES}"
        )

    try:
        loop = asyncio.get_event_loop()
        audio_data, sample_rate = await loop.run_in_executor(
            executor,
            generate_audio_sync,
            request.text,
            request.voice,
            request.language,
            request.emotion,
        )

        audio_bytes = audio_to_bytes(audio_data, sample_rate, request.format)

        media_types = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "ogg": "audio/ogg",
        }

        return Response(
            content=audio_bytes,
            media_type=media_types.get(request.format, "audio/wav"),
            headers={
                "Content-Disposition": f"attachment; filename=speech.{request.format}"
            }
        )

    except Exception as e:
        logger.error(f"TTS generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/clone")
async def voice_clone(
    text: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form(default="English"),
    ref_audio: UploadFile = File(...),
):
    """
    Clone a voice from a reference audio sample.

    Requires ~3 seconds of clear speech audio and its transcript.

    Example:
    ```
    curl -X POST http://localhost:8002/tts/clone \
      -F "text=Hello from my cloned voice!" \
      -F "ref_text=This is my reference audio transcript." \
      -F "ref_audio=@voice_sample.wav" \
      --output cloned.wav
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        # Read uploaded audio
        audio_bytes = await ref_audio.read()

        # Save to temp file (qwen_tts expects file path)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            loop = asyncio.get_event_loop()
            audio_data, sample_rate = await loop.run_in_executor(
                executor,
                generate_clone_sync,
                text,
                tmp_path,
                ref_text,
                language,
            )
        finally:
            os.unlink(tmp_path)

        audio_bytes = audio_to_bytes(audio_data, sample_rate, "wav")

        return Response(
            content=audio_bytes,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=cloned.wav"}
        )

    except Exception as e:
        logger.error(f"Voice clone failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts/design")
async def voice_design(request: TTSDesignRequest):
    """
    Generate speech with a custom voice designed from a text description.

    Example:
    ```
    curl -X POST http://localhost:8002/tts/design \
      -H "Content-Type: application/json" \
      -d '{"text": "Hello!", "voice_description": "warm female voice with slight British accent"}' \
      --output designed.wav
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    try:
        loop = asyncio.get_event_loop()
        audio_data, sample_rate = await loop.run_in_executor(
            executor,
            generate_design_sync,
            request.text,
            request.voice_description,
            request.language,
        )

        audio_bytes = audio_to_bytes(audio_data, sample_rate, "wav")

        return Response(
            content=audio_bytes,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=designed.wav"}
        )

    except Exception as e:
        logger.error(f"Voice design failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# OpenAI-Compatible Endpoint (for easier integration)
# =============================================================================

class OpenAITTSRequest(BaseModel):
    """OpenAI-compatible TTS request."""
    model: str = Field(default="qwen3-tts", description="Model name (ignored)")
    input: str = Field(..., description="Text to synthesize")
    voice: str = Field(default="aiden", description="Voice name")
    response_format: str = Field(default="wav", description="Audio format")
    speed: float = Field(default=1.0, description="Speed (ignored for now)")


@app.post("/v1/audio/speech")
async def openai_compatible_tts(request: OpenAITTSRequest):
    """
    OpenAI-compatible TTS endpoint for drop-in replacement.

    Example:
    ```
    curl -X POST http://localhost:8002/v1/audio/speech \
      -H "Content-Type: application/json" \
      -d '{"input": "Hello!", "voice": "Ethan"}' \
      --output speech.wav
    ```
    """
    tts_request = TTSRequest(
        text=request.input,
        voice=request.voice,
        format=request.response_format,
    )
    return await text_to_speech(tts_request)


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
