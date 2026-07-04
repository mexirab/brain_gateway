# homeassistant/

Home Assistant runs on **Jupiter** (the always-on node) as its own Docker
Compose project — migrated off the failed Raspberry Pi (`10.0.0.106`) on
2026-07-04. It is intentionally *not* part of the main `docker-compose.yml`: it's
a pulled image with its own lifecycle (like the monitoring stack), so the
orchestrator's build/deploy never touches it.

| File | What |
|------|------|
| `docker-compose.yml` | HA Container service. `network_mode: host` (Cast/mDNS), pinned to `2026.5.1`. |
| `backup_ha.sh` | Nightly consistent backup → Saturn + Prometheus freshness metric. |

Full runbook — migration history, run/upgrade, backup/restore, failover — is in
[`docs/HA.md`](../docs/HA.md).

## Run

```bash
cd ~/gateway_nerves/homeassistant
docker compose up -d          # start / apply changes
docker compose pull && docker compose up -d   # upgrade (bump the pinned tag first)
docker logs -f homeassistant  # watch boot
```

The `/config` volume (~125 MB: `.storage/`, recorder DB, `custom_components/`)
is external runtime state at `/home/labadmin/homeassistant/config` — **never
committed** (see `.gitignore`).

## Backup

`backup_ha.sh` runs nightly via cron on Jupiter (03:45):

```
45 3 * * * /home/labadmin/gateway_nerves/homeassistant/backup_ha.sh >> /home/labadmin/homeassistant/backup.log 2>&1
```

It snapshots the recorder DB consistently (SQLite online-backup), archives the
durable config (excludes regenerable `tts/`, `.cache/`, `deps/`), rotates 14
locally, and rsyncs to `saturn:/home/labadmin/ha-backups/`. The
`JessHABackupStale` alert (>36h) fires if a nightly is missed.
