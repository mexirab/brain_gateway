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

**Dashboard widget:** Shows closet temp, room ambient (`sensors.ambient`), heat delta (+F), and estimated monthly AC cooling cost. Polls every 60s. Colours, bar ticks and badge follow the warning/critical thresholds returned by `GET /api/temperatures` (same as the TTS alerts below): green below warning, warning band, critical band.

**TTS alerts (every 10 min, `jobs_monitoring.check_closet_temperature`):**
- Warning (`sensors.temperature.warning`): "Server closet is at X degrees. Getting warm." Held during /settings quiet hours until they end.
- Critical (`sensors.temperature.critical`): "Server closet is dangerously hot. Check ventilation." Always speaks; also marks the warning as announced (no second "getting warm" while cooling).
- Spoken on the `/settings` Speakers `temperature` route (unset → panel `default` → `reminder` route).
- Re-arms when cooled below warning − 3°F (`TEMP_CLEAR_MARGIN_F`). Critical < warning is treated as warning.
- A crossing only counts as announced if the announcement wasn't suppressed (sleep mode / voice session); otherwise it retries next run. State keys `temp:closet_warning` / `temp:closet_critical`.

**Prometheus metrics:**
- `bgw_temperature_fahrenheit{location="closet|kitchen"}` — sensor readings (ambient labelled `kitchen`); series dropped when HA has no reading
- `bgw_temperature_delta_fahrenheit` — closet minus ambient delta

**Prometheus alerts:**
- `ClosetTemperatureCritical` — closet ≥ 95°F for 10m, paging `pushover`. Hard-coded 95 matches the live profile critical; keep them together.
- `ClosetTemperatureSensorMissing` — closet series absent for 2h, quiet `pushover-quiet`.

**Grafana:** jess_glance "Closet Temp" + homelab_infra "Closet vs Ambient" use `CLOSET_TEMP_THRESHOLDS` (`dashgen/panels.py`): green / 87 yellow / 90 orange / 95 red.

**Config:** thresholds live in `user_profile.yaml` → `sensors.temperature.warning` / `.critical` (defaults 80 / 85°F). Env overrides (applied by `user_profile._apply_env_overrides`):
- `CLOSET_TEMP_WARNING` — overrides `sensors.temperature.warning`
- `CLOSET_TEMP_CRITICAL` — overrides `sensors.temperature.critical`

Live deployment runs 90 / 95 (the closet normally sits in the 80s).

**HA sensors used:** `sensors.temperature.closet` (live: `sensor.closet_temperature`) and `sensors.temperature.ambient` (any room sensor) from `user_profile.yaml`.

## Helios GPU Drivers

NVIDIA driver baseline: **580+ required for vLLM 0.19+ on Blackwell** (RTX PRO 5000 / 5090, both SM120). Current: 580.173.02. vLLM 0.19's CUDA 12.9 forward-compatibility shim does not work on driver 570 — it surfaces as "Error 804: forward compatibility was attempted on non supported HW."

Migrated 2026-04-26 from `570.169` (NVIDIA UNIX Open Kernel Module from `.run` installer) to `580.126.09` (`nvidia-driver-580-server-open` from Ubuntu noble-security). Method: surgical `.run` uninstall → unhold + purge PPA-held `libnvidia-*-570` packages → `apt install nvidia-driver-580-server-open`. DKMS rebuilt modules for kernel 6.8.0-60-generic.

### Kernel + IOMMU (2026-10-16)

Chipset IOMMU faults killed Helios's USB controller and NIC on 2026-10-16. Fix: `iommu=pt` added to the kernel cmdline in `/etc/default/grub`, and the box now boots kernel 6.8.0-147-generic. Rollback: restore `/etc/default/grub.bak-pre-iommu-pt` + `update-grub`, and/or pick the 6.8.0-60 entry under GRUB "Advanced options".

## Helios GPU Layout (post Flash-Next cutover, 2026-10-24)

| GPU | Card | VRAM | Tenants |
|-----|------|------|---------|
| GPU0 | RTX 5090 | 32 GB | Primary brain, part 1 (`llama-server-primary.service`, port 8080, Qwen3.8-Flash-Next UD-Q3_K_XL MTP; `-ts 44,56` layer split): ~29 GiB, ~90 % |
| GPU1 | RTX PRO 5000 Blackwell | 48 GB | Primary brain, part 2 + MTP draft head (`-devd CUDA1`) ~37 GiB, plus live TTS (`voxcpm-tts.service`, port 8004, VoxCPM2) ~7.7 GiB: ~92 % used, peak 92.8 % with TTS + vision + generation at once. `qwen-tts.service` (:8002, ~4.5 GiB) is **disabled** — it no longer fits. Coder retired 2026-10-16 (see below) |
| CPU | — | — | STT (`stt-onnx.service`, port 8003, Parakeet TDT 0.6b v2 int8 ONNX Runtime; source `tts/stt_server_onnx.py` + `tts/stt-onnx.service`; offline + LAN-only IP sandbox since 2026-10-27, see Egress audit) |

- **Shared GPU1:** brain generation drops from ~135–180 to ~30–85 tok/s while the TTS synthesizes (spoken replies). `After=voxcpm-tts.service` only orders process start (voxcpm-tts is `Type=simple`), so at boot the two can still race for GPU1; a cold-cache boot test had both ready in 27 s with no restarts. Never re-enable `qwen-tts` while Flash-Next is installed.
- **Qwen3-TTS GPU1 drop-in (rollback TTS only):** `/etc/systemd/system/qwen-tts.service.d/gpu1.conf` → `Environment="CUDA_VISIBLE_DEVICES=1"` (added 2026-09-28; before that it silently ran on the 5090 because the unit sets `QWEN_TTS_DEVICE=cuda:0` with no `CUDA_VISIBLE_DEVICES`). Only matters if the 27B rollback re-enables `qwen-tts`.
- **Coder retired 2026-10-16:** `llama-server-coder.service` (port 8082, Qwen3-Coder-Next 80B/3B MoE Q4_K_XL, `CUDA_VISIBLE_DEVICES=1`, MoE expert tensors in CPU RAM via `-ot .ffn_.*_exps.=CPU`) is stopped + disabled. An agentic debugging eval on planted Jess bugs had the primary brain fix 6/6 with exact one-line fixes; the coder fixed 2/3, ran ~51 tok/s (experts on CPU, 32K ctx) and wandered on a symptom-only bug. Unit file and GGUF (`/home/labadmin/.cache/llama.cpp/Qwen3-Coder-Next-UD-Q4_K_XL.gguf`) are still on disk. `sudo systemctl enable --now llama-server-coder` would restart the server, but nothing in the app can use it: the `code_agent` tool and `CODE_AGENT_*` settings were removed 2026-10-21. The unit's Description says GPU0 — wrong; it ran on GPU1. It would not fit next to Flash-Next anyway.
- `parakeet-stt.service` (NeMo, GPU, v3) is disabled; it is the GPU alternative to `stt-onnx`, same port + API.

### `llama-server-primary.service` (repo copy: `tts/llama-server-primary.service`) — LIVE since 2026-10-24

The unit's header comments are the authoritative record (weight sha256s, flags, VRAM budget, rollback pointer, sandbox caveats). Trial, A/B results and known limits: `FLASH_NEXT_TRIAL.md` (same directory). Summary:

- **Model:** Qwen3.8-Flash-Next (125B MoE, ~6B active per token, multimodal, llama.cpp arch `qwen4exp`): Unsloth UD-Q3_K_XL GGUF (3 shards) + standalone `MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf` + `mmproj-F16.gguf`, at `/home/labadmin/models/Qwen3.8-Flash-Next-Unsloth/`. Alias `qwen3.8-flash-next-q3kxl` (= `MODEL_NAME`/`FALLBACK_MODEL_NAME`/`VISION_MODEL_NAME` in Jupiter `.env`). The "shared" MTP GGUF on disk fails on this build — keep the standalone one.
- **Requires** Jupiter `.env` `VOICE_THINKING_ENABLED=true`: with voice thinking off this brain took unrequested Home Assistant actions. Mitigation, not a control (the flag defaults `false`); never restore a pre-2026-10-24 `.env` with this unit installed.
- **Engine:** llama.cpp `llama-server` build 11358 (2026-10-02) from `/home/labadmin/llama.cpp-mtp/build/bin` (has `qwen4exp` + its MTP). The OLD `/home/labadmin/llama.cpp` checkout (April 2026, build 8932) is what the retired `llama-server-coder.service` uses — it cannot load Qwen3.8 GGUFs and has no `--spec-type`. Two checkouts coexist on purpose; never point this unit at the old one.
- **n-gram table:** the model's 28.8 GB `per_layer_token_embd` is an input GET_ROWS tensor loaded lazily from mmap, so it stays in host RAM / page cache, not VRAM.

| Flag | Value | Why |
|------|-------|-----|
| `CUDA_VISIBLE_DEVICES` | `0,1` | Both GPUs |
| `-sm layer -ts 44,56` | layer split | Fits ~29 GiB on GPU0, ~37 GiB on GPU1 next to the TTS |
| `-md … -devd CUDA1 -ngld all -ctkd/-ctvd q8_0` | MTP draft head on GPU1 | |
| `-c` | `65536` | 131072 would add ~2.5–3 GiB and push GPU1 to ~98 % |
| `--cache-type-k/v` | `q8_0` | |
| `-fa on`, `-ngl 999` | | Full GPU offload |
| `--parallel 1` | single slot | MTP gain evaporates past ~4 slots. Background LLM callers (auto_learn, session_miner, task_decomposition, email→calendar, vision) QUEUE behind interactive chat — watch `llamacpp:requests_deferred` |
| `--spec-type draft-mtp --spec-draft-n-max 2` | MTP | Acceptance ~60 % prose, ~90 % code; ~105 tok/s without MTP |
| `--jinja --chat-template-kwargs '{"reasoning_effort":"low"}'` | | Tool-call + reasoning parsing; also sets the thinking depth of voice turns (with `VOICE_THINKING_ENABLED=true` they send no kwarg) |
| `--reasoning-format auto` | | Thinking routed to `reasoning_content` |
| `--fit off` | | Every placement arg is explicit; a boot that finds GPU1 short fails loudly (Loki alert `BrainFailedToStart`) instead of silently shrinking |
| `--no-webui --no-slots --metrics` | | `/metrics` scraped by Prometheus job `llama-primary` |
| `--api-key-file` | `/etc/llama-server/api-key` | Added 2026-10-27, **enforced on Helios since 2026-10-29**. Every path but `/health` 401s without `Authorization: Bearer <key>`, incl. `/v1/models` and `/metrics`. File `root:llama` 640 in a 750 dir, same value as Jupiter `.env` `MODEL_API_KEY`. `ExecStartPre` refuses a file with no key line (llama-server would run with no auth). Enable/rotate steps + check (`/v1/models` → 401): unit header |

Measured: decode ~135–180 tok/s (137–140 prose / 174–179 code), ~30–85 while the TTS synthesizes on GPU1; warm streaming TTFT through Jess 0.44–0.53 s (first turn after a restart ~8 s: cold prompt cache); loads in 6–9 s from page cache, 27 s cold (`TimeoutStartSec=360`; the readiness loop polls `/health` — the one unauthenticated path — for up to 240 s and gives up at once if the server process dies). A/B on Jess's real prompt + tool schemas: 66/66 vs 64/66 for the 27B.

- **Context limit:** 64K. Chat prompts are ~10–15K tokens, but the self-audit investigator once reached ~70K (2026-10-21); a run that large is rejected and the audit comes back `partial`.
- **Sampling:** temperature ≤ 1.0 and `repeat_penalty` 1.0 (llama.cpp's wire name — not `repetition_penalty`) or MTP acceptance collapses.
- **Garbled output / dropped tool calls:** first lever is `--spec-type none` (disables MTP, ~105 tok/s). `--enforce-eager` was a vLLM flag and means nothing here. Then `journalctl -u llama-server-primary`.
- **Never add `--verbose` or `--slot-save-path`:** both put prompt text (medical facts, conversations) in the journal / on disk.
- **API key:** see `--api-key-file` above. The orchestrator sends it via `orchestrator/model_auth.py` (origin-scoped, all model calls); a mismatch looks like "Helios asleep" and fires `BrainAuthRejected` / `BrainMetricsScrapeRejected` (`monitoring/README.md` → Helios Health Alerts). Plain HTTP on the LAN: it stops callers without the key, not a LAN sniffer. Network exposure is still also bounded by `IPAddressAllow`.
- **Sandbox:** runs as nologin `llama` (`useradd -r -M -s /usr/sbin/nologin llama`), `ProtectSystem=strict`, `ProtectHome=tmpfs` with read-only binds of the binary dir + weights dir, `PrivateTmp`, `DevicePolicy=closed` + `DeviceAllow` for `/dev/nvidia0`, `/dev/nvidia1`, `/dev/nvidiactl`, `/dev/nvidia-uvm`, `/dev/nvidia-uvm-tools` (0666 nodes created by `nvidia-persistenced` at boot), empty capability set, `IPAddressDeny=any` + `IPAddressAllow=127.0.0.0/8 10.0.0.0/24 100.64.0.0/10 172.16.0.0/12` (host-firewall substitute — ufw is inactive on Helios). Check with `systemd-analyze security llama-server-primary`. Do NOT add `MemoryDenyWriteExecute` (CUDA JITs PTX into W+X pages) or `PrivateDevices=yes` (hides `/dev/nvidia*`).
- **Rollback to the 27B:** see the header of `tts/llama-server-primary-27b-q6k.service` (Helios copy `/etc/systemd/system/llama-server-primary.service.27b-q6k.bak` — refresh it from the repo file when the key goes in, or a rollback silently drops auth; also re-enables `qwen-tts` and needs the Jupiter `.env` model names + `VOICE_THINKING_ENABLED=false`; keep the current `MODEL_API_KEY` line — the `.env.bak-*` snapshots predate it). Rolling back to `vllm-primary` turns key enforcement off (no key in that unit). The 27B weights are still on Helios. `llama-server-primary` and `vllm-primary` carry `Conflicts=` so they cannot both hold port 8080.
- **Logs:** journal, shipped to Loki by `promtail-helios`'s journald job (`{host="helios", service="llama-server-primary"}`; level re-derived from llama.cpp's `E`/`W` prefixes). Startup failures fire `BrainFailedToStart`. See `monitoring/README.md` → Log Pipeline.

### 27B Q6_K unit — ROLLBACK (repo copy: `tts/llama-server-primary-27b-q6k.service`; live 2026-10-02 → 2026-10-24)

Same unit name when installed. `DavidAU/Qwen3.8-27B-TURBO-Fable-Cold-Fusion-735-882-Heretic-Uncensored-NEO-CODER-MAX-MTP-GGUF`, Q6_K MTP quant (24 GB) + `mmproj-F16.gguf`, at `/home/labadmin/models/Qwen3.8-27B-TurboFCF-DavidAU/`, alias `qwen3.8-27b-turbo-q6k`, GPU0 RTX 5090 alone (~30 GB; peak 30.3 GB of 32.6 with a 3024×4032 vision request), same engine. Flags: `-c 131072`, q8_0 K/V, `-fa on`, `--parallel 1`, `--spec-type draft-mtp --spec-draft-n-max 2` (MTP head inside the GGUF; acceptance 70–88 %), same `--jinja` / `reasoning_effort` low / `--reasoning-format auto` / `--metrics`. Measured on the 5090: 106 prose / 122 code / 121 tool-call tok/s, ~2700 tok/s prompt processing, ~4 s warm load, ~60 s cold. Its header carries the rollback steps and the 27B weight sha256s. Rollback from it to vLLM NVFP4: `sudo systemctl disable --now llama-server-primary && sudo systemctl enable --now vllm-primary`; on Jupiter `cp .env.bak-qwen38-nvfp4 .env && docker compose up -d --force-recreate orchestrator`.

### `vllm-primary.service` — older ROLLBACK unit (repo copy: `tts/vllm-primary.service`; live 2026-09-28 → 2026-10-02, now disabled)

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

2026-10-02 → 2026-10-24: DavidAU Qwen3.8-27B TURBO Q6_K MTP on llama.cpp, GPU0 alone (now the 27B rollback unit above). Replaced by Qwen3.8-Flash-Next for a better A/B score (66/66 vs 64/66), faster decode (~135–180 vs ~106–122 tok/s), ~0.5 s warm TTFT and a 27 s cold load; costs: 64K context instead of 131K, Qwen3-TTS no longer fits on GPU1, and voice turns need thinking on. See `FLASH_NEXT_TRIAL.md`.

2026-09-28 → 2026-10-02: `RadixArk/Qwen3.8-27B-NVFP4` on vLLM 0.27.1, GPU0 (`vllm-primary.service`, now the older rollback unit above). Replaced by llama.cpp for ~equal decode speed (106–122 vs ~111 tok/s), a ~4 s warm start instead of ~2m50s, a systemd-sandboxed native process instead of a privileged `docker run`, and llama.cpp's `/metrics`.

2026-04-26 → 2026-09-28: `Lorbus/Qwen3.6-27B-int4-AutoRound` on vLLM 0.19.1, GPU0 (Plan A — a bench showed it at only 28–79% of GPU0 throughput on the PRO 5000; see `VLLM_PHASE_3_PLAN.md` → Outcome). By 2026-09 the unit had drifted to 16,384 ctx, `--gpu-memory-utilization 0.70`, `--enforce-eager`, no MTP, and TTS was sharing the 5090.

Disabled units kept on disk: `qwen-tts.service` (rollback TTS, disabled 2026-10-24), `vllm-primary.service` (older rollback target), `llama-server.service` (was the Qwen3.5-27B primary pre-vLLM; uses the old `/home/labadmin/llama.cpp` checkout), `llama-server-moe.service` (Qwen3-VL-30B-A3B trial).

## Egress audit (2026-10-27)

~10 h of overnight packet capture on Helios and Jupiter. **Result: no phone-home by the Qwen model** — no China-linked IPs, ASNs or domains; llama-server's `llama` uid opened no connections; its systemd `IPAddressDeny=any` + LAN/tailnet/Docker allow-list held. The phone-home traffic it did find (Grafana and Loki usage stats / update checks) is off now, and so are Netdata's statistics (below).

Repo changes (units take effect only when installed on Helios):

| Item | Change |
|------|--------|
| `llama-server-primary` (+ 27B rollback unit) | `--api-key-file /etc/llama-server/api-key` — see the flags table above. Installed 2026-10-29; the pre-key copies are `llama-server-primary.service.bak-pre-apikey-20261029` and `llama-server-primary.service.27b-q6k.bak.pre-apikey-20261029` in `/etc/systemd/system` |
| `stt-onnx.service` | `HF_HUB_OFFLINE=1` (model loads from `~/.cache/huggingface`; a model change = download first with it unset, outside the unit), `NoNewPrivileges=yes`, `IPAddressDeny=any` + `IPAddressAllow=127.0.0.0/8 10.0.0.0/24 100.64.0.0/10 172.16.0.0/12` (same list as the brain). It was the only model server on Helios that could reach the internet. Network-only sandbox: still runs as `labadmin` with no filesystem sandbox |
| `voxcpm-tts.service` | `InaccessiblePaths=-/etc/llama-server` — runs as the same `llama` user as the brain, so it must not read the key. The `-` skips a missing path: restart `voxcpm-tts` after `/etc/llama-server` is first created |
| `vllm-primary.service` | Header only: no API key in that unit, so rolling back to it turns key enforcement off |
| Grafana / Loki (Jupiter) | Usage stats, update checks, plugin key download/preinstall and news feed off — `monitoring/README.md` → No phone-home |

Host-side, outside the repo (done 2026-10-27):

- **Helios `mistral_proxy` / `openai_shim`** (:8000 / :8001): stale Aug 2025 services, unauthenticated, nothing used them — disabled.
- **Netdata** (Helios + Jupiter): opted out of anonymous statistics (`/etc/netdata/.opt-out-from-anonymous-statistics`). Helios Netdata was also streaming to a parent `ceres` that doesn't exist — streaming disabled.
- **Jupiter `qwen` CLI wrapper** (`~/.local/bin/qwen`, Qwen Code driven by this brain): reads the brain key from `~/.qwen/brain-api-key` (mode 600).

**Remaining gaps:** the key crosses the LAN in plain HTTP (stops callers without it, not a LAN sniffer). The brain's and STT's allow-lists still include the LAN, so DNS through the LAN resolver is the one internet-bound path they keep; narrowing `IPAddressAllow` (`LLAMA_SERVER_REMOTE_MEDIA.md` → Option A) is still open.

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

Helios's systemd journal (model units, `nut-*`, kernel) is shipped to Loki on Jupiter by `promtail-helios`, which runs from its own compose project `monitoring/promtail/docker-compose.helios.yml`, deployed to `/opt/promtail-helios` on Helios (needs a persistent journal). It pushes over the LAN to `loki-push-gw` on Jupiter `:3101` (push-only nginx; Helios IP + `LOKI_PUSH_TOKEN` bearer). Jupiter Loki stays `127.0.0.1`-only. Deploy:

```bash
rsync -a monitoring/promtail/{docker-compose.helios.yml,promtail-helios.yml} labadmin@helios:/opt/promtail-helios/
# /opt/promtail-helios/.env on Helios, chmod 600: LOKI_PUSH_TOKEN=<same as Jupiter .env>
ssh labadmin@helios 'cd /opt/promtail-helios && docker compose -f docker-compose.helios.yml -p promtail-helios up -d'
```

**Advanced profile gating:** `nebula-sync` (multi-Pi-hole replication) is behind `profiles: ["advanced"]` in `docker-compose.yml`. Default installs skip it; set `COMPOSE_PROFILES=advanced` in `.env` to bring it up. `nut-exporter` (UPS metrics, :9199) runs from its own compose project `/opt/nut-exporter-helios` (`monitoring/nut-exporter/docker-compose.helios.yml`, since 2026-10-21) — never `compose up` the stale `/opt/gateway_mvp` checkout (container_name collision). Deploy: `monitoring/README.md` → Deploy nut-exporter on Helios. `NODE_JUPITER_IP` and `NODE_SATURN_IP` use soft `${VAR:-}` defaults so default installs don't fail compose validation.

See `monitoring/README.md` for full setup details including the two-promtail topology and LogQL examples.
