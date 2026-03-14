# Frontend Dashboard (ConvivialProphet.com)

Next.js 14 + Tailwind dark theme dashboard. Docker on Jupiter (port 3001). Auth via `AUTH_TOKEN` cookie.

## Pages

| Page | Route | What |
|------|-------|------|
| Architecture | `/architecture` | Public. Interactive animated system diagram, cluster nodes, data flow, capabilities grid |
| Dashboard | `/dashboard` | Private. Calendar, reminders, focus timer, system health, temperature monitoring, finance snapshot |
| Chat | `/chat` | Private. Streaming SSE chat with Jess, routing badges |
| Home | `/home` | Private. HA entity controls grouped by domain (lights, switches, scenes) |
| Finance | `/finance` | Private. Gamified budget tracker with YNAB sync, XP/levels, quest board |

## Dashboard Widgets

- **Budget card** -> clickable, links to `/finance` page
- **System Health card** -> clickable, opens Grafana Brain Gateway Overview dashboard
- **Calendar card** -> shows merged phone+Google calendar events with source label
- **Reminders card** -> pending reminders with complete action
- **Focus timer card** -> current session with start/stop controls
- **Temperature card** -> server closet vs kitchen ambient temp, heat delta, estimated cooling cost

## Finance System (YNAB Integration)

- Syncs budget data from YNAB API (`YNAB_API_TOKEN` + `YNAB_BUDGET_ID` env vars)
- Gamified: XP for under-budget months, levels, streaks, quest board
- SQLite persistence at `/app/data/finance.db`

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
