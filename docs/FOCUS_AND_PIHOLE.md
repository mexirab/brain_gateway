# Focus Timer & Pi-hole DNS

> **Status (2026-09-29): focus-mode site blocking is DEPRECATED for this deployment.** `FOCUS_BLOCKING_ENABLED=false` in `.env`.
> - LAN DNS moved back to the router (Orbi) after recurring issues even with the 2026-08-18 Pi-hole redundancy setup — LAN clients no longer resolve through Pi-hole.
> - The device that needed blocking (a managed work laptop) uses corporate DNS over its own path, so no home DNS filter can reach it.
> - Saturn's Pi-hole is down with Saturn; Jupiter's `pihole` container still runs but has no clients; `nebula-sync` stopped since July 2026.
>
> Everything else in focus mode (timers, sprints/body doubling, check-ins, ambient audio, breaks) is unchanged and working. The blocking code stays in the product for other installs (compose default `FOCUS_BLOCKING_ENABLED=false`).
>
> **Re-enabling (installs that DO route DNS through Pi-hole):** set `FOCUS_BLOCKING_ENABLED=true`, `PIHOLE_URLS`, `PIHOLE_PASSWORD` (and optionally `PIHOLE_FOCUS_GROUP`), then recreate the orchestrator. Clients must actually resolve through the Pi-hole with **no secondary/fallback DNS** — any non-Pi-hole resolver (router, ISP, DoH in the browser, corporate VPN) lets blocked domains leak.

## Focus Timer (Pomodoro)

ADHD-friendly focus timer with ambient audio and optional site blocking:

| Feature | Status | Notes |
|---------|--------|-------|
| Timer + voice break | Done | `start_focus`, `stop_focus`, `focus_status` tools |
| Endel audio | Done | Streams HLS from Endel Pacific API to Office speaker |
| Pi-hole blocking | Deprecated (this deployment) | Code retained; disabled via `FOCUS_BLOCKING_ENABLED=false` — see status banner |
| Body doubling check-ins | Done | Periodic TTS check-ins during focus sessions |
| Sprints | Done | Multi-sprint sessions via `focus_sprint` tool (next/extend/end) |
| Ambient audio options | Done | endel, lofi, coffee_shop, silence |
| Session summary | Done | End-of-session TTS with total minutes and sprint count |

**Usage:**
- `"start focus on coding for 30 minutes"` - starts timer + audio (+ blocking, only if enabled and confirmed)
- `"start focus without blocking"` - no site blocking
- `"start focus with lofi music, 4 sprints, check in every 10 minutes"` - body doubling session
- `"stop focus"` or timer expires -> unblocks sites (if blocked), announces break
- `"next sprint"` / `"extend"` / `"end session"` - sprint control via `focus_sprint` tool

**start_focus parameters:** `task`, `duration`, `break_duration`, `speaker`, `soundscape`, `block_sites`, `check_ins`, `check_in_interval`, `audio` (endel/lofi/coffee_shop/silence), `sprints`

**Blocking confirmation (`blocking_confirmed`):** `PiHoleMultiClient` returns `success=True` for no-ops too (blocking disabled in config, no instances configured, empty focus group, every per-domain update rejected). `pihole_client.blocking_confirmed(result)` is true only when `result.success` AND the aggregated `details["domains_toggled"] > 0` (summed across successful instances). `focus_manager.tool_start_focus` and the sprint re-enable path in `tool_focus_sprint` gate on it:
- Confirmed → tool result says sites are blocked, `current_focus_session["block_sites"]` set, `bgw_pihole_blocking_toggles_total{action="enable"}` incremented.
- No-op success → no blocking claim, INFO log `[FOCUS] Site blocking not active: …`.
- Sprint re-enable failure → WARNING log.

The `block_sites` schema description tells the model blocking may be unavailable and to claim it only if the tool result says so. Tests: `orchestrator/tests/test_focus_blocking_confirmation.py`.

**focus_sprint actions:** `next_sprint`, `extend`, `end_session`

**Audio env vars:**
- `FOCUS_AUDIO_LOFI_URL` — lo-fi stream URL for HA media_player
- `FOCUS_AUDIO_COFFEE_URL` — coffee shop stream URL for HA media_player

**Key files:** `orchestrator/focus_manager.py`, `orchestrator/pihole_client.py`

## Pi-hole DNS (whole-house) — HISTORICAL

> Historical as of 2026-09-29: LAN DNS is served by the router, not Pi-hole. Kept for reference / re-enable. Saturn Pi-hole is down; Jupiter Pi-hole runs with no clients; Nebula Sync stopped.

Redundant Pi-hole v6 pair synced via Nebula Sync. Jupiter was primary, Saturn secondary.

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
