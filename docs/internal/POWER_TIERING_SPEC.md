# Power-Tiered Architecture — Spec / Plan

> Status: DRAFT (2026-06-14) · Owner: Nadim · Author: planning session
> Supersedes the "Helios is always-on" assumption baked into README/CLAUDE.md.

## 1. Problem

The README and CLAUDE.md say **"Helios is always-on (no auto-shutdown)."** In reality
Helios (the RTX 5090 GPU box) is **powered off most of the time to save electricity.**
But the entire stack — not just the LLM — runs on Helios:

- The **orchestrator** (CPU-only FastAPI app) runs as the Helios docker-compose stack.
- The **model layer** (vLLM, Qwen-TTS, Parakeet-STT) runs as Helios host systemd units.

So when Helios sleeps, you don't just lose conversation — you lose **reminders,
calendar alerts, med nudges, morning briefings, selfcare nudges, HA control, and
TTS announcements**, none of which need a GPU. This already caused a **silent ~2-month
outage** that presented as a Google Calendar token problem but was actually the whole
orchestrator being down (the token only refreshed while the orchestrator was alive).

**The orchestrator is already CPU-only** (confirmed: it can't even run hardware
detection — `hardware_scan.json` is produced host-side by `scripts/detect_hardware.sh`).
There is no technical reason it must live on the GPU box.

## 2. Goal

Split **"the brain" (GPU, intermittent)** from **"the nervous system" (CPU, 24/7)** —
**staying 100% local** (no cloud LLM; privacy/sovereignty is a hard principle for a
personal assistant holding memory-palace, finance, and health data):

- Move the orchestrator to **Jupiter** (10.0.0.248 — always-on, already runs Pi-hole +
  Prometheus/Grafana/Loki). Reminders/calendar/selfcare/HA/push stay alive 24/7 with
  **no LLM involved** (see §3.1 — the nervous system is templated, not generated).
- When Helios is down, **LLM-dependent features degrade gracefully** (conversation, voice,
  email-to-calendar, self-audit) and return when Helios wakes. The deterministic nervous
  system never stops. **No cloud fallback.**
- Wake Helios **on demand via WoL** for conversation + GPU work, then let it sleep.

**Decisions locked (2026-06-14):** all-local (no cloud fallback) · orchestrator
**Jupiter-only** · wake via **WoL** (wait for the BIOS fix; no scheduled-window stopgap).

**Hard constraint:** preserve the one-command installer. The single-box decision was made
*because* of the investment in `install.sh` (2-stage, idempotent, auto-resume across
reboot). The split must make the installer **role-aware**, not fork it into two
hand-maintained install paths.

> **Honest consequence of the locked decisions:** until WoL is fixed (needs physical access
> to Helios's BIOS), whenever Helios is off there is **no way to *converse* with Jessica** —
> she's reduced to the (still very valuable) deterministic nervous system. You're remote now,
> so remote *conversation* stays blocked until either (a) you're back at Helios to fix WoL, or
> (b) you opt into a small **local** CPU fallback model on Jupiter (§6) — the all-local
> substitute for the cloud option you declined.

## 3. Current-state facts (grounded in the code)

| Thing | Where | Notes |
|---|---|---|
| Orchestrator | `docker-compose.yml` service `orchestrator`, CPU-only | FastAPI v7.0, `orchestrator/orchestrator.py` |
| Model layer | Helios host systemd units | `vllm-primary.service` (8080), `qwen-tts.service` (8002), `parakeet-stt.service` (8003). NOT containers on Helios. |
| Fallback wiring | `orchestrator.py:496-510` | `FALLBACK_MODEL_URL` + `FALLBACK_MODEL_NAME` → `CloudBrain(...)`; health-checked in `service_registry.py:77`. Defaults to **another local model** today (`qwen3.6-27b-int4`). |
| Primary model URL | `config.py:49` `MODEL_URL` (default `http://localhost:8080/v1`); compose default `http://vllm-primary:8000/v1` | |
| Orchestrator state (all host bind-mounts under `GATEWAY_ROOT_PATH`) | `./data/chroma` (ChromaDB / memory palace), `./data/app/*.db` (brain_state.db, progress.db, finance.db, reminders, focus, …), `./data/hf_cache` (embedding model cache), `./credentials` (Google OAuth), `user_profile.yaml`, `data/routines.yaml`, `data/palace.yaml`, `data/selfcare_schedule.yaml` | Migration = rsync these to Jupiter. Bounded, well-contained. |
| Installer | `install.sh` (root, 2-stage) | **GPU-mandatory**: `check_gpu()` (L67-82) `die`s with no NVIDIA card; Stage 1 installs nvidia-driver-580 + container toolkit. **Cannot run on Jupiter as written.** |
| Service selection | `COMPOSE_PROFILES` in `.env` | `models` = LLM+TTS+STT; `advanced` = nebula-sync/promtail/nut-exporter; default = orchestrator+frontend+redis+searxng+open-webui |
| Power control today | `scripts/start-helios.sh`, `scripts/stop-helios.sh`, `scripts/helios-status.sh` | SSH-based manual start/stop already exists. |
| Wake-on-LAN | **gone** | Zero WoL/etherwake remnants in tree. Old SSH auto-start/stop config existed; WoL proven non-functional (likely BIOS ErP or NIC not persisting `wol g`). Needs physical/BIOS access to fix. |
| Tailscale | `helios.tail74fc4a.ts.net`, `jupiter-amds.tail74fc4a.ts.net` | Both nodes on the tailnet — basis for remote access + cross-node model URL. |

### 3.1 What actually depends on the LLM (verified in code)

The premise of the split is that the 24/7 functions don't need a GPU. Confirmed by grep:

| Job path | LLM call? | Behavior with Helios off |
|---|---|---|
| `reminder_manager` (reminders, DND, push) | **no** | Fires. TTS *audio* needs Helios, but push (ntfy/Pushover, F-011/F-013) is CPU-only and works. |
| `selfcare_manager` (med/meal/water/movement nudges) | **no** | Fires (templated). |
| `progress_tracker` (streaks, daily/weekly summaries) | **no** | Fires (templated). |
| `recurring_reminders` (cron → one-shot expansion) | **no** | Fires. |
| `jobs_calendar.morning_briefing` (L353) | **no** | Assembles calendar + meds + data; no generation. Fires. |
| `jobs_calendar` email-to-calendar extract (L547) | **yes** | Already graceful: `try/except → log + continue` (L554-556). Skips while Helios off. |
| Live chat / unified loop, TTS voice, self-audit (F-014) | **yes** | Unavailable while Helios off; return on wake. |

**Conclusion:** moving the orchestrator to Jupiter genuinely *fixes* the silent-outage
class — the deterministic nervous system runs with no LLM at all. Only conversation +
voice + email-to-cal + self-audit are GPU-gated, and all degrade gracefully.

## 4. Target architecture

```
                       ┌─────────────────────────────────────────┐
   phone / LAN / TS ──►│ JUPITER (always-on, no GPU)              │
                       │  orchestrator (compose, CPU)             │
                       │  frontend, redis, searxng, open-webui    │
                       │  Prometheus / Grafana / Loki (existing)  │
                       │  24/7 nervous system: reminders, calendar,│
                       │    selfcare, push — NO LLM needed         │
                       │  MODEL_URL ───────────────┐              │
                       └───────────────────────────┼──────────────┘
                                                   │ (when awake; WoL)
                       ┌───────────────────────────▼──────────────┐
                       │ HELIOS (GPU, intermittent)                │
                       │  vllm-primary (8080) systemd              │
                       │  qwen-tts (8002), parakeet-stt (8003)     │
                       │  woken on-demand via WoL                  │
                       └───────────────────────────────────────────┘
```

- **One orchestrator, on Jupiter only.** No split-brain: SQLite + ChromaDB can't be
  written by two orchestrators. Helios stops running the compose stack entirely after
  cutover; it becomes pure model layer.
- `MODEL_URL` → Helios over the tailnet/LAN (`http://10.0.0.195:8080/v1`).
- **No cloud fallback.** When the primary health check fails (Helios asleep), LLM features
  return a clear "brain is asleep — waking Helios / try again shortly" state rather than a
  cloud answer. The nervous-system jobs keep firing regardless (§3.1).
- TTS/STT: when Helios is down there's no local voice. Announcements degrade to
  **push-only** (ntfy/Pushover — already CPU-only on Jupiter). Voice returns on wake.
- **Optional local fallback (§6):** a small CPU model on Jupiter can serve degraded
  conversation while Helios sleeps — all-local, no cloud. Off by default; opt-in.

## 5. Installer: role-aware, not forked

Add a **node role** to `install.sh` (env `JESS_NODE_ROLE` or `--role`, default `full`):

| Role | GPU stage (Stage 1) | `COMPOSE_PROFILES` | MODEL_URL / FALLBACK | Use |
|---|---|---|---|---|
| `full` (default) | yes (unchanged) | `models` | local / local | Fresh single-box install — **current behavior, untouched** |
| `nerves` | **skipped** (no driver, no `check_gpu`) | default (no `models`) | remote Helios / **cloud** | Jupiter — the CPU orchestrator |
| `brain` | yes | `models` only | n/a | A GPU box that serves only the model layer |

Implementation sketch:
- Gate `check_gpu` + Stage 1 driver/toolkit install behind `role != nerves`.
- For `nerves`, Stage 2 skips the `--gpus all` Docker smoke test and the
  vllm/tts/stt health-waits; only waits on `orchestrator`.
- `detect_hardware.sh` for `nerves` records "no GPU; remote model layer" instead of dying.
- Everything else (`.env` synthesis, token generation, `setup.sh` wizard) is reused as-is.

Net: **still `bash install.sh`** on either box. The installer learns one flag; the
2-stage/auto-resume machinery and the wizard are shared. This is the design that honors
the single-installer investment.

### 5.1 Release relationship — `full` IS the shipped product

The role split is **additive**, not a fork of the product story. The public release is
exactly today's single-box experience; power-tiering is an opt-in advanced topology.

| Who | Command | Result |
|---|---|---|
| **Public / anyone (the release)** | `bash install.sh` | Single GPU box, all-local, zero config — **today's behavior, the shipped default** |
| Power-saver / multi-node | `--role nerves` on an always-on box + `--role brain` on the GPU box | CPU nervous system 24/7, GPU brain woken on demand |

**Design rules this imposes on Phase A (non-negotiable):**
- `full` is the **zero-argument default** and must stay **release-clean**: someone who
  never heard of "Jupiter" or "Helios" must never see a multi-node prompt, env var, or
  doc path on the common install path.
- Role is opt-in only (`--role` flag / `JESS_NODE_ROLE` env). Absent → `full`.
- No homelab-specific IPs/hostnames in shipped defaults (already the posture per the
  v1.0.0 portable `.env.example` work — keep it).
- The split becomes a **documented feature** ("power-efficient two-node mode"), not
  internal plumbing — it generalizes to any self-hoster who doesn't want the GPU on 24/7.

## 6. Fallback — local-only, optional

**Decision: no cloud fallback.** The existing `FALLBACK_MODEL_URL`/`FALLBACK_MODEL_NAME`
plumbing (authored for another *local* OpenAI-compatible server) is kept exactly as-is —
no API-key work, no cloud provider.

**Default behavior with Helios off:** LLM features surface a clear "brain asleep" state;
the nervous system keeps running (§3.1). This is the simplest all-local posture.

**Opt-in local fallback (deferred decision — see §10.4):** run a **small CPU model on
Jupiter** (e.g. a 3–4B Qwen via llama.cpp, CPU-only, a few tok/s) and point
`FALLBACK_MODEL_URL` at it. This is the *all-local* answer to "stay conversational when
Helios sleeps" — it trades quality/speed for keeping a basic Jessica reachable (and is the
only way to get **remote conversation before WoL is fixed**). Caveats: a 3–4B model on CPU
is weak at the tool-heavy unified loop, so treat it as degraded chat + simple commands, not
full agentic. Off by default; revisit after Phase B.

## 7. Dead-man's-switch alerting (rides along)

Once the orchestrator is on Jupiter next to Prometheus, wire alert rules (ntfy/Pushover
already in place):
- Orchestrator `/health` down → phone push.
- Morning briefing hasn't fired by 07:15 → push.
- Google token refresh failed → push.
- **Jupiter cron** that refreshes the Google OAuth token weekly, independent of the
  orchestrator (the root cause of the silent 2-month outage — token only refreshed when
  the orchestrator was alive).

## 8. Wake strategy — WoL (decided)

**Decision: wait for WoL. No scheduled-window stopgap.**

- **Until WoL is fixed:** Helios is started **manually** when GPU/conversation is wanted
  (existing `scripts/start-helios.sh` over SSH/Tailscale). The nervous system runs 24/7
  regardless, so "manual start" only gates conversation + voice, not reminders/nudges.
- **WoL fix (needs physical/BIOS access to Helios):** BIOS ErP off + persist `wol g` on
  the NIC (`ethtool`, made durable across reboots). Then the orchestrator on Jupiter sends
  a WoL magic packet on conversation/heavy-task demand, waits for `vllm-primary` health,
  serves, and auto-shuts Helios after idle — a revival of the old SSH auto-start/stop
  config. This is the path to **remote, on-demand conversation** with Helios normally off.

## 9. Phased rollout

| Phase | Needs | Work | Risk |
|---|---|---|---|
| **A — repo prep (doable now, offline)** | nothing | Role-aware `install.sh` (`nerves` role: skip GPU stage + `check_gpu`, models profile off); "brain asleep" graceful-degradation state for LLM features; Jupiter token-refresh cron + dead-man's-switch alert rules as files; `on_event`→`lifespan` freebie | low — all in-repo, committed, deploys later |
| **B — cutover (needs Jupiter reachable)** | Jupiter SSH | rsync `./data` + `./credentials` + configs to Jupiter; `install.sh --role nerves`; point `MODEL_URL` at Helios; stop Helios compose stack (Helios → model layer only); verify reminders/calendar/briefing/push fire with Helios **OFF** | med — data migration + URL cutover |
| **C — wake-on-demand (needs Helios physically)** | BIOS access | WoL fix (ErP off + durable `wol g`); orchestrator sends WoL on demand + idle shutdown; unlocks remote conversation | low — but gates remote chat until done |

**Recommended first commit:** Phase A. Lands entirely in the repo while Jupiter is
unreachable and converts "Helios off = assistant dead" into "Helios off = nervous system
runs, brain sleeps."

### Phase A — implemented 2026-06-14 (pending home test)
- **Role-aware installer** ✅ `install.sh --role nerves`; `full` stays release-clean (§5).
- **Graceful "brain asleep" state** ✅ `cloud_brain._brain_asleep_response()` returns a
  friendly HTTP-200 OpenAI-shaped reply when the model is unreachable (no model + no
  fallback, and fallback-also-failed paths) instead of a bare 503; error metrics still inc.
- **Dead-man's-switch alerting** ✅ `scripts/refresh_google_token.py` (standalone weekly
  refresh, works when orchestrator is down; writes node_exporter textfile metric);
  `bgw_morning_briefing_last_run_timestamp_seconds` gauge (+ startup baseline); alerts
  `MorningBriefingStale` (>26h, guarded) + `GoogleTokenRefreshStale` (>8d) in alert-rules.yml.
- **`on_event` → `lifespan`** ✅ `orchestrator.py` uses an asynccontextmanager lifespan
  delegating to `_startup_logic`/`_shutdown_logic` (bodies unchanged).

Wire-up TODO at home: weekly cron + `GOOGLE_TOKEN_METRICS_PATH` → node_exporter textfile
dir on the always-on host; run the CLAUDE.md review pipeline on the Python diffs live.

## 10. Decisions

1. ~~Cloud fallback provider~~ → **RESOLVED: no cloud. All-local.** (§6)
2. **Cutover boldness** → **RESOLVED: orchestrator Jupiter-only** (no split-brain). (§4)
3. **Wake policy** → **RESOLVED: WoL, wait for the BIOS fix** (no scheduled-window stopgap). (§8)
4. **OPEN — local CPU fallback model on Jupiter?** Whether to run a small 3–4B CPU model on
   Jupiter for degraded all-local conversation while Helios sleeps (the only path to remote
   chat before WoL). Recommend: **defer**, decide after Phase B once the nervous-system win
   is proven and you can feel how often "Helios is asleep but I want to talk" actually bites.
5. **OPEN — voice-down behavior.** Confirm push-only (ntfy/Pushover) is acceptable when
   Helios is asleep, or whether a tiny always-on CPU TTS on Jupiter is wanted later.
```
