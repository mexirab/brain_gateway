# Infrastructure Details

## HTTPS Access (Tailscale Serve)

Mobile mic access requires HTTPS. Handled by Tailscale Serve (no nginx needed).

```bash
# Already running — persists across reboots
sudo tailscale serve --bg http://localhost:80

# Disable
sudo tailscale serve --https=443 off

# Cert renewal (auto-managed, but manual if needed)
sudo tailscale cert --cert-file /opt/jupiter/gateway_mvp/certs/jupiter.crt \
  --key-file /opt/jupiter/gateway_mvp/certs/jupiter.key jupiter-amds.tail74fc4a.ts.net
```

**URL:** `https://jupiter-amds.tail74fc4a.ts.net/` (must use domain, not IP, for valid cert)

## RAG Personal Knowledge

154 documents indexed in ChromaDB (`nadim_rag` collection). Source docs organized by category:

| Path | Content |
|------|---------|
| `rag/nadim_rag/10_profile/` | Identity, personality models, AI preferences, Pisces symbolism |
| `rag/nadim_rag/50_patterns/` | Strengths/frictions, triggers/distortions, rejection-shame loop, social frequency plan |
| `rag/nadim_rag/20_meds/` | Medication data (auto-generated from YAML) |
| `rag/nadim_rag/30_projects/` | Project data (auto-generated from YAML) |

```bash
# RAG reindex
cd /opt/jupiter/gateway_mvp/rag && python ingest_rag.py \
  --source ~/rag/nadim_rag \
  --persist ~/.local/share/chroma/personal_rag \
  --collection nadim_rag
```

## Temperature Monitoring

Server closet temperature monitoring with dashboard widget, TTS alerts, and Grafana metrics.

**Dashboard widget:** Shows closet temp, kitchen ambient, heat delta (+F), and estimated monthly AC cooling cost. Polls every 60s. Color-coded: green (<75F), yellow (75-80F), amber (80-85F), red (>85F).

**TTS alerts (every 10 min):**
- 80F warning: "Server closet is at X degrees. Getting warm."
- 85F critical: "Server closet is dangerously hot. Check ventilation."
- Auto-clears when cooled below 78F (allows re-alerting on next heat-up)

**Prometheus metrics:**
- `bgw_temperature_fahrenheit{location="closet|kitchen"}` — sensor readings
- `bgw_temperature_delta_fahrenheit` — closet minus kitchen delta

**Config (env vars):**
- `CLOSET_TEMP_WARNING` — warning threshold in F (default: 80)
- `CLOSET_TEMP_CRITICAL` — critical threshold in F (default: 85)

**HA sensors used:** `sensor.closet_temperature`, `sensor.kitchen_temperature`

## Performance Notes

- Shared `httpx.AsyncClient` (`_http`) reused across all requests — init at startup, closed at shutdown
- HA tool definition cached 300s (`_ha_tool_cache`) — invalidated on entity refresh
- Nemotron agentic loop deduplicated into `_run_nemotron_tool_loop()` — both `call_nemotron_orchestrator()` and `_nemotron_fallback()` call it
- `TERMINAL_TOOLS` set in the loop short-circuits after state-changing tools (start_focus, stop_focus, home_assistant, set_reminder, cancel_reminder, update_data, create_calendar_event) — prevents Nemotron from undoing its own actions in subsequent rounds
- Streaming chunk size: 80 chars (was 20)

## Callisto Kiosk (Monitoring Display)

```bash
./pi-kiosk/deploy.sh                # deploy and start
./pi-kiosk/deploy.sh restart        # restart kiosk display
./pi-kiosk/deploy.sh status         # check status
./pi-kiosk/deploy.sh stop           # stop kiosk
```

## Monitoring

```bash
cd monitoring && docker compose --env-file ../.env -p monitoring up -d
```

See `monitoring/README.md` for full setup details.
