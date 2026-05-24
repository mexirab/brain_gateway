# Changelog

All notable changes to Brain Gateway are documented in this file. The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0] — first public release (May 2026)

Brain Gateway is now a self-contained single-box appliance with a browser-based setup wizard. The default install is small, hardware-aware, and free of personal references to the maintainer's deployment.

### Added

- **Browser setup wizard** at `/setup` — 7 steps (Welcome / Identity / Model / Voice / Push / Integrations / Selfcare / Review). Writes a `chmod 600` `setup_overrides.env` overlay that `config.py` loads before `Settings()`. Idempotent kill switch: every write endpoint returns HTTP 410 after `setup_completed: true`.
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

### Security

- Setup-wizard URL validator hardened across two hacker review rounds (PRs #20 + #21).
- Wake-word recordings directory (`data/wakeword/`, 41 GiB of personal voice data) added to `.gitignore`.
- `chmod 600` on the wizard's `setup_overrides.env` overlay; secret keys never echo `value` on read-back.

### Privacy

- **No telemetry.** Brain Gateway never phones home. The only outbound network traffic is what you explicitly enable (e.g. Google Calendar, ntfy push, SearXNG web search). Full disclosure: `docs/PRIVACY.md`.

---

## Older history

Pre-1.0.0 development happened on `main` without formal version tags. The full commit history is on GitHub. The productization plan that converted the maintainer's 4-node cluster into a single-box appliance is documented in `plans/ship-jess-as-product.md` (not part of the shipped tree).
