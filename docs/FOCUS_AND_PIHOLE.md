# Focus Timer & Pi-hole DNS

## Focus Timer (Pomodoro)

ADHD-friendly focus timer with ambient audio and site blocking:

| Feature | Status | Notes |
|---------|--------|-------|
| Timer + voice break | Done | `start_focus`, `stop_focus`, `focus_status` tools |
| Endel audio | Done | Streams HLS from Endel Pacific API to Office speaker |
| Pi-hole blocking | Done | 24 focus domains + 72 always-blocked adult domains |
| Body doubling check-ins | Done | Periodic TTS check-ins during focus sessions |
| Sprints | Done | Multi-sprint sessions via `focus_sprint` tool (next/extend/end) |
| Ambient audio options | Done | endel, lofi, coffee_shop, silence |
| Session summary | Done | End-of-session TTS with total minutes and sprint count |

**Usage:**
- `"start focus on coding for 30 minutes"` - starts timer + audio + blocking
- `"start focus without blocking"` - no site blocking
- `"start focus with lofi music, 4 sprints, check in every 10 minutes"` - body doubling session
- `"stop focus"` or timer expires -> unblocks sites, announces break
- `"next sprint"` / `"extend"` / `"end session"` - sprint control via `focus_sprint` tool

**start_focus parameters:** `duration_minutes`, `task`, `blocking`, `check_ins`, `check_in_interval`, `audio` (endel/lofi/coffee_shop/silence), `sprints`

**focus_sprint actions:** `next_sprint`, `extend`, `end_session`

**Audio env vars:**
- `FOCUS_AUDIO_LOFI_URL` — lo-fi stream URL for HA media_player
- `FOCUS_AUDIO_COFFEE_URL` — coffee shop stream URL for HA media_player

**Key files:** `orchestrator/focus_manager.py`, `orchestrator/pihole_client.py`

## Pi-hole DNS (whole-house)

Redundant Pi-hole v6 pair synced via Nebula Sync. Jupiter is primary, Saturn is secondary.

| Item | Jupiter (primary) | Saturn (secondary) |
|------|-------------------|-------------------|
| Admin UI | http://10.0.0.248:8053/admin | http://10.0.0.58:8053/admin |
| DNS | 10.0.0.248:53 | 10.0.0.58:53 |
| Upstream | 8.8.8.8, 8.8.4.4 | 8.8.8.8, 8.8.4.4 |
| Docker project | (runs on Jupiter directly, not in this repo's compose) | `pihole` |
| Compose file | (not in `gateway_mvp/docker-compose.yml` — the local Helios pihole was removed 2026-04-26) | `saturn/docker-compose.pihole.yml` |

**DHCP:** Disabled on both Pi-holes. DHCP served by Orbi router with static reservations for all cluster nodes. Pi-holes handle DNS only.

**Nebula Sync:** Runs as a Docker container on Jupiter (`nebula-sync` service). Uses Pi-hole v6 Teleporter API to sync config from Jupiter -> Saturn every 15 min. No SSH needed.

**Blocking groups:**
- **Default (group 0):** 72 adult domains — always blocked for all clients
- **focus_blocklist (group 1):** 19 distraction domains (reddit, twitter, youtube, etc.) — toggled by `start_focus`/`stop_focus`

**Focus blocking:** Orchestrator applies focus blocking to both instances concurrently via `PIHOLE_URLS`. If one is down, the other still blocks.

**Commands:**
```bash
# Saturn Pi-hole
./saturn/deploy-pihole.sh           # deploy and start
./saturn/deploy-pihole.sh logs      # tail logs
./saturn/deploy-pihole.sh stop      # stop
```
