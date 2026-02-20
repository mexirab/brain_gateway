# Brain Gateway Roadmap

Personal ADHD brain — voice-first, proactive, low-friction.

The guiding principle: **if it requires opening an app, I won't do it.** Everything should be capturable by voice ("Hey Jess, ...") or happen automatically in the background.

## What Works Today (v1)

| Feature | How it works |
|---------|-------------|
| Voice control | "Hey Jess" wake word → ATOM Echo S3R → HA voice pipeline |
| Home automation | "Turn off the lights" → fast-path or Nemotron tool call |
| Focus timer | "Start focus for 30 minutes" → Pomodoro + Endel audio + Pi-hole blocking |
| Reminders | "Remind me to call the dentist at 3pm" → TTS announcement on speakers |
| Personal memory (RAG) | ChromaDB with personal docs, meds, projects |
| Web search | "What's the weather?" → SearXNG |
| Hybrid LLM | Helios 120B for conversation, Nemotron 8B for tools |

## Phase 2: Calendar & Email Awareness

**Goal:** Jess knows what's on my schedule without me telling her.

### Google Calendar sync
- Poll Google Calendar API on a schedule (every 5-15 min)
- Store upcoming events in a local cache
- New tool: `check_calendar` — "Hey Jess, what's my day look like?"
- Morning briefing: proactive announcement at configurable time with today's events, weather, reminders
- Pre-event reminders: "Nadim, your Honcho pickleball game is in 2 hours"

### Gmail monitoring
- Watch for calendar invites, flight confirmations, bill due dates
- Parse and extract: event name, date, location, deadlines
- Auto-create reminders or calendar entries
- "Hey Jess, did I get any important emails today?"

### Implementation notes
- Google OAuth2 service account or app credentials
- New `google_integration.py` module in orchestrator
- New tools: `check_calendar`, `add_calendar_event`, `check_email`
- Background scheduler (APScheduler, already used for reminders)

## Phase 3: Document Memory

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
- OCR: Tesseract or the existing Whisper model's multimodal capabilities
- Image understanding: Could route to Helios or a vision model
- Extend existing `ingest_rag.py` with format handlers
- New tool: `ingest_document` (or auto-ingest on upload)

## Phase 4: Proactive Agent (OpenClaw Integration)

**Goal:** Jess doesn't just respond — she anticipates.

### Background agent loop
- Runs continuously, checks various sources on schedules
- Gmail → new important emails → summarize and notify
- Calendar → upcoming events → pre-reminders
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

### Context awareness
- Track focus sessions → "What was I working on before lunch?"
- Time-of-day awareness → suggest dinner ideas in the evening
- Location awareness (future) → remind about errands when leaving

### OpenClaw as agent framework
- OpenClaw provides the multi-step agent loop and tool orchestration
- Brain Gateway tools become OpenClaw "skills"
- OpenClaw handles complex multi-turn tasks that single Nemotron loop can't
- Example: "Plan my week" → checks calendar, pending tasks, reminders → creates a summary

## Phase 5: Vision & Multimodal

**Goal:** See the world, not just hear it.

- Pantry photos → meal planning with dietary preferences
- Document photos → OCR → RAG (contracts, receipts, business cards)
- Whiteboard photos → extract and store notes
- Screen sharing → "Hey Jess, what am I looking at?" (future)

## Hardware Roadmap

| Device | Location | Purpose |
|--------|----------|---------|
| ATOM Echo S3R | Office (done) | Wake word + mic |
| ATOM Echo S3R #2 | Bedroom | Wake word + mic |
| ATOM Echo S3R #3 | Kitchen | Wake word + mic (meal planning, timers) |
| Google speakers | Whole house | TTS output |

## Priority Order

1. **Calendar/Gmail integration** — highest impact, unlocks morning briefings and proactive reminders
2. **Document ingestion** — builds on existing RAG, immediate utility
3. **Proactive agent** — transforms from reactive to anticipatory
4. **Vision/multimodal** — nice to have, depends on model capabilities
