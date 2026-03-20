# F-010: Ambient Awareness Mode

**Priority:** P2 — Nice to Have
**Status:** Done
**Depends on:** All other features (aggregates status), Frontend (done), Calendar (done), TTS (done), HA (done)
**Blocks:** None (terminal feature)

---

## ADHD Insight

ADHDers don't check things. Dashboards, apps, calendars go unvisited. Information needs to exist in the environment passively, like a clock on the wall.

## What Jess Does

Pushes ambient status to devices — periodic spoken summaries, LED color indicating schedule density, and always-on display enhancements.

---

## Components

### 1. Periodic Ambient Summaries (TTS)

Spoken status at natural intervals, not just event alerts:

```
2:00 PM: "It's 2pm. One more meeting at 3:30, then you're free.
          Top task is finishing the PR review."

4:00 PM: "It's 4. Done with meetings. No pending reminders.
          Good time to wrap up or tackle something you've been avoiding."
```

### 2. LED Status (Smart Light via HA)

| Color | Meaning |
|-------|---------|
| Green | Clear schedule, 2+ hours free |
| Yellow | Event within 1 hour |
| Red | Event within 15 minutes |
| Blue | Focus session active |
| Purple | Routine in progress |

### 3. Dashboard Ambient Mode

Enhance existing frontend for passive viewing:

- Auto-refresh all cards every 60s
- Large clock with countdown to next event
- Ambient mode toggle: hides nav, enlarges key info (for wall tablet / Callisto kiosk)
- Reduced brightness after quiet hours

---

## Implementation

### New File: `orchestrator/ambient_manager.py`

```python
async def get_ambient_status() -> dict:
    """Aggregated status for display and TTS.
    Returns:
    - schedule_density: 'clear' | 'light' | 'busy'
    - next_event: {title, start, minutes_away} or None
    - active_task: description or None
    - pending_reminders: count
    - focus_active: bool
    - routine_active: bool
    - selfcare_overdue: list of overdue items
    - led_color: computed from above
    """

async def build_ambient_summary_text() -> str:
    """2-3 sentence TTS summary from ambient status."""

async def set_ambient_led(color: str) -> None:
    """Set LED indicator via HA service call."""
```

### Background Jobs (add to `background_jobs.py`)

```python
AMBIENT_SUMMARY_TIMES = ["10:00", "12:00", "14:00", "16:00"]

async def ambient_summary():
    """Announce brief status. Keep under 3 sentences:
    - Remaining events today
    - Top active task
    - Pending reminders
    - Self-care status if overdue"""

async def update_ambient_led():
    """Every 5 min. Check schedule → set LED color via HA.
    Uses dedicated entity_id (e.g., light.status_indicator)."""
```

### API Endpoint (add to `api_routes.py`)

```
GET /api/ambient/status → ambient_manager.get_ambient_status()
```

Used by dashboard ambient mode and LED updater.

### Frontend: Ambient Mode

Add to existing dashboard:

- Toggle button → hides sidebar nav, enlarges calendar + clock + current task
- Auto-refresh interval shortened to 30s
- Suitable for Callisto kiosk or a wall-mounted tablet
- Component: `frontend/src/components/dashboard/AmbientMode.tsx`

### Modified Files

| File | Changes |
|------|---------|
| `background_jobs.py` | Add ambient summary + LED update jobs |
| `api_routes.py` | Add `/api/ambient/status` endpoint |
| `shared.py` | Add env vars |
| `frontend/` | Add ambient mode toggle + component |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `AMBIENT_ENABLED` | `true` | Enable ambient summaries |
| `AMBIENT_SUMMARY_TIMES` | `10:00,12:00,14:00,16:00` | TTS summary times |
| `AMBIENT_LED_ENTITY` | `` | HA entity for LED (empty = disabled) |
| `AMBIENT_SPEAKER` | `media_player.office_speaker` | Speaker for summaries |

---

## Testing Checklist

- [ ] Ambient summary announces at configured times
- [ ] Summary is 2-3 sentences, accurate
- [ ] LED updates based on schedule density
- [ ] LED shows blue during focus, purple during routine
- [ ] Dashboard ambient mode hides nav and enlarges info
- [ ] API endpoint returns correct aggregated status
- [ ] Quiet hours suppresses announcements
- [ ] LED entity configurable (or disabled if empty)
