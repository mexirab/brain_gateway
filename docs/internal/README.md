# Internal docs

These docs cover the maintainer's reference deployment (Helios cluster) and historical migration records. They are not required for installing or running Brain Gateway and are kept here so the top-level `docs/` tree stays focused on what end users need.

| File | What it is |
|------|------------|
| `HELIOS_INFRASTRUCTURE.md` | Helios-specific runbook: Tailscale HTTPS cert path, GPU layout, temperature sensors, kiosk deploy script. Reference only — not portable. |
| `VLLM_PHASE_3_PLAN.md` | Historical record of the 2026-04-26 cutover from llama.cpp to vLLM 0.19.1. Kept for the Plan A vs Plan B rationale and rollback notes. |

If you are setting up your own install, see `docs/INSTALL.md` and `docs/HARDWARE.md` at the top of `docs/` instead.
