# Voice Assistant & TTS

## Voice Assistant (ATOM Echo S3R)

Hands-free "Hey Jess" voice control via M5Stack ATOM Echo S3R (ESP32-S3).

```
"Hey Jess" (on-device microWakeWord)
    -> ATOM Echo S3R (ESPHome voice_assistant)
    -> Home Assistant voice pipeline
    -> Wyoming Whisper STT (Docker on Jupiter :10300)
    -> HA Conversation Agent -> Brain Gateway :8888
    -> Wyoming TTS bridge (:10301, container `wyoming-jessica-tts`) -> Helios TTS :8002
    -> ATOM Echo S3R speaker (or Google speakers group)
```

**Current status:**
- Office ATOM Echo S3R: flashed, online, wake word working
- Voice pipeline: HA Conversation Agent calls the orchestrator (`:8888`), which runs the unified loop on RadixArk/Qwen3.8-27B-NVFP4 (vLLM 0.27.1, since 2026-09-28; Lorbus/Qwen3.6-27B-int4-AutoRound 2026-04-26 → 2026-09-28; Qwen3.5-27B on llama.cpp before that). No Nemotron — that v6 hybrid path was removed.
- TTS output: currently on ATOM Echo tiny speaker (TODO: route to Google speakers group)
- No programmable RGB LED on S3R variant (GPIO35 conflicts with PSRAM)

**Key components:**
- **Wake word:** `hey_jess.tflite` runs on-device (ESP32-S3 only, not original ATOM Echo)
- **Wake word manifest:** `hey_jess.json` lives in `/opt/gateway_mvp/models/`. Was served via an nginx `model-server` Docker container at `http://10.0.0.195:${SERVICE_MODEL_SERVER_PORT}/hey_jess.json` until 2026-04-26 when the unfinished service was removed (port 8080 collided with the primary LLM, then llama-server, now vllm-primary). Re-enable by uncommenting the wake-word block in `models/atom-echo-jess.yaml:171`, restoring the `model-server` entry in `docker-compose.yml` on a non-conflicting port, and reflashing via ESPHome.
- **STT:** `wyoming-faster-whisper` (base-int8 model, CPU on Helios)
- **TTS bridge:** `wyoming-jessica-tts` (container/program-name retained for HA wiring back-compat) bridges Wyoming protocol -> HTTP TTS server on Helios. Default voice is configurable via `TTS_VOICE` env / `user_profile.yaml` `voice:` field; live deployment uses a personal reference clone (`voice: "jessica"`).
  - **Fresh install note:** the Wyoming bridge advertises a voice named `jessica` (wire ID, kept for back-compat); the compose default `--voice` is `default`. Either set `TTS_VOICE=jessica` in `.env` (matching the wire ID) AND `POST /voices/load` to register a `jessica` voice on your TTS server, OR change the advertised wire ID in `tts/wyoming_jessica_bridge.py` to match your registered voice name. Without alignment, HA TTS calls return HTTP 400 from the TTS server with no audio (silent on the HA side).
- **ESPHome:** `ha_automations/atom_echo.yaml` — multi-room via substitutions

**Multi-room deployment:**
```bash
esphome run atom_echo.yaml -s name atom-echo-office -s friendly_name "Office Jess"
esphome run atom_echo.yaml -s name atom-echo-bedroom -s friendly_name "Bedroom Jess"
```

**Key files:** `ha_automations/atom_echo.yaml`, `ha_automations/hey_jess.tflite`, `ha_automations/hey_jess.json`, `tts/wyoming_jessica_bridge.py`, `tts/Dockerfile.wyoming-jessica`

## TTS Pacing

Voice clone (personal reference on the live deployment; configurable elsewhere) runs through Qwen3-TTS (`Qwen3-TTS-1.7B-Base`) on Helios (GPU1 RTX PRO 5000, port 8002). The GPU1 pin comes from a systemd drop-in, `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28; before that the unit's `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES` silently put it on the 5090 next to the brain). Two pacing controls:

1. **Open WebUI split:** `AUDIO_TTS_SPLIT_ON=paragraph` — splits on `\n\n` for balanced chunks
2. **Sentence pauses:** `inject_sentence_pauses()` in `/home/labadmin/server.py` on Helios — inserts `...` between sentences for calmer delivery

```bash
# Restart TTS after pacing changes
ssh labadmin@10.0.0.195 'sudo systemctl restart qwen-tts'
```

TTS announcements support per-speaker targeting via `_announce_voice(text, speaker="media_player.bedroom_pair")`. Reminders default to `REMINDER_SPEAKER` (may be comma-separated for multi-room broadcast). Morning briefing defaults to bedroom pair.

## Reminder Voice Retry

Voice-only reminder failures (`set_reminder` with `target="voice"`) auto-retry once after 2 minutes. If the retry also fails, the system falls back to a phone notification so the reminder is never silently lost.

## HTTP STT Server (port 8003)

Separate from the Wyoming bridge above. This is the OpenAI-compatible HTTP STT used by Open WebUI's browser mic and any orchestrator-side transcription via `STT_URL=http://host.docker.internal:8003`.

**Live engine:** `stt-onnx.service` — NVIDIA Parakeet TDT 0.6b **v2**, int8, ONNX Runtime on **CPU**. Same port and same endpoints (`/health`, `/transcribe`, `/v1/audio/transcriptions`). Source: `tts/stt_server_onnx.py` + `tts/stt-onnx.service` (deployed on Helios as `/home/labadmin/stt_server_onnx.py`).

**Disabled GPU alternative:** `parakeet-stt.service` — Parakeet TDT v3 (`nvidia/parakeet-tdt-0.6b-v3`) via NeMo on GPU1. It replaced Whisper medium on 2026-04-26 with no API change; English-only, ~10× faster than Whisper medium with lower WER per the wrapper docstring. Swap back with `systemctl disable --now stt-onnx && systemctl enable --now parakeet-stt` (they share port 8003 — never run both). Runbook: `tts/README_PARAKEET.md`.

| Property (`parakeet-stt`, disabled) | Value |
|----------|-------|
| Service | `parakeet-stt.service` (systemd, disabled) |
| Wrapper | `tts/stt_server_parakeet.py` |
| Unit file | `tts/parakeet-stt.service` |
| Port | 8003 |
| Model | `nvidia/parakeet-tdt-0.6b-v3` (`PARAKEET_MODEL`) |
| Device | `cuda:0` from process POV (`PARAKEET_DEVICE`); pinned to physical GPU1 via `CUDA_VISIBLE_DEVICES=1` (GPU0 belongs to the brain) |
| VRAM | ~6.3 GB on GPU1 |
| Audio normalization | ffmpeg → 16 kHz mono PCM WAV (handles webm/ogg/mp4/wav) |
| Upload cap | 25 MB (matches OpenAI Whisper API) |

**Old service:** the Whisper HTTP STT server (`whisper-stt.service` + `tts/stt_server.py`) was stopped + `systemctl disable`d on 2026-04-26 and **removed from the repo on 2026-06-16** (restore from git history if ever needed). The Wyoming bridge layer (port 10300, used by HA voice pipeline) still uses `wyoming-faster-whisper` and is independent of this HTTP STT swap — it was **not** removed.

```bash
# Restart STT (live ONNX/CPU engine)
ssh labadmin@10.0.0.195 'sudo systemctl restart stt-onnx'

# Logs
ssh labadmin@10.0.0.195 'journalctl -u stt-onnx -f'
```
