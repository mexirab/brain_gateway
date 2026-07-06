# Quick start — what your assistant can do

This is the install-day "what can it do?" reference. Pick a section, try a few things, see what sticks.

> **Naming note.** Throughout this guide, the assistant's name is whatever you set during the wizard's Identity step. The examples use "Jess" because that's the maintainer's deployment. Replace it with whatever you picked. Voice puck triggers (e.g. "Hey Jess") follow the wake-word you configured.

There are three ways to talk to it:

1. **Web UI** — open the dashboard at `http://<your-box>:3001/` and use the chat panel.
2. **API** — `POST /v1/chat/completions` with `Authorization: Bearer $API_TOKEN`. OpenAI-compatible.
3. **Voice puck** — any ATOM Echo or other Wyoming-compatible mic. Hands-free, room-aware, multi-speaker.

---

## First 5 minutes

When you open the dashboard for the first time, just say "hi" — Jess greets you with a tour of what's working, what's not yet configured, and how to set up anything optional.

After that, try this sequence to exercise the features that ship by default:

1. *"What can you do?"* — gets a tailored overview based on which integrations you've enabled.
2. *"Remind me to drink water in 15 minutes."* — proves the reminder pipeline works.
3. *"Brain dump: pay the electric bill, schedule eye exam, look into noise-cancelling headphones."* — watches Jess categorize and route each item.
4. *"Start a 25-minute focus timer on email."* — fires the Pomodoro flow with check-ins.

That's the surface area for 80% of daily use.

---

## Setting up new features (just ask Jess)

The install gives you a working chat + voice + memory + reminders. Everything else (smart home, push notifications, document storage) is configured **after** install — either by asking Jess or via the web Settings page.

| Say this | What Jess does |
|----------|----------------|
| *"Set up Home Assistant"* | Asks for your HA URL + a long-lived access token, tests the connection, saves the credentials. After it's saved, restart with `docker compose restart orchestrator` to activate. |
| *"Set up ntfy"* (Android phone push) | Asks for the ntfy server URL (default `https://ntfy.sh`), a topic name (your private channel), and an HMAC secret. Suggests `openssl rand -hex 32` for the secret. Sends a test push to confirm before saving. Active immediately. |
| *"Set up Pushover"* (iPhone-preferred phone push) | Asks for your user key + an application token (both from `https://pushover.net`). Sends a test push to confirm. Active immediately. |
| *"Set up Paperless"* (OCR + auto-tagged document storage) | Asks for the Paperless URL + an API token. Tests the connection. Active immediately. |

If you prefer a web form for these, visit `/settings` in the dashboard — same configuration, different shape.

---

## Focus & productivity

| Say this | What happens |
|----------|--------------|
| "Start a focus timer for 25 minutes on emails" | Pomodoro timer with ambient audio, optional site blocking, and check-ins |
| "Start a body doubling session, 3 sprints" | Multi-sprint focus with breaks between each |
| "Stop focus" / "I'm done" | Ends the timer early |
| "Next sprint" / "Extend by 10 minutes" | Continue or add time during a session |
| "Break down cleaning the kitchen" | Splits into micro-steps with time estimates |
| "Done" / "Skip" / "Next step" | Advance through decomposed task steps |

Pi-hole site blocking during focus is opt-in — connect a Pi-hole in `.env` (`PIHOLE_URLS`, `PIHOLE_PASSWORD`) to enable it.

---

## Routines

| Say this | What happens |
|----------|--------------|
| "Start morning routine" | Step-by-step guidance with TTS, auto-skip when you've already done a step |
| "Start evening routine" | Wind-down: meds → tomorrow's calendar → screens away |
| "Done" / "Skip" / "Pause routine" | Control the flow — it waits for you, nudges if you go quiet |

Routines also auto-trigger on the schedule you set in `/settings → Routines`. Default is 7:00 AM / 9:00 PM.

---

## Brain dumps

| Say this | What happens |
|----------|--------------|
| "Brain dump: call the dentist, buy groceries, research flights" | Each item gets categorized (task / idea / reminder / fact) and routed to memory or the reminder pipeline |
| "I keep thinking about that thing with the car" | Captured to memory, searchable later |

Brain dumps are the highest-leverage feature for ADHD use. You never have to know what to do with a thought — just say it, the assistant decides where it lives.

---

## Reminders & calendar

| Say this | What happens |
|----------|--------------|
| "Remind me to call Mom at 3pm" | TTS announcement at 3pm + push to your phone if ntfy / Pushover is configured |
| "Remind me to take meds every weekday at 8am" | Creates a recurring rule (visible in `/settings → Recurring Reminders`) |
| "What's on my calendar?" | Today's events read aloud |
| "Add pickleball Thursday at 7pm" | Creates a Google Calendar event (requires the wizard's Calendar integration) |
| "Check my email" / "Any new messages?" | Read-only Gmail summary (requires the wizard's Gmail integration) |

The assistant also proactively announces upcoming events with escalating alerts (1hr → 30min → 15min → 5min) and computes "leave by" times for events with locations.

If you set up ntfy or Pushover in the wizard, every reminder includes one-tap **Done** and **Snooze** buttons on the lockscreen.

---

## Self-care nudges

The assistant monitors meals, meds, water, and movement — nudging when something is overdue. Tune the cadence in `/settings → Selfcare Nudges`.

| Say this | What happens |
|----------|--------------|
| "I had lunch" / "Just ate a sandwich" | Logs your meal, stops food nudges |
| "I took my meds" / "Yes I took it" | Logs medication, tells you next dose |
| "Had some water" | Resets hydration timer |
| "Log a 20-minute walk" | Movement logged |

You don't need to remember to log — nudges come to you on the configured interval, and a routine step ("take meds") auto-logs the matching category when you say "done."

---

## Interruptions

| Say this | What happens |
|----------|--------------|
| "I need to take this call" / "BRB" | Bookmarks what you were doing, checks in after 5 minutes |
| "What was I working on?" | Lists your last 3 activities with timestamps |

---

## Smart home (Home Assistant)

Requires the wizard's Home Assistant integration.

| Say this | What happens |
|----------|--------------|
| "Turn off the bedroom lights" | Controls any HA device |
| "Set the office fan to 50%" | Brightness, temperature, scenes |
| "Movie time" | Triggers a scene if you have one named "movie" or similar |

The assistant infers entity IDs from natural language; it doesn't need exact HA syntax.

---

## Decision simplifier

| Say this | What happens |
|----------|--------------|
| "What should I work on?" | One directive recommendation based on tasks + calendar + recent state |
| "I'm overwhelmed" | Triage mode: single most important thing, everything else dismissed |
| "What should I eat?" | Two quick options based on your preferences + recent meals |
| "Help me decide between X and Y" | Structured comparison with a recommendation |

For ADHD use, **the assistant never gives you a list when one answer will do.** That's deliberate — choice paralysis is the failure mode, not lack of information.

---

## Shopping lists

| Say this | What happens |
|----------|--------------|
| "Add milk to my grocery list" | Adds to the named list (creates the list if it doesn't exist) |
| "What's on my grocery list?" | Reads it back |
| "Check off milk" / "I got the milk" | Marks complete without deleting |
| "Clear the checked items" | Cleans up what you've already got |

Multiple lists work — "Add batteries to the hardware list," etc.

---

## Personal RAG memory

Drop markdown files into your RAG directory (set during install). The assistant searches them in conversation.

| Say this | What happens |
|----------|--------------|
| "What did I write about that vendor evaluation?" | Semantic search across your markdown |
| "Remember that the wifi password is on the fridge" | Saves a fact into the memory palace |
| "What do you know about my morning routine?" | Pulls from RAG + auto-learned facts |

Reindex when you add docs: `docker exec brain-orchestrator python scripts/reindex_rag.py`.

---

## Quiet mode

| Say this | What happens |
|----------|--------------|
| "Goodnight" / "Bedtime" | DND: all announcements suppressed until morning |
| "Good morning" | Wakes the assistant back up (auto-clears when morning briefing fires 5–11am) |

Quiet hours can also be scheduled in `/settings → Quiet Hours` (with per-day-of-week control).

---

## Documents

| Say this | What happens |
|----------|--------------|
| "Save this as a doc: meeting notes from..." | Stored in `document_vault` (text-based, semantically searchable) |
| "Read me the lease summary" | Pulls from `document_vault` |
| Drop a PDF in the inbox + "Save the lease for OCR" | Pushed to Paperless-ngx for OCR + auto-tagging (requires the wizard's Paperless integration) |

---

## Workouts (optional)

| Say this | What happens |
|----------|--------------|
| "Generate today's workout" | Adaptive plan: full-body or split based on what you did recently |
| "I did 3 sets of bench press at 135 for 10 reps" | Logs the sets, updates PR tracking |
| "Show me today's workout" | Reads back the plan + what you've logged |
| "Swap out the deadlifts" | Modifies today's plan in place |

---

## Meals (optional)

| Say this | What happens |
|----------|--------------|
| "Log a 600-calorie lunch" | Calorie-only meal entry |
| Upload a photo + "How many calories?" | Vision-model estimate (requires the optional Qwen3-VL-8B vision model) |

---

## Proactive features (automatic)

| Feature | When |
|---------|------|
| Morning briefing | Your configured wakeup time — today's events + pending reminders + weather + yesterday's parked item |
| Evening shutdown ritual | 9:30 PM (configurable) — tomorrow's first event + leave-by time, evening meds check, parks one unfinished thing for the morning |
| Calendar alerts | Tiered countdown: 1hr, 30min, 15min, 5min before events |
| "Leave by" alerts | For events with physical locations, factoring in travel time |
| Daily progress recap | 6:00 PM — tasks done, focus time, streaks |
| Weekly digest | Sunday 7:00 PM — week summary with trends |
| Ambient summaries | 10am, 12pm, 2pm, 4pm — brief status check |
| Self-care nudges | Configurable interval — meals, meds, water, movement (if overdue) |
| Streak celebrations | "Focus streak: 5 days in a row!" |

All of these respect your quiet hours and quiet days.

---

## Dashboard

Visit `http://<your-box>:3001/` to see everything at a glance: calendar, reminders, focus timer, progress tracking, announcement history, system health.

Highlights:
- **Today card** — what's happening, what's due, what's overdue
- **Focus widget** — start / stop / extend a session without opening chat
- **Reminders** — see + ack pending reminders from any device
- **Selfcare today** — at-a-glance status for meals / meds / water / movement
- **Mobile-friendly** — works as a PWA from your phone home screen

---

## Settings

Visit `/settings` to tune things without restarting:

- **Identity & Tone** — assistant name, your name, ADHD mode toggle, tone preset (warm / balanced / direct), timezone
- **Selfcare Nudges** — turn each category (medication, meal, water, movement) on or off, set the interval, set the active hours
- **Quiet Hours** — start/end + day-of-week filter (no more nudges on Sunday mornings if you don't want them)
- **Routines** — edit step labels, change trigger times, set per-routine nudge limits
- **Speakers** — map each announcement category to a specific HA `media_player.*` entity (or multiple, comma-separated)
- **Recurring Reminders** — cron-based rules ("every weekday at 9am, take meds") that auto-materialize into the normal reminder pipeline

---

## Advanced features (operator-only)

These ship in the codebase but are gated behind `JESS_ADVANCED=true` in `.env`. They're operator-grade tools — useful when you're maintaining your own install, less useful day-to-day.

| Tool | What |
|------|------|
| `check_claude_activity` | Read what Claude Code has been doing — recent turns, files touched |
| `code_agent` | Delegate a coding task to a local Qwen3-Coder-Next 80B/3B MoE (needs significant VRAM) |
| `ask_expert` | Delegate a hard reasoning task to a separate Qwen3-32B "expert" model (needs a second GPU box) |
| `query_budget` | Query historical budget/spending data from CSV/Excel imports |
| `finance_status` | YNAB integration: budget, spending, XP/levels |

Enable with `JESS_ADVANCED=true` in `.env` and re-create the orchestrator container.

---

## Tips

- **One thing at a time.** The assistant never gives you a list when one answer will do.
- **No guilt.** Skip anything. Miss a routine. The assistant never makes you feel bad about it.
- **You don't need to check anything.** Everything important comes to you — via speakers, push, or both.
- **"Hey {your wake word}" works from any room** with a configured voice puck.
- **Use the settings page.** It exists so you never have to edit `.env` again.

---

## Where to go next

- **Printable card** — see [`JESS_REFERENCE_CARD.md`](JESS_REFERENCE_CARD.md) or open the HTML version (`jess_reference_card.html`) and print to PDF. It groups commands by *situation* ("I just got home and need to start a routine") instead of by feature, which works better for ADHD recall.
- **Voice puck setup** — [`VOICE_AND_TTS.md`](VOICE_AND_TTS.md) covers ATOM Echo provisioning, Wyoming bridges, and STT config.
- **Google integrations** — [`GOOGLE_INTEGRATIONS.md`](GOOGLE_INTEGRATIONS.md) for Calendar + Gmail OAuth setup.
- **Workouts + meals** — [`WORKOUTS_AND_MEALS.md`](WORKOUTS_AND_MEALS.md) for the gym tracker internals.
- **Memory system** — [`MEMPALACE.md`](MEMPALACE.md) for how the RAG + auto-learning works.
