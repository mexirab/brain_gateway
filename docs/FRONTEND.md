# Frontend Dashboard (ConvivialProphet.com)

Next.js 14 + Tailwind dark theme dashboard. Docker on Jupiter (port 3001). Auth via the `brain_token` session cookie (derived from `DASHBOARD_TOKEN` by `src/lib/dashboard-auth.ts`; see Auth below).

## Pages

| Page | Route | What |
|------|-------|------|
| Architecture | `/architecture` | Private (shows live infra status). Interactive animated system diagram (live deployment: power-tiered Helios + smart plug, brain also serves vision, ATOM Echo shown disabled, phone push / monitoring / nightly backups), cluster nodes, data flow, capabilities grid. Node cards come from `CLUSTER_NODES` (`lib/constants.ts`); status from `/api/services` via `lib/node-status.ts` — a node with no probed services (Saturn, backup target only) shows `unknown` |
| Dashboard | `/dashboard` | Private. Calendar, reminders, selfcare today, focus timer, system health, temperature monitoring, finance snapshot |
| Calendar | `/calendar` | Private. Mon–Sun week agenda of the merged calendar (personal CalDAV + work Exchange, tagged "Work") from `GET /api/calendar/range`. Prev/next week + "This week"; today highlighted and scrolled into view, today's finished events dimmed; banner when a source is unavailable; note when the browser timezone differs from Jess's; "Manage calendars" link to `CALENDAR_WEB_URL`. Never shows another week's data under a week's headings. Refreshes every 5 min. Helpers `lib/calendar-week.ts`, hook `useCalendarRange` (`lib/hooks.ts`) |
| Chat | `/chat` | Private. Streaming SSE chat with Jess, routing badges, mic input, spoken replies (speaker toggle, Stop pill, per-message replay), hands-free call mode (AudioLines button), optional 3D avatar (UserRound button; see Chat Avatar below). See `docs/VOICE_AND_TTS.md` → Dashboard chat voice replies + Hands-free call mode |
| Home | `/home` | Private. HA controls grouped by room or by type — see Home Controls below |
| Finance | `/finance` | Private. Gamified budget tracker with read-only Actual Budget sync, XP/levels, quest board |
| Workouts | `/workouts` | Private. Today's gym plan ("Where are you training?" location picker + "Next in your program" line before generating, flagging an upcoming deload week; per-exercise progression note under the name; exercise names open a how-to modal with photos + steps; "· deload week" in the header; add-exercise picker limited to the location's gear) with inline weight/reps inputs per set, "Ask Jess" button, add/remove exercises on today's plan, delete today's workout or any past workout from history, session history |
| Meals | `/meals` | Private. Today's meals with running calorie total, manual log + photo-estimate flow, 7-day bar chart |
| Pantry | `/pantry` | Private, gated on `pantry_enabled`. Tabs "What I have" (inventory by location, use/out/edit, "Still have these?" tray, add form with barcode scan), "Meal plan" (`PlanTab`: inline diet settings card `DietSettingsCard`; generate → poll → draft with warnings, missing-ingredient checklist where unticked = "already have", accept/discard, per-meal swap (one at a time), 👍/👎 ratings; active plan view) and "Stock-take" (kind picker Fridge/Freezer/Pantry/Receipt; 1–8 photos → poll → review checklist → commit. Receipt review: store · date · $total, printed text per line, "remembered" badge, restock "Add N"/"set back to full", grocery "ticks off …", location per new line, collapsible Not-food, sticky Save bar, "Nothing to add — discard"; duplicate-photo 409 offers "Open it" when the earlier scan is still open, else "Scan it anyway"). `?tab=scan` / `?tab=plan` deep-link. See `docs/PANTRY.md` |
| Settings | `/settings` | Private. Four-panel settings UI (Identity & Tone, Selfcare Nudges, Quiet Hours, Recurring Reminders) with left-rail tab switcher. Each panel has a shared `SaveBar`; switching tabs while a panel is dirty triggers a confirm guard so unsaved edits aren't silently lost. Backed by `/api/config/*` via `lib/settings-api.ts` (typed client, mirrors `finance-api.ts` shape; goes through `/api/proxy` for bearer injection). |
| Documents | `/documents` | Private. Document vault list; download/delete buttons always visible on phones (hover-only on desktop is gone), each with an `aria-label`. Downloads go through the proxy as attachments (see API Proxy Notes) |
| Personal Facts | `/personal-facts` | Private. Read-only peek at the meds/projects/profile the model reads from the YAML source of truth (`GET /api/config/personal-facts`) |

## Auth

- **Single token check:** `src/lib/dashboard-auth.ts` (`COOKIE_NAME`, `expectedDashboardToken()`, `isValidDashboardToken()`) — used by middleware, the `/api/proxy` route, `lib/auth.ts`, login/logout routes and the `(private)` layout. Don't re-read `DASHBOARD_TOKEN` anywhere else.
- **Session cookie ≠ password:** `brain_token` = HMAC-SHA256(key `DASHBOARD_TOKEN`, message `brain-gateway-dashboard-session-v1`), Web Crypto (Edge-safe), constant-time compare. Changing `DASHBOARD_TOKEN` (or bumping the `-v1` label) logs every browser out; the first deploy of this scheme logged out all pre-existing raw-password cookies once.
- **Secure flag:** set when `x-forwarded-proto` is `https` (Tailscale Serve) or `COOKIE_SECURE=true` (`src/lib/secure-cookie.ts`). `COOKIE_SECURE` is not passed through `docker-compose.yml` today — add it to the frontend `environment:` to use it.
- **Fail closed:** `DASHBOARD_TOKEN` unset, empty or `'changeme'` → nothing authenticates. `/login` (server page, `force-dynamic`) shows "not configured" up front; `POST /api/auth/login` returns 503 with a fix-it message (400 on malformed JSON, 401 on wrong password). Compose passes `${DASHBOARD_TOKEN:-}` (no default).
- **Login form:** client component `src/app/login/LoginForm.tsx`; handles 403/429, error text is `role="alert"`. Logout is a plain form POST → `303` to `/login`.
- **Login rate limit** (`src/lib/login-rate-limit.ts`, in-memory — `docker compose up -d --force-recreate frontend` clears it): attempt is reserved before the hash compare (parallel bursts can't bypass); 5 failures / 15 min per client (keyed on the LAST `X-Forwarded-For` hop — Tailscale Serve sets it to the real tailnet peer) and 30 failures / 15 min global (direct `:3001` can forge XFF); over limit → `429` + `Retry-After`. Successful logins never count against the global cap. Known trade-off: anyone who can reach `/login` can block NEW logins for 15 min; existing sessions are unaffected.
- **CSRF guard** (`src/lib/same-origin.ts`): proxy writes (`POST/PUT/PATCH/DELETE`) and `POST /api/auth/login|logout` require `Sec-Fetch-Site: same-origin|none`, or — when that header is absent — an `Origin` matching the request host; otherwise `403`. Needed because `SameSite=Lax` treats every Jupiter port and every `*.ts.net` node as same-site. **Rule: orchestrator GET routes must stay side-effect-free** — the proxy does not CSRF-check GET.
- **Expired session:** `src/components/providers/SessionExpiredWatcher.tsx` (mounted in the `(private)` layout) sends a page whose `/api/proxy` call returns 401 to `/login?redirect=<current path>`.
- **Logging** (`src/lib/server-log.ts`): content-free JSON lines `{ts, level, logger:"frontend", msg}` for proxy 403/502 (`[PROXY] …`) and login 403/429/failed (`[AUTH] …`); route logged as its first two path segments only.
- `(private)/layout.tsx` also redirects to `/login` server-side when the cookie is invalid (defense in depth for a page missing from the middleware lists; no `?redirect=` there). Private pages therefore render dynamically.
- `src/middleware.ts` redirects requests without a valid `brain_token` cookie to `/login?redirect=<path+query>` (307) — the query is kept so deep links like `/pantry?tab=scan` survive re-login.
- **Non-page paths:** `/avatar` (the chat avatar model) is also in both lists so the `.vrm` needs the cookie; it isn't a `(private)/` route, which `middleware.test.ts` allows.
- **Adding a page under `(private)/`:** add it to BOTH `PROTECTED_PATHS` in `src/lib/protected-paths.ts` (shared by middleware + login page; `middleware.ts` re-exports it) and `config.matcher` in `src/middleware.ts` (matcher must be a static literal — Next can't derive it from the array). The `(private)` route group does not protect anything on its own.
- **Login redirect:** `safeRedirect()` (`src/lib/protected-paths.ts`) honours `?redirect=` only if it parses (WHATWG URL) to a same-origin path under a protected route with no encoded `%2F`/`%5C`; anything else (`//host`, `javascript:`, unprotected paths) goes to `/dashboard`. Blocks open redirect / `javascript:` URLs on `/login`.
- `src/middleware.test.ts` (vitest) enforces this: every top-level `(private)/` route (nested route groups included) must be in both lists, the lists must match, anonymous/wrong-token requests redirect (with query preserved), a valid cookie passes. `src/lib/protected-paths.test.ts` covers `safeRedirect`. Runs in CI (`lint-frontend` job → `npm test`); locally `cd frontend && npm test`. `src/lib/dashboard-auth.test.ts` covers the placeholder/fail-closed rules and the derived cookie; `same-origin.test.ts`, `login-rate-limit.test.ts`, `content-disposition.test.ts`, `app/api/auth/{login,logout}/route.test.ts` cover the rest.

## Dashboard Widgets

- **Budget card** -> clickable, links to `/finance` page
- **System Health card** -> clickable, opens Grafana Brain Gateway Overview dashboard over HTTPS (Tailscale Serve; login required, tailnet only)
- **Calendar card** -> shows merged phone+Google calendar events with source label
- **Reminders card** -> pending reminders with complete action
- **Focus timer card** -> current session with start/stop controls
- **Temperature card** -> server closet vs room ambient temp (`sensors.ambient`; it read `kitchen` and always showed "--" before), heat delta, estimated cooling cost. Colours, bar ticks and badge follow `thresholds.{warning,critical}` from `GET /api/temperatures` (the same profile bands the closet announcements use; `src/lib/temp-band.ts`, falls back to 80/85 if absent). A sensor with no numeric reading shows "No reading"
- **Progress card** -> today's stats (tasks completed, focus minutes, brain dumps), 7-day bar chart, active streaks with flame icons, weekly trend arrow. Polls `/api/progress/today`, `/api/progress/week`, `/api/progress/streaks` every 60s
- **Announcement History card** -> recent TTS announcements with type color-coding, speaker name, success/failure icons, stats bar (success count, failures, fallbacks, avg latency). Polls `/api/announcements/history` and `/api/announcements/stats` every 30s
- **Selfcare Today card** -> today's medication/meal/water/movement counts + last-seen-ever timestamps for each. Expandable rows show today's individual entries. Mounted between Reminders and Focus Timer. Polls `/api/selfcare/today` every 30s

## Mobile Navigation

Bottom nav (mobile only, `<md` breakpoint) shows 4 primary tabs — Dashboard, Chat, Meals, Workouts — plus a "More" button that opens a sheet with the rest (Calendar, Pantry, Shopping, Documents, Finance, Announcements, Home, Settings, Architecture, Sign Out). Active page is signaled by both color and a top brand bar so it's readable at kiosk distance. Sheet closes on Escape, backdrop click, X button, or route change; body scroll is locked while open. Honors iOS safe-area; active links use `aria-current="page"`; sheet is `role="dialog"` with `aria-modal="true"`. Sidebar (md+) lists the same set (Calendar after Tasks). Implemented in `frontend/src/components/layout/MobileNav.tsx` (client component), mounted from `frontend/src/app/(private)/layout.tsx`.

**Feature-flag nav gating.** The Workouts, Meals, Pantry, and Finance nav links are conditional on runtime flags so disabled features don't leave dead 404 links. The server layout (`(private)/layout.tsx`) fetches `GET /api/config/features` via `getFeatureFlags()` in `lib/features.server.ts` (direct orchestrator call with the `API_TOKEN` bearer — not through `/api/proxy`, which needs the browser auth cookie a server component doesn't carry; fails open to "show all" if the orchestrator is unreachable). It filters the sidebar `NAV_ITEMS` with the pure `isNavItemEnabled(href, flags)` predicate (`lib/features.ts`) and passes the `flags` object to `<MobileNav>`, which filters both its `PRIMARY` and `MORE` lists with the same predicate. Mapping: `/workouts`→`workouts_enabled`, `/meals`→`meals_enabled`, `/pantry`→`pantry_enabled`, `/finance`→`jess_advanced`. Hiding the link isn't a security boundary (the orchestrator routes 404 / are unmounted regardless) — it's UX. As defense for direct URL hits, each feature's route `layout.tsx` (`workouts/`, `meals/`, `pantry/`, `finance/`) wraps its children in the async server component `<FeatureGate flag="..." label="...">` (`components/layout/FeatureGate.tsx`), which renders `<FeatureDisabled>` instead of the page when the flag is off. Finance's pre-existing client tabs/provider were moved to `finance/FinanceShell.tsx` so `finance/layout.tsx` could become the server gate. Flags are read with `next: { revalidate: 30 }`, so a flag flip surfaces within ~30s (or a dev-server restart) — they're env-var driven and only change on an orchestrator restart anyway.

**Full-height pages.** `<main>` in `(private)/layout.tsx` reserves bottom padding for the fixed MobileNav (`calc(5rem + env(safe-area-inset-bottom))` below `md`). Pages that fill the viewport (chat, documents) must use the `.page-viewport` class (`globals.css`, `@layer components`: `100dvh` with `vh` fallback, minus `<main>`'s padding) — not `calc(100vh …)`, which hid the chat input behind the nav on phones. Its offsets are coupled to `<main>`'s padding; change both together. Safe-area insets are currently 0 on iOS (root `viewport` export has no `viewportFit: 'cover'`).

## Chat Avatar

Optional 3D talking avatar on `/chat`. Off by default.

- **Toggle:** header button (UserRound icon, `aria-label="Jess avatar"`, `aria-pressed`), persisted in localStorage `chat:avatar`.
- **Layout:** panel above the messages, shrinks before them (`basis-[34%] max-h-[36dvh]`, `42dvh` on md+); hidden when viewport height ≤ 500px (phone landscape / keyboard open).
- **Rendering:** `components/chat/JessAvatar.tsx` — three.js `0.186.1` + `@pixiv/three-vrm` `3.5.5` (exact pins; `@types/three` dev), lazy-loaded via `next/dynamic` `ssr:false`, so `/chat` first-load JS is unchanged. Runs on the viewer's device GPU (no server GPU, no backend routes/metrics/env vars, no extra TTS calls).
- **Behaviour:** blinks, breathing, gaze, per-state body language + rim/halo colour for `idle | listening | thinking | speaking` (derived in `chat/page.tsx` from `speech.status`, `call.phase`, streaming). Honours `prefers-reduced-motion`. 30 fps when not speaking, full rate while speaking; rAF stops in hidden tabs. WebGL context loss / render errors → "couldn't load" message. Toggle-off disposes everything (`VRMUtils.deepDispose` + `renderer.forceContextLoss`).
- **Lip-sync:** `lib/lipsync.ts` decodes each TTS clip offline (one reused `OfflineAudioContext`) into a 50 fps viseme track (FFT spectral centroid → `aa`/`ih`/`ou`; per-clip loudness normalisation at the 65th-percentile voiced level; absolute noise floor keeps hiss closed; ~100 ms release closes the mouth between words). The avatar samples it with the reply `<audio>` element's `currentTime`. Clips arrive via `lib/speech-clip-bus.ts` (pub/sub), emitted by `ElementPlayer.play()` in `hooks/useTTSPlayback.ts`. The shared `<audio>` element is deliberately NOT routed through Web Audio (iOS gesture-unlock/playback).

**Model file:** `frontend/public/avatar/jess.vrm` — operator-supplied, gitignored (`frontend/public/avatar/*.vrm`). A fresh clone has none → panel shows a "No avatar model yet" hint.

1. Design in VRoid Studio (or use any VRM; the pixiv `VRM1_Constraint_Twist_Sample` is redistributable as a placeholder).
2. Export `.vrm` self-contained; strip author/contact meta.
3. Place at `frontend/public/avatar/jess.vrm` in the checkout (the Docker build context includes untracked files).
4. Rebuild: `docker compose up -d --build --force-recreate frontend`. Swapping models = replace the file + rebuild.

**Auth:** `/avatar` is in `PROTECTED_PATHS` + `config.matcher` (see Auth) — the model may be a personal likeness, so it needs the dashboard cookie.

**Known limits:**
- The `.vrm` is baked into the local frontend image — don't push that image to a registry.
- Not backed up by anything; keep your own copy of the source/export.
- Verified in headless Chromium only, not yet on a real iPhone.

## Home Controls (`/home`)

Reads `GET /api/ha/entities` (allowed, non-denied, not hidden in HA); every control sends structured `POST /api/ha/command` `{entity_id, service, data}`, held to the chat tool guard (`ha_guard.py`). Shapes: `TECHNICAL_REFERENCE.md` → Home Assistant.

| Area | Behavior |
|------|----------|
| Grouping | Segmented control: **Rooms** (one section per HA area; entities without an area under "Other") or **Type** (per domain). Rooms falls back to Type when `rooms_available` is false. Rooms collapsed by default; open sections + mode persisted in localStorage `home.open` / `home.mode` |
| Lights | Tiles: tap toggles; brightness slider (`turn_on` + `brightness_pct`, debounced) when `supports_brightness`; room-name prefix stripped from the label. Per-room "Lights off" |
| Scenes | One-tap chips (`scene.turn_on`); first 6 shown, then "N more" |
| Media | Play/pause only when playing/paused; power on/off; volume slider (`volume_set`, debounced); current title |
| Switches / fans | Toggle rows |
| Climate | Current temp + setpoint with −/+ (`set_temperature`) |
| Covers / locks | Read-only (open/unlock refused by the guard anyway) |
| Device settings | HA `config`/`diagnostic`-category entities, collapsed card. The only place they can be changed — chat and the NLP path refuse them (`ha_guard`, `allow_device_settings`) |
| Unavailable | `unavailable`/`unknown` entities, collapsed card (never-activated scenes, state `unknown`, stay with the scenes) |

- Optimistic updates with rollback on failure; plain-language per-control errors mapped from 401 / 403 (guard refusal) / 404 (unknown entity) / 502 (HA unreachable).
- Reconcile fetch 1.5 s after a command; 20 s poll while the tab is visible.
- `state_ok: false` or a failed fetch → banner "Can't reach Home Assistant — showing last known state".

Files: `frontend/src/app/(private)/home/page.tsx`, `frontend/src/components/home/{HomeContext,SectionCard,LightTile,SceneChips,MediaRow,ToggleRow,ClimateRow,CollapsedList}.tsx`, grouping logic `frontend/src/lib/home-groups.ts`. Tests: `lib/home-groups.test.ts`, `components/home/LightTile.test.tsx` (vitest).

## Finance System (Actual Budget sync)

- Read-only sync from the self-hosted Actual Budget server (`ACTUAL_*` env vars — `docs/ENV_VARS.md` → Finance; API — `TECHNICAL_REFERENCE.md` → Finance). Replaced YNAB 2026-10-02. Unconfigured = manual-entry mode (pages still work). Server: `actual/` compose project (26.9.0 digest-pinned), nightly backup — `docs/BACKUP.md` → Actual Budget.
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

All client-side API calls go through `/api/proxy` prefix -> Next.js auth middleware -> orchestrator `:8888`. Proxy behaviour (path allow-list, CSRF, response handling): see API Proxy Notes below.

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
- `frontend/src/components/chat/JessAvatar.tsx` — Optional 3D VRM avatar (lazy, client-only); see Chat Avatar
- `frontend/src/lib/lipsync.ts` — Offline per-clip viseme track (50 fps) for avatar lip-sync
- `frontend/src/lib/speech-clip-bus.ts` — Pub/sub for TTS clips (`useTTSPlayback` → avatar)
- `frontend/src/components/chat/VoiceCallBar.tsx` — Replaces the chat input during a call: phase label, Interrupt, End
- `frontend/src/hooks/useTTSPlayback.ts` — One persistent `HTMLAudioElement` unlocked by a silent WAV inside the user tap (iOS Safari autoplay); statuses `idle|loading|speaking|blocked|asleep|interrupted|error`; 30 s per-request timeout
- `frontend/src/lib/speech-text.ts` — Markdown/code/URL/emoji stripping, streamed sentence splitting, `splitLong()` 400-char per-request segments, 1200-char spoken budget `SPEECH_MAX_CHARS` ("The rest is on screen.")
- `frontend/src/lib/speech-queue.ts` — Per-sentence synth during streaming, ordered playback, ≤2 synth requests in flight per tab
- Tests: `frontend/src/lib/speech-text.test.ts`, `frontend/src/lib/speech-queue.test.ts`, `frontend/src/lib/vad.test.ts`, `frontend/src/lib/audio-session.test.ts`, `frontend/src/lib/lipsync.test.ts`, `frontend/src/lib/speech-clip-bus.test.ts` (vitest)
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

On Jupiter the frontend publishes on `127.0.0.1:3001` only (`FRONTEND_BIND_ADDR=127.0.0.1`, see `docs/ENV_VARS.md`), so `https://jupiter-amds.tail74fc4a.ts.net` is the only way in and plain `http://<host>:3001` does not answer off-box. Fresh installs default to `0.0.0.0` (LAN).

**Monitoring:** blackbox probes `http://brain-frontend:3001/login` over the dedicated `gateway_nerves_frontend-probe-net` network → quiet `FrontendDown` alert (5m). It does not cover the Tailscale Serve hop or the publish address. See `monitoring/README.md`.

Over plain `http://<lan-ip>:3001` (default bind) the pantry barcode scanner falls back to decoding a photo of the barcode or typed digits. Barcode decode: `@zxing/browser` 0.1.5 + `@zxing/library` 0.21.3 (pinned; iOS Safari has no `BarcodeDetector`).

## API Proxy Notes

Upstream URLs are built only by `buildUpstreamUrl()` (`src/lib/proxy-url.ts`) — the proxy attaches the `API_TOKEN` bearer, so the target must stay the orchestrator:

- First path segment must be `api`, `v1` or `health`.
- Empty, `.`/`..` segments and segments containing `/`, `\` or control chars are refused (Next hands segments over percent-decoded); each segment is re-encoded.
- Resulting origin must equal `ORCHESTRATOR_URL`'s origin.
- Anything else → `400 {"error":"Path not allowed"}`, nothing fetched. (Fixed an SSRF: `/api/proxy/%2Fhost/x` sent the bearer to another host.)
- New orchestrator path roots need adding to `ALLOWED_ROOTS`.
- Tests: `src/lib/proxy-url.test.ts`, `src/app/api/proxy/[...path]/route.test.ts` (refused paths never call `fetch`).

`route.ts` handles `GET/POST/PUT/PATCH/DELETE` through one `forward()`/`relay()` pair:

- Cookie check, then CSRF guard on writes (see Auth) → `403`.
- Upstream fetch uses `redirect: 'manual'`; any upstream 3xx → `502` (the bearer never follows a redirect).
- Request bodies: text/JSON forwarded as text, everything else as raw bytes.
- JSON responses relay as JSON, unless upstream marked them `Content-Disposition: attachment` (then they download).
- Successful non-JSON responses stream through. Inline only for raster images and audio; everything else gets `Content-Disposition: attachment` (`src/lib/content-disposition.ts`) + `Content-Security-Policy: sandbox` + `X-Content-Type-Options: nosniff` — document downloads work, and uploaded HTML/SVG can't execute as the dashboard origin.
- Non-JSON error, unreadable JSON, or orchestrator unreachable → JSON `502`.
- Headers timeout (`src/lib/proxy-timeout.ts`): no response headers within the budget → JSON `504 {"error":"Orchestrator timed out"}` + `[PROXY] Orchestrator timed out` warning. Budgets: 120 s default; 300 s for `api/meals/photo`, `api/finance/sync`, `api/finance/categories`, `api/self_audit/run`, `api/paperless/upload`; 600 s for `v1/chat/completions` (non-streaming reply sends headers only after the whole tool loop). The timer is cleared once headers arrive, so SSE streams and downloads are never cut. A slow budget must stay above the orchestrator's inner timeout, or a retry repeats work that finished anyway.
- Client disconnect (tab closed / navigated away) aborts the upstream fetch (also cancels an in-flight SSE body) and returns `499` with no warning log.

Binary responses forward the upstream `Cache-Control` only when it is `private`/`no-store` and not `public` (cookie-authed proxy — never let a shared cache store it). Used by the versioned exercise photos.

Shared `components/ui/Modal.tsx` panel is capped at `max-h-[calc(100dvh-2rem)]` with `overflow-y-auto` so tall modals scroll on phones.
