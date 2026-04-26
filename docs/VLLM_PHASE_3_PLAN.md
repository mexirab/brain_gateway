# vLLM Phase 3 Plan — Plan B Layout

Status: **Ungated, awaiting maintenance window.** Phase 2 trial completed 2026-04-26 (see ROADMAP.md). All decision-criteria gates passed.

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
- [ ] **Write `vllm-primary.service`** — based on the trial `docker run` invocation; wrap with systemd.
- [ ] **Bench Lorbus 27B on GPU1** — confirm GPU0 → GPU1 doesn't regress tps; verify 153K context fits at 0.93 util on the 48 GB card.
- [ ] **Plan voice repin** — write the `qwen-tts` + `parakeet-stt` env-var diffs; dry-run to confirm both fit on GPU0 alongside the coder.
- [ ] **Schedule 30-min maintenance window** — STT/TTS will be unavailable during the cutover.
- [ ] **Decide on stable 0.19.1 (153K cap) vs wait for stable 0.19.2 (full 256K).** Open question; depends on how soon 0.19.2 ships.

## Open question

**Cut over on 0.19.1 (153K context, available now) or wait for 0.19.2 (256K context, when stable)?**

- 0.19.1 wins: ship the 2.4–3.0× throughput jump *now*; 153K is already 3× the current llama.cpp practical context.
- 0.19.2 wins: avoid a second migration to bump context cap; the unmerged KV-calc fix is the only blocker.

Default lean: **cut over on 0.19.1** to bank the throughput win, then bump to 0.19.2 in-place when stable. 153K easily covers all current use cases; 256K is forward-looking.

## Cross-references

- ROADMAP.md → "vLLM Migration (Phase 2 → Phase 3)" section
- CLAUDE.md → Notes (vLLM Phase 2 trial complete bullet)
- Phase 2 trial throughput numbers, full flag set, and decision-criteria gating: ROADMAP.md
