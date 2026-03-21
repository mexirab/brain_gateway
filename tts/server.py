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
import re
import logging
import asyncio
import struct
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

# Cached voice clone prompts (voice_name -> prompt_items)
# These are pre-computed from reference audio for fast repeated generation
voice_clone_prompts = {}


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

# Loaded voice clones (populated at runtime via /voices/load)
# Maps voice name -> metadata
VOICES = {}

# Directory for storing reference audio files and voice configs
VOICE_SAMPLES_DIR = os.getenv("QWEN_TTS_VOICES_DIR", "/home/labadmin/tts-voices")
VOICES_CONFIG_FILE = os.path.join(VOICE_SAMPLES_DIR, "voices.json")

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


def load_voices_config() -> dict:
    """Load voice configurations from JSON file."""
    if os.path.exists(VOICES_CONFIG_FILE):
        try:
            import json
            with open(VOICES_CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load voices config: {e}")
    return {}


def save_voices_config(config: dict):
    """Save voice configurations to JSON file."""
    try:
        import json
        os.makedirs(VOICE_SAMPLES_DIR, exist_ok=True)
        with open(VOICES_CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        logger.info(f"Saved voices config to {VOICES_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to save voices config: {e}")


def auto_load_voices():
    """Load all configured voices on startup."""
    global voice_clone_prompts, VOICES

    config = load_voices_config()
    if not config:
        logger.info("No saved voices to load")
        return

    logger.info(f"Auto-loading {len(config)} saved voice(s)...")
    for name, voice_info in config.items():
        try:
            ref_audio = voice_info.get("ref_audio")
            ref_text = voice_info.get("ref_text")
            description = voice_info.get("description", f"Cloned voice: {name}")

            if not os.path.exists(ref_audio):
                logger.warning(f"Skipping voice '{name}': ref_audio not found at {ref_audio}")
                continue

            logger.info(f"Loading voice '{name}' from {ref_audio}")
            prompt_items = model.create_voice_clone_prompt(
                ref_audio=ref_audio,
                ref_text=ref_text,
            )
            voice_clone_prompts[name] = prompt_items
            VOICES[name] = {
                "languages": SUPPORTED_LANGUAGES,
                "description": description,
            }
            logger.info(f"Voice '{name}' loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load voice '{name}': {e}")

    logger.info(f"Auto-loaded {len(voice_clone_prompts)} voice(s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage model lifecycle."""
    load_model()
    auto_load_voices()
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
    """Synchronous audio generation (runs in thread pool).

    With Base model, we use voice cloning with a stored reference audio.
    """
    # Check if we have a stored voice clone prompt for this voice
    if voice in voice_clone_prompts:
        wavs, sr = model.generate_voice_clone(
            text=text,
            language=language,
            voice_clone_prompt=voice_clone_prompts[voice],
        )
        return wavs[0], sr
    else:
        raise ValueError(f"No voice clone prompt loaded for '{voice}'. Use /voices/load to load a custom voice.")


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


def split_sentences(text: str) -> List[str]:
    """Split text into sentences on .!? boundaries, preserving punctuation."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in parts if s.strip()]


def audio_to_pcm_bytes(audio_data) -> bytes:
    """Convert audio numpy array to raw PCM int16 bytes."""
    import numpy as np
    # Normalize to int16 range
    if audio_data.dtype != np.int16:
        peak = max(abs(audio_data.max()), abs(audio_data.min()))
        if peak > 0:
            audio_data = (audio_data / peak * 32767).astype(np.int16)
        else:
            audio_data = audio_data.astype(np.int16)
    return audio_data.tobytes()


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


class LoadVoiceRequest(BaseModel):
    """Request to load a voice from reference audio."""
    name: str = Field(..., description="Name for this voice (e.g., 'jessica')")
    ref_audio: str = Field(..., description="Path to reference audio file")
    ref_text: str = Field(..., description="Transcript of reference audio")
    description: str = Field(default="", description="Description of the voice")


def load_voice_sync(name: str, ref_audio: str, ref_text: str) -> None:
    """Load a voice clone prompt from reference audio (synchronous)."""
    global voice_clone_prompts, VOICES

    logger.info(f"Loading voice '{name}' from {ref_audio}")
    prompt_items = model.create_voice_clone_prompt(
        ref_audio=ref_audio,
        ref_text=ref_text,
    )
    voice_clone_prompts[name] = prompt_items
    logger.info(f"Voice '{name}' loaded successfully")


@app.post("/voices/load")
async def load_voice(request: LoadVoiceRequest):
    """
    Load a voice clone from a reference audio file.

    This pre-computes the voice embedding for fast repeated generation.
    The voice can then be used with /tts by specifying its name.

    Example:
    ```
    curl -X POST http://localhost:8002/voices/load \
      -H "Content-Type: application/json" \
      -d '{
        "name": "jessica",
        "ref_audio": "/home/labadmin/tts-voices/jessica_sample.wav",
        "ref_text": "The transcript of what Jessica says in the audio.",
        "description": "Jessica McCabe - warm, energetic ADHD advocate"
      }'
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not os.path.exists(request.ref_audio):
        raise HTTPException(status_code=400, detail=f"Reference audio not found: {request.ref_audio}")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            executor,
            load_voice_sync,
            request.name,
            request.ref_audio,
            request.ref_text,
        )

        # Add to VOICES registry
        VOICES[request.name] = {
            "languages": SUPPORTED_LANGUAGES,  # Cloned voices work with all languages
            "description": request.description or f"Cloned voice: {request.name}",
        }

        # Save to config file for auto-loading on restart
        config = load_voices_config()
        config[request.name] = {
            "ref_audio": request.ref_audio,
            "ref_text": request.ref_text,
            "description": request.description or f"Cloned voice: {request.name}",
        }
        save_voices_config(config)

        return {
            "status": "success",
            "voice": request.name,
            "message": f"Voice '{request.name}' loaded and saved for auto-load on restart",
        }

    except Exception as e:
        logger.error(f"Failed to load voice '{request.name}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts")
async def text_to_speech(request: TTSRequest):
    """
    Convert text to speech using a loaded voice clone.

    First load a voice with /voices/load, then use it here by name.

    Example:
    ```
    curl -X POST http://localhost:8002/tts \
      -H "Content-Type: application/json" \
      -d '{"text": "Hello Nadim!", "voice": "jessica"}' \
      --output speech.wav
    ```
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if request.voice not in voice_clone_prompts:
        available = list(voice_clone_prompts.keys()) if voice_clone_prompts else ["none - load a voice first with /voices/load"]
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{request.voice}' not loaded. Available: {available}"
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


@app.post("/tts/stream")
async def text_to_speech_stream(request: TTSRequest):
    """
    Streaming TTS endpoint — generates audio sentence-by-sentence.

    Returns raw PCM int16 mono audio at the model's sample rate.
    The first sentence starts streaming before subsequent ones are generated,
    enabling low-latency playback when piped to Snapcast.

    Headers in response:
    - X-Sample-Rate: sample rate (e.g. 24000)
    - X-Channels: 1 (mono)
    - X-Format: int16
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if request.voice not in voice_clone_prompts:
        available = list(voice_clone_prompts.keys()) if voice_clone_prompts else ["none"]
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{request.voice}' not loaded. Available: {available}"
        )

    sentences = split_sentences(request.text)
    if not sentences:
        raise HTTPException(status_code=400, detail="No text to synthesize")

    # Detect sample rate from a quick probe (model-dependent, usually 24000)
    # We'll include it in response headers
    sample_rate = 24000  # Qwen3-TTS default

    async def generate():
        loop = asyncio.get_event_loop()
        for sentence in sentences:
            try:
                audio_data, sr = await loop.run_in_executor(
                    executor,
                    generate_audio_sync,
                    sentence,
                    request.voice,
                    request.language,
                    request.emotion,
                )
                yield audio_to_pcm_bytes(audio_data)
            except Exception as e:
                logger.error(f"Stream TTS failed for sentence '{sentence[:50]}': {e}")
                break

    return StreamingResponse(
        generate(),
        media_type="application/octet-stream",
        headers={
            "X-Sample-Rate": str(sample_rate),
            "X-Channels": "1",
            "X-Format": "int16",
        },
    )


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

    NOTE: This endpoint requires the VoiceDesign model variant.
    The Base model (currently loaded) only supports voice cloning.
    """
    # Base model doesn't support voice design
    raise HTTPException(
        status_code=501,
        detail="Voice design not available. The Base model only supports voice cloning. "
               "Use /tts/clone or load a voice with /voices/load instead."
    )


# =============================================================================
# OpenAI-Compatible Endpoint (for easier integration)
# =============================================================================

class OpenAITTSRequest(BaseModel):
    """OpenAI-compatible TTS request."""
    model: str = Field(default="qwen3-tts", description="Model name (ignored)")
    input: str = Field(..., description="Text to synthesize")
    voice: str = Field(..., description="Voice name (must be loaded via /voices/load first)")
    response_format: str = Field(default="wav", description="Audio format")
    speed: float = Field(default=1.0, description="Speed (ignored for now)")


@app.post("/v1/audio/speech")
async def openai_compatible_tts(request: OpenAITTSRequest):
    """
    OpenAI-compatible TTS endpoint for drop-in replacement.

    Requires a voice to be loaded first via /voices/load.

    Example:
    ```
    curl -X POST http://localhost:8002/v1/audio/speech \
      -H "Content-Type: application/json" \
      -d '{"input": "Hello!", "voice": "jessica"}' \
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
