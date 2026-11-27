# Internal docs

These docs cover the maintainer's reference deployment (Helios cluster) and historical migration records. They are not required for installing or running Brain Gateway and are kept here so the top-level `docs/` tree stays focused on what end users need.

| File | What it is |
|------|------------|
| `HELIOS_INFRASTRUCTURE.md` | The maintainer cluster: every node (Helios, Jupiter, Saturn, retired boxes incl. Uranus), how to reach it, what runs there; the Helios GPU layout and brain unit (flags, rules, sandbox, API key); egress audit; temperature monitoring. Reference only — not portable. |
| `HISTORY.md` | How we got here: every past brain (v6 hybrid → Qwen3.5 → Qwen3.6 → Qwen3.8 NVFP4 → 27B Q6_K → Flash-Next), migrations between boxes, removed and retired services. |
| `FLASH_NEXT_TRIAL.md` | The live brain (Qwen3.8-Flash-Next): why this model and quant, VRAM fit, A/B and no-action probe results, the voice-thinking requirement, known limits. |
| `POWER_TIERING_SPEC.md` | The power-tiering plan (orchestrator on Jupiter, Helios asleep) and §11 as built: PT-C wake/sleep through the HA smart plug. |
| `LLAMA_SERVER_REMOTE_MEDIA.md` | llama-server fetches http image URLs (blind SSRF from Helios): the finding, the API key that was applied, the options still open. |
| `CODER_PLAN.md` | Approved plan (Nov 2026) for a sandboxed Qwen Code box: Jess proposes coding jobs you approve with a tap, plus `jess-coder` for interactive use. PR sequence 0–4. PRs 1–2 built (gate + interactive box; user doc `docs/CODER.md`), with the deviations listed in its "PR 2 as built"; PRs 3–4 not yet. |
| `CODER_SPIKE.md` | PR 0 results for `CODER_PLAN.md`: Docker network isolation on Jupiter (and a host-exposure finding for internal networks), Qwen Code 0.25.3 settings + request size, brain-slot cache and abort measurements, and the plan changes they caused. |
| `VLLM_PHASE_3_PLAN.md` | Historical record of the 2026-04-26 cutover from llama.cpp to vLLM 0.19.1. Kept for the Plan A vs Plan B rationale and rollback notes. |
| `CONTAINER_HOST_REACHABILITY.md` | Which Jupiter host services each Docker network can reach (measured 2026-11-21), what containers need, and the host `INPUT` allow-list for Docker bridges (`jess-docker-input`, installed in observe mode 2026-11-22; runbook in `monitoring/README.md`). |

If you are setting up your own install, see `docs/INSTALL.md` and `docs/HARDWARE.md` at the top of `docs/` instead.
