# Brain Gateway

**A private, voice-first assistant built for ADHD brains — running entirely on your own hardware.**

Dump a tangle of thoughts at 2 AM and it sorts them into reminders, tasks, and memory. Get nudged through your morning routine. Run a focus sprint with a body-double checking in on you. Set a reminder that actually lands — voice on the speakers *and* a one-tap Done/Snooze push on your phone. Ask it anything. None of it leaves your house.

> **Status:** [v1.0.0](https://github.com/mexirab/brain_gateway/releases/tag/v1.0.0) shipped May 2026. Single-box install with a 2-question CLI wizard. The default build runs the conversation model + voice (TTS/STT) + reminders + focus timer. Optional integrations (Home Assistant, Google Calendar, Gmail, ntfy/Pushover push, Paperless-ngx, monitoring stack) get wired up from the `/settings` page after install.

---

## See it in 30 seconds

<!-- DEMO GIF: replace this block with docs/img/demo.gif once recorded — see docs/DEMO.md for the capture script. -->

![Brain dump → reminder fires → one-tap Done/Snooze on your phone](docs/img/demo.gif)

> _Recording pending — see [`docs/DEMO.md`](docs/DEMO.md) for the shot list (brain-dump → reminder fires → Done/Snooze push)._

**Just want to poke at it before committing any hardware?** You can spin up the
full stack on a rented GPU for a few cents — see
[**Try it in 5 minutes on a rented GPU**](docs/TRY_IT_RENTED_GPU.md).

---

## Is this for me?

Brain Gateway is for you if you want an ADHD-aware assistant that stays on your own machines instead of in someone else's cloud. There are three ways to run it, from "I have a gaming GPU" down to "I have a MacBook" — pick the row that matches what you've got:

| You have… | Run it with | What you get |
|-----------|-------------|--------------|
| **A Linux box with a 24 GB+ NVIDIA GPU** | `install.sh` (all-in-one) | Everything local: LLM + spoken voice (TTS/STT) + reminders + the full feature set. The flagship path. |
| **A Mac, a Windows PC, or any box without an NVIDIA GPU** | [`scripts/byo-setup.sh`](docs/BYO_MODEL.md) | The assistant runs CPU-only and talks to a model *you* provide — [Ollama](https://ollama.com)/[LM Studio](https://lmstudio.ai) on your Mac, another box on your LAN, or a cloud API with your own key. Text + reminders + calendar + brain-dump + RAG all work; spoken voice is off. |
| **A spare Linux box *and* a separate GPU machine** | `install.sh --role nerves` | A CPU-only "nervous system" node (reminders, calendar, nudges, push — 24/7, no GPU) that points at the LLM running on your GPU box. See [`docs/BYO_MODEL.md`](docs/BYO_MODEL.md#run-on-a-dedicated-linux-cpu-node). |
| **Nothing yet / want to try before buying** | A rented GPU | [Try it in 5 minutes on RunPod or vast.ai](docs/TRY_IT_RENTED_GPU.md) for pennies, no commitment. |

**Honest hardware reality for the all-in-one path:** the bundled local-AI stack runs the LLM, TTS, and STT models on a single NVIDIA GPU. `install.sh` interrogates the hardware and picks a model that fits your VRAM.

| GPU VRAM | Tier | Default model | Notes |
|----------|------|---------------|-------|
| **< 20 GiB** | below floor | `Qwen/Qwen3-8B-AWQ` (auto) | RTX 4060 Ti / 5070 Ti / 5080 class. Boots; install.sh auto-substitutes the 8B model + bumps context to 16k. Quality is lower than tier 24+. |
| **20–29 GiB** | 24 GiB | `Qwen/Qwen3-14B-Instruct-AWQ` | RTX 3090 / 4090 class. Good general performance. |
| **30–43 GiB** | 32 GiB | `Lorbus/Qwen3.6-27B-int4-AutoRound` | RTX 5090 class. Recommended sweet spot. |
| **44+ GiB** | 48 GiB | `Lorbus/Qwen3.6-27B-int4-AutoRound` | RTX PRO 5000 / A6000 class. Vision-capable; can run a second VL model. |

Other requirements for the all-in-one path:
- **OS:** Ubuntu 22.04 or 24.04 (other distros work but driver/DKMS dance is on you)
- **NVIDIA driver:** 580+ (required for Blackwell + vLLM 0.19)
- **RAM:** 16 GiB minimum, 32 GiB recommended
- **Disk:** ~120 GiB free (model weights + HF cache + container images)
- **Network:** any LAN; Tailscale recommended for off-LAN HTTPS access

No GPU at all? The CPU-only path needs only Docker and ~8 GB free RAM (for a 14B local model) — or almost nothing if you point it at a cloud API. Full compatibility matrix: [`docs/HARDWARE.md`](docs/HARDWARE.md). CPU/Mac/cloud setup: [`docs/BYO_MODEL.md`](docs/BYO_MODEL.md).

---

## Privacy

**Your 2 AM brain dumps never leave your house.** Brain Gateway is local-first by design — your conversation history, RAG knowledge, reminders, and the memory of your life live in local SQLite + ChromaDB on your own hardware, and nothing else.

- **No telemetry, ever.** No usage stats, no crash reports, no phone-home — in any mode.
- **Local by default.** On the all-in-one and CPU-on-your-Mac paths, the model runs on hardware you control. Nothing about a conversation leaves your network.
- **Cloud is opt-in, and it's *your* key.** If you choose a cloud model (because your hardware can't run one locally), you bring your own Anthropic or OpenAI API key — the turns go to *your* provider account, no middleman, and reputable providers don't train on API data by default.
- **Your stored life stays put — always.** The model backend is stateless: even in cloud mode, only the *current* turn's prompt is sent. Your memory palace, RAG corpus, and history never leave the box, regardless of which model you use.

Full disclosure of what data is handled, where it lives, and what can leave the box: [`docs/PRIVACY.md`](docs/PRIVACY.md).

---

## Install

```bash
# 1. Clone
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway

# 2. Run the installer
bash install.sh
```

The installer is fully interactive — everything happens in your SSH session, no browser needed.

**What it does, in order:**
1. Installs Docker + the NVIDIA driver + the NVIDIA container toolkit.
2. Reboots once (so the new NVIDIA kernel module loads). **You don't have to re-run anything** — a bash-profile hook auto-resumes on your next SSH login.
3. Brings up the full local-AI base: orchestrator + LLM (vLLM, model auto-picked to fit your hardware) + TTS (Qwen3-TTS with a generic voice) + STT (Parakeet) + dashboard. Waits for everything to report healthy.
4. Hands off to a 30-second CLI wizard (`scripts/setup.sh`) that asks two questions: **your name** and **your timezone**. Everything else (assistant name, ADHD mode, model, voice) takes sensible defaults you can change later.
5. Prints the dashboard URL **and an auto-generated `DASHBOARD_TOKEN` login password** when you're done. Save the password — it's the only time it's shown automatically (it lives in `.env` afterward).

Plan on **~30 minutes end-to-end** on a fresh box — mostly waiting on container images and model weights to download (~50 GB the first time). The wizard itself takes ~30 seconds.

**After install, talk to Jess.** Open the dashboard at `http://<your-box>:3001/`, log in with the printed `DASHBOARD_TOKEN`, and Jess greets you on your first message with a tour of what she can do and what's not yet configured (Home Assistant, ntfy/Pushover push, Paperless-ngx). She links you straight to the `/settings` page — the single configuration surface — where every optional integration has its own panel.

Step-by-step install guide with troubleshooting: [`docs/INSTALL.md`](docs/INSTALL.md).

**No NVIDIA GPU, or running on a Mac?** Don't run `install.sh` — use [`scripts/byo-setup.sh`](docs/BYO_MODEL.md) instead. It stands the assistant up CPU-only and points it at a model you provide (local on your Mac, another box, or a cloud API). Full guide: [`docs/BYO_MODEL.md`](docs/BYO_MODEL.md).

---

## What you can do with it

Once setup is complete, you talk to Brain Gateway like a normal assistant — from the web UI, from a voice puck, or via the API. A few of the things it does well:

- **Voice-first brain dump.** Mumble a stream of thoughts; it sorts them into reminders, tasks, and long-term memory.
- **Focus sessions.** Pomodoro sprints with ambient audio, optional Pi-hole site blocking, body-doubling check-ins.
- **Reminders that actually land.** Voice on the speakers, push to your phone via ntfy / Pushover, with one-tap Done/Snooze.
- **Routine scaffolding.** Morning and evening routines with TTS guidance and auto-skip when you've already done the step.
- **Decision simplifier.** "I can't decide between X and Y" → it gathers context and gives you 1–2 concrete recommendations.
- **Home Assistant control.** Natural language → lights / scenes / climate / media.
- **Personal RAG memory.** Drop markdown into your knowledge folder; it's searchable in conversation.

Full feature reference: [`docs/JESS_QUICK_START.md`](docs/JESS_QUICK_START.md).

---

## After install

| Task | Where |
|------|-------|
| Change any setting later | `/settings` page in the dashboard |
| Upgrade to a newer release | [`docs/UPGRADE.md`](docs/UPGRADE.md) |
| Enable advanced features (monitoring, multi-host, code agent) | Set `COMPOSE_PROFILES=advanced` and/or `JESS_ADVANCED=true` in `.env`, then `docker compose up -d` |
| Run the API directly | `POST /v1/chat/completions` with `Authorization: Bearer $API_TOKEN` (OpenAI-compatible) |
| Get a printable ADHD reference card | [`docs/JESS_REFERENCE_CARD.md`](docs/JESS_REFERENCE_CARD.md) |

---

## Documentation

| Doc | What |
|-----|------|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Full step-by-step install + troubleshooting |
| [`docs/BYO_MODEL.md`](docs/BYO_MODEL.md) | Run on your Mac / another box / a cloud model — no NVIDIA GPU needed |
| [`docs/TRY_IT_RENTED_GPU.md`](docs/TRY_IT_RENTED_GPU.md) | Spin up the full stack on a rented GPU (RunPod / vast.ai) in ~5 minutes |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | GPU/VRAM tier matrix + benchmark notes |
| [`docs/DEMO.md`](docs/DEMO.md) | Demo-GIF capture script (for contributors) |
| [`docs/UPGRADE.md`](docs/UPGRADE.md) | Upgrading between releases |
| [`docs/JESS_QUICK_START.md`](docs/JESS_QUICK_START.md) | Everything you can say to it |
| [`docs/ENV_VARS.md`](docs/ENV_VARS.md) | Every environment variable, what it does |
| [`docs/DEV.md`](docs/DEV.md) | Developer setup, architecture, contributing |
| [`docs/PRIVACY.md`](docs/PRIVACY.md) | What data is handled, where it lives, what can leave the box |
| [`CHANGELOG.md`](CHANGELOG.md) | Release notes |

For developer-facing internals (architecture, tools, agent pipeline), see [`CLAUDE.md`](CLAUDE.md) at the repo root. It is written for AI coding assistants but doubles as a contributor's map of the codebase.

---

## License

MIT — see [LICENSE](LICENSE).

Copyright © 2026 Nadim Nabi. Brain Gateway is provided as-is, with no warranty. If you ship it as part of a commercial product, please keep the MIT notice.
</content>
</invoke>
