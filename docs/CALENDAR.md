# Calendar (self-hosted Radicale)

Personal calendars live in **Radicale** (CalDAV) on Jupiter, replacing Google and
iCloud calendars. Work Exchange meetings come in separately via the iPhone Shortcut →
`/api/calendar/sync` (see `docs/GOOGLE_INTEGRATIONS.md` → Phone Calendar Sync).
Build plan and later milestones (Jess reading/writing CalDAV, retiring Google):
`docs/internal/CALENDAR_SELF_HOST_PLAN.md`.

Status (2026-10-14): Milestone 1 done. The server is running with tailnet HTTPS,
nightly backups to Saturn, and alerts routed to Pushover. Jess does **not** read
Radicale yet (milestone 4).

## Layout

| What | Where |
|------|-------|
| Compose project | `radicale/docker-compose.yml` (own project, like `homeassistant/`) |
| Server config + access rules | `radicale/config/config`, `radicale/config/rights` (committed, mounted `:ro`) |
| Calendars (runtime state) | `/home/labadmin/radicale/data/collections/` — outside the repo |
| Logins (bcrypt htpasswd) | `/home/labadmin/radicale/secrets/users` — chmod 600 in a 700 dir, outside the repo; directory-mounted at `/secrets` |
| Owner's initial password | `/home/labadmin/radicale/owner-password.txt` — chmod 600; delete once it's in your password manager |
| Jess's login | `CALDAV_URL=http://radicale:5232`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` in `.env` (not yet read by any code) |
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
