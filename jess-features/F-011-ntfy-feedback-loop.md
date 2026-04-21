# F-011: ntfy Feedback Loop

**Priority:** P1 — Closes a known awareness gap
**Status:** Done
**Depends on:** F-002 (time nudges), F-008 (selfcare nudges), ntfy server on Jupiter
**Blocks:** None

---

## ADHD Insight

Today Jess sends reminders but doesn't know what you did with them. She announces on a speaker, pushes to HA Companion, and then the trail goes cold — did you take the meds, ignore the reminder, or just not hear it? Without a round-trip, she can't adapt (repeat-nag vs. leave-alone) and `selfcare_log` only updates if you remember to tell her by voice.

## What Jess Does

Pushes reminders via ntfy with tappable **Done** / **Snooze 10m** buttons. Tapping a button posts back to the orchestrator over HTTPS, which:

- Marks the reminder acknowledged (and records *how* — `ntfy` vs. `voice` vs. `ui`).
- If the reminder body matches a self-care keyword (`meds` / `meal` / `water` / `movement`), fires the same `record_*_logged` bridge that `selfcare_log` uses, so downstream nudges stop firing.
- For snooze: reschedules the underlying reminder job by N minutes and increments `snooze_count`.

ntfy runs alongside the existing TTS + HA Companion phone push — it's a third delivery channel, not a replacement. If `NTFY_ENABLED=false`, nothing changes.

---

## Interaction Examples

### Scenario 1: Meds reminder acknowledged from lock screen

```
9:00 AM: Jess TTS: "Hey Nadim! Quick reminder: take morning meds."
         HA Companion pushes "take morning meds".
         ntfy pushes "take morning meds" with [Done] [Snooze 10m].

9:01 AM: You tap [Done] on the lock screen.
         → POST /api/reminder/ack/<id>?sig=...&exp=...
         → reminders.ack_at = now, acked_via = ntfy
         → record_medication_logged("reminder:take morning meds")
         → 11:30 AM selfcare nudge "you haven't taken meds" does NOT fire.
```

### Scenario 2: Not ready yet, snooze

```
3:00 PM: ntfy pushes "stretch" with [Done] [Snooze 10m].
         You tap [Snooze 10m].
         → POST /api/reminder/snooze/<id>?min=10&sig=...&exp=...
         → scheduler reschedules deliver_reminder_job at 3:10 PM
         → snooze_count becomes 1
3:10 PM: ntfy fires again, same reminder, fresh signature/exp.
```

### Scenario 3: Runaway snooze guardrail

```
After MAX_SNOOZE_COUNT (default 5), snooze returns 409 Conflict with a
log line. Reminder drops back to Jess's standard failure path — user
clearly isn't engaging; escalate to voice nag or drop entirely per
existing reminder_manager retry logic.
```

---

## Security model

ntfy action-button URLs are **not authenticated with a Bearer token** — the API_TOKEN is a long-lived shared secret and would be visible in the ntfy server's SQLite message cache, in notification metadata on the phone, and on anyone's screen subscribed to the open-tailnet topic.

Instead:

- Each action URL is signed: `sig = HMAC-SHA256(NTFY_HMAC_SECRET, f"{reminder_id}|{action}|{exp}|{extra}")`, truncated to 32 hex chars.
- `extra` is empty for `ack` and the clamped `minutes` value for `snooze`, so an attacker who sniffs the open-tailnet topic can't replay a snooze URL with `minutes=120` to grind through the user's snooze budget.
- `exp` is a Unix timestamp ~30 minutes after the reminder fires; beyond it, the signature is refused.
- `BearerAuthMiddleware` exempts `/api/reminder/ack/` and `/api/reminder/snooze/` prefixes. Both handlers first check `settings.ntfy_enabled` (returning 404 if the feature has been turned off, so stale signed URLs can't outlive the feature), then run the HMAC check before any state mutation.
- Replay after ack is a no-op: once `ack_at` is set, further POSTs return `{"ok": true, "already_acked": true}` and do NOT re-fire the selfcare bridge. Accepted risk on the open-tailnet topic: an attacker who taps ack within the 30-min window before the user can cause the user's own later tap to land on the idempotent path, meaning the selfcare bridge never fires.

`NTFY_HMAC_SECRET` is required whenever `NTFY_ENABLED=true`. If missing or shorter than 32 chars at startup, the `model_validator` in `config.py` logs a loud error and **auto-disables** `ntfy_enabled` instead of refusing to boot — a missing optional-feature secret should never take down chat, HA, or the scheduler.

---

## Tool schema changes

None. Jess still calls `set_reminder` exactly as before. The extra delivery channel is decided at delivery time based on `NTFY_ENABLED`.

---

## New routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/reminder/ack/{id}?sig=&exp=` | HMAC | Mark reminder acknowledged; fire selfcare bridge if keyword match |
| POST | `/api/reminder/snooze/{id}?min=&sig=&exp=` | HMAC | Reschedule reminder by `min` minutes; increment snooze_count |

Both return plain JSON: `{"ok": true, ...}`. 403 on bad signature, 404 on unknown id, 409 on over-snoozed, 410 on expired sig.

---

## Modified files

- `orchestrator/config.py` — `NTFY_ENABLED`, `NTFY_URL`, `NTFY_TOPIC`, `NTFY_DEFAULT_PRIORITY`, `NTFY_CALLBACK_BASE_URL`, `NTFY_HMAC_SECRET`, `NTFY_ACK_EXP_SECONDS`, `NTFY_MAX_SNOOZE_COUNT`.
- `orchestrator/state_store.py` — migrate `reminders` table: add `ack_at`, `acked_via`, `snooze_count`. New helpers `mark_reminder_acked`, `increment_snooze_count`.
- `orchestrator/reminder_manager.py` — new `deliver_via_ntfy()` with async httpx POST + HMAC-signed action URLs; sibling `_infer_selfcare_action_from_text()` matching `_ACTION_KEYWORDS`.
- `orchestrator/tool_handlers.py` — `deliver_reminder_job()` also calls `deliver_via_ntfy` when enabled.
- `orchestrator/api_routes.py` — two new POST routes with HMAC verification and selfcare-bridge dispatch.
- `orchestrator/orchestrator.py` — `BearerAuthMiddleware.PUBLIC_PREFIXES` gains `/api/reminder/ack/` and `/api/reminder/snooze/`.
- `orchestrator/metrics.py` — new counters/histogram in the `bgw_ntfy_*` namespace.
- `docker-compose.yml` — pass through new env vars to the orchestrator service.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `NTFY_ENABLED` | `false` | Master switch |
| `NTFY_URL` | *(empty)* | ntfy server base URL (e.g., `http://10.0.0.248:8889`) |
| `NTFY_TOPIC` | `jess-reminders` | Topic name |
| `NTFY_DEFAULT_PRIORITY` | `3` | ntfy priority 1 (min) .. 5 (max) |
| `NTFY_CALLBACK_BASE_URL` | *(empty)* | Public URL for ack/snooze callbacks (phone → here) |
| `NTFY_HMAC_SECRET` | *(empty)* | 32+ byte hex/base64 secret; **required** when enabled |
| `NTFY_ACK_EXP_SECONDS` | `1800` | Signature validity window (default 30 min) |
| `NTFY_MAX_SNOOZE_COUNT` | `5` | Guardrail against indefinite snooze loops |

---

## Metrics

- `bgw_ntfy_push_total{result="ok|fail|skipped"}` — Counter.
- `bgw_ntfy_ack_total{inferred_action}` — Counter; label is `medication|meal|water|movement|none`.
- `bgw_ntfy_snooze_total` — Counter.
- `bgw_ntfy_push_latency_seconds` — Histogram (push-to-ntfy duration, not phone delivery).
- `bgw_ntfy_callback_rejected_total{reason}` — Counter; label is `bad_signature|expired|not_found|over_snoozed|signing_disabled`.
- `bgw_reminder_ack_latency_seconds` — Histogram, observed only on the first successful ack: how long from `trigger_time` to user tap. The **core F-011 product KPI**.

---

## Testing checklist

- [ ] Signing/verification round-trip with valid & invalid `(sig, exp)` pairs.
- [ ] `/api/reminder/ack/{id}` unknown id → 404; already-acked → `{"ok":true,"already_acked":true}`.
- [ ] `/api/reminder/snooze/{id}` reschedules the APScheduler job; `snooze_count` increments.
- [ ] Snooze beyond `NTFY_MAX_SNOOZE_COUNT` → 409.
- [ ] Expired signature → 410.
- [ ] Selfcare bridge: ack on reminder text "take your meds" → `record_medication_logged` called.
- [ ] Selfcare bridge: ack on text "stretch for five minutes" → `record_movement_logged`.
- [ ] `deliver_via_ntfy` with `NTFY_ENABLED=false` is a no-op (returns `{"success": False, "skipped": True}`).
- [ ] ntfy server unreachable (connection refused) → metric `bgw_ntfy_push_total{result="fail"}` increments, reminder flow continues.
- [ ] Missing/short `NTFY_HMAC_SECRET` + `NTFY_ENABLED=true` at startup → `model_validator` logs error and auto-disables; orchestrator boots normally.
- [ ] Tampering with `minutes` on a snooze URL (valid sig, different minutes param) → `bad_signature` rejection.
- [ ] Reminder fired with `target=phone` (or `both`) dispatches `deliver_via_ntfy` as a detached task — scheduler job returns immediately even if ntfy is slow.

---

## Future (not in this increment)

- Inbound **jess-fyi** topic: Jess subscribes to a second ntfy topic for ambient notifications from other services (Paperless OCR complete, Immich face match, Uptime Kuma events) and can surface them in morning briefings.
- Custom per-reminder actions (e.g., "Took 1 pill" / "Took 2 pills").
- Per-reminder ntfy priority / tags so focus interruptions look different from meal reminders.
