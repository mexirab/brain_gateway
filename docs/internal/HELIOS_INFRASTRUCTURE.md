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

## Helios GPU Layout (post llama.cpp cutover, 2026-10-02)

| GPU | Card | VRAM | Tenants |
|-----|------|------|---------|
| GPU0 | RTX 5090 | 32 GB | llama.cpp primary only (`llama-server-primary.service`, port 8080, DavidAU Qwen3.8-27B TURBO Fable Q6_K MTP GGUF + `mmproj-F16.gguf`, ~30 GB; peak 30.3 GB of 32.6 with a 3024×4032 vision request) |
| GPU1 | RTX PRO 5000 Blackwell | 48 GB | TTS (`qwen-tts.service`, port 8002, `Qwen3-TTS-1.7B-Base`, voice `jessica`), Coder (`llama-server-coder.service`, port 8082, Qwen3-Coder-Next 80B/3B MoE Q4_K_XL, `CUDA_VISIBLE_DEVICES=1`, MoE expert tensors in CPU RAM via `-ot .ffn_.*_exps.=CPU`) |
| CPU | — | — | STT (`stt-onnx.service`, port 8003, Parakeet TDT 0.6b v2 int8 ONNX Runtime; source `tts/stt_server_onnx.py` + `tts/stt-onnx.service`) |

- **TTS on GPU1 via drop-in** `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28). Before that it silently ran on the 5090: the unit sets `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES`, despite its Description saying GPU1. Revert: delete the file, `daemon-reload`, `restart qwen-tts`.
- `llama-server-coder.service`'s Description says GPU0 — wrong; it runs on GPU1.
- `parakeet-stt.service` (NeMo, GPU, v3) is disabled; it is the GPU alternative to `stt-onnx`, same port + API.

### `llama-server-primary.service` (repo copy: `tts/llama-server-primary.service`) — LIVE since 2026-10-02

The unit's header comments are the authoritative record (weight sha256s, tunables, rollback, sandbox caveats). Summary:

- **Model:** `DavidAU/Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX-MTP-GGUF`, Q6_K MTP quant (24 GB) + `mmproj-F16.gguf`, at `/home/labadmin/models/Qwen3.8-27B-TurboFCF-DavidAU/`. Alias `qwen3.8-27b-turbo-q6k` (= `MODEL_NAME`/`FALLBACK_MODEL_NAME`/`VISION_MODEL_NAME` in Jupiter `.env`).
- **Engine:** llama.cpp `llama-server` build 11358 (2026-10-02) from `/home/labadmin/llama.cpp-mtp/build/bin`. The OLD `/home/labadmin/llama.cpp` checkout (April 2026, build 8932) is what `llama-server-coder.service` uses — it cannot load Qwen3.8 GGUFs and has no `--spec-type`. Two checkouts coexist on purpose; never point this unit at the old one.

| Flag | Value | Why |
|------|-------|-----|
| `-c` | `131072` | Same context as the vLLM era |
| `--cache-type-k/v` | `q8_0` | Weights + 131K KV fit the 5090 with headroom for the vision encoder |
| `-fa on`, `-ngl 999` | | Full GPU offload |
| `--parallel 1` | single slot | MTP gain evaporates past ~4 slots. Background LLM callers (auto_learn, session_miner, task_decomposition, email→calendar, vision) QUEUE behind interactive chat — watch `llamacpp:requests_deferred` |
| `--spec-type draft-mtp --spec-draft-n-max 2` | MTP via the draft head inside the GGUF | Draft acceptance 70–88 %; 3 was ~equal on the PRO 5000, sweep on the 5090 |
| `--jinja --chat-template-kwargs '{"reasoning_effort":"low"}'` | | Tool-call + reasoning parsing from the GGUF template |
| `--reasoning-format auto` | | Thinking routed to `reasoning_content` |
| `--no-webui --no-slots --metrics` | | `/metrics` scraped by Prometheus job `llama-primary` |

Measured on the 5090: 106 prose / 122 code / 121 tool-call tok/s, ~2700 tok/s prompt processing, loads in ~4 s from page cache (~60 s cold from NVMe; `TimeoutStartSec=360`, readiness loop polls `/v1/models` for up to 240 s). vLLM NVFP4 was ~111 tok/s with a ~2m50s start. PRO 5000 trial before cutover: 65/88/90 tok/s with MTP vs 45 without.

- **Sampling:** temperature ≤ 1.0 and `repeat_penalty` 1.0 (llama.cpp's wire name — not `repetition_penalty`) or MTP acceptance collapses.
- **Garbled output / dropped tool calls:** first lever is `--spec-type none` (disables MTP, ~45% slower). `--enforce-eager` was a vLLM flag and means nothing here. Then `journalctl -u llama-server-primary`.
- **Never add `--verbose` or `--slot-save-path`:** both put prompt text (medical facts, conversations) in the journal / on disk.
- **`--api-key` NOT enabled:** `auto_learn.py`, `jobs_calendar.py`, `vision_handler.py`, `meal_manager.py` call `MODEL_URL` with bare httpx and have no api-key plumbing. Network exposure is bounded by the unit's `IPAddressAllow` instead.
- **Sandbox:** runs as nologin `llama` (`useradd -r -M -s /usr/sbin/nologin llama`), `ProtectSystem=strict`, `ProtectHome=tmpfs` with read-only binds of the binary dir + weights dir, `PrivateTmp`, `DevicePolicy=closed` + `DeviceAllow` for `/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`, `/dev/nvidia-uvm-tools` (0666 nodes created by `nvidia-persistenced` at boot), empty capability set, `IPAddressDeny=any` + `IPAddressAllow=127.0.0.0/8 10.0.0.0/24 100.64.0.0/10 172.16.0.0/12` (host-firewall substitute — ufw is inactive on Helios). `systemd-analyze security llama-server-primary` → 3.1. Do NOT add `MemoryDenyWriteExecute` (CUDA JITs PTX into W+X pages) or `PrivateDevices=yes` (hides `/dev/nvidia*`).
- **Rollback to vLLM NVFP4:** on Helios `sudo systemctl disable --now llama-server-primary && sudo systemctl enable --now vllm-primary`; on Jupiter `cp .env.bak-qwen38-nvfp4 .env && docker compose up -d --force-recreate orchestrator`. The two units carry `Conflicts=` so they cannot both hold port 8080.
- **Logs:** journal only. `promtail-helios` has been down since 2026-07-24 and the repo promtail config has no journal scrape, so nothing from this unit reaches Loki (see `monitoring/README.md`; the F-014 self-audit therefore sees no Helios logs).

### `vllm-primary.service` — ROLLBACK unit (repo copy: `tts/vllm-primary.service`; live 2026-09-28 → 2026-10-02, now disabled)

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

- **Garbled output (vLLM era):** add `--enforce-eager` first (drops to ~36 tok/s), then check `journalctl -u vllm-primary`.
- **Rollback from the vLLM unit to Qwen3.6:** `sudo cp /etc/systemd/system/vllm-primary.service.qwen36.bak /etc/systemd/system/vllm-primary.service && sudo systemctl daemon-reload && sudo systemctl restart vllm-primary`; on Jupiter `cp .env.bak-qwen36 .env` and recreate the orchestrator. Qwen3.6 weights and the v0.19.1 image remain on Helios. (An older `vllm-primary.service.pre-singlegpu` backup from 2026-07-24 also sits there.)

Full trial + acceptance record: `QWEN38_PREP_RESULTS.md` (same directory).

### History

2026-09-28 → 2026-10-02: `RadixArk/Qwen3.8-27B-NVFP4` on vLLM 0.27.1, GPU0 (`vllm-primary.service`, now the rollback unit above). Replaced by llama.cpp for ~equal decode speed (106–122 vs ~111 tok/s), a ~4 s warm start instead of ~2m50s, a systemd-sandboxed native process instead of a privileged `docker run`, and llama.cpp's `/metrics`.

2026-04-26 → 2026-09-28: `Lorbus/Qwen3.6-27B-int4-AutoRound` on vLLM 0.19.1, GPU0 (Plan A — a bench showed it at only 28–79% of GPU0 throughput on the PRO 5000; see `VLLM_PHASE_3_PLAN.md` → Outcome). By 2026-09 the unit had drifted to 16,384 ctx, `--gpu-memory-utilization 0.70`, `--enforce-eager`, no MTP, and TTS was sharing the 5090.

Disabled units kept on disk: `vllm-primary.service` (rollback target), `llama-server.service` (was the Qwen3.5-27B primary pre-vLLM; uses the old `/home/labadmin/llama.cpp` checkout), `llama-server-moe.service` (Qwen3-VL-30B-A3B trial).

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
