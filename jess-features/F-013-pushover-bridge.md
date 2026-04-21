# F-013: Pushover Bridge

**Priority:** P1 — Fallback from F-011 when iOS APNs registration via ntfy.sh-upstream proves unreliable
**Status:** Done
**Depends on:** F-011 (ntfy feedback loop — reuses HMAC callback routes)
**Blocks:** None

---

## ADHD Insight

The whole point of F-011 was to close the reminder feedback loop on the phone. If the channel doesn't reliably land a **banner** on the lockscreen — only appearing inside the app after pull-to-refresh — it fails the ADHD test: an invisible notification is no notification. ntfy-on-self-hosted over iOS APNs depends on a multi-hop setup (Jupiter → ntfy.sh upstream → APNs → phone) where the last hop needs the iOS app to have registered its device token on ntfy.sh's hashed-topic map. That registration is fragile enough in practice that a separate vendor with a rock-solid iOS push path is worth the $5 app + the glue code.

Pushover is a commercial push service with a native iOS app, proper APNs certificates, and no self-hosted-server indirection. Notification reliability on iOS is dramatically better than ntfy-upstream.

## What Jess Does

When `PUSHOVER_ENABLED=true`, every reminder that would fire an ntfy push **also** fires a Pushover push — running alongside ntfy, not replacing it. The user can turn ntfy off (`NTFY_ENABLED=false`) if they want Pushover-only.

The reminder body is sent with the **Done callback URL as the primary tap action** (Pushover's `url` field) and the **Snooze callback URL embedded as an HTML link in the message body** (Pushover's `html=1` mode makes `<a href>` tags tappable inside the notification expanded view). HMAC signing is unchanged — same ack/snooze routes, same signature scheme.

The F-011 confirm side-channel (visible "Logged" / "Snoozed until X:XX" after a tap) also fires via Pushover when enabled.

---

## Interaction Examples

### Primary reminder with Pushover

```
9:00 AM: Jess TTS speaks "Hey Nadim! Quick reminder: take morning meds."
         Pushover pushes:
           Title:   Jess reminder
           Body:    take morning meds
                    💤 Snooze 10 min         <- tappable HTML link in body
           URL:     (primary tap action = Done callback URL)
           URL-Title: ✓ Done

         iOS banner lands reliably within 1–3 seconds.

9:00:04: You tap the banner (or swipe + tap "✓ Done" on the URL action).
         → Safari opens the signed ack URL → orchestrator acks → closes tab
         → reminders.ack_at = now, acked_via=ntfy   (yes, 'ntfy' — the
           callback routes don't distinguish channel; they just verify HMAC)
         → selfcare bridge fires (meds keyword match)
         → Pushover confirm push lands: "✓ Logged" with body
           "take morning meds\n(medication logged)"
```

### Snoozed path

```
3:00 PM: Pushover push "stretch" lands as banner.
         User taps to expand in Pushover app, taps the "💤 Snooze 10 min"
         link in the body.
         → Safari opens signed snooze URL → orchestrator reschedules
         → Pushover confirm push: "💤 Snoozed until 3:10 PM"
                                   body: "1/5 snoozes used"
```

---

## Security model

No change from F-011. Both ack and snooze URLs are HMAC-signed (`sig = HMAC-SHA256(NTFY_HMAC_SECRET, f"{id}|{action}|{exp}|{extra}")[:32]`). The routes themselves are bearer-exempt via `BearerAuthMiddleware.PUBLIC_PREFIXES` and do their own signature verification.

**Pushover-specific data considerations:** reminder body transits Pushover's infrastructure (their servers + APNs). For a personal ADHD assistant the content is mostly "take meds" / "pet the cat" — low sensitivity. Medical category is NOT in titles (same rule as F-011 security review). The callback URLs contain the reminder_id (random UUID4) and HMAC sig — if a Pushover operator wanted to ack a user's reminder they'd need to extract the URL and ignore that they're clicking someone else's ack. Accepted risk.

**Credentials:** `PUSHOVER_APP_TOKEN` and `PUSHOVER_USER_KEY` in `.env` (chmod 600). Both are sent in the POST body only, never logged. If `PUSHOVER_ENABLED=true` but either token is missing, a `model_validator` auto-disables the channel with a loud error log — same pattern as F-011's HMAC secret check.

---

## Modified files

- NEW `orchestrator/pushover_manager.py` — async httpx client with `deliver_via_pushover(reminder_id, text)` and `deliver_pushover_confirm(title, message, reminder_id=None)`. Never raises; every exit path increments metrics exactly once.
- NEW `jess-features/F-013-pushover-bridge.md` (this file).
- `orchestrator/config.py` — `pushover_enabled`, `pushover_user_key`, `pushover_app_token`, `pushover_default_priority`, `pushover_api_url`, `pushover_upload_timeout_seconds`; new `model_validator` for auto-disable.
- `orchestrator/metrics.py` — `bgw_pushover_push_total{result,kind}` and `bgw_pushover_push_latency_seconds{kind}`.
- `orchestrator/tool_handlers.py` — `deliver_reminder_job()` also fires `deliver_via_pushover` via `_fire_and_forget` when enabled.
- `orchestrator/api_routes.py` — ack/snooze routes also fire `deliver_pushover_confirm` via `_fire_and_forget`.
- `docker-compose.yml` — env passthrough for `PUSHOVER_*`.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `PUSHOVER_ENABLED` | `false` | Master switch |
| `PUSHOVER_USER_KEY` | *(empty)* | Your Pushover user key (from the dashboard home page). 30+ char alphanum; **required** when enabled |
| `PUSHOVER_APP_TOKEN` | *(empty)* | Pushover application token (create one on the dashboard and paste here). 30+ char alphanum; **required** when enabled |
| `PUSHOVER_DEFAULT_PRIORITY` | `0` | -2 (low, silent) to 2 (emergency — requires ack, rarely what you want for reminders). 0 = normal; 1 = high (bypasses quiet hours) |
| `PUSHOVER_API_URL` | `https://api.pushover.net/1/messages.json` | Override only for testing / mock servers |
| `PUSHOVER_UPLOAD_TIMEOUT_SECONDS` | `10` | httpx timeout per push |

---

## Metrics

- `bgw_pushover_push_total{result, kind}` — `result`: `ok|fail|skipped`; `kind`: `reminder|confirm`. Every exit path from a push function increments exactly once.
- `bgw_pushover_push_latency_seconds{kind}` — histogram of the POST latency (orchestrator → Pushover).

---

## Testing checklist

- [ ] `deliver_via_pushover` with `PUSHOVER_ENABLED=false` returns `{success: False, skipped: True, reason: "disabled"}`, no HTTP, metric `result=skipped, reason=disabled`.
- [ ] Missing user_key or app_token with enabled=true at runtime → skipped path.
- [ ] Happy path mocked 200: metric `result=ok, kind=reminder`; request body includes `token`, `user`, `message`, `title`, `url` (Done), `url_title`, `html=1`, and the snooze link embedded in message.
- [ ] Pushover 4xx with JSON error body: metric `result=fail`; error body sanitized (no HTML, capped 300 chars) before surfacing into the handler return.
- [ ] Network error (httpx.ConnectError / TimeoutException): metric `result=fail, reason=connect_error|timeout`; no exception surfaced.
- [ ] `deliver_pushover_confirm` with `PUSHOVER_ENABLED=false` → skipped; with enabled → single message, no url, priority=-1 (quiet).
- [ ] Model validator auto-disables on missing creds with a loud error log; does NOT raise.
- [ ] Ack/snooze route wiring: `_fire_and_forget(deliver_pushover_confirm(...))` called after state mutation; NOT called in the `already_acked` idempotent-replay branch.

---

## Future (not in this increment)

- **Multi-user delegation** — Pushover supports "groups" (multiple user keys aggregated under one delegate key). If a household partner ever wants the same reminders, switch `PUSHOVER_USER_KEY` to a delegate key without changing code.
- **Emergency priority** on safety-critical reminders (e.g. "turn off stove") — priority=2 makes Pushover require an explicit ack before the alert stops repeating. Not wired today because we don't have a way to mark a reminder as emergency-class.
- **Rich attachments** — Pushover supports image attachments. If a reminder is tied to a photo (e.g. "check the package I left by the door" with a doorbell snapshot), we could attach the JPEG. Speculative.
