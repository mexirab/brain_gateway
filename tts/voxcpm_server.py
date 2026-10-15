"""
VoxCPM2 TTS server for Brain Gateway — cloned voice + per-utterance style.
==========================================================================
Drop-in for the Qwen3-TTS server (`tts/server.py`): same `/health`, `/voices`,
`/tts` and `/v1/audio/speech` shapes, so the orchestrator, Telegram and the
Wyoming bridge only need TTS_URL repointed. The difference is style control:
`/tts` honors the `emotion` field (which the Qwen server accepts but ignores)
and `/v1/audio/speech` honors OpenAI's `instructions` field. Either becomes a
VoxCPM2 "controllable cloning" note — "(calm, gentle and reassuring)<text>" —
spoken in the cloned voice. No note = the model picks prosody from the text.

Voices come from the same `voices.json` as the Qwen server (only `ref_audio`
is used: reference-only cloning keeps the timbre but not the reference clip's
own mood, which is what lets the style note steer).

Env: VOXCPM_MODEL (HF id or local snapshot path), VOXCPM_VOICES_DIR,
VOXCPM_HOST/VOXCPM_PORT (0.0.0.0:8004), VOXCPM_OUTPUT_SR (24000 — matches the
Qwen server so Wyoming/HA consumers see the same rate; 0 = native 48 kHz),
VOXCPM_CFG (2.0), VOXCPM_STEPS (10), VOXCPM_MAX_CHARS (2000).
Unit: tts/voxcpm-tts.service. Text is never logged — lengths only.
"""

import asyncio
import io
import json
import logging
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

MODEL_ID = os.getenv("VOXCPM_MODEL", "openbmb/VoxCPM2")
VOICES_DIR = os.getenv("VOXCPM_VOICES_DIR", "/home/labadmin/tts-voices")
HOST = os.getenv("VOXCPM_HOST", "0.0.0.0")
PORT = int(os.getenv("VOXCPM_PORT", "8004"))
OUTPUT_SR = int(os.getenv("VOXCPM_OUTPUT_SR", "24000"))
CFG_VALUE = float(os.getenv("VOXCPM_CFG", "2.0"))
STEPS = int(os.getenv("VOXCPM_STEPS", "10"))
MAX_CHARS = int(os.getenv("VOXCPM_MAX_CHARS", "2000"))
MAX_STYLE_CHARS = 80
# Refuse to start on CPU: a CPU fallback "works" at ~7 s per sentence, which
# looks like a hung TTS to every caller. Set false only for local testing.
REQUIRE_CUDA = os.getenv("VOXCPM_REQUIRE_CUDA", "true").lower() == "true"
# Requests waiting for (or holding) the GPU worker before new ones get 503 —
# one flooding client must not delay every reminder by minutes.
MAX_PENDING = int(os.getenv("VOXCPM_MAX_PENDING", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("voxcpm-tts")

# One worker: a single model instance on one GPU; concurrent requests queue.
executor = ThreadPoolExecutor(max_workers=1)
_pending = 0
model = None
sample_rate = 0
VOICES: dict[str, dict] = {}  # name -> {"ref_audio": path, "description": str}

MEDIA_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg", "flac": "audio/flac"}

# VoxCPM2 reads a leading "(...)" as a style instruction. Parentheses in the
# spoken text would let an LLM reply (or an injected one) steer the voice, so
# they're stripped from the body; the style note is the only parenthetical.
# NFKC first: lookalikes such as ﹙ ⁽ ₍ normalise to "(".
_PARENS = re.compile(r"[()（）\[\]【】⦅⦆❨❩〔〕]")


def _strip_brackets(text: str) -> str:
    return re.sub(r"\s+", " ", _PARENS.sub(" ", unicodedata.normalize("NFKC", text))).strip()


def clean_text(text: str) -> str:
    return _strip_brackets(text)


def clean_style(style: Optional[str]) -> str:
    if not style:
        return ""
    return _strip_brackets(style)[:MAX_STYLE_CHARS].strip()


def load_voices() -> None:
    path = os.path.join(VOICES_DIR, "voices.json")
    try:
        with open(path) as f:
            config = json.load(f)
    except (OSError, ValueError) as e:
        logger.error("Could not read %s: %s", path, e)
        return
    for name, info in config.items():
        ref = info.get("ref_audio", "")
        if ref and os.path.exists(ref):
            VOICES[name] = {"ref_audio": ref, "description": info.get("description", f"Cloned voice: {name}")}
        else:
            logger.warning("Voice %r skipped: reference audio missing", name)
    logger.info("Loaded %d voice(s): %s", len(VOICES), sorted(VOICES))


def load_model() -> None:
    global model, sample_rate
    from voxcpm import VoxCPM

    if REQUIRE_CUDA and not torch.cuda.is_available():
        raise RuntimeError("CUDA not available (check DeviceAllow / driver); refusing CPU fallback")
    if not VOICES:
        logger.error("No usable voices — every /tts call will 400 and /health reports 503")
    t0 = time.time()
    model = VoxCPM.from_pretrained(MODEL_ID, load_denoiser=False)
    sample_rate = model.tts_model.sample_rate
    logger.info("Model %s loaded in %.1fs (native %d Hz)", MODEL_ID, time.time() - t0, sample_rate)
    # Warm-up: the first calls trigger torch.compile; keep that off the first real request.
    for voice in list(VOICES)[:1]:
        for style in ("", "calm"):
            generate_sync("Warming up.", voice, style)
    logger.info("Warm-up done")


def generate_sync(text: str, voice: str, style: str) -> np.ndarray:
    prompt = f"({style}){text}" if style else text
    wav = model.generate(
        text=prompt,
        reference_wav_path=VOICES[voice]["ref_audio"],
        cfg_value=CFG_VALUE,
        inference_timesteps=STEPS,
    )
    if OUTPUT_SR and sample_rate != OUTPUT_SR:
        wav = torchaudio.functional.resample(torch.from_numpy(wav).float(), sample_rate, OUTPUT_SR).numpy()
    return wav


def encode(wav: np.ndarray, fmt: str) -> bytes:
    sr = OUTPUT_SR or sample_rate
    buf = io.BytesIO()
    if fmt == "mp3":
        sf.write(buf, wav, sr, format="MP3")
    elif fmt == "ogg":
        sf.write(buf, wav, sr, format="OGG", subtype="VORBIS")
    else:
        sf.write(buf, wav, sr, format=fmt.upper(), subtype="PCM_16")
    return buf.getvalue()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_voices()
    await asyncio.get_running_loop().run_in_executor(executor, load_model)
    yield
    executor.shutdown(wait=False)


app = FastAPI(title="VoxCPM2 TTS", lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str = Field(..., max_length=MAX_CHARS * 2, description="Text to synthesize")
    voice: str = Field(default="default", description="Voice name (see /voices)")
    language: str = Field(default="English", description="Accepted for API parity; VoxCPM2 detects language")
    emotion: str = Field(default="", max_length=200, description="Style note, e.g. 'calm, gentle and reassuring'")
    format: str = Field(default="wav", description="wav, mp3, ogg or flac")


class OpenAITTSRequest(BaseModel):
    model: str = Field(default="tts-1")
    input: str = Field(..., max_length=MAX_CHARS * 2)
    voice: str = Field(default="default")
    response_format: str = Field(default="wav")
    instructions: str = Field(default="", max_length=200, description="Style note (OpenAI field name)")
    speed: float = Field(default=1.0, description="Ignored")


@app.get("/health")
async def health():
    ok = model is not None and bool(VOICES)
    body = {
        "status": "healthy" if ok else "unhealthy",
        "model": MODEL_ID,
        "model_loaded": model is not None,
        "voices": len(VOICES),
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/voices")
async def list_voices():
    return [{"name": n, "languages": ["English"], "description": v["description"]} for n, v in VOICES.items()]


@app.post("/tts")
async def text_to_speech(request: TTSRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if request.voice not in VOICES:
        raise HTTPException(status_code=400, detail=f"Voice '{request.voice}' not loaded. Available: {sorted(VOICES)}")
    fmt = request.format.lower()
    if fmt not in MEDIA_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Use one of {sorted(MEDIA_TYPES)}")
    text = clean_text(request.text)
    if not text:
        raise HTTPException(status_code=400, detail="No text provided")
    if len(text) > MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"Text too long (max {MAX_CHARS} chars)")
    style = clean_style(request.emotion)

    global _pending
    if _pending >= MAX_PENDING:
        logger.warning("Busy: %d requests pending, rejecting", _pending)
        raise HTTPException(status_code=503, detail="TTS busy, try again shortly")
    _pending += 1
    t0 = time.time()
    try:
        loop = asyncio.get_running_loop()
        wav = await loop.run_in_executor(executor, generate_sync, text, request.voice, style)
        audio = await asyncio.to_thread(encode, wav, fmt)
    except Exception as e:
        # Exception text is torch/CUDA detail (e.g. OOM), never the request text.
        logger.error("Synthesis failed: %s: %s", type(e).__name__, str(e)[:120])
        raise HTTPException(status_code=500, detail="TTS generation failed") from e
    finally:
        _pending -= 1
    logger.info("tts chars=%d styled=%s fmt=%s took=%.2fs", len(text), bool(style), fmt, time.time() - t0)
    return Response(content=audio, media_type=MEDIA_TYPES[fmt])


@app.post("/v1/audio/speech")
async def openai_compatible_tts(request: OpenAITTSRequest):
    return await text_to_speech(
        TTSRequest(
            text=request.input, voice=request.voice, emotion=request.instructions, format=request.response_format
        )
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
