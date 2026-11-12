# Voice Assistant & TTS

## Voice satellite (ATOM Echo → Jess)

An ESPHome voice device (M5Stack ATOM Echo S3R, ESP32-S3) talks to Jess **without Home Assistant's voice pipeline**; HA only plays the reply. Code: `orchestrator/voice_satellite.py` (module docstring is authoritative). Firmware: `models/atom-echo-jess.yaml`. **Default off** (`VOICE_SATELLITE_ENABLED=false`). Status 2026-11-10: firmware flashed OTA to the real device, "Hey Jess" verified on-device (7/7 wakes, confidence 0.97–1.00). 2026-11-11, after the first live tests: VAD rebuilt for the measured room (below) and a firmware lock-up fixed (Firmware rules).

```
"Hey Jess" (micro_wake_word on the device) → beep
  -> 16 kHz mono PCM over the ESPHome native API (:6053, noise-encrypted with the device API key)
  -> voice_satellite.py (Jupiter): energy VAD (EnergyVad, learns the room's level each run) ends the utterance
  -> RUN_END to the device (it goes straight back to wake-word listening)
  -> STT on Helios (STT_URL /v1/audio/transcriptions)
  -> cloud_brain.chat(messages, stream=False, is_voice=True)   one Jess voice turn, voice tool subset
  -> reminder_manager._announce_voice(...) -> VoxCPM2 (TTS_URL) -> HA play_media on VOICE_SATELLITE_SPEAKER
```

- **Connection:** outbound from the orchestrator to the device; no ports opened on Jupiter (`handle_start` returns port 0 = audio over the API connection). `expected_name` = `VOICE_SATELLITE_NAME` pins the node name so a DHCP reshuffle can't point it at another ESPHome device. The real device check is the noise key: anyone holding it (a lost device, the build box's `secrets.yaml`) can pose as the device on the LAN. Rotate it if lost.
- **Lifecycle:** forever asyncio task started in `orchestrator.py` startup (like the Telegram bot; handle `shared.voice_satellite_task`), cancelled at shutdown; `log_loop_exit` logs an unexpected exit at ERROR. Reconnects with backoff 5 s → 120 s (back to 5 s after a session that stayed up > 60 s).
- **Not in HA:** the device must NOT be added/enabled in HA's ESPHome integration. It takes one voice-assistant client; HA and Jess would fight over it.
- **Hardware:** the on-device wake word needs the ESP32-S3 (not the original ATOM Echo). No programmable RGB LED on the S3R (GPIO35 conflicts with PSRAM). Mic and speaker share one I2S bus.

### Event contract (bridge → device)

| Event | When |
|-------|------|
| `RUN_START` + `STT_START` | first audio chunk arrives |
| `STT_VAD_START` | VAD confirms speech |
| `RUN_END` **alone** | utterance captured, nothing said, no audio, or 30 s run wall clock |
| `VOICE_ASSISTANT_ERROR` `{code}` | an outcome the device should make audible (below). Also sent after the run has ended; ESPHome still fires `on_error` |

**Gotcha:** never send `STT_VAD_END` before `RUN_END`. It leaves ESPHome 2026.x in STOP_MICROPHONE when `RUN_END` arrives, which ignores it; the device then sits in AWAITING_RESPONSE and never listens again (verified in ESPHome `voice_assistant.cpp`, 2026.10). From STREAMING_MICROPHONE, `RUN_END` stops the mic and goes IDLE.

| Code | Means | Sound (firmware `on_error`) |
|------|-------|------------------------------|
| `jess-no-speech` | nothing (or no words) heard after the wake word | `mute_switch_on.flac` |
| `jess-busy` | a turn from this device is still running, or the rate limit hit | `center_button_double_press.flac` |
| `jess-waking` | Helios asleep (woken) or still booting; the answer comes when it's up. Not a failure, the turn continues | `center_button_long_press.flac` |
| `jess-asleep` | Helios asleep and `HELIOS_WAKE_ENABLED` off | `jack_disconnected.flac` (same as error) |
| `jess-error` | anything else failed (STT, brain, TTS, speaker, wake) | `jack_disconnected.flac` |

Wake beep: `wake_word_triggered.flac`. Sounds are the Home Assistant Voice PE set, pinned to commit `a5429c2b` (CC BY 4.0, attributed in the YAML). The mapping is a first guess; swap files by ear.

### Listening (VAD)

`VadConfig` / `EnergyVad` in `voice_satellite.py`; their docstrings and comments are authoritative. Input: one level per 50 ms frame, RMS around the frame's own mean (DC removed), 0..1 of full scale.

| Parameter | Value |
|-----------|-------|
| Smoothing | every decision uses the 200 ms moving mean of frame RMS ("smoothed level") |
| Room level (`floor`) | 25th percentile of the smoothed level over the last 2 s. Starts at 0.045; nothing learned from the first 300 ms (mic start-up); held between 0.036 and 0.045 for the first 1.5 s (words straight after the beep can't become "the room"); frozen while speaking; never below 0.003 |
| Speech start | smoothed ≥ `max(0.03, floor × 1.8)`, with ≥ 2 single frames above that bar in the last 200 ms (rejects clicks), for 150 ms |
| End of utterance | for 1.2 s no smoothed level reached the speech bar **and** the mean frame level over those 1.2 s < `max(0.015, floor × 1.35)` |
| Cap | 15 s, then processed as is (`max_utterance`) |
| Nothing said | 5 s → `jess-no-speech` |
| Backstops | no audio within 4 s → `no_audio` (`jess-error`); run wall clock 30 s (`wall_timeout`) |

**Why (2026-11-11):** measured on the device (per 50 ms frame), the room in silence reads 0.025–0.09 (median 0.042: fans, air, mic hiss) and a spoken question 0.19–0.24. The first VAD, a port of the dashboard's browser-mic VAD (`frontend/src/lib/vad.ts`), capped its learned floor at 0.015, so its end-of-speech bar (0.027) sat below the room itself: every question ran to the 15 s cap (answer ~15 s late) or was missed as no-speech. Grafana looked healthy, because a capped run still counts as an `ok` turn; `bgw_satellite_listen_end_total{reason}` now shows it. Simulated on the measured levels (400 runs per case): questions of 0.8–10 s caught 100 % with no cut-offs, end decided ~1.25–1.35 s after the last word; a quiet voice 95–99.5 %; nobody speaking ≥ 99.75 % correctly `no_speech`. Rejected alternatives: D13.

**Not the cause (don't re-check):** ESPHome's `voice_assistant` `noise_suppression_level` / `auto_gain` / `volume_multiplier` are only hints sent to the server (HA applies them); the device streams raw audio and the bridge ignores them, so they were removed from the firmware. The mic's DC offset was ~0; the firmware keeps `correct_dc_offset: true` and `frame_rms` removes the frame mean anyway.

**Diagnosing:** each run logs `listening ended (<reason>)` with the numbers the VAD decided on (room level, both bars, peak, tail mean). Fields, metric, LogQL: `monitoring/README.md` → Voice Satellite. **New room:** measure the room's levels first (bridge off); much louder than ~0.045 means expect `max_utterance` (Known limits).

### A turn

1. **Helios check (D5):** `stt`, `model`, `tts` via `service_registry.check_service` (3 s each). All up → go. Nothing on the brain's host answering and the plug says off → wake (`HELIOS_WAKE_ENABLED`), `jess-waking`, wait ≤ `VOICE_SATELLITE_HELIOS_TIMEOUT_SECONDS` (180) → else `wake_timeout`. Only services whose URL host matches `MODEL_URL`'s count as proof that Helios is on (`Satellite._on_brain_host`), so the always-on Saturn TTS answering while Helios sleeps doesn't skip the wake. Only a different **IP literal** counts as another box: the live `.env` uses IPs for `MODEL_URL`, `STT_URL` and `TTS_URL`. Hostnames (compose DNS names, or a name against an IP) may be one machine, so they count as the brain's host, as before. If Helios is not powered and a service on another box is down too, the turn fails as `service_down` at once without a wake: waking Helios can't make the turn work. Helios on but a service down (boot, brain reload, crashed unit) → no wake, `jess-waking`, wait 90 s → else `service_down`. Wake off → `jess-asleep`. The plug is only read with `HELIOS_WAKE_ENABLED`, so with wake off a booting Helios also counts as asleep.
2. **STT** (30 s). No words → `jess-no-speech`.
3. **Brain:** `cloud_brain.chat(..., stream=False, is_voice=True)` with the short history (D7); 300 s backstop (`CHAT_TIMEOUT_S`). Counts in `bgw_voice_pipeline_seconds{path="buffered"}` like any buffered voice turn.
4. **Speak:** `speakable()` keeps link text, drops URLs and markdown marks, caps at 1200 chars on a sentence end. One clip: `_announce_voice(spoken, speaker=VOICE_SATELLITE_SPEAKER, announcement_type="voice_reply", ignore_voice_session=True, ignore_dnd=True, style=current_reply_style(), allow_fallback=False, record_history=False)` (120 s).
5. Only a turn whose reply played goes into the follow-up history.

One turn per device at a time (else `jess-busy`); at most 6 turns per 5 min per device (`rate_limited`, `jess-busy`). Device button: short press = push-to-talk (wake word label `button`); 10 s press = factory reset.

### Decisions

| # | Decision | Why / consequence |
|---|----------|-------------------|
| D1 | Reply plays on the room's Google speaker via HA `play_media`, not on the device | The ATOM Echo speaker is tiny and shares the I2S bus with the mic; the device plays only short sounds |
| D2 | Turn = in-process `cloud_brain.chat(..., is_voice=True)`, not HTTP to `/v1/chat/completions` | Same brain call the dashboard makes. The route only spots voice from HA's IP or the STT beacon, so a direct call is exact. No API token in the bridge |
| D3 | One TTS clip per reply | One `play_media` per answer. The first word waits for the whole reply plus its synthesis (≤ 1200 chars) |
| D4 | Answers even under sleep mode (`ignore_dnd`) | You asked out loud, so you want the answer. Announcements and reminders stay muted |
| D5 | Helios asleep → wake it and answer late, capped | `jess-waking` says the answer is coming; capped by `VOICE_SATELLITE_HELIOS_TIMEOUT_SECONDS` |
| D6 | Reply bypasses the mid-conversation guard (`ignore_voice_session`) | Each turn calls `shared.mark_voice_activity()` so reminders hold off while you talk; without the bypass the reply would hold itself off |
| D7 | Short history: RAM, ≤ 12 messages, reset after 5 min of silence, only what was actually spoken | Follow-ups ("and tomorrow?") work; a restart loses it |
| D8 | **Full access** (owner decision 2026-11-10) | Below |
| D9 | Privacy | Below |
| D10 | `VOICE_SATELLITE_SPEAKER` required; no fallback to other rooms (`allow_fallback=False`) | Replies play under sleep mode, so they must never land in another room. Speaker unavailable in HA → `speaker_unavailable` + `jess-error` |
| D11 | No multi-device arbitration | One device, env-configured. Two devices hearing one wake word would both answer |
| D12 | Wake words run on the device only, never server-side | "Hey Jess" = local model (`models/hey_jess.json` + `.tflite`, the trainer's manifest: `probability_cutoff` 0.97, `tensor_arena_size` 30000). On the device (2026-11-11) the Hey Jess model also fires on "Okay Jess" (0.98–0.99) and on other "-ay Jess" phrases ("Play Jess", "May Jess"); owner-accepted, so it covers both wake words alone. A dedicated "Okay Jess" model (`models/okay_jess.json` + `.tflite`, microwakeword.com 2026-11-10, "robust" variant, manifest as delivered: `probability_cutoff` 0.35 = its benchmark threshold, recall 86.2% / 0.38 false accepts per hour in hard conditions; cutoff scale not comparable with Hey Jess's) is kept in the repo but commented out in the YAML: Hey Jess always fired first, so it only added a second way to wake by accident. The stock `okay_nabu` model was a bring-up control, removed 2026-11-10: it is said in many public HA videos, an easy false/remote trigger |
| D13 | VAD = loudness only; the room level is learned fresh every run and frozen while speaking (2026-11-11) | Rejected in review, don't re-propose: (a) a "steady level = it's the room" escape hatch — speech from this device is steady too (smoothed p90/p25 ≈ 1.3), so it cut off questions longer than ~3.5 s; (b) remembering each room's level between runs — a cut-off run, a TV, Jess's own reply or a muted run can poison it, and it didn't help in the tuning room. Telling a loud room from speech needs a spectral VAD |

**D8 — full access.** Room voices get the same voice tool subset as dashboard voice (`tool_definitions.VOICE_TOOL_NAMES`, filtered by the usual feature flags), and anyone in the room can use it: a visitor, a TV, a radio, someone on speakerphone. That includes reading meds/projects/profile (`get_data`) and memories (`search_memory`), storing a "fact" (`update_memory`), reading and creating calendar events (`check_calendar`, `create_calendar_event`), setting and cancelling reminders, logging meds/meals as done (`selfcare_log`, `log_meal`), sleep mode (mutes announcements until morning), focus sessions, routines (`start_routine`), tasks, shopping list, pantry/meal plan, web search, and device control (`home_assistant`, still behind `ha_guard` and the `ha_intent` did-the-user-ask gate, which checks that the transcript asked, not who asked). The meds block and IDENTITY CONTEXT are in every system prompt anyway, and every answer is spoken aloud in the room. Safeguards: these turns never feed auto_learn (`cloud_brain.skip_auto_learn_var`); replies are not written to `announcement_history` (not in the dashboard history, not replayed into `/chat`, and ha_intent's yes-path to Jess's own question still works — it treats history rows as announcements); replies play only on the room speaker; 6 turns / 5 min. Known limit, accepted by the owner.

**D9 — privacy.** Mic audio is held in RAM for one utterance and never written to disk. The spoken reply goes the announcement way: an audio file in `/tmp/brain_audio`, served to the Cast speaker by an unguessable URL (`/api/audio/<uuid8>.<ext>`, bearer-exempt) for up to an hour. Logs (`[SATELLITE]`) carry lengths, levels, outcomes, stage timings and the fixed wake-word label, never the transcript or the reply (`tests/test_no_user_content_in_logs.py`). The `aioesphomeapi` / `zeroconf` loggers are held at WARNING, and `log_config.DropPayloadArgsFilter` drops their arguments and exception text (their records can carry raw audio bytes).

### Setup

1. Deploy the change first (the `VoiceSatelliteDisconnected` alert and the Grafana row ship with it).
2. Give the device a DHCP reservation. `VOICE_SATELLITE_HOST` must be an IP; `.local` names are rejected.
3. Remove the device from HA's ESPHome integration (or don't add it when HA discovers it).
4. Build and flash the firmware (below) with a fresh `api_encryption_key`.
5. Jupiter `.env`: `VOICE_SATELLITE_ENABLED=true`, `VOICE_SATELLITE_HOST`, `VOICE_SATELLITE_API_KEY` (same value as the firmware's `api_encryption_key`), `VOICE_SATELLITE_NAME` (the node name the device runs), `VOICE_SATELLITE_SPEAKER` (e.g. `media_player.office_speaker`). Also needs `STT_URL`, `TTS_URL`, `HA_URL`, `HA_TOKEN`. Full table: `docs/ENV_VARS.md` → Voice satellite.
6. `docker compose up -d --force-recreate orchestrator`. A bad config logs `[CONFIG] VOICE_SATELLITE_ENABLED=true but …; disabling the voice satellite.` and stays off.
7. Check: `docker logs brain-orchestrator 2>&1 | grep SATELLITE` shows `connected, listening for runs`; "Hey Jess, what time is it?" answers on the room speaker.

Turn off: `VOICE_SATELLITE_ENABLED=false` + `docker compose up -d --force-recreate orchestrator`.

### Firmware build + flash

`models/atom-echo-jess.yaml`: no HA bits; "Hey Jess" as a local model file (it also wakes on "Okay Jess"); wake word listens from boot; `api: reboot_timeout: 0s` (no reboot every 15 min while Jess isn't connected); `on_error` maps the `jess-*` codes to sounds; node name and friendly name are substitutions. Validated and compiled with ESPHome 2026.10.2.

Build dir: `atom-echo-jess.yaml`, `hey_jess.json`, `hey_jess.tflite` (plus `okay_jess.json` + `okay_jess.tflite` only if that model is uncommented) (from `models/`) and a `secrets.yaml` (gitignored) with `wifi_ssid`, `wifi_password`, `api_encryption_key`, `ota_password`, `ap_password`. The fallback-AP password used to be a literal in the YAML (still in git history), so use a new value.

```bash
# api_encryption_key / VOICE_SATELLITE_API_KEY: base64 of 32 random bytes
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'

# Build + flash over the air (<dir> = the build dir above)
docker run --rm -it -v <dir>:/config -v <dir>/.cache:/cache ghcr.io/esphome/esphome:stable \
  -s name <node> run atom-echo-jess.yaml --device <ip>

# Keep an existing unit's identity (VOICE_SATELLITE_NAME must match the name):
esphome -s name atom-echo-office -s friendly_name "Office Mic" run atom-echo-jess.yaml --device <ip>
```

First flash of a blank device: USB (web.esphome.io). After that, OTA. A new API key = reflash + `.env` + recreate the orchestrator. Firmware is always flashed by hand; CI never flashes devices.

**Firmware rules (lock-up fix, 2026-11-11).** In the first live test a second "Hey Jess" mid-run deadlocked the device: the speaker retried "Parent bus is busy" forever, and the device stayed connected (alert green) but deaf. Causes: the media player's `on_idle` restarted the wake word during the voice run, and the wake-detected automation waited for its beep with no timeout. Mic and speaker share one I2S bus, so the YAML now follows these rules (keep them when editing it):

- Stop the wake word, and wait for it to stop, before any sound. `play_sound` also waits for the voice assistant to release the mic, and stops the wake word twice (a start already pending survives one stop).
- Every wait is bounded; a sound that can't finish is cancelled, so the media player can't keep retrying it.
- `start_wake_word` refuses while the speaker amp is on. No `on_idle` restart: whoever plays a sound restarts the wake word when done.
- `on_end` waits for the voice assistant to go idle and leaves the restart to `play_sound` when a sound is playing. `on_client_disconnected` restarts the wake word (a run cut by a disconnect goes idle without `on_end`/`on_error`). Push-to-talk waits for the wake word to stop. A run that never starts (Jess not connected) goes back to the wake word.
- Self-heal `interval:` (15 s): nothing running for two checks in a row → restart the wake word (WARN log); a voice run still "running" after ~60 s → `voice_assistant.stop`.

### False wakes

- **Signal:** a wake followed by nothing said is almost always false. `sum by (wake_word) (increase(bgw_satellite_turn_total{result=~"no_speech|empty_transcript"}[24h]))` against all wakes; Grafana "Wakes vs. False Wakes by Wake Word (24h)". `{helios="woken",result="empty_transcript"}` = Helios booted for nothing.
- **Tune:** `probability_cutoff` in the wake word's manifest (`models/hey_jess.json`), useful range 0.95–0.99: higher = fewer false wakes, more missed ones. Rebuild + OTA.
- **Still bad:** retrain the model with more negatives from the room (TV, radio).
- The rate limit (6 turns / 5 min) caps the damage from a TV that keeps waking it.

### Known limits

- One device, env-configured; no "two devices heard it" arbitration (D11).
- Energy VAD can't tell the user from a TV or another person talking.
- A room much louder than ~0.045 RMS (TV on ≈ 0.075) false-starts and records to the 15 s cap (`max_utterance`, answer ~15 s late): loudness alone can't separate a loud room from steady speech (D13). Before enabling a satellite in a new room, measure the room's levels first; if it is much louder than ~0.045, expect `max_utterance`.
- No follow-up listening: every question needs the wake word (or the button).
- A turn that hits `CHAT_TIMEOUT_S` (300 s) may already have run tools (reminder set, light switched) before the device plays the error sound.
- No error sound when nothing is connected (orchestrator down, bridge off): ESPHome logs `No API client connected`, so you hear the wake beep, then silence.
- The reply audio sits in `/tmp/brain_audio`, reachable by its unguessable URL, like any announcement.
- Anyone in the room has full access (D8). `jess-asleep` and `jess-error` share a sound.

**Tests:** `orchestrator/tests/test_voice_satellite.py` (fake ESPHome client, respx STT/HA, injected brain; VAD cases built from the room levels measured 2026-11-11; the listen-end counter + log line; also the `_announce_voice` flags, `skip_auto_learn_var`, the config validator, `DropPayloadArgsFilter`, shutdown). Needs an orchestrator image built with `aioesphomeapi`.

**Monitoring:** metrics, `VoiceSatelliteDisconnected`, Grafana row: `monitoring/README.md` → Voice Satellite.

### HA Assist pipeline (Wyoming; the ATOM Echo no longer uses it)

HA's own voice pipeline, for HA Assist and for installs that flash `ha_automations/atom_echo.yaml` (mic-only template, TTS redirected to a Cast speaker by HA) instead of the satellite firmware:

```
wake word on the device -> HA voice pipeline
    -> Wyoming Whisper STT (:10300, wyoming-faster-whisper)
    -> HA Conversation Agent -> Brain Gateway :8888
    -> Wyoming TTS bridge (:10301, container `wyoming-jessica-tts`) -> TTS (`TTS_URL`; gets no audio from the Saturn server)
```

- **Known gap:** the HA Conversation Agent still points at `http://10.0.0.195:8888` (Helios); the orchestrator has lived on Jupiter (`http://10.0.0.248:8888`) since the HA migration, so HA Assist turns fail at the conversation step until the agent's base URL is repointed in the HA UI (Settings → Voice assistants → the Jess conversation agent). The ATOM Echo is not affected: it goes through the voice satellite.
- **TTS bridge:** `wyoming-jessica-tts` (name kept for HA wiring back-compat) bridges Wyoming → the HTTP TTS server at `TTS_URL`. Since the Saturn move it gets no audio: the Saturn server admits Jupiter only (see VoxCPM2 on Saturn). Default voice: `TTS_VOICE` env / `user_profile.yaml` `voice:`; the live deployment uses a personal reference clone (`voice: "jessica"`).
  - **Fresh install note:** the bridge advertises a voice named `jessica` (wire ID, kept for back-compat); the compose default `--voice` is `default`. Either set `TTS_VOICE=jessica` in `.env` (matching the wire ID) AND `POST /voices/load` to register a `jessica` voice on your TTS server, OR change the advertised wire ID in `tts/wyoming_jessica_bridge.py` to match your registered voice name. Without alignment, HA TTS calls return HTTP 400 from the TTS server with no audio (silent on the HA side).
- `ha_automations/hey_jess.json` still carries the old manifest values (`probability_cutoff` 0.5); `models/hey_jess.json` has the trainer's.

**Key files:** `ha_automations/atom_echo.yaml`, `ha_automations/hey_jess.{json,tflite}`, `tts/wyoming_jessica_bridge.py`, `tts/Dockerfile.wyoming-jessica`

## TTS Pacing

Qwen3-TTS (`Qwen3-TTS-1.7B-Base`) on Helios (GPU1 RTX PRO 5000, port 8002) is the rollback TTS — **disabled since 2026-10-24**: the Qwen3.8-Flash-Next brain uses its ~4.5 GiB on GPU1, so it only comes back with the 27B brain rollback (`tts/llama-server-primary-27b-q6k.service` header). The live orchestrator voice is VoxCPM2 on :8004 (see VoxCPM2 TTS server below). The pacing notes here are Qwen-specific. The GPU1 pin comes from a systemd drop-in, `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28; before that the unit's `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES` silently put it on the 5090 next to the brain). Pacing control:

- **Sentence pauses:** `inject_sentence_pauses()` in `/home/labadmin/server.py` on Helios — inserts `...` between sentences for calmer delivery

```bash
# Restart TTS after pacing changes (only when qwen-tts is re-enabled for a rollback)
ssh labadmin@10.0.0.195 'sudo systemctl restart qwen-tts'
```

TTS announcements support per-speaker targeting via `_announce_voice(text, speaker="media_player.bedroom_pair")`. Without an explicit `speaker=`, the target comes from the `/settings` Speakers panel (`announcement_routes.route_for(type)`), else the legacy `REMINDER_SPEAKER` / `MORNING_BRIEFING_SPEAKER` / `FOCUS_AUDIO_PLAYER` env vars (comma-separated = multi-room broadcast). Categories: `selfcare`, `reminder`, `calendar`, `ambient`, `progress`, `focus`, `briefing`, `temperature` (closet heat alerts; panel row "Server closet temperature"). `temperature` was added after the panel shipped: left unset it follows the panel `default` → `reminder` route (then legacy `REMINDER_SPEAKER`), as before it had a row.

Other `_announce_voice` flags (defaults keep the announcement behaviour; the voice satellite sets all of them):

| Flag | Default | Effect |
|------|---------|--------|
| `ignore_voice_session` | `False` | Play even while a voice session holds announcements (see Reminder Voice Retry) |
| `ignore_dnd` | `False` | Play under Do Not Disturb. Only for the reply to something just asked out loud (voice satellite, D4); proactive announcements never set it |
| `style` | `None` | TTS style note instead of the announcement-type default |
| `allow_fallback` | `True` | `False` = report failure when every target is unavailable instead of falling back to the reminder speakers (like `manual`) |
| `record_history` | `True` | `False` = metrics only, no `announcement_history` row (`_record_announcement(..., persist=False)`) |

### Speaker liveness (dead-speaker skip + fallback)

HA returns 200 for `play_media` on an `unavailable` Cast entity, so a dead speaker used to log as delivered. `_announce_voice` now checks each target's HA state BEFORE synthesizing TTS:

| HA state result | Treated as |
|-----------------|-----------|
| `unavailable` or 404 | dead — skipped |
| `unknown`, `off`, any other state, HA error/timeout | live — attempted |

- If every routed speaker is dead, falls back to the live reminder-route speakers — for every `announcement_type` except `manual` (`/api/announce` naming a speaker), which fails honestly (HTTP 502), and calls with `allow_fallback=False` (voice satellite replies).
- Return dict may carry `unavailable` (dead targets) and `fallback` (speakers used instead).
- Speaker ids must match `media_player\.[a-z0-9_]+` — enforced at announce time, in the Speakers panel validator (`announcement_routes._validate_speaker_string`), and on a routine's `speaker` (`routines_config`). The state GET URL-encodes the entity id.
- Metrics: `bgw_announcement_speaker_unavailable_total{speaker}` (configured speakers, else `other`; pre-seeded at 0 by `seed_announcement_metrics` at startup) and `bgw_announcement_fallback_total{type}`. Alert `AnnouncementSpeakerUnavailable` — see `monitoring/README.md`.
- Fix for a dead speaker is physical: power-cycle it; a stereo pair that doesn't return in HA needs re-pairing in Google Home.

## VoxCPM2 TTS server (port 8004)

The live voice, replacing Qwen3-TTS (:8002). It ran on Helios GPU1 from 2026-10-15 and moves to **Saturn's RTX 3090** (see VoxCPM2 on Saturn below) so the brain can use GPU1 for its 262144 context. Jupiter `.env` then has `TTS_URL=http://10.0.0.58:8004`. The Helios unit stays installed but disabled, as rollback. `qwen-tts` on :8002 is an older rollback, disabled since 2026-10-24. Chosen over a Qwen3-TTS clone and IndexTTS-2.5 by listening A/B; also Apache-2.0 vs IndexTTS's bilibili license.

| Property | Value |
|----------|-------|
| Unit / source | `voxcpm-tts.service` / `tts/voxcpm_server.py`. Repo copies: `tts/voxcpm-tts-saturn.service` (Saturn, live), `tts/voxcpm-tts.service` (Helios, rollback), `tts/voxcpm_server.py` |
| Model | `openbmb/VoxCPM2` (Apache-2.0, 2B), weights copied to `/home/labadmin/models/VoxCPM2`, loaded offline; HF revision + sha256s in the unit header |
| GPU / VRAM | Saturn RTX 3090 24 GB, picked by UUID in `/etc/voxcpm-tts.env`. ~7.4 GiB fresh; the PyTorch cache grows to ~9.7 GiB after long texts (measured on Helios) and is never released, which is why the 10 GB RTX 3080 was ruled out (owner decision 2026-11-12). On Helios it shared GPU1 with part of the brain, which then decoded at ~30–85 instead of ~135–180 tok/s while it synthesized |
| Speed | ~0.4–1.3 s per sentence on the Helios RTX PRO 5000 (Saturn: compare with the check below); output 24 kHz WAV (`VOXCPM_OUTPUT_SR`, same rate as Qwen) |
| Startup | first-ever start ~50 s on Helios, a few minutes on a new box (torch.compile); warm ~15–30 s — compile cache in `/var/cache/voxcpm`, persists across boots |
| API | Same as `tts/server.py`: `/health`, `/voices`, `/tts` (honors `emotion`), `/v1/audio/speech` (honors OpenAI `instructions`) |
| Voices | Same `voices.json` as the Qwen server (`VOXCPM_VOICES_DIR`); only `ref_audio` is used — reference-only cloning keeps the timbre, not the clip's mood, so the style note can steer |
| Logs | `journalctl -u voxcpm-tts` on Saturn (text never logged, lengths only). **Not in Loki or the self-audit:** Saturn runs no promtail. On Helios this unit's journal was shipped by promtail-helios. Follow-up: promtail on Saturn |

**Style note:** `emotion` / `instructions` becomes a VoxCPM2 controllable-cloning prefix, `(calm, gentle and reassuring)text`. No note = the model infers prosody from the text.

**Server guards:** `()` / `[]` and their NFKC lookalikes are stripped from body text (so text can't inject a style note); text capped at 2000 chars (`VOXCPM_MAX_CHARS`), style at 80; more than 8 pending requests → 503 (`VOXCPM_MAX_PENDING`); `/health` returns 503 when no voice is usable; refuses to start without CUDA (`VOXCPM_REQUIRE_CUDA=true`).

**Sandbox** (mirrors `llama-server-primary`). On Saturn: nologin `voxcpm` user, `IPAddressAllow` Jupiter only (no loopback), bound to the LAN address only (the server is unauthenticated and speaks in the cloned voice, and Saturn is on the tailnet), `ProtectHome=tmpfs` hides the Jess backups Saturn holds. On Helios: nologin `llama` user, `ProtectSystem=strict`, `ProtectHome=tmpfs` + read-only binds of the venv, uv Python, voices and weights dirs, `DevicePolicy=closed`. `DeviceAllow` **must include `/dev/nvidia0`** even though it is pinned to GPU1 — CUDA opens it while enumerating, and without it torch silently falls back to CPU (~7 s/sentence). Install steps (uv-managed Python 3.11 venv, pinned `voxcpm`/torch cu128, world-readable paths): unit header.

Env vars: `VOXCPM_*` — `docs/ENV_VARS.md` → TTS.

**Original cutover, Qwen3-TTS → VoxCPM2 on Helios (done, history; the Saturn move is below):**
```bash
cp .env .env.bak-qwen-tts
# set TTS_URL=http://10.0.0.195:8004 in .env
docker compose up -d --force-recreate orchestrator
docker logs brain-orchestrator 2>&1 | grep '\[TTS\] Backend'   # confirm :8004
```
Rollback to Qwen3-TTS: only together with the 27B brain rollback (`qwen-tts` is disabled and no longer fits next to Flash-Next) — re-enable `qwen-tts`, set `TTS_URL` back to :8002, `docker compose up -d --force-recreate orchestrator`. (`.env.bak-qwen-tts` predates later `.env` changes, incl. the 2026-10-24 model names + `VOICE_THINKING_ENABLED`; don't `cp` it over a Flash-Next `.env`.) A Wyoming bridge still pointed at :8002 gets no audio now. The Wyoming bridge (`wyoming-jessica-tts`) reads `--tts-url ${TTS_URL}` from the `.env` of the compose host it runs on. Since the Saturn move it can't reach the voice (only Jupiter is allowed, see VoxCPM2 on Saturn), so HA Assist has no Jess voice.

```bash
ssh labadmin@10.0.0.58 'sudo systemctl restart voxcpm-tts'    # Saturn: the live voice
ssh labadmin@10.0.0.58 'journalctl -u voxcpm-tts -f'
# Never start voxcpm-tts on Helios while the 262K brain unit is installed (its ExecStartPre refuses)
```

**Monitoring:** blackbox probe `service="tts-voxcpm"` on `${NODE_SATURN_IP}:8004/health` feeds `TTSDown` (quiet Pushover; counts while Saturn's node-exporter is up; a Saturn outage pages as `NodeDown`; `monitoring/README.md` → Routing coverage). The Qwen3-TTS :8002 probe (`service="tts"`) was removed 2026-10-24 when `qwen-tts` was disabled — restore it in `monitoring/prometheus/prometheus.yml.template` if it is re-enabled. No server-side `/metrics` yet.

### VoxCPM2 on Saturn (RTX 3090): move + cutover runbook

**Why:** at the brain's native 262144 context, the brain needs ~42.9–43.7 GiB of Helios GPU1. VoxCPM2 (7.4 GiB fresh, up to ~9.7 GiB) no longer fits beside it (measurements: header of `tts/llama-server-primary.service`). Moving the voice also stops speech from slowing the brain's decode.

**What the voice depends on afterwards:** Helios (brain, STT) **and** Saturn (TTS). STT stays on Helios's CPU, because moving it frees no VRAM.
- A Saturn outage pages as `NodeDown`. Saturn was down for 8.75 days in the 30 days before the move (2026-10-29 → 11-07), and no Helios TTS failure happened while Helios was up.
- **If Saturn stays down for more than ~1 h, run the rollback** (~3 min, see Rollback below) to get the voice back on Helios at 64K.
- **Behavior change:** announcements that failed while Helios slept will now speak. That was 377 in the 30 days before the move: selfcare 218, calendar 100, routine 30, ambient 24, and others. Quiet hours and DND still apply.
- **Alerts, all quiet:**
  - `TTSDown`: the server doesn't answer `/health`.
  - `TTSSynthesisFailing`: it answers, but synthesis keeps failing.
  - `TTSGPUMemoryLow`: more than 16 GiB used on the 3090 (almost always an experiment beside the voice).
  - `SaturnGpuDriverBroken`.
- `VOXCPM_HOST=10.0.0.58` needs Saturn's DHCP reservation on the router; confirm it exists.

**Only Jupiter may call the Saturn server.** Its `IPAddressAllow` is `10.0.0.248/32` alone, and the server binds the LAN address only.
- The Wyoming bridge (`wyoming-jessica-tts`, :10301, no auth) loses TTS after the move. That breaks the HA Assist voice path, which already doesn't work (see CLAUDE.md known gaps).
- Never "fix" this by adding Helios or the LAN to the allow-list: that turns the bridge into an open relay for the cloned voice.
- Don't run the bridge on Jupiter either. `docker compose up -d` without service names would start it there, publishing :10301 to the LAN, and Saturn would admit its traffic.

**1. Install (the owner, on Saturn):** no hardware change; the voice goes on the RTX 3090 already in Saturn (owner decision 2026-11-12; the 10 GB RTX 3080 is too small, see the GPU / VRAM row above, and becomes the experiment card). Step 0 also removes the retired `expert-model` container, which is bound to the 3090 and would put ~20 GiB on the voice card if anyone started it. Follow steps 0–7 in the header of `tts/voxcpm-tts-saturn.service`. Until the PR is merged, the two new files exist only on its branch, so step 2 takes them from there. Everything is copied from the working Helios install and checked by sha256. Packages are pinned in `tts/requirements-voxcpm.txt`.

**2. Compare with Helios before cutting over** (both servers live, run on Jupiter). Generic sentences only. Run the speed loop three times and compare the middle value per sentence: one run is noisy.
```bash
for h in 10.0.0.195 10.0.0.58; do
  for s in "Good morning, it is seven o'clock." "Your timer for the oven is done." \
           "Here is a quick summary of the weather for today: mild, with some sun in the afternoon." \
           "You have two things on the calendar tomorrow, and the first one starts at nine."; do
    curl -s -o /dev/null -w "$h %{time_total}s\n" -H 'Content-Type: application/json' \
      -d "{\"text\":\"$s\",\"voice\":\"jessica\"}" http://$h:8004/tts
  done
done
# Memory: texts of several lengths up to the server's 2000-char maximum, with and without a style
# note (the peak grows with text length: the VAE decodes the whole clip at once), then check memory on Saturn
S="This is a long test sentence for the voice server. "
for n in 6 16 27 39 39; do
  for e in "" "calm, gentle and reassuring"; do
    T=$(python3 -c "print('$S' * $n)")
    curl -s -o /dev/null -w "$((n*51)) chars, style='$e': HTTP %{http_code} %{time_total}s\n" \
      -H 'Content-Type: application/json' \
      -d "{\"text\":\"$T\",\"voice\":\"jessica\",\"emotion\":\"$e\"}" http://10.0.0.58:8004/tts
  done
done
ssh -t labadmin@10.0.0.58 'nvidia-smi --query-gpu=name,memory.used,memory.free --format=csv; sudo journalctl -u voxcpm-tts --since -15min | grep -ci "out of memory"'
# Every run must print HTTP 200 (an out-of-memory synthesis is a 500), and the count must be 0
```
Pass:
- **Speed:** each short sentence on Saturn takes at most ~1.5x its Helios time, and none over ~2 s. The 3090 has ~70 % of the RTX PRO 5000's memory bandwidth and less compute, and Saturn's CPU is slower (3.7 vs 5.6 GHz), so expect it to be slower; on Helios the voice also shared its GPU with the brain. Too slow: don't cut over yet. Either accept it after a listening test in dashboard call mode, or keep the voice on Helios at 64K and decide again;
- **Long texts:** the ~2000-char runs finish in under ~45 s. The orchestrator gives up on a TTS call at 60 s, so a slower long text would cut off a long briefing;
- **Memory:** every run returns HTTP 200, the journal shows no CUDA out-of-memory error, and the 3090 shows at most 14 GiB used (`TTSGPUMemoryLow` fires above 16 GiB). Write down the used memory after the ~2000-char runs: PyTorch keeps the peak cached. It can still creep over time (on Helios it grew 7.3 → 9.3 GiB in one week of texts under 610 chars), so a week after cutover check the 7-day peak in Prometheus (Jupiter): `max_over_time((nvidia_smi_memory_used_bytes{node="saturn"} and on(uuid) nvidia_smi_gpu_info{node="saturn", name=~".*3090.*"})[7d:5m]) / 2^30`;
- the owner listens to a styled reply and it sounds the same: `curl ... -d '{"text":"...","voice":"jessica","emotion":"calm, gentle and reassuring"}'`.

**3. Cutover** (~5 min; chat and voice are offline for ~1 min while the brain restarts):
1. Merge the PR. CI deploys Jupiter: the probe moves to Saturn, and the orchestrator is rebuilt with the satellite wake fix.
2. Jupiter (`/home/labadmin/gateway_nerves`):
   ```bash
   cp .env .env.bak-tts-helios
   # set TTS_URL=http://10.0.0.58:8004 in .env
   docker compose up -d --force-recreate orchestrator
   docker logs brain-orchestrator 2>&1 | grep '\[TTS\] Backend'   # confirm 10.0.0.58:8004
   scp tts/llama-server-primary.service tts/voxcpm-tts.service labadmin@10.0.0.195:/tmp/
   ```
3. Helios (`ssh labadmin@10.0.0.195`; the 64K unit is already backed up as `/etc/systemd/system/llama-server-primary.service.64k.bak`):
   ```bash
   sudo systemctl disable --now voxcpm-tts
   # The new voxcpm-tts.service has an ExecStartPre guard: it refuses to start while the 262K brain
   # unit is installed, so a stray `systemctl restart voxcpm-tts` can't squeeze the brain. (`mask`
   # doesn't work here: the unit file lives in /etc/systemd/system.)
   sudo install -m 644 /tmp/llama-server-primary.service /tmp/voxcpm-tts.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl restart llama-server-primary
   nvidia-smi   # expect GPU0 ~29.1 GiB, GPU1 ~42.9 GiB, no python process
   ```
4. Verify:
   - **vision at 262K**, never measured with a full-size photo: send a full-resolution phone photo in dashboard chat, once at the start of a chat and once after a long one. After each, `nvidia-smi` on Helios must still show at least 1 GiB free on GPU0. If not, roll back, or try `-ub 256` (another ~1.2 GiB free on GPU0, 26 % slower long prefill);
   - a spoken reply in dashboard chat;
   - a test announcement;
   - one satellite turn;
   - `probe_success{service="tts-voxcpm"}` = 1, and `ALERTS{alertname=~"BrainFailedToStart|TTSDown|TTSSynthesisFailing"}` is empty;
   - note a new `bgw_chat_ttft_seconds` baseline: the first turn after a Helios wake is a cold prefill, ~16 % slower on long prompts at `-ub 384`;
   - the Grafana TTS latency panel, against the Helios baseline (30 days before the move): `bgw_voice_tts_seconds` p50 1.69 s, p95 4.64 s, p99 7.81 s; `bgw_tts_latency_seconds` p50 1.79 s, p95 4.96 s, p99 8.8 s;
   - brain decode no longer drops while a reply is spoken.

**Rollback** (~3 min): header of `tts/llama-server-primary.service`. In order: Helios 64K unit + `voxcpm-tts` back on, then Jupiter `TTS_URL` back, then (in a PR) point the probe back at Helios and move `'tts'` back to Helios in `frontend/src/lib/constants.ts`. `TTSDown` follows the probe's `node` label, so it needs no change.

**Moving the voice to another card later** (a 16 GB RTX 5080, which needs a new PSU): on Saturn put the new card's UUID in `/etc/voxcpm-tts.env` and run `sudo systemctl restart voxcpm-tts`, then re-run the memory test above. In a PR, change `TTSGPUMemoryLow` (`monitoring/prometheus/alert-rules.yml`): the card name (expr and summary) to 5080, and the used-memory line to what the test measured plus ~1 GiB, since 16 GiB can never be exceeded on a 16 GB card. A name that matches no card silently never fires. Move the experiment pin to whichever card the voice leaves.

**Saturn experiments must be pinned to the 3080.** llama.cpp's auto-fit and most tools use every GPU they can see, and CUDA numbers the 3090 first (`cuda:0`, `docker run --gpus all`) while `nvidia-smi` lists the 3080 first, so pin by UUID only, never by index. Put `export CUDA_DEVICE_ORDER=PCI_BUS_ID` and `export CUDA_VISIBLE_DEVICES=GPU-<3080 uuid>` at the top of `~/.bashrc` on Saturn, above the line that returns early for non-interactive shells, so `ssh saturn 'cmd'` gets them too; check with `ssh labadmin@10.0.0.58 'echo $CUDA_VISIBLE_DEVICES'`. That doesn't reach Docker containers, cron jobs or systemd units: give those `--gpus device=GPU-<3080 uuid>` or their own `CUDA_VISIBLE_DEVICES`. An unpinned experiment shares the voice card. It slows synthesis, and one that fills the card makes synthesis fail while `/health` stays green. `TTSGPUMemoryLow` (quiet, more than 16 GiB used on the 3090) and `TTSSynthesisFailing` (quiet, 5 failures in 30 min) catch most of that; a co-tenant under ~6 GiB only shows as slower speech. Publish experiment servers on `127.0.0.1` (`-p 127.0.0.1:PORT:PORT`), not on every interface: Saturn also holds the voice clip and the off-box backups. Check which card each process is on with `nvidia-smi --query-compute-apps=gpu_name,pid,process_name,used_memory --format=csv`.

## Voice style

How Jess *sounds* (not what she says), chosen per utterance by `orchestrator/voice_style.py` and sent to the TTS as a style note. Qwen3-TTS accepts and ignores it, so it is harmless on the :8002 rollback (disabled since 2026-10-24).

**Presets** (the only phrases that ever reach the TTS — never free text from the user, model or content): `auto` (no note), `calm`, `gentle`, `soothing`, `quiet`, `warm`, `thoughtful`, `encouraging`, `upbeat`, `proud`, `energetic`, `firm`, plus acting tones `excited`, `sad`, `angry`, `scared`, `whisper`, `sultry`, `sarcastic` (`ACTING_PRESETS`) — 19 keys incl. `auto`. The tool's `style` enum is built from `PRESETS` (minus `auto`).

**Resolution** (highest first):
1. **One-reply style** — set by the tone-request detector (below) OR by `voice_style` `scope=this_reply` (the default): "say it angry", "say that like you're scared", "how would you say it if you were sad", "softer for the next response". RAM-only, cleared when the next user message is routed (`record_reply_style`) or after 300 s (`REPLY_STYLE_TTL_SECONDS`). Chat replies only, never announcements. Not audited.
2. **Ongoing override** — `scope=ongoing` ("talk softer from now on", "be upbeat for an hour"; `duration_hours` implies ongoing). Stored in `app_state` key `voice_style_override` (survives restarts), optional expiry ≤ 168 h. Audited in `config_changes` as panel `voice_style_override`. Clearable from Settings. An ongoing **acting** preset applies to chat replies only — announcements/reminders fall back to their per-type default (a `whisper` override can't make a med reminder inaudible). The Settings panel can still assign acting presets per announcement type (explicit owner choice).
3. **Defaults** — `/settings` → Voice Style (`/api/config/voice_style`); YAML at `VOICE_STYLE_PATH` stores only non-default entries (audited as panel `voice_style`).

| Mode (chat replies) | Default | | Announcement type | Default |
|------|---------|-|------|---------|
| explainer | auto | | progress | proud |
| mirror | thoughtful | | greeting, briefing | warm |
| counterbalance | gentle | | selfcare | gentle |
| challenge | firm | | routine | encouraging |
| baseline | soothing | | ambient | quiet |
| | | | all others (incl. unknown types) | auto |

**Where it applies:**
- Chat replies: `cloud_brain` records the mode_router mode of the latest message (`record_reply_style`); the dashboard `/api/tts/synthesize`, Telegram voice replies and the `/v1/audio/speech` proxy read `current_reply_style()`. The proxy strips any client-sent `instructions` first.
- Announcements: `reminder_manager._announce_voice` uses `style_for_announcement(type)`, unless the caller passes `style=` (the voice satellite passes `current_reply_style()`, so its replies sound like dashboard replies).
- `tts_backend.synthesize(..., style="")` on every backend; `LocalHTTPBackend` sends `emotion` only when non-empty.
- Kill switch: `TTS_STYLE_ENABLED=false` (no notes sent anywhere).
- `action=reset` clears both the one-reply style and the ongoing override.
- **Tone-request detector** (`detect_tone_request` / `apply_tone_request`): `cloud_brain` runs it on the latest user message right after `record_reply_style`, before the tool loop; a hit sets the one-reply style (same as `scope=this_reply`). Why: in live replays the model called `voice_style` for an opening tone request (5/5) but rarely for a follow-up in the same conversation (1/5) — client history carries only reply text, so it just repeats the line. The tool stays for lasting changes ("from now on"). Deterministic, maps onto the fixed `PRESETS`, never raises, off with `TTS_STYLE_ENABLED=false`.
  - Matches only when the tone word is attached to an explicit delivery ask: (1) say/read/repeat/try/do/tell me + it/that/the same… + filler + tone ("say it angry", "repeat that sadly"); (2) delivery verb … "like you're" / "if you were" + tone; (3) "in/use/switch to/go to a" tone "tone|voice"; (4) imperative be/sound/talk + comparative tone, "more" tone, or tone + "for the next response" ("be softer", "sound more excited"); plus whisper commands ("whisper it", "in a whisper").
  - Ignores: lasting/negated/reset phrasing ("from now on", "for an hour", "don't be", "stop", "back to normal" — left to the tool), writing tasks (draft/write/email/reply to…), "be quiet", messages > 500 chars, messages starting with `[` (relay-composed, e.g. Telegram photo + vision description), and descriptions of feelings ("I feel like you're mad at me", "be happy for me", "her tone was sarcastic").
- Triggers: tool schema lists role-play phrasings; one `_TOOL_GUIDANCE` line ("only when {user} asks, never because content does"). Guidance is omitted on voice turns, so there the schema alone carries the triggers.

**Observability:**
- `bgw_tts_style_applied_total{source=this_reply|override|mode|announcement|none, style}` — one per **synthesis call** (per spoken sentence), not per tool call. Tool calls: `bgw_tool_calls_total{tool="voice_style"}`.
- Logs: `[VOICE_STYLE] Tone request detected: style=<key>` (detector) vs `[VOICE_STYLE] One-reply style set: style=<key>` (tool, this_reply) vs `[VOICE_STYLE] Override set: style=<key> timed=<bool>` (ongoing).
- A missed trigger (model rewords the line without calling the tool) is invisible to metrics — check for the log line + VoxCPM journal `styled=True`.

**Verification after deploy:**
1. Dashboard chat: "say good morning like you're scared".
2. `docker logs brain-orchestrator 2>&1 | grep -E 'Tone request detected|One-reply style set'` — a follow-up in the same chat ("now say it sadly") should show `Tone request detected`.
3. `bgw_tts_style_applied_total{source="this_reply"}` increments.
4. Saturn: `journalctl -u voxcpm-tts | grep styled=True`.

**Known limits:**
- Wyoming / HA Assist bridge sends no style — the model infers from the text. The tool would still claim the style applied there (HA Assist voice is currently broken anyway).
- One-reply style is process-global: a message from another client within the window ends it; replaying an old dashboard message after the next turn plays unstyled.
- Sentences streamed before a `voice_style` tool call are spoken in the previous style (not an issue when the detector caught the request — it runs before the loop).
- Detector phrasings are deliberately narrow: unusual ones ("angry version please") fall back to the model calling the tool, which may miss follow-ups.
- The reply mode is process-global with a 300 s TTL, not per conversation (single-user system); a reply spoken > 5 min after its message gets no mode style.
- Long text is not sentence-split server-side; callers (dashboard, Telegram) send short segments.
- No `/metrics` on the VoxCPM2 server yet — health is the blackbox probe only.

## Dashboard chat voice replies

The dashboard `/chat` page speaks Jess's replies, including on iOS Safari (needs a secure context for the mic — the dashboard is served over HTTPS via Tailscale Serve, tailnet-only).

**Flow:** streamed reply → `lib/speech-text.ts` (strip code blocks, inline code, URLs, link targets, markdown, tables, emoji, lone surrogates; split into sentences as they complete; merge <12-char fragments; `splitLong()` cuts any segment over 400 chars — after a comma/semicolon/em dash past a third of the window, else at a space, else a hard cut that never splits a surrogate pair — so each request stays under the backend cap; spoken budget `SPEECH_MAX_CHARS` = 1200 chars (same as Telegram `TELEGRAM_VOICE_REPLY_MAX_CHARS`), then "The rest is on screen.") → `lib/speech-queue.ts` (per-segment `POST /api/tts/synthesize` while still streaming, ≤2 in flight per tab, ordered playback) → `hooks/useTTSPlayback.ts`.

**iOS autoplay:** WebKit rejects `play()` that runs seconds after the tap (`NotAllowedError` — the old hook swallowed it, so no audio on iPhone). The hook keeps ONE `HTMLAudioElement`, unlocks it by playing a tiny silent WAV synchronously inside the send/mic/speaker-toggle/replay tap, and reuses it for every clip.

**When it speaks:**
- Mic-sent message → always spoken reply (walkie-talkie), even with the speaker toggle off.
- Typed message → follows the speaker toggle (localStorage `tts_enabled`, per device, default off).

**Voice mode (backend):** thinking on voice turns is controlled by `VOICE_THINKING_ENABLED` (default `false`; read at process start, change with `docker compose up -d --force-recreate orchestrator` — see `docs/ENV_VARS.md`). The startup log shows it: `CloudBrain initialized (… voice_thinking=…)`. **Live deployment: `true` since 2026-10-24** — required by the Qwen3.8-Flash-Next brain (see Why the switch); the 27B rollback runs with `false`.

| `VOICE_THINKING_ENABLED` | Voice-turn request | First spoken word (Qwen3.8-27B) | First spoken token (Flash-Next) |
|---|---|---|---|
| `false` (default) | `max_tokens` 1024 + `chat_template_kwargs {"enable_thinking": false}` | ~0.16 s | 0.13 s (not used: unrequested actions) |
| `true` (live) | `max_tokens` 2048, no kwarg; depth set by llama-server `--chat-template-kwargs '{"reasoning_effort":"low"}'` (keep that flag in the unit). 2048 because reasoning tokens count against `max_tokens` on llama.cpp | ~0.77 s | 0.40 s |

- **Why the switch:** some brains act unasked with thinking off. The live brain (Qwen3.8-Flash-Next, since 2026-10-24) in its trial turned off speakers or a light on 4 of 8 voice "tell me a fun fact" turns with thinking off, 0 of 33 with it on, then 0 of 80 in the full no-action probe (voice tool cases 18/18); first spoken token 0.40 s vs 0.13 s. This is a model-behavior mitigation, not a control: the flag defaults `false` and nothing ties it to the running brain, so restoring a pre-2026-10-24 `.env` brings the unrequested actions back. A deterministic "did the user ask?" gate in front of `home_assistant` calls is a separate change.
- **Thinking off caveat:** the Qwen3.8 template pre-fills an empty `<think></think>` and the model can end its turn at once (7/8 when the history held an empty assistant turn; 0/48 with clean history). Mitigations: `cloud_brain` drops empty assistant turns from history, and an empty round is retried once — with thinking on when the switch is off, as a plain resend when it is on. Detail: `TECHNICAL_REFERENCE.md` → Empty-Reply Handling.
- **Thinking on caveat:** a long reasoning pass can use up the 2048 budget and cut the reply. Look for `finish=length` on the `LLM probe (stream): …` / `LLM probe: …` lines in Loki.

**Empty replies (client):** the chat page snapshots history before adding the new turn (the first message of a new chat used to go out twice with a blank assistant turn between) and never sends blank or error bubbles as history. A reply that is still empty shows — and in voice/call mode speaks — "Sorry — I didn't get a reply just now. Please try again."

**Controls:** header Stop pill while speaking; starting a recording stops playback (mic stays enabled while Jess speaks); replay button on finished assistant bubbles (not error bubbles); 44px touch targets; always-mounted sr-only aria-live region announces failures.

| Status | Trigger | UI |
|--------|---------|----|
| `blocked` | `NotAllowedError` | "Tap to play" |
| `asleep` | HTTP 503 (TTS unreachable — the TTS box, Saturn, or its server is down; status name kept from the Helios era) | "Jess's voice is offline — tap to retry" |
| `interrupted` | iOS paused the element (screen lock, call, Siri) | "Voice paused — tap to replay" |
| `error` | other failure, 30 s client timeout | error text |

**Backend:** `POST /api/tts/synthesize` (`orchestrator/routes_vision.py`) — caps 1000 chars / 2000 UTF-8 bytes, status contract in `TECHNICAL_REFERENCE.md` → Voice. Success pre-pads ~150 ms WAV silence (`TTS_SILENCE_PAD_MS`, same as `/v1/audio/speech`) and calls `shared.mark_voice_activity()`, so `reminder_manager`'s voice-session gate holds house-speaker announcements while the browser speaks (reminders: speakers only, at most ~10 min; phone push is immediate — see Reminder Voice Retry).

**Metrics:** `bgw_dashboard_tts_total{result}` (`ok|unreachable|timeout|upstream_error|not_configured|bad_request|too_long`, pre-initialized). `bgw_voice_tts_seconds` observes SUCCESSFUL synth only, fed by `/v1/audio/speech` and `/api/tts/synthesize` (one sample per segment; buckets extend to 15 s and 20 s). Grafana TTS Synthesis row (`brain_gateway_sre.py`): "Dashboard TTS Outcomes" (replaced "TTS Call Rate"), "TTS Sentences (24h)" (was "TTS Calls (24h)"); TTS latency panel thresholds 3 s yellow / 6 s red (per-clip median is ~2–3 s live).

**Tests:** `orchestrator/tests/test_tts_synthesize_route.py`, `frontend/src/lib/speech-text.test.ts`, `frontend/src/lib/speech-queue.test.ts`.

**Known limits:**
- Stop / 503 cancels only the browser fetch — the orchestrator + TTS server still finish up to 2 in-flight sentences.
- TTS server has 2 worker threads: a speaking tab can delay an announcement or Telegram voice reply by ~one sentence.
- 500 emoji (2000 bytes) still pass the server caps (the client strips emoji anyway).
- TTS unreachable (Saturn or its server down): the first sentence waits for the connect failure (typically a few seconds) before "voice is offline".
- ElevenLabs/OpenAI TTS backends: auto-spoken replies are sent to that provider.
- iOS behaviour verified only in Chromium with an injected iOS-like per-element `play()` policy, not yet on a real iPhone.

## Hands-free call mode

Continuous voice conversation in `/chat`: speak, Jess answers aloud, she listens again. Builds on Dashboard chat voice replies above.

**Start:** the AudioLines button next to the mic — one tap creates the `AudioContext` inside the tap (iOS), opens the mic with `echoCancellation`/`noiseSuppression`/`autoGainControl`, and takes a screen wake lock. `components/chat/VoiceCallBar.tsx` replaces the input while in a call (phase label, Interrupt, End; focus moves to End, and back to the call button afterwards).

**VAD (`lib/vad.ts`, `EnergyVad`):**

| Parameter | Value |
|-----------|-------|
| Calibration | 400 ms of room noise, then adaptive noise floor |
| Speech threshold | `max(0.01, floor × 3)` |
| Speech confirm | 200 ms above threshold |
| Utterance end | 1.8 s of silence |
| Cap | 90 s → `speech-timeout`: treated as steady noise (TV): discarded in the browser, never transcribed, its level becomes the new floor |

**Loop (`hooks/useVoiceCall.ts`):** `MediaRecorder` runs while listening, restarted gaplessly every 3 s of idle (≤ ~3 s lead-in rides along with an utterance) → speech end → **mic released** (tracks stopped, source disconnected) → `POST /api/stt/transcribe` via `lib/stt.ts` → sent to chat as a voice message (reply always spoken) → waits for the spoken reply to finish (`SpeechQueue.begin()`/`beginReply()` return a completion promise) → 350 ms → `ensureRunning()` (AudioContext resume, bounded 1.5 s wait) → **mic re-acquired** (new `getUserMedia` into the call-lifetime analyser; VAD calibration kept) → listening again. No barge-in; Interrupt stops her speech, listening resumes once the reply stream finishes.

**Mic released during replies:** while capture is open iOS runs the audio session in voice-processing ("phone call") mode, which made call-mode replies sound crackly/walkie-talkie (mic-button replies were clear). So the mic is open only while listening; the iOS mic indicator turning off during Jess's reply is expected, not a dropped call. Safari 16.4+: `navigator.audioSession.type` = `playback` while released, `play-and-record` before capture. The type is page-global and sticky, so call `end()` (every exit path goes through it) sets it back to `auto`; a session left on `playback` made iOS refuse capture and broke the plain mic button until reload. The mic button also sets `auto` before `getUserMedia`. Helper: `lib/audio-session.ts` (`setAudioSessionType`, no-op without the API) — anything that sets `playback` must hand back `auto`. Empty transcripts back off 1/2/4/5 s.

**Call ends on:** End; page hidden; 5 min with nothing said; STT 503 ("Jess can't hear right now — Helios (speech recognition) is asleep"); 3 consecutive STT failures (single failures are skipped); iOS `AudioContext` interruption not recovering within 1.5 s — checked after the first acquire and before each re-acquire ("The call was paused by your phone — tap the call button to start again."); mic can't be re-acquired after a reply ("Couldn't get the microphone back — tap the call button to start again."); mic track ended.

**Recorder MIME (`lib/stt.ts` `pickRecorderMime`, shared with the mic button's `useVoiceRecorder.ts`):** webm/opus → webm → mp4 (older Safari).

**Backend:** `POST /api/stt/transcribe` (`orchestrator/routes_vision.py`) — status contract in `TECHNICAL_REFERENCE.md` → Voice. Voice activity (`shared.mark_voice_activity()` + next-chat voice flag) is marked only for a non-empty transcript, so a noise false-trigger doesn't hold announcements. Forwarded upload filename/content-type are pinned to an allowlist.

**Metrics:** `bgw_dashboard_stt_total{result}` (`ok|empty|unreachable|timeout|upstream_error|too_large|not_configured`, pre-initialized; counts mic button + call utterances). Grafana TTS Synthesis row: "Dashboard STT Outcomes" next to "Dashboard TTS Outcomes".

**Tests:** `orchestrator/tests/test_stt_transcribe_route.py`, `frontend/src/lib/vad.test.ts`, `frontend/src/lib/speech-queue.test.ts`.

**Known limits:**
- Verified only in Chromium with an injected WAV microphone through the real VAD/MediaRecorder/STT path — not yet on a real iPhone. Open: AirPods (HFP), WebKit analyser behaviour, interruption recovery. Reply playback quality with the mic released between turns is the fix for the iPhone static report — not yet confirmed on the iPhone.
- Re-acquiring the mic each turn adds `getUserMedia` start-up (~0.1–0.8 s on iOS) before listening; a very quick answer can lose its first syllable.
- Safari normally keeps the mic permission for the page, but some setups may re-prompt each turn; dismissing the prompt ends the call.
- Energy VAD can't tell the user from other people: anything transcribed is sent as the user's message. Steady TV-like noise is discarded; conversation in the room is not.
- Interrupt waits for the text stream to finish before listening again.
- A full-budget (1200-char) reply runs ~75–90 s of speech; Interrupt is a tap, not barge-in. Voice turns are normally short (the orchestrator adds a conciseness hint for voice).
- The OpenAI-compatible `/v1/audio/transcriptions` route (not used by `/chat`) still returns an unhandled 500 on STT failure.

## Reminder Voice Retry

Voice-only reminder failures (`set_reminder` with `target="voice"`) auto-retry once after 2 minutes. If the retry also fails, the system falls back to a phone notification so the reminder is never silently lost.

**Suppression (`tool_handlers.deliver_reminder_job`):**

| Gate | Effect |
|------|--------|
| Voice session active (`shared.is_voice_session_active()`, 60 s window refreshed by dashboard STT + each `/api/tts/synthesize` sentence + each voice satellite turn) | Phone channels (HA Companion, ntfy, Pushover, Telegram) delivered in the same run; only the speaker part re-armed +2 min, max `MAX_VOICE_SESSION_DEFERRALS` = 5 (~10 min, kwarg `voice_deferrals`). After the cap: speakers skipped if the push went out, else announced via `_announce_voice(..., ignore_voice_session=True)` (DND still applies). |
| DND active | Whole delivery (phone included) deferred 30 min. If DND starts after a voice-session push already went out, the speaker part is skipped, not replayed when DND lifts. |

A failed re-arm does not strand the reminder `pending`: speakers are dropped if the push went out, else announced immediately. Tests: `orchestrator/tests/test_reminder_delivery.py`.

## HTTP STT Server (port 8003)

Separate from the Wyoming bridge above. This is the OpenAI-compatible HTTP STT behind the orchestrator's `/api/stt/transcribe` + `/v1/audio/transcriptions` routes and any orchestrator-side transcription (the voice satellite, Telegram voice notes) via `STT_URL=http://host.docker.internal:8003`.

**Live engine:** `stt-onnx.service` — NVIDIA Parakeet TDT 0.6b **v2**, int8, ONNX Runtime on **CPU**. Same port and same endpoints (`/health`, `/transcribe`, `/v1/audio/transcriptions`). Source: `tts/stt_server_onnx.py` + `tts/stt-onnx.service` (deployed on Helios as `/home/labadmin/stt_server_onnx.py`). Since 2026-10-27 the unit runs offline (`HF_HUB_OFFLINE=1`: a new `STT_ONNX_MODEL` must be downloaded into the HF cache first, outside the unit) and can't reach the internet (LAN/tailnet/Docker-only IP sandbox) — `docs/internal/HELIOS_INFRASTRUCTURE.md` → Egress audit.

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

## Voice tool subset

Voice turns (the dashboard chat mic, the voice satellite, HA Assist) get a reduced tool list, `tool_definitions.VOICE_TOOL_NAMES`, because every tool schema is prefilled on each turn and voice is latency-sensitive. `prompt_builder` also drops the tool-guidance block on voice, so on voice the model routes by the schema descriptions alone. The voice list is filtered from the already-gated full list, so feature flags still apply. `tests/test_voice_tools.py` pins this down, and also catches a typo in the name list.
- **Voice tool budgets:** While a tool runs for a voice turn, `shared.voice_turn_var` (a ContextVar that `unified_loop._run_one` sets per tool call, so it reaches parallel calls and never leaks into a typed turn) is `True`; tools read it to tighten their budgets. `web_search` (when on the voice list) uses a 5 s whole-request deadline on voice, 15 s typed (SearXNG gives each engine 3 s). A turn that searches in several rounds can still wait rounds × 5 s. The fallback model gets `is_voice` too (buffered `cloud_brain._unified_fallback` since 2026-10-25; the streamed fallback already did), so it keeps the voice generation settings and the 5 s deadline; its tool list stays the full one. Search outcomes, metrics and alerts (`bgw_web_search_total{result,voice}`, `WebSearchFailing`, `SearXNGDown`): `TECHNICAL_REFERENCE.md` → web_search; voice latency (`bgw_voice_pipeline_seconds{path}`): `monitoring/README.md`.

On the voice satellite this whole subset is open to anyone in the room (D8 above). What's included: device control, memory read/write, structured data (`get_data`), brain dump, the shopping list, reminders, calendar read/create, selfcare and meal logging, focus sessions, sleep mode, decision help, routines and tasks. Since 2026-10-14 it also includes **`pantry` and `meal_plan`**, when `PANTRY_ENABLED` is on. Without them a spoken "what can I cook with the ground turkey?" got "I don't have access to your pantry". They add ~2.2k chars of schema (≈0.2 s of extra prefill on a cache miss). Since 2026-10-25 it also includes **`web_search`** (SearXNG) (see below), and **`calculate`** (exact math + date/time math, `TECHNICAL_REFERENCE.md` → calculate): "what's 18% of 64" and "when do I need to leave" are spoken asks, and without the tool the brain did the arithmetic in its head and got it wrong. ~1.4k chars of schema; non-terminal, so Jess speaks the answer from the result.

Notes for the pantry tools on voice:
- The `pantry` description itself carries the hints the dropped guidance used to give: summarise `view` when speaking, and `out` already adds to the grocery list (don't also call `shopping_list`).
- HA Assist sends only the latest utterance, so a two-step "accept the plan?" → "yes" can't work there. Say "accept the meal plan" in one go, or accept on the dashboard.
- `meal_plan swap` never waits for chat to go idle (`meal_planner._ask(..., wait_for_idle=False)`). On non-streaming turns (HA Assist, Telegram) the turn that called the tool is itself counted as active, so waiting would stall the swap for the full 60 s idle timeout.

### web_search on voice (since 2026-10-25)

The dashboard mic, hands-free call mode, the voice satellite and HA Assist can look things up (weather, sports scores, store hours, news). It used to be typed-chat only, left off for latency, and voice said it couldn't look things up 27 of 30 times. No new env vars, routes, metrics or jobs. `tests/test_voice_tools.py::test_web_search_offered_on_voice` keeps it on the voice list under every gating-flag combination.

Measured 2026-10-25 on the live brain (Qwen3.8 Flash-Next, `VOICE_THINKING_ENABLED=true`). The real voice prompt and tool schemas went straight to llama-server; only `web_search` ran for real.

| What | Result |
|------|--------|
| First spoken token, search turns | ~2.0 s median, 3.1 s p95, 4.0 s worst (normal voice turn ~0.5 s) |
| Where the time goes | round 1 (decide to search) ~0.8 s; SearXNG ~0.3 s median; round 2 prefills ~570 tokens |
| Tool choice | 30/30 search questions called `web_search`; 0/30 non-search questions; 0/100 personal questions (meds, calendar, memory) |
| Poisoned search results | 0 extra tool calls (0/16 voice thinking on, 0/10 off). The 27B rollback brain was not measured — re-run before relying on it after a rollback |
| Schema cost | ~725 chars (~180 tokens; ~120 ms only on a prompt-cache miss) |

Behaviour:
- No spoken preamble before searching: ~2 s of silence, then the answer.
- News questions often search 2–3 times.
- List answers (movies, events, news) can run 4–6 sentences. The dashboard TTS (`frontend/src/lib/speech-text.ts`) strips markdown and URLs and caps speech at 1200 chars; the voice satellite does the same (`voice_satellite.speakable`, 1200 chars); HA Assist has no such cleanup.

Known limits (from the SRE and security reviews; not fixed):
- Brave ("too many requests"), DuckDuckGo and Startpage (CAPTCHA) refuse this server's searches on most days (seen on every day with searches since at least 2026-10-14). Results come from the engines that still answer: Google CSE for general queries, Bing News for news. A burst of searches can also briefly leave news queries with no results.
- SearXNG `/healthz` stays 200 while that happens. There is no probe or alert on SearXNG.
- `tool_web_search` returns failures as text, so `bgw_tool_call_errors_total{tool="web_search"}` never rises; empty or failed searches are not counted anywhere.
- SearXNG client timeout is 15 s per search round, with no tighter voice budget. A hung SearXNG can leave a voice turn silent for a long time.
- Streamed (dashboard) voice turns record `bgw_voice_pipeline_seconds` before the tool loop runs, so slow search turns don't show there; only HA Assist turns are really measured.
- SearXNG's own container logs carry query text at WARNING when an engine fails. They stay out of Loki only because the promtail keep regex excludes searxng — keep it that way.
