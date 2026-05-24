# vLLM Phase 3 Plan — Plan A Layout (LANDED)

Status: **DONE — cutover landed 2026-04-26.** Primary model swapped from Qwen3.5-27B (llama.cpp) to Lorbus/Qwen3.6-27B-int4-AutoRound (vLLM 0.19.1) on Helios GPU0. See [Outcome](#outcome) section at bottom for what actually shipped vs. the original Plan B.

**Original plan body kept below as historical reference. The decisive change at cutover time was Plan B → Plan A: vLLM stays on the RTX 5090 (GPU0) — the same card used for the Phase 2 trial — instead of moving to the RTX PRO 5000 (GPU1). The voice services (qwen-tts, parakeet-stt) did NOT move; the coder did.**

## Trial recipe (already validated)

- **Image:** `vllm/vllm-openai:latest` (= v0.19.1 at trial time)
- **Model:** `Lorbus/Qwen3.6-27B-int4-AutoRound`
- **Flags:** AutoRound INT4 + MTP n=3 + flashinfer + fp8_e4m3 KV + `--language-model-only --skip-mm-profiling --performance-mode interactivity --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --max-num-seqs 2`
- **Context cap:** 153,600 tokens at `--gpu-memory-utilization 0.93` on RTX 5090. Full 256K requires vLLM 0.19.2+ (unmerged KV-calc fix in 0.19.1).

## Plan B — Target GPU layout

```
GPU1 RTX PRO 5000 Blackwell (48 GB) — vLLM primary only
  Lorbus 27B INT4:                ~17 GB
  KV pool @ 256K (when 0.19.2+):   ~9 GB
  Activations + CUDA graphs:       ~6 GB
  Total:                          ~32 GB / 48 GB

GPU0 RTX 5090 (32 GB) — Coder + Voice
  llama-server-coder (Qwen3-Coder): ~4 GB
  qwen-tts (re-pin cuda:1 -> cuda:0): ~5 GB
  parakeet-stt (re-pin cuda:1 -> cuda:0): ~6 GB
  Total:                           ~15 GB / 32 GB
```

Rationale: keeps the primary on the larger card (room to grow KV when 0.19.2 lands), and consolidates all "supporting" services on the 5090 with ~17 GB headroom.

## Systemd / config changes required

| Change | File / target | Notes |
|--------|---------------|-------|
| New `vllm-primary.service` | `/etc/systemd/system/vllm-primary.service` (Helios) | Wraps `docker run vllm/vllm-openai:0.19.1+` with the trial flags above; `CUDA_VISIBLE_DEVICES=1` for physical GPU1, `Restart=on-failure`. |
| Stop / disable `llama-server.service` | systemd | Keep unit on disk as historical reference (same pattern as `llama-server-moe.service`). |
| Re-pin `qwen-tts.service` | `QWEN_TTS_DEVICE=cuda:0` (or whatever the post-`CUDA_VISIBLE_DEVICES` mapping resolves to) | Currently `cuda:1`. |
| Re-pin `parakeet-stt.service` | `CUDA_VISIBLE_DEVICES=0` + `PARAKEET_DEVICE=cuda:0` | Currently `CUDA_VISIBLE_DEVICES=1`. |
| Orchestrator `MODEL_URL` | `.env` on Helios | No change if vLLM stays on port 8080; otherwise update. |
| Tool-call parser sanity-check | trial showed `qwen3_coder` parser works for the Lorbus 27B; re-verify after image bump. |

## Readiness checklist

- [x] **Driver 580+ on Helios** — done 2026-04-26 (was blocking Blackwell sm_100 forward-compat).
- [x] **Parakeet STT live** — done 2026-04-26 (replaced Whisper on port 8003; one less GPU1 tenant to repin later).
- [x] **Write `vllm-primary.service`** — done 2026-04-26 (`/etc/systemd/system/vllm-primary.service`).
- [x] **Bench Lorbus 27B on GPU1** — done 2026-04-26. Result: 28–79% of Phase 2 throughput. **Triggered Plan B → Plan A pivot.**
- [x] ~~**Plan voice repin**~~ — superseded by Plan A. Voice services stayed on GPU1; coder repinned instead.
- [x] **Schedule 30-min maintenance window** — done 2026-04-26.
- [x] **Decide on stable 0.19.1 (153K cap) vs wait for stable 0.19.2 (full 256K).** Resolved: **cut over on 0.19.1** to bank the throughput win now. 256K migration deferred until 0.19.2 stabilizes.

## Open question

**Cut over on 0.19.1 (153K context, available now) or wait for 0.19.2 (256K context, when stable)?**

- 0.19.1 wins: ship the 2.4–3.0× throughput jump *now*; 153K is already 3× the current llama.cpp practical context.
- 0.19.2 wins: avoid a second migration to bump context cap; the unmerged KV-calc fix is the only blocker.

Default lean: **cut over on 0.19.1** to bank the throughput win, then bump to 0.19.2 in-place when stable. 153K easily covers all current use cases; 256K is forward-looking.

## Cross-references

- ROADMAP.md → "vLLM Migration (Phase 2 → Phase 3)" section
- CLAUDE.md → Notes (vLLM Phase 3 cutover landed bullet)
- Phase 2 trial throughput numbers, full flag set, and decision-criteria gating: ROADMAP.md

---

## Outcome

**Cutover landed 2026-04-26. Layout shipped: Plan A, not Plan B.**

### Why Plan A (not Plan B)

A pre-cutover bench of Lorbus 27B on GPU1 (RTX PRO 5000 Blackwell, 48 GB) hit only 28–79% of the Phase 2 throughput recorded on GPU0 (RTX 5090, 32 GB). The PRO 5000 has lower memory bandwidth and fewer SMs than the 5090; its only advantage is more VRAM, which Lorbus at 153K context doesn't need. The Phase 2 trial environment was the 5090, so the throughput numbers in ROADMAP.md were RTX 5090 numbers — moving to GPU1 would have given up the throughput win that justified the migration.

Plan A keeps vLLM on the card that delivered the win, and instead repins the **coder** GPU0 → GPU1 to free the 5090 for the primary. Voice services were left in place on GPU1.

### Final GPU layout (post-cutover)

```
GPU0 RTX 5090 (32 GB) — vLLM primary only
  Lorbus 27B INT4 + KV @ 153K + activations:  ~28 GB / 32 GB at gpu-memory-utilization=0.93

GPU1 RTX PRO 5000 Blackwell (48 GB) — Coder + Voice
  llama-server-coder (Qwen3-Coder-Next 80B/3B MoE Q4_K_XL): MoE expert tensors in CPU RAM
  qwen-tts:                                                  unchanged on GPU1
  parakeet-stt:                                              unchanged on GPU1
```

### What actually changed

| Change | File / target | Done |
|--------|---------------|------|
| New `vllm-primary.service` | `/etc/systemd/system/vllm-primary.service` (Helios) | ✅ Wraps `docker run vllm/vllm-openai:v0.19.1`, `--gpus "device=0"`, port 8080→8000, full Phase 2 flag set (AutoRound INT4 + MTP n=3 + flashinfer + fp8_e4m3 KV + `--max-model-len 153600` + `--gpu-memory-utilization 0.93`). |
| Stop / disable `llama-server.service` | systemd | ✅ Stopped + disabled. Unit file retained on disk as historical reference (same pattern as `llama-server-moe.service`). |
| Repin `llama-server-coder.service` GPU0 → GPU1 | `CUDA_VISIBLE_DEVICES=0` → `1` | ✅ Same model, same port 8082, GPU only changed. |
| Voice services unchanged | qwen-tts, parakeet-stt | ✅ Stayed on GPU1 — Plan B would have repinned them, but Plan A made the repin unnecessary. |
| `MODEL_NAME` + `FALLBACK_MODEL_NAME` | `.env` on Helios | ✅ Flipped from `Qwen3.5-27B` → `qwen3.6-27b-int4`. Orchestrator force-recreated to pick up the change. |
| Rollback script | `/home/labadmin/vllm-trial/rollback_phase3.sh` | ✅ Idempotent restore: stops vllm-primary, re-enables llama-server, swaps coder GPU1 → GPU0, reverts `.env` model names. |

### Forward-looking trade-off

When vLLM 0.19.2 ships with the unmerged KV-calc fix and we want the full 256K context, the math no longer fits on the 5090: Lorbus + 256K KV exceeds 32 GB. At that point the primary will need to migrate to GPU1 (RTX PRO 5000, 48 GB) and we'll need to re-bench whether the 0.19.2 throughput on GPU1 is acceptable. Until then, 153K is plenty.

### Open question (resolved)

> Cut over on 0.19.1 (153K context, available now) or wait for 0.19.2 (256K context, when stable)?

**Resolved: cut over on 0.19.1.** Banked the 2.4–3.0× throughput win immediately. 153K covers all current use cases. Will revisit the GPU1 migration when 0.19.2 stabilizes and 256K becomes worth pursuing.
