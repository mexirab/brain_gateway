# How we got here

The record of past setups in the maintainer deployment: earlier brains, migrations, and services that were removed. Nothing here describes the live system. For that, read `CLAUDE.md`, `HELIOS_INFRASTRUCTURE.md` (same directory) and the unit headers in `tts/`. Per-change detail is in `CHANGELOG.md`.

## Primary model (brain) eras

| Dates | Model | Engine / unit | GPU | Why it ended | Detail |
|-------|-------|---------------|-----|--------------|--------|
| to 2026-03 | Qwen3-32B + Nemotron-8B (v6 hybrid, two models) | — | Helios | Replaced by the v7 unified loop | `ARCHITECTURE.md` (superseded), `scripts/V7_DEPLOYMENT_RUNBOOK.md` |
| 2026-03 → 2026-04-26 | Qwen3.5-27B (+ Qwen3.5-9B fallback at first) | llama.cpp, `llama-server.service` (old `/home/labadmin/llama.cpp` checkout) | Helios (GPU1 PRO 5000 per the v7 runbook, later GPU0) | vLLM Phase 3 | `VLLM_PHASE_3_PLAN.md` |
| early 2026-04 (trial) | Qwen3-VL-30B-A3B (Huihui abliterated) | llama.cpp, `llama-server-moe.service` | Helios | Hallucinated tool calls instead of executing them; reverted to Qwen3.5-27B | — |
| 2026-04-26 → 2026-09-28 | `Lorbus/Qwen3.6-27B-int4-AutoRound` | vLLM 0.19.1, `vllm-primary.service` | GPU0 RTX 5090 | Qwen3.8 cutover | `VLLM_PHASE_3_PLAN.md` |
| 2026-09-28 → 2026-10-02 | `RadixArk/Qwen3.8-27B-NVFP4` | vLLM 0.27.1, `vllm-primary.service` | GPU0 | llama.cpp cutover | `QWEN38_PREP_RESULTS.md`, `HELIOS_INFRASTRUCTURE.md` → `vllm-primary.service` |
| 2026-10-02 → 2026-10-24 | DavidAU Qwen3.8-27B TURBO Q6_K MTP GGUF | llama.cpp build 11358, `llama-server-primary.service` | GPU0 | Flash-Next cutover | `HELIOS_INFRASTRUCTURE.md` → 27B Q6_K unit |
| 2026-10-24 → now | Qwen3.8-Flash-Next UD-Q3_K_XL MTP GGUF | llama.cpp build 11358, `llama-server-primary.service` | GPU0 + GPU1 | — (live) | `FLASH_NEXT_TRIAL.md`, `HELIOS_INFRASTRUCTURE.md` |

### v6 hybrid → v7 unified loop (2026-03)

- v6 ran two models (Qwen3-32B on Helios + Nemotron-8B) behind a mode router. v7 has one model doing conversation and tools in one loop (`unified_loop.py`). "v6 hybrid mode" is gone from the code.
- The migration ran while the stack lived on Jupiter. The stack then moved to Helios (`/opt/gateway_mvp/`), and later back to Jupiter (see Where things run).

### Qwen3.5-27B and the Qwen3-VL-30B trial (to 2026-04-26)

- Qwen3-VL-30B-A3B (Huihui abliterated) was trialed as primary in early April 2026. It hallucinated tool calls instead of executing them, so the brain went back to Qwen3.5-27B on llama.cpp.
- Both units, `llama-server.service` (Qwen3.5-27B) and `llama-server-moe.service` (the VL-30B trial), are disabled. Their unit files stay on Helios as references.

### Qwen3.6 on vLLM 0.19.1 (2026-04-26 → 2026-09-28)

- Phase 3 cutover, Plan A: GPU0 RTX 5090, not the lower-bandwidth PRO 5000.
- Launched at 153,600 context with MTP.
- A single-GPU change on 2026-07-24 (Helios backup `vllm-primary.service.pre-singlegpu`) drifted the live unit to 16,384 context, `--gpu-memory-utilization 0.70`, `--enforce-eager` and no MTP. The TTS shared the 5090 then.
- Kept on Helios for rollback: `vllm-primary.service.qwen36.bak`, the Qwen3.6 weights and the v0.19.1 image. Jupiter: `.env.bak-qwen36`.

### Qwen3.8 NVFP4 on vLLM 0.27.1 (2026-09-28 → 2026-10-02)

- Model: hybrid attention, natively multimodal, 262K native context. Weights hand-staged at `/home/labadmin/models/Qwen3.8-27B-NVFP4` on Helios (copy on Jupiter). Served name `qwen3.8-27b-nvfp4`. Flags: `HELIOS_INFRASTRUCTURE.md` → `vllm-primary.service`.
- Context 131,072, not the 262K 4-bit-KV community config: that needs unmerged vLLM PR #40914, and TurboQuant KV + speculative decoding silently corrupts output on stock vLLM (issue #53180).
- ~111 tok/s decode (Qwen3.6 was ~52), startup ~2m50s. The garble lever then was `--enforce-eager` (drops to ~36 tok/s; meaningless on llama.cpp).
- Same day, `qwen-tts` moved from GPU0 to GPU1 (drop-in `qwen-tts.service.d/gpu1.conf`, `CUDA_VISIBLE_DEVICES=1`).
- Same day, vision moved to the brain: `VISION_MODEL_URL=http://10.0.0.195:8080/v1`, `VISION_MODEL_NAME=qwen3.8-27b-nvfp4`. `analyze_image` and meal-photo estimates were verified (correct descriptions, parseable calorie JSON, 2–5 s). Saturn's Qwen3-VL-8B (:8010) left the runtime path. Since then vision needs Helios awake, like chat. Rollback to Saturn vision: restore the two `VISION_*` lines from Jupiter `.env.bak-vision-saturn`.
- Same day, the expert model (Qwen3-32B on Saturn :8084) was deprecated (`EXPERT_ENABLED=false`).

### Qwen3.8-27B Q6_K on llama.cpp (2026-10-02 → 2026-10-24)

Model, flags and measurements: `HELIOS_INFRASTRUCTURE.md` → 27B Q6_K unit and the header of `tts/llama-server-primary-27b-q6k.service`. What else came with it:

- **Trial before cutover** on the PRO 5000: 65 / 88 / 90 tok/s (prose / code / tool call) with MTP, 45 without.
- **Single slot** (`--parallel 1`): background LLM callers (auto_learn, session_miner, task_decomposition, email→calendar, vision) began to queue behind chat. Each got a 120 s timeout; failures count in `bgw_background_llm_errors_total{caller,error_type}`.
- **Monitoring:** new Prometheus job `llama-primary` (`10.0.0.195:8080/metrics`). This build does not export `kv_cache_usage_ratio`. The target is down most of the day by design, so there is no target-down alert. Grafana: "Brain Server (llama-server)" row. `HighVRAMUsage` stopped excluding GPU0. The `ToolCallsSilentlyDropped` and `ChatStreamTruncating` runbooks point at `journalctl -u llama-server-primary` and `--spec-type none`.
- **Sandbox:** `systemd-analyze security` scored 3.1.
- **Vision:** verified on a 3024×4032 photo: correct description in 5.7 s, VRAM peak 30.3 GB of 32.6.
- **Config:** `config.py` `model_start_cmd` / `model_stop_cmd` default to `sudo systemctl start|stop llama-server-primary` (they named the disabled `llama-server` unit before).
- **Jupiter `.env`:** `MODEL_NAME` / `FALLBACK_MODEL_NAME` / `VISION_MODEL_NAME` = `qwen3.8-27b-turbo-q6k`; `MODEL_URL` and `VISION_MODEL_URL` unchanged.
- **Rollback from this era to vLLM NVFP4:** `.env.bak-qwen38-nvfp4` has no `ACTUAL_*` lines (re-add them, or the budget sync is disabled) and no `MODEL_API_KEY`.
- The prompt-cache layout and loop hardening (see `CLAUDE.md` Notes) were written for this brain.

### Qwen3.8-Flash-Next (2026-10-24 → now)

- Chosen over the 27B for its A/B score (66/66 vs 64/66), faster decode (~135–180 vs ~106–122 tok/s), ~0.5 s warm TTFT and a 27 s cold load. Costs at the time: 64K context instead of 131K, Qwen3-TTS no longer fit on GPU1, and voice turns need thinking on (`VOICE_THINKING_ENABLED=true`). Trial: `FLASH_NEXT_TRIAL.md`.
- 2026-10-24 monitoring changes: Loki alert `BrainFailedToStart` (because `--fit off` makes a short GPU1 fail loudly), `BrainOnCPU` suppressed while either Helios GPU is > 50 % full, the Qwen3-TTS :8002 probe removed.
- 2026-10-27: egress audit and `--api-key-file` (enforced on Helios 2026-10-29). `HELIOS_INFRASTRUCTURE.md` → Egress audit.
- 2026-11-23 (PR #172): the live TTS moved from Helios GPU1 to a Jupiter RTX 5080, and the context went from 64K (`-ts 44,56 -c 65536`) to 262K (`-ts 40,60 -c 262144 -ub 384`). Saturn's RTX 3090 was tried first and was too slow. `docs/VOICE_AND_TTS.md` → VoxCPM2 on Jupiter.

## Where things run (migrations)

| When | What moved | Why | Detail |
|------|------------|-----|--------|
| 2026-03 → 2026-06-26 | Orchestrator stack Jupiter → Helios (`/opt/gateway_mvp/`) → back to Jupiter (CI deploy retargeted 2026-06-26) | Single-box install, then power tiering: the nervous system (reminders, calendar, nudges) must run while the GPU box sleeps | `POWER_TIERING_SPEC.md` |
| 2026-06-25 | Helios wake: Wake-on-LAN plan → HA smart plug (PT-C) | WoL is a dead end on Helios's Aquantia `atlantic` NIC | `POWER_TIERING_SPEC.md` → As built |
| 2026-07-04 | Home Assistant: Raspberry Pi (10.0.0.106) → Jupiter | The Pi's SD card failed | `docs/HA.md` |
| late 2026-09 | LAN DNS: Pi-hole → the router | Recurring Pi-hole issues | `docs/FOCUS_AND_PIHOLE.md` |
| 2026-10-14 → 2026-11-24 | Monitoring kiosk: Callisto (Pi 4, 10.0.0.136) → closet monitors on Jupiter | Callisto unused from 2026-10-14; the Jupiter kiosk went live 2026-11-24 | `scripts/jupiter-kiosk/README.md` |
| 2026-10-17 | Helios promtail → its own compose project, journald only | No Docker socket on Helios | `monitoring/README.md` |
| 2026-10-21 | `nut-exporter` → its own Helios compose project | Out of the main compose | `monitoring/README.md` |
| 2026-11-21 | Two RTX 5080s into Jupiter; Saturn kept as backup box | The 5080 wouldn't run in Saturn | `HELIOS_INFRASTRUCTURE.md` → Cluster nodes |
| 2026-11-23 | Live TTS: Helios GPU1 → a Jupiter RTX 5080 | The brain needed GPU1 for 262K context | `docs/VOICE_AND_TTS.md` → VoxCPM2 on Jupiter |

## Removed or retired services

- **Helios `pihole` + nginx `model-server`** (removed from compose 2026-04-26). The production Pi-holes were then Jupiter (primary) and Saturn (secondary); `PIHOLE_URLS` already pointed there. The model-server was an unfinished file server for a custom "Hey Jess" wake word. Since 2026-11-10 the voice satellite firmware (`models/atom-echo-jess.yaml`) builds `hey_jess.json` + `hey_jess.tflite` in as local files. The `pihole-etc` and `pihole-dnsmasq` named volumes stay on disk until removed by hand. The Helios promtail Docker job that scraped these containers is gone (journald only since 2026-10-17).
- **ollama** on Jupiter and Saturn (stopped + disabled 2026-04-26). Both ran with 0 models loaded. No tool used it; it was left over from experiments.
- **`nebula-sync`** (Pi-hole config sync): gated behind `profiles: ["advanced"]` since the Step 5 productization; stopped since July 2026.
- **Model-layer compose stanzas** (`models` profile, authored 2026-05-16): build-validated, never deployed on Helios. Base image `nvidia/cuda:12.8.1-runtime-ubuntu24.04` with cu128 torch; lockfiles `tts/requirements.txt` (from `qwen-tts-env`) and `tts/requirements-parakeet.txt` (from `parakeet-env`). Not done: removing the Helios systemd units and repointing `MODEL_URL` / `TTS_URL` / `STT_URL` at compose-internal names. `docs/ENV_VARS.md` → Model layer.
- **Expert model** (Qwen3-32B Q4_K_M, Saturn :8084, `expert-model` container): deprecated 2026-09-28. `docs/ENV_VARS.md` → expert section.
- **Saturn vision** (Qwen3-VL-8B-Instruct Q4_K_M, :8010): out of the runtime path 2026-09-28.
- **Code agent** (Qwen3-Coder-Next, `llama-server-coder.service` :8082): retired 2026-10-16; the `code_agent` tool was removed 2026-10-21. `HELIOS_INFRASTRUCTURE.md` → GPU layout.
- **Qwen3-TTS** (`qwen-tts.service` :8002): disabled 2026-10-24, rollback only.
- **Helios `mistral_proxy` / `openai_shim`** (:8000 / :8001): stale, unauthenticated, disabled 2026-10-27. `HELIOS_INFRASTRUCTURE.md` → Egress audit.

Disabled units still on Helios's disk: `HELIOS_INFRASTRUCTURE.md` → History.

## Older records (same directory)

- `VLLM_PHASE_3_PLAN.md`: the 2026-04-26 llama.cpp → vLLM cutover (Plan A vs B).
- `QWEN38_PREP_RESULTS.md` + `HANDOFF_qwen38_prep.md`: the Qwen3.8 NVFP4 trials and acceptance (superseded 2026-10-02).
- `DECISION_local_vs_cloud_brain.md`: local brain chosen over a cloud brain (2026-07-24).
- `LOCAL_SINGLE_BOX_PLAN.md`: the 2026-09-26 GPU consolidation plan (never deployed as written; Helios now holds both GPUs for the brain).
