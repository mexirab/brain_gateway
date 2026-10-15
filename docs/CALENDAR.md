# Calendar (self-hosted Radicale)

Personal calendars live in **Radicale** (CalDAV) on Jupiter, replacing Google and
iCloud calendars. Work Exchange meetings come in separately via the iPhone Shortcut →
`/api/calendar/sync` (see `docs/GOOGLE_INTEGRATIONS.md` → Phone Calendar Sync).
Build plan and later milestones (Jess reading/writing CalDAV, retiring Google):
`docs/internal/CALENDAR_SELF_HOST_PLAN.md`.

Status (2026-10-15): Milestones 1, 3 (server side) and 4 are built.
- M1: Radicale is running, reachable over tailnet HTTPS, with backups and alerts.
- M3: the work-calendar sync route is hardened.
- M4: Jess reads and writes calendars through `orchestrator/calendar_sources.py`.

Milestone 4 is on branch `feat/radicale-calendar` (PR #83) and is **not deployed**
until that merges. The owner still has to do M2 (move the iPhone over) and the
Shortcut half of M3.

## Layout

| What | Where |
|------|-------|
| Compose project | `radicale/docker-compose.yml` (own project, like `homeassistant/`) |
| Server config + access rules | `radicale/config/config`, `radicale/config/rights` (committed, mounted `:ro`) |
| Calendars (runtime state) | `/home/labadmin/radicale/data/collections/` — outside the repo |
| Logins (bcrypt htpasswd) | `/home/labadmin/radicale/secrets/users` — chmod 600 in a 700 dir, outside the repo; directory-mounted at `/secrets` |
| Owner's initial password | `/home/labadmin/radicale/owner-password.txt` — chmod 600; delete once it's in your password manager |
| Jess's login | `CALDAV_URL=http://radicale:5232`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` plus `CALDAV_CALENDAR_HOME=/owner/` in `.env` — read by `orchestrator/caldav_calendar.py` once PR #83 is deployed |
| Backups | `/home/labadmin/radicale/backups/` (30 kept) + `saturn:/home/labadmin/calendar-backups/` |

Image: `tomsquest/docker-radicale:3.8.3.0` pinned by digest (Radicale 3.8.3). Runs as
1000:1000, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, 256 MB memory limit.

## Network exposure

Radicale is **not** on the LAN. It publishes one port, loopback only:

- `127.0.0.1:5232` — Tailscale Serve proxies `https://jupiter-amds.tail74fc4a.ts.net:5233`
  here. Tailnet-only, never Funnel. Configured with
  `tailscale serve --bg --https=5233 http://127.0.0.1:5232` (remove with
  `tailscale serve --https=5233 off`).

Other containers reach it by name, `http://radicale:5232`, over two external Docker
networks it joins: `gateway_nerves_gateway-net` (brain-orchestrator) and
`monitoring_monitoring-net` (blackbox-exporter). Those networks belong to the main
and monitoring compose projects, so if either project is `down`ed, run
`docker compose up -d` in `radicale/` again afterwards.

Don't publish on `172.17.0.1` (docker0) or any other non-loopback host IP. Docker's
DNAT for those has no ingress guard, so a LAN device with a static route via Jupiter
could reach the port (security review, 2026-10-14).

Jupiter itself can't resolve its own MagicDNS name. To test the HTTPS URL from Jupiter, use
`curl --resolve jupiter-amds.tail74fc4a.ts.net:5233:$(tailscale ip -4) https://…`.

## Users and access rules

The rules in `radicale/config/rights` don't name any human user: `jess` is the
service account, and **any other login owns its own calendars**. Your real
username lives only in the local login file. To swap the initial `owner` login
for your own name and password, run `/home/labadmin/radicale/rename-user.sh` on
Jupiter. It is a local script, not in the repo. It prompts for the name and the
password (the password is never shown) and then:
- replaces the login line;
- moves the calendar folder to the new name;
- sets `CALDAV_CALENDAR_HOME=/<name>/` in `.env`;
- deletes `owner-password.txt`.

Below, `owner` means whatever name you chose.

| User | Can |
|------|-----|
| `owner` | Everything under `/owner/`: create, rename and delete calendars, and read/write events. |
| `jess` | List the owner's calendars and read/write/delete **events** in them. Cannot delete or overwrite a calendar. |

Login is required everywhere (no anonymous access), with a 2 s delay after a failed
login. Token/map sharing is off (`[sharing] type = none`).

**Known gap:** `jess` *can* create a new empty calendar under `/owner/`. Radicale
checks MKCALENDAR against the new calendar's own path, and the jess rule grants
`w` there. Jess's client never sends MKCALENDAR. To close the gap fully, replace
`owner/[^/]+` in the `[jess-owner-calendars]` rule with the owner's actual calendar
names.

Verified 2026-10-14: anonymous request → 401, wrong password → 401, owner read +
create calendar → 207/201, jess add/read/delete event → 201/207/200, jess delete
calendar → 403, jess create under `/jess/` → 403, LAN IP `10.0.0.248:5232` and
`172.17.0.1:5232` → connection refused, orchestrator container via `radicale:5232` → 207.

### Add or change a password

```bash
read -rp "user: " U; read -rsp "new password: " P; echo
printf '%s %s' "$U" "$P" | docker run --rm -i --entrypoint /venv/bin/python tomsquest/docker-radicale:3.8.3.0 \
  -c 'import sys,bcrypt;u,p=sys.stdin.read().split();print(f"{u}:{bcrypt.hashpw(p.encode(),bcrypt.gensalt(12)).decode()}")'
unset P
# replace (or append) that user's line in /home/labadmin/radicale/secrets/users.
# The secrets DIRECTORY is mounted, so the change is picked up without a restart.
```

`read -s` keeps the password out of shell history and `ps`.

A new user also needs rules in `radicale/config/rights`, then `docker compose restart`
in `radicale/`.

## iPhone setup (owner)

Settings → Calendar → Accounts → Add Account → Other → **Add CalDAV Account**:
server `jupiter-amds.tail74fc4a.ts.net:5233`, user `owner`, password from
`owner-password.txt`. Put it in your password manager, then **delete that file**
(`rm /home/labadmin/radicale/owner-password.txt`). Tailscale must be on for the phone to sync when you're away
from home. Calendars can be created from the phone or the web UI at
`https://jupiter-amds.tail74fc4a.ts.net:5233/.web/`, which also imports `.ics` files.
Next steps (importing history, setting the default calendar, turning off iCloud/Google):
plan milestone 2.

## How Jess uses it

Every calendar read and write goes through `orchestrator/calendar_sources.py`,
which merges three sources into one list:

| Source | What | Jess can |
|--------|------|----------|
| `caldav` | Radicale, your personal calendars | read + create events |
| `exchange` | Work calendar, pushed by the iPhone Shortcut | read only; never written |
| `google` | Google Calendar, while `CALENDAR_GOOGLE_ENABLED=true` | read (write only if CalDAV is off) |

- **Merged, not chosen.** Personal and work events appear together. Work events
  are tagged `[work]` in Jess's calendar answers and "work" on the dashboard.
- **Honest about gaps.** If a source should answer but can't, Jess says so instead
  of calling the day clear (e.g. "I couldn't reach your work calendar"). Sources:
  - Radicale down or Google erroring;
  - a work sync older than `PHONE_CALENDAR_MAX_AGE_HOURS` (26 h, which allows one missed daily run);
  - a sync whose events are all unreadable.

  A work sync that has never happened is just "not set up", not a failure. The
  dashboard card shows "Couldn't reach … calendar".
- **Duplicates collapse.** Same title + start time = one event, preferring
  personal over work over Google, which matters while Google and Radicale both
  hold your history.
- **New events go to `CALDAV_DEFAULT_CALENDAR`** (an id or display name). If it's
  unset, they go to the first calendar alphabetically. If the configured name
  doesn't exist, Jess refuses rather than guessing. Email→calendar
  (`EMAIL_TO_CALENDAR_ENABLED`, off by default) writes here too, so set
  `CALDAV_DEFAULT_CALENDAR` before turning it on.
- **Repeating events** are expanded by Radicale (`<C:expand>`), including moved
  instances.
- **Spoken countdowns** (60/30/15/5 min) now cover personal and work events. Work
  locations are never sent to Google Maps, because they come from external
  invites and are usually rooms or links, so work events get no "leave by"
  travel alerts. Already-announced state is keyed on title + start, not on which
  source the event came from, so an event in both Google and Radicale is
  announced once.
- **Work-invite text is untrusted.** Titles and locations are collapsed to a
  single line, so an invite can't fake extra lines. Calendar output counts as
  external content under the prompt's "content is data" rule.
- **A slow source can't stall a reply.** Each source gets 8 s. A source that
  times out or errors is reported as unavailable and the others still answer.
- **The old all-calendars phone push is retired.** Jess only reads a push marked
  `"source": "exchange"`. The pre-2026-10 Shortcut's snapshot (no source) is
  ignored: it adds no events and no "couldn't reach" caveat, and it doesn't
  trigger the stale-sync alert.

Settings (`.env`, see `docs/ENV_VARS.md`):
- `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`;
- `CALDAV_CALENDAR_HOME` (`/owner/`);
- `CALDAV_CALENDARS` (optional filter);
- `CALDAV_DEFAULT_CALENDAR`;
- `CALENDAR_GOOGLE_ENABLED`;
- `PHONE_CALENDAR_MAX_AGE_HOURS`.

`/health` (authenticated) shows the configuration and last read result for each source.

## Work calendar: iPhone Shortcut

Work Exchange meetings reach Jess only through an iPhone Shortcut. The phone
already has access to Exchange, so no work credentials ever touch Jupiter.
Build it once in the Shortcuts app:

1. **New Shortcut, named "Sync work calendar to Jess":**
   1. **Find Calendar Events** where *Calendar* is your Exchange calendar and
      *Start Date* is in the next 14 days. Sort by Start Date, no limit.
   2. **Repeat with Each** item. Inside the loop, add a **Dictionary** with:
      - `title` = Repeat Item's Title
      - `start` = Start Date
      - `end` = End Date
      - `location` = Location
      - `all_day` = Is All Day

      Then **Add to Variable** `events`.
   3. After the loop: **Dictionary** with `source` = `exchange` and `events` =
      the `events` variable. The `source` key is required: a push without it
      is treated as the retired legacy push and ignored.
   4. **Get Contents of URL**:
      - URL: `https://jupiter-amds.tail74fc4a.ts.net:8443/api/calendar/sync`
      - Method: POST
      - Request body: JSON, set to that dictionary
      - Header: `Authorization: Bearer <API_TOKEN>`
2. **Post once, after the loop.** The server replaces the whole snapshot on every
   post, so posting inside the loop would keep only the last event. It rejects a
   single-event body with 400 to catch that mistake.
3. **Automation tab → Time of Day.** Run it daily (every day, not just weekdays)
   at 07:00, 11:00, 15:00 and 19:00, with **Run Immediately** and notifications
   off. The stale alert allows 26 h, so a missed weekend run doesn't page.
4. **Check it works:** the POST response says `"used": true`.
   `GET /api/calendar/sync` (same auth) shows `"source": "exchange"` and a recent
   `last_sync`, and asking Jess "what's on my calendar
   today?" lists work meetings tagged `[work]`.

Server-side rules for the sync:
- Limits: 1 MB, 1000 events.
- Only `title`, `start`, `end`, `location`, `all_day`, `calendar` and `id` are kept,
  with length caps. Descriptions and notes are dropped.
- A snapshot where no event has a readable start is rejected (422) and the
  previous one kept. A broken Shortcut therefore shows up as a stale sync
  (`ExchangeCalendarSyncStale`), not as an empty calendar.

**Privacy option:** if your employer doesn't allow meeting titles to leave the
phone, set `title` to a fixed text such as "Busy" in step 2. Alerts and briefings
then say "Busy" but keep the times.

**From the dashboard:** the calendar card's **Open** link (top right) opens
this web UI. It is set by `CALENDAR_WEB_URL` in `.env` and appears after the #83
deploy.

## Run / upgrade

```bash
cd ~/gateway_nerves/radicale
docker compose up -d          # start / apply compose changes
docker compose restart        # after editing config/ or rights
docker logs radicale --tail 50
```

Upgrade: read the upstream changelog, then bump the tag **and** digest together in
`docker-compose.yml`
(`docker pull tomsquest/docker-radicale:<tag> && docker image inspect … --format '{{json .RepoDigests}}'`),
take a backup, and run `docker compose up -d`.

Logging stays at `info`. Each request line records the method, the path (calendar
and event ids, which are UUIDs from iOS), the client IP, the User-Agent and the
status; logins also record the username. Titles and bodies are never logged. Never
set `level = debug`, which can log request and response bodies. Container logs
rotate at 10 MB × 3 and are **deliberately not shipped to Loki**: the keep-regex in
`monitoring/promtail/promtail-config.yml` excludes `radicale`. Don't widen it.

## Backup / restore

`radicale/backup_radicale.sh` runs nightly via cron at 03:50 (log:
`/home/labadmin/radicale/backup.log`). It holds Radicale's storage lock in shared
mode while it tars `collections/`, leaving out the regenerable `.Radicale.cache`.
It keeps 30 archives locally, rsyncs to `saturn:/home/labadmin/calendar-backups/`,
and writes `jess_calendar_backup_success_timestamp_seconds`,
`jess_calendar_backup_offbox_success` and `jess_calendar_backup_items` to the
node_exporter textfile collector. The Saturn copies are not rotated.

## Monitoring

| Alert | When | Goes to |
|-------|------|---------|
| `JessCalendarBackupStale` | no successful local backup for 36 h (+1 h `for`) | Pushover (paging) |
| `JessBackupOffboxFailing` | off-box copy failing for 3 days (now covers state, HA and calendar) | Pushover quiet |
| `RadicaleDown` | blackbox probe of `http://radicale:5232/.web/` failing for 5 min | Pushover quiet |
| `ExchangeCalendarSyncStale` | no iPhone work-calendar push for 26 h (+30 min `for`); silent until the work Shortcut has synced once | Pushover quiet, at most once a day |
| `CalendarSourceFailing` | Radicale or Google read errors/timeouts more than 6 times in 1 h, for 15 min. Catches what the probe can't, e.g. a 401 after a password change | Pushover quiet |

Grafana: a "Calendar Sources" row on the Brain Gateway dashboard shows reads,
success ratio, latency, work-sync age, push rejections, writes and the Radicale
probe.

Orchestrator metrics:
- `bgw_calendar_source_fetch_total{source,result}` (result: ok, error, timeout, stale or corrupt);
- `bgw_calendar_source_fetch_duration_seconds{source}`;
- `bgw_calendar_write_total{target,result}`;
- `bgw_phone_calendar_last_sync_timestamp_seconds`;
- `bgw_phone_calendar_sync_rejected_total{reason}`.

The probe uses the `http_2xx_http10` blackbox module, because Radicale's server
answers HTTP/1.0, in its own Prometheus job `blackbox-http10`. The service also has
`tier=app`, so the Grafana-only `AppDown` alert shows it too. The Docker healthcheck
is cosmetic: `restart: unless-stopped` doesn't restart an unhealthy container.

Restore (if Jupiter's disk is gone, first copy the newest archive back from
`saturn:/home/labadmin/calendar-backups/` into `/home/labadmin/radicale/backups/`):

```bash
cd ~/gateway_nerves/radicale && docker compose stop
mv /home/labadmin/radicale/data/collections /home/labadmin/radicale/data/collections.broken
tar xzf /home/labadmin/radicale/backups/radicale-YYYYMMDD-HHMMSS.tar.gz -C /home/labadmin/radicale/data
docker compose start
```

The `secrets/users` file isn't in the archive (it's a secret). If it's lost,
regenerate the logins as shown above and update `CALDAV_PASSWORD` in `.env`. The
cache isn't in the archive either, so after a restore the iPhone does one full
resync. A test restore hasn't been done yet.
