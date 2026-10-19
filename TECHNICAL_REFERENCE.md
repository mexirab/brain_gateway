# Technical Reference

API specs and schemas for implementation.

## API Endpoints

All orchestrator endpoints live on port 8888. Many endpoints require `Authorization: Bearer $API_TOKEN` when `API_TOKEN` is set in `.env` — if you get a 401, that's why. The check (`orchestrator/bearer_auth.py` `bearer_token_matches`, shared by `BearerAuthMiddleware` and the `/health` detail gate) accepts the scheme in any case (`bearer`/`Bearer`, RFC 7235), compares the token in constant time, and never matches an empty `API_TOKEN`. `/health` reads the token from `settings.api_token` (includes the setup-wizard overlay), same as the middleware.

### Core

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Status, versions, counts (authenticated: includes a `calendar` block, see Calendar & Email) |
| GET | `/metrics` | Prometheus metrics (bearer-auth) |
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| GET | `/v1/models` | List models |

**`/v1/chat/completions` request body:** OpenAI shape (`messages`, `stream`, `tools`) plus optional `conversation_id` — stable client conversation id used only to key auto_learn (one extraction per conversation). ≤128 chars, `[A-Za-z0-9:_.\-]+`; a malformed value is silently dropped to `null` (never fails the chat). Senders: dashboard `dashboard:<conversation uuid>`, Telegram `telegram:<chat_id>:<conversation start unix ts>` (new on `/new` or empty history). Omitted → first-user-message + date fallback key. See `docs/MEMPALACE.md` → Auto-learn.

### Home Assistant

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/ha/entities` | Entities chat/dashboard may control (listed domains, not denied) |
| POST | `/api/ha/command` | One guarded HA call (natural-language or structured) |
| GET | `/api/temperatures` | Temperature sensor readings from HA |

**`GET /api/ha/entities`** → `{total, rooms_available, state_ok, controllable: {domain: [entity]}}`. Only `HA_LISTED_DOMAINS` and entities not denied by `ha_guard` (Helios plug, `HA_TOOL_DENY_ENTITIES`); entities hidden in HA are omitted (listing only — not an access control; chat and `/api/ha/command` can still target them); device-setting entities ARE listed (the `/home` Device settings card) but only the structured `/api/ha/command` body may change them; empty domains omitted; `total` counts only what's shown (vacuum etc. hidden). Raw HA entity count: `/health` → `ha_entities`.

| Field | Meaning |
|-------|---------|
| `rooms_available` | `false` when the HA registries couldn't be read (no `area`/`category`; `/home` falls back to grouping by type) |
| `state_ok` | `false` when HA couldn't be reached on this request — states are the last known |
| entity: `entity_id`, `friendly_name`, `state` | Always |
| entity: `area`, `category` | HA area name (entity override, else its device's area) and entity category (`config`/`diagnostic`); `null` when unset or registries unavailable |
| light: `brightness` (0–255\|null), `supports_brightness`, `is_group` | `is_group` = HA light group (has member `entity_id` list) |
| media_player: `volume` (0–1\|null), `media_title` (≤120 chars\|null) | |
| climate: `current_temperature`, `temperature` (number\|null) | `temperature` = setpoint |

States are at most 3 s old (`ha_client.refresh_entities(max_age_seconds=3)`: single-flight, 10 s timeout, 15 s back-off after a failed fetch — `force=True` bypasses; failures log WARNING `[HA] Entity refresh failed (<ExceptionType>)`). Area/category/hidden come from `orchestrator/ha_registry.py`: HA's WebSocket API (`config/entity_registry/list_for_display`, `config/device_registry/list`, `config/area_registry/list`), cached 600 s, retried no sooner than 60 s after a failure, 8 s timeout, never raises (serves the last good cache). The category is also a guard input: `ensure_fresh()` (called before every tool / NLP call) starts one background single-flight refresh and returns at once when a cache exists, waiting ≤3 s (shielded) only on an empty cache; `cached_meta()` is the sync read. Warmed at startup after the HA entity refresh; a changed registry invalidates the HA tool-schema cache. Metrics `bgw_ha_registry_fetch_total{result=ok|error}`, `bgw_ha_registry_entities` (0 = never read → device-setting checks run on names only); failures log WARNING `[HA] Registry fetch failed (<ExceptionType>)`.

**`GET /api/temperatures`** → `{ok: true, sensors: {closet, ambient}, delta, thresholds: {warning, critical}, estimated_monthly_cooling_cost, timestamp}`. Each sensor is `{temperature, unit, friendly_name}`; a non-numeric/NaN HA state (`unknown`, `unavailable`, text) → `{temperature: null, error: "No reading"}`. `delta` = closet − ambient (null unless both read). `thresholds` are the profile `sensors.temperature.warning/critical` (env `CLOSET_TEMP_WARNING`/`CLOSET_TEMP_CRITICAL` override) — the same bands the closet announcements use; `critical` is clamped ≥ `warning`.

**`POST /api/ha/command`** — exactly one shape:

| Body | Path |
|------|------|
| `{"command": "turn off the kitchen light"}` (1–500 chars) | Legacy NLP parser (`ha_client.execute_command`, same as fast path). "unlock" / "open the X" are refused. |
| `{"entity_id": "light.x", "service": "turn_on", "data": {...}}` (`data` optional, ≤32 keys) | Structured call — what the dashboard `/home` controls send. |

Both go through `ha_guard.check_ha_call` (same rules as the `home_assistant` tool, below), except that the structured body passes `allow_device_settings=True` (the `/home` Device settings card); the `command` form does not. Mixed/partial bodies → 422. Responses: success `{ok, action, entity_id, message}`; guard refusal (structured) → 403 `{ok:false, error}`; unknown entity (structured; not in a state list ≤5 s old — HA itself answers 200 for unknown ids) → 404 `{ok:false, error:"Unknown entity"}`; HA unreachable or call failure (structured) → 502 `{ok:false, error:"Home Assistant call failed"}`; NLP-path refusals/failures return 200 with `ok:false` + message. Refusals count `bgw_ha_call_refused_total{path=tool|nlp|api}` and log WARNING `[HA] Legacy command refused: domain.service` / `[HA] API command refused: domain.service|malformed` (never data values).

### Memory (RAG + MemPalace + Auto-Learn)

Legacy flat RAG endpoints and the structured MemPalace endpoints both read/write the same unified `mempalace` ChromaDB collection.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/memory/search` | Flat semantic search |
| GET | `/api/memory/stats` | Collection stats |
| POST | `/api/memory/add` | Add document (optional `tags` array) |
| GET | `/api/memory/learned` | List auto-learned facts (optional `?category=`, `?limit=`) |
| DELETE | `/api/memory/learned/{doc_id}` | Delete a single learned fact |
| DELETE | `/api/memory/learned?confirm=true` | Wipe all learned facts |
| GET | `/api/memory/learned/stats` | Auto-learn statistics (counts by category) |
| POST | `/api/memory/learned/toggle` | Enable/disable auto-learn at runtime |
| GET | `/api/palace/search?query=&wing=&room=&n=5` | Structured palace search with optional wing/room filter |
| POST | `/api/palace/store` | Store a memory: `{text, wing?, room?, source?, category?, project?}` |
| GET | `/api/palace/memory/{doc_id}` | Get a single memory by ID |
| DELETE | `/api/palace/memory/{doc_id}` | Delete a memory by ID |
| GET | `/api/palace/wings` | Palace wing structure |
| GET | `/api/palace/wings/{wing}/rooms` | Rooms in a wing with memory counts |
| GET | `/api/palace/stats` | Memory counts by wing |
| POST | `/api/palace/mine` | Trigger Claude Code session mining. Single-flight: 409 while a run is in progress; 500 body is generic (detail in logs) |
| POST | `/api/rag/ingest` | Force immediate re-ingest of source files (bypasses 2-min scheduler) |

### Chat History

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/chat/conversations?limit=50` | List conversations (most recent first) |
| POST | `/api/chat/conversations` | Create conversation: `{title}` |
| GET | `/api/chat/conversations/:id/messages` | Get conversation + messages |
| POST | `/api/chat/conversations/:id/messages` | Save message: `{role, content, routing?, announcement_type?}` |
| PUT | `/api/chat/conversations/:id` | Update title: `{title}` |
| DELETE | `/api/chat/conversations/:id` | Delete conversation + messages |

### Voice (STT / TTS / Announcements)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/stt/transcribe` | Dashboard mic button + hands-free call utterances. Multipart `file` (max 10 MB) → `STT_URL` `/v1/audio/transcriptions`; forwarded filename/content-type pinned to an allowlist (webm/ogg/mp4/mpeg/wav, else `application/octet-stream`). 200 = STT JSON (`{text}`); voice activity (`shared.mark_voice_activity()` + next-chat voice flag) marked only when `text` is non-empty. Errors `{ok:false, error}`: **413** > 10 MB; **503** not configured or STT unreachable (Helios asleep — call mode ends); **504** STT read/write/pool timeout; **502** STT HTTP error, non-JSON or non-object body, unexpected exception. Metric `bgw_dashboard_stt_total{result}`. See `docs/VOICE_AND_TTS.md` → Hands-free call mode. |
| POST | `/api/tts/synthesize` | Dashboard chat voice replies (one call per sentence; the client splits segments at ≤ 400 chars and speaks ≤ 1200 chars per reply). Body `{text}`. 200 audio (media type = TTS backend `audio_format`, ~150 ms WAV silence pre-pad via `TTS_SILENCE_PAD_MS`, `Cache-Control: no-store`; calls `shared.mark_voice_activity()` so house-speaker reminder announcements defer while the browser speaks — speakers only, +2 min, max ~10 min; phone push is not held). Errors `{ok:false, error}`: **400** invalid JSON / non-object / missing, blank or non-string `text` / not UTF-8 encodable (lone surrogate); **413** > 1000 chars (`TTS_SYNTH_MAX_CHARS`) or > 2000 UTF-8 bytes (`TTS_SYNTH_MAX_BYTES`); **503** TTS unreachable (connect failure — Helios asleep) or not configured; **504** TTS read/write/pool timeout; **502** TTS HTTP error or unexpected exception. Sends the current reply style note (`voice_style.current_reply_style()`). Metric `bgw_dashboard_tts_total{result}`. See `docs/VOICE_AND_TTS.md` → Dashboard chat voice replies. |
| POST | `/api/announce` | Trigger TTS announcement via voice system (`{text, speaker?}`, `announcement_type="manual"`). 200 `{ok:true, text, speaker}`; 200 `{ok:true, suppressed:true, reason}` under DND; **502** `{ok:false, error, unavailable:[...]}` when delivery fails (e.g. named speaker is `unavailable` in HA — manual announcements never fall back to another speaker). Speaker must match `media_player\.[a-z0-9_]+`. |
| GET | `/api/audio/{filename}` | Serve audio files (reminders, TTS) |
| GET | `/api/announcements/history` | Recent announcement history (optional `?limit=`, `?type=`) |
| GET | `/api/announcements/stats` | Success rates, per-speaker breakdown, latency |

### Reminders & Focus

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/reminder/trigger` | Trigger a reminder |
| GET | `/api/reminders` | List pending reminders |
| POST | `/api/reminder/complete/{id}` | Mark reminder completed |
| POST | `/api/reminder/ack/{id}?sig=&exp=` | Ntfy Done callback (HMAC-gated, bearer-exempt). Marks reminder acked via ntfy and fires selfcare bridge. |
| POST | `/api/reminder/snooze/{id}?sig=&exp=&minutes=` | Ntfy Snooze callback (HMAC-gated, bearer-exempt). Reschedules delivery by `minutes` (default 15); rejected once `snooze_count >= NTFY_MAX_SNOOZE_COUNT`. |
| GET | `/api/focus` | Current focus session status |
| POST | `/api/focus/start` | Start focus session via API |
| POST | `/api/focus/stop` | Stop focus session via API |

**Ntfy callback HMAC scheme.** Both `/api/reminder/ack/{id}` and `/api/reminder/snooze/{id}` are registered in `BearerAuthMiddleware.PUBLIC_PREFIXES` (bearer-exempt) and gated instead by an HMAC-SHA256 signature. Signature construction: `sig = hmac.new(NTFY_HMAC_SECRET, f"{id}|{action}|{exp}|{extra}".encode(), sha256).hexdigest()[:32]` where `action` is `ack` or `snooze`, `exp` is a Unix timestamp, and `extra` is the snooze-`minutes` value for snooze callbacks (empty string for ack). Requests are rejected when the signature mismatches, `exp` is in the past, the reminder doesn't exist, or (snooze only) `snooze_count >= NTFY_MAX_SNOOZE_COUNT`. Rejections increment `bgw_ntfy_callback_rejected_total{reason}`. On successful ack/snooze, if `NTFY_CONFIRM_ENABLED=true` the routes fire `asyncio.create_task(reminder_manager.deliver_ack_confirm(...))` to push a low-priority (priority=1) confirm message back to the same topic ("✓ Logged" / "💤 Snoozed until ..."). The confirm title stays generic (topic is open-tailnet; titles render on lockscreen). `bgw_ntfy_push_total` and `bgw_ntfy_push_latency_seconds` carry a `kind` label (`reminder|confirm`) to distinguish the two paths.

**Feature-flag gate (F-013 widening).** The routes' bearer-exempt / feature-enabled gate widens to `if not (ntfy_enabled or pushover_enabled)` on BOTH ack and snooze so that pushover-only deployments (`NTFY_ENABLED=false`, `PUSHOVER_ENABLED=true`) still process callbacks. On success the routes fire both `_fire_and_forget(deliver_ack_confirm(...))` (F-011) and `_fire_and_forget(deliver_pushover_confirm(...))` (F-013) — each channel is independently gated by its own enable flag inside its manager, so users who run only one channel get exactly one confirm.

**Pushover push metrics (F-013).** `bgw_pushover_push_total{result,kind,reason}` counter + `bgw_pushover_push_latency_seconds{kind}` histogram. `kind` ∈ `{reminder, confirm}` (same split as ntfy). `reason` ∈ `{ok, http_4xx, http_5xx, timeout, connect_error, other, disabled, missing_user_key, missing_app_token, missing_credentials}` — lets Grafana distinguish "Pushover token revoked" (http_4xx) from "Pushover down" (http_5xx / connect_error / timeout) from "user turned feature off" (disabled). Reminder text is HTML-escaped via `html.escape(text, quote=False)` before embedding in the Pushover HTML body (prompt-injection defense — reminder text comes from the LLM via `set_reminder` and would otherwise render as tappable `<a href>`). Error bodies run through a credential-regex-strip before being logged.

**Shared ack/snooze.** The HMAC routes, the Telegram Done/Snooze buttons and the dashboard inbox all call `orchestrator/reminder_actions.py` (`ack_reminder` / `snooze_reminder`, snooze clamped 1–120 min, retry-job cancellation, selfcare bridge, matching inbox card resolved). Log prefix `[REMINDER] ... via=<channel>`. `bgw_reminder_ack_latency_seconds` counts acks from every channel.

### Dashboard Inbox

Bearer-gated; the dashboard calls them through `/api/proxy`. Backed by `orchestrator/inbox.py` + `routes_inbox.py`. Cards are created by `deliver_reminder_job` (phone branch — voice-only reminders get none) and `selfcare_manager._dispatch_nudge` (all four kinds), plus informational `message` cards from `jobs_calendar._mirror_message` (`inbox.post_message`); a newer card for the same reminder / selfcare kind / message ref supersedes the old (message: `resolution=superseded`, `via=renewed`). Any resolution elsewhere (ntfy/Pushover/Telegram ack or snooze, selfcare logged by voice/routine/Telegram) clears the card.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/inbox` | Open cards from the last 24 h, newest first (max 20): `{ok, items:[{id, kind, ref, text, created_at}]}`. `kind` = `reminder` (ref = reminder id) \| `selfcare` (ref = `medication\|meal\|water\|movement`) \| `message` (ref = `morning_briefing\|evening_briefing\|missed_recap`; text ≤2000 chars, others ≤500). Side-effect-free. |
| POST | `/api/inbox/{id}/done` | Reminder → ack (`{ok, logged}`); selfcare → logs it via `selfcare_manager.record_selfcare`; message → 400 `not_actionable`. |
| POST | `/api/inbox/{id}/snooze` | Body `{minutes}` (default 15, clamped 1–120). Reminders only. → `{ok, rescheduled_for, minutes, snooze_count}`. |
| POST | `/api/inbox/{id}/dismiss` | Hide the card without logging anything. |

Errors: `bad_id` 400 (id must be 32 hex), `not_found` 404, `reminder_gone` 404 (card closed as dismissed), `not_snoozable` 400 (selfcare, message), `not_actionable` 400 (done on a message card), `bad_minutes` / `bad_body` 400, `max_snoozes_reached` 409 (`NTFY_MAX_SNOOZE_COUNT`), `log_failed` / `reschedule_failed` 500. An already-closed card returns 200 `{ok: true, already_resolved: true}`.

### Web Push

Bearer-gated, via `/api/proxy`. `orchestrator/webpush_manager.py` + `routes_webpush.py`; env `WEBPUSH_*` (`docs/ENV_VARS.md`).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/webpush/public-key` | `{ok, enabled, public_key, devices}` (VAPID application server key; `enabled:false` when off). Side-effect-free — the key is created at startup. |
| POST | `/api/webpush/subscribe` | Browser `PushSubscription` JSON (`endpoint`, `keys.p256dh`, `keys.auth`) → `{ok, devices}`. 409 `disabled` / `too_many_devices` (20); 400 `bad_endpoint` / `push_service_not_allowed` / `bad_keys`. |
| POST | `/api/webpush/unsubscribe` | `{endpoint}` → `{ok, removed, devices}`. |
| POST | `/api/webpush/test` | Test notification to every device → `{ok, sent, failed}` (`ok` = at least one sent). |

**Send path.** Each new inbox card fires `fire_inbox_push` (fire-and-forget, max 4 concurrent sends, 10 s timeout); the notification opens `/chat`. Endpoints are allow-listed to Apple / FCM / Mozilla / WNS push hosts (https, port 443, no userinfo/backslash/whitespace, `urllib3` and `urlsplit` must agree on the host), rechecked at send time; redirects are refused. Medication nudges and all message cards always use a generic lock-screen body (messages per ref: "Morning briefing ready" / "Evening wrap-up ready" / "Some reminders didn't reach you"); other text is shown unless `WEBPUSH_SHOW_TEXT=false`. Message cards posted under sleep mode (`DND_ACTIVE`) send no push. 404/410 from the push service deletes the subscription (`result="gone"`). Web Push counts as phone delivery in `deliver_reminder_job` only while a device succeeded (or subscribed) within the last 14 days; subscriptions with no success for 30 days are pruned by the daily `inbox_housekeeping` job (04:25, also deletes inbox rows older than 30 days; startup runs the inbox prune too). Metrics: `bgw_webpush_send_total{result}` (`ok|gone|auth|http_4xx|http_5xx|timeout|redirect|error|no_subscribers`), `bgw_webpush_subscriptions`, `bgw_inbox_items_total{kind}` (`reminder|selfcare|message`), `bgw_inbox_resolved_total{kind,resolution,via}` (via adds `renewed`).

### Selfcare

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/selfcare/today` | Today's selfcare log + last-seen-ever timestamps for the 4 tracked actions (`medication`, `meal`, `water`, `movement`). Bearer-gated (NOT in `PUBLIC_PREFIXES`). Backed by existing `state_store.get_selfcare_today()` + `get_last_selfcare()` helpers — no new schema. |

**Response shape:**

```json
{
  "ok": true,
  "as_of": "2026-04-27T14:30:00-05:00",
  "today_date": "2026-04-27",
  "actions": {
    "medication": {"today_count": 1, "last_at": "2026-04-27T08:15:00-05:00", "entries": [{"id": 42, "logged_at": "...", "detail": "Adderall"}]},
    "meal":       {"today_count": 2, "last_at": "...", "entries": [...]},
    "water":      {"today_count": 0, "last_at": "2026-04-26T22:10:00-05:00", "entries": []},
    "movement":   {"today_count": 3, "last_at": "...", "entries": [...]}
  }
}
```

On exception → 500 `{ok: false, error: "Selfcare read failed"}` and a `logger.error`. Powers the dashboard `SelfcareTodayCard` (polls every 30s).

### Calendar & Email

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/calendar/today` | Today's events from every source merged (`calendar_sources`: CalDAV + work Exchange push + Google) |
| GET | `/api/calendar/range` | Merged events for `days` days from `start` (dashboard `/calendar` week view) |
| GET/POST/PUT | `/api/calendar/sync` | Work-calendar (Exchange) push from the iPhone Shortcut (GET=status, POST/PUT=full snapshot) |
| POST | `/api/email-to-calendar/run` | Manually trigger email-to-calendar extraction (dormant by default — see `EMAIL_TO_CALENDAR_ENABLED`); writes to CalDAV (else Google), skipped when nothing is writable. An email is marked processed only after the model answers — a failed call is retried next run (query looks back 1 day) |

**`GET /api/calendar/today`** → `{ok, events: [{id, title, start, end, location, description: null, all_day, calendar, source}], source: "merged"|"none", count, unavailable: [<source label>], web_url}`. `unavailable` lists sources that should have answered but failed (dashboard shows "Couldn't reach your X calendar"); `source: "none"` when no source answered; `web_url` = `CALENDAR_WEB_URL` or null.

**`GET /api/calendar/range?start=YYYY-MM-DD&days=N`** (bearer) → same shape as `/today` plus `start, days, timezone`. `days` 1–42 (default 7); `start` strict `YYYY-MM-DD` within today −2 y / +5 y. Bad input → 400 `{ok: false, error}`. 30 s in-process cache keyed by `(start, days)`, complete successful answers only, max 32 entries.

**`POST/PUT /api/calendar/sync`** body: `{"source": "exchange", "events": [{title, start, end, location, all_day, calendar?, id?}]}` — a full snapshot posted once; it replaces the previous one. Without `"source": "exchange"` (bare list or `{"events": [...]}`) it's the retired legacy push: stored as `phone`, ignored by Jess. iOS dict-of-dicts `events` is converted. Only the listed fields are kept (length-capped, whitespace collapsed); descriptions are dropped. Limits/rejections (counted in `bgw_phone_calendar_sync_rejected_total{reason}`): body >1 MB (streamed cap) or >1000 events → 413; single-event body / bad shape / bad JSON / empty → 400; events present but none with a readable start → 422 (previous snapshot kept). Success → `{ok, source, used: source=="exchange", events_received, events_parsed, message}`. **GET** → `{ok, synced, last_sync, event_count, source}`.

**`/health` `calendar` block** (authenticated): `{configured, caldav: {configured, last}, google: {configured, last}, phone_sync: {expected, source, last, age_minutes, event_count}, poll_interval_min, morning_briefing}` where `last` = `{result, at}` of the most recent read (`ok|error|timeout|stale|corrupt`) or `null`.

### Progress Tracking

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/progress/today` | Today's stats (tasks, focus, brain dumps) |
| GET | `/api/progress/week` | This week's stats + trend vs prior week |
| GET | `/api/progress/streaks` | Active streaks (with lazy decay — stale streaks report `current: 0`) |

### Shopping Lists

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/shopping?list_name=&include_checked=` | Get shopping list items: `[{id, item, list_name, checked, added_at, checked_at, amount}]` |
| POST | `/api/shopping` | Add item: `{item, list_name}` (item whitespace collapsed to one line, ≤200 chars). 400 on non-JSON / non-object body or blank item |
| POST | `/api/shopping/{id}/check` | Check off item |
| POST | `/api/shopping/{id}/uncheck` | Uncheck item |
| DELETE | `/api/shopping/checked?list_name=` | Clear all checked items |
| DELETE | `/api/shopping/{id}` | Delete item |

`shopping_list` table: `id, item, list_name, checked, added_at, checked_at, amount` (`amount` nullable TEXT ≤80 chars, added by startup migration). `amount` is written only by the meal planner (week total from `grocery_amounts.combine_amounts`, see `docs/PANTRY.md` → Accept); no route sets it. The `shopping_list` tool's `list` output shows it (`- milk — 1 1/2 lb (id: 3)`).

### Vision

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/vision/analyze` | Analyze an image (multipart form or JSON with base64) |
| GET | `/api/vision/status` | Vision model health and configuration |

Live deployment: `VISION_MODEL_URL=http://10.0.0.195:8080/v1`, `VISION_MODEL_NAME=qwen3.8-27b-turbo-q6k` — the primary brain serves vision (llama.cpp `llama-server-primary` with `--mmproj` since 2026-10-02; the vLLM NVFP4 brain did the same 2026-09-28 → 2026-10-02; Saturn Qwen3-VL-8B :8010 is out of the runtime path). The brain has one slot (`--parallel 1`), so vision requests queue behind chat. The service-registry vision probe hits `/v1/models`, which the brain serves. Vision therefore needs Helios awake.

### Ambient / System

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/ambient/status` | Aggregated ambient status (schedule, focus, tasks, LED color). Calendar: merged via `calendar_sources` (same as `check_calendar` and the briefings); schedule reads "unknown" when a source fails — see `docs/CALENDAR.md`. |

### Helios Power (PT-C)

All bearer-gated. Return 409 when `HELIOS_WAKE_ENABLED` is false (`{ok:false, skipped:"disabled", reason}`), 502 on HA error (`{ok:false, error}`). All control runs through Home Assistant `switch.turn_on`/`switch.turn_off` on the Helios smart plug — see `orchestrator/helios_power.py`.

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/helios/wake` | Power Helios on (`switch.turn_on`). Debounced — a recent wake returns 200 `{ok:true, skipped:"debounced", retry_after_s}`; a fresh wake returns 200 `{ok:true, action:"wake", entity}`. |
| POST | `/api/helios/sleep` | Power Helios off (`switch.turn_off` — a hard cut; safe, Helios is stateless). 200 `{ok:true, action:"sleep", entity}`. |
| GET | `/api/helios/power` | Read plug switch state + power draw, infer running/asleep. 200 `{ok:true, switch:"on\|off\|unknown", watts:float\|null, inferred:"running\|asleep\|unknown", entity}`. |

Metrics: `bgw_helios_wake_total{result}` (`ok\|debounced\|disabled\|error`), `bgw_helios_sleep_total{result}` (`ok\|disabled\|error`), `bgw_helios_status_total{result}` (`ok\|disabled\|error`), `bgw_helios_plug_watts` gauge, `bgw_helios_running` gauge. The `helios_status_poll` scheduler job (60s, when `HELIOS_WAKE_ENABLED` or `HELIOS_PLUG_MONITOR_ENABLED`) refreshes the gauges. Monitor-only mode (`HELIOS_PLUG_MONITOR_ENABLED=true`, wake off) polls read-only but these routes still return 409 — deliberate. Auto-wake also fires from the brain-asleep chat path (`cloud_brain._maybe_wake_helios`).

### Workouts

Mounted only when `WORKOUTS_ENABLED=true` (otherwise 404). Errors: `{"ok": false, "error": "..."}` — 404 for `location not found` / `no active program`, 400 for validation failures. Detail: `docs/WORKOUTS_AND_MEALS.md`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/workouts/today` | Today's workout plan + logged sets (incl. `location_id`, `location` name, `is_deload`, `exercises[].note` — the progression note) |
| POST | `/api/workouts/generate` | Generate today's plan. Body (optional): `{location_id?}` — see below |
| GET | `/api/workouts/history` | Past workout sessions (`days` 1–365, default 14) |
| GET | `/api/workouts/exercises` | Exercise catalog (~72). Query: `movement_pattern?`, `equipment?`, `location_id?` (keeps only exercises whose `requires` ⊆ the location's equipment; unknown id → 404) |
| GET | `/api/workouts/program` | `{program: {...} \| null}` |
| PUT | `/api/workouts/program` | Set the program: `{goal, days_per_week, session_minutes, deload_every_weeks?}` → `{ok, program}`. `deload_every_weeks`: `0` (off) or 3–12; omitted/`null` keeps the current value (default 6 for a new program); bool / non-integer float → 400 |
| DELETE | `/api/workouts/program` | Clear the active program → `{ok: true}` (404 if none) |
| GET | `/api/workouts/locations` | `{locations: [...], equipment_vocabulary: [12 tokens], templates: {name: [tokens]}}` — most recently used first |
| POST | `/api/workouts/locations` | Create: `{name, template? \| equipment?, limits?}` → 201 `{ok, location}` |
| PATCH | `/api/workouts/locations/{id}` | Update any of `{name, equipment, add_equipment, remove_equipment, limits}` → `{ok, location}` |
| DELETE | `/api/workouts/locations/{id}` | Delete → `{ok, removed: "<name>"}`. Past workouts keep the dangling `location_id` |
| POST | `/api/workouts/sets` | Log a set: `{exercise, weight_lbs, reps, rpe?, set_id?, workout_id?}` |
| PATCH | `/api/workouts/{id}` | Modify workout: `{remove_exercises?: [name], add_exercises?: [name]}` → `{ok, workout_id, removed_sets, added_exercises, skipped_exercises: [{name, reason, alternatives?}], removal_held}` |
| DELETE | `/api/workouts/{id}` | Delete a workout |
| POST | `/api/workouts/{id}/end` | End a workout session |
| DELETE | `/api/workouts/sets/{id}` | Delete a logged set |
| GET | `/api/workouts/exercise-info?name=` | How-to for one exercise → `{ok: true, name, library_available, found, video_url, match?, instructions?, primary_muscles?, equipment?, frames?}`. `frames` = `/api/workouts/exercise-library/<id>/<n>.jpg?v=<sha7>`. 400 whitespace-only name; 422 empty or >200 chars |
| GET | `/api/workouts/exercise-library/{lib_id}/{frame}.jpg` | Library photo → `image/jpeg`, `Cache-Control: private, max-age=604800` (URLs are versioned by the pinned source commit). Unknown id/frame → 404 JSON |

**`POST /api/workouts/generate`:** `location_id` must be an integer or null (else 400). Omitted → the only location, else the last-used one, else none (whole catalog, no load caps). Explicit `null` → no location (whole catalog). Idempotent per day: if today's workout exists it returns `{ok, workout_id, workout_type, reasoning, exercises: [], already_exists: true, location_mismatch}` (`location_mismatch` = a `location_id` was sent and differs from today's plan). New plan → `{ok, workout_id, workout_type, reasoning, exercises: [{name, primary_muscle, muscle_groups, equipment, is_compound, sets: [{set_number, target_reps, target_weight_lbs}], progression, note?}], location: {id, name} | null, program_day: {index, of, label} | null, deload: bool, notes: [str]}`. `progression` ∈ `new|up|reps|hold|stall_deload|comeback|capped|deload_week` (rules: `docs/WORKOUTS_AND_MEALS.md` → Progression). Failure (unknown location, nothing fits the gear) → 400.

**Program object:** `{id, goal: "strength"|"muscle"|"general", days_per_week: 2–6, session_minutes: 20–120, deload_every_weeks: 0|3–12, active, created_at, split: [day labels], next_day_index, next_day_label, exercises_per_session, deload: {enabled, is_deload, sessions_until_deload: int|null}}` (returned by GET and PUT).

**Location object:** `{id, name (≤60 chars, unique case-insensitive), equipment: [tokens], limits: {dumbbell_max_lbs?, dumbbell_step_lbs?, kettlebell_weights_lbs?: [lb]}, last_used_at, created_at, updated_at}`. Equipment tokens: `barbell, squat_rack, bench, trap_bar, dumbbells, kettlebell, cable, machines, leg_press, pull_up_bar, dip_station, ab_wheel` (unknown token → 400). Templates: `commercial_gym` (all 12), `home_basic` (`dumbbells`, `kettlebell`), `empty`; default `commercial_gym`, ignored when `equipment` is given. Limits: `dumbbell_max_lbs` 1–300, `dumbbell_step_lbs` 0.5–50, `kettlebell_weights_lbs` ≤12 weights each 1–300; a limits patch merges onto existing limits, `null`/`""`/`[]` clears a key, unknown keys → 400. Max 20 locations. Program + location writes are audited to `config_changes` (panel `workouts`).

Metrics: `bgw_workout_generated_total{result}` (`ok|no_exercises|bad_location|ask_location`), `bgw_workout_progression_total{progression}` (one per planned exercise), `bgw_exercise_info_lookups_total{result}` (`found|not_found|library_missing`).

### Meals

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/meals/today` | Today's meals + running calorie total |
| GET | `/api/meals/history?days=7` | Meal history (`days` clamped 1–365) |
| POST | `/api/meals/` | Log a meal: `{name, calories, notes?}` |
| PATCH | `/api/meals/{id}` | Update a meal (`photo_path` field excluded from allowlist) |
| DELETE | `/api/meals/{id}` | Delete a meal |
| POST | `/api/meals/photo` | Upload meal photo → vision estimate: multipart `file` field; returns `{calories_estimate, description, confidence}` |
| GET | `/api/meals/photo/{filename}` | Serve a stored meal photo |

**Photo flow:** upload → vision model (`VISION_MODEL_URL`; live: the Qwen3.8 brain on Helios :8080 since 2026-09-28 — llama.cpp `qwen3.8-27b-turbo-q6k` since 2026-10-02; was Qwen3-VL-8B on Saturn) strict-JSON prompt → return estimate → user confirms in UI before save (or pass `auto_log=true` in POST body to skip confirmation). Extension allowlist: `jpg`, `jpeg`, `png`, `gif`, `webp`. Files saved as uuid4 names under `MEAL_PHOTOS_DIR`.

### Pantry (F-015)

Mounted only when `PANTRY_ENABLED=true` (otherwise 404). Bearer-gated. Errors: `{"ok": false, "error": "..."}`. Detail: `docs/PANTRY.md`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/pantry/items` | `{items, count}`. Query: `status` (`active` default \| `probably_gone` \| `gone` \| `all`), `location` (`fridge\|freezer\|pantry`) |
| POST | `/api/pantry/items` | Add: `{name, category?, location?, track_mode?, qty?, level?, shelf_life_days?, barcode?}` → 201 new, 200 when merged into an existing item (`merged: true`); 400 when `name` isn't a string |
| PATCH | `/api/pantry/items/{id}` | Update any of `name, category, location, track_mode, qty, level, status, shelf_life_days` |
| DELETE | `/api/pantry/items/{id}` | Delete (ledger keeps a `remove` event) |
| POST | `/api/pantry/items/{id}/use` | "Used some": `{count?}` — count −N (default 1), level steps down one, staple unchanged |
| POST | `/api/pantry/items/{id}/out` | Mark out (does NOT touch the shopping list — the chat tool does) |
| GET | `/api/pantry/events` | Ledger. Query: `item_id?`, `limit` 1–500 (default 50) |
| POST | `/api/pantry/scan` | Multipart `files` (1–8, JPEG/PNG/WebP magic bytes) + `kind` (`fridge\|freezer\|pantry\|receipt`) + `force?` (`true` skips the receipt duplicate-photo check) → 202 `{ok, scan_id, status:"queued"}`. 400 bad kind/count/empty, 409 `VISION_ENABLED=false`, 409 `{reason:"duplicate", duplicate_of, duplicate_status}` (receipt photo identical to one in an earlier non-failed/non-discarded receipt scan), 411 no `Content-Length`, 413 photo over cap, 415 bad format, 429 already 3 open scans |
| GET | `/api/pantry/scans` | Recent scans. Query: `status?` (`queued\|processing\|pending_review\|failed\|committed\|discarded`), `limit` 1–50 |
| GET | `/api/pantry/scan/{id}` | `{id, kind, status, photo_count, photos_available, proposed: {items, warnings} \| null, error, created_at, committed_at}` — no filesystem paths. Poll while `queued`/`processing`. Receipts: `proposed` adds `receipt: {store, date, total, items_sold, duplicate_of}`; lines carry `key, raw, read_name, action (restock\|add), match_id, match_name, matched_by (alias\|inventory\|model), food, ticks[], include`. After commit/discard a receipt's `proposed` is only `{receipt: {store, date, total}}` |
| GET | `/api/pantry/scan/{id}/photo/{index}` | Scan photo (`Cache-Control: private, no-store`); 404 after commit/discard |
| POST | `/api/pantry/scan/{id}/commit` | `{items: [{include?, match_id?, name, category?, location?, track_mode?, qty?, level?}]}` (≤200). Matched lines SET the item, others add → `{ok, added, updated, skipped, gone}`. Stock-take proposals also carry `action: "not_seen"` lines (tracked items the photos didn't show, unticked); such a line marks the item out only with `include: true` → counted in `gone`. A `match_id` is honoured only if this scan's own proposal matched it (otherwise the line adds). 409 unless `pending_review`. **Receipts:** lines also send `key` (proposal line); one transaction; matched lines restock (count +qty ≤50, level → full), aliases learned from corrections, grocery entries ticked → adds `aliases_learned, grocery_checked` |
| POST | `/api/pantry/scan/{id}/discard` | Discard + delete photos. 409 unless `pending_review`/`failed` (or stuck >10 min) |
| POST | `/api/pantry/scan/{id}/retry` | Re-queue a `failed` scan → 202. 409 otherwise |
| GET | `/api/pantry/barcode/{code}` | Open Food Facts lookup (cached) → `{ok, name, category, ...}`. 400 not 8–14 digits, 409 lookup disabled, 404 unknown product, 502 upstream error |
| GET | `/api/pantry/diet` | `{settings, profiles: [{key, label, description}]}` — settings fields: `docs/PANTRY.md` → Diet settings |
| PUT | `/api/pantry/diet` | Partial update (`reminders` and `fridge_check` merged key-wise) → `{ok, settings, rescheduled_plans}` (active plans' reminders moved when reminder settings changed). 400 on invalid value or non-object body |
| GET | `/api/pantry/plan` | `{active, draft}` — `active` = accepted plan covering today, else next upcoming; `draft` = newest `generating`/`draft`, else a `failed` attempt from the last 2 days. Each a plan object or null |
| POST | `/api/pantry/plan/generate` | 202 `{ok, plan_id, week_start}` (runs in background — poll `GET /plan/{id}`). 409 one already generating (`plan_id`), 400 nothing to plan (both counts 0), 503 brain asleep (wakes Helios if `HELIOS_WAKE_ENABLED`) |
| GET | `/api/pantry/plan/{id}` | Plan: `{id, week_start, status, profile, profile_label, notes, warnings[], error, created_at, accepted_at, entries[{id, plan_id, day, slot, recipe_id, mode, portions, status, prep_batch_id}], recipes{id: {name, minutes, ingredients[{item, amount, have}], steps, est_*, rating, ...}}, batches{id: {id, portions_made, portions_left, good_until, status}}, missing[], missing_amounts{name: total}}`. `have`/`missing` computed live vs inventory; `missing_amounts` = week total per missing item (`""` if no recipe gave one) |
| POST | `/api/pantry/plan/{id}/accept` | `{already_have?: [str]}` (≤100) → `{ok, added_to_list, added_to_pantry, reminders}`. 409 unless `draft`, 400 bad `already_have` |
| POST | `/api/pantry/plan/{id}/discard` | `draft`/`failed`/`active` (or `generating` >10 min) → `discarded`; pending reminders cancelled. 409 otherwise |
| POST | `/api/pantry/plan/entry/{id}/swap` | Replace one recipe (draft/active plan) → `{ok, added_to_list, plan}`. 404 unknown entry, 409 busy / plan not editable / already changed / entry not `planned`, 502 LLM error or unusable reply, 503 brain asleep |
| POST | `/api/pantry/plan/entry/{id}/cooked` | Prep or dinner of the active plan `planned`/`skipped` → `cooked`; deducts ingredients → `{ok, recipe, slot, deducted[], running_low[], not_tracked[], prep_batch_id, portions}` (`prep_batch_id`/`portions` only for the batch prep). 400 not prep/dinner, 404 unknown entry, 409 plan not active / already cooked |
| POST | `/api/pantry/plan/entry/{id}/uncook` | Undo the latest cook (a dinner's meal log from the last 24 h is deleted) → `{ok, restored}`. 404 unknown entry, 409 not cooked / portions already eaten |
| POST | `/api/pantry/plan/entry/{id}/skip` | `planned` prep/dinner → `skipped` (pending reminder cancelled) → `{ok}`. 404, 409 otherwise |
| GET | `/api/pantry/prep` | Active prep batches → `{batches: [{id, plan_id, entry_id, recipe_id, recipe_name, cooked_at, portions_made, portions_left, good_until, location, status, expired}]}`, soonest `good_until` first |
| POST | `/api/pantry/prep/{id}/eat` | One portion eaten (ticks today's planned lunch, logs a meal) → `{ok, batch_id, recipe, portions_left, good_until}`. 404 unknown batch, 409 batch finished |
| POST | `/api/pantry/prep/{id}/uneat` | Undo the newest eat of this batch in the last 24 h (removes its meal log, re-plans the lunch it ticked, restores the portion unless the batch expired or was finished since) → `{ok, batch_id, recipe, portions_left, portion_restored}`. 404 no eat in the last day |
| POST | `/api/pantry/prep/{id}/finish` | Batch finished/thrown out → `{ok}`. 404 no active batch with that id |
| POST | `/api/pantry/cooked-other` | Off-plan meal `{item_ids: [int], dish?}` — one use of each item (out/gone skipped, staples untouched), logs a meal → `{ok, cook_id, dish, deducted[], running_low[], not_tracked[]}`. 400 `item_ids` missing/empty/>30, 404 nothing usable, 409 same items logged within 10 min |
| GET | `/api/pantry/cooked-other?days=` | Off-plan meals of the last `days` (1–14, default 2), newest first, max 20 → `{cooks: [{id, dish, source, cooked_at, status, items[], can_undo}]}` |
| POST | `/api/pantry/cooked-other/{id}/undo` | Undo an off-plan meal (≤48 h; deletes its meal log) → `{ok, cook_id, dish, restored}`. 404 unknown id, 409 already undone / too old |
| POST | `/api/pantry/recipe/{id}/rate` | `{rating: "up"\|"down"\|"clear"}` → `{ok}`. 400 bad rating, 404 unknown recipe |

Metrics: `bgw_pantry_scan_total{kind,result}`, `bgw_pantry_scan_latency_seconds{kind,result}`, `bgw_pantry_receipt_lines_total{result}`, `bgw_pantry_events_total{kind,source}`, `bgw_barcode_lookup_total{result}`, `bgw_meal_plan_generated_total{result}`, `bgw_meal_plan_latency_seconds`, `bgw_meal_plan_swap_total{result}`, `bgw_meal_plan_swap_latency_seconds`, `bgw_pantry_cook_events_total{slot,source}` (slot `prep|dinner|other`), `bgw_pantry_portions_eaten_total{source}`, `bgw_pantry_decay_total{kind}`, `bgw_pantry_decay_last_run_timestamp_seconds`, `bgw_pantry_fridge_check_nudge_total{result}`, `bgw_pantry_reminder_done_total{result}`.

### Document Vault

All bearer-gated (`orchestrator/routes_documents.py`).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/documents` | List documents |
| GET | `/api/documents/categories` | Vault categories |
| GET | `/api/documents/{doc_id}` | Document metadata |
| POST | `/api/documents` | Multipart upload + save. OCR (Tesseract), embeddings and the Chroma add run in worker threads (no event-loop stall) |
| POST | `/api/documents/suggest-name` | Photo → suggested `title` + `category` (see below). Stateless — nothing saved |
| PUT | `/api/documents/{doc_id}` | Update metadata |
| DELETE | `/api/documents/{doc_id}` | Delete document + its RAG chunks |
| GET | `/api/documents/{doc_id}/download` | Original file (attachment) |
| GET | `/api/documents/{doc_id}/text` | Extracted text |

**`POST /api/documents/suggest-name`** — multipart `file`, JPEG/PNG/WebP only (magic-byte check). Body cap: middleware `_PATH_UPLOAD_LIMITS` 16 MB (Content-Length required → 411), handler cap `min(15 MB, VISION_MAX_IMAGE_SIZE)`. Bad input → HTTP 400 `{ok:false, error}`; everything else HTTP 200:

| Response | Meaning |
|----------|---------|
| `{ok:true, title, category}` | Title sanitized (control chars/markup stripped, ≤80 chars); category coerced to a vault category |
| `{ok:false, reason:"busy"}` | Another suggestion is running (one at a time, `asyncio.Lock`) |
| `{ok:false, reason:"vision_unavailable"}` | `check_vision_health` failed (Helios asleep) — fails fast, does NOT wake Helios |
| `{ok:false, reason:"timeout"}` | `analyze_image(thinking=False)` exceeded its 60 s budget |
| `{ok:false, reason:"vision_error"\|"unreadable"}` | Vision call failed / reply not parseable |

Logs result, bytes and latency only. Metric `bgw_document_name_suggest_total{result}` (`ok|vision_off|timeout|vision_error|unparseable|busy|bad_image`); Grafana panel "Document Photo Name Suggestions (1h)" (Vision/Web Search/TTS row). No alert by design.

### Paperless Bridge (F-012)

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/paperless/upload` | Multipart upload → forward file to Paperless-ngx for OCR + tagging. Bearer-gated (NOT in `PUBLIC_PREFIXES`). 100 MB cap via `_LARGE_UPLOAD_PATHS`. Form fields: `file` (required), `title`, `correspondent`, `document_type`, `tags` (repeat field). Returns `{ok, task_id, latency_ms}` on 200, `{ok:false, error}` on 503 (feature disabled / runtime-disabled) or 502 (Paperless rejected / unreachable). |

Responses are never cached. No local copy is persisted on Helios — Paperless owns the file once it returns a task id. Metrics: `bgw_paperless_upload_total{result,reason}` (labels: `result` ∈ `{ok, fail, skipped}`, `reason` ∈ `{ok, http_4xx, http_5xx, timeout, connect_error, other, disabled, missing_url, missing_token, file_too_large, file_missing}`), `bgw_paperless_upload_latency_seconds`.

### Finance — Budget Sync (Actual Budget)

Router: `finance_manager.router` (`/api/finance/*`). All bearer-gated; all return `{"ok": bool, ...}`. Failure codes: 400 not configured, 409 busy, 422 bad body, 502 upstream (Actual) failure. Replaced `/api/finance/ynab/*` (removed 2026-10-02). Read-only against Actual — nothing is ever committed back.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/finance/sync/status` | No network call. `{ok, provider:"actual", configured, connected, budget_name, last_synced_at, last_attempt_at, last_error, last_result, category_count, discretionary_count}`. `connected` = most recent attempt succeeded. |
| POST | `/api/finance/sync` | Manual sync. Within 60 s of the last attempt returns the stored result with `cooldown:true` (502 if that attempt failed) instead of re-downloading. Success: `{ok, synced, inserted, updated, deleted, legacy_removed, skipped_inflows, window_start, fun_money_category, fun_money_balance}`. |
| GET | `/api/finance/categories` | `{ok, budget_name, groups:[{group_name, categories:[{name, is_discretionary, budgeted, activity, balance}]}]}` for this month (income + hidden categories omitted). Served from the last snapshot for 15 min (syncs first when stale); a failure is negative-cached for 2 min. |
| POST | `/api/finance/categories/mapping` | Body `CategoryMappingRequest` (`schemas.py`): `{mappings: {"<category>": bool}}` — `StrictBool` values (string `"false"` rejected), 1–1000 entries, names 1–200 chars. Re-flags every synced row and recomputes `discretionary_spent`. Returns `{ok, success, updated}`. |
| POST | `/api/finance/transactions/reclassify` | Transactions-page discretionary dot. Body `ReclassifyTransactionRequest` (`schemas.py`): `{id: StrictInt 1..2^63-1, is_discretionary: StrictBool}`. Returns `{ok, success, id, is_discretionary}`; 404 `{ok:false, error}` unknown id; 422 bad body. On a synced row, sets `discretionary_override` when the click disagrees with the category mapping, clears it when it agrees (the dot is its own undo); manual rows never get one. Current month with a synced Fun Money balance: re-derives `discretionary_budget = fun_money_balance (budget_sync_state.last_result) + discretionary_spent` absolutely (idempotent under concurrent clicks). |
| POST | `/api/finance/sync/reset` | Deletes all `source='actual'` rows (and their discretionary overrides) + `budget_sync_state`, clears the category caches; next sync re-imports. |

**Sync semantics** (`finance_manager.apply_snapshot` / `actual_client.fetch_snapshot`): mirrors the last `ACTUAL_SYNC_MONTHS` months. Spending = outflows from on-budget accounts, split legs individually; all transfers (incl. categorized transfers to off-budget accounts) excluded; inflows skipped. Rows are upserted by `external_id`, rows gone upstream are deleted, legacy `source='ynab'` rows inside the window are replaced. `discretionary_spent` is recomputed from rows (manual entries count); current-month `discretionary_budget` = Fun Money balance + spent, so the health bar remaining equals Actual's category balance. One sync at a time (`asyncio.Lock`, callers get `busy`); the download runs on a dedicated single-worker executor with a 120 s timeout and refuses to start while a timed-out download is still running. Errors pass through `actual_client.safe_error()` (URL credentials stripped). Scheduler job `budget_sync` every `ACTUAL_SYNC_INTERVAL` min. Metrics: `bgw_budget_sync_total{result=ok|error|busy}`, `bgw_budget_sync_last_success_timestamp_seconds`; alert `BudgetSyncFailing`.

### Claude Code Integration

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/claude_code/turn` | Stop hook target — logs a completed Claude Code turn to the 7-day rolling buffer |
| GET | `/api/claude_code/recent?minutes=120&limit=20` | List recent Claude Code turns for dashboards or the `check_claude_activity` tool |

### Setup Wizard (`/api/setup/*`)

First-boot setup-wizard backend. Backed by `orchestrator/routes_setup.py`. All three endpoints are bearer-gated (NOT in `PUBLIC_PREFIXES`) — the orchestrator refuses to boot without `API_TOKEN`, so a token always exists by the time these are reachable. No DB schema change: state is two JSON files under the `/app/data` bind mount.

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/setup/status` | Report whether the first-boot wizard has been completed. Returns `{ok: true, setup_completed: bool, completed_at: str\|null}`. |
| GET  | `/api/setup/hardware` | Serve the cached hardware scan that feeds the wizard's model-selection step. |
| GET  | `/api/setup/env` | Per-key state for every allow-listed env override. Returns `{ok: true, locked: bool, restart_required: bool, keys: {KEY: {secret, service, description, present, value?}}}`. `locked` mirrors "first-boot done"; `restart_required` mirrors the in-process `_dirty_since_boot` flag. Secret keys (`HA_TOKEN`, `NTFY_HMAC_SECRET`, `PUSHOVER_*_TOKEN`, `PAPERLESS_API_TOKEN`, ...) never include `value` — presence only. |
| POST | `/api/setup/env` | Body `{values: {KEY: value, ...}}` — write one or more overrides to `/app/data/setup_overrides.env`. **First-boot only**: returns HTTP 410 once `setup_completed` flips true (Gitea `INSTALL_LOCK` pattern). Allow-list enforced (400 on unknown key — see `ALLOWED_KEYS` in `orchestrator/setup_env.py`). Empty strings and control characters rejected (400). Held under `setup_env._write_lock` so a concurrent `POST /complete` can't race past the gate. Log lines redacted via `setup_env.redact_for_log()`. Returns `{ok: true, written: [KEY, ...], restart_required: true}`. |
| DELETE | `/api/setup/env/{key}` | Remove a single key from the overrides file. First-boot only (HTTP 410 after `/complete`). Returns `{ok: true, key, removed: bool, restart_required: bool}`. |
| POST | `/api/setup/env/validate` | Body `{service, values}` — run a per-service live validator (HA `/api/`, Pushover `/users/validate.json`, ntfy publish ping, Paperless `/api/`) against the supplied creds via `httpx`. **NOT locked** — operator can re-check a stored token after setup. `values` is filtered to the allow-list before being passed to the validator (defence-in-depth against attacker-controlled URLs/headers). Returns `{ok: bool, detail: str}`. |
| POST | `/api/setup/complete` | Mark the wizard done and persist the timestamp. Idempotent — a re-POST keeps the original `completed_at`. Returns `{ok: true, setup_completed: true, completed_at: str}`. |

**State files.** `/app/data/setup_state.json` (`{setup_completed, completed_at}`) is written by `/api/setup/complete` via an atomic tmpfile + `os.replace` + fsync write (JSON sibling of `config_writer.atomic_write_yaml`). `is_first_boot()` fails CLOSED: only an absent `setup_state.json` means first boot; a present-but-corrupt/unreadable file returns `False`, so the first-boot-only `/api/setup/env` write surface stays locked (410). The file is fchown'd to the data dir's owner on write (`config_writer.match_parent_owner`) so the host backup can read it.

**Hardware scan is host-produced.** The orchestrator container is CPU-only — it has no GPU access and cannot run `nvidia-smi`. `/api/setup/hardware` does NOT detect hardware live; it serves a cached `/app/data/hardware_scan.json` artifact written HOST-SIDE by `scripts/detect_hardware.sh --json <path>`. The operator runs `bash scripts/detect_hardware.sh --json data/app/hardware_scan.json` on the host before/during install.

`GET /api/setup/hardware` response when the scan exists:

```json
{
  "ok": true,
  "available": true,
  "scan": {
    "gpus": [{"index": 0, "name": "NVIDIA GeForce RTX 5090", "vram_gib": 32}],
    "gpu_count": 1,
    "driver": "570.xx",
    "ram_gib": 125,
    "largest_gpu_gib": 32,
    "vram_tier": 32,
    "tensor_parallel_advisory": 1,
    "recommendation": {
      "model": "Lorbus/Qwen3.6-27B-int4-AutoRound",
      "quantization": "...",
      "max_model_len": 32768,
      "gpu_mem_util": 0.90,
      "vision_capable": false
    }
  }
}
```

When no scan file is present, the response is `{"ok": true, "available": false, "hint": "<re-run instruction>"}` — never an error; the wizard surfaces the hint to the operator.

**Env overrides overlay.** `POST /api/setup/env` writes to `/app/data/setup_overrides.env` (`.env`-style, `chmod 600`, gitignored — managed by `orchestrator/setup_env.py`). The file is consumed by `setup_env.apply_to_environ()`, which `config.py` calls BEFORE constructing `Settings()` so wizard writes win over compose-injected env. Allow-list lives in `setup_env.ALLOWED_KEYS` (`VLLM_*`, `MODEL_NAME`, `TTS_VOICE`, `HA_URL`/`HA_TOKEN`, `NTFY_*`, `PUSHOVER_*`, `PAPERLESS_*`). Each spec carries `secret: bool` (secrets never echo `value` on `GET /api/setup/env`) and `service: str` (drives `POST /api/setup/env/validate`). A module-level `_dirty_since_boot` flag flips on the first write — `GET /api/setup/env` surfaces it as `restart_required: true`. Corrupt overrides degrade gracefully (try/except in `config.py`, no startup crash). See `docs/ENV_VARS.md` → Setup wizard overrides file.

### Settings (`/api/config/*`)

All bearer-gated. Reached from the frontend via `/api/proxy/*`. Backed by `orchestrator/routes_config.py`. Every successful PUT/POST/DELETE writes a redacted before/after diff to the `config_changes` SQLite table via `config_writer.log_config_change(panel, before, after)`. YAML writes go through `config_writer.atomic_write_yaml()` (tmpfile + `os.replace` + fsync).

| Method | Path | Purpose |
|--------|------|---------|
| GET    | `/api/config/features` | Returns `{workouts_enabled, meals_enabled, pantry_enabled, jess_advanced}` (read live from `config.settings`). Read-only — no PUT. Consumed by the dashboard server layout + `MobileNav` to hide nav links for features whose routes aren't mounted (`/api/workouts/*`, `/api/meals/*`, `/api/pantry/*`) or are gated behind `JESS_ADVANCED` (Finance), and by the `/workouts`, `/meals`, `/pantry`, `/finance` route layouts to render a "feature not enabled" gate on direct hits. |
| GET    | `/api/config/identity` | Returns `{assistant_name, user_name, adhd_mode, tone_preference, timezone}`. Reads the merged base + overrides view of the user profile. |
| PUT    | `/api/config/identity` | Partial update of the same shape. Writes to `USER_PROFILE_OVERRIDES_PATH` only (base is `:ro`). Calls `reload_profile()` to mutate the singleton in place. |
| GET    | `/api/config/selfcare` | Returns `{categories: {medication: {...}, meal: {...}, water: {...}, movement: {...}}}`. |
| PUT    | `/api/config/selfcare` | Partial `categories.*` merge. Writes to `SELFCARE_SCHEDULE_PATH` and calls `selfcare_schedule.reload_schedule()`. |
| GET    | `/api/config/quiet_hours` | Returns `{start, end, days: ["mon", ...]}`. Source-of-truth shared with the Selfcare panel (lives in the same YAML). |
| PUT    | `/api/config/quiet_hours` | Partial update of the same shape. Same write/reload path as selfcare. |
| GET    | `/api/config/recurring_reminders` | Returns `{rules: [{id, text, cron_expression, target, days_of_week, enabled, ...}, ...]}`. |
| POST   | `/api/config/recurring_reminders` | Create a rule: `{text, cron_expression, target, days_of_week, enabled}`. Cron validated via `croniter`. |
| PUT    | `/api/config/recurring_reminders/{id}` | Partial update. |
| DELETE | `/api/config/recurring_reminders/{id}` | Delete a rule. |
| GET    | `/api/config/voice_style` | Returns `{enabled, presets: {key: phrase}, modes: [...], announcement_types: [...], defaults: {modes: {mode: preset}, announcements: {type: preset}}, override: {style, until} \| null}` (`until` = epoch seconds or null). |
| PUT    | `/api/config/voice_style` | Partial `{modes, announcements}` maps (extra keys rejected). Unknown mode/type or preset → 400. Writes `VOICE_STYLE_PATH` (non-default entries only); audited as panel `voice_style`. Returns the GET view. |
| DELETE | `/api/config/voice_style/override` | Clear the chat-set override (audited as panel `voice_style_override`). Returns the GET view. |

**Recurring expansion job.** `recurring_reminders.expand_due_reminders()` runs every 5 min as APScheduler interval job `id="recurring_reminders_expand"` (registered in `orchestrator.py`). For each enabled rule it walks `croniter` forward (capped at 14 days lookahead), filters by `days_of_week`, materializes due rows into the existing one-shot `reminders` table for `deliver_reminder_job` to pick up, and auto-disables rules whose cron never fires (e.g. `0 0 30 2 *` — Feb 30). Metrics: `bgw_recurring_reminders_expanded_total`, `bgw_recurring_reminders_expand_errors_total`.

### HA Command Format

```json
{
  "entity_id": "light.living_room",
  "service": "turn_on",
  "data": {"brightness": 128, "rgb_color": [0, 0, 255]}
}
```

**Services by domain:**

| Domain | Services | Data |
|--------|----------|------|
| light | turn_on, turn_off, toggle | brightness (0-255), rgb_color |
| switch/fan | turn_on, turn_off, toggle | - |
| climate | set_temperature | temperature |
| cover | open_cover, close_cover | position (0-100) |
| scene | turn_on | - |

**Colors (RGB):** blue=[0,0,255], red=[255,0,0], green=[0,255,0], purple=[128,0,128], white=[255,255,255]

**Brightness:** 50%=128, 75%=191, 100%=255

## Tool Result Cap

The unified loop enforces `MAX_TOOL_RESULT_CHARS = 8000` (~2000 tokens) on every tool result before it is appended to the conversation. This leaves context headroom for the system prompt (static prefix ~1.7k tokens + per-turn dynamic block 0.3–1k tokens including RAG injection; the tool schemas add ~33k chars on top, ~10k tokens total), turn history, and several concurrent tool results within the 131K context window.

**Markup neutralization (2026-10-02):** before the cap, `_cap_tool_result()` runs `_neutralize_control_markup()` on the stringified result. `<tool_call>`, `<tool_response>`, `<think>`, `<function=…>`, `<parameter=…>` tags (open or close, any attributes) and `<|…|>` chat-template specials are rewritten with Unicode angle quotes (`‹tool_call›`) so the text stays readable but can no longer be parsed as a call, fool the `StreamGate`, or forge a chat turn at token level. Tool results are untrusted (web pages, email, calendar descriptions, document notes, OCR, auto-learned memory). Emits a `WARNING` with the tool name and increments `bgw_tool_result_markup_neutralized_total{tool}`.

**Overflow behavior:** `_cap_tool_result()` truncates to 8000 chars and appends a model-facing footer: _"Work with the information above; do not call this tool again to retrieve the rest."_ A `WARNING` log is emitted with the tool name and both char counts.

**Design implication:** Tools must not be designed around returning large blobs (full email threads, long document text, etc.). Summarize or paginate at the tool handler level — the cap is a safety net, not a substitute for well-scoped tool output.

## Tool-Call Loop Guards

Echo-injection defences in `unified_loop.py` (2026-10-02 security review: a `<tool_call>` block quoted from a web page was executed 82 times in one round):

| Guard | Behavior | Metric |
|-------|----------|--------|
| `_xml_fallback_allowed(content, finish_reason)` | XML `<tool_call>` fallback parsing only when `finish_reason != "length"` and the prose outside tool-call/think blocks is ≤ `_XML_FALLBACK_MAX_PROSE_CHARS` (200). Otherwise the markup is treated as quoted content and logged at WARNING. | `bgw_tool_call_source_total{source="xml_rejected"}` |
| `_filter_new_tool_calls()` | Drops calls already executed in earlier rounds, collapses identical `(name, args)` within a round, caps a round at `MAX_TOOL_CALLS_PER_ROUND = 5`. | `bgw_tool_calls_capped_total` |
| `cloud_brain` message strip | Client-supplied `role: "system"` and `role: "tool"` messages are removed before the loop — the orchestrator is the only legitimate producer of tool results. Log: `[UNIFIED] Stripped N client system/tool message(s)`. | — |

Known gaps (not fixed): announcement-ack inference still trusts the client transcript's assistant turns (API-token holders only). HA unlock/cover-open have no confirmation flow, so the `home_assistant` tool refuses them outright (see below).

## Empty-Reply Handling

Fix for blank replies (2026-10-14): llama-server sometimes generated a single EOS token, mostly in voice mode (`enable_thinking: false`) when the history held an empty assistant turn.

| Layer | Behavior |
|-------|----------|
| `cloud_brain._is_empty_assistant` | Empty assistant turns in client history (`/v1/chat/completions`, every client) are dropped before the loop. Log: `Dropped N empty assistant message(s)`. |
| `unified_loop.run_unified_tool_loop` | A round with no tool call and no visible text (think-only counts as empty; a `<tool_call>` echo does not) is retried once, with thinking re-enabled in voice mode. The retry gets its own round (`round_limit += 1`); if tools already ran this turn the retry is text-only (its tool calls are ignored). `TOOL_ROUNDS` and the selfcare safety net use the effective round (retry not counted). Logs: retry INFO, `[UNIFIED] Empty reply after thinking retry` WARNING. |
| Metric / alert | `bgw_llm_empty_reply_total{outcome=retried\|recovered\|still_empty}`; Grafana "Empty Model Replies (/min)" + "Blank Replies (still_empty, 24h)" (Request Flow row, `brain_gateway_sre.py`); `EmptyRepliesReachingUsers` (`still_empty` in 1h → quiet Pushover). |

No post-terminal-tool round cap (by design): the old `max_rounds = round_num + 2` reassignment never took effect and was removed 2026-10-17 — enforcing it would cut off legitimate follow-up actions. `MAX_TOOL_ROUNDS` (plus the empty-retry `round_limit`) bounds the loop; `TOOL_ROUNDS` observes `round_limit`.

## Tool Schemas

### home_assistant
```json
{"entity_id": "light.x", "service": "turn_on", "data": {"brightness": 128}}
```

**Entity listing (schema description).** `tool_definitions.format_ha_entity_listing` lists every entity in `HA_LISTED_DOMAINS` (`light, switch, fan, climate, cover, scene, lock, media_player`), one line per domain: `light: light.kitchen, light.k1 (Kitchen Pendant), …`. Full entity_ids sorted by id; friendly name only when it adds information (NFKC-normalised, allow-listed characters, 32-char cap — names are device-owner-controlled text in the cache prefix). Depends only on ids + names, never state (prompt-cache prefix stability). Bounded by `HA_LISTING_BUDGET_CHARS = 6000`, shared round-robin across domains; overflow shows `(+N more not listed)`. Denied and device-setting entities are not listed.

**Guard (`orchestrator/ha_guard.py` → `check_ha_call`), in order** — shared by `tool_home_assistant`, the legacy NLP path (`ha_client.execute_command`: fast path + `/api/ha/command {"command"}`) and structured `/api/ha/command`. Internal callers (reminders, focus audio, routines, wind-down) call `ha_client.call_service` directly and are not guarded (operator-config targets). Every refusal ends "Nothing was changed.":

| Check | Refusal |
|-------|---------|
| `entity_id` matches `[a-z_]+\.[a-z0-9_]{1,128}`, `service` matches `[a-z_]{1,64}`, `data` is an object | format error |
| Domain in `HA_LISTED_DOMAINS` (alarm etc. not callable) | "the X domain can't be controlled from chat" |
| Not denied: `HA_TOOL_DENY_ENTITIES` globs + the Helios plug object id in any domain (power goes through `helios_power` only) | "X can't be controlled from chat" |
| Not a device setting (skipped only for structured `/api/ha/command`, `allow_device_settings=True`): source `registry` = cached entity_category `config`/`diagnostic`; `name` = object-id word naming a camera/motion/privacy/detection/recording/LED/IR/notification-style setting (any entity — many camera integrations don't categorise their switches; `led`/`indicator` ignored for `light`, `record` for `media_player`); `member` = scene/light group whose state `entity_id` members (one level) include one | "X is a device setting and can't be changed from chat"; metric `bgw_ha_device_setting_refused_total{path=tool\|nlp,source}` |
| Service in `HA_ALLOWED_SERVICES[domain]` | cover/lock: "needs a confirmation step chat doesn't have yet"; others: "isn't available from chat" |
| `media_player.play_media` / `join` data (see below) | "can only play the media library … not an arbitrary URL" / "join can only group media players that chat may control" |
| Entity exists in the cache (tool only); if not, TTL refresh capped at 3 s | unknown → up to 5 "Closest matches: …" (listed domains, not denied, not device settings), metric `bgw_ha_unknown_entity_total`; HA unreachable → "couldn't reach Home Assistant" |

Allowed services: light/switch `turn_on|turn_off|toggle`; fan adds `set_percentage|oscillate|set_preset_mode|set_direction`; climate `set_temperature|set_hvac_mode|set_fan_mode|set_preset_mode|turn_on|turn_off`; scene `turn_on`; media_player play/pause/stop/next/previous/volume/mute/on/off/`select_source|shuffle_set|repeat_set|join|unjoin|play_media`; cover `close_cover|stop_cover|close_cover_tilt|stop_cover_tilt` (no open); lock `lock` (no unlock).

Re-targeting keys are stripped from `data` before the call: `entity_id, area_id, device_id, label_id, floor_id, target, entities, snapshot_entities, scene_id`.

Media limits: `play_media` keeps only `media_content_id`, `media_content_type`, `announce`, `enqueue` (nested `media` / `extra` dropped); `media_content_id` (≤2048, no whitespace, no `..`) must be `media-source://media_source/…` or `media-source://radio_browser/…` (not `tts`/camera), an exact `FOCUS_AUDIO_*_URL` stream, or `{ORCHESTRATOR_URL}/api/audio/…`. Chat can't play arbitrary URLs — focus audio goes through `start_focus`. `join` keeps only `group_members`: 1–16 non-denied `media_player.*` ids.

Refusals: metric `bgw_ha_call_refused_total{path}` (`tool`, `nlp`, `api`).

Device-setting threat: prompt-injected content (email, web page) steering the model to disable a camera or its motion detection. Known limits: member check is one level deep; name match can over-refuse (e.g. a camera floodlight — the dashboard still works). Add camera-touching scenes, or uncategorised camera switches with unusual names, to `HA_TOOL_DENY_ENTITIES` as a backstop.

### search_memory
```json
{"query": "current projects"}
```

### update_data
```json
{"action": "add_medication", "name": "Adderall", "dose": "20mg", "schedule": "morning"}
{"action": "update_medication", "name": "Vyvanse", "skip_weekends": true}
{"action": "update_medication", "name": "Vyvanse", "days": ["mon", "wed", "fri"]}
```

**Actions:** add_medication, remove_medication, update_medication, add_project, update_project_status, add_project_step, complete_step

**Per-med weekday scheduling.** `add_medication` / `update_medication` accept an optional `days` (subset of `mon,tue,wed,thu,fri,sat,sun`) or the `skip_weekends: true` shorthand (→ `[mon,tue,wed,thu,fri]`). Explicit `days` wins if both are sent; unknown tokens are dropped. Sending `days: []` (empty) on `update_medication` **clears** the restriction (take every day again); omitting `days` leaves the schedule unchanged. Weekday matching is index-based (`datetime.weekday()`), not locale-dependent `strftime`. The value is normalized and stored as a `days` list on the med dict in `medications.yaml`:

```yaml
daily:
  morning:
    - name: Vyvanse
      dose: 30mg
      purpose: ADHD
      days: [mon, tue, wed, thu, fri]   # optional; absent = every day
```

`selfcare_manager._check_meds` and `evening_meds_status` skip a med whose `days` excludes today (drug-holiday support, e.g. weekends-off stimulants); a malformed/empty `days` fails open (still reminds). The compact renderer (`get_data` + injected prompt block) surfaces a schedule hint — `Vyvanse 30mg (Mon–Fri)` — and the `/personal-facts` page shows it as a badge. `update_medication` also relocates a med between buckets when `schedule` changes, and returns an honest "nothing to update" (without writing) when a found med has no changed field.

### set_reminder
```json
{"reminder_text": "call mom", "time": "in 30 minutes", "target": "both"}
```

### cancel_reminder
```json
{"reminder_id": "abc123"}
```

### start_focus
```json
{"task": "writing", "duration": 30, "block_sites": true}
```
- `block_sites` (bool, optional, default true): *requests* DNS blocking. The result only states sites are blocked when `pihole_client.blocking_confirmed(result)` is true (multi-client `success` AND aggregated `details["domains_toggled"] > 0`); no-ops (blocking disabled, no Pi-holes, empty group) are not claimed. Blocking is deprecated on the maintainer deployment (`FOCUS_BLOCKING_ENABLED=false`) — see `docs/FOCUS_AND_PIHOLE.md`.

### stop_focus
```json
{}
```

### focus_status
```json
{}
```

### web_search
```json
{"query": "weather Houston today", "category": "general", "time_range": "day"}
```

### check_calendar
```json
{"days_ahead": 7}
```
- `days_ahead` (int, optional): Number of days to look ahead. Default: 7. Use 1 for today, 2 for tomorrow.

### create_calendar_event
```json
{"title": "Pickleball at Honcho", "start_time": "2026-02-26T19:00:00", "duration_minutes": 60, "location": "Honcho"}
```
- `title` (string, required): Event title
- `start_time` (string, required): ISO 8601 datetime
- `duration_minutes` (int, optional): Default 60
- `description` (string, optional): Event description
- `location` (string, optional): Event location

### focus_sprint
```json
{"action": "next_sprint", "duration_minutes": 25}
```
- `action` (string, required): `next_sprint`, `extend`, or `end_session`
- `duration_minutes` (int, optional): Override sprint length or minutes to add for extend

### start_routine
```json
{"routine_id": "morning"}
```
- `routine_id` (string, required): `morning` or `evening`

### routine_action
```json
{"action": "done"}
```
- `action` (string, required): `done`, `skip`, `pause`, `resume`, `stop`, or `status`
- Side effect on `"done"`: `routine_manager.advance_step` calls `selfcare_manager.mark_selfcare_from_routine_step(step)`, which dispatches to `record_medication_logged`/`record_meal_logged`/`record_hydration_logged`/`record_movement_logged` based on the medication/meal/water/movement keyword map against step id/label. Suppresses the corresponding nudge. NOT fired on `"skip"` or auto-end `"stop"`.

### routine_status
```json
{}
```

### selfcare_log
```json
{"action": "meal", "detail": "lunch"}
```
- `action` (string, required): `meal`, `medication`, `water`, or `movement`
- `detail` (string, optional): Medication name or meal type
- Side effect: if an active routine's current step matches `action` (word-boundary match on step id/label against the medication/meal/water/movement keyword map), the routine auto-advances via `routine_manager.advance_step("done")`. Fire-and-forget; never blocks the selfcare write.

### decide_for_me
```json
{"domain": "work", "constraints": "under 30 minutes"}
```
- `domain` (string, required): `work`, `food`, `general`, or `overwhelm`
- `constraints` (string, optional): Constraints like "quick", "healthy", "under 30 minutes"

### bookmark_context
```json
{"description": "writing the API docs"}
```
- `description` (string, optional): What the user is working on (auto-detected from active focus/task if omitted)

### recall_context
```json
{"count": 3}
```
- `count` (int, optional): Number of recent contexts to return (default: 3)

### check_system
```json
{"query": "system_health"}
```
- `query` (string, required): `morning_briefing`, `calendar_poll`, `email_poll`, `reminders`, `focus_timer`, `temperature`, `system_health`, or `recent_errors`

### memory/add
```json
{
  "content": "Document text to store",
  "category": "personal",
  "source": "api",
  "tags": ["adhd", "pattern"]
}
```
- `content` (string, required): Text to embed and store
- `category` (string, optional): Category label
- `source` (string, optional): Source identifier
- `tags` (array of strings, optional): Stored as comma-separated string in ChromaDB metadata

### generate_workout
```json
{"location": "home"}
```
- `location` (string, optional): where they're training, as said. Resolved by `workout_setup.find_location` — exact (case/punctuation-insensitive) match, else a unique containment match ("downtown" → "Downtown Gym", "at home" → "Home").
- No `location` + exactly 1 location → uses it. No `location` + ≥2 locations → returns "Not generated yet — ask the user where they're training today: … (last time: X)" and counts `ask_location`; the model asks once and calls again. No locations → whole catalog.
- Unknown location → "Not generated" text listing known locations (or offering `workout_setup` add_location when none exist).
- Follows the active weekly program if set, else the adaptive no-program logic (`docs/WORKOUTS_AND_MEALS.md`).

Returns the plan as text (location, program day "Label (n of N)", reasoning incl. deload week, one line per exercise with sets×reps @ weight and its progression note, e.g. "+5 lb from last time") so the model keeps it in context for follow-ups. Model should NOT read the plan aloud — user is at the gym.

### workout_setup
```json
{"action": "set_program", "goal": "muscle", "days_per_week": 4, "session_minutes": 45}
```
```json
{"action": "update_location", "name": "Home", "add_equipment": ["pull_up_bar"], "limits": {"dumbbell_max_lbs": 50}}
```
- `action` (required): `get` | `set_program` | `clear_program` | `add_location` | `update_location` | `remove_location`.
- `get` → JSON `{program: {goal, days_per_week, session_minutes, deload_every_weeks, next_is_deload, split, next_day} | null, locations: [{name, equipment, limits}], equipment_vocabulary, templates}`. The schema tells the model to call `get` first to learn equipment names.
- `set_program`: `goal` `strength|muscle|general` (default `general`), `days_per_week` 2–6, `session_minutes` 20–120, optional `deload_every_weeks` (0 = off, 3–12; omit to keep). Reply text states the deload cadence ("Deload week every N weeks." / "No scheduled deload weeks.").
- `add_location`: `name`, `template` `commercial_gym|home_basic|empty` (default `commercial_gym`), optional `limits`. (Explicit equipment lists are a dashboard-only option.)
- `update_location`: `name` (fuzzy-resolved like `generate_workout`), optional `new_name`, `add_equipment`, `remove_equipment`, `limits` (`dumbbell_max_lbs`, `dumbbell_step_lbs`, `kettlebell_weights_lbs`; `null` clears one).
- `remove_location`: `name` must match exactly (case/punctuation-insensitive) — no fuzzy match, so a near-name never deletes the wrong place.
- Validation and limits are shared with the REST routes (`orchestrator/workout_setup.py`); errors return `{"ok": false, "error": ...}` JSON (location misses add `known_locations`). Writes are audited (`config_changes`, panel `workouts`, `changed_by=jess`).
- Gated with the other workout tools (`WORKOUT_TOOL_NAMES`, `WORKOUTS_ENABLED`).

### log_set
```json
{"exercise": "Goblet Squat", "weight_lbs": 35.0, "reps": 10, "rpe": 8}
```
- All weights in lbs.
- Side effect: after persisting the set, calls `selfcare_manager.record_movement_logged(f"set:{exercise_name}")`, which resets both `last_movement_nudge` and `sitting_since` (prevents sitting-timer nudges while at the gym). Wrapped in try/except; never blocks the set write.

### workout_status
```json
{"action": "today|history|how_to", "days": 7, "exercise": "Goblet Squat"}
```
- `action` required. `days` for `history` (default 7); `exercise` for `how_to`.
- `how_to` → `{exercise, reference, steps: [≤8, ≤500 chars each], muscles, note}`; no guide → `{exercise, steps: null, video_url, note}` (note tells the model not to improvise form cues). Source: `docs/WORKOUTS_AND_MEALS.md` → Exercise how-to library.

### modify_workout
```json
{"workout_id": 1, "remove_exercises": ["Back Squat"], "add_exercises": ["Leg Press"]}
```
- Only planned (uncompleted) sets are removed. Adds are validated BEFORE anything is removed: names not in the catalog, already in today's plan (duplicates), or needing gear the workout's location lacks are returned in `skipped_exercises` (`{name, reason, alternatives?}` — up to 2 same-pattern exercises the location supports).
- `removal_held: true` when adds were requested, none fit, and removals were requested — the removal is not applied (a failed swap must not just delete the original).
- Added exercises get the active program's rep scheme (if the workout belongs to it) and the location's load caps.

### log_meal
```json
{"name": "chicken and rice", "calories": 650, "notes": "post-gym"}
```
- Calories-only (v1). Independent of `selfcare_log` (which is nudge tracking, not calorie accounting).
- `auto_log` (bool, optional): If `true`, skips confirmation when called after a photo estimate.

### paperless_save
```json
{"filename": "tax-q3-2026.pdf", "title": "Q3 2026 Taxes", "correspondent": "IRS", "document_type": "tax", "tags": ["taxes", "2026"]}
```
- `filename` (string, required): Basename of a file already present in `PAPERLESS_INBOX_PATH`. Handler rejects path separators, `..`, absolute paths, null bytes, and symlink escape (`Path.resolve() + relative_to(inbox)`).
- `title`, `correspondent`, `document_type` (string, optional): Paperless metadata. Inferred from filename/OCR when omitted.
- `tags` (array of strings, optional): Added to `PAPERLESS_DEFAULT_TAGS`. Missing tags are created by Paperless if the server setting allows; otherwise ignored.
- File read uses `asyncio.to_thread` to avoid blocking the event loop. Size cap enforced before `read_bytes()`. Auto-disabled bridge surfaces as a structured "skipped" tool result rather than raising.

### ask_expert
```json
{"question": "Why might my 2025 gaming spend have spiked in November?"}
```
- `question` (string, required): A self-contained question. The expert (formerly Qwen3-32B Thinking on Saturn 3090, port 8084 — deprecated/disabled on the live deployment since 2026-09-28, so the tool is not in the live tool list) is stateless — pass all needed context in the question.
- One-shot, blocking. Latency 30-150s in practice; 180s timeout. The primary should warn the user before invoking.
- llama.cpp `--jinja` mode separates `message.content` (final answer) from `message.reasoning_content` (the `<think>` trace); only the final content is returned.
- Auto-disabled when `EXPERT_ENABLED=false` or `EXPERT_MODEL_URL` is empty — returns a short string explaining the disabled state instead of raising. Circuit breaker opens after `EXPERT_CIRCUIT_BREAKER_FAILURES` (default 3) consecutive failures, half-opens after `EXPERT_CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default 120s).
- Metrics: `bgw_expert_call_total{result}`, `bgw_expert_call_latency_seconds`, `bgw_expert_circuit_open` (gauge), `bgw_expert_reasoning_tokens` (histogram).

### query_budget
```json
{"question_type": "analyze", "analysis_question": "What patterns stood out in my 2025 gaming spend?", "year": 2025, "category": "Gaming"}
```
- `question_type` (enum, required): `total | by_category | by_month | top_payees | outliers | analyze`. Narrow types return structured facts for a single dimension. `analyze` gathers totals + top 5 categories + top 5 payees + up to 36 months + top 3 outliers (respecting filters) and delegates the synthesis to the expert reasoning model (`handle_ask_expert`) in a single tool call.
- `analysis_question` (string, required when `question_type="analyze"`): The user's synthesis question, passed verbatim to the expert.
- Filters (all optional): `year`, `month`, `category`, `payee`, `min_amount`, `max_amount`.
- Response for `analyze`: `{expert_synthesis: str | null, expert_error: str | null, data: {...}, ...}`. `expert_synthesis=null` + non-null `expert_error` when the expert is unreachable, disabled, or its circuit is open — the `data` block is always populated so the primary model can fall back to surface-level summary.
- `budget_manager.query()` is `async` (awaits the expert). `by_month` internally capped at 36 entries.

### helios_power
```json
{"action": "status"}
```
- `action` (enum, required): `wake | sleep | status`. `wake` = power Helios on (HA `switch.turn_on`, debounced), `sleep` = power off (HA `switch.turn_off`, a hard cut), `status` = report inferred running/asleep from switch state + power draw.
- Only exposed to the LLM when `HELIOS_WAKE_ENABLED=true` (filtered via `HELIOS_TOOL_NAMES` in `get_all_tools()`); the handler stays registered and self-gates. Routes through `orchestrator/helios_power.py` — same dicts as `POST /api/helios/{wake,sleep}` + `GET /api/helios/power`.

### voice_style
```json
{"action": "set", "style": "angry"}
{"action": "set", "style": "quiet", "scope": "ongoing", "duration_hours": 1}
```
- `action` (enum, required): `set | reset | status`. `reset` clears both one-reply and ongoing styles.
- `style` (enum, required for `set`; built from `voice_style.PRESETS` minus `auto`): `calm | gentle | soothing | quiet | warm | thoughtful | encouraging | upbeat | proud | energetic | firm | excited | sad | angry | scared | whisper | sultry | sarcastic`.
- `scope` (enum, optional): `this_reply` (default) = only the reply being generated (RAM, cleared on next routed user message or 300 s, chat only, not audited); `ongoing` = persistent override. Omitted scope + `duration_hours` → `ongoing`; `duration_hours` with explicit `this_reply` is ignored (result says so).
- `duration_hours` (number, optional, ongoing only): auto-revert after N hours, `0 < N ≤ 168`. Omit = until reset.
- Non-terminal; in `VOICE_TOOL_NAMES`; listed as state-changing in the prompt's EXTERNAL CONTENT rule. Ongoing override stored in `app_state` key `voice_style_override`, audited as `config_changes` panel `voice_style_override`; ongoing acting presets (`excited|sad|angry|scared|whisper|sultry|sarcastic`) skip announcements. Counted per synthesis call (per sentence) in `bgw_tts_style_applied_total{source=this_reply|override|mode|announcement|none, style}`. See `docs/VOICE_AND_TTS.md` → Voice style.

### pantry
```json
{"action": "out", "item": "olive oil"}
```
- `action` (enum, required): `view | add | use | out | set | search | remove`. Other fields: `item`, `count` (number), `level` (`full|half|low|out`), `location` (`fridge|freezer|pantry`), `category` (add only: `produce|dairy|protein|grain|legume|canned|condiment|spice|frozen|snack|beverage|bakery|prepared|other`), `add_to_shopping` (out only, default true).
- `out` also adds the item to the `grocery` shopping list (dedup) unless `add_to_shopping=false`. `set` needs `count`, `level`, or `location`. `remove` requires the exact item name.
- Only exposed when `PANTRY_ENABLED=true` (`PANTRY_TOOL_NAMES`); handler self-gates. Photo stock-takes and receipt scans are a dashboard flow, not a tool.

### meal_plan
```json
{"action": "swap", "day": "2026-10-20", "slot": "dinner"}
```
- `action` (enum, required): `view | generate | accept | swap | recipe | cooked | uncook | ate | uneat | skip`. `day` (`YYYY-MM-DD`, default today) and `slot` (`dinner|batch|other`, default `dinner`; `batch` = the batch prep; `other` = a meal not in the plan, `cooked`/`uncook` only) apply to `swap`/`recipe`/`cooked`/`uncook`/`skip`.
- `ingredients` (array of strings) + `dish` (string, optional): for an off-plan `cooked` — pantry items used, fuzzy-matched to inventory (max 30). `cooked` + `slot: other` → off-plan; `cooked` with `ingredients` and slot `dinner` (or omitted) → off-plan only when that day has no dinner in an **active** plan (a draft doesn't count), else the planned dinner. `uncook` + `slot: other` undoes the latest off-plan meal (≤48 h).
- `cooked` marks the entry cooked and deducts its ingredients (reply lists used / running low / not tracked); `uncook` undoes the latest cook; `skip` marks a planned meal skipped; `ate` eats one portion from the soonest-expiring prep batch and logs the meal (the model must not also call `selfcare_log`); `uneat` undoes the newest eat of the last 24 h ("I didn't actually eat that"). Detail: `docs/PANTRY.md` → Consumption tracking.
- `generate` starts a background plan (~1 min) and returns immediately; fails fast when the brain is asleep. `accept` accepts the current draft (grocery list + cooking reminders) — only on the user's explicit say-so. `view` returns the draft, else the active plan (≤7500 chars; recipe names labelled as data).
- Only exposed when `PANTRY_ENABLED=true` (`PANTRY_TOOL_NAMES = {pantry, meal_plan}`); handler self-gates. Listed in the EXTERNAL CONTENT state-changing tool list.

## ChromaDB Schema

**Chunk ID:** `chunk::{path}::{section}::{index}::{hash[:12]}`

```json
{
  "metadata": {
    "file_path": "path/to/file.md",
    "section": "h2:Section Name",
    "chunk_index": 0,
    "kind": "chunk",
    "tags": "adhd,pattern"
  }
}
```

## SQLite Schema (`brain_state.db`)

Defined in `orchestrator/state_store.py::SCHEMA_SQL`. Listing only the tables not covered elsewhere in this doc.

### `recurring_reminders`

CRUD source for the `/api/config/recurring_reminders` endpoints. Rows are read every 5 min by `expand_due_reminders()` and materialized into one-shot `reminders` rows for `deliver_reminder_job`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | Auto-increment rule id. |
| `text` | TEXT NOT NULL | Reminder text (HTML-escaped before Pushover delivery — F-013). |
| `cron_expression` | TEXT NOT NULL | Standard 5-field cron, validated with `croniter`. Impossible crons (e.g. `0 0 30 2 *`) are detected at expand time and the rule is auto-disabled. |
| `target` | TEXT | Speaker/device target — same shape `set_reminder` uses. |
| `days_of_week` | TEXT | Comma-separated `mon,tue,...` filter applied on top of the cron. Empty = all days. |
| `enabled` | INTEGER | `1` enabled, `0` disabled. Set to `0` automatically on impossible crons. |
| `last_expanded_at` | TEXT | ISO 8601 timestamp of the last successful expansion pass; used to dedupe materialized rows. |
| `created_at` / `updated_at` | TEXT | ISO 8601 timestamps. |

### `config_changes`

Append-only audit log written by `config_writer.log_config_change(panel, before, after)`. Every successful PUT/POST/DELETE on `/api/config/*` adds one row. Sensitive fields are masked through `_redact()` before persistence.

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | Auto-increment. |
| `panel` | TEXT NOT NULL | e.g. `identity`, `selfcare`, `quiet_hours`, `recurring_reminders`, `workouts` (program/location writes from routes and the `workout_setup` tool). |
| `before` | TEXT | Redacted JSON snapshot of the prior value. |
| `after` | TEXT | Redacted JSON snapshot of the new value. |
| `changed_at` | TEXT | ISO 8601 timestamp. |

### Workout program builder tables

Created by `SCHEMA_SQL`; `init_db()` adds the new columns to pre-existing DBs (one `[STATE] Migrated <table>: added <col>` log line each).

| Table / column | Notes |
|----------------|-------|
| `workout_locations` | `id`, `name` (UNIQUE, `COLLATE NOCASE`), `equipment` (JSON list of tokens), `limits` (JSON object), `last_used_at` (stamped on generate; drives the default location), `created_at`, `updated_at`. |
| `workout_programs` | `id`, `goal`, `days_per_week`, `session_minutes`, `deload_every_weeks` (INTEGER NOT NULL DEFAULT 6; 0 = off), `active` (0/1), `created_at`. One active row; replaced programs stay as inactive history. Goal/minutes/deload changes update the active row in place. |
| `exercises.requires` | JSON list of equipment tokens the exercise needs (default `[]`). Written only by the startup seed (upsert). |
| `workouts.location_id` / `program_id` / `program_day` | Nullable. Set on generate. `program_day` is the index into the split; the next day is computed from the newest workout in the program with ≥1 completed set. `location_id` may dangle after a location is deleted. |
| `workouts.is_deload` | INTEGER NOT NULL DEFAULT 0. 1 = program deload session; excluded from progression history, counted for comeback timing and as the training-block boundary. |
| `workout_sets.notes` (set 1) | Holds the exercise's progression note for the planned session. |

### Pantry tables (F-015)

Created unconditionally by `SCHEMA_SQL`. Timestamps ISO 8601 local.

| Table | Columns / notes |
|-------|-----------------|
| `pantry_items` | `id`, `name`, `category`, `location` (`fridge\|freezer\|pantry`), `track_mode` (`count\|level\|staple`), `qty` (count mode), `level` (`full\|half\|low\|out`), `status` (`active\|probably_gone\|gone`), `shelf_life_days` (NULL = keeps), `acquired_at`, `last_confirmed_at`, `barcode`, `source` (`manual\|chat\|scan\|receipt\|barcode`), `created_at`, `updated_at`. Index on `status` |
| `pantry_events` | Append-only ledger: `id`, `item_id`, `kind` (`add\|consume\|adjust\|remove`), `source`, `before_json`, `after_json`, `ref_type`/`ref_id` (e.g. scan; `cook` / `uncook` → plan entry id; `cook_other` / `uncook_other` → `pantry_cooks.id`; `uncook` marker rows have NULL `item_id`), `note`, `at`. Indexes on `(item_id, at)` and `(ref_type, ref_id)` |
| `pantry_scans` | `id`, `kind` (`fridge\|freezer\|pantry\|receipt`), `photo_paths` (JSON list under `PANTRY_PHOTOS_DIR`), `image_sha256` (receipts: every photo's hash, comma-joined), `status` (`queued\|processing\|pending_review\|committing\|committed\|discarded\|failed`), `proposed_json` (receipts: cut to `{receipt:{store,date,total}}` after commit/discard), `error`, `created_at`, `committed_at`. Index on `status` |
| `pantry_aliases` | `alias_norm` PK (`receipt_scanner.alias_key(raw)`), `item_name`, `hits`, `updated_at`. Receipt text → item name, learned only from user corrections / re-confirmed remembered lines |
| `barcode_cache` | `barcode` PK, `name`, `category`, `payload_json`, `fetched_at`. Misses are cached too (re-tried after 7 days) |
| `recipes` | `id`, `name`, `diet_tags`, `servings`, `batchable`, `minutes`, `method` (steps JSON), `est_kcal`, `est_satfat_g`, `est_fiber_g`, `source` (`llm`), `times_cooked`, `rating` (5 up / 1 down / NULL), `created_at`. Never pruned |
| `recipe_ingredients` | `id`, `recipe_id`, `item_name`, `amount`, `consume` (default `some`), `optional`. Index on `recipe_id` |
| `meal_plans` | `id`, `week_start`, `diet_profile`, `status` (`generating\|draft\|active\|failed\|discarded\|replaced`), `notes`, `warnings` (JSON), `error`, `settings_json` (diet settings snapshot), `reminder_ids` (JSON, `mp-` ids, 10 hex chars since Phase 4), `created_at`, `accepted_at`. Index on `(status, week_start)` |
| `meal_plan_entries` | `id`, `plan_id`, `day`, `slot` (`prep\|lunch\|dinner`), `recipe_id`, `mode` (`batch\|portion\|fresh`), `portions`, `status` (`planned\|cooked\|eaten\|skipped`, default `planned`), `prep_batch_id` (lunch entries → the cooked batch; added by migration). Index on `plan_id` |
| `prep_batches` | `id`, `plan_id`, `entry_id`, `recipe_id`, `cooked_at`, `portions_made`, `portions_left`, `good_until` (cook date + 4 days), `location` (default `fridge`), `status` (`active\|finished\|expired\|undone`). Index on `status` |
| `pantry_cooks` | Off-plan meals: `id`, `dish` (allowlist-cleaned, may be empty), `source` (`dashboard\|chat`), `cooked_at`, `status` (`active\|undone`). Deductions are `pantry_events` with `ref_type` `cook_other` |
| `meal_plan_reminders` | `reminder_id` PK (`mp-` id), `entry_id`. Links a cooking reminder to its plan entry so a Done tap = cooked; rows deleted when the reminder is cancelled or its entry swapped |

### Dashboard inbox + Web Push tables

| Table | Columns / notes |
|-------|-----------------|
| `inbox_items` | `id` (32 hex), `kind` (`reminder\|selfcare\|message`), `ref` (reminder id, selfcare kind or message ref), `text` (≤500 chars; ≤2000 for `message`), `created_at`, `resolved_at` (NULL = open), `resolution` (`done\|snoozed\|dismissed\|superseded`), `resolved_via` (`dashboard`, `ntfy`, `telegram`, `logged`, `redelivered`, `renewed`, …). Index `(resolved_at, created_at)`. Rows deleted after 30 days |
| `webpush_subscriptions` | `endpoint` PK, `p256dh`, `auth`, `created_at`, `last_ok_at`. One row per device, max 20 |

## SQLite Schema (`finance.db`)

Defined in `orchestrator/finance_manager.py::SCHEMA_SQL`; created by `setup_finance()` → `init_db()`, called unconditionally at startup. `_migrate_schema()` upgrades a YNAB-era DB in place (adds `external_id`, backfills `"ynab:<id>"`, copies the old category mappings). Budget-sync tables only:

| Table / column | Notes |
|----------------|-------|
| `transactions.external_id` | `"<provider>:<id>"` for synced rows (`actual:<uuid>`; legacy `ynab:<id>`), NULL for manual. Unique index `idx_transactions_external_id`. |
| `transactions.source` | `actual` \| `ynab` (legacy, outside the sync window) \| `manual`. |
| `budget_sync_state` | Single row (`id = 1`): `provider`, `budget_name`, `last_synced_at`, `last_attempt_at`, `last_error` (≤500 chars, credential-stripped), `last_result` (JSON). |
| `transactions.discretionary_override` | Nullable 0/1, added by `_migrate_schema()`. NULL = follow `category_mapping`; set by `/transactions/reclassify` on synced rows. `apply_snapshot` and `update_category_mapping` apply `COALESCE(discretionary_override, …)`, so overrides survive syncs and mapping changes. |
| `category_mapping` | `category_name` PK, `is_discretionary` 0/1. Unmapped categories count as non-discretionary. |

## Environment Variables

See `.env.example` for full list. Key vars:

| Variable | Purpose |
|----------|---------|
| NODE_*_IP | Cluster node IPs |
| HA_TOKEN | Home Assistant token |
| API_TOKEN | Orchestrator API auth |
| CHROMA_PERSIST | ChromaDB path |
| MIN_COS, TOP_K | RAG params |
| GOOGLE_CREDENTIALS_PATH | OAuth2 credentials JSON path |
| GOOGLE_TOKEN_PATH | OAuth2 token JSON path |
| CALENDAR_POLL_INTERVAL | Minutes between calendar polls (default: 5) |
| CALDAV_*, CALENDAR_GOOGLE_ENABLED, PHONE_CALENDAR_MAX_AGE_HOURS | Calendar sources — see `docs/ENV_VARS.md` → Calendar sources |
| MORNING_BRIEFING_TIME | HH:MM for morning briefing (default: 07:00 in config.py; .env.example sets 07:30) |
| MORNING_BRIEFING_ENABLED | true/false (default: true) |
| EVENING_BRIEFING_TIME | HH:MM for the evening shutdown ritual (default: 21:30) |
| EVENING_BRIEFING_ENABLED | true/false (default: true) |
| REAL_STREAMING_ENABLED | true/false (default: true) — real SSE token streaming on /v1/chat/completions; false = buffered chunking |
| WIND_DOWN_ENABLED | true/false (default: true) — T-60 lights + T-30 nudge ahead of bedtime |
| WIND_DOWN_BEDTIME | HH:MM bedtime anchor (default: 22:30) |
| WIND_DOWN_SCENE | Comma-separated HA scene ids for the lights rung (empty = skip) |
| WIND_DOWN_SHORT_NIGHT_HOURS | Short-night threshold for the gentle morning briefing (default: 6.5) |
| ACTUAL_* | Actual Budget sync (URL, password, budget file, interval, window, fun-money category) — see `docs/ENV_VARS.md` → Finance |

## External APIs

| API | Base URL | Auth |
|-----|----------|------|
| Home Assistant | http://10.0.0.248:8123/api | Bearer token |
| Google Calendar | https://www.googleapis.com/calendar/v3 | OAuth2 bearer token |
| SearXNG | http://searxng:8080 (internal) | None |
| Actual Budget (via `actualpy`, read-only) | http://actual-budget:5006 (Jupiter `actual-budget`, over `gateway-net`; host port is loopback-only) | Server password (`ACTUAL_PASSWORD`) |
