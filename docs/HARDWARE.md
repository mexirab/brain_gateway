# Hardware compatibility matrix

Brain Gateway runs the LLM, TTS, and STT models on a single NVIDIA GPU. This page tells you what to expect at each tier, what the setup wizard will pick for you, and which optional integrations are practical on smaller hardware.

The model recommendation logic itself lives in [`scripts/detect_hardware.sh`](../scripts/detect_hardware.sh) and is authoritative — run it on your box for a one-shot scan:

```bash
bash scripts/detect_hardware.sh                    # human-readable summary
bash scripts/detect_hardware.sh >> .env            # append KEY=value to .env
bash scripts/detect_hardware.sh --json scan.json   # structured scan (wizard uses this)
```

---

## GPU tiers

Tiers are based on the **largest single GPU** the wizard finds (the default `docker-compose.yml` pins vLLM to one GPU; multi-GPU tensor-parallel is opt-in).

| Largest GPU VRAM | Tier | Default model | Quant | Context | GPU mem util |
|------------------|------|---------------|-------|---------|--------------|
| < 10 GiB | **unsupported** | — | — | — | — |
| 10–19 GiB | **below floor** | pick a 7–8B AWQ yourself | awq | 8192 | 0.88 |
| 20–29 GiB | **24** | `Qwen/Qwen3-14B-Instruct-AWQ` | awq | 32768 | 0.90 |
| 30–43 GiB | **32** | `Lorbus/Qwen3.6-27B-int4-AutoRound` | auto_round | 153600 | 0.92 |
| 44+ GiB | **48** | `Lorbus/Qwen3.6-27B-int4-AutoRound` | auto_round | 153600 | 0.93 |

**Why 20 GiB and not 24?** `nvidia-smi` reports memory in MiB; integer division truncates a marketed "24 GB" card (≈23 GiB usable) to 23, and ECC reservation on workstation cards can shave a bit more. The 20 GiB floor keeps a real 24 GB card from being mis-tiered as "below floor."

**Multi-GPU advisory.** If you have 2+ GPUs and the second one is at least 85% of the largest, the wizard flags tensor-parallel as feasible. Wiring TP up needs a manual edit of the `vllm-primary` device list + `--tensor-parallel-size` flag — not done automatically.

---

## Card → tier quick reference

| Card | VRAM | Tier | Default model | Notes |
|------|------|------|---------------|-------|
| RTX 3060 12 GB | 12 GiB | below floor | manual | Boots but quality degrades; pick an 8B AWQ |
| RTX 4060 Ti 16 GB | 16 GiB | below floor | manual | Same as 3060 12 GB |
| RTX 3090 | 24 GiB | 24 | Qwen3-14B-Instruct-AWQ | Solid entry point; common used-card sweet spot |
| RTX 4090 | 24 GiB | 24 | Qwen3-14B-Instruct-AWQ | Same tier as 3090, faster decode |
| RTX 5070 Ti | 16 GiB | below floor | manual | Below tier-24 |
| RTX 5080 | 16 GiB | below floor | manual | Below tier-24 (boot-tested on Uranus) |
| RTX 5090 | 32 GiB | 32 | Qwen3.6-27B-int4-AutoRound | **Recommended sweet spot**. Helios primary. |
| RTX A6000 | 48 GiB | 48 | Qwen3.6-27B-int4-AutoRound | Vision-capable; can run a second VL model |
| RTX PRO 5000 Blackwell | 48 GiB | 48 | Qwen3.6-27B-int4-AutoRound | Same tier as A6000; lower decode bandwidth |

**Driver floor:** NVIDIA driver **580+** for Blackwell (sm_100) cards and for vLLM 0.19+. Driver 570 surfaces "Error 804: forward compatibility was attempted on non supported HW" on Blackwell.

---

## What slows down at each tier

The 24 GiB and 32 GiB tiers run the same stack — only the conversation model size differs. TTS, STT, mempalace search, and tool execution latency are identical.

| Tier | First-token latency | Sustained decode | Cold start (model load) | Notes |
|------|--------------------:|-----------------:|------------------------:|-------|
| 24 | ~0.8–1.5 s | ~40–55 tok/s | ~30–60 s | 14B AWQ; tool calls feel snappy |
| 32 | ~0.6–1.2 s | ~60–80 tok/s | ~60–120 s | 27B INT4 on 5090 (Helios reference) |
| 48 | ~0.8–1.4 s | ~30–55 tok/s | ~90–180 s | 27B INT4 on PRO 5000 — lower bandwidth than 5090 |

*Numbers are reference-deployment measurements on Helios, not formal benchmarks. The PRO 5000's lower decode throughput vs the 5090 was the reason Phase 3 kept vLLM on GPU0 instead of moving it to the bigger card — see [`docs/internal/VLLM_PHASE_3_PLAN.md`](internal/VLLM_PHASE_3_PLAN.md).*

**TTS (Qwen3-TTS):** ~0.4–1.0 s end-of-sentence latency, identical on any GPU that can hold it (~2 GiB VRAM).

**STT (Parakeet TDT v3):** ~real-time × 10 on any RTX 30-series or newer (~6.3 GiB VRAM). Whisper-equivalent quality at far lower cost.

---

## Which optional integrations work at each tier

The default install runs the conversation model + voice + reminders + focus timer + Home Assistant + RAG. Everything below is optional and gated by either `JESS_ADVANCED=true` or `COMPOSE_PROFILES=advanced` in `.env`.

| Integration | Below floor | Tier 24 | Tier 32 | Tier 48 |
|-------------|:-----------:|:-------:|:-------:|:-------:|
| Home Assistant control | ✅ | ✅ | ✅ | ✅ |
| Voice (TTS + STT) | ✅ | ✅ | ✅ | ✅ |
| Reminders + focus timer | ✅ | ✅ | ✅ | ✅ |
| ntfy / Pushover push | ✅ | ✅ | ✅ | ✅ |
| Personal RAG memory | ✅ | ✅ | ✅ | ✅ |
| Google Calendar / Gmail | ✅ | ✅ | ✅ | ✅ |
| Paperless-ngx (OCR) | ✅¹ | ✅¹ | ✅¹ | ✅¹ |
| Monitoring stack (Prom/Graf/Loki) | ⚠️² | ✅ | ✅ | ✅ |
| Code agent (Qwen3-Coder MoE) | ❌ | ❌³ | ❌³ | ✅⁴ |
| Vision (Qwen3-VL-8B) | ❌ | ⚠️⁵ | ⚠️⁵ | ✅ |
| Expert reasoner (Qwen3-32B) | ❌ | ❌⁶ | ❌⁶ | ❌⁶ |

¹ Paperless-ngx runs on a separate box or container; no GPU requirement on this side.
² Advanced profile adds ~1.5 GiB RAM overhead; tight on a 16 GiB system.
³ Code agent is an 80B MoE that spills experts to system RAM. Needs ≥48 GiB GPU + 64 GiB system RAM.
⁴ Realistic only on a second 48 GiB GPU (pinned away from vLLM). On a single-GPU box it competes for VRAM.
⁵ Vision model fits on the same GPU only if you can spare ~6 GiB VRAM; usually means dropping context.
⁶ Expert reasoner runs on a separate box (Qwen3-32B Q4_K_M on a dedicated 24 GiB GPU). Not viable on a single-box install.

---

## System (non-GPU) requirements

- **OS:** Ubuntu 22.04 LTS or 24.04 LTS. Other distros work but you're on your own for driver + DKMS packaging.
- **Kernel:** 6.5+ (needed for nvidia-driver-580). Default on Ubuntu 22.04.5+ and 24.04+.
- **CPU:** any x86-64 from the last ~5 years. The orchestrator is I/O-bound, not CPU-bound.
- **RAM:** 16 GiB minimum (boots); 32 GiB recommended (advanced profile, code agent spill).
- **Disk:** ~120 GiB free. ~40 GiB for the docker images, ~50 GiB for the HF model cache (vLLM + Qwen3-TTS + Parakeet), the rest for ChromaDB + state.
- **Network:** any LAN. For off-LAN access from your phone, Tailscale (free tier) + `tailscale serve` is the easiest path; the maintainer's deployment uses exactly this.

---

## NVIDIA driver gotcha (DKMS vs prebuilt)

Reimaged or freshly-updated boxes can end up with the `linux-modules-nvidia-580-open-<KERNELVER>` *prebuilt* package version-locked to a kernel that no longer matches the running one. Symptom: `nvidia-smi` returns "Failed to initialize NVML: Driver/library version mismatch" even though apt thinks everything is installed.

**Fix:** purge the prebuilt-module packages and install the DKMS flavor instead, which rebuilds against your current kernel:

```bash
sudo apt purge 'linux-modules-nvidia-580-open*'
sudo apt install nvidia-driver-580-open       # DKMS variant
sudo reboot
```

This bit the Uranus boot-test box (kernel 6.8.0-111 vs prebuilt module for 6.8.0-110). The full incident is in commit `cd3c9a4`'s message.

---

## Where to go next

- Hardware looks good? Run the [5-minute install](../README.md#5-minute-install).
- Hardware is below floor? Pick a 7–8B AWQ model (any Qwen3-7B-Instruct-AWQ variant works) and set `VLLM_MODEL` manually in `.env` before `docker compose up -d`.
- Want to actually benchmark your box? `bash scripts/model_layer_smoketest.sh` boots the model layer, runs a 5-prompt smoke test, and tears it down.
