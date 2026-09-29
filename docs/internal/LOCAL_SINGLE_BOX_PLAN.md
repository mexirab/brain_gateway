# Single-Box Local Consolidation Plan

> **Status: PLANNING (not deployed). Revised 2026-09-26** — layout changed from
> "one box, one GPU" (5090 only) to **"one box, two GPUs" (5090 + PRO 5000)**, and the
> brain target moved from Qwen3.6-27B to **Qwen3.8-27B**. The 2026-07-24 one-GPU version
> is in git history. The all-local decision itself is unchanged — see
> `DECISION_local_vs_cloud_brain.md`.

## Goal

Collapse the homelab GPU fleet down to **one GPU box (Helios) with two cards** running
the full local assistant, and retire every other GPU. The electricity win comes from
dropping the extra cards/boxes, not from the choice of brain (see the decision doc).

## Final layout

| Box | Hardware | Role | Always on? |
|---|---|---|---|
| **Helios** | **RTX 5090 32 GB** (GPU0) + **RTX PRO 5000 48 GB** (GPU1) | GPU model layer: brain, TTS, (STT), helper models | No — power-tiered, woken on demand via HA smart plug (unchanged) |
| **Jupiter** | no GPU | Always-on hub: orchestrator, frontend, HA, Pi-hole primary, monitoring, Immich, Paperless, Actual, Audiobookshelf | Yes |
| **Saturn** | **cheap boot-only GPU** (CPU has no iGPU — board won't POST without a card) | Pi-hole secondary, off-box backup target, cold failover for HA + orchestrator | Yes |

**Retire / sell:** 2× RTX 5080 16 GB (Uranus), RTX 3090 24 GB, RTX 3080, and the
Uranus/Neptune boxes.

**Saturn boot card:** any GPU works. Options: reuse the 3080 (zero effort, but idles
~15–25 W and forfeits a few hundred dollars of resale), or buy a used GT 710 / GT 1030 /
Quadro P400 (~$20–40, ~5 W idle) and sell the 3080. Check the BIOS for a headless /
"no VGA halt" option first — if present, no card is needed at all.

> ⚠️ Verify the 3080's VRAM — this repo's CLAUDE.md records Saturn's 3080 as **10 GB**,
> not 16 GB. Irrelevant for a boot card, but matters for resale listings.

## Why 5090 + PRO 5000 (and not another pair)

Decode speed is memory-bandwidth-bound. The 2026-04 bench (`VLLM_PHASE_3_PLAN.md`)
measured the PRO 5000 at **28–79% of the 5090's LLM throughput**. So: **put the speed
on the brain, put the capacity on the helper card.**

| Card | Job | Why |
|---|---|---|
| **5090** (~1.8 TB/s) | Brain only | Fastest card; dedicating all 32 GB gets the long-context setups people run |
| **PRO 5000** (48 GB, ~300 W) | TTS + helpers | Lots of VRAM at moderate power; bandwidth matters less for small/secondary models |

Rejected pairs:
- **5090 + 3090** — Ampere (no native FP4/FP8), ~350 W, poor idle, half the VRAM of the
  PRO 5000.
- **2× 5080** — 32 GB total; TP2 across PCIe is slower than a single 5090. Same VRAM,
  more complexity.
- **PRO 5000 as the brain card** — gives up 21–72% of decode speed for context we can
  already reach on the 5090 alone.

Mixed cards are fine here because they are **not** tensor-parallel — each GPU serves its
own models.

## GPU budget

### GPU0 — RTX 5090 (32 GB): brain only

**Qwen3.8-27B** (released 2026-08-14): dense 27B, native 262K context, native vision,
thinking controls, MTP head.

Community single-5090 configurations (as reported, not yet verified here):

| Config | Weights | Context | Decode | Caveats |
|---|---|---|---|---|
| vLLM official recipe | `Inferact/Qwen3.8-27B-NVFP4` | 32K | — | Needs `--enforce-eager`. Conservative baseline. |
| MiaAI-Lab repo | `RadixArk/Qwen3.8-27B-NVFP4` (~22 GiB) | **262K** | ~160 t/s | 4-bit KV (`turboquant_4bit_nc`), `max-num-seqs 1` (MTP + concurrency crashes), needs vLLM 0.27.1 + PR #40914 patch (garbled output without it) |
| HF discussion #132 | NVFP4 + lm_head quant + DFlash drafter | 195K | ~222 t/s @8K, ~90 @60K | vLLM main build, CUDA 13.2, fills all 32 GB |

**Recommended starting point:** the MiaAI-Lab / RadixArk config. Single-stream
(`max-num-seqs 1`) is fine — the assistant is effectively one user. Fall back to the
official 32K recipe if the patched build misbehaves; 32K is still 2× today's
single-GPU plan.

### GPU1 — RTX PRO 5000 (48 GB): everything else

| Component | VRAM | Notes |
|---|---:|---|
| Qwen3-TTS-1.7B | ~4 GB | Unchanged — same custom voice |
| STT | 0 GB *or* ~6.3 GB | See STT choice below |
| **Free for helpers** | **~38–44 GB** | See options below |

**STT choice:** `tts/stt_server_onnx.py` + `tts/stt-onnx.service` (written 2026-07-24,
uncommitted, never deployed) run the same Parakeet weights on **CPU via ONNX Runtime**
(~2 GB RAM, ~30× real-time). With 48 GB on GPU1 the VRAM pressure that motivated it is
gone, so either works:
- **ONNX/CPU** — frees GPU1 for helpers, no NeMo dependency. Note the unit file pins
  `nemo-parakeet-tdt-0.6b-v2`; the GPU server uses **v3** — reconcile before deploying.
- **NeMo/GPU** (current `parakeet-stt.service`) — known-good, zero change.

**Helper options for the free ~40 GB** (pick later; none required):
- **Code agent** — Qwen3-Coder-Next 80B/3B MoE stays viable (it already runs on GPU1 +
  system RAM via expert offload). Keeps `code_agent`.
- **Expert model** — replaces Saturn's Qwen3-32B for `ask_expert` / `query_budget`
  analyze mode.
- **Small fast model** — for fast-path / fallback (`FALLBACK_MODEL_*`).

## Tools: what comes back, what goes

| Tool | Status |
|---|---|
| `analyze_image` | **Restored for free** — Qwen3.8 is natively multimodal; repoint `VISION_*` at the brain instead of Saturn's Qwen3-VL-8B. Needs a test. |
| `code_agent` | Kept if the coder stays on GPU1 |
| `ask_expert` | Kept only if an expert model is placed on GPU1; otherwise auto-disabled by `service_registry.py` |

## Migration work (brain upgrade)

This is the biggest risk — `unified_loop` depends on reliable tool calls.

1. **vLLM 0.19.1 → 0.27.x** (plus PR #40914 patch for the RadixArk config). Update the
   `vllm-primary.service` docker image tag, or the compose `vllm-primary` stanza.
2. **Quant format:** AutoRound INT4 → **NVFP4** (Blackwell-native).
3. **Tool-call parser:** `qwen3_xml` (recipe / MiaAI) or `qwen3_coder` (HF thread).
   Re-test the `StreamGate` + XML-fallback path in `unified_loop.py` against the new
   parser before cutover.
4. **Reasoning parser:** `qwen3` (unchanged family).
5. **Config:** `MODEL_NAME` / `FALLBACK_MODEL_NAME` → new served name; revisit
   `VLLM_MAX_MODEL_LEN` and `VLLM_EXTRA_ARGS`.
6. **Acceptance test:** run a representative tool-calling session (HA, reminders,
   `get_data` meds, calendar) and compare against Qwen3.6 before switching.
   Keep Qwen3.6 weights on disk as the rollback.

## Deploy steps (when back)

1. **Hardware:** pull the 2× 5080 and the 3090. Put the boot card in Saturn (or enable
   headless boot). Leave Helios as 5090 (GPU0) + PRO 5000 (GPU1).
2. **Saturn back online:** confirm the secondary Pi-hole resolves, `PIHOLE_URLS` still
   lists it, and the nightly rsyncs land (`JessBackupStale` / `JessHABackupStale`).
3. **USB drives on Jupiter:** reconnect before restarting Immich/Paperless/Audiobookshelf
   (Immich wrote ~226 MB into the unmounted `/mnt/media` stub — move it before mounting).
4. **Brain:** stage Qwen3.8 NVFP4 weights, bring up vLLM 0.27.x on GPU0 alongside the
   old unit (different port), run the acceptance test, then cut `MODEL_URL` over.
5. **GPU1:** TTS as-is; decide STT (ONNX/CPU vs NeMo/GPU); add helpers if wanted.
6. **Repoint** `VISION_*` at the brain; retire Saturn's vision/expert endpoints.
7. **Restart** the orchestrator and monitoring stack on Jupiter; confirm `/health`,
   Grafana, and alert routing.

## Open questions

- Which Qwen3.8 config on the 5090: RadixArk 262K (patched vLLM) vs official 32K (stock)?
- STT: ONNX/CPU or NeMo/GPU? (If ONNX: v2 vs v3 weights.)
- Which helpers on GPU1, if any?
- Saturn: reuse 3080 as boot card, or sell it and buy a ~$30 card?
