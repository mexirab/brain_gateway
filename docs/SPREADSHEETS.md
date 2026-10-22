# Spreadsheets (Grist)

Self-hosted [Grist](https://www.getgrist.com) on Jupiter: the local replacement for Google Sheets. The owner edits sheets in the browser / on the phone; Jess reads and edits allow-listed documents through the `spreadsheet` tool. Nothing leaves the box.

`grist/docker-compose.yml`'s header is authoritative on exposure and auth; this file is the runbook.

## What it is

| | |
|---|---|
| Compose project | `grist/` (own project, like `actual/` and `radicale/`; never touched by the orchestrator's build/deploy) |
| Image | `gristlabs/grist:1.7.20`, digest-pinned |
| Hardening | uid 1000, read-only rootfs, `cap_drop: ALL`, `no-new-privileges`, `/tmp` tmpfs 256m, `mem_limit 1g`, `pids_limit 512`; formulas run in gVisor (`GRIST_SANDBOX_FLAVOR=gvisor`) |
| Publish | `127.0.0.1:8484` only |
| UI | https://jupiter-amds.tail74fc4a.ts.net:8485 (Tailscale Serve, tailnet-only) |
| Networks | `gateway_nerves_grist-net` (created by the main compose; orchestrator only) and `monitoring_grist-probe-net` (created by the monitoring compose; blackbox only). NOT on `gateway-net` / `monitoring-net` |
| Secrets | `/opt/apps/grist/grist.env` (chmod 600; template `grist/grist.env.example`) |
| Data | `/opt/apps/grist/persist` (1000:1000, mode 700): `home.sqlite3` (users, orgs, API keys) + `docs/*.grist` (one SQLite per doc) |
| Backup | nightly `grist/backup_grist.sh` → Saturn (see `docs/BACKUP.md` → Grist) |
| Monitoring | blackbox probe `grist:8484/status` (`service=grist`) → `GristDown` (quiet); `JessGristBackupStale` (paging); `JessBackupOffboxFailing` (shared). See `monitoring/README.md` |
| Jess side | `orchestrator/grist_client.py` + `spreadsheet` tool, gated by `GRIST_ENABLED` (env: `docs/ENV_VARS.md` → Grist spreadsheet; schema: `TECHNICAL_REFERENCE.md` → spreadsheet) |

## Security model

- **Header trust.** Browser login is forwarded-header: Tailscale Serve sets `Tailscale-User-Login` on every proxied request from a tailnet user and strips client-sent copies, so Grist trusts it (`GRIST_FORWARD_AUTH_HEADER`, `GRIST_IGNORE_SESSION`). Only `GRIST_DEFAULT_EMAIL` (the owner's Tailscale login) owns the docs; other tailnet users see an empty site. `GRIST_FORCE_LOGIN` makes anonymous requests 401 / list nothing. `GRIST_IN_SERVICE` skips the `/boot` wizard (the compose sets everything it would).
- **Caveat: the header needs no secret.** Anything that reaches Grist without going through Tailscale Serve can claim to be the owner:
  - members of `grist-net` / `grist-probe-net` — only the orchestrator and blackbox-exporter. That is why Grist has its own networks and is not on `gateway-net` (a compromised frontend/searxng could send the header) or `monitoring-net`. Keep it that way.
  - any process on Jupiter via `127.0.0.1:8484` (host-networked Home Assistant, Pi-hole, local users). **Accepted:** those already control the house / the box.
- **Least-privilege Jess user.** The orchestrator authenticates with a Bearer API key belonging to a separate Grist user `jess@grist.invalid`, shared as **Editor** on the allow-listed docs only — never Owner, never the admin. `GRIST_DOCS` is a second, orchestrator-side allow-list: the model names a nickname, never a raw doc id.
- **Untrusted content.** Read output is framed between `BEGIN/END SPREADSHEET DATA <random tag>` markers and boundary-escaped; IMPORTANT RULES list `spreadsheet` both as an untrusted source and as a state-changing tool, so writes happen only when the user asked.

## Deploy (in order)

1. **Host dirs + secrets** (Jupiter):
   ```bash
   sudo install -d -o 1000 -g 1000 -m 700 /opt/apps/grist /opt/apps/grist/persist
   cp ~/gateway_nerves/grist/grist.env.example /opt/apps/grist/grist.env
   chmod 600 /opt/apps/grist/grist.env
   tailscale status --json | jq -r '.User[].LoginName'   # → GRIST_DEFAULT_EMAIL / GRIST_ADMIN_EMAIL
   openssl rand -hex 32                                   # → GRIST_SESSION_SECRET
   ```
2. **Merge the PR.** CI recreates the orchestrator (creates `gateway_nerves_grist-net`) and reconciles the monitoring stack (creates `monitoring_grist-probe-net`, adds the probe). `GristDown` is quiet with `for: 5m`, so start Grist right after.
3. **Start Grist:**
   ```bash
   cd ~/gateway_nerves/grist && docker compose up -d
   docker logs grist | grep "gvisor check ok"
   docker ps --filter name=grist        # (healthy)
   curl -s 'http://127.0.0.1:9115/probe?module=http_2xx&target=http://grist:8484/status' | grep ^probe_success   # 1
   ```
4. **Tailscale Serve:** `sudo tailscale serve --bg --https=8485 http://127.0.0.1:8484`
5. **Header-strip test** from another tailnet device (not the owner's):
   ```bash
   curl -H 'Tailscale-User-Login: <owner tailscale login>' https://jupiter-amds.tail74fc4a.ts.net:8485/api/orgs
   ```
   Must NOT list the owner's org. If it does, Serve is not stripping the header — stop and take Grist down.
6. **Create the docs** in the UI (as the owner).
7. **Least-privilege Jess user** (from Jupiter; the first request creates the user):
   ```bash
   curl -s -H 'Tailscale-User-Login: jess@grist.invalid' http://127.0.0.1:8484/api/profile/user
   curl -s -X POST -H 'Tailscale-User-Login: jess@grist.invalid' -H 'X-Requested-With: XMLHttpRequest' \
     -H 'Origin: http://127.0.0.1:8484' http://127.0.0.1:8484/api/profile/apiKey    # → GRIST_API_KEY
   ```
   Then in the UI share each allow-listed doc with `jess@grist.invalid` as **Editor** (never Owner, never the admin).
8. **Backup:**
   ```bash
   mkdir -m 700 /home/labadmin/grist
   ssh labadmin@saturn 'mkdir -p -m 700 /home/labadmin/grist-backups'
   /home/labadmin/gateway_nerves/grist/backup_grist.sh    # seeds the metric — the stale alert can't fire before the first run
   crontab -e
   # 5 4 * * * /home/labadmin/gateway_nerves/grist/backup_grist.sh >> /home/labadmin/grist/backup.log 2>&1
   ```
9. **Enable Jess** — in `.env`:
   ```
   GRIST_ENABLED=true
   GRIST_URL=http://grist:8484
   GRIST_API_KEY=<jess user key>
   GRIST_DOCS=gym=<docId>
   ```
   `docker compose up -d --force-recreate orchestrator` (not `restart`). Check `docker logs brain-orchestrator | grep '\[CONFIG\]'` shows no Grist disable line. Smoke test in chat: "what spreadsheets do you have?"
10. **Homepage tile:** add Grist to `/opt/homepage/config/services.yaml`.

## Rollback

- Jess side only: `GRIST_ENABLED=false` in `.env` → `docker compose up -d --force-recreate orchestrator` (tool leaves the schema, prompt line disappears).
- Server: `cd ~/gateway_nerves/grist && docker compose down`; `sudo tailscale serve --https=8485 off`; remove the cron line. `GristDown` will fire (quiet) until the probe target is removed from `monitoring/prometheus/prometheus.yml.template` and configs re-rendered.
- Data stays in `/opt/apps/grist/persist` + backups; restore steps in `docs/BACKUP.md` → Grist.

## Known limits

- No delete action (add / update only); deleting rows is done in the UI.
- Shared docs: anyone a doc is shared with can write text Jess reads (framed as untrusted, but still prompt-injection surface).
- `pids_limit: 512` with gVisor — raise it if many docs are open at once.
- `/tmp` tmpfs is 256m — raise for big imports.
- Grist logs are not shipped to Loki (`docker logs grist`).
- No Grafana panel for `bgw_spreadsheet_total{action,result}` yet.
- Sheet contents reach chat history / the training corpus like email does — keep medical/financial sheets off `GRIST_DOCS`. Budget stays in Actual.
