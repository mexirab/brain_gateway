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
VOXCPM_CFG (2.0), VOXCPM_STEPS (10), VOXCPM_MAX_CHARS (2000),
VOXCPM_API_KEY_FILE (empty = no key), VOXCPM_MAX_BODY_BYTES (65536).
Units: tts/voxcpm-tts-jupiter.service (live), tts/voxcpm-tts.service (Helios
rollback). Text is never logged — lengths only.

Access control (_Guard, pure ASGI, outermost): with VOXCPM_API_KEY_FILE set,
every path except /health needs `Authorization: Bearer <key>` (constant-time
compare; the orchestrator sends TTS_API_KEY, see orchestrator/model_auth.py).
The server speaks in a cloned voice, so on the Jupiter hub, where every
container can route to it, the key is what limits it to Jess. A missing,
unreadable or short key file fails the start. Bodies over
VOXCPM_MAX_BODY_BYTES get 413 before they are read (a declared
Content-Length) or once they pass the cap (chunked); uvicorn's
limit_concurrency caps open connections.
"""

import asyncio
import hmac
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
API_KEY_FILE = os.getenv("VOXCPM_API_KEY_FILE", "")
# Real requests are a few KB (text <= 2000 chars, JSON-escaped). Anything far bigger
# is not a TTS request, and reading it would only cost memory.
MAX_BODY_BYTES = int(os.getenv("VOXCPM_MAX_BODY_BYTES", "65536"))
MAX_CONNECTIONS = int(os.getenv("VOXCPM_MAX_CONNECTIONS", "32"))
MAX_NAME_CHARS = 64
MIN_KEY_CHARS = 24

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
# NFKC first (lookalikes such as ﹙ ⁽ ₍ normalise to "("), then every opening or
# closing bracket by Unicode category (Ps/Pe), not a hand-written list: glyphs such
# as ❪ ⸨ ﴾ ⟮ don't fold under NFKC but still look like a parenthesis.
_BRACKET_CATEGORIES = frozenset({"Ps", "Pe"})


def _strip_brackets(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(" " if unicodedata.category(ch) in _BRACKET_CATEGORIES else ch for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def _load_api_key(path: str) -> bytes:
    """The bearer key from VOXCPM_API_KEY_FILE, or b"" when no file is configured.

    Raises on a missing, unreadable or short file: an unusable key must fail the
    start (TTSDown), not quietly leave the server open.
    """
    if not path:
        return b""
    with open(path, "rb") as f:
        key = f.read().strip()
    if len(key) < MIN_KEY_CHARS or b"\n" in key:
        raise RuntimeError(f"{path} must hold one key of at least {MIN_KEY_CHARS} characters")
    return key


API_KEY = _load_api_key(API_KEY_FILE)


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


async def _reply(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _Guard:
    """Pure ASGI: bearer key (all paths but /health) and a request-body cap.

    The body is read here, up to MAX_BODY_BYTES, and replayed to the app, so an
    oversized or never-ending body never reaches FastAPI's parser.
    """

    def __init__(self, app, api_key: bytes = b"", max_body: int = MAX_BODY_BYTES):
        self.app = app
        self.api_key = api_key
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if self.api_key and scope.get("path") != "/health":
            auth = headers.get(b"authorization", b"")
            scheme, _, token = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(token.strip(), self.api_key):
                await _reply(send, 401, "Unauthorized")
                return
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_body
            except ValueError:
                await _reply(send, 400, "Bad Content-Length")
                return
            if too_big:
                await _reply(send, 413, "Request body too large")
                return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > self.max_body:
                await _reply(send, 413, "Request body too large")
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# No /docs, /redoc or /openapi.json: nothing needs them, and they map the API for anyone who can reach it.
app = FastAPI(title="VoxCPM2 TTS", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(_Guard, api_key=API_KEY)


class TTSRequest(BaseModel):
    text: str = Field(..., max_length=MAX_CHARS * 2, description="Text to synthesize")
    voice: str = Field(default="default", max_length=MAX_NAME_CHARS, description="Voice name (see /voices)")
    language: str = Field(
        default="English", max_length=MAX_NAME_CHARS, description="Accepted for API parity; VoxCPM2 detects language"
    )
    emotion: str = Field(default="", max_length=200, description="Style note, e.g. 'calm, gentle and reassuring'")
    format: str = Field(default="wav", max_length=MAX_NAME_CHARS, description="wav, mp3, ogg or flac")


class OpenAITTSRequest(BaseModel):
    model: str = Field(default="tts-1", max_length=MAX_NAME_CHARS)
    input: str = Field(..., max_length=MAX_CHARS * 2)
    voice: str = Field(default="default", max_length=MAX_NAME_CHARS)
    response_format: str = Field(default="wav", max_length=MAX_NAME_CHARS)
    instructions: str = Field(default="", max_length=200, description="Style note (OpenAI field name)")
    speed: float = Field(default=1.0, description="Ignored")


@app.get("/health")
async def health():
    ok = model is not None and bool(VOICES)
    body = {
        "status": "healthy" if ok else "unhealthy",
        # Base name only: the full path shows the install's user and layout.
        "model": os.path.basename(MODEL_ID.rstrip("/")),
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
        raise HTTPException(status_code=400, detail="Voice not loaded (see /voices)")
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
        # The type only: exception text from deep in voxcpm could in principle quote the input.
        logger.error("Synthesis failed: %s", type(e).__name__)
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

    if not API_KEY:
        logger.warning("No VOXCPM_API_KEY_FILE: every caller that can reach this server can synthesize")
    # Socket activation (tts/voxcpm-tts-jupiter.socket): systemd hands over the listening socket as fd 3,
    # so the allow-list lives on the socket unit and this process can run with IPAddressDeny=any.
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        uvicorn.run(app, fd=3, limit_concurrency=MAX_CONNECTIONS)
    else:
        uvicorn.run(app, host=HOST, port=PORT, limit_concurrency=MAX_CONNECTIONS)
