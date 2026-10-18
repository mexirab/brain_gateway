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
- Voice pipeline: HA Conversation Agent calls the orchestrator (`:8888`), which runs the unified loop on DavidAU Qwen3.8-27B TURBO Fable Q6_K MTP GGUF (llama.cpp `llama-server-primary`, since 2026-10-02; RadixArk/Qwen3.8-27B-NVFP4 on vLLM 0.27.1 2026-09-28 → 2026-10-02; Lorbus/Qwen3.6-27B-int4-AutoRound 2026-04-26 → 2026-09-28; Qwen3.5-27B on llama.cpp before that). No Nemotron — that v6 hybrid path was removed. **Known gap (2026-10-02):** the HA Conversation Agent is still configured with `http://10.0.0.195:8888` (Helios), but the orchestrator has lived on Jupiter (`http://10.0.0.248:8888`) since the HA migration — ATOM Echo voice turns currently fail at the conversation step until the agent's base URL is repointed in the HA UI (Settings → Voice assistants → the Jess conversation agent).
- TTS output: currently on ATOM Echo tiny speaker (TODO: route to Google speakers group)
- No programmable RGB LED on S3R variant (GPIO35 conflicts with PSRAM)

**Key components:**
- **Wake word:** `hey_jess.tflite` runs on-device (ESP32-S3 only, not original ATOM Echo)
- **Wake word manifest:** `hey_jess.json` lives in `/opt/gateway_mvp/models/`. Was served via an nginx `model-server` Docker container at `http://10.0.0.195:${SERVICE_MODEL_SERVER_PORT}/hey_jess.json` until 2026-04-26 when the unfinished service was removed (port 8080 collided with the primary LLM, then llama-server, vllm-primary, now llama-server-primary). Re-enable by uncommenting the wake-word block in `models/atom-echo-jess.yaml:171`, restoring the `model-server` entry in `docker-compose.yml` on a non-conflicting port, and reflashing via ESPHome.
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

Qwen3-TTS (`Qwen3-TTS-1.7B-Base`) on Helios (GPU1 RTX PRO 5000, port 8002) is the rollback TTS; the live orchestrator voice is VoxCPM2 on :8004 (see VoxCPM2 TTS server below). The pacing notes here are Qwen-specific. The GPU1 pin comes from a systemd drop-in, `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28; before that the unit's `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES` silently put it on the 5090 next to the brain). Two pacing controls:

1. **Open WebUI split:** `AUDIO_TTS_SPLIT_ON=paragraph` — splits on `\n\n` for balanced chunks
2. **Sentence pauses:** `inject_sentence_pauses()` in `/home/labadmin/server.py` on Helios — inserts `...` between sentences for calmer delivery

```bash
# Restart TTS after pacing changes
ssh labadmin@10.0.0.195 'sudo systemctl restart qwen-tts'
```

TTS announcements support per-speaker targeting via `_announce_voice(text, speaker="media_player.bedroom_pair")`. Without an explicit `speaker=`, the target comes from the `/settings` Speakers panel (`announcement_routes.route_for(type)`), else the legacy `REMINDER_SPEAKER` / `MORNING_BRIEFING_SPEAKER` / `FOCUS_AUDIO_PLAYER` env vars (comma-separated = multi-room broadcast). Categories: `selfcare`, `reminder`, `calendar`, `ambient`, `progress`, `focus`, `briefing`, `temperature` (closet heat alerts; panel row "Server closet temperature"). `temperature` was added after the panel shipped: left unset it follows the panel `default` → `reminder` route (then legacy `REMINDER_SPEAKER`), as before it had a row.

### Speaker liveness (dead-speaker skip + fallback)

HA returns 200 for `play_media` on an `unavailable` Cast entity, so a dead speaker used to log as delivered. `_announce_voice` now checks each target's HA state BEFORE synthesizing TTS:

| HA state result | Treated as |
|-----------------|-----------|
| `unavailable` or 404 | dead — skipped |
| `unknown`, `off`, any other state, HA error/timeout | live — attempted |

- If every routed speaker is dead, falls back to the live reminder-route speakers — for every `announcement_type` except `manual` (`/api/announce` naming a speaker), which fails honestly (HTTP 502).
- Return dict may carry `unavailable` (dead targets) and `fallback` (speakers used instead).
- Speaker ids must match `media_player\.[a-z0-9_]+` — enforced at announce time, in the Speakers panel validator (`announcement_routes._validate_speaker_string`), and on a routine's `speaker` (`routines_config`). The state GET URL-encodes the entity id.
- Metrics: `bgw_announcement_speaker_unavailable_total{speaker}` (configured speakers, else `other`; pre-seeded at 0 by `seed_announcement_metrics` at startup) and `bgw_announcement_fallback_total{type}`. Alert `AnnouncementSpeakerUnavailable` — see `monitoring/README.md`.
- Fix for a dead speaker is physical: power-cycle it; a stereo pair that doesn't return in HA needs re-pairing in Google Home.

## VoxCPM2 TTS server (port 8004)

Second TTS server on Helios, replacing Qwen3-TTS (:8002) as the live voice. **Status: cut over** — Jupiter `.env` has `TTS_URL=http://10.0.0.195:8004`; `qwen-tts` on :8002 stays running as rollback. Chosen over a Qwen3-TTS clone and IndexTTS-2.5 by listening A/B; also Apache-2.0 vs IndexTTS's bilibili license.

| Property | Value |
|----------|-------|
| Unit / source | `voxcpm-tts.service` / `tts/voxcpm_server.py` (repo copies: `tts/voxcpm-tts.service`, `tts/voxcpm_server.py`) |
| Model | `openbmb/VoxCPM2` (Apache-2.0, 2B), weights copied to `/home/labadmin/models/VoxCPM2`, loaded offline; HF revision + sha256s in the unit header |
| GPU / VRAM | GPU1 RTX PRO 5000 (`CUDA_VISIBLE_DEVICES=1`), ~7.4 GiB resident |
| Speed | ~0.4–1.3 s per sentence; output 24 kHz WAV (`VOXCPM_OUTPUT_SR`, same rate as Qwen) |
| Startup | first-ever start ~50 s (torch.compile); warm ~27 s — compile cache in `/var/cache/voxcpm`, persists across boots |
| API | Same as `tts/server.py`: `/health`, `/voices`, `/tts` (honors `emotion`), `/v1/audio/speech` (honors OpenAI `instructions`) |
| Voices | Same `voices.json` as the Qwen server (`VOXCPM_VOICES_DIR`); only `ref_audio` is used — reference-only cloning keeps the timbre, not the clip's mood, so the style note can steer |
| Logs | `journalctl -u voxcpm-tts` on Helios (not shipped to Loki; text never logged, lengths only) |

**Style note:** `emotion` / `instructions` becomes a VoxCPM2 controllable-cloning prefix, `(calm, gentle and reassuring)text`. No note = the model infers prosody from the text.

**Server guards:** `()` / `[]` and their NFKC lookalikes are stripped from body text (so text can't inject a style note); text capped at 2000 chars (`VOXCPM_MAX_CHARS`), style at 80; more than 8 pending requests → 503 (`VOXCPM_MAX_PENDING`); `/health` returns 503 when no voice is usable; refuses to start without CUDA (`VOXCPM_REQUIRE_CUDA=true`).

**Sandbox** (mirrors `llama-server-primary`): nologin `llama` user, `ProtectSystem=strict`, `ProtectHome=tmpfs` + read-only binds of the venv, uv Python, voices and weights dirs, `DevicePolicy=closed`. `DeviceAllow` **must include `/dev/nvidia0`** even though it is pinned to GPU1 — CUDA opens it while enumerating, and without it torch silently falls back to CPU (~7 s/sentence). Install steps (uv-managed Python 3.11 venv, pinned `voxcpm`/torch cu128, world-readable paths): unit header.

Env vars: `VOXCPM_*` — `docs/ENV_VARS.md` → TTS.

**Cutover (done; operator steps for reference, on Jupiter; Helios must be awake):**
```bash
cp .env .env.bak-qwen-tts
# set TTS_URL=http://10.0.0.195:8004 in .env
docker compose up -d --force-recreate orchestrator
docker logs brain-orchestrator 2>&1 | grep '\[TTS\] Backend'   # confirm :8004
```
Rollback: `cp .env.bak-qwen-tts .env && docker compose up -d --force-recreate orchestrator`. `qwen-tts` keeps running on :8002 alongside. The Wyoming bridge (`wyoming-jessica-tts`) reads `--tts-url ${TTS_URL}` from the `.env` of the compose host it runs on and is not recreated by the command above — recreate it there too if HA Assist should use VoxCPM2.

```bash
ssh labadmin@10.0.0.195 'sudo systemctl restart voxcpm-tts'
ssh labadmin@10.0.0.195 'journalctl -u voxcpm-tts -f'
```

**Monitoring:** blackbox probe `service="tts-voxcpm"` on `:8004/health`; `TTSDown` covers `service=~"tts|tts-voxcpm"`, so both servers alert while both run. No server-side `/metrics` yet.

## Voice style

How Jess *sounds* (not what she says), chosen per utterance by `orchestrator/voice_style.py` and sent to the TTS as a style note. Qwen3-TTS accepts and ignores it, so it is harmless on the :8002 rollback.

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
- Announcements: `reminder_manager._announce_voice` uses `style_for_announcement(type)`.
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
4. Helios: `journalctl -u voxcpm-tts | grep styled=True`.

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

**Voice mode (backend):** voice turns run the unified loop with `max_tokens` 1024 and `chat_template_kwargs {"enable_thinking": false}` — first spoken word ~0.16 s vs ~0.77 s with the server default `reasoning_effort=low`. Caveat: the Qwen3.8 template then pre-fills an empty `<think></think>` and the model can end its turn at once (7/8 when the history held an empty assistant turn; 0/48 with clean history). Mitigations: `cloud_brain` drops empty assistant turns from history, and an empty round is retried once with thinking on. Detail: `TECHNICAL_REFERENCE.md` → Empty-Reply Handling.

**Empty replies (client):** the chat page snapshots history before adding the new turn (the first message of a new chat used to go out twice with a blank assistant turn between) and never sends blank or error bubbles as history. A reply that is still empty shows — and in voice/call mode speaks — "Sorry — I didn't get a reply just now. Please try again."

**Controls:** header Stop pill while speaking; starting a recording stops playback (mic stays enabled while Jess speaks); replay button on finished assistant bubbles (not error bubbles); 44px touch targets; always-mounted sr-only aria-live region announces failures.

| Status | Trigger | UI |
|--------|---------|----|
| `blocked` | `NotAllowedError` | "Tap to play" |
| `asleep` | HTTP 503 (TTS unreachable — Helios asleep) | "Jess's voice is asleep — tap to retry" |
| `interrupted` | iOS paused the element (screen lock, call, Siri) | "Voice paused — tap to replay" |
| `error` | other failure, 30 s client timeout | error text |

**Backend:** `POST /api/tts/synthesize` (`orchestrator/routes_vision.py`) — caps 1000 chars / 2000 UTF-8 bytes, status contract in `TECHNICAL_REFERENCE.md` → Voice. Success pre-pads ~150 ms WAV silence (`TTS_SILENCE_PAD_MS`, same as `/v1/audio/speech`) and calls `shared.mark_voice_activity()`, so `reminder_manager`'s voice-session gate holds house-speaker announcements while the browser speaks (reminders: speakers only, at most ~10 min; phone push is immediate — see Reminder Voice Retry).

**Metrics:** `bgw_dashboard_tts_total{result}` (`ok|unreachable|timeout|upstream_error|not_configured|bad_request|too_long`, pre-initialized). `bgw_voice_tts_seconds` observes SUCCESSFUL synth only, fed by `/v1/audio/speech` and `/api/tts/synthesize` (one sample per segment; buckets extend to 15 s and 20 s). Grafana TTS Synthesis row (`brain_gateway_sre.py`): "Dashboard TTS Outcomes" (replaced "TTS Call Rate"), "TTS Sentences (24h)" (was "TTS Calls (24h)"); TTS latency panel thresholds 3 s yellow / 6 s red (per-clip median is ~2–3 s live).

**Tests:** `orchestrator/tests/test_tts_synthesize_route.py`, `frontend/src/lib/speech-text.test.ts`, `frontend/src/lib/speech-queue.test.ts`.

**Known limits:**
- Stop / 503 cancels only the browser fetch — the orchestrator + TTS server still finish up to 2 in-flight sentences.
- TTS server has 2 worker threads: a speaking tab can delay an announcement or Telegram voice reply by ~one sentence.
- 500 emoji (2000 bytes) still pass the server caps (the client strips emoji anyway).
- Helios asleep: the first sentence waits for the connect failure (typically a few seconds) before "voice is asleep".
- ElevenLabs/OpenAI TTS backends: auto-spoken replies are sent to that provider.
- iOS behaviour verified only in Chromium with an injected iOS-like per-element `play()` policy, not yet on a real iPhone.

## Hands-free call mode

Open-WebUI-style continuous voice conversation in `/chat`: speak, Jess answers aloud, she listens again. Builds on Dashboard chat voice replies above.

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
- The OWUI-only `/v1/audio/transcriptions` route still returns an unhandled 500 on STT failure.

## Reminder Voice Retry

Voice-only reminder failures (`set_reminder` with `target="voice"`) auto-retry once after 2 minutes. If the retry also fails, the system falls back to a phone notification so the reminder is never silently lost.

**Suppression (`tool_handlers.deliver_reminder_job`):**

| Gate | Effect |
|------|--------|
| Voice session active (`shared.is_voice_session_active()`, 60 s window refreshed by dashboard STT + each `/api/tts/synthesize` sentence) | Phone channels (HA Companion, ntfy, Pushover, Telegram) delivered in the same run; only the speaker part re-armed +2 min, max `MAX_VOICE_SESSION_DEFERRALS` = 5 (~10 min, kwarg `voice_deferrals`). After the cap: speakers skipped if the push went out, else announced via `_announce_voice(..., ignore_voice_session=True)` (DND still applies). |
| DND active | Whole delivery (phone included) deferred 30 min. If DND starts after a voice-session push already went out, the speaker part is skipped, not replayed when DND lifts. |

A failed re-arm does not strand the reminder `pending`: speakers are dropped if the push went out, else announced immediately. Tests: `orchestrator/tests/test_reminder_delivery.py`.

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

## Voice tool subset

Voice turns (the dashboard chat mic, HA Assist / ATOM Echo) get a reduced tool list, `tool_definitions.VOICE_TOOL_NAMES`, because every tool schema is prefilled on each turn and voice is latency-sensitive. `prompt_builder` also drops the tool-guidance block on voice, so on voice the model routes by the schema descriptions alone. The voice list is filtered from the already-gated full list, so feature flags still apply. `tests/test_voice_tools.py` pins this down, and also catches a typo in the name list.

What's included: device control, memory read/write, structured data (`get_data`), brain dump, the shopping list, reminders, calendar read/create, selfcare and meal logging, focus sessions, sleep mode, decision help, routines and tasks. Since 2026-10-14 it also includes **`pantry` and `meal_plan`**, when `PANTRY_ENABLED` is on. Without them a spoken "what can I cook with the ground turkey?" got "I don't have access to your pantry". They add ~2.2k chars of schema (≈0.2 s of extra prefill on a cache miss).

Notes for the pantry tools on voice:
- The `pantry` description itself carries the hints the dropped guidance used to give: summarise `view` when speaking, and `out` already adds to the grocery list (don't also call `shopping_list`).
- HA Assist sends only the latest utterance, so a two-step "accept the plan?" → "yes" can't work there. Say "accept the meal plan" in one go, or accept on the dashboard.
- `meal_plan swap` never waits for chat to go idle (`meal_planner._ask(..., wait_for_idle=False)`). On non-streaming turns (HA Assist, Telegram) the turn that called the tool is itself counted as active, so waiting would stall the swap for the full 60 s idle timeout.
