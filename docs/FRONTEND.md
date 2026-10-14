# Frontend Dashboard (ConvivialProphet.com)

Next.js 14 + Tailwind dark theme dashboard. Docker on Jupiter (port 3001). Auth via `AUTH_TOKEN` cookie.

## Pages

| Page | Route | What |
|------|-------|------|
| Architecture | `/architecture` | Public. Interactive animated system diagram, cluster nodes, data flow, capabilities grid |
| Dashboard | `/dashboard` | Private. Calendar, reminders, selfcare today, focus timer, system health, temperature monitoring, finance snapshot |
| Chat | `/chat` | Private. Streaming SSE chat with Jess, routing badges, mic input, spoken replies (speaker toggle, Stop pill, per-message replay), hands-free call mode (AudioLines button). See `docs/VOICE_AND_TTS.md` → Dashboard chat voice replies + Hands-free call mode |
| Home | `/home` | Private. HA entity controls grouped by domain (lights, switches, scenes) |
| Finance | `/finance` | Private. Gamified budget tracker with read-only Actual Budget sync, XP/levels, quest board |
| Workouts | `/workouts` | Private. Today's gym plan ("Where are you training?" location picker + "Next in your program" line before generating, flagging an upcoming deload week; per-exercise progression note under the name; exercise names open a how-to modal with photos + steps; "· deload week" in the header; add-exercise picker limited to the location's gear) with inline weight/reps inputs per set, "Ask Jess" button, add/remove exercises on today's plan, delete today's workout or any past workout from history, session history |
| Meals | `/meals` | Private. Today's meals with running calorie total, manual log + photo-estimate flow, 7-day bar chart |
| Pantry | `/pantry` | Private, gated on `pantry_enabled`. Tabs "What I have" (inventory by location, use/out/edit, "Still have these?" tray, add form with barcode scan), "Meal plan" (`PlanTab`: inline diet settings card `DietSettingsCard`; generate → poll → draft with warnings, missing-ingredient checklist where unticked = "already have", accept/discard, per-meal swap (one at a time), 👍/👎 ratings; active plan view) and "Stock-take" (kind picker Fridge/Freezer/Pantry/Receipt; 1–8 photos → poll → review checklist → commit. Receipt review: store · date · $total, printed text per line, "remembered" badge, restock "Add N"/"set back to full", grocery "ticks off …", location per new line, collapsible Not-food, sticky Save bar, "Nothing to add — discard"; duplicate-photo 409 offers "Open it" when the earlier scan is still open, else "Scan it anyway"). `?tab=scan` / `?tab=plan` deep-link. See `docs/PANTRY.md` |
| Settings | `/settings` | Private. Four-panel settings UI (Identity & Tone, Selfcare Nudges, Quiet Hours, Recurring Reminders) with left-rail tab switcher. Each panel has a shared `SaveBar`; switching tabs while a panel is dirty triggers a confirm guard so unsaved edits aren't silently lost. Backed by `/api/config/*` via `lib/settings-api.ts` (typed client, mirrors `finance-api.ts` shape; goes through `/api/proxy` for bearer injection). |

## Dashboard Widgets

- **Budget card** -> clickable, links to `/finance` page
- **System Health card** -> clickable, opens Grafana Brain Gateway Overview dashboard
- **Calendar card** -> shows merged phone+Google calendar events with source label
- **Reminders card** -> pending reminders with complete action
- **Focus timer card** -> current session with start/stop controls
- **Temperature card** -> server closet vs kitchen ambient temp, heat delta, estimated cooling cost
- **Progress card** -> today's stats (tasks completed, focus minutes, brain dumps), 7-day bar chart, active streaks with flame icons, weekly trend arrow. Polls `/api/progress/today`, `/api/progress/week`, `/api/progress/streaks` every 60s
- **Announcement History card** -> recent TTS announcements with type color-coding, speaker name, success/failure icons, stats bar (success count, failures, fallbacks, avg latency). Polls `/api/announcements/history` and `/api/announcements/stats` every 30s
- **Selfcare Today card** -> today's medication/meal/water/movement counts + last-seen-ever timestamps for each. Expandable rows show today's individual entries. Mounted between Reminders and Focus Timer. Polls `/api/selfcare/today` every 30s

## Mobile Navigation

Bottom nav (mobile only, `<md` breakpoint) shows 4 primary tabs — Dashboard, Chat, Meals, Workouts — plus a "More" button that opens a sheet with the rest (Pantry, Shopping, Documents, Finance, Announcements, Home, Settings, Architecture, Sign Out). Active page is signaled by both color and a top brand bar so it's readable at kiosk distance. Sheet closes on Escape, backdrop click, X button, or route change; body scroll is locked while open. Honors iOS safe-area; active links use `aria-current="page"`; sheet is `role="dialog"` with `aria-modal="true"`. Sidebar (md+) lists the same set. Implemented in `frontend/src/components/layout/MobileNav.tsx` (client component), mounted from `frontend/src/app/(private)/layout.tsx`.

**Feature-flag nav gating.** The Workouts, Meals, Pantry, and Finance nav links are conditional on runtime flags so disabled features don't leave dead 404 links. The server layout (`(private)/layout.tsx`) fetches `GET /api/config/features` via `getFeatureFlags()` in `lib/features.server.ts` (direct orchestrator call with the `API_TOKEN` bearer — not through `/api/proxy`, which needs the browser auth cookie a server component doesn't carry; fails open to "show all" if the orchestrator is unreachable). It filters the sidebar `NAV_ITEMS` with the pure `isNavItemEnabled(href, flags)` predicate (`lib/features.ts`) and passes the `flags` object to `<MobileNav>`, which filters both its `PRIMARY` and `MORE` lists with the same predicate. Mapping: `/workouts`→`workouts_enabled`, `/meals`→`meals_enabled`, `/pantry`→`pantry_enabled`, `/finance`→`jess_advanced`. Hiding the link isn't a security boundary (the orchestrator routes 404 / are unmounted regardless) — it's UX. As defense for direct URL hits, each feature's route `layout.tsx` (`workouts/`, `meals/`, `pantry/`, `finance/`) wraps its children in the async server component `<FeatureGate flag="..." label="...">` (`components/layout/FeatureGate.tsx`), which renders `<FeatureDisabled>` instead of the page when the flag is off. Finance's pre-existing client tabs/provider were moved to `finance/FinanceShell.tsx` so `finance/layout.tsx` could become the server gate. Flags are read with `next: { revalidate: 30 }`, so a flag flip surfaces within ~30s (or a dev-server restart) — they're env-var driven and only change on an orchestrator restart anyway.

**Full-height pages.** `<main>` in `(private)/layout.tsx` reserves bottom padding for the fixed MobileNav (`calc(5rem + env(safe-area-inset-bottom))` below `md`). Pages that fill the viewport (chat, documents) must use the `.page-viewport` class (`globals.css`, `@layer components`: `100dvh` with `vh` fallback, minus `<main>`'s padding) — not `calc(100vh …)`, which hid the chat input behind the nav on phones. Its offsets are coupled to `<main>`'s padding; change both together. Safe-area insets are currently 0 on iOS (root `viewport` export has no `viewportFit: 'cover'`).

## Finance System (Actual Budget sync)

- Read-only sync from the self-hosted Actual Budget server (`ACTUAL_*` env vars — `docs/ENV_VARS.md` → Finance; API — `TECHNICAL_REFERENCE.md` → Finance). Replaced YNAB 2026-10-02. Unconfigured = manual-entry mode (pages still work).
- Client: `lib/finance-api.ts` (`getSyncStatus`, `triggerSync`, `getBudgetCategories`, `updateCategoryMapping`, `resetSync`; errors surface the backend `{error}`), types in `lib/finance-types.ts` (`BudgetSyncStatus`; `Transaction.source` = `actual` | `ynab` (legacy) | `manual`).
- Quest board: Sync button shown whenever the sync is configured (even after a failure); a failed last sync shows "Last sync failed" linking to Finance settings; 409 → "Sync already running".
- Finance settings: Actual connection card, `last_error` alert, per-category discretionary mapping (grouped as in Actual, with budgeted/spent/balance).
- Transactions: source filter All / Synced / Manual; synced rows badged Actual or YNAB (legacy history). Discretionary dot toggles via `reclassifyTransaction` → `POST /api/finance/transactions/reclassify` (`{id, is_discretionary}`); on synced rows the choice is stored as a per-transaction override that sticks through syncs (clicking back to the category's mapping clears it); failures show an inline error.
- Gamified: XP for under-budget months, levels, streaks, quest board. Health bar remaining = Actual's Fun Money category balance (`ACTUAL_FUN_MONEY_CATEGORY`).
- SQLite persistence at `/app/data/finance.db`

## First-Boot Setup (no web wizard)

The web `/setup` wizard was deleted before v1.0.0. `frontend/src/app/setup/`, `frontend/src/components/setup/`, and `frontend/src/lib/setup-api.ts` are all gone. The 7-step browser flow (Welcome / Identity / Model / Voice / Push / Integrations / Selfcare / Review) was replaced by the 2-question express CLI wizard at `scripts/setup.sh`, which calls the same `/api/setup/*` and `/api/config/*` endpoints over `localhost:8888`. Post-install configuration of every panel (Identity, Selfcare, Quiet Hours, Routines, Speakers, Recurring Reminders) happens via `/settings`.

The `/api/setup/*` backend endpoints (`status`, `hardware`, `complete`, `env`, `env/validate`) still exist with the same first-boot-only kill-switch (HTTP 410 after `/complete`) — see `CLAUDE.md` Notes → "Setup-wizard backend".

## API Pattern

All client-side API calls go through `/api/proxy` prefix -> Next.js auth middleware -> orchestrator `:8888`.

## Deploy

```bash
# On Jupiter (or via SSH)
docker compose up -d --build --force-recreate frontend
```

**Note:** `npm run build` on host does NOT update the Docker container. Must rebuild the Docker image.

## Key Files

- `frontend/src/components/architecture/SystemDiagram.tsx` — Interactive animated SVG system architecture diagram
- `frontend/src/components/dashboard/TemperatureCard.tsx` — Server closet temperature monitoring widget
- `frontend/src/components/dashboard/ProgressCard.tsx` — Daily stats, 7-day bar chart, streaks with flame icons
- `frontend/src/components/dashboard/AnnouncementHistoryCard.tsx` — TTS announcement history with type color-coding and stats
- `frontend/src/components/dashboard/SelfcareTodayCard.tsx` — Today's selfcare log (medication/meal/water/movement) with last-seen timestamps and expandable entry rows
- `frontend/src/components/layout/MobileNav.tsx` — Mobile bottom nav (4 tabs + More sheet); used by `(private)/layout.tsx`
- `frontend/src/app/(private)/chat/page.tsx` — Chat page: SSE streaming, mic (walkie-talkie: mic-sent messages always get a spoken reply), speaker toggle (localStorage `tts_enabled`, default off), header Stop pill, sr-only aria-live voice-status region, call-mode wiring; a failed conversation-create no longer leaves the input disabled
- `frontend/src/hooks/useVoiceCall.ts` — Hands-free call loop: listen (MediaRecorder, 3 s gapless restarts) → release mic → STT → send as voice message → wait for spoken reply → re-acquire mic → listen (mic closed during replies — iOS playback quality); end conditions in `docs/VOICE_AND_TTS.md` → Hands-free call mode
- `frontend/src/components/chat/ChatInput.tsx` — Chat input + mic button; mic/STT errors shown under the input in plain language (permission blocked / needs the https address / no mic / mic busy / recording unsupported / Helios speech recognition asleep / transcription failed) with Dismiss; cleared on the next mic attempt or a typed send
- `frontend/src/hooks/useVoiceRecorder.ts` — Mic-button recorder → STT; sets the audio session to `auto` before `getUserMedia` (a `playback` session left by a call blocks capture on iOS); surfaces errors to `ChatInput` instead of swallowing them
- `frontend/src/lib/audio-session.ts` — `setAudioSessionType('auto'|'playback'|'play-and-record')` wrapper for Safari's `navigator.audioSession` (no-op without it); type is page-global and sticky — whoever sets `playback` must restore `auto`
- `frontend/src/lib/vad.ts` — `EnergyVad`: calibrated adaptive-floor energy VAD (1.8 s silence ends an utterance, 90 s cap discarded as noise)
- `frontend/src/lib/stt.ts` — `/api/stt/transcribe` client + `pickRecorderMime` (webm/opus → webm → mp4); used by call mode and `hooks/useVoiceRecorder.ts`
- `frontend/src/components/chat/VoiceCallBar.tsx` — Replaces the chat input during a call: phase label, Interrupt, End
- `frontend/src/hooks/useTTSPlayback.ts` — One persistent `HTMLAudioElement` unlocked by a silent WAV inside the user tap (iOS Safari autoplay); statuses `idle|loading|speaking|blocked|asleep|interrupted|error`; 30 s per-request timeout
- `frontend/src/lib/speech-text.ts` — Markdown/code/URL/emoji stripping, streamed sentence splitting, `splitLong()` 400-char per-request segments, 1200-char spoken budget `SPEECH_MAX_CHARS` ("The rest is on screen.")
- `frontend/src/lib/speech-queue.ts` — Per-sentence synth during streaming, ordered playback, ≤2 synth requests in flight per tab
- Tests: `frontend/src/lib/speech-text.test.ts`, `frontend/src/lib/speech-queue.test.ts`, `frontend/src/lib/vad.test.ts`, `frontend/src/lib/audio-session.test.ts` (vitest)
- `frontend/src/app/(private)/workouts/page.tsx` — Workout page: location picker, program line, today's plan, inline set logging, history
- `frontend/src/components/workouts/ExerciseInfoModal.tsx` — Exercise how-to modal: 1 s photo flip (pause/play; paused under `prefers-reduced-motion`), steps, muscles, video link, retry on error. See `docs/WORKOUTS_AND_MEALS.md`
- `frontend/src/app/(private)/meals/page.tsx` — Meals page: calorie log, photo-estimate upload, 7-day bar chart
- `frontend/src/app/(private)/pantry/page.tsx` + `components/pantry/*` — Pantry page (`InventoryTab`, `ScanTab`, `AddItemForm`, `BarcodeScanner`, `PlanTab`, `DietSettingsCard`); client `lib/pantry-api.ts` (incl. `planApi`)
- `frontend/src/lib/image-resize.ts` — `resizeImage`/`resizeAll`: downscale photos to ≤2048 px JPEG before upload (strips EXIF incl. GPS; converts HEIC in Safari). Throws on undecodable input — callers must not fall back to the original
- `frontend/src/app/(private)/settings/page.tsx` — Settings page: left-rail tab switcher with dirty-state guard on tab switch
- `frontend/src/components/settings/IdentityPanel.tsx` — Identity & Tone panel; also exports the shared `SaveBar` consumed by the other three panels
- `frontend/src/components/settings/SelfcarePanel.tsx` — Selfcare nudge cadence (categories, intervals, active hours)
- `frontend/src/components/settings/QuietHoursPanel.tsx` — Quiet hours start/end + day-of-week filter
- `frontend/src/components/settings/RecurringRemindersPanel.tsx` — CRUD UI for cron-based recurring reminder rules
- `frontend/src/components/settings/WorkoutsPanel.tsx` — Workouts panel: weekly program (incl. "Lighter deload week" cadence select + deload status in the summary) + workout locations (equipment, dumbbell/kettlebell limits). Uses `/api/workouts/*`; tab hidden when `workouts_enabled` is false (fails open). See `docs/WORKOUTS_AND_MEALS.md`
- `frontend/src/lib/settings-api.ts` — Typed client for `/api/config/*` (+ `/api/workouts/{program,locations}` for the Workouts panel); routed through `/api/proxy` for bearer injection
- `frontend/src/lib/selfcare-categories.ts` — Shared selfcare-category constants (consumed by `SelfcarePanel`)

## HTTPS (camera / microphone)

Browser camera and microphone APIs (`getUserMedia`) only work in a secure context (HTTPS or `localhost`). The dashboard is served over HTTPS on the tailnet via Tailscale Serve, run on the dashboard host:

```bash
tailscale serve --bg --https=443 http://localhost:3001   # → https://<host>.<tailnet>.ts.net
```

Over plain `http://<lan-ip>:3001` the pantry barcode scanner falls back to decoding a photo of the barcode or typed digits. Barcode decode: `@zxing/browser` 0.1.5 + `@zxing/library` 0.21.3 (pinned; iOS Safari has no `BarcodeDetector`).

## API Proxy Notes

The proxy `route.ts` now handles `PATCH` in addition to `GET/POST/PUT/DELETE`. Required by both workouts (modify plan) and meals (edit entry) endpoints.

Binary responses forward the upstream `Cache-Control` only when it is `private`/`no-store` and not `public` (cookie-authed proxy — never let a shared cache store it). Used by the versioned exercise photos.

Shared `components/ui/Modal.tsx` panel is capped at `max-h-[calc(100dvh-2rem)]` with `overflow-y-auto` so tall modals scroll on phones.
