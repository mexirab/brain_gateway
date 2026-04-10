# Voice Assistant & TTS

## Voice Assistant (ATOM Echo S3R)

Hands-free "Hey Jess" voice control via M5Stack ATOM Echo S3R (ESP32-S3).

```
"Hey Jess" (on-device microWakeWord)
    -> ATOM Echo S3R (ESPHome voice_assistant)
    -> Home Assistant voice pipeline
    -> Wyoming Whisper STT (Docker on Jupiter :10300)
    -> HA Conversation Agent -> Brain Gateway :8888
    -> Wyoming Jessica TTS bridge (:10301) -> Uranus TTS :8002
    -> ATOM Echo S3R speaker (or Google speakers group)
```

**Current status:**
- Office ATOM Echo S3R: flashed, online, wake word working
- Voice pipeline: currently routed to Nemotron directly (TODO: route through orchestrator for hybrid LLM)
- TTS output: currently on ATOM Echo tiny speaker (TODO: route to Google speakers group)
- No programmable RGB LED on S3R variant (GPIO35 conflicts with PSRAM)

**Key components:**
- **Wake word:** `hey_jess.tflite` runs on-device (ESP32-S3 only, not original ATOM Echo)
- **Wake word manifest:** `hey_jess.json` served via nginx model-server at http://10.0.0.195:8080/hey_jess.json
- **STT:** `wyoming-faster-whisper` (base-int8 model, CPU on Helios)
- **TTS bridge:** `wyoming-jessica-tts` bridges Wyoming protocol -> HTTP Jessica TTS on Helios
- **ESPHome:** `ha_automations/atom_echo.yaml` — multi-room via substitutions

**Multi-room deployment:**
```bash
esphome run atom_echo.yaml -s name atom-echo-office -s friendly_name "Office Jess"
esphome run atom_echo.yaml -s name atom-echo-bedroom -s friendly_name "Bedroom Jess"
```

**Key files:** `ha_automations/atom_echo.yaml`, `ha_automations/hey_jess.tflite`, `ha_automations/hey_jess.json`, `tts/wyoming_jessica_bridge.py`, `tts/Dockerfile.wyoming-jessica`

## TTS Pacing

Jessica voice clone uses Qwen3-TTS on Uranus. Two pacing controls:

1. **Open WebUI split:** `AUDIO_TTS_SPLIT_ON=paragraph` — splits on `\n\n` for balanced chunks
2. **Sentence pauses:** `inject_sentence_pauses()` in `/home/labadmin/server.py` on Uranus — inserts `...` between sentences for calmer delivery

```bash
# Restart TTS after pacing changes
ssh labadmin@100.102.29.14 "ssh labadmin@10.0.0.173 'sudo systemctl restart qwen-tts'"
```

TTS announcements support per-speaker targeting via `_announce_voice(text, speaker="media_player.bedroom_pair")`. Reminders default to `REMINDER_SPEAKER`, morning briefing defaults to bedroom pair. Speaker fallback (`FALLBACK_SPEAKER`) catches both HTTP errors and connection-level errors (e.g., speaker offline/unreachable).

## Reminder Voice Retry

Voice-only reminder failures (`set_reminder` with `target="voice"`) auto-retry once after 2 minutes. If the retry also fails, the system falls back to a phone notification so the reminder is never silently lost.
