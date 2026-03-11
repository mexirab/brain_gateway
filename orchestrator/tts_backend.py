"""
TTS Backend Abstraction Layer.

Translates between the internal TTS interface used by _announce_voice()
and the actual provider API format (local HTTP, ElevenLabs, Piper, OpenAI).

Call sites do NOT change. They still call _announce_voice(text, speaker).
The backend handles API translation transparently.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class TTSConfig:
    """Configuration for a TTS endpoint."""
    backend: str          # "local_http", "elevenlabs", "piper", "openai"
    url: str = ""         # base URL (for local/piper backends)
    voice: str = ""       # voice name or ID
    api_key: str = ""     # for cloud APIs
    model: str = ""       # for OpenAI (tts-1, tts-1-hd)


class TTSBackend(ABC):
    """Abstract base for TTS API backends."""

    def __init__(self, config: TTSConfig, http_client: httpx.AsyncClient):
        self.config = config
        self._http = http_client

    @property
    def audio_format(self) -> str:
        """MIME type of audio output. Override in subclasses that return non-WAV."""
        return "audio/wav"

    @property
    def file_extension(self) -> str:
        """File extension for audio output."""
        return "wav"

    @abstractmethod
    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        """
        Generate audio from text.

        Args:
            text: Text to synthesize.
            voice: Override voice (uses config default if None).

        Returns:
            Audio bytes in the format indicated by audio_format property.
        """
        ...

    async def health_check(self) -> bool:
        """Check if the TTS backend is reachable."""
        try:
            r = await self._http.get(f"{self.config.url}/health", timeout=5)
            return r.status_code == 200
        except Exception:
            return False


class LocalHTTPBackend(TTSBackend):
    """
    Backend for local HTTP TTS servers (Qwen3-TTS, etc.).
    This is the current behavior — POST to {url}/tts with JSON payload.
    """

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        target_voice = voice or self.config.voice
        r = await self._http.post(
            f"{self.config.url}/tts",
            json={"text": text, "voice": target_voice},
            timeout=60,
        )
        r.raise_for_status()
        return r.content


class ElevenLabsBackend(TTSBackend):
    """
    Backend for ElevenLabs TTS API.

    Uses v1/text-to-speech/{voice_id} endpoint.
    Returns mp3 audio.
    """

    @property
    def audio_format(self) -> str:
        return "audio/mpeg"

    @property
    def file_extension(self) -> str:
        return "mp3"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        target_voice = voice or self.config.voice
        r = await self._http.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{target_voice}",
            headers={
                "xi-api-key": self.config.api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    async def health_check(self) -> bool:
        """ElevenLabs has no simple health endpoint; check if api_key is set."""
        return bool(self.config.api_key)


class PiperBackend(TTSBackend):
    """
    Backend for Piper TTS (local CPU, free tier).

    Compatible with wyoming-piper HTTP mode or standalone Piper server.
    No GPU required.
    """

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        target_voice = voice or self.config.voice
        r = await self._http.get(
            f"{self.config.url}/api/tts",
            params={"text": text, "voice": target_voice},
            timeout=60,
        )
        r.raise_for_status()
        return r.content


class OpenAITTSBackend(TTSBackend):
    """
    Backend for the OpenAI TTS API.

    Uses /v1/audio/speech endpoint.
    Voices: alloy, echo, fable, onyx, nova, shimmer.
    Returns mp3 audio.
    """

    @property
    def audio_format(self) -> str:
        return "audio/mpeg"

    @property
    def file_extension(self) -> str:
        return "mp3"

    async def synthesize(self, text: str, voice: Optional[str] = None) -> bytes:
        target_voice = voice or self.config.voice or "nova"
        model = self.config.model or "tts-1"
        r = await self._http.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {self.config.api_key}"},
            json={
                "model": model,
                "input": text,
                "voice": target_voice,
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    async def health_check(self) -> bool:
        """OpenAI has no simple health endpoint; check if api_key is set."""
        return bool(self.config.api_key)


# ---------------------------------------------------------------------------
# Factory + Registry
# ---------------------------------------------------------------------------

_BACKENDS = {
    "local_http": LocalHTTPBackend,
    "elevenlabs": ElevenLabsBackend,
    "piper": PiperBackend,
    "openai": OpenAITTSBackend,
}


def create_tts_backend(config: TTSConfig, http_client: httpx.AsyncClient) -> TTSBackend:
    """Create a TTS backend instance from config."""
    cls = _BACKENDS.get(config.backend)
    if not cls:
        raise ValueError(
            f"Unknown TTS backend: '{config.backend}'. "
            f"Must be one of: {list(_BACKENDS.keys())}"
        )
    return cls(config, http_client)
