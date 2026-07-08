# Brain Gateway Roadmap

*Rewritten 2026-07-05. Everything shipped before this date lives in [CHANGELOG.md](CHANGELOG.md) — this file is forward-looking only.*

Personal ADHD brain — voice-first, proactive, low-friction.

The guiding principle: **if it requires opening an app, I won't do it.** Everything should be capturable by voice ("Hey Jess, ...") or happen automatically in the background.

## Where things stand (July 2026)

- **v1.0.0 public release** shipped May 2026: one-command install, CLI setup wizard, containerized model layer, de-personalized codebase, MIT license. The 14-feature ADHD suite (F-001–F-014), vLLM migration, finance/workout/meals dashboards, and settings page are all done.
- **July 2026 reliability push** (PRs #32–#43): reminder-delivery state machine fixed, code-agent shell hardened, nightly off-box backups to Saturn, June perf branches rebased in, HA migrated off the dead Pi onto Jupiter, monitoring/Homepage config folded into the repo, deploy race fixed, Grafana consolidated to one app dashboard.
- **Durable task backlog** shipped July 2026 (PRs #44/#45/#46): `tasks` table + `backlog_manager` + voice tools (`add_task`, `what_now`, …) + `/tasks` page + dashboard TasksCard + brain-dump capture routing + `decompose_task` auto-linking + weekly Sunday review + Grafana row. The missing spine now exists.

The list below is ordered by tier, and within each tier roughly by priority.

---

## Tier 1 — Core gaps (conspicuously missing, not polish)

### 1. ~~Away-from-home capture: Telegram bot~~ ✅ BUILT (July 2026)

Shipped as `orchestrator/telegram_bot.py`: long-polling (no webhook / public ingress), locked to the allow-listed chat ID, inbound text through `/v1/chat/completions` (full Jess + tools from anywhere), reminders with inline **Done / Snooze** buttons handled with the F-011 state-machine semantics. Default-OFF — **setup still needed**: BotFather token + chat ID in `.env` (see `docs/ENV_VARS.md` → Telegram Bot).

Voice notes (→ STT on Helios → same pipeline) and photos (→ vision model on Saturn → same pipeline) shipped July 2026 (`TELEGRAM_VOICE_ENABLED`/`TELEGRAM_PHOTO_ENABLED`). Stretch goal still open: morning briefing as a Telegram digest.

### 2. ~~Trust layer as a feature~~ ✅ BUILT (July 2026)

Shipped: the morning briefing owns up to missed/failed reminders from the last 24h (mirrored to Telegram when the bot is on); the dashboard RemindersCard shows a last-24h delivery log (delivered / missed / failed, ack channel, "N not delivered" badge); Grafana "Reminder Delivery — Trust" row (outcomes, failed/missed 7-day stats, ack latency, per-channel push health, per-speaker TTS success).

### 3. ~~Durable task backlog~~ ✅ DONE (PRs #44/#45/#46, July 2026)

Tasks table, one-answer-at-a-time `what_now`, brain-dump capture, decompose linking, weekly review, dashboard + Grafana. Remaining nice-to-have: unify the older `update_data(add_project)` YAML "projects" concept with backlog tasks (projects = multi-step efforts, tasks = quick to-dos).

## Tier 2 — HA-enabled quick wins (HA on Jupiter is the enabler)

HA is now co-located and reliable, so HA-driven features are suddenly cheap.

### 4. ~~Sleep wind-down ladder~~ ✅ BUILT (July 2026)

Shipped as `jobs_winddown.py` (`WIND_DOWN_*` env vars, bedtime anchor default 22:30): T-60 (21:30) dims the house via configured HA scene(s) silently — the evening briefing (#6) is the ladder's spoken tomorrow-preview anchor at the same moment; T-30 (22:00) speaks a screens-away nudge with a one-line tomorrow anchor. Both rungs skip under DND (scene.turn_on would raise lights an early goodnight turned off); the nudge also yields to an active guided routine. Morning half: `sleep_mode("on")` stamps `sleep_started_at`, and a night under `WIND_DOWN_SHORT_NIGHT_HOURS` (6.5) softens the morning briefing — gentler greeting, weather skipped. Remaining stretch: softer *alarm/routine* escalation on short nights (routine nudges are unchanged).

### 5. Geofenced errand reminders ⬜

`presence_tracker.py` already polls HA presence — this is mostly wiring. New reminder trigger type bound to presence transitions: "next time I leave home, remind me to take the package" / "next time I'm home, …".

### 6. ~~Evening shutdown ritual~~ ✅ BUILT (July 2026)

Shipped as `jobs_calendar.evening_briefing()` (default 21:30, `EVENING_BRIEFING_*` env vars): tomorrow's first event + leave-by time via Google Maps, evening meds check, and parking one unfinished thing (active focus task, else top backlog task) into persistent `app_state` — the morning briefing offers it back and clears it only after a successful announce. DND-aware (parks silently), Telegram-mirrored, with an `EveningBriefingStale` dead-man's-switch alert. Now the spoken anchor of the wind-down ladder (#4).

## Tier 3 — Richer intelligence (later, but high-leverage)

### 7. Time-estimation calibration ⬜

Estimated vs. actual is already stored on decomposed tasks — learn my personal multiplier per category instead of the fixed 1.5× buffer. A real time-blindness aid almost nobody ships.

### 8. Unified reward economy ⬜

Finance XP, routine streaks, and workout consistency are three silos. One cross-domain streak/XP system with **streak insurance** (one free miss per week) so a single bad day doesn't nuke motivation.

### 9. Mood/state longitudinal log ⬜

The mode router already classifies per-utterance intensity (panic/shame/spiral). Log it — encrypted, like `auto_learn` — and correlate weekly with meds adherence, sleep, and streaks for patterns.

## Tier 4 — Improvements & debt (not features)

| Item | Why | Status |
|------|-----|--------|
| **Real streaming** | ~~Faked~~ ✅ BUILT (July 2026): every model round streams from vLLM; gate-safe tokens relay through SSE while the tool loop runs (`unified_loop.StreamGate` suppresses think/XML-tool-call blocks, tool_calls assemble from deltas, buffered fallback per round). `REAL_STREAMING_ENABLED` kill switch; `bgw_chat_ttft_seconds` histogram. HA Assist/Telegram (`stream=false`) unchanged. | ✅ |
| **`/api/announce` honesty** | Returns `ok: true` even when every speaker fails (only 500s if the call raises) — dashboard looks successful during an HA outage. Small fix, real honesty. | ⬜ |
| **`.claude/agents/*.md` refresh** | Still describe the removed v6 Nemotron architecture and will mislead the review agents CLAUDE.md invokes; `prod-support.md` also references the deleted Deep-Dive dashboard. | ⬜ |
| **Grafana Alertmanager datasource** | The loose thread from the dashboard consolidation — alerts don't render in the single pane yet. | ⬜ |
| **Pin env/time-coupled tests** | `test_selfcare_manager::TestMealCheck` (wall clock), `test_config` defaults (host env), `test_ntfy_feedback` `disabled_returns_404` (assumes `PUSHOVER_ENABLED` off) — green today, flake risk. | ⬜ |
| **Reconcile `homelab-infra.json` dashgen drift** | Confirmed 2026-07-05 while regenerating Grafana JSON for #46: running `dashgen/build.py` drops ~440 lines of stale hand-edits (`mappings: []` / `values: true`) — the committed JSON is out of sync with its builder. Reverted out of #46 to keep scope clean; reconcile task spawned separately. | 🔶 task spawned |
| **Chores** | Delete the three `perf/*` backup branches (rebased in as #36); rotate off-box backups on Saturn (local KEEP=30 rotates, Saturn grows unbounded); ~~fold alertmanager into `monitoring/docker-compose.yml`~~ ✅ 2026-07-06 (compose-managed, config rendered by `generate-configs.sh`, CI re-renders + reloads on merge); `git rm` `data/palace.yaml` from public view. | ⬜ |

## Tier 5 — Carried forward from the old roadmap

### Jess Face avatar 🔶 mostly built

3D VRM tap-to-talk kiosk (`jess-face/`, built 2026-07-04, verified against the live gateway) for Pi 5 + 5" DSI touchscreen. Remaining: commit it, deploy to the Pi (needs the Pi on the network + a USB mic — DSI carries no audio), and design Jess's real look in VRoid Studio (current model is the pixiv sample placeholder).

### Document memory ⬜ (half-covered)

Paperless-ngx bridge (F-012) covers ingestion + OCR + tagging. The missing half is **voice-queryable** document knowledge: parse → chunk → embed into ChromaDB so "when does my lease expire?" / "what's my policy number?" work. Extend `ingest_rag.py` with PDF/OCR handlers, or query Paperless's full-text index from a tool.

### Vision & multimodal ⬜

Qwen3-VL-8B already runs on Saturn (meal-photo calories use it). Extend: pantry photo → meal ideas, whiteboard/receipt photos → OCR → RAG, "what am I looking at?".

### Frontend: public domain + polish ⬜

- Phase 6: DNS + Cloudflare Tunnel → ConvivialProphet.com (plus orchestrator CORS update)
- Phase 7: PWA, mobile optimization, animations, toasts

### Hardware ⬜

| Item | Why |
|------|-----|
| Speaker for the record player | Frees the Google Max aux input — the root cause of `all_speakers` HTTP 500s and TTS group failures |
| ATOM Echo #2 (bedroom), #3 (kitchen) | Whole-house wake word |
| Route voice replies to Google speakers | Replies still play on the ATOM Echo's tiny speaker (needs HA UI work) |

### vLLM 256K context ⬜ (when worth it)

Needs vLLM 0.19.2+ (KV-calc fix) and the primary moving GPU0 → GPU1 (the 5090 can't hold Lorbus + 256K KV in 32 GB). Revisit when a use case actually needs >150K context.

## Dropped / superseded

- **ClickUp integration** — superseded by the native task backlog + planned Telegram push
- **OpenClaw** — researched, rejected (CVEs, unreliable memory, API costs); custom orchestrator stays
- **Web `/setup` wizard UI** — prototyped through 7 slices, deleted in favor of the express CLI flow (v1.0.0)
- **ATOM Echo S3R LED feedback** — hardware limitation (no programmable RGB on the S3R), wontfix
- **Wake-on-LAN for Helios** — dead end (Aquantia driver); replaced by the HA smart-plug power-cycle (PRs #29/#31)

## Priority order

1. ~~Durable task backlog~~ — ✅ DONE (#44/#45/#46)
2. ~~Telegram capture bot~~ — ✅ LIVE (@Jess_brain_bot)
3. ~~Trust layer~~ — ✅ BUILT (morning recap + delivery log on the dashboard + Grafana trust row)
4. ~~Evening shutdown ritual + sleep wind-down ladder~~ ✅ BOTH BUILT (July 2026)
5. **Geofenced errand reminders** — mostly wiring
6. ~~Real streaming~~ ✅ BUILT (July 2026)
7. **Jess Face deploy** — hardware session when home (Pi + USB mic)
8. **Quick honesty/debt fixes** — `/api/announce`, agents docs, Alertmanager datasource (each < an hour, batchable)
9. **Time-estimation calibration** → **reward economy** → **mood log** — the intelligence tier, in that order
10. **Document memory, vision, public domain, 256K** — as need arises
