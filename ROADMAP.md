# Brain Gateway Roadmap

Personal ADHD brain — voice-first, proactive, low-friction.

The guiding principle: **if it requires opening an app, I won't do it.** Everything should be capturable by voice ("Hey Jess, ...") or happen automatically in the background.

## What Works Today (v1)

| Feature | How it works | Status |
|---------|-------------|--------|
| Voice control | "Hey Jess" wake word → ATOM Echo S3R → HA voice pipeline | Working (office) |
| Home automation | "Turn off the lights" → fast-path or Nemotron tool call | Working |
| Focus timer | "Start focus for 30 minutes" → Pomodoro + Endel audio + Pi-hole blocking | Working |
| Reminders | "Remind me to call the dentist at 3pm" → TTS announcement on speakers | Working |
| Personal memory (RAG) | ChromaDB with 154 docs: profile, patterns, meds, projects | Working |
| Web search | "What's the weather?" → SearXNG | Working |
| Hybrid LLM | Helios 120B for conversation, Nemotron 8B for tools | Working |
| Google Calendar | "What's on my calendar?" → check/create events, proactive alerts | Working |
| Morning briefing | 7:30 AM daily → today's events + pending reminders via TTS | Working |
| Calendar polling | Every 15 min → announce events starting within 2 hours | Working |
| Mode router | Intent-based coaching: explainer/mirror/counterbalance/challenge/baseline | Working |
| HTTPS access | Tailscale Serve → valid TLS cert → mobile mic support | Working |
| TTS pacing | Sentence pause injection + paragraph splitting for calmer speech | Working |

## Known Issues / TODOs

| Issue | Priority | Notes |
|-------|----------|-------|
| Voice pipeline routes to Nemotron directly | High | Should route through orchestrator :8888 for hybrid Helios+Nemotron. Requires HA UI access to change conversation agent. |
| TTS output on ATOM Echo tiny speaker | High | Should route to Google speakers group ("all speakers"). Requires HA UI. |
| ATOM Echo S3R has no LED feedback | Low | S3R variant has no programmable RGB LED (GPIO35 conflicts with PSRAM). Hardware limitation. |
| Calendar only reads Google Calendar | Medium | Outlook/Exchange (work) and Apple Calendar not integrated yet |

## Phase 2: Calendar & Email Awareness

**Goal:** Jess knows what's on my schedule without me telling her.

### Google Calendar sync — DONE
- ✅ OAuth2 setup (Google Cloud project, Desktop credentials, consent flow)
- ✅ `check_calendar` tool — "Hey Jess, what's on my calendar this week?"
- ✅ `create_calendar_event` tool — "Add pickleball Thursday at 7pm"
- ✅ Proactive polling: every 15 min, TTS announcement for events within 2 hours
- ✅ Morning briefing: 7:30 AM, today's events + pending reminders
- ✅ Deployed and configured on Jupiter

### Calendar unification — NOT STARTED
- Outlook/Exchange (Cisco work calendar) → ICS subscription into Google Calendar
- If corporate ICS blocked → iPhone Shortcuts bridge as fallback
- Apple Calendar events → already sync to Google if using Google account

### Gmail monitoring — NOT STARTED
- Watch for calendar invites, flight confirmations, bill due dates
- Parse and extract: event name, date, location, deadlines
- Auto-create reminders or calendar entries
- "Hey Jess, did I get any important emails today?"
- Requires adding Gmail API scopes to OAuth2

## Phase 3: Document Memory — NOT STARTED

**Goal:** Upload anything important → ask about it later by voice.

### Document ingestion pipeline
- Upload via Open WebUI file attachment, or a simple web endpoint
- Supported formats: PDF, images (OCR), Word docs, plain text
- Parse → chunk → embed → store in ChromaDB
- Metadata: upload date, document type, source

### Use cases
- Upload a lease → "Hey Jess, when does my lease expire?"
- Upload insurance card → "Hey Jess, what's my insurance policy number?"
- Photo of a receipt → stored for expense tracking
- Photo of pantry → "Hey Jess, what can I make for dinner with what I have?"

### Implementation notes
- PDF parsing: PyMuPDF or pdfplumber
- OCR: Tesseract or vision model
- Extend existing `ingest_rag.py` with format handlers
- New tool: `ingest_document` (or auto-ingest on upload)

## Phase 4: Proactive Agent — NOT STARTED

**Goal:** Jess doesn't just respond — she anticipates.

### Background agent loop
- Runs continuously, checks various sources on schedules
- Gmail → new important emails → summarize and notify
- Calendar → upcoming events → pre-reminders (✅ done)
- Bills/deadlines from ingested documents → warning notifications
- Medication schedule → daily reminders at set times

### Proactive notifications
- Push to Google speakers via HA media_player TTS
- Priority levels: urgent (immediate), normal (next quiet moment), low (morning briefing)
- Quiet hours: no announcements during sleep, configurable

### Task capture and follow-up
- "Hey Jess, I need to call the insurance company" → stored as task
- Jess follows up: "You mentioned calling the insurance company yesterday. Want me to remind you at a good time today?"
- Gentle nagging with escalation for overdue items (ADHD-friendly, not guilt-inducing)
- ClickUp integration for task visibility on phone

### Context awareness
- Track focus sessions → "What was I working on before lunch?"
- Time-of-day awareness → suggest dinner ideas in the evening
- Location awareness (future) → remind about errands when leaving

### OpenClaw consideration
- Researched extensively — NOT recommended as orchestrator replacement
- Better as multi-channel frontend (WhatsApp/Telegram) alongside existing system
- Security concerns (3 CVEs, exposed instances), unreliable memory, high API costs
- Current custom orchestrator is more deterministic and reliable for tool execution

## Phase 5: Vision & Multimodal — NOT STARTED

**Goal:** See the world, not just hear it.

- Pantry photos → meal planning with dietary preferences
- Document photos → OCR → RAG (contracts, receipts, business cards)
- Whiteboard photos → extract and store notes
- Screen sharing → "Hey Jess, what am I looking at?" (future)

## Hardware Roadmap

| Device | Location | Status |
|--------|----------|--------|
| ATOM Echo S3R | Office | Flashed, online, wake word working |
| ATOM Echo S3R #2 | Bedroom | Not purchased |
| ATOM Echo S3R #3 | Kitchen | Not purchased |
| Google speakers | Whole house (office, bedroom, kitchen) | Existing, TTS output target |

## Priority Order

1. ~~**Calendar integration**~~ — ✅ DONE
2. ~~**Mode-aware coaching**~~ — ✅ DONE (intent router + personalized system prompts)
3. ~~**Personal RAG knowledge**~~ — ✅ DONE (154 docs: identity, patterns, preferences)
4. ~~**HTTPS + mobile mic**~~ — ✅ DONE (Tailscale Serve)
5. ~~**TTS pacing**~~ — ✅ DONE (paragraph splitting + sentence pause injection)
6. **Voice pipeline routing** — Route through orchestrator for hybrid LLM quality (needs HA UI)
7. **TTS to Google speakers** — Better audio output than tiny ATOM Echo speaker (needs HA UI)
8. **Gmail integration** — Email awareness, auto-reminders from invites
9. **Document ingestion** — builds on existing RAG, immediate utility
10. **Proactive agent** — transforms from reactive to anticipatory
11. **Vision/multimodal** — nice to have, depends on model capabilities
