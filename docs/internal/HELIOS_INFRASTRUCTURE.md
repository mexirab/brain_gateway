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
| `rag/nadim_rag/10_profile/` | Identity & assistant preferences (personal profile notes) |
| `rag/nadim_rag/50_patterns/` | Personal behavioral-pattern & self-knowledge notes |
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

NVIDIA driver baseline: **580+ required for vLLM 0.19+ on Blackwell** (RTX PRO 5000 / 5090, both SM120). Current: 580.173.02. vLLM 0.19's CUDA 12.9 forward-compatibility shim does not work on driver 570 — it surfaces as "Error 804: forward compatibility was attempted on non supported HW."

Migrated 2026-04-26 from `570.169` (NVIDIA UNIX Open Kernel Module from `.run` installer) to `580.126.09` (`nvidia-driver-580-server-open` from Ubuntu noble-security). Method: surgical `.run` uninstall → unhold + purge PPA-held `libnvidia-*-570` packages → `apt install nvidia-driver-580-server-open`. DKMS rebuilt modules for kernel 6.8.0-60-generic.

## Helios GPU Layout (post Qwen3.8 cutover, 2026-09-28)

| GPU | Card | VRAM | Tenants |
|-----|------|------|---------|
| GPU0 | RTX 5090 | 32 GB | vLLM primary only (`vllm-primary.service`, port 8080, `RadixArk/Qwen3.8-27B-NVFP4`, ~30 GB used) |
| GPU1 | RTX PRO 5000 Blackwell | 48 GB | TTS (`qwen-tts.service`, port 8002, `Qwen3-TTS-1.7B-Base`, voice `jessica`), Coder (`llama-server-coder.service`, port 8082, Qwen3-Coder-Next 80B/3B MoE Q4_K_XL, `CUDA_VISIBLE_DEVICES=1`, MoE expert tensors in CPU RAM via `-ot .ffn_.*_exps.=CPU`) |
| CPU | — | — | STT (`stt-onnx.service`, port 8003, Parakeet TDT 0.6b v2 int8 ONNX Runtime; deployed on Helios, source not yet committed to the repo) |

- **TTS on GPU1 via drop-in** `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28). Before that it silently ran on the 5090: the unit sets `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES`, despite its Description saying GPU1. Revert: delete the file, `daemon-reload`, `restart qwen-tts`.
- `llama-server-coder.service`'s Description says GPU0 — wrong; it runs on GPU1.
- `parakeet-stt.service` (NeMo, GPU, v3) is disabled; it is the GPU alternative to `stt-onnx`, same port + API.

### `vllm-primary.service` (repo copy: `tts/vllm-primary.service`)

`docker run vllm/vllm-openai:v0.27.1`, `--gpus device=0`, host port 8080 → 8000, weights bind-mounted read-only from `/home/labadmin/models/Qwen3.8-27B-NVFP4` (HF revision `319f741cce68d7914884900c138a1fbb70a42f30`, 20.4 GiB, hand-staged; copy also on Jupiter).

| Flag | Value | Why |
|------|-------|-----|
| `--served-model-name` | `qwen3.8-27b-nvfp4` | Matches `MODEL_NAME`/`FALLBACK_MODEL_NAME` in Jupiter `.env` |
| `--max-model-len` | `131072` | Model is 262K native; 131K leaves KV headroom for 2 sequences |
| `--kv-cache-dtype` | `fp8` | The clean pairing with MTP (TurboQuant 4-bit KV + spec decoding corrupts output on stock vLLM, issue #53180; the 262K community config also needs unmerged PR #40914) |
| `--speculative-config` | MTP, 3 tokens | ~70% acceptance |
| (no `--enforce-eager`) | CUDA graphs on | ~111 tok/s decode vs ~36 eager (Qwen3.6 was ~52) |
| `--max-num-seqs` | `2` | Chat, `auto_learn` and scheduled jobs overlap |
| `--tool-call-parser` / `--reasoning-parser` | `qwen3_xml` / `qwen3` | |
| `--default-chat-template-kwargs` | `{"reasoning_effort":"low"}` | Template otherwise defaults to `xhigh`; only `xhigh\|medium\|low` are valid — anything else is a hard 400 |
| (no `--language-model-only`) | vision tower on | Brain serves vision too: `VISION_*` points here since 2026-09-28 (Saturn Qwen3-VL-8B retired from the runtime path) |

Startup ~2m50s (torch.compile + CUDA graphs + the `ExecStartPost` `/v1/models` readiness loop; `TimeoutStartSec=900`).

- **Garbled output:** add `--enforce-eager` first (drops to ~36 tok/s), then check `journalctl -u vllm-primary`.
- **Rollback to Qwen3.6:** `sudo cp /etc/systemd/system/vllm-primary.service.qwen36.bak /etc/systemd/system/vllm-primary.service && sudo systemctl daemon-reload && sudo systemctl restart vllm-primary`; on Jupiter `cp .env.bak-qwen36 .env` and recreate the orchestrator. Qwen3.6 weights and the v0.19.1 image remain on Helios. (An older `vllm-primary.service.pre-singlegpu` backup from 2026-07-24 also sits there.)

Full trial + acceptance record: `QWEN38_PREP_RESULTS.md` (same directory).

### History

2026-04-26 → 2026-09-28: `Lorbus/Qwen3.6-27B-int4-AutoRound` on vLLM 0.19.1, GPU0 (Plan A — a bench showed it at only 28–79% of GPU0 throughput on the PRO 5000; see `VLLM_PHASE_3_PLAN.md` → Outcome). By 2026-09 the unit had drifted to 16,384 ctx, `--gpu-memory-utilization 0.70`, `--enforce-eager`, no MTP, and TTS was sharing the 5090.

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
