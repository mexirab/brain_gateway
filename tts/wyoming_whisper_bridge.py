"""
Wyoming Protocol Bridge for Whisper STT

Bridges Home Assistant's Wyoming STT protocol to the HTTP-based STT server
(currently Parakeet TDT v3, formerly Whisper large-v3).

Wyoming clients (HA voice pipelines) connect here,
send audio → this bridge forwards to the HTTP STT API → returns transcript.

Usage:
    python wyoming_whisper_bridge.py --uri tcp://0.0.0.0:10300 \
        --stt-url http://stt-host:8003 --language en

The --stt-url flag (or STT_URL env in compose) is required.
"""

import argparse
import asyncio
import io
import logging
import os
import sys
import wave
from functools import partial

import httpx
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

logger = logging.getLogger(__name__)


class WhisperBridgeHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        stt_url: str,
        language: str,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.wyoming_info_event = wyoming_info.event()
        self.stt_url = stt_url
        self.language = language
        self._audio_bytes = b""
        self._rate = 16000
        self._width = 2
        self._channels = 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            return True

        if Transcribe.is_type(event.type):
            # Reset audio buffer for new transcription
            self._audio_bytes = b""
            return True

        if AudioStart.is_type(event.type):
            audio_start = AudioStart.from_event(event)
            self._rate = audio_start.rate
            self._width = audio_start.width
            self._channels = audio_start.channels
            self._audio_bytes = b""
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self._audio_bytes += chunk.audio
            return True

        if AudioStop.is_type(event.type):
            # All audio received — send to STT server
            if not self._audio_bytes:
                await self.write_event(Transcript(text="").event())
                return True

            logger.info(
                "Transcribing %d bytes of audio (%d Hz, %d-bit)",
                len(self._audio_bytes),
                self._rate,
                self._width * 8,
            )

            try:
                # Wrap raw PCM in a WAV container for the HTTP API
                wav_buffer = io.BytesIO()
                with wave.open(wav_buffer, "wb") as wav:
                    wav.setnchannels(self._channels)
                    wav.setsampwidth(self._width)
                    wav.setframerate(self._rate)
                    wav.writeframes(self._audio_bytes)
                wav_buffer.seek(0)

                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        f"{self.stt_url}/v1/audio/transcriptions",
                        files={"file": ("audio.wav", wav_buffer, "audio/wav")},
                        data={
                            "model": "whisper-1",
                            "language": self.language,
                            "response_format": "json",
                        },
                    )
                    resp.raise_for_status()

                text = resp.json().get("text", "").strip()
                logger.info("Transcribed: %s", text[:80])
                await self.write_event(Transcript(text=text).event())

            except httpx.HTTPStatusError as e:
                logger.error("STT API error: %s", e.response.status_code)
                await self.write_event(Transcript(text="").event())
            except Exception as e:
                logger.error("STT transcription failed: %s", e)
                await self.write_event(Transcript(text="").event())

            self._audio_bytes = b""
            return True

        return True


async def main():
    parser = argparse.ArgumentParser(description="Wyoming bridge for Whisper STT")
    parser.add_argument("--uri", default="tcp://0.0.0.0:10300", help="Wyoming server URI")
    parser.add_argument(
        "--stt-url",
        default=os.environ.get("STT_URL", ""),
        help="STT HTTP endpoint (required; or set STT_URL env)",
    )
    parser.add_argument("--language", default="en", help="Default language")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if not args.stt_url:
        logger.error("--stt-url is required (or set STT_URL env)")
        sys.exit(2)
    logger.info("Starting Wyoming STT bridge on %s → %s", args.uri, args.stt_url)

    wyoming_info = Info(
        asr=[
            AsrProgram(
                name="whisper-uranus",
                description="STT bridge (Parakeet/Whisper)",
                attribution=Attribution(
                    name="Brain Gateway",
                    url="https://github.com/ConvivialProphet/brain",
                ),
                installed=True,
                version="1.0.0",
                models=[
                    AsrModel(
                        name="large-v3",
                        description="Whisper large-v3 (GPU-accelerated)",
                        languages=["en"],
                        attribution=Attribution(
                            name="OpenAI",
                            url="https://github.com/openai/whisper",
                        ),
                        installed=True,
                        version="large-v3",
                    )
                ],
            )
        ]
    )

    server = AsyncServer.from_uri(args.uri)
    await server.run(partial(WhisperBridgeHandler, wyoming_info, args.stt_url, args.language))


if __name__ == "__main__":
    asyncio.run(main())
