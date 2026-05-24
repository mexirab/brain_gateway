# Brain Gateway

A self-hosted, voice-first personal assistant tuned for ADHD support.

One model, one box, one HTTPS URL. Talk to it from your phone, your laptop, or any Home Assistant voice puck. Set reminders, run focus sprints, dump your brain at 2 AM, get nudged through morning routines, and ask it anything — all without your data leaving your network.

> **Status:** v1.0.0 release candidate (May 2026). Single-box install with a browser-based setup wizard. The default build runs the conversation model + voice (TTS/STT) + reminders + focus timer + Home Assistant control. Optional integrations (Google Calendar, Gmail, ntfy/Pushover push, Paperless-ngx, monitoring stack) get wired up through the wizard.

---

## Hardware requirements

Brain Gateway runs the LLM, TTS, and STT models locally on a single NVIDIA GPU. The setup wizard picks a model based on what it finds.

| GPU VRAM | Tier | Default model | Notes |
|----------|------|---------------|-------|
| **< 20 GiB** | below floor | pick a 7–8B AWQ yourself | Boots but you'll need to set `VLLM_MODEL` manually. Quality degrades. |
| **20–29 GiB** | 24 GiB | `Qwen/Qwen3-14B-Instruct-AWQ` | RTX 3090 / 4090 / 5070 Ti class. Good general performance. |
| **30–43 GiB** | 32 GiB | `Lorbus/Qwen3.6-27B-int4-AutoRound` | RTX 5090 class. Recommended sweet spot. |
| **44+ GiB** | 48 GiB | `Lorbus/Qwen3.6-27B-int4-AutoRound` | RTX PRO 5000 / A6000 class. Vision-capable; can run a second VL model. |

Other requirements:
- **OS:** Ubuntu 22.04 or 24.04 (other distros work but driver/DKMS dance is on you)
- **NVIDIA driver:** 580+ (required for Blackwell + vLLM 0.19)
- **RAM:** 16 GiB minimum, 32 GiB recommended
- **Disk:** ~120 GiB free (model weights + HF cache + container images)
- **Network:** any LAN; Tailscale recommended for off-LAN HTTPS access

Full compatibility matrix: see [`docs/HARDWARE.md`](docs/HARDWARE.md).

---

## Install

```bash
# 1. Clone
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway

# 2. Run the installer
bash install.sh
```

The installer handles Docker, the NVIDIA driver, the NVIDIA container toolkit, the orchestrator stack, and prints the wizard URL when it's done. A reboot is required midway (to load the new NVIDIA kernel module); the script tells you exactly when and asks you to re-run it after the box comes back. Plan on ~20 minutes end-to-end, mostly waiting on apt and container image pulls.

When the installer finishes, it prints a URL like `http://<your-box-ip>:3001/setup`. Open that from any browser on your LAN to walk the setup wizard:
- **Identity** — your name, timezone, ADHD mode
- **Model** — confirm the recommended model from your hardware scan
- **Voice** — pick a TTS voice
- **Push channels** — ntfy + Pushover (both optional)
- **Optional integrations** — Home Assistant, Paperless-ngx, etc.
- **Selfcare nudges** — meal / water / med / movement reminders
- **Review + launch**

![Setup wizard — TODO: add screenshot](docs/img/setup-wizard.png)

Step-by-step install guide with troubleshooting: [`docs/INSTALL.md`](docs/INSTALL.md).

---

## What you can do with it

Once the wizard finishes, you talk to Brain Gateway like a normal assistant — from the web UI, from a voice puck, or via the API. A few of the things it does well:

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

## Privacy

Brain Gateway runs entirely on your hardware. There is no telemetry — no usage stats, no crash reports, no phone-home. The only outbound network traffic is what *you* explicitly enable (e.g. Google Calendar, ntfy push, web search through SearXNG). Your conversation history, RAG knowledge, and reminders never leave the box.

Full disclosure of what data is handled, where it lives, and what can leave the box: [`docs/PRIVACY.md`](docs/PRIVACY.md).

---

## Documentation

| Doc | What |
|-----|------|
| [`docs/INSTALL.md`](docs/INSTALL.md) | Full step-by-step install + troubleshooting |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | GPU/VRAM tier matrix + benchmark notes |
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
