# Infrastructure Details

## HTTPS Access (Tailscale Serve)

Mobile mic access requires HTTPS. Handled by Tailscale Serve (no nginx needed).

```bash
# Already running — persists across reboots
sudo tailscale serve --bg http://localhost:80

# Disable
sudo tailscale serve --https=443 off

# Cert renewal (auto-managed, but manual if needed)
sudo tailscale cert --cert-file /opt/gateway_mvp/certs/helios.crt \
  --key-file /opt/gateway_mvp/certs/helios.key helios.tail74fc4a.ts.net
```

**URL:** `https://helios.tail74fc4a.ts.net/` (must use domain, not IP, for valid cert)

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
cd /opt/gateway_mvp/rag && python ingest_rag.py \
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

## Helios GPU Drivers

NVIDIA driver baseline: **580+ required for vLLM 0.19+ on Blackwell sm_100** (RTX PRO 5000 / 5090). vLLM 0.19's CUDA 12.9 forward-compatibility shim does not work on driver 570 — it surfaces as "Error 804: forward compatibility was attempted on non supported HW."

Migrated 2026-04-26 from `570.169` (NVIDIA UNIX Open Kernel Module from `.run` installer) to `580.126.09` (`nvidia-driver-580-server-open` from Ubuntu noble-security). Method: surgical `.run` uninstall → unhold + purge PPA-held `libnvidia-*-570` packages → `apt install nvidia-driver-580-server-open`. DKMS rebuilt modules for kernel 6.8.0-60-generic.

## Helios GPU Layout (post vLLM Phase 3, 2026-04-26)

| GPU | Card | VRAM | Tenants |
|-----|------|------|---------|
| GPU0 | RTX 5090 | 32 GB | vLLM primary (`vllm-primary.service`, port 8080, Lorbus/Qwen3.6-27B-int4-AutoRound) |
| GPU1 | RTX PRO 5000 Blackwell | 48 GB | Coder (`llama-server-coder.service`, port 8082, Qwen3-Coder-Next 80B/3B MoE Q4_K_XL with MoE expert tensors in CPU RAM via `-ot .ffn_.*_exps.=CPU`), TTS (`qwen-tts.service`, port 8002), STT (`parakeet-stt.service`, port 8003) |

vLLM was kept on GPU0 (Plan A) because a pre-cutover bench showed Lorbus 27B on GPU1 hit only 28–79% of the Phase 2 throughput recorded on GPU0 — the PRO 5000 has lower memory bandwidth and fewer SMs than the 5090. Forward-looking: when vLLM 0.19.2 ships with the unmerged KV-calc fix and 256K context becomes feasible, the primary will need to migrate to GPU1 (the 5090's 32 GB can't hold Lorbus + 256K KV). See `docs/VLLM_PHASE_3_PLAN.md` → Outcome.

Disabled units kept on disk as historical reference: `llama-server.service` (was the Qwen3.5-27B primary pre-vLLM), `llama-server-moe.service` (Qwen3-VL-30B-A3B trial).

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

Helios container logs are shipped to Loki on Jupiter via a promtail sidecar (`promtail-helios`) defined in the main `docker-compose.yml`. The push path uses Tailscale MagicDNS (`LOKI_PUSH_URL`). If the tailnet is down, override `LOKI_PUSH_URL` to the Jupiter LAN IP (`http://10.0.0.248:3100/loki/api/v1/push`).

**Advanced profile gating:** `promtail` (Helios sidecar), `nebula-sync` (multi-Pi-hole replication), and `nut-exporter` (UPS metrics) are all behind `profiles: ["advanced"]` in `docker-compose.yml`. Default installs skip them; set `COMPOSE_PROFILES=advanced` in `.env` to bring them up. `LOKI_PUSH_URL`, `NODE_JUPITER_IP`, and `NODE_SATURN_IP` use soft `${VAR:-}` defaults so default installs don't fail compose validation.

See `monitoring/README.md` for full setup details including the two-promtail topology and LogQL examples.
