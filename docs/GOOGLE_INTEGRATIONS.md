# Google Integrations

## Google Calendar

Google Calendar via OAuth2 — one of the sources merged by `orchestrator/calendar_sources.py` (with self-hosted CalDAV and the work-calendar push; see `docs/CALENDAR.md`). Read while `CALENDAR_GOOGLE_ENABLED=true`; written by `create_calendar_event` only when CalDAV is off. `GoogleCalendarClient.list_window` pages through results; the OAuth token refresh runs off the event loop.

**Status:** Fully deployed and configured on Jupiter. OAuth2 token generated and mounted.

**Setup (one-time on dev machine):**
1. Google Cloud Console -> create project -> enable Calendar API + Gmail API -> create OAuth2 Desktop credentials
2. Add your Google account as a test user (OAuth consent screen -> Test users)
3. Download `credentials.json` -> `credentials/google_credentials.json`
4. Run consent flow:
   ```bash
   python3 -m venv /tmp/google-auth-venv
   /tmp/google-auth-venv/bin/pip install google-auth google-auth-oauthlib
   /tmp/google-auth-venv/bin/python orchestrator/google_setup.py \
     --credentials credentials/google_credentials.json \
     --token-output credentials/google_token.json
   ```
5. Copy credentials to Helios:
   ```bash
   scp credentials/google_credentials.json labadmin@10.0.0.195:/opt/gateway_mvp/credentials/
   scp credentials/google_token.json labadmin@10.0.0.195:/opt/gateway_mvp/credentials/
   ```
6. Restart orchestrator: `docker compose restart orchestrator`

**Proactive features (APScheduler):**
- Calendar polling: every 5 min, announces events from all merged sources starting within 2 hours via TTS (travel-time awareness for physical locations; not for work Exchange events). Announced state is keyed on `alert_key(ev)` (title + start), so an event in two sources is announced once
- Tiered countdown alerts: configurable tier thresholds (default: 60/30/15/5 min before event). Selects the closest un-announced tier (smallest-to-largest), and auto-marks larger tiers as notified on catch-up so you never hear a stale "in about an hour" when an event is 28 min away. Custom tier values get generic message templates automatically.
- Morning briefing: 7:00 AM on bedroom pair, announces today's events + pending reminders via TTS
  - **Routine gate:** if a guided routine is active (not paused, < 3h old) or a routine trigger fires within 120s (checked after a 1s yield, to catch the same-slot 07:00 case), the spoken briefing is skipped and its text is mirrored to Telegram instead; the parked item stays parked. With the default 07:00 morning routine this means the briefing is Telegram-only daily — see `jess-features/F-006-routine-scaffolding.md` → Routine vs morning briefing.
  - Metric: `bgw_morning_briefing_outcome_total{outcome=spoken|skipped_routine|suppressed|failed}` (pre-seeded at 0)

**Config (env vars):**
- `CALENDAR_POLL_INTERVAL` — minutes between polls (default: 5, defensive parsing with fallback)
- `CALENDAR_TIERED_ALERTS` — enable tiered countdown alerts (default: true)
- `CALENDAR_ALERT_TIERS` — comma-separated tier thresholds in minutes (default: 60,30,15,5, defensive parsing with fallback)
- `MORNING_BRIEFING_TIME` — HH:MM 24h format (default: 07:00)
- `MORNING_BRIEFING_ENABLED` — true/false (default: true)
- `MORNING_BRIEFING_SPEAKER` — HA media_player entity (default: media_player.bedroom_pair)

**Key files:** `orchestrator/google_calendar.py`, `orchestrator/google_auth.py`, `orchestrator/google_setup.py`

## Gmail

Read-only Gmail access via OAuth2. Tools: `check_email`, `search_email`.

**Setup (after Calendar is already configured):**
1. Google Cloud Console -> enable **Gmail API** (same project as Calendar)
2. Delete existing `credentials/google_token.json`
3. Re-run consent flow (step 4 above) — will now request Calendar + Gmail permissions
4. Copy new token to Jupiter (step 5 above)
5. Restart orchestrator: `docker compose up -d --build orchestrator`

**Tools:**
- `check_email` — check inbox for recent/unread emails. Optional query, unread_only filter
- `search_email` — search with Gmail query syntax (`from:`, `subject:`, `has:attachment`, `newer_than:`, etc.)

**Proactive features (APScheduler):**
- Email polling: every 30 min, announces new unread emails (Primary inbox only — skips promotions/social/forums/updates) via TTS

**Config (env vars):**
- `EMAIL_POLL_INTERVAL` — minutes between polls (default: 30, defensive parsing with fallback)
- `EMAIL_POLL_ENABLED` — true/false (default: true)

**Key files:** `orchestrator/google_gmail.py`

**OAuth2 scopes (all three in google_auth.py):**
- `calendar.readonly` — read calendar events
- `calendar.events` — create/modify calendar events
- `gmail.readonly` — read email messages

## Phone Calendar Sync (work calendar push)

The iPhone Shortcut posts the **work (Exchange) calendar** to `/api/calendar/sync` as a full snapshot with `"source": "exchange"`. `calendar_sources` merges it (read-only) with CalDAV and Google for every caller — `check_calendar`, briefings, wind-down, ambient, `poll_calendar`, the dashboard. Shortcut build steps, merge/dedup rules, staleness (`PHONE_CALENDAR_MAX_AGE_HOURS`, 26 h) and failure reporting: `docs/CALENDAR.md`. Request/response shapes and limits: `TECHNICAL_REFERENCE.md` → Calendar & Email.

**Legacy push retired:** the old all-calendars Shortcut (no `source` key) is still accepted but stored as `phone` and ignored — no events, no "couldn't reach" caveat, no stale alert.

**Persistence:** the last snapshot is saved to `/app/data/phone_calendar.json` and loaded at startup.

**iOS date format handling:** iPhone Shortcuts sends dates like `"Mar 4, 2026 at 10:00 AM"` with narrow no-break space (`\u202f`). `calendar_sources.parse_phone_datetime()` normalizes Unicode spaces and handles multiple date formats.

**Known quirk:** iOS Shortcuts serialization adds trailing spaces to dict keys (`"calendar "` vs `"calendar"`). Code handles both.

## Travel-Time Calendar Alerts

Calendar polling checks events with physical locations against Google Maps Directions API for real-time traffic. Announces "leave in X minutes" alerts instead of just "event in X minutes".

**How it works:**
1. Event within 2 hours with a physical location (not virtual meetings; never for work Exchange events, whose locations come from external invites — applies to the poller and the evening briefing)
2. Maps API returns drive time with traffic from home address
3. `leave_by = event.start - travel_time - buffer`
4. Announces "You need to leave in X minutes for Event. It's a Y minute drive."
5. If leave_by already passed: "You should leave now for Event."

**Config (env vars):**
- `GOOGLE_MAPS_API_KEY` — Google Maps Directions API key
- `TRAVEL_TIME_BUFFER` — extra minutes buffer (default: 10)
- Home address hardcoded in `shared.py` as `HOME_ADDRESS`

**Virtual meeting detection:** `travel_time.is_virtual_location()` skips Maps for links (`http`, `www`) and meeting names (Zoom, Teams/Microsoft Teams, Google Meet, Webex, GoTo Meeting, Amazon Chime, BlueJeans, Whereby). Maps errors log only the exception type.

**Spoken location:** `travel_time.spoken_location()` gates every calendar TTS line (countdowns, 5-min prep hint, leave-now/soon). Virtual or >100-char locations are not read out; the drive line becomes "It's a N minute drive."

**Key files:** `orchestrator/travel_time.py`, `orchestrator/background_jobs.py`
