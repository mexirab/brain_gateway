# Self-hosted calendar — build plan (drafted 2026-10-14)

**Goal:** Jess stops using Google and Apple calendars. Personal events live in a
self-hosted CalDAV server (Radicale) on Jupiter. Work Exchange meetings reach Jupiter
through a read-only iPhone Shortcut. Jess reads both sources and writes only to Radicale.

```
iPhone Calendar app ──CalDAV (Tailscale HTTPS)──► Radicale (Jupiter :5232) ◄──read/write── Jess
iPhone Exchange account ──Shortcut POST──► /api/calendar/sync ──► phone cache (source=exchange) ◄──read── Jess
```

Status (2026-10-15): **M1 live** (2026-10-14). **M3 server side + M4 built on PR #83, not deployed** (Radicale runs from the `gateway_nerves-calendar` worktree until it merges). Owner steps remain: M2 (move the iPhone) and the M3 work Shortcut. M5 (retire Google) pending. Current behaviour: `docs/CALENDAR.md`.

---

## What exists today (findings)

- **Phone sync is dead.** `data/app/phone_calendar.json` was last written on 2026-07-12.
  Everything Jess says about the calendar today comes from Google, or she has nothing.
- **Callers use one of two different sources.** Moving off Google means touching every one of them:

  | Caller | Reads |
  |---|---|
  | `tool_handlers.tool_check_calendar` | phone first → Google fallback |
  | `jobs_calendar.morning_briefing` | phone first → Google fallback |
  | `jobs_calendar.get_tomorrow_events` (evening briefing, wind-down) | phone first → Google fallback |
  | `ambient_manager` (10/12/2/4 summaries, LED) | phone first → Google fallback |
  | `routes_calendar` `/api/calendar/today` (dashboard) | phone + Google merged |
  | `jobs_calendar.poll_calendar` (tiered 60/30/15/5 alerts, travel time) | **Google only** — work meetings have never had alerts |
  | `routine_manager._get_calendar_buffer` / `_get_calendar_summary_text` | Google only |
  | `tool_handlers` decide_for_me context (~L1070) | Google only |
  | `api_routes` `/health` calendar block | Google only |
  | `tool_create_calendar_event` | writes to Google |
  | `jobs_calendar.process_emails_for_events` (dormant, `EMAIL_TO_CALENDAR_ENABLED=false`) | writes to Google |

- **Phone-first logic is copied in four places**, each with its own staleness check and
  "all records unparseable" guard. This plan replaces all four with one shared helper.
- **`/api/calendar/sync` replaces the whole cache on every POST.** The route docstring
  still says "one event per request, accumulate within 60s", but the code no longer does
  that. If the Shortcut posts inside a Repeat loop, only the last event survives. The
  Shortcut **must** post the whole list in one request.
- The route requires the bearer token (it isn't in `PUBLIC_PREFIXES`), so the Shortcut
  sends `Authorization: Bearer <API_TOKEN>`.
- Pattern to copy: Actual Budget runs on Jupiter, is exposed on the tailnet with
  `tailscale serve` (`:5007 → 127.0.0.1:5006`), and the orchestrator reaches it at
  `host.docker.internal`.

---

## Milestone 1 — Radicale on Jupiter (infra, no code)

1. Add `radicale/docker-compose.yml`, its own compose project like `homeassistant/`:
   - Image pinned **by digest** (candidate: `tomsquest/docker-radicale`; check it's
     maintained and pick a 3.x tag when building). `cap_drop: ALL`, `no-new-privileges`,
     `read_only: true` with the data dir as the only writable mount.
   - Bind to `127.0.0.1:5232` only. The data volume goes at `/home/labadmin/radicale/data`,
     outside the repo and gitignored.
   - `radicale/config`: `auth type = htpasswd`, `htpasswd_encryption = bcrypt`,
     `rights type = from_file`, `max_content_length` set, and the request logger left
     at the default (it must not log event bodies).
   - Two users: **owner** (the iPhone) and **jess** (the orchestrator). In the rights file,
     owner gets read/write on their own collections and jess gets read/write on the owner's
     calendars. Nobody else gets anything.
2. Tailnet HTTPS: `tailscale serve --bg --https=5233 http://127.0.0.1:5232`. Keep it
   tailnet-only, never on Funnel.
3. Backups: add the Radicale data dir to the nightly Saturn backup (extend
   `scripts/backup_state.py` or add a `radicale/backup_radicale.sh` cron next to the HA
   one) plus a `JessCalendarBackupStale` alert. (While you're in there, Actual's
   `/opt/apps/actual/data` still has no backup either.)
4. Runbook `docs/CALENDAR.md`: run/upgrade, adding a user, backup/restore.

**Done when:** `curl -u jess:… -X PROPFIND https://jupiter-amds.tail74fc4a.ts.net:5233/`
lists the owner's calendar collection over the tailnet.

## Milestone 2 — Move the iPhone over (owner steps)

1. iPhone: Settings → Calendar → Accounts → Add Account → Other → **Add CalDAV Account**,
   server `jupiter-amds.tail74fc4a.ts.net:5233`, user owner. Tailscale must be on for it
   to sync when you're away from home. Events stay on the phone between syncs.
2. Create the personal calendars in Radicale (e.g. Personal, Health, Family) from the
   iPhone or Radicale's web UI.
3. Import the history: export .ics files from Google (Takeout → Calendar) and iCloud
   (Mac Calendar → File → Export), then import each into the matching Radicale calendar.
4. Set the iPhone **Default Calendar** to the Radicale one, so new events stop going to iCloud.
5. Run both for two weeks, then turn off the iCloud and Google calendar accounts on the
   phone. Delete the cloud copies only after the Saturn backup has captured Radicale.

Watch out for: the Birthdays calendar comes from Contacts, not a calendar account, so
it doesn't move. Holiday calendars are subscriptions; re-add them as a read-only
subscription or skip them. Calendars shared with another person need a Radicale user
for them; see the open decisions below.

## Milestone 3 — Exchange-only Shortcut (owner steps + small server change)

Shortcut, "Sync work calendar to Jess":

1. **Find Calendar Events** where *Calendar is <Exchange calendar>*, Start Date is in the
   next 14 days, sorted by start date, no limit.
2. **Repeat with each**: build a Dictionary `{title, start, end, location, all_day}`
   (`calendar` is optional; the server labels it). Add it to a list variable.
3. After the loop, **one** "Get Contents of URL": POST
   `https://jupiter-amds.tail74fc4a.ts.net:8443/api/calendar/sync`, JSON body
   `{"source": "exchange", "events": <list>}`, header `Authorization: Bearer <API_TOKEN>`.
4. Personal Automations → Time of Day, **Run Immediately**, notifications off. Run it at
   07:00, 11:00, 15:00 and 19:00 on weekdays, plus "When I connect to <home Wi-Fi>".

Server changes in `routes_calendar.sync_phone_calendar`:
- Read an optional `source` (default `"exchange"`) and stamp it on every stored event.
- Fix the docstring; reject single-event bodies with a 400 instead of wrapping them, so
  a Shortcut that posts inside the Repeat loop fails loudly instead of silently keeping
  one event.
- Cap at 1000 events or 1 MB. Don't log titles (the current code already only logs the size).
- New gauge `bgw_phone_calendar_last_sync_timestamp_seconds`, seeded from the file mtime
  at startup, plus a quiet alert `ExchangeCalendarSyncStale` (no sync in 26h on a weekday).
  This would have caught the July outage.

**Done when:** `GET /api/calendar/sync` shows a fresh sync, and `check_calendar` lists a
work meeting tagged as work.

## Milestone 4 — Jess reads both sources (code)

1. **`orchestrator/calendar_types.py`**: move `CalendarEvent` and `CalendarResponse` out
   of `google_calendar.py`, and add `source: str` (`personal | exchange | google`).
   `google_calendar.py` re-imports them so nothing else breaks.
2. **`orchestrator/caldav_calendar.py`**: `CalDAVCalendarClient` with the same surface as
   the Google client (`is_configured`, `list_events`, `get_upcoming`, `create_event`):
   - Use plain `httpx` (consistent with the rest of the codebase; no sync `caldav` library
     in the event loop). Send a `REPORT` calendar-query with a time-range plus
     `<C:expand>` so Radicale expands recurring events on the server. Parse the
     VEVENTs with `icalendar`. If expand turns out to be unreliable, fall back to
     `recurring-ical-events` on the client.
   - Event id = `UID` + `RECURRENCE-ID`, which stays stable across polls, so the
     `travel:{id}` / tier-notified keys still work.
   - All-day `DATE` values become midnight local time with `all_day=True`. Timezones
     come from each event's TZID, falling back to `TIMEZONE`.
   - Never raises: errors come back as `CalendarResponse(success=False, error=safe_error(…))`
     with credentials stripped.
3. **`orchestrator/calendar_sources.py`**: the one helper every caller uses:
   - `async get_events(start, end) -> CalendarResult(events, sources_ok, sources_failed)`
     merges CalDAV (personal), the phone cache (exchange, if newer than
     `PHONE_CALENDAR_MAX_AGE_HOURS`, default 24) and Google (only while
     `CALENDAR_GOOGLE_ENABLED=true`). It dedups on (normalised title, start), sorts, and
     keeps the "records present but none parse" guard in one place.
   - `get_upcoming(hours)` and `get_tomorrow(tz)` are thin wrappers around it.
   - A failed source never empties the result. Jess says "I couldn't reach your work
     calendar" instead of "no events" (honesty rule).
4. **Point every caller in the table above at `calendar_sources`.** That deletes the four
   copies of phone-first logic. `poll_calendar` then gets tiered alerts and travel time
   for Exchange meetings too. Virtual-meeting detection already skips Teams links.
   Grep `get_calendar_client` and `_phone_calendar_events` after the change: only
   `calendar_sources.py` should match.
5. **Writes:** `tool_create_calendar_event` → `CalDAVCalendarClient.create_event`, which
   PUTs an `.ics` with a fresh UUID into `CALDAV_DEFAULT_CALENDAR`. Build it with
   `icalendar` (never by string formatting, so newlines or injected properties in titles
   can't get through). Also repoint `process_emails_for_events` (still dormant).
   Exchange is never written to.
6. **Dashboard:** `/api/calendar/today` returns `source` per event. The frontend shows a
   small Work/Personal badge.
7. **Prompt/tool text:** the `check_calendar` / `create_calendar_event` schema descriptions
   say "personal calendar (self-hosted) + work calendar (read-only)". Make sure Jess
   never offers to put something on the work calendar.

New env vars (`config.py` + `docs/ENV_VARS.md`): `CALDAV_URL` (live:
`http://radicale:5232` — already in `.env`), `CALDAV_USERNAME`, `CALDAV_PASSWORD`,
`CALDAV_CALENDARS` (comma-separated collection paths to read; empty = discover all the
user can see), `CALDAV_DEFAULT_CALENDAR` (write target), `PHONE_CALENDAR_MAX_AGE_HOURS`,
`CALENDAR_GOOGLE_ENABLED` (default true during transition). A `model_validator`
auto-disables CalDAV if URL/credentials are missing (logs, never raises — same pattern as
F-011/F-013).

Metrics: `bgw_calendar_fetch_total{source,result}`,
`bgw_calendar_fetch_latency_seconds{source}`, `bgw_calendar_events{source}` (gauge),
`bgw_calendar_write_total{result}`. Grafana row "Calendar". Quiet alert
`CalDAVUnreachable` (fetch errors for 30 min).

Tests (`orchestrator/tests/`):
- `test_caldav_client.py` with respx: REPORT parse, recurring expand, all-day, TZID,
  malformed VEVENT skipped, 401/timeout → `success=False`, create builds valid ics and
  escapes a title with CRLF.
- `test_calendar_sources.py`: merge + dedup, stale phone cache dropped, a failed source
  is reported rather than turned into "no events", Google flag off → never called.
- Update the existing tests for the briefing, ambient and check_calendar callers to the
  new helper.
- Keep `test_no_user_content_in_logs.py` green (no titles in logs).

**Done when:** with `CALENDAR_GOOGLE_ENABLED=false`, the morning briefing, `check_calendar`,
the 60/30/15/5 alerts and the dashboard all show personal and work events, and
"put dentist on Thursday at 3" appears on the iPhone.

## Milestone 5 — Retire Google Calendar

1. After about two weeks of side-by-side running, set `CALENDAR_GOOGLE_ENABLED=false` in `.env`.
2. Remove the Google calendar code path in a later PR (keep `google_auth.py`; Gmail
   `check_email` still uses it — Gmail is out of scope here).
3. Narrow the OAuth scope to Gmail-only, re-consent, and check the weekly
   `refresh_google_token.py` cron still works.
4. Update CLAUDE.md (Tools table, Calendar source priority note, Services table:
   Radicale), `docs/GOOGLE_INTEGRATIONS.md`, and `docs/JESS_QUICK_START.md`.

---

## Open decisions (owner)

1. **Work meeting detail:** send full titles/locations, or only "Busy" + times? Full
   titles make the alerts useful ("Leave for <meeting>"). Check what your employer
   allows before sending them off the phone.
2. **Shared calendars:** does anyone else need to see or edit your personal calendar?
   If so, they get a Radicale user and need Tailscale too, or you accept that one
   calendar stays on iCloud.
3. **Shortcut cadence:** four times a day on weekdays is the suggestion. More often
   means fresher alerts but more Shortcut banners and battery use.
4. **Transition length:** two weeks of Google + Radicale side by side before switching
   Google off.

## Build order

M1 and M3 (server bit) are independent and small, so do them first. M2 is owner-only
and can run alongside M4. M4 is the bulk (one PR, full review pipeline). M5 is a
cleanup PR.
