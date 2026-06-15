# Frontend Dashboard (ConvivialProphet.com)

Next.js 14 + Tailwind dark theme dashboard. Docker on Jupiter (port 3001). Auth via `AUTH_TOKEN` cookie.

## Pages

| Page | Route | What |
|------|-------|------|
| Architecture | `/architecture` | Public. Interactive animated system diagram, cluster nodes, data flow, capabilities grid |
| Dashboard | `/dashboard` | Private. Calendar, reminders, selfcare today, focus timer, system health, temperature monitoring, finance snapshot |
| Chat | `/chat` | Private. Streaming SSE chat with Jess, routing badges |
| Home | `/home` | Private. HA entity controls grouped by domain (lights, switches, scenes) |
| Finance | `/finance` | Private. Gamified budget tracker with YNAB sync, XP/levels, quest board |
| Workouts | `/workouts` | Private. Today's adaptive gym plan with inline weight/reps inputs per set, "Ask Jess" button, add/remove exercises on today's plan, delete today's workout or any past workout from history, session history |
| Meals | `/meals` | Private. Today's meals with running calorie total, manual log + photo-estimate flow, 7-day bar chart |
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

Bottom nav (mobile only, `<md` breakpoint) shows 4 primary tabs — Dashboard, Chat, Meals, Workouts — plus a "More" button that opens a sheet with the rest (Shopping, Documents, Finance, Announcements, Home, Settings, Architecture, Sign Out). Active page is signaled by both color and a top brand bar so it's readable at kiosk distance. Sheet closes on Escape, backdrop click, X button, or route change; body scroll is locked while open. Honors iOS safe-area; active links use `aria-current="page"`; sheet is `role="dialog"` with `aria-modal="true"`. Sidebar (md+) lists the same set. Implemented in `frontend/src/components/layout/MobileNav.tsx` (client component), mounted from `frontend/src/app/(private)/layout.tsx`.

**Feature-flag nav gating.** The Workouts, Meals, and Finance nav links are conditional on runtime flags so disabled features don't leave dead 404 links. The server layout (`(private)/layout.tsx`) fetches `GET /api/config/features` via `getFeatureFlags()` in `lib/features.server.ts` (direct orchestrator call with the `API_TOKEN` bearer — not through `/api/proxy`, which needs the browser auth cookie a server component doesn't carry; fails open to "show all" if the orchestrator is unreachable). It filters the sidebar `NAV_ITEMS` with the pure `isNavItemEnabled(href, flags)` predicate (`lib/features.ts`) and passes the `flags` object to `<MobileNav>`, which filters both its `PRIMARY` and `MORE` lists with the same predicate. Mapping: `/workouts`→`workouts_enabled`, `/meals`→`meals_enabled`, `/finance`→`jess_advanced`. Hiding the link isn't a security boundary (the orchestrator routes 404 / are unmounted regardless) — it's UX. As defense for direct URL hits, each feature's route `layout.tsx` (`workouts/`, `meals/`, `finance/`) wraps its children in the async server component `<FeatureGate flag="..." label="...">` (`components/layout/FeatureGate.tsx`), which renders `<FeatureDisabled>` instead of the page when the flag is off. Finance's pre-existing client tabs/provider were moved to `finance/FinanceShell.tsx` so `finance/layout.tsx` could become the server gate. Flags are read with `next: { revalidate: 30 }`, so a flag flip surfaces within ~30s (or a dev-server restart) — they're env-var driven and only change on an orchestrator restart anyway.

## Finance System (YNAB Integration)

- Syncs budget data from YNAB API (`YNAB_API_TOKEN` + `YNAB_BUDGET_ID` env vars)
- Gamified: XP for under-budget months, levels, streaks, quest board
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
- `frontend/src/app/(private)/workouts/page.tsx` — Workout page: today's plan, inline set logging, history
- `frontend/src/app/(private)/meals/page.tsx` — Meals page: calorie log, photo-estimate upload, 7-day bar chart
- `frontend/src/app/(private)/settings/page.tsx` — Settings page: left-rail tab switcher with dirty-state guard on tab switch
- `frontend/src/components/settings/IdentityPanel.tsx` — Identity & Tone panel; also exports the shared `SaveBar` consumed by the other three panels
- `frontend/src/components/settings/SelfcarePanel.tsx` — Selfcare nudge cadence (categories, intervals, active hours)
- `frontend/src/components/settings/QuietHoursPanel.tsx` — Quiet hours start/end + day-of-week filter
- `frontend/src/components/settings/RecurringRemindersPanel.tsx` — CRUD UI for cron-based recurring reminder rules
- `frontend/src/lib/settings-api.ts` — Typed client for `/api/config/*`; routed through `/api/proxy` for bearer injection
- `frontend/src/lib/selfcare-categories.ts` — Shared selfcare-category constants (consumed by `SelfcarePanel`)

## API Proxy Notes

The proxy `route.ts` now handles `PATCH` in addition to `GET/POST/PUT/DELETE`. Required by both workouts (modify plan) and meals (edit entry) endpoints.
