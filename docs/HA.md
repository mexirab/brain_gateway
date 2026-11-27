# Home Assistant runbook

HA runs on **Jupiter** as a standalone Docker Compose project at
[`homeassistant/`](../homeassistant/), not in the main `docker-compose.yml`: it
is a pulled image with its own lifecycle (like the monitoring stack), so the
orchestrator's build and CI deploy never touch it. The orchestrator talks to it
over the LAN (`HA_URL=http://10.0.0.248:8123`); `HA_TOKEN` is a long-lived token
stored in HA's `.storage/auth`.

## Why it's on Jupiter (migration history)

HA originally ran on a Raspberry Pi (`10.0.0.106`). The Pi kept failing and on
2026-07-04 booted to a systemd emergency console — almost certainly SD-card
wear, the classic HA-on-a-Pi death (constant recorder writes). It was migrated
to Jupiter, co-locating HA with the orchestrator and removing two failure
sources at once: the flaky Pi *and* the orchestrator→network→Pi hop.

The setup is all-network — ESPHome (WiFi) devices, Bluetooth via **remote
proxies** (not a local dongle), Google/Nest Cast, and cloud — so nothing
physical had to move: the migration was a config copy plus a container.
`HA_URL` changed from `http://10.0.0.106:8123` to `http://10.0.0.248:8123`.

The Pi is dead but untouched (safe to reimage). A pristine rescue copy of its
config is kept at `~/ha-rescue` on Jupiter and on Saturn.

## Run / upgrade

```bash
cd ~/gateway_nerves/homeassistant
docker compose up -d
```

To upgrade: bump the pinned tag in `docker-compose.yml` (don't float it — a
pinned version keeps an unrelated change from also becoming an HA upgrade), then
`docker compose pull && docker compose up -d`. Check `docker logs homeassistant`
after. Pinned at `2026.8.3` since 2026-09-01 (was `2026.5.1`, carried over from
the Pi): the Starling Home Hub HACS integration needs 2026.8.2 or newer.

`network_mode: host` is required for Cast/mDNS speaker discovery. The `/config`
volume lives at `/home/labadmin/homeassistant/config` (external runtime state,
never committed).

## Backup

`homeassistant/backup_ha.sh`, nightly cron on Jupiter at 03:45:

- Consistent recorder-DB snapshot via SQLite's online-backup API (a plain copy
  of a live WAL DB can tear).
- Durable config only: `.storage/` (auth, entity/device registry,
  integrations), all `*.yaml`, `secrets.yaml`, `custom_components/`,
  `blueprints/`, `esphome/`. Excludes regenerable `tts/`, `.cache/`, `deps/`.
- Rotate 14 locally; rsync to `saturn:/home/labadmin/ha-backups/`.
- Prometheus metric `jess_ha_backup_success_timestamp_seconds` → the
  `JessHABackupStale` alert (>36h) in `monitoring/prometheus/alert-rules.yml`.

Config env: `JESS_HA_BACKUP_REMOTE` (rsync target), `JESS_HA_BACKUP_METRICS_PATH`
(textfile-collector path). Both default to the Jupiter/Saturn setup.

## Restore

Archives are plain `tar.gz` containing a top-level `config/`:

```bash
cd ~/gateway_nerves/homeassistant
docker compose down
# inspect
tar tzf /home/labadmin/homeassistant/backups/ha-config-<STAMP>.tar.gz | head
# restore over the config dir (overwrites — be sure)
tar xzf <archive> -C /tmp/ha-restore && rsync -a /tmp/ha-restore/config/ /home/labadmin/homeassistant/config/
docker compose up -d
```

`custom_components/` are included, so a restore doesn't depend on HACS
re-downloading. Quarterly, extract the latest archive to a scratch dir and
confirm `home-assistant_v2.db` opens (`pragma integrity_check`).

## Failover to Saturn (Jupiter dies)

HA is a stateful single-instance app — never run two live copies against the
same devices (they fight over state and double-fire automations). "Standby" =
backup + a stopped container, not active-active.

If Jupiter dies, both the orchestrator and HA move to Saturn:
1. On Saturn, restore the latest `ha-backups/` archive into a `config/` dir and
   `docker compose up -d` an HA service (copy `homeassistant/`).
2. Bring the orchestrator up on Saturn (from its own nightly Saturn backup).
3. Point the orchestrator's `HA_URL` at Saturn's LAN IP.

RTO is a few minutes, manual. For an HA-only crash (Jupiter fine),
`restart: unless-stopped` + the healthcheck recover it automatically.

## Gotchas

- **Cloud integrations** (Google/Nest) may prompt for re-auth after a
  host/hardware change — handle in the HA UI (Settings → Devices & Services).
- **ESPHome dashboard** (the firmware-flash UI) was a Pi add-on and did not
  migrate; ESPHome *devices* still work via the network integration. Run the
  dashboard as its own container only if you need to reflash firmware.
- The orchestrator reaches host-networked HA via Jupiter's LAN IP
  (`10.0.0.248:8123`), not `localhost` (they're in different network namespaces).
- Rollback target if HA_URL ever needs reverting: the old Pi was
  `http://10.0.0.106:8123`.
