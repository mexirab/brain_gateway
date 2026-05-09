"""
Wyoming Protocol Bridge for Jessica TTS

Bridges Home Assistant's Wyoming protocol to the HTTP-based Qwen3-TTS server
(Jessica voice clone).

Wyoming clients (like voice_assistant ESPHome devices) connect here,
send text → this bridge calls the HTTP TTS API → returns audio over Wyoming.

Usage:
    python wyoming_jessica_bridge.py --uri tcp://0.0.0.0:10301 \
        --tts-url http://tts-host:8002 --voice jessica

The --tts-url flag (or TTS_URL env in compose) is required.
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
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

logger = logging.getLogger(__name__)


class JessicaTtsHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        tts_url: str,
        voice: str,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.wyoming_info_event = wyoming_info.event()
        self.tts_url = tts_url
        self.voice = voice

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            return True

        if Synthesize.is_type(event.type):
            synthesize = Synthesize.from_event(event)
            text = synthesize.text.strip()
            if not text:
                return True

            voice = self.voice
            if synthesize.voice and synthesize.voice.name:
                voice = synthesize.voice.name

            logger.info("Synthesizing: %s (voice=%s)", text[:80], voice)

            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{self.tts_url}/tts",
                        json={"text": text, "voice": voice},
                    )
                    resp.raise_for_status()

                # Parse WAV to extract raw PCM and audio params
                wav_bytes = resp.content
                with io.BytesIO(wav_bytes) as buf:
                    with wave.open(buf, "rb") as wav:
                        rate = wav.getframerate()
                        width = wav.getsampwidth()
                        channels = wav.getnchannels()
                        raw_pcm = wav.readframes(wav.getnframes())

                # Stream audio back over Wyoming protocol
                await self.write_event(
                    AudioStart(rate=rate, width=width, channels=channels).event()
                )

                chunk_size = 1024 * width * channels
                for offset in range(0, len(raw_pcm), chunk_size):
                    chunk = raw_pcm[offset : offset + chunk_size]
                    await self.write_event(
                        AudioChunk(
                            audio=chunk, rate=rate, width=width, channels=channels
                        ).event()
                    )

                await self.write_event(AudioStop().event())
                logger.info("Sent %d bytes of audio (%d Hz, %d-bit)", len(raw_pcm), rate, width * 8)

            except httpx.HTTPStatusError as e:
                logger.error("TTS API error: %s", e.response.status_code)
            except Exception as e:
                logger.error("TTS synthesis failed: %s", e)

            return True

        return True


async def main():
    parser = argparse.ArgumentParser(description="Wyoming bridge for Jessica TTS")
    parser.add_argument("--uri", default="tcp://0.0.0.0:10301", help="Wyoming server URI")
    parser.add_argument(
        "--tts-url",
        default=os.environ.get("TTS_URL", ""),
        help="Jessica TTS HTTP endpoint (required; or set TTS_URL env)",
    )
    parser.add_argument("--voice", default="jessica", help="Default voice name")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if not args.tts_url:
        logger.error("--tts-url is required (or set TTS_URL env)")
        sys.exit(2)
    logger.info("Starting Wyoming Jessica TTS bridge on %s → %s", args.uri, args.tts_url)

    wyoming_info = Info(
        tts=[
            TtsProgram(
                name="jessica-tts",
                description="Jessica voice (Qwen3-TTS clone)",
                attribution=Attribution(
                    name="Brain Gateway",
                    url="https://github.com/ConvivialProphet/brain",
                ),
                installed=True,
                version="1.0.0",
                voices=[
                    TtsVoice(
                        name="jessica",
                        description="Jessica McCabe voice clone",
                        languages=["en-US"],
                        attribution=Attribution(
                            name="Brain Gateway",
                            url="https://github.com/ConvivialProphet/brain",
                        ),
                        installed=True,
                        version="1.0.0",
                    )
                ],
            )
        ]
    )

    server = AsyncServer.from_uri(args.uri)
    await server.run(
        partial(JessicaTtsHandler, wyoming_info, args.tts_url, args.voice)
    )


if __name__ == "__main__":
    asyncio.run(main())
