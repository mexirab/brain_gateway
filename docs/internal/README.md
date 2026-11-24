# Internal docs

These docs cover the maintainer's reference deployment (Helios cluster) and historical migration records. They are not required for installing or running Brain Gateway and are kept here so the top-level `docs/` tree stays focused on what end users need.

| File | What it is |
|------|------------|
| `HELIOS_INFRASTRUCTURE.md` | Helios-specific runbook: Tailscale HTTPS cert path, GPU layout, temperature sensors, kiosk deploy script. Reference only — not portable. |
| `CODER_PLAN.md` | Approved plan (Nov 2026) for a sandboxed Qwen Code box: Jess proposes coding jobs you approve with a tap, plus `jess-coder` for interactive use. PR sequence 0–4. PRs 1–2 built (gate + interactive box; user doc `docs/CODER.md`), with the deviations listed in its "PR 2 as built"; PRs 3–4 not yet. |
| `CODER_SPIKE.md` | PR 0 results for `CODER_PLAN.md`: Docker network isolation on Jupiter (and a host-exposure finding for internal networks), Qwen Code 0.25.3 settings + request size, brain-slot cache and abort measurements, and the plan changes they caused. |
| `VLLM_PHASE_3_PLAN.md` | Historical record of the 2026-04-26 cutover from llama.cpp to vLLM 0.19.1. Kept for the Plan A vs Plan B rationale and rollback notes. |

If you are setting up your own install, see `docs/INSTALL.md` and `docs/HARDWARE.md` at the top of `docs/` instead.
