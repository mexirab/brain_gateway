# Changelog

All notable changes to Brain Gateway are documented in this file. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] — reliability, backups & the Home Assistant migration (2026-07-04)

Maintainer-deployment work: a reliability/backup pass, the June-12 latency branches rebased in, and Home Assistant moved off a failed Raspberry Pi onto the always-on server.

### Added

- **Telegram bot — away-from-home capture** (`orchestrator/telegram_bot.py`). A long-polling background task (outbound HTTPS only — no webhook, no public ingress) that gives you full two-way Jess from anywhere: inbound text relays through `/v1/chat/completions` (mode router, fast-path, tools — task capture, brain dumps, calendar questions), and reminders arrive as Telegram messages with inline **Done / Snooze** buttons handled with the same state-machine semantics as the F-011 ack/snooze routes (retry-job cancellation, selfcare bridge, snooze cap). Locked to `TELEGRAM_ALLOWED_CHAT_ID`; unknown chats are dropped with the ID logged (rate-limited) for first-time setup. Plain-text replies (no parse_mode) so LLM output can't fail Telegram's Markdown parser. RAM-only rolling history per chat, `/new` resets. Default-OFF (`TELEGRAM_ENABLED`); auto-disabled on a missing token. New metrics: `bgw_telegram_send_total`, `bgw_telegram_send_latency_seconds`, `bgw_telegram_update_total`, `bgw_telegram_callback_total`. Docs: `docs/ENV_VARS.md` → Telegram Bot.
- **Reminder trust layer** — the PR #32 delivery state machine, made visible. (1) **Morning recap**: the briefing now owns up to reminders that went `missed`/`failed` in the last 24h ("Heads up: 2 reminders didn't reach you…"), naming up to three; when the Telegram bot is enabled the same recap is mirrored there so it's actionable away from the speakers (`telegram_bot.send_system_message`). (2) **Dashboard delivery log**: `GET /api/reminders` gains a `recent` section (last-24h terminal-state reminders via `state_store.get_recent_reminder_outcomes`), and the RemindersCard renders it — ✓ delivered (with "Done via telegram/ntfy" when acked), ⚠ missed, ✕ failed, problems sorted first, with a red "N not delivered" header badge. Also fixes the pending rows' `time` field (was `trigger_time`-only, so the card's time column rendered "Invalid Date"). (3) **Grafana "Reminder Delivery — Trust" row** on the Brain Gateway dashboard: delivery outcomes /day, failed/missed 7-day stats, ack latency p50/p95, per-channel push OK/failure rates (ntfy / Pushover / Telegram), and per-speaker TTS success — a failing speaker group is now visible before anyone notices missing audio.

### Infrastructure

- **Home Assistant migrated off the dead Pi to Jupiter.** The Pi at `10.0.0.106` running HA suffered SD-card failure (booted to an emergency console). HA now runs as a docker container on Jupiter (host-networked, `:8123`), pinned to `2026.5.1`, managed from the new `homeassistant/` compose project. `HA_URL` changed `http://10.0.0.106:8123` → `http://10.0.0.248:8123`. All devices are network-based (ESPHome/Cast/Bluetooth-proxies/cloud — no USB radios) so the migration was a config copy. Runbook: `docs/HA.md`.
- **Nightly off-box backups to Saturn.** `scripts/backup_state.py` (cron 03:30) snapshots orchestrator state consistently — the SQLite DBs + `auto_learn.key` (the Fernet key that decrypts learned facts) + chroma + credentials, excluding the reconstructable `hf_cache` — and rsyncs to Saturn. `homeassistant/backup_ha.sh` (cron 03:45) does the same for the HA config. Prometheus alerts `JessBackupStale` / `JessHABackupStale` fire if a nightly is missed. Docs: `docs/BACKUP.md`, `docs/HA.md`. (Closes the audit's biggest gap: `data/` had no backup, and losing `auto_learn.key` permanently bricked encrypted memories.)

### Fixed

- **Reminder-delivery state machine** (PR #32) — four silent-failure modes fixed: snooze no longer permanently kills a reminder; reminders due during downtime are late-delivered or marked `missed` instead of silently dropped; DND / active-voice-session suppression no longer counts as delivery; the TTS retry is now finite and doesn't spam phone pushes. New `bgw_reminders_failed_total` / `bgw_reminders_missed_total`.
- **Audit quick-wins** (PR #33) — Helios status-poll log spam (was ~1 error/min while asleep) reduced to state-transition logging; routines settings-save no longer kills an active routine's nudges; medication YAML now written atomically (a crash no longer silently drops all med nudges); email-to-calendar dedup window sized to the event date (was `days_ahead=1`, duplicating far-out events); Anthropic/OpenAI backends accept `extra_body` (the BYO/cloud brain-asleep path was `TypeError`-ing); the dead finance + closet-temperature scheduled jobs are now registered (gated on config).
- **`code_agent` shell hardening** (PR #34) — `run_command` now tokenizes with `shlex` and runs an argv-token allowlist **without a shell** (was string-prefix + `shell=True`, bypassable by prompt injection to read `/app/.env`).

### Performance

- **June-12 latency branches rebased onto main** (PR #36) — `rag_context` is now async (embedding + Chroma query off the shared event loop, ~100-400ms/request; also fixes the `decide_for_me` food-path `TypeError`); `_announce_voice` casts to all speakers concurrently via the shared pooled HTTP client; per-round tool-list assembly is cached (respecting all feature-flag gates).

### Housekeeping

- Home Assistant setup version-controlled in `homeassistant/` + `docs/HA.md` (PR #37).

---

## [1.0.0] — first public release (May 2026)

Brain Gateway is now a self-contained single-box appliance. One command (`bash install.sh`) brings up Docker + NVIDIA driver + the full local-AI base (LLM + TTS + STT + dashboard) + a 2-question CLI wizard. Hardware-aware (auto-picks a model that fits your GPU), free of personal references to the maintainer's deployment.

### Added

- **Dream install** — `bash install.sh` brings up the FULL local-AI base on first run (`COMPOSE_PROFILES=models`): orchestrator + vLLM + qwen-tts + parakeet-stt + dashboard. Auto-substitutes `VLLM_MODEL=Qwen/Qwen3-8B-AWQ` + `VLLM_EXTRA_ARGS=--tool-call-parser hermes` + `VLLM_MAX_MODEL_LEN=16384` on below-floor (<20 GiB) GPUs. Auto-writes `API_TOKEN`, `DASHBOARD_TOKEN`, `JESS_LAN_IP`, `GATEWAY_ROOT_PATH`. Hands off to a 2-question CLI wizard (name + timezone) — everything else takes auto-defaults (`assistant_name=Jess`, `adhd_mode=true`, `tone=warm`, `TTS_VOICE=aiden`). Ends with `docker compose up -d --force-recreate orchestrator` (not `restart` — env-file changes need recreate) and prints the dashboard URL + login password.
- **First-chat welcome** (`orchestrator/welcome.py`) — one-time markdown tour prepended to the assistant's first reply, listing what's working + un-configured integrations + a clickable `/settings` link. Defangs markdown injection in operator-set identity fields. Skips on fast-path + voice. Metric: `bgw_welcome_fired_total{result}`.
- **Setup wizard backend** (`/api/setup/*`) — `status`, `hardware`, `complete`, `env`, `env/validate`. Writes a `chmod 600` `setup_overrides.env` overlay that `config.py` loads before `Settings()`. Idempotent kill switch: every write/validate endpoint returns HTTP 410 after `setup_completed: true`. (The matching web `/setup` UI was prototyped through 7 wizard slices and then deleted in favor of the express CLI flow.)
- **Hardware-aware model recommendation.** `scripts/detect_hardware.sh` reads `nvidia-smi`, classifies the largest GPU into a tier (24 / 32 / 48 GiB), and emits a `KEY=value` block ready to append to `.env`. A `--json` mode writes a structured scan consumed by `GET /api/setup/hardware`.
- **Containerized model layer.** `vllm-primary`, `qwen-tts`, and `parakeet-stt` have full `docker-compose.yml` stanzas behind a `models` profile. Fresh single-box installs bring them up with `COMPOSE_PROFILES=models`. (Maintainer's reference Helios deployment continues to run them as host systemd units.)
- **Default + advanced profiles** in `docker-compose.yml`. `nebula-sync`, `promtail`, and `nut-exporter` are now gated behind `COMPOSE_PROFILES=advanced`; the default install brings up only the core stack.
- **`JESS_ADVANCED` gate** for owner-specific tools (`code_agent`, `ask_expert`, `query_budget`, `finance_status`, `check_claude_activity`) and background jobs (self-audit, training corpus drain). Default `false`.
- **MIT LICENSE** at repo root.
- **End-user docs**: rewritten `README.md` (hardware reqs + 5-minute install), new `docs/INSTALL.md`, `docs/HARDWARE.md`, `docs/UPGRADE.md`, `CHANGELOG.md`, `docs/DEV.md`.
- **Settings page** at `/settings` with 6 panels (Identity & Tone, Selfcare Nudges, Quiet Hours, Routines, Speakers, Recurring Reminders) — `routes_config.py` + `frontend/src/app/(private)/settings/`. Every write atomic + diff-logged via `config_writer.atomic_write_yaml()` + `log_config_change()`.
- **F-011 Ntfy feedback loop** — reminders delivered to ntfy with Done / Snooze action buttons; HMAC-signed callback URLs (`/api/reminder/ack/{id}`, `/api/reminder/snooze/{id}`). Bearer-exempt + injection-safe.
- **F-012 Paperless-ngx bridge** — `paperless_save` tool + `POST /api/paperless/upload` (100 MB cap). Path-traversal + symlink-escape guards.
- **F-013 Pushover bridge** — parallel iOS push channel alongside F-011, reuses HMAC routes, HTML-escapes reminder text to block prompt-injected `<a href>` from landing on the lockscreen.
- **F-014 Daily self-audit** — 7am UTC Loki scan + Jess diagnosis + Pushover digest + markdown report. Read-only safety story (allow-list + dangerous-pattern + secret-pattern filters). Default-OFF; needs both `SELF_AUDIT_ENABLED=true` and `JESS_ADVANCED=true`.
- **Adaptive workout generator** — recency-aware split logic; `generate_workout`, `log_set`, `workout_status`, `modify_workout` tools.
- **Calorie-only meal logging** — `log_meal` tool with optional vision-model calorie estimation via Qwen3-VL-8B.
- **vLLM Phase 3 cutover** — primary LLM is now `Lorbus/Qwen3.6-27B-int4-AutoRound` served by vLLM 0.19.1 (replaces the llama.cpp `Qwen3.5-27B` from earlier builds).
- **STT swap** — Parakeet TDT v3 replaces Whisper-medium on port 8003 (~10× faster, lower WER, OpenAI-compatible API unchanged).
- **UPS power-chain visibility** — NUT exporter + Grafana dashboard + alerts. Advanced-profile only.

### Changed

- **De-personalization** — every owner-specific literal that snuck into the codebase (`Nadim` in `presence_tracker.py`, hardcoded `10.0.0.*` LAN IPs, Helios-only Tailscale FQDNs, the `jessica` TTS voice ID as a default) is now either configurable via env or routed through the wizard. `TTS_VOICE` default changed from `"jessica"` → `"default"`; live Helios deployment keeps `TTS_VOICE=jessica` as an `.env` override.
- **`POST /api/setup/env/validate`** — now subject to the same first-boot kill switch as the write endpoints (closes a hacker-discovered SSRF + port-scan oracle).
- **URL validation** — `setup_env._validate_url` rejects URL fragments, queries, params, trailing `?`, out-of-range ports, control characters, leading/trailing whitespace, and userinfo. Five distinct path-append silently-breaks bug classes covered.
- **README** — was a dev-oriented stack overview; now an end-user install guide.
- **CLAUDE.md** — now carries an "AI assistants only" header at the top; remains the canonical briefing for Claude Code + Cursor + similar tools.
- **Doc layout** — maintainer's Helios runbook moved to `docs/internal/HELIOS_INFRASTRUCTURE.md`; historical migration plan moved to `docs/internal/VLLM_PHASE_3_PLAN.md`.

### Removed

- `docs/REMOTE_DEV.md` — personal mosh+tmux workflow doc, not portable.
- Hardcoded TTS-voice branding from `tts/server.py`, `tts/wyoming_jessica_bridge.py`, `tts/README.md`.
- Helios pi-hole + nginx model-server stanzas from `docker-compose.yml` (2026-04-26).
- 7-day audit calibration review job (date-stamped, already past).
- Default `NODE_*_IP` fallbacks in `nebula-sync` config.
- **Web setup wizard** (`frontend/src/app/setup/*` + `frontend/src/components/setup/*` + `frontend/src/lib/setup-api.ts`) — replaced by the 2-question express CLI wizard at `scripts/setup.sh`. The `/api/setup/*` backend endpoints are unchanged and now consumed by the CLI over localhost.
- **Short-lived in-chat configure_* tools** (`configure_home_assistant`, `configure_ntfy`, `configure_pushover`, `configure_paperless`) added in d9ce730 and removed in 6ca7d21 — never reached v1.0.0. Credential prompts via chat were a prompt-injection risk (hacker review found exfiltration paths); `/settings` is the supported post-install configuration surface.

### Security

- Setup-wizard URL validator hardened across two hacker review rounds (PRs #20 + #21).
- Wake-word recordings directory (`data/wakeword/`, 41 GiB of personal voice data) added to `.gitignore`.
- `chmod 600` on the wizard's `setup_overrides.env` overlay; secret keys never echo `value` on read-back.

### Privacy

- **No telemetry.** Brain Gateway never phones home. The only outbound network traffic is what you explicitly enable (e.g. Google Calendar, ntfy push, SearXNG web search). Full disclosure: `docs/PRIVACY.md`.

---

## Older history

Pre-1.0.0 development happened on `main` without formal version tags. The full commit history is on GitHub. The productization plan that converted the maintainer's 4-node cluster into a single-box appliance is documented in `plans/ship-jess-as-product.md` (not part of the shipped tree).
