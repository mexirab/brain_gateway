# Decision: Local Brain vs. Cloud Brain

> **Status: DECIDED — Option A (all-local single box), 2026-07-24.** Rationale: the
> system is local-first by design (custom voice clone, `mempalace`, encrypted learned
> facts, `docs/PRIVACY.md`), the data is personal meds/ADHD/self-care context, cost
> between options is a wash, and the 32 GB card is already owned. Cloud brain was
> rejected on privacy + offline-resilience grounds, not cost. Execution plan:
> `LOCAL_SINGLE_BOX_PLAN.md`. Analysis below retained for the record.
>
> **Revised 2026-09-26:** still Option A, but the layout is now **one box, two GPUs**
> (5090 brain + PRO 5000 for TTS/helpers) with **Qwen3.8-27B** as the brain. The
> "32 GB / single card" numbers below describe the superseded one-GPU variant.

## Start here: this is not a cost decision

The whole thread started as "my bill is $300, is it the GPUs?" The winter-bill analysis
settled that:

- **Rate:** $0.174/kWh all-in (~$0.17 marginal).
- **The controllable load is the always-on homelab**, ~$66–92/mo of the bill (the
  year-over-year winter jump, +13–18 kWh/day, no AC involved). AC is the *summer surge*
  on top — the uncontrollable part.
- **The assistant's GPU layer specifically is only ~$15–30/mo.** The bigger homelab
  number is dominated by Jupiter + the ~20 always-on containers (immich, paperless,
  postgres, monitoring…), which stay regardless.

**The electricity win — ~$30–50/mo — comes from dropping the 3 extra GPUs (PRO 5000 +
Saturn's 3080 + 3090). You get that win in *every* option below.** So the choice between
local-brain and cloud-brain is **not** about the power bill. It's qualitative:

| Axis | Weight in this decision |
|---|---|
| **Privacy** — does personal/ADHD/health context leave the house? | High (see `docs/PRIVACY.md` — this system was built local-first) |
| **Offline resilience** — does the assistant work with no internet? | High (it drives HA, reminders, meds) |
| **Brain quality** — reasoning/tool-calling on hard asks | Medium (Sonnet > local 27B) |
| **Voice** — the "Hey Jess" loop | Medium (needs a GPU *either way*) |
| **Maintenance** — models, VRAM tuning, upgrades | Medium |
| **Monthly cost** | **Low — all options land ~$20–50/mo** |

## The three real configurations

Two independent questions define the space: **where's the brain** (local GPU vs cloud
API) and **do you want voice** (voice needs a GPU for TTS/STT regardless of the brain).

| | **A. All-local** | **B. Cloud brain + local voice** | **C. Cloud brain, text-only** |
|---|---|---|---|
| Brain | Qwen 27B on local GPU | **Sonnet 5 via API** | **Sonnet 5 via API** |
| Voice (TTS/STT) | Local, same GPU | Local, small GPU | **None** |
| GPU needed | **32 GB** (5090) | **16 GB** (voice only ~10.3 GB) | **None** |
| Orchestrator/HA/Pi-hole | Local (same box) | Local (Jupiter/CPU box) | Local (Jupiter/CPU box) |
| Saturn | CPU-only backup/DNS/failover | CPU-only backup/DNS/failover | CPU-only backup/DNS/failover |
| ~Electricity (assistant) | ~$20–23/mo | ~$12/mo | ~$3–5/mo |
| ~API | $0 | **~$15–40/mo** (Sonnet, cached) | ~$15–40/mo (cached) |
| **~Total/mo** | **~$20–23** | **~$27–52** | **~$18–45** |
| Works offline? | ✅ Yes | ⚠️ Voice yes, brain no | ❌ No |
| Personal data leaves house? | ✅ No | ❌ Yes (to API) | ❌ Yes |
| Brain quality | Good (27B) | **Best** | **Best** |
| Wake latency | Helios-style (mitigated: always-on) | None | None |
| Maintenance | Highest (VRAM tuning, model upgrades) | Medium | **Lowest** |

Notes:
- **All three keep Saturn** as a CPU-only node and drop the 3 extra GPUs, so all three
  bank the ~$30–50/mo homelab-GPU savings vs. today.
- **Cost spread is small and overlapping** — don't pick on price. A is cheapest and B is
  priciest, but the gap is ~$30/mo, dwarfed by the qualitative differences.
- **API cost assumes prompt caching is added** to `AnthropicBackend`
  (`llm_backend.py`) — cache the system prompt + 30 tool schemas. Without it, Sonnet
  runs ~$50–100/mo instead of $15–40. This is the single most valuable change before
  choosing B or C.
- **Voice can't realistically run CPU-only** — Parakeet + Qwen3-TTS need a GPU for
  acceptable real-time factor. "Want voice" ⇒ "need a GPU" in every option.

## Decision tree

```
Does personal/ADHD/health context leaving the house bother you? (see docs/PRIVACY.md)
├── Yes → Option A (all-local). Non-negotiable if privacy is the point.
└── No  → Must the assistant keep working with no internet? (it runs meds/reminders/HA)
         ├── Yes → Option A, OR Option B/C + a small local fallback model (hybrid).
         └── No  → Do you want the "Hey Jess" voice loop?
                  ├── Yes → Option B (cloud brain, local voice on a 16 GB card).
                  └── No  → Option C (cloud brain, text-only, zero GPU — lowest power/effort).
```

## The crosscutting resilience note (applies to B and C)

A cloud brain means **no internet = no assistant**, and a Saturn failover restores the
*hub* (HA + orchestrator + data) but not a brain. Two ways to de-risk:

- **Hybrid fallback:** primary = Sonnet (cloud), `FALLBACK_MODEL` = a small local model
  on the voice GPU. Best of both — smart when online, degraded-but-alive when not. The
  fallback plumbing already exists (`fallback_model_*` in `config.py`).
- **Accept it:** for a personal assistant, an internet outage that also kills Jess is
  probably tolerable. HA's own local automations keep running either way.

## Recommendation

**Lean Option A (all-local single box).** Reasons, in order:

1. **It's what you built.** The system is local-first by design — custom voice clone,
   the `mempalace` memory, `docs/PRIVACY.md`, encrypted auto-learned facts
   (`auto_learn.key`). The data is meds, self-care, ADHD context, personal routines.
   Sending that to an API to save nothing (cost is a wash) contradicts the premise.
2. **Offline resilience for free.** It drives your meds and reminders; keeping it
   internet-independent is worth a lot.
3. **Cost is not a reason to go cloud** — A is actually the *cheapest* monthly option,
   and you already own the 32 GB card.

**Choose Option B instead if** you specifically want the smarter brain (harder reasoning,
better tool use) and are comfortable with personal context going to the API — then add
prompt caching and a local fallback model, and downsize to a 16 GB voice card.

**Choose Option C only if** you'd drop voice entirely — it's the lowest-effort, lowest-
power path, but you lose the whole "Hey Jess" experience that a lot of this was built for.

## What each option commits you to next

- **A** → follow `LOCAL_SINGLE_BOX_PLAN.md`: 5090 as the single box, `COMPOSE_PROFILES=
  models`, `VLLM_GPU_MEM_UTIL≈0.62`, all model stanzas on `device_ids: ["0"]`.
- **B** → add prompt caching to `AnthropicBackend`; set `MODEL_BACKEND=anthropic` +
  `MODEL_URL`/`MODEL_NAME`/`MODEL_API_KEY`; run TTS/STT on a 16 GB card; set a local
  `FALLBACK_MODEL_*`.
- **C** → same as B minus the voice GPU; disable the voice pipeline; accept text-only.

All three: pull the 3 extra GPUs, keep Saturn CPU-only, keep the nightly backups +
Pi-hole secondary pointed at it.
